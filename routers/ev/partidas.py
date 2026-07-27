# ============================================================
# routers/ev/partidas.py — CRUD de partidas + OTMs + importador masivo (F0.5b)
# Extraído de valor_ganado.py SIN cambios de lógica.
# ============================================================
import json  # noqa: F401
import os  # noqa: F401
from collections import defaultdict  # noqa: F401
from datetime import date, timedelta, datetime, timezone  # noqa: F401
from typing import Optional  # noqa: F401

import asyncpg  # noqa: F401
from fastapi import APIRouter, HTTPException  # noqa: F401
from pydantic import BaseModel, Field  # noqa: F401

from core.db import db  # noqa: F401
from core.log import get_logger
from core.tiempo import LIMA, semana_de as _semana_de  # noqa: F401

from routers.ev._datos import (  # noqa: F401
    _hoy_lima, _as_date, _get, _norm_tipo_costo, _norm_naturaleza, _fecha_base,
    _hh_real_por_semana, _hh_real_split, _hh_gastadas_unificada, _improductivas,
    _datos_base,
)
from routers.ev._engine import (  # noqa: F401
    _validar_pesos, _acum_a_semana, _calcular, _agrupar, _matriz_area_disciplina,
    _calc_costo_mo, _totales,
)
from routers.ev._modelos import *  # noqa: F401,F403

log = get_logger("ev")

# Sin prefijo: /ev lo aporta el router agregador (valor_ganado.py).
router = APIRouter()
router_campo = APIRouter()


# ---------------------- Catálogo de fases (disciplina) ----------------------
# `ev_partidas.fase` es el eje DISCIPLINA de la matriz Área×Disciplina y la
# clave con la que el RO cruza costo↔meta POR IGUALDAD DE STRING. Cuando era
# texto libre, una variante ('est' vs 'EST') partía el cruce en dos y
# descuadraba el Resultado Operativo sin avisar.
#
# Equivale a un Activity Code de Primavera P6 (código + nombre + color + orden):
# el catálogo `fases` (migración 0018) ya tiene esa forma; aquí se CONECTA.
def _norm_fase(v) -> Optional[str]:
    """MAYÚSCULAS + espacios colapsados, máx 20 (mismo criterio que
    routers/fases.py::_norm_codigo). None si viene vacío — un nodo PADRE del
    WBS legítimamente no tiene fase."""
    t = " ".join(str(v or "").split()).upper()
    return t[:20] or None


async def _asegurar_fases(con, codigos, proyecto_id: int = 1) -> list:
    """Da de alta en el catálogo las fases usadas que aún no existan.

    Auto-alta (no rechazo) a propósito: una importación de partidas nuevas no
    debe bloquearse porque la disciplina todavía no esté en el catálogo. Se
    devuelven las creadas para poder AVISAR en la respuesta, y oficina las
    completa después (nombre/color/orden) desde la Guía de Fases.
    """
    nuevas = []
    for c in sorted({c for c in codigos if c}):
        creada = await con.fetchval(
            """INSERT INTO fases (proyecto_id, codigo, nombre, orden)
               VALUES ($1, $2, $3, 999)
               ON CONFLICT (proyecto_id, codigo) DO NOTHING
               RETURNING codigo""",
            proyecto_id, c, f"Fase {c}")
        if creada:
            nuevas.append(creada)
    if nuevas:
        log.info("fases_autocreadas", extra={"fases": nuevas, "proyecto_id": proyecto_id})
    return nuevas


def _wbs_de(codigo: str, nivel=None, parent_codigo=None) -> tuple:
    """Lugar de la partida en el WBS: (nivel, parent_codigo).

    Misma regla que el importador de Excel: el separador del código define la
    jerarquía ('03.02.15' cuelga de '03.02'). Se calcula también en el alta de
    a una (antes solo lo hacía /ev/importar): sin nivel/parent la partida nace
    huérfana y `buildWbsTree` del panel la trata como RAÍZ, así que el planner
    la ve suelta al final del árbol en vez de dentro de su fase.

    Un parent_codigo explícito (selector «cuelga de») manda sobre el código."""
    cod = str(codigo or "").strip()
    sep = "." if "." in cod else ","
    partes = [p for p in cod.split(sep) if p]
    padre = str(parent_codigo or "").strip() or None
    if padre is None and len(partes) > 1:
        padre = sep.join(partes[:-1])
    if nivel:
        return int(nivel), padre
    # Con padre explícito el nivel se cuenta sobre ÉL (el código puede no
    # seguir la numeración del padre: un adicional 'ADIC-01' bajo '03.02').
    if padre:
        psep = "." if "." in padre else ","
        return len([p for p in padre.split(psep) if p]) + 1, padre
    return max(1, len(partes)), padre


