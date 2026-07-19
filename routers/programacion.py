# ============================================================
# routers/programacion.py — calendario combinado plan/ejecutado (mejoras UX pre-F4)
#
# Oficina (`router`, /ev/programacion): semana del calendario, CRUD de
#   actividades, indicador de almacenamiento por semana y purga manual.
# Campo (`router_campo`, /campo): el supervisor envía su reporte del día
#   (descripción + fotos, opcionalmente contra una actividad programada).
#
# Regla del calendario combinado: cuando llega un reporte vinculado a una
# actividad PROGRAMADO, la actividad pasa sola a EJECUTADO.
# ============================================================
import asyncio
from datetime import date, timedelta
from typing import List, Optional

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core.auth import exigir_identidad_supervisor, require_role
from core.db import db
from core.log import get_logger
from core.media import (MAX_FOTO_BYTES, MAX_FOTOS_POR_REPORTE, guardar_foto,
                        media_dir, semana_iso_a_lunes, url_firmada)
from core.tiempo import fecha_lima, parse_fecha, semana_de

log = get_logger("programacion")

router = APIRouter(prefix="/ev/programacion", tags=["programacion"])
router_campo = APIRouter(prefix="/campo", tags=["programacion"])

_ESTADOS = ("PROGRAMADO", "EJECUTADO", "CANCELADO", "NO_CUMPLIDA")

# Errores de datos del usuario (FK inexistente, texto demasiado largo…): deben
# responder 400 — un 500 del handler global sale sin headers CORS y el panel
# lo muestra como "Failed to fetch".
_ERRORES_DATO = (asyncpg.IntegrityConstraintViolationError, asyncpg.DataError)

# Catálogo de Causas de No Cumplimiento (Last Planner): categoría cerrada para
# poder hacer el Pareto; el detalle libre acompaña en causa_nc.
CNC = {
    "MATERIALES": "Falta de materiales",
    "MANO_OBRA": "Falta de mano de obra",
    "EQUIPOS": "Falta de equipos",
    "INFORMACION": "Falta de información / ingeniería",
    "CLIMA": "Clima",
    "INTERFERENCIA": "Interferencia con otra disciplina",
    "PRERREQUISITO": "Prerrequisito no terminado",
    "CLIENTE": "Cambio de prioridad del cliente",
    "PROGRAMACION": "Mala programación / estimación",
    "OTROS": "Otros",
}
_TIPOS_RESTRICCION = ("MATERIALES", "MANO_OBRA", "EQUIPOS", "INFORMACION",
                      "PRERREQUISITO", "PERMISOS", "ESPACIO", "OTROS")


def _validar_cnc(cat) -> Optional[str]:
    c = str(cat or "").strip().upper() or None
    if c and c not in CNC:
        raise HTTPException(422, f"Categoría de causa inválida (usa {'/'.join(CNC)})")
    return c


def _lunes_de(fecha: date) -> date:
    return fecha - timedelta(days=fecha.weekday())


def _distribuir(metrado: float, dias: list, medios: set = frozenset()) -> dict:
    """Distribución del metrado entre los días dados, como la fórmula del
    LookAhead del ex-gerente, con PESOS: un día en `medios` pesa 0.5 (recibe
    la mitad que un día completo, F4 v2). El último día absorbe el redondeo."""
    if not dias:
        return {}
    peso = lambda d: 0.5 if d in medios else 1.0          # noqa: E731
    cuota = metrado / sum(peso(d) for d in dias)
    out, acum = {}, 0.0
    for d in dias[:-1]:
        v = round(cuota * peso(d), 3)
        out[d] = v
        acum += v
    out[dias[-1]] = round(metrado - acum, 3)
    return out


async def _calendario(con, proyecto_id: int, desde: date, hasta: date) -> tuple:
    """(días ISO de la semana que se trabajan, feriados del rango)."""
    ds = await con.fetchval(
        "SELECT dias_semana FROM prog_config WHERE proyecto_id = $1", proyecto_id)
    fer = await con.fetch(
        "SELECT fecha FROM prog_feriados WHERE proyecto_id = $1 AND fecha BETWEEN $2 AND $3",
        proyecto_id, desde, hasta)
    return (set(ds) if ds else {1, 2, 3, 4, 5, 6, 7}), {r["fecha"] for r in fer}


def _dias_habiles(desde: date, hasta: date, dias_semana: set, feriados: set,
                  saltos: set) -> list:
    return [d for i in range((hasta - desde).days + 1)
            if (d := desde + timedelta(days=i)).isoweekday() in dias_semana
            and d not in feriados and d not in saltos]


async def _redistribuir(con, act: dict, solo_despues_de: Optional[date] = None) -> None:
    """Recalcula la distribución diaria del metrado de la actividad. El metrado
    META es inmutable aquí (solo se cambia desde el formulario):
      · salta los días no laborables (prog_config + prog_feriados) y los
        saltos intencionales de la actividad (dias_salto);
      · los días que YA tienen avance real registrado quedan CONGELADOS
        (su programado es la línea contra la que se compara el cumplimiento);
      · las celdas MANUALES dentro del rango (escritas por el planner vía
        metrado-dias, 0027) se respetan: su cantidad descuenta del saldo y
        no se recalculan; fuera del rango vigente (reprogramación) el plan
        manual viejo se descarta salvo que el día tenga real;
      · el SALDO (metrado − real acumulado − plan manual pendiente) se
        re-prorratea entre los días hábiles restantes — así la actividad
        sigue apuntando a terminar en su F.Fin con lo que falta.
    Con solo_despues_de (al registrar un avance): los días ANTERIORES no se
    tocan ("eso ya se hizo") y el saldo cae solo en los días posteriores."""
    desde, hasta = act["fecha"], act["fecha_fin"] or act["fecha"]
    dias_semana, feriados = await _calendario(con, act["proyecto_id"], desde, hasta)
    saltos = set(act.get("dias_salto") or [])
    habiles = _dias_habiles(desde, hasta, dias_semana, feriados, saltos)

    reales: dict = {}
    if act.get("partida_id"):
        # TODOS los reales de la MISMA etapa (hito) de la actividad, SIN
        # acotar al rango vigente: al reprogramar fechas, lo ya anotado en el
        # rango viejo sigue descontando del saldo (si no, el metrado completo
        # reaparecería prorrateado como si nada se hubiera hecho). Una
        # actividad del hito principal equivale a una sin hito (NULL).
        reales = {r["fecha"]: float(r["cantidad_dia"]) for r in await con.fetch(
            """SELECT fecha, cantidad_dia FROM ev_avances_diarios ad
               WHERE partida_id = $1
                 AND cantidad_dia IS NOT NULL
                 AND COALESCE(ad.hito_id, (SELECT id FROM ev_hitos h
                       WHERE h.partida_id = $1
                       ORDER BY h.es_principal DESC, h.peso DESC, h.id LIMIT 1))
                     = COALESCE($2, (SELECT id FROM ev_hitos h
                       WHERE h.partida_id = $1
                       ORDER BY h.es_principal DESC, h.peso DESC, h.id LIMIT 1))""",
            act["partida_id"], act.get("hito_id"))}

    # Celdas manuales del rango vigente: plan fino del planner, se protege.
    manuales = {r["fecha"]: float(r["cantidad"]) for r in await con.fetch(
        """SELECT fecha, cantidad FROM prog_metrado_dia
           WHERE actividad_id = $1 AND manual AND fecha BETWEEN $2 AND $3""",
        act["id"], desde, hasta)}

    intactos = set(reales) | set(manuales)
    if solo_despues_de:
        intactos |= {d for d in habiles if d <= solo_despues_de}
    # Se borran solo las celdas que se van a recalcular.
    await con.execute(
        "DELETE FROM prog_metrado_dia WHERE actividad_id = $1"
        " AND NOT (fecha = ANY($2::date[]))", act["id"], list(intactos))
    metrado = float(act["metrado_prog"] or 0)
    if metrado <= 0:
        return
    # El plan manual PENDIENTE (días sin real, aún por delante) descuenta del
    # saldo; un día manual ya pasado sin real es línea base congelada (como
    # cualquier día congelado) y no descuenta.
    plan_manual = sum(c for d, c in manuales.items()
                      if d not in reales
                      and (solo_despues_de is None or d > solo_despues_de))
    saldo = round(metrado - sum(reales.values()) - plan_manual, 3)
    restantes = [d for d in habiles if d not in intactos]
    if saldo <= 0 or not restantes:
        return
    medios = set(act.get("dias_medio") or [])             # pesan 0.5 (F4 v2)
    await con.executemany(
        "INSERT INTO prog_metrado_dia (actividad_id, fecha, cantidad) VALUES ($1,$2,$3)"
        " ON CONFLICT (actividad_id, fecha) DO UPDATE SET cantidad = $3, manual = false",
        [(act["id"], f, c) for f, c in _distribuir(saldo, restantes, medios).items() if c > 0])


def _parse_saltos(v) -> list:
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise HTTPException(400, "dias_salto debe ser una lista de fechas")
    out = []
    for s in v:
        f = parse_fecha(s)
        if not f:
            raise HTTPException(400, f"dias_salto: fecha inválida {s}")
        out.append(f)
    return sorted(set(out))


# SELECT canónico de actividades: partida de control, supervisor y el resumen
# de restricciones (rest_pend > 0 = aún no está "sana" para comprometerse).
_ACT_SQL = """
    SELECT a.*, o.descripcion AS otm_desc, s.nombre AS supervisor_nombre,
           ev.codigo AS partida_codigo, ev.descripcion AS partida_desc,
           (SELECT count(*) FROM prog_restricciones pr
             WHERE pr.actividad_id = a.id) AS rest_total,
           (SELECT count(*) FROM prog_restricciones pr
             WHERE pr.actividad_id = a.id AND NOT pr.liberada) AS rest_pend,
           (SELECT count(*) FROM prog_dependencias pd
             WHERE pd.actividad_id = a.id OR pd.predecesora_id = a.id) AS dep_total
    FROM prog_actividades a
    LEFT JOIN otms o ON o.id = a.otm_id
    LEFT JOIN supervisores s ON s.id = a.supervisor_id
    LEFT JOIN ev_partidas ev ON ev.id = a.partida_id
"""


def _foto_out(f: dict) -> dict:
    purgada = f["purgada"]
    return {"id": f["id"], "purgada": purgada, "bytes": f["bytes"],
            "url": None if purgada else url_firmada(f["ruta"]),
            "url_thumb": None if purgada else url_firmada(f["ruta_thumb"])}


