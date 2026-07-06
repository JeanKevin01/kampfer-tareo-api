# ============================================================
# routers/ev/isp.py — arbol WBS + ISP + reporte + curvas S (F0.5b)
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


@router_campo.get("/arbol")
async def arbol_wbs(otm: Optional[str] = None, semana: int = 1):
    """Árbol WBS completo (padre + hoja) con valores EV calculados.
    Nodos padre tienen hh_ganadas/gastadas = 0 — el rollup lo hace el frontend."""
    try:
        pool = await db()
        async with pool.acquire() as con:
            if otm:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
                )
            else:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo ORDER BY codigo"
                )
            hitos   = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
            avances = await con.fetch(
                "SELECT hito_id, semana, cantidad_acum FROM ev_avances WHERE semana <= $1", semana
            )
            tareo   = await _hh_gastadas_unificada(con)
            split   = await _hh_real_split(con)

        filas_ev = _calcular(list(partidas), list(hitos), list(avances), [], tareo, semana, split)
        ev_por_id = {f["partida_id"]: f for f in filas_ev}

        result = []
        for p in partidas:
            ev = ev_por_id.get(p["id"], {})
            result.append({
                "id":              p["id"],
                "codigo":          p["codigo"],
                "otm_id":          p["otm_id"],
                "fase":            p["fase"],
                "sub_fase":        p["sub_fase"],
                "descripcion":     p["descripcion"],
                "unidad":          p["unidad"],
                "hh_presup":       float(p["hh_presup"] or 0),
                "metrado_presup":  float(p["metrado_presup"] or 0),
                "metrado_proyec":  float(p["metrado_proyec"]) if p["metrado_proyec"] is not None else None,
                "metrado_ejec":    float(ev.get("cantidad_instalada", 0.0)),
                "nivel":           int(p["nivel"] or 1),
                "parent_codigo":   p["parent_codigo"],
                "es_hoja":         p["fase"] is not None,
                "tipo_costo":      _get(p, "tipo_costo", "DIRECTO"),
                "naturaleza":      _get(p, "naturaleza", "CONTRACTUAL"),
                "hh_ganadas_acum": ev.get("hh_ganadas_acum", 0.0),
                "hh_gastadas_acum":ev.get("hh_gastadas_acum", 0.0),
                "hh_gastadas_dir_acum": ev.get("hh_gastadas_dir_acum", 0.0),
                "hh_gastadas_ind_acum": ev.get("hh_gastadas_ind_acum", 0.0),
                "pct_avance":      ev.get("pct_avance", 0.0),
                "pf_acum":         ev.get("pf_acum", 0.0),
                "pf_dir_acum":     ev.get("pf_dir_acum", 0.0),
            })
        return {"semana": semana, "otm": otm, "filas": result}
    except HTTPException:
        raise
    except Exception:
        # F0.8: traceback al log; al cliente solo mensaje genérico (antes filtraba str(e))
        log.exception("error calculando árbol WBS")
        raise HTTPException(500, "Error interno calculando árbol WBS")


@router.get("/isp")
async def isp_reporte(otm: Optional[str] = None):
    """ISP completo estilo Fluor: ResPorSubFase + Productividades + Resumen.
    Devuelve datos por partida × semana para el periodo completo del proyecto."""
    try:
        pool = await db()
        async with pool.acquire() as con:
            base = await _fecha_base(con)
            if not base:
                return {"semanas": [], "partidas": []}
            today = date.today()
            total = max(_semana_de(today, base), 1)

            if otm:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
                )
            else:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo ORDER BY codigo"
                )
            hitos   = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
            avances = await con.fetch("SELECT * FROM ev_avances ORDER BY semana")
            tareo   = await _hh_gastadas_unificada(con)
            split   = await _hh_real_split(con)

        # Calcular EV para cada semana (una llamada por semana, datos cargados en memoria)
        result_por_partida: dict = {}
        for p in partidas:
            pid = p["id"]
            mp  = float(p["metrado_proyec"] or p["metrado_presup"] or 0)
            hp  = float(p["hh_presup"] or 0)
            fc  = round(hp / mp, 4) if mp > 0 else 0.0
            result_por_partida[pid] = {
                "partida_id":   pid,
                "codigo":       p["codigo"],
                "otm_id":       p["otm_id"],
                "descripcion":  p["descripcion"],
                "unidad":       p["unidad"],
                "fase":         p["fase"],
                "hh_presup":    hp,
                "metrado_presup": float(p["metrado_presup"] or 0),
                "metrado_proyec": mp,
                "factor_conv":  fc,
                "es_hoja":      p["fase"] is not None,
                "nivel":        int(p["nivel"] or 1),
                "parent_codigo": p["parent_codigo"],
                "semanas":      {},
            }

        for s in range(1, total + 1):
            filas = _calcular(list(partidas), list(hitos), list(avances), [], tareo, s, split)
            for f in filas:
                pid = f["partida_id"]
                if pid in result_por_partida:
                    result_por_partida[pid]["semanas"][s] = {
                        "hh_gan_acum":  round(f["hh_ganadas_acum"],   2),
                        "hh_gan_sem":   round(f["hh_ganadas_sem"],    2),
                        "hh_gast_acum": round(f["hh_gastadas_acum"],  2),
                        "hh_gast_sem":  round(f["hh_gastadas_sem"],   2),
                        "pf_acum":      round(f["pf_acum"],           4),
                        "pf_sem":       round(f["pf_sem"],            4),
                        "pct_avance":   round(f["pct_avance"],        4),
                        "cant_acum":    round(
                            f["hh_ganadas_acum"] / (result_por_partida[pid]["factor_conv"] or 1), 2
                        ) if result_por_partida[pid]["factor_conv"] > 0 else 0,
                    }

        # Semanas con labels
        MESES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
        def fmt(d: date) -> str: return f"{d.day} {MESES[d.month-1]}"
        semanas_out = []
        for n in range(1, total + 1):
            lunes  = base + timedelta(weeks=n-1)
            domingo = lunes + timedelta(days=6)
            semanas_out.append({
                "semana": n,
                "label": f"Sem {n}",
                "inicio": lunes.isoformat(),
                "fin": domingo.isoformat(),
                "label_full": f"Sem {n} · {fmt(lunes)}–{fmt(domingo)}",
            })

        return {"semanas": semanas_out, "partidas": list(result_por_partida.values())}
    except HTTPException:
        raise
    except Exception:
        log.exception("error calculando ISP")
        raise HTTPException(500, "Error interno calculando ISP")