_PREFIJO_ADICIONAL = "ADIC"


def _siguiente_codigo(codigos, parent_codigo=None, naturaleza=None) -> str:
    """Propone el código que le toca a una partida nueva.

    · CONTRACTUAL con padre '03.02' → el correlativo que sigue entre SUS hijos
      directos ('03.02.07' si el último es '03.02.06'), respetando el ancho de
      los ceros que ya usa el presupuesto.
    · CONTRACTUAL sin padre → el correlativo de las raíces ('04' tras '03').
    · ADICIONAL → su propia serie 'ADIC-01', 'ADIC-02'… porque un adicional no
      pertenece a la numeración del contrato (y así se reconoce de un vistazo).

    Los tramos no numéricos se ignoran al buscar el máximo: un 'ADIC-01' suelto
    dentro de la jerarquía no rompe el correlativo del contrato."""
    if str(naturaleza or "").strip().upper() == "ADICIONAL":
        usados = []
        for c in codigos:
            t = str(c or "").strip().upper()
            if t.startswith(_PREFIJO_ADICIONAL + "-"):
                suf = t[len(_PREFIJO_ADICIONAL) + 1:]
                if suf.isdigit():
                    usados.append((int(suf), len(suf)))
        if not usados:
            return f"{_PREFIJO_ADICIONAL}-01"
        top = max(usados)
        return f"{_PREFIJO_ADICIONAL}-{top[0] + 1:0{max(top[1], 2)}d}"

    padre = str(parent_codigo or "").strip()
    if padre:
        # El separador se deduce del padre; si el padre es de un solo tramo
        # ('01') no dice nada, así que se mira el presupuesto ya cargado y, en
        # último caso, se usa el punto (lo habitual).
        if "." in padre:
            sep = "."
        elif "," in padre:
            sep = ","
        else:
            hay = [str(c or "") for c in codigos]
            sep = "," if (any("," in c for c in hay)
                          and not any("." in c for c in hay)) else "."
        pref = padre + sep
        n_padre = len([x for x in padre.split(sep) if x])
        hijos = []
        for c in codigos:
            t = str(c or "").strip()
            if not t.startswith(pref):
                continue
            tramos = [x for x in t.split(sep) if x]
            if len(tramos) == n_padre + 1 and tramos[-1].isdigit():
                hijos.append((int(tramos[-1]), len(tramos[-1])))
        if not hijos:
            return f"{padre}{sep}01"
        top = max(hijos)
        return f"{padre}{sep}{top[0] + 1:0{max(top[1], 2)}d}"

    # Raíces: los códigos de UN solo tramo (el separador se toma del que exista).
    raices = []
    for c in codigos:
        t = str(c or "").strip()
        if t.isdigit():
            raices.append((int(t), len(t)))
    if not raices:
        return "01"
    top = max(raices)
    return f"{top[0] + 1:0{max(top[1], 2)}d}"


@router.get("/partidas/siguiente-codigo")
async def siguiente_codigo(otm: Optional[str] = None, parent_codigo: Optional[str] = None,
                           naturaleza: Optional[str] = None):
    """Correlativo sugerido para una partida nueva, dentro de su OTM.

    Es una SUGERENCIA: el planner puede sobrescribirla. Se calcula sobre las
    partidas de la misma OTM (o del cajón «(SIN)» si aún no tiene), que es el
    ámbito donde el código debe ser único."""
    pool = await db()
    async with pool.acquire() as con:
        if otm:
            rows = await con.fetch(
                "SELECT codigo FROM ev_partidas WHERE activo AND otm_id=$1", otm)
        else:
            rows = await con.fetch(
                "SELECT codigo FROM ev_partidas WHERE activo AND otm_id IS NULL")
    codigo = _siguiente_codigo([r["codigo"] for r in rows], parent_codigo, naturaleza)
    return {"codigo": codigo, "otm_id": otm, "parent_codigo": parent_codigo or None,
            "naturaleza": (naturaleza or "CONTRACTUAL").upper()}