# ── Calendario (oficina) ─────────────────────────────────────
@router.get("/semana")
async def semana(proyecto_id: int = 1, lunes: str = ""):
    """Actividades + reportes (con URLs de foto firmadas) de la semana Lun-Dom."""
    base = _lunes_de(parse_fecha(lunes) or fecha_lima())
    fechas = [base + timedelta(days=i) for i in range(7)]
    pool = await db()
    async with pool.acquire() as con:
        acts = [dict(r) for r in await con.fetch(
            _ACT_SQL + " WHERE a.proyecto_id = $1 AND a.fecha BETWEEN $2 AND $3"
                       " ORDER BY a.fecha, a.id", proyecto_id, fechas[0], fechas[6])]
        reps = [dict(r) for r in await con.fetch(
            """SELECT r.*, s.nombre AS supervisor_nombre
               FROM campo_reportes r LEFT JOIN supervisores s ON s.id = r.supervisor_id
               WHERE r.proyecto_id = $1 AND r.fecha BETWEEN $2 AND $3
               ORDER BY r.fecha, r.id""", proyecto_id, fechas[0], fechas[6])]
        fotos = [dict(r) for r in await con.fetch(
            """SELECT f.* FROM campo_fotos f
               JOIN campo_reportes r ON r.id = f.reporte_id
               WHERE r.proyecto_id = $1 AND r.fecha BETWEEN $2 AND $3
               ORDER BY f.id""", proyecto_id, fechas[0], fechas[6])]

    fotos_por_rep: dict = {}
    for f in fotos:
        fotos_por_rep.setdefault(f["reporte_id"], []).append(_foto_out(f))
    for r in reps:
        r["fotos"] = fotos_por_rep.get(r["id"], [])
    reps_por_act: dict = {}
    for r in reps:
        if r["actividad_id"]:
            reps_por_act.setdefault(r["actividad_id"], []).append(r["id"])
    for a in acts:
        a["reportes"] = reps_por_act.get(a["id"], [])

    return {"lunes": str(fechas[0]), "fechas": [str(f) for f in fechas],
            "actividades": acts, "reportes": reps}


def _parse_metrado(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        m = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "metrado_prog debe ser un número")
    if m < 0:
        raise HTTPException(400, "metrado_prog no puede ser negativo")
    return m or None


@router.post("/actividades")
async def crear_actividad(data: dict, user: dict = Depends(require_role("oficina"))):
    fecha = parse_fecha(data.get("fecha"))
    titulo = str(data.get("titulo") or "").strip()
    if not fecha or not titulo:
        raise HTTPException(400, "fecha y titulo son obligatorios")
    fecha_fin = parse_fecha(data.get("fecha_fin"))
    if fecha_fin and fecha_fin < fecha:
        raise HTTPException(400, "fecha_fin no puede ser anterior a fecha")
    metrado = _parse_metrado(data.get("metrado_prog"))
    und = (str(data.get("und") or "").strip()[:10] or None)
    saltos = _parse_saltos(data.get("dias_salto"))
    medios = _parse_saltos(data.get("dias_medio"))
    if set(saltos) & set(medios):
        raise HTTPException(400, "Un día no puede ser salto y medio día a la vez")
    partida_id = int(data["partida_id"]) if data.get("partida_id") else None
    hito_id = int(data["hito_id"]) if data.get("hito_id") else None
    pool = await db()
    async with pool.acquire() as con:
        if hito_id:
            await _validar_hito(con, partida_id, hito_id)
        async with con.transaction():
            try:
                row = await con.fetchrow(
                    """INSERT INTO prog_actividades
                       (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo, descripcion,
                        responsable, supervisor_id, metrado_prog, und, dias_salto, dias_medio,
                        hito_id, creado_por)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING *""",
                    int(data.get("proyecto_id") or 1), fecha, fecha_fin,
                    (str(data["otm_id"]).strip() or None) if data.get("otm_id") else None,
                    partida_id,
                    titulo, data.get("descripcion") or None, data.get("responsable") or None,
                    (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None,
                    metrado, und, saltos, medios, hito_id, user.get("sub"))
            except _ERRORES_DATO:
                raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
            await _redistribuir(con, dict(row))
    return dict(row)


async def _validar_hito(con, partida_id: Optional[int], hito_id: int) -> None:
    if not partida_id:
        raise HTTPException(400, "hito_id requiere partida_id")
    pid = await con.fetchval("SELECT partida_id FROM ev_hitos WHERE id = $1", hito_id)
    if pid is None:
        raise HTTPException(404, "Hito no encontrado")
    if pid != partida_id:
        raise HTTPException(400, "El hito no pertenece a la partida indicada")


@router.put("/actividades/{act_id}")
async def editar_actividad(act_id: int, data: dict):
    campos, valores = [], []
    if "estado" in data:
        if data["estado"] not in _ESTADOS:
            raise HTTPException(422, f"estado inválido (usa {'/'.join(_ESTADOS)})")
        campos.append("estado"); valores.append(data["estado"])
    if "fecha" in data:
        f = parse_fecha(data["fecha"])
        if not f:
            raise HTTPException(400, "fecha inválida")
        campos.append("fecha"); valores.append(f)
    if "fecha_fin" in data:
        campos.append("fecha_fin"); valores.append(parse_fecha(data["fecha_fin"]))
    if "metrado_prog" in data:
        campos.append("metrado_prog"); valores.append(_parse_metrado(data["metrado_prog"]))
    if "und" in data:
        campos.append("und")
        valores.append(str(data["und"]).strip()[:10] or None if data["und"] is not None else None)
    if "dias_salto" in data:
        campos.append("dias_salto"); valores.append(_parse_saltos(data["dias_salto"]))
    if "dias_medio" in data:
        campos.append("dias_medio"); valores.append(_parse_saltos(data["dias_medio"]))
    if "causa_nc_cat" in data:
        campos.append("causa_nc_cat"); valores.append(_validar_cnc(data["causa_nc_cat"]))
    if "causa_nc_planner_cat" in data:
        campos.append("causa_nc_planner_cat"); valores.append(_validar_cnc(data["causa_nc_planner_cat"]))
    for k in ("titulo", "descripcion", "responsable", "otm_id", "supervisor_id",
              "causa_nc", "causa_nc_planner"):
        if k in data:
            v = str(data[k]).strip() if data[k] is not None else None
            if k == "titulo" and not v:
                raise HTTPException(400, "titulo no puede quedar vacío")
            campos.append(k); valores.append(v or None)
    if "partida_id" in data:
        campos.append("partida_id")
        valores.append(int(data["partida_id"]) if data["partida_id"] else None)
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(campos))
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            try:
                row = await con.fetchrow(
                    f"UPDATE prog_actividades SET {sets}, actualizado_en = now() "
                    f"WHERE id = $1 RETURNING *", act_id, *valores)
            except _ERRORES_DATO:
                raise HTTPException(400, "OTM, partida, supervisor o rango de fechas inválido: revisa los datos")
            if not row:
                raise HTTPException(404, "Actividad no encontrada")
            if set(row["dias_salto"] or []) & set(row["dias_medio"] or []):
                raise HTTPException(400, "Un día no puede ser salto y medio día a la vez")
            # Si cambió el rango, el metrado, los saltos o los medios días, la
            # distribución diaria se recalcula (las ediciones celda a celda van
            # por /actividades/{id}/metrado-dias y NO pasan por aquí).
            movidas: list = []
            if {"fecha", "fecha_fin", "metrado_prog", "dias_salto", "dias_medio"} & data.keys():
                await _redistribuir(con, dict(row))
            # Auto-cascada FS: mover el rango empuja a las sucesoras (F5b v2).
            if {"fecha", "fecha_fin"} & data.keys():
                movidas = await recalcular_cascada(con, act_id)
    return {**dict(row), "movidas": movidas}


@router.delete("/actividades/{act_id}")
async def borrar_actividad(act_id: int):
    pool = await db()
    async with pool.acquire() as con:
        n_reps = await con.fetchval(
            "SELECT count(*) FROM campo_reportes WHERE actividad_id = $1", act_id)
        if n_reps:
            raise HTTPException(409, "La actividad tiene reportes de campo; cancélala en vez de borrarla")
        n = await con.execute("DELETE FROM prog_actividades WHERE id = $1", act_id)
    if n == "DELETE 0":
        raise HTTPException(404, "Actividad no encontrada")
    return {"ok": True}


# ── Last Planner: lookahead, restricciones y PPC/CNC ─────────
@router.get("/lookahead")
async def lookahead(proyecto_id: int = 1, desde: str = "", semanas: int = 4):
    """Actividades de las próximas N semanas con su estado de restricciones —
    el nivel '¿qué se PUEDE hacer?' del Last Planner."""
    semanas = max(1, min(int(semanas or 4), 8))
    base = _lunes_de(parse_fecha(desde) or fecha_lima())
    pool = await db()
    acts = [dict(r) for r in await pool.fetch(
        _ACT_SQL + " WHERE a.proyecto_id = $1 AND a.fecha BETWEEN $2 AND $3"
                   " ORDER BY a.fecha, a.id",
        proyecto_id, base, base + timedelta(days=semanas * 7 - 1))]
    out = []
    for i in range(semanas):
        lun = base + timedelta(days=i * 7)
        dom = lun + timedelta(days=6)
        out.append({"lunes": str(lun), "domingo": str(dom),
                    "actividades": [a for a in acts
                                    if lun <= a["fecha"] <= dom]})
    return {"desde": str(base), "semanas": out, "cnc": CNC,
            "tipos_restriccion": list(_TIPOS_RESTRICCION)}


# ── Calendario laboral (por proyecto — multi-empresa) ────────
async def _reprorratear_programadas(con, proyecto_id: int) -> int:
    """Tras cambiar el calendario, se recalcula la distribución de todas las
    actividades aún PROGRAMADO con metrado (las ejecutadas no se tocan)."""
    acts = await con.fetch(
        """SELECT * FROM prog_actividades
           WHERE proyecto_id = $1 AND estado = 'PROGRAMADO' AND metrado_prog IS NOT NULL""",
        proyecto_id)
    for a in acts:
        await _redistribuir(con, dict(a))
    return len(acts)


@router.get("/config")
async def ver_config(proyecto_id: int = 1):
    pool = await db()
    async with pool.acquire() as con:
        ds = await con.fetchval(
            "SELECT dias_semana FROM prog_config WHERE proyecto_id = $1", proyecto_id)
        fer = await con.fetch(
            "SELECT id, fecha, motivo FROM prog_feriados WHERE proyecto_id = $1 ORDER BY fecha",
            proyecto_id)
    return {"dias_semana": sorted(ds) if ds else [1, 2, 3, 4, 5, 6, 7],
            "feriados": [{"id": r["id"], "fecha": str(r["fecha"]), "motivo": r["motivo"]}
                         for r in fer]}


