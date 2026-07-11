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


@router.post("/actividades")
async def crear_actividad(data: dict, user: dict = Depends(require_role("oficina"))):
    fecha = parse_fecha(data.get("fecha"))
    titulo = str(data.get("titulo") or "").strip()
    if not fecha or not titulo:
        raise HTTPException(400, "fecha y titulo son obligatorios")
    pool = await db()
    try:
        row = await pool.fetchrow(
            """INSERT INTO prog_actividades
               (proyecto_id, fecha, otm_id, partida_id, titulo, descripcion,
                responsable, supervisor_id, creado_por)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
            int(data.get("proyecto_id") or 1), fecha,
            (str(data["otm_id"]).strip() or None) if data.get("otm_id") else None,
            int(data["partida_id"]) if data.get("partida_id") else None,
            titulo, data.get("descripcion") or None, data.get("responsable") or None,
            (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None,
            user.get("sub"))
    except _ERRORES_DATO:
        raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
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
    try:
        row = await pool.fetchrow(
            f"UPDATE prog_actividades SET {sets}, actualizado_en = now() "
            f"WHERE id = $1 RETURNING *", act_id, *valores)
    except _ERRORES_DATO:
        raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
    if not row:
        raise HTTPException(404, "Actividad no encontrada")
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
           WHERE fecha = $1 AND estado <> 'CANCELADO'
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
                  ev.codigo AS partida_codigo, ev.descripcion AS partida_desc
           FROM prog_actividades a
           LEFT JOIN otms o ON o.id = a.otm_id
           LEFT JOIN ev_partidas ev ON ev.id = a.partida_id
           WHERE a.fecha = $1 AND a.supervisor_id = $2 AND a.estado <> 'CANCELADO'
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