# ---------------------- CRUD Partidas ----------------------
@router.get("/partidas")
async def listar_partidas(otm: Optional[str] = None, sin_otm: bool = False):
    """Partidas activas. `otm=` acota a una OTM; `sin_otm=true` devuelve las
    que están en el cajón «(SIN)» — creadas sin saber todavía a qué obra van
    (misceláneos: muchos proyectitos, cada uno con su metrado y HH). Son las
    que la bandeja «por ubicar» del panel resuelve después."""
    pool = await db()
    async with pool.acquire() as con:
        if sin_otm:
            partidas = await con.fetch(
                "SELECT * FROM ev_partidas WHERE activo AND otm_id IS NULL ORDER BY codigo")
        elif otm:
            partidas = await con.fetch(
                "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
            )
        else:
            partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(dict(h))
    return [{**dict(p), "hitos": por_partida.get(p["id"], [])} for p in partidas]


@router.get("/otms")
async def listar_otms_ev():
    """TODAS las OTMs registradas, con su cantidad de partidas en el módulo EV (0 si aún no tiene)."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT o.id AS otm_id, o.descripcion, o.estado,
                      COUNT(p.id) FILTER (WHERE p.activo) AS partidas
               FROM otms o
               LEFT JOIN ev_partidas p ON p.otm_id = o.id
               GROUP BY o.id, o.descripcion, o.estado
               ORDER BY o.id"""
        )
    return [dict(r) for r in rows]


@router.post("/partidas")
async def crear_partida(body: PartidaIn):
    _validar_pesos(body.hitos)
    body.fase = _norm_fase(body.fase)
    body.sub_fase = _norm_fase(body.sub_fase)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await _asegurar_fases(con, [body.fase])
            nivel, parent_codigo = _wbs_de(body.codigo, body.nivel, body.parent_codigo)
            # Herencia del ÁREA del proyecto, igual que en /ev/importar: el alta
            # de una partida en pleno LookAhead no pregunta el área, y sin ella
            # la partida se cae de la matriz Área × Disciplina.
            sistema = body.sistema
            if not sistema and body.otm_id:
                sistema = await con.fetchval(
                    "SELECT NULLIF(area, '') FROM otms WHERE id=$1", body.otm_id)
            try:
                # naturaleza / tipo_costo se persisten (antes se caían al
                # default): así un ADICIONAL creado desde Programación queda
                # marcado como tal y el ISP puede separarlo del contractual.
                pid = await con.fetchval(
                    """INSERT INTO ev_partidas
                       (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                        metrado_presup, metrado_proyec, hh_presup, hh_actualizado,
                        naturaleza, tipo_costo, nivel, parent_codigo, precio_unitario)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                               COALESCE($16, 0)) RETURNING id""",
                    body.codigo, body.otm_id, body.fase, body.sub_fase, body.descripcion,
                    body.unidad, sistema, body.metrado_presup,
                    body.metrado_proyec, body.hh_presup, body.hh_actualizado,
                    _norm_naturaleza(body.naturaleza), _norm_tipo_costo(body.tipo_costo),
                    nivel, parent_codigo, body.precio_unitario,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    409, f"Ya existe una partida con código {body.codigo} "
                         f"en la OTM {body.otm_id or '(sin OTM)'}")
            for h in body.hitos:
                await con.execute(
                    """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
                       VALUES ($1,$2,$3,$4,$5)""",
                    pid, h.numero, h.descripcion, h.peso, h.es_principal,
                )
    return {"id": pid, "ok": True}