@router.put("/config")
async def guardar_config(data: dict):
    """Días de la semana que se trabajan (ISO: 1=Lun … 7=Dom). Re-prorratea
    las actividades PROGRAMADO para que el plan salte los días nuevos."""
    dias = data.get("dias_semana")
    if (not isinstance(dias, list) or not dias
            or any(not isinstance(d, int) or d < 1 or d > 7 for d in dias)):
        raise HTTPException(400, "dias_semana debe ser una lista no vacía con valores 1..7")
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                """INSERT INTO prog_config (proyecto_id, dias_semana)
                   VALUES ($1, $2) ON CONFLICT (proyecto_id)
                   DO UPDATE SET dias_semana = $2, actualizado_en = now()""",
                proyecto_id, sorted(set(dias)))
            n = await _reprorratear_programadas(con, proyecto_id)
    return {"ok": True, "dias_semana": sorted(set(dias)), "reprorrateadas": n}


@router.post("/feriados")
async def crear_feriado(data: dict):
    f = parse_fecha(data.get("fecha"))
    if not f:
        raise HTTPException(400, "fecha requerida")
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                """INSERT INTO prog_feriados (proyecto_id, fecha, motivo)
                   VALUES ($1,$2,$3) ON CONFLICT (proyecto_id, fecha)
                   DO UPDATE SET motivo = $3 RETURNING id, fecha, motivo""",
                proyecto_id, f, (str(data.get("motivo") or "").strip() or None))
            n = await _reprorratear_programadas(con, proyecto_id)
    return {"id": row["id"], "fecha": str(row["fecha"]), "motivo": row["motivo"],
            "reprorrateadas": n}


@router.delete("/feriados/{fer_id}")
async def borrar_feriado(fer_id: int):
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "DELETE FROM prog_feriados WHERE id = $1 RETURNING proyecto_id", fer_id)
            if not row:
                raise HTTPException(404, "Feriado no encontrado")
            n = await _reprorratear_programadas(con, row["proyecto_id"])
    return {"ok": True, "reprorrateadas": n}


def _parse_cantidad(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        c = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "cantidad debe ser un número")
    if c < 0:
        raise HTTPException(400, "la cantidad no puede ser negativa")
    return c


async def _hito_principal(con, partida_id: int) -> Optional[int]:
    """Id del hito principal de la partida. Si la partida NO tiene hitos, lo
    crea silenciosamente ('Ejecución', peso 100%) — así el % EV de cualquier
    partida queda conectado al avance diario sin trabajo extra del planner."""
    hid = await con.fetchval(
        """SELECT id FROM ev_hitos WHERE partida_id = $1
           ORDER BY es_principal DESC, peso DESC, id LIMIT 1""", partida_id)
    if hid is None:
        hid = await con.fetchval(
            """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
               VALUES ($1, 1, 'Ejecución', 1.0, true) RETURNING id""", partida_id)
    return hid


async def _rollup_ev_avances(con, partida_id: int) -> None:
    """Fuente única: deriva ev_avances (acumulado semanal por hito — la entrada
    del motor EV) del registro diario. Por hito: las semanas desde su primer
    registro diario se recalculan como base_manual_previa + Σ diario; las
    semanas anteriores (captura manual / carga histórica) no se tocan."""
    principal = await _hito_principal(con, partida_id)
    rows = await con.fetch(
        """SELECT COALESCE(hito_id, $2) AS hid, semana, SUM(cantidad_dia) AS c
           FROM ev_avances_diarios
           WHERE partida_id = $1 AND cantidad_dia IS NOT NULL
           GROUP BY 1, 2""", partida_id, principal)
    por_hito: dict = {}
    for r in rows:
        por_hito.setdefault(r["hid"], {})[r["semana"]] = float(r["c"] or 0)
    for hid, sems in por_hito.items():
        primera = min(sems)
        base = float(await con.fetchval(
            """SELECT COALESCE(MAX(cantidad_acum), 0) FROM ev_avances
               WHERE hito_id = $1 AND semana < $2""", hid, primera) or 0)
        existentes = {r["semana"] for r in await con.fetch(
            "SELECT semana FROM ev_avances WHERE hito_id = $1 AND semana >= $2",
            hid, primera)}
        for s in sorted(set(sems) | existentes):
            acum = base + sum(c for w, c in sems.items() if w <= s)
            await con.execute(
                """INSERT INTO ev_avances (hito_id, semana, cantidad_acum, registrado_en)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (hito_id, semana)
                   DO UPDATE SET cantidad_acum = $3, registrado_en = NOW()""",
                hid, s, round(acum, 4))


async def registrar_avance_partida(con, partida_id: int, fecha: date, cantidad,
                                   notas=None, actualizar_notas: bool = False,
                                   hito_id: Optional[int] = None) -> None:
    """Escritura ÚNICA del avance real diario (F1 LookAhead v2 / auditoría F-2):
    upsert (o DELETE si cantidad es None) en ev_avances_diarios y re-prorrateo
    de TODA actividad del LookAhead vinculada a la partida cuyo rango cubre la
    fecha — venga el avance de programación o del módulo Valor Ganado, el dato
    y sus consecuencias son los mismos. La semana usa core.tiempo.semana_de.
    hito_id = etapa de la partida a la que pertenece el registro (NULL = hito
    principal). Tras escribir, _rollup_ev_avances deriva ev_avances (la entrada
    del motor EV): un solo dato alimenta LookAhead, VG diario y % de avance."""
    from routers.ev._datos import _fecha_base
    # Convención dura: el hito principal SIEMPRE se guarda como NULL (las
    # vistas por partida — semana-grid, matriz — leen NULL = cant. instalada).
    principal = await _hito_principal(con, partida_id)
    if hito_id is not None and hito_id == principal:
        hito_id = None
    if cantidad is None:
        await con.execute(
            """DELETE FROM ev_avances_diarios WHERE partida_id = $1 AND fecha = $2
               AND COALESCE(hito_id, 0) = COALESCE($3, 0)""",
            partida_id, fecha, hito_id)
    else:
        base = await _fecha_base(con)
        semana = max(1, semana_de(fecha, base)) if base else 1
        if actualizar_notas:
            await con.execute(
                """INSERT INTO ev_avances_diarios
                     (partida_id, fecha, semana, cantidad_dia, notas, hito_id, registrado_en)
                   VALUES ($1, $2, $3, $4, $5, $6, NOW())
                   ON CONFLICT (partida_id, fecha, COALESCE(hito_id, 0))
                   DO UPDATE SET cantidad_dia = $4, notas = $5, registrado_en = NOW()""",
                partida_id, fecha, semana, cantidad, notas, hito_id)
        else:
            await con.execute(
                """INSERT INTO ev_avances_diarios
                     (partida_id, fecha, semana, cantidad_dia, hito_id, registrado_en)
                   VALUES ($1, $2, $3, $4, $5, NOW())
                   ON CONFLICT (partida_id, fecha, COALESCE(hito_id, 0))
                   DO UPDATE SET cantidad_dia = $4, registrado_en = NOW()""",
                partida_id, fecha, semana, cantidad, hito_id)
    await _rollup_ev_avances(con, partida_id)
    # Los días anteriores al registrado no se tocan; el saldo para cumplir el
    # metrado meta se re-prorratea en los días siguientes de cada actividad
    # de la MISMA etapa (hito) de la partida — una actividad apuntando al
    # hito principal equivale a una sin hito ($4 = id del principal).
    acts = await con.fetch(
        """SELECT * FROM prog_actividades
           WHERE partida_id = $1 AND $2 BETWEEN fecha AND COALESCE(fecha_fin, fecha)
             AND COALESCE(hito_id, $4) = COALESCE($3, $4)""",
        partida_id, fecha, hito_id, principal)
    for a in acts:
        await _redistribuir(con, dict(a), solo_despues_de=fecha)


@router.post("/actividades/{act_id}/avance-dia")
async def avance_dia_actividad(act_id: int, data: dict):
    """El avance REAL del día contra una actividad del LookAhead: escribe en
    ev_avances_diarios (la partida de control de la actividad) y RE-PRORRATEA
    el saldo entre los días hábiles restantes — la actividad sigue apuntando
    a terminar en su F.Fin. El programado del día avanzado queda congelado
    como línea base de comparación (celeste→verde/ámbar/rojo en el panel)."""
    f = parse_fecha(data.get("fecha"))
    if not f:
        raise HTTPException(400, "fecha requerida")
    cantidad = _parse_cantidad(data.get("cantidad"))
    pool = await db()
    async with pool.acquire() as con:
        act = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", act_id)
        if not act:
            raise HTTPException(404, "Actividad no encontrada")
        if not act["partida_id"]:
            raise HTTPException(400, "La actividad no tiene partida de control: asígnala para registrar avance")
        async with con.transaction():
            await registrar_avance_partida(con, act["partida_id"], f, cantidad,
                                           hito_id=act["hito_id"])
    return {"ok": True, "cantidad": cantidad}


