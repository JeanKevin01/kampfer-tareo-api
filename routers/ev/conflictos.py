# ============================================================
# routers/ev/conflictos.py — conflictos de HH duplicadas (F0.5b)
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


# ── Conflictos HH duplicadas ──────────────────────────────────

@router.get("/conflictos")
async def listar_conflictos(
    estado: Optional[str] = None,
    fecha:  Optional[str] = None,
):
    pool = await db()
    async with pool.acquire() as con:
        conds, args = ["1=1"], []
        if estado:
            args.append(estado);  conds.append(f"c.estado = ${len(args)}")
        if fecha:
            args.append(_as_date(fecha));   conds.append(f"c.fecha  = ${len(args)}::date")
        where = " AND ".join(conds)
        rows = await con.fetch(
            f"""SELECT c.*,
                       t.nombre   AS trabajador_nombre,
                       s1.nombre  AS sup1_nombre,
                       s2.nombre  AS sup2_nombre
                FROM hh_conflictos c
                JOIN trabajadores t   ON t.id  = c.trabajador_id
                LEFT JOIN supervisores s1 ON s1.id = c.supervisor_id_1
                LEFT JOIN supervisores s2 ON s2.id = c.supervisor_id_2
                WHERE {where}
                ORDER BY c.fecha DESC, c.created_at DESC""",
            *args
        )
        return [dict(r) for r in rows]


@router.post("/conflictos/resolver")
async def resolver_conflicto(data: dict):
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE hh_conflictos
               SET estado='RESUELTO', resolucion=$1, notas=$2
               WHERE id=$3""",
            data.get("resolucion"), data.get("notas"), data.get("conflicto_id")
        )
    return {"ok": True}