@router.put("/partidas/{partida_id}")
async def actualizar_partida(partida_id: int, body: PartidaIn):
    _validar_pesos(body.hitos)
    body.fase = _norm_fase(body.fase)
    body.sub_fase = _norm_fase(body.sub_fase)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await _asegurar_fases(con, [body.fase])
            prev = await con.fetchrow(
                "SELECT nivel, parent_codigo FROM ev_partidas WHERE id=$1", partida_id)
            if prev is None:
                raise HTTPException(404, "Partida no encontrada")
            # El WBS solo se toca si el cliente lo manda explícitamente o si la
            # partida venía huérfana (nivel/parent NULL): así una edición normal
            # nunca reubica sola una partida que el importador ya colgó bien.
            if body.nivel or body.parent_codigo:
                nivel, parent_codigo = _wbs_de(body.codigo, body.nivel, body.parent_codigo)
            elif prev["nivel"] is None and prev["parent_codigo"] is None:
                nivel, parent_codigo = _wbs_de(body.codigo)
            else:
                nivel, parent_codigo = prev["nivel"], prev["parent_codigo"]
            try:
                # PU con COALESCE: quien no lo manda (todo cliente anterior a
                # esta versión) no puede borrar la venta que dejó el congelado
                # del presupuesto contractual.
                res = await con.execute(
                    """UPDATE ev_partidas SET codigo=$2, otm_id=$3, fase=$4, sub_fase=$5,
                       descripcion=$6, unidad=$7, sistema=$8, metrado_presup=$9,
                       metrado_proyec=$10, hh_presup=$11, hh_actualizado=$12,
                       nivel=$13, parent_codigo=$14,
                       precio_unitario=COALESCE($15, precio_unitario) WHERE id=$1""",
                    partida_id, body.codigo, body.otm_id, body.fase, body.sub_fase,
                    body.descripcion, body.unidad, body.sistema,
                    body.metrado_presup, body.metrado_proyec, body.hh_presup,
                    body.hh_actualizado, nivel, parent_codigo, body.precio_unitario,
                )
            except asyncpg.UniqueViolationError:
                # El código es único POR OTM (índice uq_partida_otm_codigo, 0008):
                # mover la partida a una OTM que ya lo usa choca aquí.
                raise HTTPException(
                    409, f"La OTM {body.otm_id or '(sin OTM)'} ya tiene una partida "
                         f"con código {body.codigo}: cámbiale el código antes de moverla")
            if res == "UPDATE 0":
                raise HTTPException(404, "Partida no encontrada")
            existentes = await con.fetch(
                "SELECT id, numero FROM ev_hitos WHERE partida_id=$1", partida_id
            )
            por_numero = {e["numero"]: e["id"] for e in existentes}
            nuevos = {h.numero for h in body.hitos}
            for e in existentes:
                if e["numero"] not in nuevos:
                    await con.execute("DELETE FROM ev_hitos WHERE id=$1", e["id"])
            for h in body.hitos:
                if h.numero in por_numero:
                    await con.execute(
                        "UPDATE ev_hitos SET descripcion=$2, peso=$3, es_principal=$4 WHERE id=$1",
                        por_numero[h.numero], h.descripcion, h.peso, h.es_principal,
                    )
                else:
                    await con.execute(
                        """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
                           VALUES ($1,$2,$3,$4,$5)""",
                        partida_id, h.numero, h.descripcion, h.peso, h.es_principal,
                    )
    return {"ok": True}