@router.post("/actividades-lote")
async def crear_actividades_lote(data: dict, user: dict = Depends(require_role("oficina"))):
    """Programación por partidas (flujo LookAhead): el planner elige una OTM,
    marca varias partidas del presupuesto y cada una se vuelve UNA actividad
    con su rango F.Inic-F.Fin y su metrado meta. Si el item no trae metrado,
    se toma el metrado del presupuesto de la partida; en ambos casos se
    prorratea equitativamente entre los días del rango."""
    otm_id = str(data.get("otm_id") or "").strip()
    items = data.get("items") or []
    if not otm_id or not isinstance(items, list) or not items:
        raise HTTPException(400, "otm_id e items son obligatorios")
    if len(items) > 50:
        raise HTTPException(422, "Máximo 50 partidas por lote")

    parsed = []
    for i, it in enumerate(items, 1):
        pid = it.get("partida_id")
        fecha = parse_fecha(it.get("fecha"))
        if not pid or not fecha:
            raise HTTPException(400, f"Item {i}: partida_id y fecha son obligatorios")
        fecha_fin = parse_fecha(it.get("fecha_fin"))
        if fecha_fin and fecha_fin < fecha:
            raise HTTPException(400, f"Item {i}: fecha_fin anterior a fecha")
        parsed.append((int(pid), fecha, fecha_fin, _parse_metrado(it.get("metrado_prog")),
                       int(it["hito_id"]) if it.get("hito_id") else None))

    proyecto_id = int(data.get("proyecto_id") or 1)
    supervisor_id = (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None
    responsable = data.get("responsable") or None
    descripcion = data.get("descripcion") or None

    encadenar = bool(data.get("encadenar_hitos"))
    pool = await db()
    creadas = []
    async with pool.acquire() as con:
        pinfo = {r["id"]: dict(r) for r in await con.fetch(
            "SELECT id, descripcion, metrado_presup, unidad FROM ev_partidas WHERE id = ANY($1)",
            [p[0] for p in parsed])}
        faltan = [str(p[0]) for p in parsed if p[0] not in pinfo]
        if faltan:
            raise HTTPException(400, f"Partidas inexistentes: {', '.join(faltan)}")
        hinfo = {}
        for pid, _f, _ff, _m, hid in parsed:
            if hid:
                await _validar_hito(con, pid, hid)
                hinfo[hid] = await con.fetchrow(
                    "SELECT descripcion, peso FROM ev_hitos WHERE id = $1", hid)
        async with con.transaction():
            # id de la actividad anterior de la MISMA partida (para encadenar FS
            # las etapas desplegadas por hitos, en el orden en que llegan).
            prev_de_partida: dict = {}
            for pid, fecha, fecha_fin, metrado, hid in parsed:
                p = pinfo[pid]
                if metrado is None and p["metrado_presup"] is not None:
                    metrado = float(p["metrado_presup"]) or None
                titulo = (p["descripcion"] or f"Partida {pid}")[:170]
                if hid and hinfo.get(hid):
                    titulo = f"{titulo} — {hinfo[hid]['descripcion'] or 'Etapa'}"[:200]
                try:
                    row = await con.fetchrow(
                        """INSERT INTO prog_actividades
                           (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo,
                            descripcion, responsable, supervisor_id, metrado_prog, hito_id,
                            creado_por)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *""",
                        proyecto_id, fecha, fecha_fin, otm_id, pid, titulo,
                        descripcion, responsable, supervisor_id, metrado, hid,
                        user.get("sub"))
                except _ERRORES_DATO:
                    raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
                await _redistribuir(con, dict(row))
                if encadenar and hid and prev_de_partida.get(pid):
                    await con.execute(
                        """INSERT INTO prog_dependencias (actividad_id, predecesora_id, lag_dias)
                           VALUES ($1, $2, 0) ON CONFLICT DO NOTHING""",
                        row["id"], prev_de_partida[pid])
                if hid:
                    prev_de_partida[pid] = row["id"]
                creadas.append(dict(row))
    return {"creadas": len(creadas), "actividades": creadas}


# ── Dependencias (F5 v2): antecesoras Fin→Inicio con auto-cascada ──
async def _hay_ciclo(con, predecesora_id: int, actividad_id: int) -> bool:
    """¿Agregar 'predecesora_id precede a actividad_id' crearía un ciclo?
    Sí, cuando predecesora_id es alcanzable desde actividad_id siguiendo
    la relación 'precede a' (BFS sobre las sucesoras)."""
    cola, vistas = [actividad_id], set()
    while cola:
        actual = cola.pop(0)
        if actual == predecesora_id:
            return True
        if actual in vistas:
            continue
        vistas.add(actual)
        cola += [r["actividad_id"] for r in await con.fetch(
            "SELECT actividad_id FROM prog_dependencias WHERE predecesora_id = $1", actual)]
    return False


def _siguiente_habil(d: date, dias_semana: set, feriados: set) -> date:
    """Primer día ESTRICTAMENTE posterior a d que es hábil del calendario."""
    x = d + timedelta(days=1)
    while not (x.isoweekday() in dias_semana and x not in feriados):
        x += timedelta(days=1)
    return x


async def recalcular_cascada(con, actividad_id_movida: int) -> list:
    """Auto-cascada FS (F5b v2): al mover una antecesora, cada sucesora se
    desplaza SOLO HACIA ADELANTE — nueva F.Inicio = max(F.Inicio actual,
    siguiente día hábil tras F.Fin de la antecesora + lag). La F.Fin se
    desplaza preservando la duración en días hábiles del calendario, los
    saltos/medios que queden fuera del nuevo rango se descartan y el metrado
    se re-prorratea. BFS en orden; los ciclos ya están vetados al crear."""
    movidas: list = []
    cola, vistas = [actividad_id_movida], set()
    while cola:
        actual = cola.pop(0)
        if actual in vistas:
            continue
        vistas.add(actual)
        pred = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", actual)
        if not pred:
            continue
        fin_pred = pred["fecha_fin"] or pred["fecha"]
        deps = await con.fetch(
            "SELECT actividad_id, lag_dias FROM prog_dependencias WHERE predecesora_id = $1",
            actual)
        for dep in deps:
            suc = await con.fetchrow(
                "SELECT * FROM prog_actividades WHERE id = $1", dep["actividad_id"])
            if not suc or suc["estado"] == "CANCELADO":
                continue
            dias_semana, feriados = await _calendario(
                con, suc["proyecto_id"], fin_pred, fin_pred + timedelta(days=366))
            inicio_min = _siguiente_habil(
                fin_pred + timedelta(days=int(dep["lag_dias"] or 0)), dias_semana, feriados)
            if inicio_min <= suc["fecha"]:
                continue                      # nunca se adelanta ni se toca
            fin_suc = suc["fecha_fin"] or suc["fecha"]
            dur = len(_dias_habiles(suc["fecha"], fin_suc, dias_semana, feriados, set()))
            if dur > 0:
                nueva_fin, n = inicio_min, 1 if inicio_min.isoweekday() in dias_semana and inicio_min not in feriados else 0
                while n < dur:
                    nueva_fin = _siguiente_habil(nueva_fin, dias_semana, feriados)
                    n += 1
            else:
                nueva_fin = inicio_min + (fin_suc - suc["fecha"])
            rango = {inicio_min + timedelta(days=i)
                     for i in range((nueva_fin - inicio_min).days + 1)}
            row = await con.fetchrow(
                """UPDATE prog_actividades
                   SET fecha = $2, fecha_fin = $3,
                       dias_salto = $4, dias_medio = $5, actualizado_en = now()
                   WHERE id = $1 RETURNING *""",
                suc["id"], inicio_min, nueva_fin,
                sorted(d for d in (suc["dias_salto"] or []) if d in rango),
                sorted(d for d in (suc["dias_medio"] or []) if d in rango))
            await _redistribuir(con, dict(row))
            movidas.append(suc["id"])
            cola.append(suc["id"])
    return movidas


@router.get("/actividades")
async def listar_actividades(proyecto_id: int = 1, q: str = "", limite: int = 200,
                             otm: str = ""):
    """Listado ligero para el selector de antecesoras del modal. Con otm= el
    selector trabaja en 2 pasos (primero la OTM, luego sus actividades)."""
    limite = max(1, min(int(limite or 200), 500))
    filtro = f"%{q.strip()}%" if q.strip() else None
    otm_f = otm.strip() or None
    pool = await db()
    rows = await pool.fetch(
        """SELECT id, titulo, otm_id, fecha, COALESCE(fecha_fin, fecha) AS fecha_fin, estado
           FROM prog_actividades
           WHERE proyecto_id = $1 AND estado <> 'CANCELADO'
             AND ($2::text IS NULL OR titulo ILIKE $2 OR otm_id ILIKE $2)
             AND ($4::text IS NULL OR otm_id = $4)
           ORDER BY fecha DESC, id DESC LIMIT $3""",
        proyecto_id, filtro, limite, otm_f)
    return [{**dict(r), "fecha": str(r["fecha"]), "fecha_fin": str(r["fecha_fin"])} for r in rows]


# ── Hitos (rules of credit) vistos desde Programación ────────
@router.get("/partidas/{partida_id}/hitos")
async def hitos_de_partida(partida_id: int):
    """Hitos de la partida con su % automático (acum de ev_avances / metrado
    proyectado) y si su avance viene del registro DIARIO (auto) o de un
    checkpoint manual. Si la partida no tiene hitos aún, devuelve el hito
    'Ejecución 100%' virtual (se materializa recién al primer registro)."""
    pool = await db()
    async with pool.acquire() as con:
        p = await con.fetchrow(
            """SELECT id, COALESCE(metrado_proyec, metrado_presup) AS mp, unidad
               FROM ev_partidas WHERE id = $1""", partida_id)
        if not p:
            raise HTTPException(404, "Partida no encontrada")
        mp = float(p["mp"] or 0)
        hitos = [dict(r) for r in await con.fetch(
            """SELECT h.id, h.numero, h.descripcion, h.peso, h.es_principal,
                      (SELECT cantidad_acum FROM ev_avances a
                        WHERE a.hito_id = h.id ORDER BY a.semana DESC LIMIT 1) AS acum,
                      EXISTS (SELECT 1 FROM ev_avances_diarios d
                        WHERE d.partida_id = h.partida_id AND d.hito_id = h.id) AS con_diario,
                      EXISTS (SELECT 1 FROM prog_actividades pa
                        WHERE pa.hito_id = h.id) AS con_actividad
               FROM ev_hitos h WHERE h.partida_id = $1
               ORDER BY h.numero""", partida_id)]
    if not hitos:
        return {"partida_id": partida_id, "metrado": mp, "unidad": p["unidad"],
                "hitos": [{"id": None, "numero": 1, "descripcion": "Ejecución",
                           "peso": 1.0, "es_principal": True, "pct": None,
                           "auto": True, "con_actividad": False, "virtual": True}]}
    principal = max(hitos, key=lambda h: (h["es_principal"], float(h["peso"]), -h["id"]))["id"]
    out = []
    for h in hitos:
        acum = float(h["acum"] or 0)
        out.append({
            "id": h["id"], "numero": h["numero"], "descripcion": h["descripcion"],
            "peso": float(h["peso"]), "es_principal": h["es_principal"],
            "pct": round(min(acum / mp, 1.0), 4) if mp > 0 else None,
            "acum": round(acum, 4),
            # auto = su acumulado sale del registro diario (principal o etapa
            # con sub-actividad); si no, se marca con checkpoint manual.
            "auto": h["id"] == principal or h["con_diario"],
            "con_actividad": h["con_actividad"], "virtual": False,
        })
    return {"partida_id": partida_id, "metrado": mp, "unidad": p["unidad"], "hitos": out}


@router.post("/hitos/{hito_id}/checkpoint")
async def checkpoint_hito(hito_id: int, data: dict,
                          user: dict = Depends(require_role("oficina"))):
    """Marca el avance de un hito SECUNDARIO (etapa sin registro diario):
    pct 0..1 del metrado que ya pasó por esa etapa (1 = etapa completa) en la
    fecha dada — escribe ev_avances en la semana canónica de esa fecha. Los
    hitos alimentados por el diario se rechazan (los gobierna el rollup)."""
    from routers.ev._datos import _fecha_base
    f = parse_fecha(data.get("fecha")) or fecha_lima()
    try:
        pct = float(data.get("pct", 1.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "pct debe ser un número entre 0 y 1")
    if not (0 <= pct <= 1):
        raise HTTPException(400, "pct debe estar entre 0 y 1")
    pool = await db()
    async with pool.acquire() as con:
        h = await con.fetchrow(
            """SELECT h.id, h.partida_id,
                      COALESCE(p.metrado_proyec, p.metrado_presup) AS mp
               FROM ev_hitos h JOIN ev_partidas p ON p.id = h.partida_id
               WHERE h.id = $1""", hito_id)
        if not h:
            raise HTTPException(404, "Hito no encontrado")
        principal = await _hito_principal(con, h["partida_id"])
        con_diario = await con.fetchval(
            """SELECT EXISTS (SELECT 1 FROM ev_avances_diarios
               WHERE partida_id = $1 AND COALESCE(hito_id, $2) = $3)""",
            h["partida_id"], principal, hito_id)
        if con_diario:
            raise HTTPException(409, "Este hito se alimenta del registro diario; corrige las celdas del día")
        mp = float(h["mp"] or 0)
        if mp <= 0:
            raise HTTPException(400, "La partida no tiene metrado: define el presupuesto primero")
        base = await _fecha_base(con)
        semana = max(1, semana_de(f, base)) if base else 1
        async with con.transaction():
            await con.execute(
                """INSERT INTO ev_avances (hito_id, semana, cantidad_acum, registrado_en)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (hito_id, semana)
                   DO UPDATE SET cantidad_acum = $3, registrado_en = NOW()""",
                hito_id, semana, round(pct * mp, 4))
            # El acumulado no puede DECRECER en semanas posteriores ya escritas.
            await con.execute(
                """UPDATE ev_avances SET cantidad_acum = $3, registrado_en = NOW()
                   WHERE hito_id = $1 AND semana > $2 AND cantidad_acum < $3""",
                hito_id, semana, round(pct * mp, 4))
    return {"ok": True, "hito_id": hito_id, "semana": semana, "pct": pct}


@router.get("/actividades/{act_id}/dependencias")
async def listar_dependencias(act_id: int):
    pool = await db()
    rows = await pool.fetch(
        """SELECT d.id, d.predecesora_id, d.tipo, d.lag_dias,
                  p.titulo AS pred_titulo, p.fecha AS pred_fecha,
                  COALESCE(p.fecha_fin, p.fecha) AS pred_fecha_fin, p.estado AS pred_estado
           FROM prog_dependencias d
           JOIN prog_actividades p ON p.id = d.predecesora_id
           WHERE d.actividad_id = $1 ORDER BY d.id""", act_id)
    return [{**dict(r), "pred_fecha": str(r["pred_fecha"]),
             "pred_fecha_fin": str(r["pred_fecha_fin"])} for r in rows]


@router.post("/actividades/{act_id}/dependencias")
async def crear_dependencia(act_id: int, data: dict):
    pred_id = data.get("predecesora_id")
    if not pred_id:
        raise HTTPException(400, "predecesora_id requerida")
    pred_id = int(pred_id)
    if pred_id == act_id:
        raise HTTPException(400, "Una actividad no puede ser su propia antecesora")
    try:
        lag = int(data.get("lag_dias") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "lag_dias debe ser un entero")
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            if await _hay_ciclo(con, pred_id, act_id):
                raise HTTPException(409, "La dependencia crearía un ciclo (la actividad ya precede a esa antecesora)")
            try:
                row = await con.fetchrow(
                    """INSERT INTO prog_dependencias (actividad_id, predecesora_id, tipo, lag_dias)
                       VALUES ($1, $2, 'FS', $3)
                       ON CONFLICT (actividad_id, predecesora_id)
                       DO UPDATE SET lag_dias = $3 RETURNING *""",
                    act_id, pred_id, lag)
            except _ERRORES_DATO:
                raise HTTPException(400, "Actividad o antecesora inexistente")
            # La nueva dependencia puede exigir empujar la sucesora ya mismo.
            movidas = await recalcular_cascada(con, pred_id)
    return {**dict(row), "movidas": movidas}


@router.delete("/dependencias/{dep_id}")
async def borrar_dependencia(dep_id: int):
    pool = await db()
    n = await pool.execute("DELETE FROM prog_dependencias WHERE id = $1", dep_id)
    if n == "DELETE 0":
        raise HTTPException(404, "Dependencia no encontrada")
    return {"ok": True}


# ── Lookahead-grid: la vista tipo Excel del ex-gerente ───────
# Réplica del "Anexo 01 - LookAhead" / "F030b - Planeamiento": filas =
# actividades agrupadas por OTM, columnas = días de N semanas, con el
# metrado PROGRAMADO por día (prog_metrado_dia) y el REAL por día
# (ev_avances_diarios — la MISMA tabla del módulo de Valor Ganado, así el
# avance ingresado aquí o en EV es uno solo).
@router.get("/lookahead-grid")
async def lookahead_grid(proyecto_id: int = 1, desde: str = "", semanas: int = 4):
    semanas = max(1, min(int(semanas or 4), 8))
    base = _lunes_de(parse_fecha(desde) or fecha_lima())
    fin = base + timedelta(days=semanas * 7 - 1)
    pool = await db()
    async with pool.acquire() as con:
        acts = [dict(r) for r in await con.fetch(
            _ACT_SQL + """ WHERE a.proyecto_id = $1
                AND a.fecha <= $3 AND COALESCE(a.fecha_fin, a.fecha) >= $2
                ORDER BY a.otm_id NULLS LAST, a.fecha, a.id""",
            proyecto_id, base, fin)]
        ids = [a["id"] for a in acts]
        pids = sorted({a["partida_id"] for a in acts if a["partida_id"]})
        prog_rows = await con.fetch(
            """SELECT actividad_id, fecha::text AS f, cantidad, manual FROM prog_metrado_dia
               WHERE actividad_id = ANY($1) AND fecha BETWEEN $2 AND $3""",
            ids, base, fin) if ids else []
        real_rows = await con.fetch(
            """SELECT partida_id, hito_id, fecha::text AS f, cantidad_dia
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3""",
            pids, base, fin) if pids else []
        partidas = {r["id"]: dict(r) for r in await con.fetch(
            """SELECT id, unidad, metrado_presup FROM ev_partidas WHERE id = ANY($1)""",
            pids)} if pids else {}
        acum = {(r["partida_id"], r["hito_id"]): float(r["total"] or 0)
                for r in await con.fetch(
            """SELECT partida_id, hito_id, SUM(cantidad_dia) AS total
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) GROUP BY partida_id, hito_id""",
            pids)} if pids else {}
        hitos_rows = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, descripcion, peso, es_principal FROM ev_hitos
               WHERE partida_id = ANY($1)
               ORDER BY partida_id, es_principal DESC, peso DESC, id""",
            pids)] if pids else []
        dias_semana, feriados = await _calendario(con, proyecto_id, base, fin)
        deps = await con.fetch(
            """SELECT d.id AS dep_id, d.actividad_id, d.predecesora_id, d.lag_dias,
                      p.titulo AS pred_titulo, COALESCE(p.fecha_fin, p.fecha) AS pred_fin
               FROM prog_dependencias d
               JOIN prog_actividades p ON p.id = d.predecesora_id
               WHERE d.actividad_id = ANY($1) OR d.predecesora_id = ANY($1)""",
            ids) if ids else []

    preds_map: dict = {}
    sucs_map: dict = {}
    for r in deps:
        preds_map.setdefault(r["actividad_id"], []).append({
            "id": r["predecesora_id"], "dep_id": r["dep_id"], "titulo": r["pred_titulo"],
            "fecha_fin": str(r["pred_fin"]), "lag_dias": r["lag_dias"]})
        sucs_map.setdefault(r["predecesora_id"], []).append(r["actividad_id"])

    prog_map: dict = {}
    manual_map: dict = {}
    for r in prog_rows:
        prog_map.setdefault(r["actividad_id"], {})[r["f"]] = float(r["cantidad"])
        if r["manual"]:
            manual_map.setdefault(r["actividad_id"], []).append(r["f"])
    # Reales por (partida, etapa): NULL = hito principal (convención 0025).
    real_map: dict = {}
    for r in real_rows:
        if r["cantidad_dia"] is not None:
            real_map.setdefault((r["partida_id"], r["hito_id"]), {})[r["f"]] = \
                float(r["cantidad_dia"])
    principal_de: dict = {}
    hitos_de: dict = {}
    for h in hitos_rows:
        hitos_de.setdefault(h["partida_id"], []).append(h)
        principal_de.setdefault(h["partida_id"], h["id"])   # 1º = principal (ORDER BY)
    hito_info = {h["id"]: h for h in hitos_rows}

    grupos: list = []
    idx: dict = {}
    for a in acts:
        pinfo = partidas.get(a["partida_id"]) or {}
        met_base = float(pinfo["metrado_presup"]) if pinfo.get("metrado_presup") is not None else None
        # Clave de etapa de la actividad: el hito principal se guarda como NULL.
        hkey = a["hito_id"]
        if hkey is not None and hkey == principal_de.get(a["partida_id"]):
            hkey = None
        acum_real = acum.get((a["partida_id"], hkey)) if a["partida_id"] else None
        act_out = {
            "id": a["id"], "titulo": a["titulo"], "estado": a["estado"],
            "descripcion": a["descripcion"],
            "fecha": str(a["fecha"]), "fecha_fin": str(a["fecha_fin"] or a["fecha"]),
            "otm_id": a["otm_id"], "partida_id": a["partida_id"],
            "partida_codigo": a["partida_codigo"], "partida_desc": a["partida_desc"],
            "responsable": a["responsable"], "supervisor_id": a["supervisor_id"],
            "supervisor_nombre": a["supervisor_nombre"],
            "causa_nc": a["causa_nc"], "causa_nc_cat": a["causa_nc_cat"],
            "causa_nc_planner": a["causa_nc_planner"],
            "causa_nc_planner_cat": a["causa_nc_planner_cat"],
            "rest_pend": a["rest_pend"], "rest_total": a["rest_total"],
            "dias_salto": [str(d) for d in (a["dias_salto"] or [])],
            "dias_medio": [str(d) for d in (a["dias_medio"] or [])],
            "predecesoras": preds_map.get(a["id"], []),
            "sucesoras": sucs_map.get(a["id"], []),
            "dep_total": a["dep_total"],
            "und": pinfo.get("unidad") or a["und"],
            "metrado_prog": float(a["metrado_prog"]) if a["metrado_prog"] is not None else None,
            "metrado_base": met_base,
            "acum_real": acum_real,
            "saldo": round(met_base - acum_real, 3) if met_base is not None and acum_real is not None else None,
            "hito_id": a["hito_id"],
            "hito_desc": (hito_info.get(a["hito_id"]) or {}).get("descripcion") if a["hito_id"] else None,
            "hito_peso": float(hito_info[a["hito_id"]]["peso"]) if a["hito_id"] in hito_info else None,
            "prog": prog_map.get(a["id"], {}),
            "prog_manual": sorted(manual_map.get(a["id"], [])),
            "real": real_map.get((a["partida_id"], hkey), {}) if a["partida_id"] else {},
        }
        clave = a["otm_id"] or ""
        if clave not in idx:
            idx[clave] = {"otm_id": a["otm_id"], "otm_desc": a["otm_desc"], "actividades": []}
            grupos.append(idx[clave])
        idx[clave]["actividades"].append(act_out)

    sem_out = [{"lunes": str(base + timedelta(days=i * 7)),
                "domingo": str(base + timedelta(days=i * 7 + 6)),
                "fechas": [str(base + timedelta(days=i * 7 + d)) for d in range(7)]}
               for i in range(semanas)]
    return {"desde": str(base), "hasta": str(fin), "semanas": sem_out,
            "fechas": [str(base + timedelta(days=i)) for i in range(semanas * 7)],
            "dias_semana": sorted(dias_semana), "feriados": sorted(str(f) for f in feriados),
            "grupos": grupos, "cnc": CNC}


@router.put("/actividades/{act_id}/metrado-dias")
async def editar_metrado_dias(act_id: int, data: dict):
    """Replanificar un día del PROGRAMADO (la misma lógica del avance real,
    aplicada al plan — encargo Jean 2026-07-19): la celda escrita queda
    MANUAL (protegida, 0027) y el saldo del metrado META (que NO cambia) se
    re-prorratea en los demás días hábiles sin real y sin celda manual.
    cantidad null/'' libera la celda: vuelve al prorrateo automático.
    cantidad 0 = día sin programación (protegido en 0)."""
    dias = data.get("dias") or {}
    if not isinstance(dias, dict) or not dias:
        raise HTTPException(400, "dias requerido: {\"YYYY-MM-DD\": cantidad}")
    celdas = []
    for k, v in dias.items():
        f = parse_fecha(k)
        if not f:
            raise HTTPException(400, f"fecha inválida: {k}")
        if v in (None, ""):
            celdas.append((f, None))
            continue
        try:
            cant = float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"cantidad inválida para {k}")
        if cant < 0:
            raise HTTPException(400, "la cantidad no puede ser negativa")
        celdas.append((f, cant))
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            act = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", act_id)
            if not act:
                raise HTTPException(404, "Actividad no encontrada")
            for f, cant in celdas:
                if cant is None:
                    await con.execute(
                        "DELETE FROM prog_metrado_dia WHERE actividad_id = $1 AND fecha = $2",
                        act_id, f)
                else:
                    await con.execute(
                        """INSERT INTO prog_metrado_dia (actividad_id, fecha, cantidad, manual)
                           VALUES ($1,$2,$3,true)
                           ON CONFLICT (actividad_id, fecha)
                           DO UPDATE SET cantidad = $3, manual = true""",
                        act_id, f, cant)
            await _redistribuir(con, dict(act))
    m = float(act["metrado_prog"]) if act["metrado_prog"] is not None else None
    return {"ok": True, "metrado_prog": m}


@router.post("/avance-dia")
async def avance_dia(data: dict):
    """Registra el metrado REAL ejecutado de una partida en un día — escribe
    en ev_avances_diarios, la misma tabla del módulo de Valor Ganado (2 vías,
    un solo dato) y re-prorratea la actividad vinculada si existe.
    cantidad null borra el registro del día."""
    partida_id = data.get("partida_id")
    f = parse_fecha(data.get("fecha"))
    if not partida_id or not f:
        raise HTTPException(400, "partida_id y fecha son obligatorios")
    cantidad = _parse_cantidad(data.get("cantidad"))
    hito_id = int(data["hito_id"]) if data.get("hito_id") else None
    pool = await db()
    async with pool.acquire() as con:
        if hito_id:
            await _validar_hito(con, int(partida_id), hito_id)
        async with con.transaction():
            try:
                await registrar_avance_partida(con, int(partida_id), f, cantidad,
                                               hito_id=hito_id)
            except _ERRORES_DATO:
                raise HTTPException(400, "Partida inexistente o datos inválidos")
    return {"ok": True, "cantidad": cantidad}


@router.get("/historial-grid")
async def historial_grid(otm: str, desde: str = "", semanas: int = 0):
    """Historial COMPLETO de avance de las partidas de una OTM, estilo Excel
    del LookAhead (tab «Avance diario» del VG): arranca en el primer registro
    (avance diario o HH de tareo, lo más antiguo) y llega hasta hoy o hasta
    el fin programado. Editable en todo el rango — misma fuente única.
    Por partida: fila por ETAPA (hito con actividad/registros propios; NULL =
    principal) con real diario, programado (prog_metrado_dia) y HH del tareo.
    desde/semanas permiten acotar la ventana (máx. 26 semanas por página)."""
    otm = otm.strip()
    if not otm:
        raise HTTPException(400, "otm requerida")
    pool = await db()
    async with pool.acquire() as con:
        partidas = [dict(r) for r in await con.fetch(
            """SELECT id, codigo, descripcion, unidad, hh_presup, metrado_presup,
                      COALESCE(metrado_proyec, metrado_presup) AS metrado,
                      CASE WHEN metrado_presup > 0 THEN hh_presup / metrado_presup
                           ELSE 0 END AS factor_conv
               FROM ev_partidas
               WHERE activo AND fase IS NOT NULL AND otm_id = $1
               ORDER BY codigo""", otm)]
        if not partidas:
            return {"otm": otm, "partidas": [], "fechas": [], "semanas": []}
        pids = [p["id"] for p in partidas]

        lim = await con.fetchrow(
            """SELECT LEAST((SELECT MIN(fecha) FROM ev_avances_diarios WHERE partida_id = ANY($1)),
                            (SELECT MIN(fecha) FROM tareo_partida WHERE partida_id = ANY($1))) AS ini,
                      GREATEST((SELECT MAX(fecha) FROM ev_avances_diarios WHERE partida_id = ANY($1)),
                               (SELECT MAX(COALESCE(fecha_fin, fecha)) FROM prog_actividades
                                WHERE partida_id = ANY($1))) AS fin""", pids)
        hoy = fecha_lima()
        ini = lim["ini"] or hoy
        fin = max(lim["fin"] or hoy, hoy)
        base = _lunes_de(parse_fecha(desde) or ini)
        fin_dom = fin + timedelta(days=6 - fin.weekday())
        n_sem = min(max(1, int(semanas) or ((fin_dom - base).days + 1) // 7), 26)
        fin_dom = min(fin_dom, base + timedelta(days=n_sem * 7 - 1))
        truncado = (base + timedelta(days=n_sem * 7 - 1)) < fin

        real_rows = await con.fetch(
            """SELECT partida_id, hito_id, fecha::text AS f, cantidad_dia
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3
                 AND cantidad_dia IS NOT NULL""", pids, base, fin_dom)
        hh_rows = await con.fetch(
            """SELECT partida_id, fecha::text AS f, SUM(hh) AS hh
               FROM tareo_partida
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3 AND hh IS NOT NULL
               GROUP BY partida_id, fecha""", pids, base, fin_dom)
        acts = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, hito_id, fecha, fecha_fin, estado,
                      dias_salto, dias_medio, metrado_prog
               FROM prog_actividades
               WHERE partida_id = ANY($1)
               ORDER BY fecha""", pids)]
        prog_rows = await con.fetch(
            """SELECT pm.actividad_id, pm.fecha::text AS f, pm.cantidad,
                      pa.partida_id, pa.hito_id
               FROM prog_metrado_dia pm
               JOIN prog_actividades pa ON pa.id = pm.actividad_id
               WHERE pa.partida_id = ANY($1) AND pm.fecha BETWEEN $2 AND $3""",
            pids, base, fin_dom)
        hitos_rows = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, descripcion, peso, es_principal FROM ev_hitos
               WHERE partida_id = ANY($1)
               ORDER BY partida_id, es_principal DESC, peso DESC, id""", pids)]
        primer_reg = {r["partida_id"]: r["ini"] for r in await con.fetch(
            """SELECT partida_id, LEAST(MIN(f1), MIN(f2)) AS ini FROM (
                 SELECT partida_id, fecha AS f1, NULL::date AS f2
                   FROM ev_avances_diarios WHERE partida_id = ANY($1)
                 UNION ALL
                 SELECT partida_id, NULL::date, fecha FROM tareo_partida
                  WHERE partida_id = ANY($1)) x GROUP BY partida_id""", pids)}
        dias_semana, feriados = await _calendario(con, 1, base, fin_dom)

    principal_de: dict = {}
    hito_info: dict = {}
    for h in hitos_rows:
        principal_de.setdefault(h["partida_id"], h["id"])
        hito_info[h["id"]] = h

    def _hkey(pid, hid):
        return None if hid is None or hid == principal_de.get(pid) else hid

    real_map: dict = {}
    for r in real_rows:
        real_map.setdefault((r["partida_id"], _hkey(r["partida_id"], r["hito_id"])),
                            {})[r["f"]] = float(r["cantidad_dia"])
    hh_map: dict = {}
    for r in hh_rows:
        hh_map.setdefault(r["partida_id"], {})[r["f"]] = float(r["hh"])
    prog_map: dict = {}
    for r in prog_rows:
        k = (r["partida_id"], _hkey(r["partida_id"], r["hito_id"]))
        prog_map.setdefault(k, {})[r["f"]] = \
            prog_map.get(k, {}).get(r["f"], 0) + float(r["cantidad"])
    acts_map: dict = {}
    for a in acts:
        acts_map.setdefault((a["partida_id"], _hkey(a["partida_id"], a["hito_id"])),
                            []).append({
            "id": a["id"], "fecha": str(a["fecha"]),
            "fecha_fin": str(a["fecha_fin"] or a["fecha"]), "estado": a["estado"],
            "dias_salto": [str(d) for d in (a["dias_salto"] or [])],
            "dias_medio": [str(d) for d in (a["dias_medio"] or [])],
            "metrado_prog": float(a["metrado_prog"]) if a["metrado_prog"] is not None else None,
        })

    out = []
    for p in partidas:
        pid = p["id"]
        claves = sorted(
            {k[1] for m in (real_map, prog_map, acts_map) for k in m if k[0] == pid},
            key=lambda h: (h is not None, h or 0))
        if not claves:
            claves = [None]
        etapas = [{
            "hito_id": hk,
            "hito_desc": hito_info[hk]["descripcion"] if hk in hito_info else None,
            "hito_peso": float(hito_info[hk]["peso"]) if hk in hito_info else None,
            "real": real_map.get((pid, hk), {}),
            "prog": prog_map.get((pid, hk), {}),
            "actividades": acts_map.get((pid, hk), []),
        } for hk in claves]
        out.append({
            "id": pid, "codigo": p["codigo"], "descripcion": p["descripcion"],
            "unidad": p["unidad"], "metrado": float(p["metrado"] or 0),
            "factor_conv": round(float(p["factor_conv"] or 0), 4),
            "hh_presup": float(p["hh_presup"] or 0),
            "hh": hh_map.get(pid, {}),
            "primer_registro": str(primer_reg[pid]) if primer_reg.get(pid) else None,
            "sin_registros": primer_reg.get(pid) is None,
            "etapas": etapas,
        })

    n_dias = (fin_dom - base).days + 1
    sem_out = [{"lunes": str(base + timedelta(days=i * 7)),
                "domingo": str(base + timedelta(days=i * 7 + 6)),
                "fechas": [str(base + timedelta(days=i * 7 + d)) for d in range(7)]}
               for i in range(n_dias // 7)]
    return {"otm": otm, "desde": str(base), "hasta": str(fin_dom),
            "truncado": truncado, "hoy": str(hoy),
            "fechas": [str(base + timedelta(days=i)) for i in range(n_dias)],
            "semanas": sem_out, "dias_semana": sorted(dias_semana),
            "feriados": sorted(str(f) for f in feriados), "partidas": out}


@router.get("/actividades/{act_id}/restricciones")
async def listar_restricciones(act_id: int):
    pool = await db()
    rows = await pool.fetch(
        "SELECT * FROM prog_restricciones WHERE actividad_id = $1 ORDER BY liberada, id",
        act_id)
    return [dict(r) for r in rows]


@router.post("/actividades/{act_id}/restricciones")
async def crear_restriccion(act_id: int, data: dict):
    desc = str(data.get("descripcion") or "").strip()
    if not desc:
        raise HTTPException(400, "descripcion requerida")
    tipo = str(data.get("tipo") or "OTROS").strip().upper()
    if tipo not in _TIPOS_RESTRICCION:
        raise HTTPException(422, f"tipo inválido (usa {'/'.join(_TIPOS_RESTRICCION)})")
    pool = await db()
    try:
        row = await pool.fetchrow(
            """INSERT INTO prog_restricciones
               (actividad_id, descripcion, tipo, responsable, fecha_requerida)
               VALUES ($1,$2,$3,$4,$5) RETURNING *""",
            act_id, desc, tipo, data.get("responsable") or None,
            parse_fecha(data.get("fecha_requerida")))
    except _ERRORES_DATO:
        raise HTTPException(400, "Actividad inexistente o datos inválidos")
    return dict(row)


@router.put("/restricciones/{rest_id}")
async def editar_restriccion(rest_id: int, data: dict):
    campos, valores = [], []
    for k in ("descripcion", "responsable"):
        if k in data:
            campos.append(f"{k} = ${len(valores) + 2}")
            valores.append(str(data[k]).strip() or None)
    if "fecha_requerida" in data:
        campos.append(f"fecha_requerida = ${len(valores) + 2}")
        valores.append(parse_fecha(data["fecha_requerida"]))
    if "liberada" in data:
        lib = bool(data["liberada"])
        campos.append(f"liberada = ${len(valores) + 2}")
        valores.append(lib)
        campos.append("liberada_en = " + ("now()" if lib else "NULL"))
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    pool = await db()
    row = await pool.fetchrow(
        f"UPDATE prog_restricciones SET {', '.join(campos)} WHERE id = $1 RETURNING *",
        rest_id, *valores)
    if not row:
        raise HTTPException(404, "Restricción no encontrada")
    return dict(row)


@router.delete("/restricciones/{rest_id}")
async def borrar_restriccion(rest_id: int):
    pool = await db()
    n = await pool.execute("DELETE FROM prog_restricciones WHERE id = $1", rest_id)
    if n == "DELETE 0":
        raise HTTPException(404, "Restricción no encontrada")
    return {"ok": True}


@router.get("/ppc")
async def ppc(proyecto_id: int = 1, semanas: int = 8):
    """PPC (Porcentaje de Plan Cumplido) semanal + Pareto de causas + detalle
    por supervisor — el nivel de APRENDIZAJE del LPS.

    Cumplimiento AUTOMÁTICO por metrado (encargo Jean 2026-07-19, «al cierre
    + SI anticipado»): el compromiso de una actividad en una semana es su
    metrado PROGRAMADO de esa semana; se cumple apenas el real registrado lo
    alcanza o supera (aunque la semana siga corriendo) y recién cuenta como
    NO cumplida cuando la semana CERRÓ sin llegar — mientras corre queda "en
    curso" (comprometida sin veredicto). Los estados manuales mandan:
    NO_CUMPLIDA → no cumplida (su causa alimenta el Pareto) · EJECUTADO →
    cumplida · CANCELADO se excluye. Las actividades sin celdas programadas
    (sin metrado) se evalúan por estado en la semana de su F.Inicio."""
    semanas = max(1, min(int(semanas or 8), 26))
    hoy = fecha_lima()
    hasta = _lunes_de(hoy) + timedelta(days=6)
    desde = _lunes_de(hoy) - timedelta(days=(semanas - 1) * 7)
    pool = await db()
    async with pool.acquire() as con:
        acts = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, hito_id, estado, fecha, supervisor_id
               FROM prog_actividades
               WHERE proyecto_id = $1
                 AND fecha <= $3 AND COALESCE(fecha_fin, fecha) >= $2""",
            proyecto_id, desde, hasta)]
        ids = [a["id"] for a in acts]
        pids = sorted({a["partida_id"] for a in acts if a["partida_id"]})
        prog_rows = await con.fetch(
            """SELECT actividad_id,
                      (fecha - ((EXTRACT(ISODOW FROM fecha)::int) - 1)) AS lunes,
                      SUM(cantidad) AS c
               FROM prog_metrado_dia
               WHERE actividad_id = ANY($1) AND fecha BETWEEN $2 AND $3
               GROUP BY 1, 2""", ids, desde, hasta) if ids else []
        real_rows = await con.fetch(
            """SELECT partida_id, hito_id,
                      (fecha - ((EXTRACT(ISODOW FROM fecha)::int) - 1)) AS lunes,
                      SUM(cantidad_dia) AS c
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3
                 AND cantidad_dia IS NOT NULL
               GROUP BY 1, 2, 3""", pids, desde, hasta) if pids else []
        principal = {r["partida_id"]: r["id"] for r in await con.fetch(
            """SELECT DISTINCT ON (partida_id) partida_id, id FROM ev_hitos
               WHERE partida_id = ANY($1)
               ORDER BY partida_id, es_principal DESC, peso DESC, id""",
            pids)} if pids else {}
        supervisores = {r["id"]: r["nombre"] for r in await con.fetch(
            "SELECT id, nombre FROM supervisores")}
        # Pareto (F3 v2): manda la causa del PLANNER; si no existe, la de campo.
        cnc_rows = await con.fetch(
            """SELECT COALESCE(causa_nc_planner_cat, causa_nc_cat, 'OTROS') AS causa, count(*) AS n
               FROM prog_actividades
               WHERE proyecto_id = $1 AND estado = 'NO_CUMPLIDA' AND fecha BETWEEN $2 AND $3
               GROUP BY 1 ORDER BY n DESC""", proyecto_id, desde, hasta)

    prog_de: dict = {}
    for r in prog_rows:
        prog_de.setdefault(r["actividad_id"], {})[r["lunes"]] = float(r["c"] or 0)
    reales = {(r["partida_id"], r["hito_id"], r["lunes"]): float(r["c"] or 0)
              for r in real_rows}

    def _etapa(a: dict):
        """Etapa normalizada: el hito principal se guarda como NULL en el diario."""
        h = a["hito_id"]
        return None if h is not None and h == principal.get(a["partida_id"]) else h

    sem: dict = {}
    sup: dict = {}

    def _suma(lunes, sup_id, comp, cump, noc):
        s = sem.setdefault(lunes, {"comprometidas": 0, "cumplidas": 0, "no_cumplidas": 0})
        s["comprometidas"] += comp; s["cumplidas"] += cump; s["no_cumplidas"] += noc
        if sup_id:
            v = sup.setdefault(sup_id, {"comprometidas": 0, "cumplidas": 0})
            v["comprometidas"] += comp; v["cumplidas"] += cump

    for a in acts:
        if a["estado"] == "CANCELADO":
            continue
        por_semana = prog_de.get(a["id"])
        if por_semana:
            for lun, comprom in por_semana.items():
                if comprom <= 0:
                    continue
                alcanz = (reales.get((a["partida_id"], _etapa(a), lun), 0.0)
                          if a["partida_id"] else 0.0)
                cerrada = lun + timedelta(days=6) < hoy
                if a["estado"] == "NO_CUMPLIDA":
                    cump, noc = 0, 1
                elif a["estado"] == "EJECUTADO" or alcanz >= comprom - 5e-4:
                    cump, noc = 1, 0
                elif cerrada:
                    cump, noc = 0, 1
                else:
                    cump, noc = 0, 0                    # en curso: sin veredicto
                _suma(lun, a["supervisor_id"], 1, cump, noc)
        else:
            lun = _lunes_de(a["fecha"])
            if not (desde <= lun <= hasta):
                continue
            _suma(lun, a["supervisor_id"], 1,
                  1 if a["estado"] == "EJECUTADO" else 0,
                  1 if a["estado"] == "NO_CUMPLIDA" else 0)

    def _ppc(c, e):
        return round(e / c, 4) if c else None

    return {
        "desde": str(desde), "hasta": str(hasta), "cnc_catalogo": CNC,
        "semanal": [{"lunes": str(lun), "comprometidas": v["comprometidas"],
                     "cumplidas": v["cumplidas"], "no_cumplidas": v["no_cumplidas"],
                     "ppc": _ppc(v["comprometidas"], v["cumplidas"])}
                    for lun, v in sorted(sem.items())],
        "cnc": [{"causa": r["causa"], "etiqueta": CNC.get(r["causa"], r["causa"]),
                 "n": r["n"]} for r in cnc_rows],
        "por_supervisor": [{"supervisor_id": sid, "nombre": supervisores.get(sid),
                            "comprometidas": v["comprometidas"], "cumplidas": v["cumplidas"],
                            "ppc": _ppc(v["comprometidas"], v["cumplidas"])}
                           for sid, v in sorted(sup.items(),
                                                key=lambda kv: supervisores.get(kv[0]) or "")],
    }


