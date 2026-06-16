# ============================================================
# routers/sesiones.py
# Session-First model — endpoints para armar lista y enviar
#
# Integrar en main.py:
#   from routers.sesiones import router as ses_router
#   app.include_router(ses_router)   # después de app = FastAPI(...)
# ============================================================
import os
from datetime import date, datetime
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["sesiones"])

_pool: Optional[asyncpg.Pool] = None


async def db() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"], min_size=1, max_size=5
        )
    return _pool


# ---- Modelos ----
class TrabajadorSesion(BaseModel):
    trab_id: str
    presente: bool = True
    hh_override: Optional[float] = None
    agregado_via: str = "busqueda"


class SesionIn(BaseModel):
    supervisor_id: str
    otm_id: str
    fecha: date
    hh_turno: float = Field(default=10, ge=1, le=24)
    trabajadores: list[TrabajadorSesion] = []


class EnviarIn(BaseModel):
    trabajadores: list[TrabajadorSesion]


# ---- Endpoints ----

@router.get("/sesion/hoy/{supervisor_id}")
async def sesiones_hoy(supervisor_id: str, fecha: Optional[date] = None):
    """Devuelve las sesiones del día del supervisor con su lista de trabajadores."""
    if fecha is None:
        fecha = date.today()
    pool = await db()
    async with pool.acquire() as con:
        sesiones = await con.fetch(
            """SELECT s.*,
               COUNT(st.id)                             AS total,
               COUNT(st.id) FILTER (WHERE st.presente)  AS presentes
               FROM sesiones s
               LEFT JOIN sesion_trabajadores st ON st.sesion_id = s.id
               WHERE s.supervisor_id = $1 AND s.fecha = $2
               GROUP BY s.id
               ORDER BY s.created_at DESC""",
            supervisor_id, fecha,
        )
        result = []
        for s in sesiones:
            trabs = await con.fetch(
                """SELECT st.trab_id, st.presente, st.hh_override, st.agregado_via,
                          t.nombre, t.cargo
                   FROM sesion_trabajadores st
                   JOIN trabajadores t ON t.id = st.trab_id
                   WHERE st.sesion_id = $1
                   ORDER BY t.nombre""",
                s["id"],
            )
            result.append({**dict(s), "trabajadores": [dict(t) for t in trabs]})
    return result


@router.post("/sesion")
async def crear_sesion(body: SesionIn):
    """Crea una sesión en estado borrador."""
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            sid = await con.fetchval(
                """INSERT INTO sesiones (supervisor_id, otm_id, fecha, hh_turno)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                body.supervisor_id, body.otm_id, body.fecha, body.hh_turno,
            )
            for t in body.trabajadores:
                await con.execute(
                    """INSERT INTO sesion_trabajadores
                       (sesion_id, trab_id, presente, hh_override, agregado_via)
                       VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (sesion_id, trab_id) DO UPDATE
                       SET presente=$3, hh_override=$4""",
                    sid, t.trab_id, t.presente, t.hh_override, t.agregado_via,
                )
    return {"id": sid, "ok": True}


@router.post("/sesion/{sesion_id}/enviar")
async def enviar_sesion(sesion_id: int, body: EnviarIn):
    """Confirma la lista, crea registros de tareo y marca la sesión como enviada."""
    pool = await db()
    async with pool.acquire() as con:
        sesion = await con.fetchrow(
            "SELECT * FROM sesiones WHERE id=$1 AND estado='borrador'", sesion_id
        )
        if not sesion:
            raise HTTPException(404, "Sesión no encontrada o ya enviada")

        async with con.transaction():
            # Reemplaza la lista de trabajadores
            await con.execute(
                "DELETE FROM sesion_trabajadores WHERE sesion_id=$1", sesion_id
            )
            for t in body.trabajadores:
                await con.execute(
                    """INSERT INTO sesion_trabajadores
                       (sesion_id, trab_id, presente, hh_override, agregado_via)
                       VALUES ($1,$2,$3,$4,$5)""",
                    sesion_id, t.trab_id, t.presente, t.hh_override, t.agregado_via,
                )

            # Crea registros en la tabla de tareo para los presentes
            hora = datetime.now().strftime("%H:%M:%S")
            enviados = 0
            for t in body.trabajadores:
                if not t.presente:
                    continue
                hh = t.hh_override if t.hh_override is not None else float(sesion["hh_turno"])
                await con.execute(
                    """INSERT INTO registros
                       (trab_id, otm_id, supervisor_id, fecha, hora, hh)
                       VALUES ($1,$2,$3,$4,$5,$6)
                       ON CONFLICT (trab_id, otm_id, fecha)
                       DO UPDATE SET hh=$6, hora=$5""",
                    t.trab_id, sesion["otm_id"], sesion["supervisor_id"],
                    sesion["fecha"], hora, hh,
                )
                enviados += 1

            await con.execute(
                "UPDATE sesiones SET estado='enviada', enviada_at=now() WHERE id=$1",
                sesion_id,
            )

    return {"ok": True, "enviados": enviados}


@router.delete("/sesion/{sesion_id}")
async def eliminar_sesion(sesion_id: int):
    """Elimina un borrador (no elimina sesiones ya enviadas)."""
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            "DELETE FROM sesiones WHERE id=$1 AND estado='borrador'", sesion_id
        )
    return {"ok": True}