@router.get("/partidas-por-ubicar")
async def partidas_por_ubicar():
    """Bandeja de partidas creadas sin saber todavía a qué OTM van (cajón
    «(SIN)» del índice uq_partida_otm_codigo) más las que sí tienen OTM pero
    quedaron huérfanas del WBS. Las dos se arreglan con /partidas/{id}/ubicar.

    El caso que la motiva (Jean, misceláneos): muchos proyectitos con su propio
    metrado y HH contractual, donde al programar la actividad todavía no se
    sabe bajo qué OTM cae. Mientras la partida está sin OTM NO aparece en el
    selector de partidas de una actividad con OTM, así que esta lista es lo que
    evita que se queden olvidadas.

    SIN_PU (2026-07-27): la venta del RO es Σ(cantidad_valorizada × PU), así que
    una partida sin PU entra al resultado como COSTO PURO SIN VENTA. Solo se
    reclama en las que YA tienen actividad programada: esas van a consumir HH
    esta semana. Una partida sin PU que nadie trabaja todavía no es un problema
    y llenaría la bandeja de ruido (el PU llega al congelar el contractual)."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT p.id, p.codigo, p.descripcion, p.unidad, p.fase, p.otm_id,
                      p.metrado_presup, p.hh_presup, p.naturaleza, p.nivel, p.parent_codigo,
                      p.precio_unitario,
                      (SELECT count(*) FROM prog_actividades a WHERE a.partida_id = p.id) AS actividades
               FROM ev_partidas p
               WHERE p.activo
                 AND (p.otm_id IS NULL
                      -- «suelta del WBS» solo si el CÓDIGO anuncia jerarquía: una
                      -- partida plana ('E2E-001') o un nodo raíz ('01') tienen
                      -- parent NULL con toda razón y no son un problema.
                      OR (p.parent_codigo IS NULL
                          AND (p.codigo LIKE '%.%' OR p.codigo LIKE '%,%'))
                      -- sin HH solo cuenta en las HOJAS (fase NOT NULL): un nodo
                      -- padre del WBS no tiene presupuesto propio.
                      OR (p.fase IS NOT NULL AND COALESCE(p.hh_presup, 0) <= 0)
                      OR (p.fase IS NOT NULL AND COALESCE(p.precio_unitario, 0) <= 0
                          AND EXISTS (SELECT 1 FROM prog_actividades a
                                      WHERE a.partida_id = p.id)))
               ORDER BY p.otm_id NULLS FIRST, p.codigo""")
    out = []
    for r in rows:
        motivos = []
        if r["otm_id"] is None:
            motivos.append("SIN_OTM")
        if r["parent_codigo"] is None and ("." in r["codigo"] or "," in r["codigo"]):
            motivos.append("SIN_PADRE")
        if r["fase"] is not None and not float(r["hh_presup"] or 0):
            motivos.append("SIN_HH")
        if (r["fase"] is not None and not float(r["precio_unitario"] or 0)
                and int(r["actividades"] or 0) > 0):
            motivos.append("SIN_PU")
        out.append({**dict(r), "metrado_presup": float(r["metrado_presup"] or 0),
                    "hh_presup": float(r["hh_presup"] or 0),
                    "precio_unitario": float(r["precio_unitario"] or 0),
                    "motivos": motivos})
    return out


_COMPLETABLES = ("hh_presup", "precio_unitario", "metrado_presup")


def _campos_completar(body: dict) -> dict:
    """Los campos que la bandeja manda a rellenar, validados. PURA (testeable).

    Un campo ausente o None NO se toca: la bandeja edita una celda a la vez y
    no puede borrar de rebote las otras dos."""
    campos = {}
    for k in _COMPLETABLES:
        if body.get(k) is None:
            continue
        try:
            v = float(body[k])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{k} debe ser un número")
        if v < 0:
            raise HTTPException(400, f"{k} no puede ser negativo")
        campos[k] = v
    if not campos:
        raise HTTPException(
            400, "Nada que completar: manda hh_presup, precio_unitario o metrado_presup")
    return campos


@router.put("/partidas/{partida_id}/completar")
async def completar_partida(partida_id: int, body: dict):
    """Rellena desde la bandeja los datos que le faltan a una partida creada en
    obra: `hh_presup`, `precio_unitario`, `metrado_presup`. Un campo ausente NO
    se toca (a diferencia del PUT completo, que además exige mandar los hitos y
    los reescribe): esto es edición en línea de una celda, no una edición de la
    partida."""
    campos = _campos_completar(body)
    sets = ", ".join(f"{k}=${i}" for i, k in enumerate(campos, start=2))
    pool = await db()
    async with pool.acquire() as con:
        res = await con.execute(
            f"UPDATE ev_partidas SET {sets} WHERE id=$1 AND activo", partida_id, *campos.values())
    if res == "UPDATE 0":
        raise HTTPException(404, "Partida no encontrada")
    return {"ok": True, "actualizado": list(campos)}


