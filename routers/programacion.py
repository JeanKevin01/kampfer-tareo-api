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
from core.tiempo import fecha_lima, parse_fecha

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


def _distribuir(metrado: float, dias: list) -> dict:
    """Distribución uniforme del metrado entre los días dados, como la fórmula
    del LookAhead del ex-gerente. El último día absorbe el redondeo."""
    if not dias:
        return {}
    cuota = round(metrado / len(dias), 3)
    out = {d: cuota for d in dias}
    out[dias[-1]] = round(metrado - cuota * (len(dias) - 1), 3)
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


async def _redistribuir(con, act: dict) -> None:
    """Recalcula la distribución diaria del metrado de la actividad:
      · salta los días no laborables (prog_config + prog_feriados) y los
        saltos intencionales de la actividad (dias_salto);
      · los días que YA tienen avance real registrado quedan CONGELADOS
        (su programado es la línea contra la que se compara el cumplimiento)
        y el SALDO (metrado − real acumulado) se re-prorratea entre los
        días hábiles restantes — así la actividad sigue apuntando a terminar
        en su F.Fin con lo que falta."""
    desde, hasta = act["fecha"], act["fecha_fin"] or act["fecha"]
    dias_semana, feriados = await _calendario(con, act["proyecto_id"], desde, hasta)
    saltos = set(act.get("dias_salto") or [])
    habiles = _dias_habiles(desde, hasta, dias_semana, feriados, saltos)

    reales: dict = {}
    if act.get("partida_id"):
        reales = {r["fecha"]: float(r["cantidad_dia"]) for r in await con.fetch(
            """SELECT fecha, cantidad_dia FROM ev_avances_diarios
               WHERE partida_id = $1 AND fecha BETWEEN $2 AND $3
                 AND cantidad_dia IS NOT NULL""",
            act["partida_id"], desde, hasta)}

    # Se borran solo las celdas NO congeladas (las de días con real se quedan).
    await con.execute(
        "DELETE FROM prog_metrado_dia WHERE actividad_id = $1"
        " AND NOT (fecha = ANY($2::date[]))", act["id"], list(reales))
    metrado = float(act["metrado_prog"] or 0)
    if metrado <= 0:
        return
    saldo = round(metrado - sum(reales.values()), 3)
    restantes = [d for d in habiles if d not in reales]
    if saldo <= 0 or not restantes:
        return
    await con.executemany(
        "INSERT INTO prog_metrado_dia (actividad_id, fecha, cantidad) VALUES ($1,$2,$3)"
        " ON CONFLICT (actividad_id, fecha) DO UPDATE SET cantidad = $3",
        [(act["id"], f, c) for f, c in _distribuir(saldo, restantes).items() if c > 0])


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
             WHERE pr.actividad_id = a.id AND NOT pr.liberada) AS rest_pend
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
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            try:
                row = await con.fetchrow(
                    """INSERT INTO prog_actividades
                       (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo, descripcion,
                        responsable, supervisor_id, metrado_prog, und, dias_salto, creado_por)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
                    int(data.get("proyecto_id") or 1), fecha, fecha_fin,
                    (str(data["otm_id"]).strip() or None) if data.get("otm_id") else None,
                    int(data["partida_id"]) if data.get("partida_id") else None,
                    titulo, data.get("descripcion") or None, data.get("responsable") or None,
                    (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None,
                    metrado, und, saltos, user.get("sub"))
            except _ERRORES_DATO:
                raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
            await _redistribuir(con, dict(row))
    return dict(row)


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
    if "causa_nc_cat" in data:
        campos.append("causa_nc_cat"); valores.append(_validar_cnc(data["causa_nc_cat"]))
    for k in ("titulo", "descripcion", "responsable", "otm_id", "supervisor_id", "causa_nc"):
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
            # Si cambió el rango, el metrado o los saltos, la distribución
            # diaria se recalcula (las ediciones celda a celda van por
            # /actividades/{id}/metrado-dias y NO pasan por aquí).
            if {"fecha", "fecha_fin", "metrado_prog", "dias_salto"} & data.keys():
                await _redistribuir(con, dict(row))
    return dict(row)


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


@router.post("/actividades/{act_id}/avance-dia")
async def avance_dia_actividad(act_id: int, data: dict):
    """El avance REAL del día contra una actividad del LookAhead: escribe en
    ev_avances_diarios (la partida de control de la actividad) y RE-PRORRATEA
    el saldo entre los días hábiles restantes — la actividad sigue apuntando
    a terminar en su F.Fin. El programado del día avanzado queda congelado
    como línea base de comparación (celeste→verde/ámbar/rojo en el panel)."""
    from routers.ev._datos import _fecha_base
    f = parse_fecha(data.get("fecha"))
    if not f:
        raise HTTPException(400, "fecha requerida")
    cantidad = data.get("cantidad")
    if cantidad not in (None, ""):
        try:
            cantidad = float(cantidad)
        except (TypeError, ValueError):
            raise HTTPException(400, "cantidad debe ser un número")
        if cantidad < 0:
            raise HTTPException(400, "la cantidad no puede ser negativa")
    else:
        cantidad = None
    pool = await db()
    async with pool.acquire() as con:
        act = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", act_id)
        if not act:
            raise HTTPException(404, "Actividad no encontrada")
        if not act["partida_id"]:
            raise HTTPException(400, "La actividad no tiene partida de control: asígnala para registrar avance")
        async with con.transaction():
            if cantidad is None:
                await con.execute(
                    "DELETE FROM ev_avances_diarios WHERE partida_id = $1 AND fecha = $2",
                    act["partida_id"], f)
            else:
                base = await _fecha_base(con)
                semana = max(1, (f - base).days // 7 + 1) if base else 1
                await con.execute(
                    """INSERT INTO ev_avances_diarios
                         (partida_id, fecha, semana, cantidad_dia, registrado_en)
                       VALUES ($1, $2, $3, $4, NOW())
                       ON CONFLICT (partida_id, fecha)
                       DO UPDATE SET cantidad_dia = $4, registrado_en = NOW()""",
                    act["partida_id"], f, semana, cantidad)
            await _redistribuir(con, dict(act))
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
        parsed.append((int(pid), fecha, fecha_fin, _parse_metrado(it.get("metrado_prog"))))

    proyecto_id = int(data.get("proyecto_id") or 1)
    supervisor_id = (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None
    responsable = data.get("responsable") or None
    descripcion = data.get("descripcion") or None

    pool = await db()
    creadas = []
    async with pool.acquire() as con:
        pinfo = {r["id"]: dict(r) for r in await con.fetch(
            "SELECT id, descripcion, metrado_presup FROM ev_partidas WHERE id = ANY($1)",
            [p[0] for p in parsed])}
        faltan = [str(p[0]) for p in parsed if p[0] not in pinfo]
        if faltan:
            raise HTTPException(400, f"Partidas inexistentes: {', '.join(faltan)}")
        async with con.transaction():
            for pid, fecha, fecha_fin, metrado in parsed:
                p = pinfo[pid]
                if metrado is None and p["metrado_presup"] is not None:
                    metrado = float(p["metrado_presup"]) or None
                try:
                    row = await con.fetchrow(
                        """INSERT INTO prog_actividades
                           (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo,
                            descripcion, responsable, supervisor_id, metrado_prog, creado_por)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
                        proyecto_id, fecha, fecha_fin, otm_id, pid,
                        (p["descripcion"] or f"Partida {pid}")[:200],
                        descripcion, responsable, supervisor_id, metrado, user.get("sub"))
                except _ERRORES_DATO:
                    raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
                await _redistribuir(con, dict(row))
                creadas.append(dict(row))
    return {"creadas": len(creadas), "actividades": creadas}


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
            """SELECT actividad_id, fecha::text AS f, cantidad FROM prog_metrado_dia
               WHERE actividad_id = ANY($1) AND fecha BETWEEN $2 AND $3""",
            ids, base, fin) if ids else []
        real_rows = await con.fetch(
            """SELECT partida_id, fecha::text AS f, cantidad_dia FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3""",
            pids, base, fin) if pids else []
        partidas = {r["id"]: dict(r) for r in await con.fetch(
            """SELECT id, unidad, metrado_presup FROM ev_partidas WHERE id = ANY($1)""",
            pids)} if pids else {}
        acum = {r["partida_id"]: float(r["total"] or 0) for r in await con.fetch(
            """SELECT partida_id, SUM(cantidad_dia) AS total FROM ev_avances_diarios
               WHERE partida_id = ANY($1) GROUP BY partida_id""", pids)} if pids else {}
        dias_semana, feriados = await _calendario(con, proyecto_id, base, fin)

    prog_map: dict = {}
    for r in prog_rows:
        prog_map.setdefault(r["actividad_id"], {})[r["f"]] = float(r["cantidad"])
    real_map: dict = {}
    for r in real_rows:
        if r["cantidad_dia"] is not None:
            real_map.setdefault(r["partida_id"], {})[r["f"]] = float(r["cantidad_dia"])

    grupos: list = []
    idx: dict = {}
    for a in acts:
        pinfo = partidas.get(a["partida_id"]) or {}
        met_base = float(pinfo["metrado_presup"]) if pinfo.get("metrado_presup") is not None else None
        acum_real = acum.get(a["partida_id"]) if a["partida_id"] else None
        act_out = {
            "id": a["id"], "titulo": a["titulo"], "estado": a["estado"],
            "descripcion": a["descripcion"],
            "fecha": str(a["fecha"]), "fecha_fin": str(a["fecha_fin"] or a["fecha"]),
            "otm_id": a["otm_id"], "partida_id": a["partida_id"],
            "partida_codigo": a["partida_codigo"], "partida_desc": a["partida_desc"],
            "responsable": a["responsable"], "supervisor_id": a["supervisor_id"],
            "supervisor_nombre": a["supervisor_nombre"],
            "causa_nc": a["causa_nc"], "causa_nc_cat": a["causa_nc_cat"],
            "rest_pend": a["rest_pend"], "rest_total": a["rest_total"],
            "dias_salto": [str(d) for d in (a["dias_salto"] or [])],
            "und": pinfo.get("unidad") or a["und"],
            "metrado_prog": float(a["metrado_prog"]) if a["metrado_prog"] is not None else None,
            "metrado_base": met_base,
            "acum_real": acum_real,
            "saldo": round(met_base - acum_real, 3) if met_base is not None and acum_real is not None else None,
            "prog": prog_map.get(a["id"], {}),
            "real": real_map.get(a["partida_id"], {}) if a["partida_id"] else {},
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
    """Edición celda a celda del metrado programado (como escribir en el día
    del Excel). cantidad 0 o null borra la celda. El total metrado_prog de la
    actividad pasa a ser la suma de sus celdas."""
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
        celdas.append((f, cant or None))
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            existe = await con.fetchval("SELECT 1 FROM prog_actividades WHERE id = $1", act_id)
            if not existe:
                raise HTTPException(404, "Actividad no encontrada")
            for f, cant in celdas:
                if cant is None:
                    await con.execute(
                        "DELETE FROM prog_metrado_dia WHERE actividad_id = $1 AND fecha = $2",
                        act_id, f)
                else:
                    await con.execute(
                        """INSERT INTO prog_metrado_dia (actividad_id, fecha, cantidad)
                           VALUES ($1,$2,$3)
                           ON CONFLICT (actividad_id, fecha) DO UPDATE SET cantidad = $3""",
                        act_id, f, cant)
            total = float(await con.fetchval(
                "SELECT COALESCE(SUM(cantidad),0) FROM prog_metrado_dia WHERE actividad_id = $1",
                act_id))
            await con.execute(
                """UPDATE prog_actividades SET metrado_prog = NULLIF($2, 0),
                       actualizado_en = now() WHERE id = $1""", act_id, total)
    return {"ok": True, "metrado_prog": total or None}


@router.post("/avance-dia")
async def avance_dia(data: dict):
    """Registra el metrado REAL ejecutado de una partida en un día — escribe
    en ev_avances_diarios, la misma tabla del módulo de Valor Ganado (2 vías,
    un solo dato). cantidad null borra el registro del día."""
    from routers.ev._datos import _fecha_base
    partida_id = data.get("partida_id")
    f = parse_fecha(data.get("fecha"))
    if not partida_id or not f:
        raise HTTPException(400, "partida_id y fecha son obligatorios")
    cantidad = data.get("cantidad")
    pool = await db()
    async with pool.acquire() as con:
        if cantidad in (None, ""):
            await con.execute(
                "DELETE FROM ev_avances_diarios WHERE partida_id = $1 AND fecha = $2",
                int(partida_id), f)
            return {"ok": True, "cantidad": None}
        try:
            cant = float(cantidad)
        except (TypeError, ValueError):
            raise HTTPException(400, "cantidad debe ser un número")
        if cant < 0:
            raise HTTPException(400, "la cantidad no puede ser negativa")
        base = await _fecha_base(con)
        semana = max(1, (f - base).days // 7 + 1) if base else 1
        try:
            await con.execute(
                """INSERT INTO ev_avances_diarios
                     (partida_id, fecha, semana, cantidad_dia, registrado_en)
                   VALUES ($1, $2, $3, $4, NOW())
                   ON CONFLICT (partida_id, fecha)
                   DO UPDATE SET cantidad_dia = $4, registrado_en = NOW()""",
                int(partida_id), f, semana, cant)
        except _ERRORES_DATO:
            raise HTTPException(400, "Partida inexistente o datos inválidos")
    return {"ok": True, "cantidad": cant}


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
    """PPC (Porcentaje de Plan Cumplido) semanal + Pareto de causas de no
    cumplimiento + detalle por supervisor — el nivel de APRENDIZAJE del LPS.
    Comprometidas = actividades no canceladas de la semana; cumplidas = EJECUTADO."""
    semanas = max(1, min(int(semanas or 8), 26))
    hasta = _lunes_de(fecha_lima()) + timedelta(days=6)
    desde = _lunes_de(fecha_lima()) - timedelta(days=(semanas - 1) * 7)
    pool = await db()
    filas = await pool.fetch(
        """SELECT (fecha - ((EXTRACT(ISODOW FROM fecha)::int) - 1)) AS lunes,
                  count(*) FILTER (WHERE estado <> 'CANCELADO') AS comprometidas,
                  count(*) FILTER (WHERE estado = 'EJECUTADO') AS cumplidas,
                  count(*) FILTER (WHERE estado = 'NO_CUMPLIDA') AS no_cumplidas
           FROM prog_actividades
           WHERE proyecto_id = $1 AND fecha BETWEEN $2 AND $3
           GROUP BY 1 ORDER BY 1""", proyecto_id, desde, hasta)
    cnc_rows = await pool.fetch(
        """SELECT COALESCE(causa_nc_cat, 'OTROS') AS causa, count(*) AS n
           FROM prog_actividades
           WHERE proyecto_id = $1 AND estado = 'NO_CUMPLIDA' AND fecha BETWEEN $2 AND $3
           GROUP BY 1 ORDER BY n DESC""", proyecto_id, desde, hasta)
    sup_rows = await pool.fetch(
        """SELECT a.supervisor_id, s.nombre,
                  count(*) FILTER (WHERE a.estado <> 'CANCELADO') AS comprometidas,
                  count(*) FILTER (WHERE a.estado = 'EJECUTADO') AS cumplidas
           FROM prog_actividades a LEFT JOIN supervisores s ON s.id = a.supervisor_id
           WHERE a.proyecto_id = $1 AND a.fecha BETWEEN $2 AND $3
             AND a.supervisor_id IS NOT NULL
           GROUP BY a.supervisor_id, s.nombre ORDER BY s.nombre""",
        proyecto_id, desde, hasta)

    def _ppc(c, e):
        return round(e / c, 4) if c else None

    return {
        "desde": str(desde), "hasta": str(hasta), "cnc_catalogo": CNC,
        "semanal": [{"lunes": str(r["lunes"]), "comprometidas": r["comprometidas"],
                     "cumplidas": r["cumplidas"], "no_cumplidas": r["no_cumplidas"],
                     "ppc": _ppc(r["comprometidas"], r["cumplidas"])} for r in filas],
        "cnc": [{"causa": r["causa"], "etiqueta": CNC.get(r["causa"], r["causa"]),
                 "n": r["n"]} for r in cnc_rows],
        "por_supervisor": [{"supervisor_id": r["supervisor_id"], "nombre": r["nombre"],
                            "comprometidas": r["comprometidas"], "cumplidas": r["cumplidas"],
                            "ppc": _ppc(r["comprometidas"], r["cumplidas"])} for r in sup_rows],
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