# ── Almacenamiento (indicador semanal + purga manual) ────────
@router.get("/media-uso")
async def media_uso(proyecto_id: int = 1):
    """Uso de disco por semana ISO (los bytes viven en BD: no hay que escanear)."""
    pool = await db()
    rows = await pool.fetch(
        """SELECT f.semana_iso, count(*) AS n_fotos,
                  count(*) FILTER (WHERE f.purgada) AS n_purgadas,
                  COALESCE(SUM((f.bytes + f.bytes_thumb)) FILTER (WHERE NOT f.purgada), 0) AS bytes_en_disco
           FROM campo_fotos f JOIN campo_reportes r ON r.id = f.reporte_id
           WHERE r.proyecto_id = $1
           GROUP BY f.semana_iso ORDER BY f.semana_iso DESC""", proyecto_id)
    return [dict(r) for r in rows]


@router.post("/purgar")
async def purgar_semana(data: dict, user: dict = Depends(require_role("oficina"))):
    """Borra del disco las fotos de una semana (originales + thumbs) y las marca
    purgadas. El reporte (texto/fecha/autor) SE CONSERVA. Exportar el reporte
    semanal ANTES de purgar es responsabilidad del usuario (el panel lo recuerda)."""
    semana_iso = str(data.get("semana_iso") or "").strip()
    proyecto_id = int(data.get("proyecto_id") or 1)
    if not semana_iso:
        raise HTTPException(400, "semana_iso requerida (ej. 2026-W28)")
    pool = await db()
    async with pool.acquire() as con:
        fotos = await con.fetch(
            """SELECT f.id, f.ruta, f.ruta_thumb FROM campo_fotos f
               JOIN campo_reportes r ON r.id = f.reporte_id
               WHERE r.proyecto_id = $1 AND f.semana_iso = $2 AND NOT f.purgada""",
            proyecto_id, semana_iso)
        n, liberados = await _purgar_fotos(con, fotos)
    log.info("purga de media", extra={"semana": semana_iso, "fotos": n,
                                      "bytes": liberados, "por": user.get("sub")})
    return {"fotos_purgadas": n, "bytes_liberados": liberados}


