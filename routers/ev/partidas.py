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
            try:
                # naturaleza / tipo_costo se persisten (antes se caían al
                # default): así un ADICIONAL creado desde Programación queda
                # marcado como tal y el ISP puede separarlo del contractual.
                pid = await con.fetchval(
                    """INSERT INTO ev_partidas
                       (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                        metrado_presup, metrado_proyec, hh_presup, hh_actualizado,
                        naturaleza, tipo_costo, nivel, parent_codigo)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id""",
                    body.codigo, body.otm_id, body.fase, body.sub_fase, body.descripcion,
                    body.unidad, body.sistema, body.metrado_presup,
                    body.metrado_proyec, body.hh_presup, body.hh_actualizado,
                    _norm_naturaleza(body.naturaleza), _norm_tipo_costo(body.tipo_costo),
                    nivel, parent_codigo,
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
                res = await con.execute(
                    """UPDATE ev_partidas SET codigo=$2, otm_id=$3, fase=$4, sub_fase=$5,
                       descripcion=$6, unidad=$7, sistema=$8, metrado_presup=$9,
                       metrado_proyec=$10, hh_presup=$11, hh_actualizado=$12,
                       nivel=$13, parent_codigo=$14 WHERE id=$1""",
                    partida_id, body.codigo, body.otm_id, body.fase, body.sub_fase,
                    body.descripcion, body.unidad, body.sistema,
                    body.metrado_presup, body.metrado_proyec, body.hh_presup,
                    body.hh_actualizado, nivel, parent_codigo,
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
    evita que se queden olvidadas."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT p.id, p.codigo, p.descripcion, p.unidad, p.fase, p.otm_id,
                      p.metrado_presup, p.hh_presup, p.naturaleza, p.nivel, p.parent_codigo,
                      (SELECT count(*) FROM prog_actividades a WHERE a.partida_id = p.id) AS actividades
               FROM ev_partidas p
               WHERE p.activo AND (p.otm_id IS NULL OR p.parent_codigo IS NULL)
               ORDER BY p.otm_id NULLS FIRST, p.codigo""")
    out = []
    for r in rows:
        motivos = []
        if r["otm_id"] is None:
            motivos.append("SIN_OTM")
        if r["parent_codigo"] is None:
            motivos.append("SIN_PADRE")
        if not float(r["hh_presup"] or 0):
            motivos.append("SIN_HH")
        out.append({**dict(r), "metrado_presup": float(r["metrado_presup"] or 0),
                    "hh_presup": float(r["hh_presup"] or 0), "motivos": motivos})
    return out


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


@router.delete("/partidas/{partida_id}")
async def eliminar_partida(partida_id: int):
    pool = await db()
    async with pool.acquire() as con:
        await con.execute("UPDATE ev_partidas SET activo=FALSE WHERE id=$1", partida_id)
    return {"ok": True}


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
                    await con.execute(
                        """UPDATE ev_partidas SET otm_id=$2, fase=$3, sub_fase=$4, descripcion=$5,
                           unidad=$6, sistema=$7, metrado_presup=$8, metrado_proyec=$9,
                           hh_presup=$10, nivel=$11, parent_codigo=$12, tipo_costo=$13,
                           naturaleza=$14, hh_actualizado=$15, activo=TRUE WHERE id=$1""",
                        existente, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo, tipo_costo, naturaleza, p.hh_actualizado,
                    )
                    await con.execute("DELETE FROM ev_hitos WHERE partida_id=$1", existente)
                    pid = existente; actualizadas += 1
                else:
                    pid = await con.fetchval(
                        """INSERT INTO ev_partidas
                           (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                            metrado_presup, metrado_proyec, hh_presup, nivel, parent_codigo,
                            tipo_costo, naturaleza, hh_actualizado)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id""",
                        p.codigo, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo, tipo_costo, naturaleza, p.hh_actualizado,
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


