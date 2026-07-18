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

@router.get("/semana-grid")
async def semana_grid(
    semana: int,
    otm: Optional[str] = None,
    lunes: Optional[str] = None,   # ISO date del lunes — override de fecha_base
):
    """
    Grilla semanal: partidas × días (Lun-Dom).
    Incluye HH reales (tareo_partida), HH estimadas (registros histórico)
    y cant_ejecutada (ev_avances_diarios).
    """
    try:
        pool = await db()
        async with pool.acquire() as con:
            # ── Fechas del período ───────────────────────────────
            if lunes:
                lunes_date = date.fromisoformat(lunes)
            else:
                base = await _fecha_base(con)
                if not base:
                    raise HTTPException(
                        400,
                        "Fecha base no configurada. Ve a Configuración en el panel, "
                        "o pasa ?lunes=YYYY-MM-DD como parámetro."
                    )
                lunes_date = base + timedelta(weeks=semana - 1)

            domingo_date = lunes_date + timedelta(days=6)
            fechas = [lunes_date + timedelta(days=i) for i in range(7)]
            fechas_str = [f.isoformat() for f in fechas]

            # ── Partidas hoja de la OTM ──────────────────────────
            q = """
                SELECT p.id, p.codigo, p.descripcion, p.fase, p.sub_fase,
                       p.unidad, p.hh_presup, p.metrado_presup,
                       p.nivel, p.parent_codigo,
                       CASE WHEN p.metrado_presup > 0
                            THEN p.hh_presup / p.metrado_presup
                            ELSE 0 END AS factor_conv
                FROM ev_partidas p
                WHERE p.activo = true AND p.fase IS NOT NULL
            """
            args: list = []
            if otm:
                args.append(otm)
                q += f" AND p.otm_id = ${len(args)}"
            q += " ORDER BY p.codigo"

            partidas = await con.fetch(q, *args)

            # ── Nodos de agrupación (padres, fase IS NULL) ───────
            qg = """
                SELECT p.codigo, p.descripcion, p.nivel, p.parent_codigo
                FROM ev_partidas p
                WHERE p.activo = true AND p.fase IS NULL
            """
            gargs: list = []
            if otm:
                gargs.append(otm)
                qg += f" AND p.otm_id = ${len(gargs)}"
            qg += " ORDER BY p.codigo"
            grupos = await con.fetch(qg, *gargs)
            if not partidas:
                return {
                    "semana":   semana,
                    "otm":      otm,
                    "lunes":    lunes_date.isoformat(),
                    "fechas":   fechas_str,
                    "partidas": [],
                }

            p_ids         = [p["id"] for p in partidas]
            total_hh_pres = sum(float(p["hh_presup"] or 0) for p in partidas)

            # ── HH exactas: tareo_partida (nuevo flujo) ──────────
            hh_rows = await con.fetch(
                """SELECT partida_id, fecha::text AS f, SUM(hh) AS hh_total
                   FROM tareo_partida
                   WHERE partida_id = ANY($1)
                     AND fecha >= $2::date AND fecha <= $3::date
                     AND hh IS NOT NULL
                   GROUP BY partida_id, fecha""",
                p_ids,
                lunes_date,
                domingo_date,
            )
            hh_map = {
                (r["partida_id"], r["f"]): float(r["hh_total"] or 0)
                for r in hh_rows
            }

            # ── HH estimadas: registros histórico (fallback) ─────
            # Total HH del OTM por día (tareo viejo, sin asignación a partida)
            fallback_by_date: dict = {}
            if otm and total_hh_pres > 0:
                fb = await con.fetch(
                    """SELECT fecha::text AS f, SUM(hh) AS hh_dia
                       FROM registros
                       WHERE otm_id = $1
                         AND fecha >= $2::date AND fecha <= $3::date
                         AND hh IS NOT NULL AND hh > 0
                       GROUP BY fecha""",
                    otm,
                    lunes_date,
                    domingo_date,
                )
                fallback_by_date = {r["f"]: float(r["hh_dia"] or 0) for r in fb}

            # ── Avances diarios (cant_ejecutada) ─────────────────
            # Solo hito principal (hito_id NULL) = cantidad instalada; las
            # etapas desplegadas por hitos viven en el LookAhead (0025).
            cant_rows = await con.fetch(
                """SELECT partida_id, fecha::text AS f, cantidad_dia
                   FROM ev_avances_diarios
                   WHERE partida_id = ANY($1)
                     AND fecha >= $2::date AND fecha <= $3::date
                     AND hito_id IS NULL""",
                p_ids,
                lunes_date,
                domingo_date,
            )
            cant_map   = {(r["partida_id"], r["f"]): float(r["cantidad_dia"] or 0) for r in cant_rows}
            cant_exist = {(r["partida_id"], r["f"]) for r in cant_rows}

            # ── Construir resultado ───────────────────────────────
            result = []
            for p in partidas:
                pid    = p["id"]
                factor = float(p["factor_conv"] or 0)
                hh_p   = float(p["hh_presup"]  or 0)
                dias   = {}

                for fecha in fechas:
                    fs  = fecha.isoformat()
                    key = (pid, fs)

                    # HH real (nuevo tareo)
                    hh_real = hh_map.get(key, 0)

                    # HH estimada (tareo viejo proporcional)
                    hh_est = 0.0
                    if hh_real == 0 and hh_p > 0 and total_hh_pres > 0:
                        hh_dia_otm = fallback_by_date.get(fs, 0)
                        if hh_dia_otm > 0:
                            hh_est = round(hh_p / total_hh_pres * hh_dia_otm, 2)

                    # Cant ejecutada
                    cant       = cant_map.get(key, None) if key in cant_exist else None
                    hh_activa  = hh_real if hh_real > 0 else hh_est
                    hh_ganadas = round(cant * factor, 4) if cant is not None and factor > 0 else None
                    pf         = round(hh_ganadas / hh_activa, 3) if hh_ganadas and hh_activa > 0 else None

                    if hh_real > 0 or hh_est > 0 or key in cant_exist:
                        dias[fs] = {
                            "hh_gastadas":    round(hh_real, 2),   # nuevo tareo (exacto)
                            "hh_estimada":    hh_est,               # tareo viejo (proporcional)
                            "cant_ejecutada": cant,                  # None = no ingresada aún
                            "hh_ganadas":     hh_ganadas,
                            "pf":             pf,
                        }

                result.append({
                    "id":             pid,
                    "codigo":         p["codigo"],
                    "descripcion":    p["descripcion"],
                    "fase":           p["fase"],
                    "sub_fase":       p["sub_fase"],
                    "nivel":          int(p["nivel"] or 1),
                    "parent_codigo":  p["parent_codigo"],
                    "unidad":         p["unidad"],
                    "factor_conv":    round(factor, 4),
                    "hh_presup":      float(p["hh_presup"]      or 0),
                    "metrado_presup": float(p["metrado_presup"] or 0),
                    "dias":           dias,
                })

            grupos_out = [
                {
                    "codigo":        g["codigo"],
                    "descripcion":   g["descripcion"],
                    "nivel":         int(g["nivel"] or 1),
                    "parent_codigo": g["parent_codigo"],
                }
                for g in grupos
            ]

            return {
                "semana":   semana,
                "otm":      otm,
                "lunes":    lunes_date.isoformat(),
                "fechas":   fechas_str,
                "grupos":   grupos_out,
                "partidas": result,
            }
    except HTTPException:
        raise
    except Exception:
        log.exception("error calculando grilla diaria")
        raise HTTPException(500, "Error interno calculando grilla diaria")


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