@router.put("/partidas/{partida_id}/ubicar")
async def ubicar_partida(partida_id: int, body: dict):
    """Le da OTM y lugar en el WBS a una partida de la bandeja, en un gesto.

    body: {otm_id, parent_codigo?, codigo?}. `codigo` permite renombrarla en el
    mismo movimiento cuando la OTM destino ya usa ese código (el índice es
    único por OTM) — de otro modo el planner tendría que ir a editarla aparte.
    No toca hitos, avances ni HH: solo la ubica."""
    otm_id = str(body.get("otm_id") or "").strip() or None
    if not otm_id:
        raise HTTPException(400, "otm_id requerido: elige la OTM a la que pertenece")
    parent = str(body.get("parent_codigo") or "").strip() or None
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            p = await con.fetchrow(
                "SELECT codigo, otm_id FROM ev_partidas WHERE id=$1 AND activo", partida_id)
            if not p:
                raise HTTPException(404, "Partida no encontrada")
            if not await con.fetchval("SELECT 1 FROM otms WHERE id=$1", otm_id):
                raise HTTPException(400, f"La OTM {otm_id} no existe")
            codigo = str(body.get("codigo") or "").strip() or p["codigo"]
            if parent and not await con.fetchval(
                    "SELECT 1 FROM ev_partidas WHERE codigo=$1 AND otm_id=$2 AND activo",
                    parent, otm_id):
                raise HTTPException(
                    400, f"La OTM {otm_id} no tiene ninguna partida con código {parent} "
                         f"de la cual colgar esta")
            nivel, parent_codigo = _wbs_de(codigo, None, parent)
            try:
                await con.execute(
                    """UPDATE ev_partidas SET otm_id=$2, codigo=$3, nivel=$4, parent_codigo=$5
                       WHERE id=$1""", partida_id, otm_id, codigo, nivel, parent_codigo)
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    409, f"La OTM {otm_id} ya tiene una partida con código {codigo}: "
                         f"dale otro código a esta para poder ubicarla")
    return {"ok": True, "id": partida_id, "otm_id": otm_id, "codigo": codigo,
            "nivel": nivel, "parent_codigo": parent_codigo}


async def _uso_de_partida(con, partida_id: int) -> dict:
    """Qué hay colgado de la partida. Es lo que decide si se puede quitar de en
    medio sin perder rastro de trabajo real."""
    r = await con.fetchrow(
        """SELECT
             (SELECT count(*) FROM prog_actividades WHERE partida_id = $1) AS actividades,
             (SELECT count(*) FROM ev_avances_diarios
               WHERE partida_id = $1 AND cantidad_dia IS NOT NULL) AS dias_avance,
             (SELECT COALESCE(SUM(hh), 0) FROM tareo_partida WHERE partida_id = $1) AS hh_tareo,
             (SELECT count(*) FROM ev_avances a JOIN ev_hitos h ON h.id = a.hito_id
               WHERE h.partida_id = $1) AS avances_semanales,
             (SELECT count(*) FROM costo_documentos WHERE partida_id = $1) AS documentos_costo""",
        partida_id)
    return {"actividades": r["actividades"], "dias_avance": r["dias_avance"],
            "hh_tareo": float(r["hh_tareo"] or 0),
            "avances_semanales": r["avances_semanales"],
            "documentos_costo": r["documentos_costo"]}


@router.get("/partidas/{partida_id}/uso")
async def uso_partida(partida_id: int):
    """Qué se perdería de vista al quitar la partida — el panel lo muestra antes
    de dejar desactivarla."""
    pool = await db()
    async with pool.acquire() as con:
        if not await con.fetchval("SELECT 1 FROM ev_partidas WHERE id=$1", partida_id):
            raise HTTPException(404, "Partida no encontrada")
        return await _uso_de_partida(con, partida_id)


@router.delete("/partidas/{partida_id}")
async def eliminar_partida(partida_id: int, desactivar: bool = False):
    """Quita la partida de las listas (`activo = false`; nunca se borra la fila:
    el avance y las HH ya registradas son historia real).

    Si tiene trabajo colgado se responde **409 explicando qué**, en vez de
    hacerlo callado: desactivar una partida con actividades programadas o con
    avance registrado deja huecos difíciles de rastrear después. Para hacerlo de
    todos modos, `?desactivar=true` — es una decisión, no un descuido."""
    pool = await db()
    async with pool.acquire() as con:
        if not await con.fetchval("SELECT 1 FROM ev_partidas WHERE id=$1", partida_id):
            raise HTTPException(404, "Partida no encontrada")
        uso = await _uso_de_partida(con, partida_id)
        if not desactivar and any(v for v in uso.values()):
            detalle = ", ".join(t for t in (
                f"{uso['actividades']} actividad(es) programadas" if uso["actividades"] else "",
                f"{uso['dias_avance']} día(s) con avance" if uso["dias_avance"] else "",
                f"{uso['hh_tareo']:g} HH de tareo" if uso["hh_tareo"] else "",
                f"{uso['avances_semanales']} avance(s) semanales" if uso["avances_semanales"] else "",
                f"{uso['documentos_costo']} documento(s) de costo" if uso["documentos_costo"] else "",
            ) if t)
            raise HTTPException(
                409, f"Esta partida tiene trabajo registrado ({detalle}). "
                     f"Si aun así quieres sacarla de las listas, confirma para desactivarla: "
                     f"sus datos se conservan pero dejará de aparecer.")
        await con.execute("UPDATE ev_partidas SET activo=FALSE WHERE id=$1", partida_id)
    return {"ok": True, "desactivada": True, "uso": uso}


