# ============================================================
# routers/ev/tarifas.py — tarifas por cargo + rentabilidad (resultado operativo simple) (F0.5b)
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


# ---------------------- Rentabilidad (Fase 3): tarifas + resultado operativo ----------------------
_HH_POR_CARGO_SQL = """
    SELECT {sel} COALESCE(t.cargo, '(Sin cargo)') AS cargo, SUM(tp.hh) AS hh
    FROM tareo_partida tp
    LEFT JOIN trabajadores t
           ON t.id = tp.trabajador_id
    WHERE tp.hh IS NOT NULL
    GROUP BY {grpby} COALESCE(t.cargo, '(Sin cargo)')
"""
# Nota: en GROUP BY no se puede usar el alias (`AS otm`) — Postgres lanza
# "syntax error at or near AS". Por eso la expr del SELECT y la del GROUP BY
# se inyectan por separado.


@router.get("/tarifas")
async def listar_tarifas():
    """Cargos con HH reales (de tareo_partida) + su tarifa S/./HH. Incluye '(Default)'."""
    pool = await db()
    async with pool.acquire() as con:
        hh_rows = await con.fetch(_HH_POR_CARGO_SQL.format(sel="", grpby=""))
        tar_rows = await con.fetch("SELECT cargo, costo_hh FROM ev_tarifas_cargo")
    tar = {r["cargo"]: float(r["costo_hh"]) for r in tar_rows}
    default = tar.get("(Default)")          # None ⇒ respaldo sin configurar
    cargos = []
    vistos = set()
    for r in hh_rows:
        c = r["cargo"]; vistos.add(c)
        # costo_hh None ⇒ "sin configurar" (≠ 0 explícito). hh entera para que
        # la suma de la tarjeta cuadre con el total de la tabla (mismo redondeo).
        cargos.append({"cargo": c, "costo_hh": tar.get(c), "hh": round(float(r["hh"] or 0))})
    # cargos con tarifa pero sin HH aún (excepto el respaldo)
    for c, v in tar.items():
        if c != "(Default)" and c not in vistos:
            cargos.append({"cargo": c, "costo_hh": v, "hh": 0})
    cargos.sort(key=lambda x: x["cargo"])
    return {"cargos": cargos, "default": default}


@router.post("/tarifas")
async def guardar_tarifa(body: TarifaIn):
    """Upsert de la tarifa de un cargo (o de '(Default)')."""
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO ev_tarifas_cargo (cargo, costo_hh) VALUES ($1, $2)
               ON CONFLICT (cargo) DO UPDATE SET costo_hh = EXCLUDED.costo_hh""",
            body.cargo.strip(), body.costo_hh,
        )
    return {"ok": True}


def _resultado_operativo(por_otm: dict, tar: dict, default, otm_info: dict):
    """Arma filas y totales de rentabilidad. Función pura → testeable sin BD.

    • Incluye TODA OTM con HH de tareo O con ingreso valorizado (>0), para que el
      ingreso de una OTM aún sin tareo no desaparezca del total (bug 🔴).
    • HH redondeadas a entero UNA sola vez por cargo: la tabla cuadra con la
      tarjeta de tarifas y el costo es auditable (HH×tarifa = costo) (🟡).
    Devuelve (filas, total)."""
    otm_keys = set(por_otm.keys()) | {
        oid for oid, info in otm_info.items()
        if float(info["monto_valorizado"] or 0) > 0
    }
    out = []
    tot_ing = tot_costo = tot_hh = tot_hh_sin = 0.0
    for otm in otm_keys:
        hhc = {cargo: round(hh) for cargo, hh in por_otm.get(otm, {}).items()}
        costo, hh_sin = _calc_costo_mo(hhc, tar, default)
        hh_total = sum(hhc.values())
        info = otm_info.get(otm)
        ingreso = float(info["monto_valorizado"] or 0) if info else 0.0
        contractual = float(info["monto_contractual"] or 0) if info else 0.0
        margen = round(ingreso - costo, 2)
        out.append({
            "otm": otm,
            "descripcion": info["descripcion"] if info else None,
            "ingreso_valorizado": round(ingreso, 2),
            "ingreso_contractual": round(contractual, 2),
            "hh_total": hh_total,
            "hh_sin_tarifa": hh_sin,
            "costo_mo": costo,
            "margen": margen,
            "pct_margen": round(margen / ingreso, 4) if ingreso > 0 else 0,
        })
        tot_ing += ingreso; tot_costo += costo; tot_hh += hh_total; tot_hh_sin += hh_sin
    out.sort(key=lambda x: x["otm"] or "")
    tot_margen = round(tot_ing - tot_costo, 2)
    total = {
        "ingreso_valorizado": round(tot_ing, 2),
        "costo_mo": round(tot_costo, 2),
        "hh_total": round(tot_hh),
        "hh_sin_tarifa": round(tot_hh_sin),
        "margen": tot_margen,
        "pct_margen": round(tot_margen / tot_ing, 4) if tot_ing > 0 else 0,
    }
    return out, total


@router.get("/rentabilidad")
async def rentabilidad():
    """Resultado Operativo por OTM: Ingreso valorizado − Costo MO (HH reales × tarifa).
    Costo basado en tareo_partida (HH reales por trabajador → cargo → tarifa)."""
    pool = await db()
    async with pool.acquire() as con:
        hh_rows = await con.fetch(
            _HH_POR_CARGO_SQL.format(sel="tp.otm_id AS otm,", grpby="tp.otm_id,")
        )
        tar_rows = await con.fetch("SELECT cargo, costo_hh FROM ev_tarifas_cargo")
        otm_rows = await con.fetch(
            "SELECT id, descripcion, monto_contractual, monto_valorizado FROM otms"
        )
    tar = {r["cargo"]: float(r["costo_hh"]) for r in tar_rows}
    default = tar.get("(Default)")          # None ⇒ respaldo sin configurar
    otm_info = {r["id"]: r for r in otm_rows}

    por_otm: dict = defaultdict(lambda: defaultdict(float))
    for r in hh_rows:
        por_otm[r["otm"]][r["cargo"]] += float(r["hh"] or 0)

    out, total = _resultado_operativo(por_otm, tar, default, otm_info)
    return {
        "otms": out,
        "total": total,
        "tarifa_default": default if default is not None else 0.0,
    }


