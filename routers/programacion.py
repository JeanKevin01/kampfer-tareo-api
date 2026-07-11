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
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from core.auth import exigir_identidad_supervisor, require_role
from core.db import db
from core.log import get_logger
from core.media import (MAX_FOTO_BYTES, MAX_FOTOS_POR_REPORTE, guardar_foto,
                        media_dir, url_firmada)
from core.tiempo import fecha_lima, parse_fecha

log = get_logger("programacion")

router = APIRouter(prefix="/ev/programacion", tags=["programacion"])
router_campo = APIRouter(prefix="/campo", tags=["programacion"])

_ESTADOS = ("PROGRAMADO", "EJECUTADO", "CANCELADO")


def _lunes_de(fecha: date) -> date:
    return fecha - timedelta(days=fecha.weekday())


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
            """SELECT a.*, o.descripcion AS otm_desc
               FROM prog_actividades a LEFT JOIN otms o ON o.id = a.otm_id
               WHERE a.proyecto_id = $1 AND a.fecha BETWEEN $2 AND $3
               ORDER BY a.fecha, a.id""", proyecto_id, fechas[0], fechas[6])]
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
    row = await pool.fetchrow(
        """INSERT INTO prog_actividades
           (proyecto_id, fecha, otm_id, partida_id, titulo, descripcion, responsable, creado_por)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
        int(data.get("proyecto_id") or 1), fecha,
        (str(data["otm_id"]).strip() or None) if data.get("otm_id") else None,
        int(data["partida_id"]) if data.get("partida_id") else None,
        titulo, data.get("descripcion") or None, data.get("responsable") or None,
        user.get("sub"))
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
    for k in ("titulo", "descripcion", "responsable", "otm_id"):
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
    row = await pool.fetchrow(
        f"UPDATE prog_actividades SET {sets}, actualizado_en = now() "
        f"WHERE id = $1 RETURNING *", act_id, *valores)
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
    log.info("purga de media", extra={"semana": semana_iso, "fotos": len(fotos),
                                      "bytes": liberados, "por": user.get("sub")})
    return {"fotos_purgadas": len(fotos), "bytes_liberados": liberados}


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
            rid = await con.fetchval(
                """INSERT INTO campo_reportes
                   (proyecto_id, fecha, otm_id, actividad_id, supervisor_id, descripcion)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                proyecto_id, f_rep, otm_id.strip(), actividad_id,
                supervisor_id.strip(), descripcion.strip() or None)
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