async def _purgar_fotos(con, fotos) -> tuple:
    """Borra del disco (original + thumb) y marca purgada. Devuelve (n, bytes)."""
    liberados = 0
    for f in fotos:
        for ruta in (f["ruta"], f["ruta_thumb"]):
            p = media_dir() / ruta
            if p.is_file():
                liberados += p.stat().st_size
                p.unlink()
    if fotos:
        await con.execute(
            "UPDATE campo_fotos SET purgada = true WHERE id = ANY($1::int[])",
            [f["id"] for f in fotos])
    return len(fotos), liberados


async def purgar_semanas_antiguas() -> dict:
    """Purga AUTOMÁTICA por retención (decisión de Jean: conservar ~2 meses para
    que el ingeniero de costos pueda revisar; el PDF semanal es el archivo
    permanente). Purga las semanas ISO cuyo lunes es más viejo que
    MEDIA_RETENCION_SEMANAS. La corre el loop diario del lifespan."""
    from core import config
    corte = fecha_lima() - timedelta(weeks=config.MEDIA_RETENCION_SEMANAS)
    pool = await db()
    total_n, total_b = 0, 0
    async with pool.acquire() as con:
        semanas = [r["semana_iso"] for r in await con.fetch(
            "SELECT DISTINCT semana_iso FROM campo_fotos WHERE NOT purgada")]
        for semana in semanas:
            lunes = semana_iso_a_lunes(semana)
            if lunes is None or lunes >= corte:
                continue
            fotos = await con.fetch(
                "SELECT id, ruta, ruta_thumb FROM campo_fotos "
                "WHERE semana_iso = $1 AND NOT purgada", semana)
            n, b = await _purgar_fotos(con, fotos)
            total_n += n
            total_b += b
            log.info("purga automática", extra={"semana": semana, "fotos": n, "bytes": b})
    return {"fotos_purgadas": total_n, "bytes_liberados": total_b}