@router.get("/reporte")
async def reporte(semana: int, otm: Optional[str] = None):
    partidas, hitos, avances, hh, tareo, split = await _datos_base(semana, otm)
    filas = _calcular(partidas, hitos, avances, hh, tareo, semana, split)

    # Totales SOLO sobre hojas (fase != None): los nodos padre del WBS pueden traer
    # hh_presup propio y sumarlos duplicaría el plan. El detalle (partidas) sí incluye padres.
    hojas = [f for f in filas if f["fase"] is not None]

    # #5: HH improductivas (oficina). Se suman a las HH consumidas del proyecto y
    # bajan el PF (son HH directas de obreros no asignadas a partidas).
    pool = await db()
    async with pool.acquire() as con:
        improd = await _improductivas(con, semana, otm)

    totales = _totales(hojas, improd["acum"])
    totales["hh_improductivas_sem"] = round(improd["sem"], 2)

    return {
        "semana": semana,
        "otm": otm,
        "totales": totales,
        "por_otm": _agrupar(hojas, "otm_id"),
        "por_fase": _agrupar(hojas, "fase"),
        "por_naturaleza": _agrupar(hojas, "naturaleza"),
        "por_sistema": _agrupar(hojas, "sistema"),
        "matriz_area_disciplina": _matriz_area_disciplina(hojas),
        "improductivas": improd,
        "partidas": filas,
    }


@router.get("/curva")
async def curva(hasta: int, otm: Optional[str] = None):
    partidas, hitos, avances, hh, tareo, _split = await _datos_base(hasta, otm)
    semanas_set = sorted(
        {a["semana"] for a in avances} | {r["semana"] for r in hh}
        | {s for (_, s) in tareo.keys()} | {hasta}
    )
    serie = []
    for s in semanas_set:
        if s > hasta:
            continue
        filas = _calcular(partidas, hitos, avances, hh, tareo, s)
        g = sum(f["hh_ganadas_acum"] for f in filas)
        c = sum(f["hh_gastadas_acum"] for f in filas)
        gs = sum(f["hh_ganadas_sem"] for f in filas)
        cs = sum(f["hh_gastadas_sem"] for f in filas)
        serie.append({
            "semana": s,
            "hh_ganadas_acum": round(g, 2),
            "hh_gastadas_acum": round(c, 2),
            "pf_acum": round(g / c, 3) if c > 0 else None,
            "pf_sem": round(gs / cs, 3) if cs > 0 else None,
        })
    return serie


@router.get("/curva-fase")
async def curva_fase(hasta: int, otm: Optional[str] = None):
    """Serie semanal de PF acumulado por fase — gráficos por disciplina."""
    partidas, hitos, avances, hh, tareo, _split = await _datos_base(hasta, otm)
    semanas_set = sorted(
        {a["semana"] for a in avances} | {r["semana"] for r in hh}
        | {s for (_, s) in tareo.keys()} | {hasta}
    )
    fases = sorted({p["fase"] for p in partidas})
    serie = []
    for s in semanas_set:
        if s > hasta:
            continue
        filas = _calcular(partidas, hitos, avances, hh, tareo, s)
        punto: dict = {"semana": s}
        for fase in fases:
            ff = [f for f in filas if f["fase"] == fase]
            g = sum(f["hh_ganadas_acum"] for f in ff)
            c = sum(f["hh_gastadas_acum"] for f in ff)
            punto[f"pf_{fase}"] = round(g / c, 3) if c > 0 else None
        serie.append(punto)
    return {"fases": fases, "serie": serie}


