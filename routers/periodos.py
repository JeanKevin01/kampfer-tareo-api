# ============================================================
# routers/periodos.py — F2.1: periodos contables del RO mensual
#
# GET/POST /ev/periodos · POST /ev/periodos/{id}/cerrar ·
# POST /ev/periodos/{id}/reabrir (SOLO admin).
# `periodo_de(con, proyecto_id, fecha)` crea el mes si falta — lo usan los
# documentos de costo (F2.2), ajustes de venta (F2.4) y valorizaciones (F2.8).
# `exigir_abierto(con, periodo_id)` → 409 si el periodo está CERRADO.
# ============================================================
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db

router = APIRouter(prefix="/ev/periodos", tags=["periodos"])


async def periodo_de(con, proyecto_id: int, fecha: date) -> int:
    """id del periodo (proyecto, año, mes) — lo crea ABIERTO si no existe."""
    pid = await con.fetchval(
        "SELECT id FROM periodos WHERE proyecto_id=$1 AND anio=$2 AND mes=$3",
        proyecto_id, fecha.year, fecha.month)
    if pid:
        return pid
    return await con.fetchval(
        """INSERT INTO periodos (proyecto_id, anio, mes) VALUES ($1,$2,$3)
           ON CONFLICT (proyecto_id, anio, mes) DO UPDATE SET proyecto_id = EXCLUDED.proyecto_id
           RETURNING id""",
        proyecto_id, fecha.year, fecha.month)


async def exigir_abierto(con, periodo_id: int) -> None:
    estado = await con.fetchval("SELECT estado FROM periodos WHERE id=$1", periodo_id)
    if estado is None:
        raise HTTPException(404, "Periodo no encontrado")
    if estado != "ABIERTO":
        raise HTTPException(409, "El periodo está CERRADO; reábrelo (admin) para modificarlo")


@router.get("")
async def listar_periodos(proyecto_id: int = 1):
    pool = await db()
    rows = await pool.fetch(
        "SELECT p.* FROM periodos p WHERE p.proyecto_id = $1 ORDER BY p.anio, p.mes",
        proyecto_id)
    return [dict(r) for r in rows]


@router.post("")
async def crear_periodo(data: dict):
    """Crea (o devuelve) el periodo {proyecto_id, anio, mes, tipo_cambio?}."""
    proyecto_id = int(data.get("proyecto_id") or 1)
    anio, mes = int(data.get("anio") or 0), int(data.get("mes") or 0)
    if not (2000 <= anio <= 2100) or not (1 <= mes <= 12):
        raise HTTPException(400, "anio/mes inválidos")
    pool = await db()
    async with pool.acquire() as con:
        pid = await periodo_de(con, proyecto_id, date(anio, mes, 1))
        tc = data.get("tipo_cambio")
        if tc is not None:
            await exigir_abierto(con, pid)
            await con.execute("UPDATE periodos SET tipo_cambio=$1 WHERE id=$2", float(tc), pid)
        row = await con.fetchrow("SELECT * FROM periodos WHERE id=$1", pid)
    return dict(row)


@router.post("/{periodo_id}/cerrar")
async def cerrar_periodo(periodo_id: int, user: dict = Depends(require_role("oficina"))):
    """Cierra el mes. Además dispara el snapshot PREV de la proyección (F2.6)."""
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow("SELECT * FROM periodos WHERE id=$1", periodo_id)
            if not row:
                raise HTTPException(404, "Periodo no encontrado")
            if row["estado"] == "CERRADO":
                raise HTTPException(409, "El periodo ya está cerrado")
            await con.execute(
                "UPDATE periodos SET estado='CERRADO', cerrado_en=now(), cerrado_por=$1 WHERE id=$2",
                user.get("sub"), periodo_id)
            # F2.6: snapshot de la proyección del mes siguiente → ro_prev (el PREV del motor)
            try:
                from routers.ro_proyeccion import snapshot_prev
                await snapshot_prev(con, row["proyecto_id"], periodo_id)
            except ImportError:
                pass
    return {"ok": True, "estado": "CERRADO"}


@router.post("/{periodo_id}/reabrir")
async def reabrir_periodo(periodo_id: int, _u: dict = Depends(require_role("admin"))):
    """Reabrir un mes cerrado — SOLO admin (auditoría: el cierre queda registrado)."""
    pool = await db()
    n = await pool.execute(
        "UPDATE periodos SET estado='ABIERTO' WHERE id=$1 AND estado='CERRADO'", periodo_id)
    if n == "UPDATE 0":
        raise HTTPException(409, "El periodo no existe o no está cerrado")
    return {"ok": True, "estado": "ABIERTO"}


# ── Helper de fecha→periodo para otros routers (con validación de apertura) ──
async def periodo_para_movimiento(con, proyecto_id: int, fecha: Optional[date],
                                  periodo_id: Optional[int] = None) -> int:
    """Resuelve el periodo de un movimiento: por id explícito o desde la fecha
    (creándolo si falta) y EXIGE que esté abierto."""
    if periodo_id:
        await exigir_abierto(con, periodo_id)
        return periodo_id
    pid = await periodo_de(con, proyecto_id, fecha or date.today())
    await exigir_abierto(con, pid)
    return pid