async def purga_automatica_loop():
    """Tarea de fondo (lifespan): purga por retención al arrancar y cada 24 h."""
    while True:
        try:
            await purgar_semanas_antiguas()
        except Exception:
            log.exception("purga automática falló (reintenta en 24h)")
        await asyncio.sleep(24 * 3600)


# ── Campo: el supervisor reporta con fotos ───────────────────
@router_campo.get("/programacion-dia")
async def programacion_dia(fecha: str = "", otm_id: str = ""):
    """Actividades programadas del día para una OTM (para reportar 'contra' ellas)."""
    f = parse_fecha(fecha) or fecha_lima()
    pool = await db()
    rows = await pool.fetch(
        """SELECT id, titulo, estado FROM prog_actividades
           WHERE $1 BETWEEN fecha AND COALESCE(fecha_fin, fecha)
             AND NOT ($1 = ANY(dias_salto))
             AND estado <> 'CANCELADO'
             AND (otm_id = $2 OR otm_id IS NULL)
           ORDER BY id""", f, otm_id or None)
    return [dict(r) for r in rows]


@router_campo.get("/mis-actividades")
async def mis_actividades(supervisor_id: str, fecha: str = "",
                          user: dict = Depends(require_role())):
    """Las actividades del día asignadas a ESTE supervisor (todas sus OTMs) —
    el flujo A de la app de campo: la agenda que el planner le dejó."""
    exigir_identidad_supervisor(user, supervisor_id)
    f = parse_fecha(fecha) or fecha_lima()
    pool = await db()
    rows = await pool.fetch(
        """SELECT a.id, a.titulo, a.descripcion, a.estado, a.causa_nc, a.causa_nc_cat,
                  a.otm_id, o.descripcion AS otm_desc, a.responsable,
                  ev.codigo AS partida_codigo, ev.descripcion AS partida_desc,
                  COALESCE(ev.unidad, a.und) AS und,
                  pm.cantidad AS metrado_dia
           FROM prog_actividades a
           LEFT JOIN otms o ON o.id = a.otm_id
           LEFT JOIN ev_partidas ev ON ev.id = a.partida_id
           LEFT JOIN prog_metrado_dia pm ON pm.actividad_id = a.id AND pm.fecha = $1
           WHERE $1 BETWEEN a.fecha AND COALESCE(a.fecha_fin, a.fecha)
             AND NOT ($1 = ANY(a.dias_salto))
             AND a.supervisor_id = $2 AND a.estado <> 'CANCELADO'
           ORDER BY a.id""", f, supervisor_id)
    return [dict(r) for r in rows]


