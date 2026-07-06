# ============================================================
# routers/ev/valorizado.py — cantidad valorizada por el cliente (F0.5b)
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


# ---------------------- #2: Valorizado (cantidad reconocida por el cliente) ----------------------
@router.get("/valorizado")
async def listar_valorizado(semana: int, otm: Optional[str] = None):
    """Por partida hoja: cantidad ejecutada (del motor) vs cantidad valorizada
    (lo que el cliente reconoce) y su variación. Base de la futura Valorización."""
    partidas, hitos, avances, hh, tareo, split = await _datos_base(semana, otm)
    filas = _calcular(partidas, hitos, avances, hh, tareo, semana, split)
    hojas = [f for f in filas if f["fase"] is not None]

    pool = await db()
    async with pool.acquire() as con:
        # último valor acumulado por partida hasta la semana de corte
        rows = await con.fetch(
            "SELECT partida_id, semana, cantidad_valorizada FROM ev_valorizado "
            "WHERE semana <= $1 ORDER BY partida_id, semana", semana,
        )
    valz: dict = {}
    for r in rows:
        valz[r["partida_id"]] = float(r["cantidad_valorizada"])  # el más reciente gana

    out = []
    for f in hojas:
        ejec = float(f["cantidad_instalada"])
        v = valz.get(f["partida_id"], 0.0)
        out.append({
            "partida_id": f["partida_id"], "codigo": f["codigo"], "fase": f["fase"],
            "descripcion": f["descripcion"], "unidad": f["unidad"],
            "cantidad_ejecutada": round(ejec, 2),
            "cantidad_valorizada": round(v, 2),
            "variacion": round(ejec - v, 2),
        })
    return {"semana": semana, "otm": otm, "partidas": out}


@router.post("/valorizado")
async def guardar_valorizado(body: ValorizadoIn):
    """Upsert de la cantidad valorizada de una partida en una semana."""
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO ev_valorizado (partida_id, semana, cantidad_valorizada)
               VALUES ($1,$2,$3)
               ON CONFLICT (partida_id, semana)
               DO UPDATE SET cantidad_valorizada = EXCLUDED.cantidad_valorizada,
                             registrado_en = now()""",
            body.partida_id, body.semana, body.cantidad_valorizada,
        )
    return {"ok": True}


