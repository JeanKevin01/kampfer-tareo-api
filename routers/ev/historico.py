# ============================================================
# routers/ev/historico.py — cuadrillas plantilla + carga de historico (F0.5b)
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
# FASE 1 — Control Maestro: Cuadrillas + Asignación + Histórico
# ═══════════════════════════════════════════════════════════════

# ── Cuadrillas típicas por OTM ────────────────────────────────

@router_campo.get("/cuadrillas-plantilla")
async def listar_cuadrillas_plantilla(
    supervisor_id: str,
    otm_id: str,
):
    """Devuelve las cuadrillas típicas del supervisor para esa OTM."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT c.trabajador_id, t.nombre, t.cargo,
                      COALESCE(t.tipo,'DIRECTO') AS tipo,
                      c.nombre AS plantilla, c.orden
               FROM cuadrilla_otm c
               JOIN trabajadores t ON t.id = c.trabajador_id
               WHERE c.supervisor_id = $1 AND c.otm_id = $2 AND c.activo = TRUE
               ORDER BY c.nombre, c.orden""",
            supervisor_id, otm_id
        )
        plantillas: dict = {}
        for r in rows:
            n = r["plantilla"]
            if n not in plantillas:
                plantillas[n] = []
            plantillas[n].append({
                "trabajador_id": r["trabajador_id"],
                "nombre":        r["nombre"],
                "cargo":         r["cargo"],
                "tipo":          r["tipo"],
                "orden":         r["orden"],
            })
        return plantillas


@router_campo.post("/cuadrillas-plantilla")
async def guardar_cuadrilla_plantilla(data: dict):
    """Crea o reemplaza una cuadrilla típica para supervisor+OTM."""
    supervisor_id = data.get("supervisor_id")
    otm_id        = data.get("otm_id")
    nombre        = data.get("nombre", "Principal")
    trabajadores  = data.get("trabajadores", [])

    if not supervisor_id or not otm_id:
        raise HTTPException(400, "supervisor_id y otm_id son requeridos")

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "DELETE FROM cuadrilla_otm WHERE supervisor_id=$1 AND otm_id=$2 AND nombre=$3",
                supervisor_id, otm_id, nombre
            )
            for idx, tid in enumerate(trabajadores):
                await con.execute(
                    """INSERT INTO cuadrilla_otm
                         (supervisor_id, otm_id, nombre, trabajador_id, orden)
                       VALUES ($1,$2,$3,$4,$5)""",
                    supervisor_id, otm_id, nombre, str(tid), idx
                )
    return {"ok": True, "nombre": nombre, "total": len(trabajadores)}


@router.post("/historico/cargar")
async def cargar_historico(data: dict):
    """
    Carga acumulados históricos de HH y cantidades para una OTM/semana.
    Popula ev_hh_gastadas (fuente='historico') y ev_avances (hito principal).
    """
    otm_id = data.get("otm_id")
    semana = data.get("semana")
    filas  = data.get("filas", [])   # [{partida_id, hh_gastadas_acum, cantidad_ejecutada_acum}]

    if not otm_id or not semana:
        raise HTTPException(400, "otm_id y semana son requeridos")

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for fila in filas:
                pid  = fila["partida_id"]
                hh   = float(fila.get("hh_gastadas_acum", 0))
                cant = float(fila.get("cantidad_ejecutada_acum", 0))

                # ev_historico_carga (trazabilidad)
                await con.execute(
                    """INSERT INTO ev_historico_carga
                         (otm_id, partida_id, semana,
                          hh_gastadas_acum, cantidad_ejecutada_acum)
                       VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (otm_id, partida_id, semana)
                       DO UPDATE SET
                         hh_gastadas_acum        = EXCLUDED.hh_gastadas_acum,
                         cantidad_ejecutada_acum = EXCLUDED.cantidad_ejecutada_acum,
                         fecha_carga             = NOW()""",
                    otm_id, pid, semana, hh, cant
                )

                # ev_hh_gastadas (fuente='historico' — puede ser sobreescrito por manual)
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1,$2,$3,'historico')
                       ON CONFLICT (partida_id, semana)
                       DO UPDATE SET hh=$3, fuente='historico'
                       WHERE ev_hh_gastadas.fuente NOT IN ('manual')""",
                    pid, semana, hh
                )

                # ev_avances con el hito principal (para cálculo de % avance)
                hito = await con.fetchrow(
                    """SELECT id FROM ev_hitos
                       WHERE partida_id=$1
                       ORDER BY peso DESC NULLS LAST, id
                       LIMIT 1""",
                    pid
                )
                if hito and cant > 0:
                    await con.execute(
                        """INSERT INTO ev_avances (hito_id, semana, cantidad_acum)
                           VALUES ($1,$2,$3)
                           ON CONFLICT (hito_id, semana)
                           DO UPDATE SET cantidad_acum=$3""",
                        hito["id"], semana, cant
                    )

    return {"ok": True, "otm_id": otm_id, "semana": semana, "partidas": len(filas)}


@router.get("/historico/lista")
async def listar_historico(otm_id: str, semana: int):
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT h.*, p.codigo, p.descripcion, p.fase, p.unidad
               FROM ev_historico_carga h
               JOIN ev_partidas p ON p.id = h.partida_id
               WHERE h.otm_id=$1 AND h.semana=$2
               ORDER BY p.codigo""",
            otm_id, semana
        )
        return [dict(r) for r in rows]