@router_campo.post("/actividades/{act_id}/no-cumplida")
async def marcar_no_cumplida(act_id: int, data: dict,
                             user: dict = Depends(require_role())):
    """El supervisor registra la CAUSA DE NO CUMPLIMIENTO de una actividad
    programada que no se ejecutó (Last Planner): categoría del catálogo CNC
    (obligatoria, para el Pareto) + detalle libre (opcional)."""
    supervisor_id = str(data.get("supervisor_id") or "").strip()
    causa = str(data.get("causa") or "").strip()
    causa_cat = _validar_cnc(data.get("causa_cat"))
    exigir_identidad_supervisor(user, supervisor_id)
    if not causa_cat and not causa:
        raise HTTPException(400, "Indica la causa de no cumplimiento")
    pool = await db()
    row = await pool.fetchrow(
        """UPDATE prog_actividades
           SET estado = 'NO_CUMPLIDA', causa_nc = $2, causa_nc_cat = $3,
               actualizado_en = now()
           WHERE id = $1 AND estado = 'PROGRAMADO' RETURNING id""",
        act_id, causa or None, causa_cat or "OTROS")
    if not row:
        raise HTTPException(409, "La actividad no existe o ya no está PROGRAMADO")
    return {"ok": True, "estado": "NO_CUMPLIDA"}


@router_campo.post("/reportes")
async def crear_reporte(
    proyecto_id: int = Form(1),
    fecha: str = Form(""),
    otm_id: str = Form(...),
    supervisor_id: str = Form(...),
    descripcion: str = Form(""),
    actividad_id: Optional[int] = Form(None),
    fotos: List[UploadFile] = File(default=[]),
    user: dict = Depends(require_role()),
):
    exigir_identidad_supervisor(user, supervisor_id)
    f_rep = parse_fecha(fecha) or fecha_lima()
    if not descripcion.strip() and not fotos:
        raise HTTPException(400, "El reporte necesita una descripción o al menos una foto")
    if len(fotos) > MAX_FOTOS_POR_REPORTE:
        raise HTTPException(422, f"Máximo {MAX_FOTOS_POR_REPORTE} fotos por reporte")

    # Procesar/escribir las fotos ANTES de la transacción (si algo falla, 4xx limpio).
    guardadas = []
    for up in fotos:
        data = await up.read()
        if len(data) > MAX_FOTO_BYTES:
            raise HTTPException(413, f"{up.filename}: supera el máximo de 8 MB")
        try:
            guardadas.append(guardar_foto(f_rep, data))
        except ValueError as e:
            raise HTTPException(415, f"{up.filename}: {e}")

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            try:
                rid = await con.fetchval(
                    """INSERT INTO campo_reportes
                       (proyecto_id, fecha, otm_id, actividad_id, supervisor_id, descripcion)
                       VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                    proyecto_id, f_rep, otm_id.strip(), actividad_id,
                    supervisor_id.strip(), descripcion.strip() or None)
            except _ERRORES_DATO:
                raise HTTPException(400, "OTM, actividad o supervisor inválido: revisa los datos")
            for g in guardadas:
                await con.execute(
                    """INSERT INTO campo_fotos
                       (reporte_id, semana_iso, ruta, ruta_thumb, bytes, bytes_thumb, ancho, alto)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    rid, g["semana_iso"], g["ruta"], g["ruta_thumb"],
                    g["bytes"], g["bytes_thumb"], g["ancho"], g["alto"])
            # Calendario combinado: el reporte "ejecuta" la actividad programada.
            if actividad_id:
                await con.execute(
                    "UPDATE prog_actividades SET estado='EJECUTADO', actualizado_en=now() "
                    "WHERE id = $1 AND estado = 'PROGRAMADO'", actividad_id)
    return {"ok": True, "id": rid, "fotos": len(guardadas)}
