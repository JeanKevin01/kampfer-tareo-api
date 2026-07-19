# ============================================================
# routers/ev/avance_diario.py — grilla semanal + avance diario de campo (F0.5b)
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


# ═══════════════════════════════════════════════════════════════
# SPRINT 2: Control diario por partida
# ═══════════════════════════════════════════════════════════════

@router_campo.post("/avance-diario")
async def guardar_avance_diario(data: dict):
    """Guarda o actualiza la cantidad ejecutada de una partida en un día.

    F1 LookAhead v2: usa el helper ÚNICO registrar_avance_partida — el mismo
    del módulo de programación — así el avance ingresado desde Valor Ganado
    también re-prorratea la actividad del LookAhead vinculada (un solo dato,
    mismas consecuencias por las dos vías). cantidad_dia None borra el día."""
    from routers.programacion import registrar_avance_partida
    partida_id   = data.get("partida_id")
    fecha_str    = data.get("fecha")
    cantidad_dia = data.get("cantidad_dia")  # None = borrar el registro del día
    if cantidad_dia in (None, ""):
        cantidad_dia = None
    notas        = data.get("notas")
    if not partida_id or not fecha_str:
        raise HTTPException(400, "partida_id y fecha son requeridos")

    hito_id = int(data["hito_id"]) if data.get("hito_id") else None
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await registrar_avance_partida(
                con, int(partida_id), _as_date(fecha_str), cantidad_dia,
                notas=notas, actualizar_notas=True, hito_id=hito_id)
    return {"ok": True}


