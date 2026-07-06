# ============================================================
# routers/ev/rendimiento.py — rendimiento por trabajador y por cuadrilla (F0.5b)
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


@router.get("/rendimiento-trabajador")
async def rendimiento_trabajador(
    trabajador_id: str,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
):
    """HH y PF de un trabajador desglosado por partida."""
    pool = await db()
    async with pool.acquire() as con:
        conds = ["tp.trabajador_id = $1"]
        args: list = [trabajador_id]
        if desde:
            args.append(_as_date(desde))
            conds.append(f"tp.fecha >= ${len(args)}::date")
        if hasta:
            args.append(_as_date(hasta))
            conds.append(f"tp.fecha <= ${len(args)}::date")
        where = " AND ".join(conds)

        # Info del trabajador
        trab = await con.fetchrow(
            "SELECT nombre, cargo FROM trabajadores WHERE id = $1", trabajador_id
        )

        rows = await con.fetch(
            f"""SELECT p.id AS partida_id, p.codigo, p.descripcion,
                       p.fase, p.unidad,
                       CASE WHEN p.metrado_presup > 0
                            THEN p.hh_presup / p.metrado_presup ELSE 0
                       END AS factor_presup,
                       SUM(tp.hh)              AS hh_total,
                       COUNT(DISTINCT tp.fecha) AS dias_trabajados
                FROM tareo_partida tp
                JOIN ev_partidas p ON p.id = tp.partida_id
                WHERE {where} AND tp.hh IS NOT NULL
                GROUP BY p.id, p.codigo, p.descripcion, p.fase, p.unidad,
                         p.hh_presup, p.metrado_presup
                ORDER BY hh_total DESC""",
            *args
        )

        # Cant ejecutada acumulada por partida en el mismo rango de fechas.
        # OJO: se deduplica (partida, fecha) ANTES de unir con ev_avances_diarios;
        # de lo contrario la cantidad del día se multiplicaría por cada trabajador.
        cant_rows = await con.fetch(
            f"""SELECT d.partida_id,
                       SUM(COALESCE(ad.cantidad_dia, 0)) AS cant_acum
                FROM (
                    SELECT DISTINCT tp.partida_id, tp.fecha
                    FROM tareo_partida tp
                    WHERE {where}
                ) d
                LEFT JOIN ev_avances_diarios ad
                  ON ad.partida_id = d.partida_id AND ad.fecha = d.fecha
                GROUP BY d.partida_id""",
            *args
        )
        cant_by_pid = {r["partida_id"]: float(r["cant_acum"] or 0)
                       for r in cant_rows}

        partidas = []
        for r in rows:
            hh      = float(r["hh_total"] or 0)
            factor  = float(r["factor_presup"] or 0)
            cant    = cant_by_pid.get(r["partida_id"], 0)
            hh_gan  = round(cant * factor, 2) if factor > 0 else None
            pf      = round(hh_gan / hh, 3) if hh_gan and hh > 0 else None
            partidas.append({
                "partida_id":     r["partida_id"],
                "codigo":         r["codigo"],
                "descripcion":    r["descripcion"],
                "fase":           r["fase"],
                "unidad":         r["unidad"],
                "factor_presup":  round(factor, 4),
                "hh_total":       round(hh, 2),
                "cant_acum":      round(cant, 2),
                "hh_ganadas":     hh_gan,
                "pf_promedio":    pf,
                "dias_trabajados": int(r["dias_trabajados"]),
            })

        return {
            "trabajador_id": trabajador_id,
            "nombre":  trab["nombre"] if trab else "—",
            "cargo":   trab["cargo"]  if trab else "—",
            "partidas": partidas,
            "hh_total_global": round(sum(p["hh_total"] for p in partidas), 2),
        }


@router.get("/rendimiento-cuadrillas")
async def rendimiento_cuadrillas(
    semana:        Optional[int] = None,
    supervisor_id: Optional[str] = None,
):
    """Comparativa de PF por cuadrilla (supervisor) y partida."""
    pool = await db()
    async with pool.acquire() as con:
        conds = ["tp.hh IS NOT NULL"]
        args: list = []
        if semana:
            args.append(semana)
            conds.append(f"tp.semana = ${len(args)}")
        if supervisor_id:
            args.append(supervisor_id)
            conds.append(f"tp.supervisor_id = ${len(args)}")
        where = " AND ".join(conds)

        rows = await con.fetch(
            f"""SELECT tp.supervisor_id,
                       s.nombre AS supervisor_nombre,
                       p.id AS partida_id, p.codigo, p.descripcion,
                       p.fase, p.unidad,
                       CASE WHEN p.metrado_presup > 0
                            THEN p.hh_presup / p.metrado_presup ELSE 0
                       END AS factor_presup,
                       SUM(tp.hh) AS hh_total,
                       COUNT(DISTINCT tp.trabajador_id) AS n_trabajadores,
                       COUNT(DISTINCT tp.fecha) AS dias
                FROM tareo_partida tp
                JOIN ev_partidas p  ON p.id  = tp.partida_id
                JOIN supervisores s ON s.id  = tp.supervisor_id
                WHERE {where}
                GROUP BY tp.supervisor_id, s.nombre, p.id, p.codigo,
                         p.descripcion, p.fase, p.unidad,
                         p.hh_presup, p.metrado_presup
                ORDER BY tp.supervisor_id, p.codigo""",
            *args
        )

        # Cant ejecutada por (supervisor, partida) en el rango.
        # Se deduplica (supervisor, partida, fecha) ANTES de unir con
        # ev_avances_diarios para no multiplicar la cantidad del día por el
        # número de trabajadores de la cuadrilla.
        cant_args = list(args)
        cant_rows = await con.fetch(
            f"""SELECT d.supervisor_id, d.partida_id,
                       SUM(COALESCE(ad.cantidad_dia, 0)) AS cant_acum
                FROM (
                    SELECT DISTINCT tp.supervisor_id, tp.partida_id, tp.fecha
                    FROM tareo_partida tp
                    WHERE {where}
                ) d
                LEFT JOIN ev_avances_diarios ad
                  ON ad.partida_id = d.partida_id AND ad.fecha = d.fecha
                GROUP BY d.supervisor_id, d.partida_id""",
            *cant_args
        )
        cant_map = {(r["supervisor_id"], r["partida_id"]): float(r["cant_acum"] or 0)
                    for r in cant_rows}

        # Agrupar por supervisor
        por_sup: dict = {}
        for r in rows:
            sid = r["supervisor_id"]
            if sid not in por_sup:
                por_sup[sid] = {"supervisor_id": sid,
                                "nombre": r["supervisor_nombre"],
                                "partidas": []}
            hh     = float(r["hh_total"] or 0)
            factor = float(r["factor_presup"] or 0)
            cant   = cant_map.get((sid, r["partida_id"]), 0)
            hh_gan = round(cant * factor, 2) if factor > 0 else None
            pf     = round(hh_gan / hh, 3) if hh_gan and hh > 0 else None
            por_sup[sid]["partidas"].append({
                "partida_id":    r["partida_id"],
                "codigo":        r["codigo"],
                "descripcion":   r["descripcion"],
                "fase":          r["fase"],
                "unidad":        r["unidad"],
                "hh_total":      round(hh, 2),
                "cant_acum":     round(cant, 2),
                "hh_ganadas":    hh_gan,
                "pf":            pf,
                "n_trabajadores": int(r["n_trabajadores"]),
                "dias":          int(r["dias"]),
            })

        return list(por_sup.values())


