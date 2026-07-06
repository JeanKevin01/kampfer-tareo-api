# ============================================================
# routers/ev/improductivas.py — HH improductivas (CRUD) (F0.5b)
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


# ---------------------- #5: HH improductivas (CRUD) ----------------------
@router.get("/improductivas")
async def listar_improductivas(otm: Optional[str] = None, semana: Optional[int] = None):
    """Lista las HH improductivas registradas (opcionalmente por OTM y/o semana)."""
    pool = await db()
    async with pool.acquire() as con:
        cond, args = [], []
        if otm:
            args.append(otm); cond.append(f"otm_id = ${len(args)}")
        if semana is not None:
            args.append(semana); cond.append(f"semana = ${len(args)}")
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        rows = await con.fetch(
            f"""SELECT id, otm_id, semana, hh, motivo, nota, partida_id, registrado_en
                FROM ev_hh_improductivas{where} ORDER BY semana, id""",
            *args,
        )
    return [
        {"id": r["id"], "otm_id": r["otm_id"], "semana": r["semana"],
         "hh": float(r["hh"]), "motivo": r["motivo"], "nota": r["nota"],
         "partida_id": r["partida_id"],
         "registrado_en": r["registrado_en"].isoformat() if r["registrado_en"] else None}
        for r in rows
    ]


@router.post("/improductivas")
async def crear_improductiva(body: ImproductivaIn):
    """Registra HH improductivas para una OTM/semana. Son HH consumidas NO asignadas
    a partidas (bucket aparte): suman al total y bajan el PF del proyecto.
    partida_id es opcional (solo para atribución/trazabilidad; no cambia el PF por partida)."""
    pool = await db()
    async with pool.acquire() as con:
        new_id = await con.fetchval(
            """INSERT INTO ev_hh_improductivas (otm_id, semana, hh, motivo, nota, partida_id)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
            body.otm_id, body.semana, body.hh, body.motivo, body.nota, body.partida_id,
        )
    return {"id": new_id, "ok": True}


@router.delete("/improductivas/{improd_id}")
async def eliminar_improductiva(improd_id: int):
    pool = await db()
    async with pool.acquire() as con:
        res = await con.execute("DELETE FROM ev_hh_improductivas WHERE id=$1", improd_id)
    if res == "DELETE 0":
        raise HTTPException(404, "Registro no encontrado")
    return {"ok": True}