# ---------------------- Importador masivo ----------------------
@router.post("/importar")
async def importar(body: ImportarIn):
    """Carga masiva en UNA transacción: partidas (upsert por código) +
    histórico opcional de avances y HH. Si una fila falla, nada se guarda."""
    pool = await db()
    creadas, actualizadas = 0, 0
    errores: list[str] = []

    async with pool.acquire() as con:
        # Herencia del ÁREA del proyecto (decisión Jean 2026-07-18): la plantilla
        # ya no pide área/sistema — si la fila no lo trae, la partida toma el
        # área del proyecto y la matriz Área×Disciplina sigue funcionando.
        areas_proy = {r["id"]: r["area"] for r in await con.fetch(
            "SELECT id, area FROM otms WHERE area IS NOT NULL AND area <> ''")}

        async with con.transaction():
            codigo_a_id: dict[str, int] = {}

            for i, p in enumerate(body.partidas, start=1):
                # Calcular nivel y parent_codigo si no vienen en el payload
                sep = '.' if '.' in p.codigo else ','
                nivel = p.nivel or len(p.codigo.split(sep))
                parent_codigo = p.parent_codigo
                if parent_codigo is None and nivel > 1:
                    parent_codigo = sep.join(p.codigo.split(sep)[:-1])
                tipo_costo = _norm_tipo_costo(p.tipo_costo)
                naturaleza = _norm_naturaleza(p.naturaleza)
                # Disciplina normalizada ANTES de decidir si el nodo es padre
                # (fase None = nodo padre del WBS, sin hitos).
                p.fase = _norm_fase(p.fase)
                p.sub_fase = _norm_fase(p.sub_fase)
                if not p.sistema and p.otm_id:
                    p.sistema = areas_proy.get(p.otm_id)

                # Resolver hitos según tipo de nodo
                if p.fase is None:
                    # Nodo PADRE del WBS: sin hitos (rollup calculado desde hijos)
                    hitos = []
                elif p.hitos:
                    try:
                        hitos = [HitoIn(**h.model_dump()) for h in p.hitos]
                        _validar_pesos(hitos)
                    except HTTPException as e:
                        errores.append(f"Fila {i} ({p.codigo}): {e.detail}"); continue
                else:
                    # Sin hitos declarados (HITO1..5): hito único 'Ejecución'
                    # 100% — la misma convención del hito principal silencioso
                    # (0025). tipo_actividad de archivos viejos se ignora
                    # (Fase S: la tabla de plantillas de hitos se retiró).
                    hitos = [HitoIn(numero=1, descripcion="Ejecución",
                                    peso=1.0, es_principal=True)]

                # 0008: el código es único POR OTM — resolver siempre con la pareja (codigo, otm)
                existente = await con.fetchval(
                    "SELECT id FROM ev_partidas WHERE codigo=$1 AND otm_id IS NOT DISTINCT FROM $2",
                    p.codigo, p.otm_id,
                )
                if existente:
                    # precio_unitario con COALESCE: la plantilla de importación
                    # no trae PU, así que un re-import no puede borrar la venta
                    # que dejó el congelado del presupuesto contractual.
                    await con.execute(
                        """UPDATE ev_partidas SET otm_id=$2, fase=$3, sub_fase=$4, descripcion=$5,
                           unidad=$6, sistema=$7, metrado_presup=$8, metrado_proyec=$9,
                           hh_presup=$10, nivel=$11, parent_codigo=$12, tipo_costo=$13,
                           naturaleza=$14, hh_actualizado=$15,
                           precio_unitario=COALESCE($16, precio_unitario),
                           activo=TRUE WHERE id=$1""",
                        existente, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo, tipo_costo, naturaleza, p.hh_actualizado,
                        p.precio_unitario,
                    )
                    await con.execute("DELETE FROM ev_hitos WHERE partida_id=$1", existente)
                    pid = existente; actualizadas += 1
                else:
                    pid = await con.fetchval(
                        """INSERT INTO ev_partidas
                           (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                            metrado_presup, metrado_proyec, hh_presup, nivel, parent_codigo,
                            tipo_costo, naturaleza, hh_actualizado, precio_unitario)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                                   COALESCE($16, 0)) RETURNING id""",
                        p.codigo, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo, tipo_costo, naturaleza, p.hh_actualizado,
                        p.precio_unitario,
                    )
                    creadas += 1
                codigo_a_id[p.codigo] = pid
                for h in hitos:
                    await con.execute(
                        """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
                           VALUES ($1,$2,$3,$4,$5)""",
                        pid, h.numero, h.descripcion, h.peso, h.es_principal,
                    )

            if errores:
                raise HTTPException(400, {"errores": errores})

            # Disciplinas nuevas → alta automática en el catálogo, para que la
            # matriz Área×Disciplina y el cruce del RO nunca vean un string
            # huérfano. Se informan para que oficina las complete.
            fases_nuevas = await _asegurar_fases(
                con, [p.fase for p in body.partidas])

            # mapa hito (partida, numero) -> id para el histórico
            hitos_db = await con.fetch("SELECT id, partida_id, numero FROM ev_hitos")
            hito_id = {(h["partida_id"], h["numero"]): h["id"] for h in hitos_db}

            async def _pid_por_codigo(codigo: str):
                """Resuelve un código NO incluido en este import. Con unicidad por OTM (0008)
                el código a secas puede ser ambiguo → error explícito en vez de adivinar."""
                filas = await con.fetch("SELECT id FROM ev_partidas WHERE codigo=$1", codigo)
                if len(filas) > 1:
                    return "AMBIGUO"
                return filas[0]["id"] if filas else None

            av_ins, hist_err = 0, []
            for a in body.avances:
                pid = codigo_a_id.get(a.codigo) or await _pid_por_codigo(a.codigo)
                if pid == "AMBIGUO":
                    hist_err.append(f"Avance: código {a.codigo} existe en varias OTMs — "
                                    "incluye la partida en el import para desambiguar")
                    continue
                if not pid:
                    hist_err.append(f"Avance: código {a.codigo} no existe")
                    continue
                hid = hito_id.get((pid, a.hito))
                if not hid:
                    hist_err.append(f"Avance: {a.codigo} no tiene hito {a.hito}")
                    continue
                await con.execute(
                    """INSERT INTO ev_avances (hito_id, semana, cantidad_acum)
                       VALUES ($1,$2,$3)
                       ON CONFLICT (hito_id, semana) DO UPDATE SET cantidad_acum=$3""",
                    hid, a.semana, a.cantidad_acum,
                )
                av_ins += 1

            hh_ins = 0
            for r in body.hh:
                pid = codigo_a_id.get(r.codigo) or await _pid_por_codigo(r.codigo)
                if pid == "AMBIGUO":
                    hist_err.append(f"HH: código {r.codigo} existe en varias OTMs — "
                                    "incluye la partida en el import para desambiguar")
                    continue
                if not pid:
                    hist_err.append(f"HH: código {r.codigo} no existe")
                    continue
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1,$2,$3,'importado')
                       ON CONFLICT (partida_id, semana) DO UPDATE SET hh=$3""",
                    pid, r.semana, r.hh,
                )
                hh_ins += 1

            if hist_err:
                raise HTTPException(400, {"errores": hist_err})

    return {
        "ok": True,
        "partidas_creadas": creadas,
        "partidas_actualizadas": actualizadas,
        "avances_importados": av_ins,
        "hh_importadas": hh_ins,
        # Disciplinas dadas de alta solas: oficina debería ponerles nombre y
        # color en la Guía de Fases (salen como "Fase XX" hasta entonces).
        "fases_nuevas": fases_nuevas,
    }


