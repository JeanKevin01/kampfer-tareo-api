# ============================================================
# routers/ev/captura.py — config (fecha base) + semanas + captura de avances + semanas-auto (F0.5b)
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


@router.get("/config")
async def get_config():
    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
    return {"fecha_base": base.isoformat() if base else None}


@router.put("/config")
async def put_config(body: dict):
    fb = body.get("fecha_base")
    if not fb:
        raise HTTPException(400, "fecha_base requerida (YYYY-MM-DD)")
    date.fromisoformat(fb)  # valida formato
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO ev_config (clave, valor) VALUES ('fecha_base', $1)
               ON CONFLICT (clave) DO UPDATE SET valor=$1""", fb
        )
    return {"ok": True, "fecha_base": fb}


# ---------------------- Tareo QR → partida ----------------------
@router.get("/semanas")
async def semanas():
    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
        rows = await con.fetch(
            """SELECT DISTINCT semana FROM (
                 SELECT semana FROM ev_avances
                 UNION SELECT semana FROM ev_hh_gastadas
               ) s"""
        )
        sem = {r["semana"] for r in rows}
        if base:
            tareo = await con.fetch(
                "SELECT DISTINCT fecha FROM tareo_partida WHERE hh IS NOT NULL AND hh > 0"
            )
            for t in tareo:
                sem.add(_semana_de(t["fecha"], base))
    return sorted(sem)


# (F0.5b: _hh_real_por_semana, _hh_real_split y _hh_gastadas_unificada viven en
#  routers/ev/_datos.py — re-exportadas arriba.)


@router.get("/captura")
async def captura(semana: int, otm: Optional[str] = None):
    pool = await db()
    async with pool.acquire() as con:
        if otm:
            partidas = await con.fetch(
                "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
            )
        else:
            partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
        avances = await con.fetch(
            """SELECT hito_id, semana, cantidad_acum FROM ev_avances
               WHERE semana <= $1 ORDER BY hito_id, semana""", semana
        )
        hh_man = await con.fetch(
            "SELECT partida_id, semana, hh FROM ev_hh_gastadas WHERE semana = $1", semana
        )
        # F0.3 (fix inconsistencia #10): hh_tareo ahora son las HH EXACTAS del tareo QR
        # (antes mostraba la distribución proporcional y difería del reporte)
        tareo = await _hh_real_por_semana(con)

    ult_av, av_actual = {}, {}
    for a in avances:
        if a["semana"] == semana:
            av_actual[a["hito_id"]] = float(a["cantidad_acum"])
        else:
            ult_av[a["hito_id"]] = float(a["cantidad_acum"])

    hh_manual = {r["partida_id"]: float(r["hh"]) for r in hh_man}

    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(h)

    out = []
    for p in partidas:
        out.append({
            "partida_id": p["id"],
            "codigo": p["codigo"],
            "otm_id": p["otm_id"],
            "fase": p["fase"],
            "sub_fase": p["sub_fase"],
            "nivel": int(p["nivel"] or 1),
            "parent_codigo": p["parent_codigo"],
            "es_hoja": p["fase"] is not None,
            "hh_presup": float(p["hh_presup"] or 0),
            "descripcion": p["descripcion"],
            "unidad": p["unidad"],
            "metrado_proyec": float(p["metrado_proyec"] or p["metrado_presup"]),
            "hh_tareo": round(tareo.get((p["id"], semana), 0.0), 2),
            "hh_semana": hh_manual.get(p["id"], 0.0),
            "hitos": [
                {
                    "hito_id": h["id"], "numero": h["numero"],
                    "descripcion": h["descripcion"], "peso": float(h["peso"]),
                    "es_principal": h["es_principal"],
                    "cant_anterior": ult_av.get(h["id"], 0.0),
                    "cant_actual": av_actual.get(h["id"], ult_av.get(h["id"], 0.0)),
                }
                for h in por_partida.get(p["id"], [])
            ],
        })
    return out


@router.post("/captura")
async def guardar_captura(body: CapturaIn):
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for a in body.avances:
                await con.execute(
                    """INSERT INTO ev_avances (hito_id, semana, cantidad_acum)
                       VALUES ($1,$2,$3)
                       ON CONFLICT (hito_id, semana)
                       DO UPDATE SET cantidad_acum=$3, registrado_en=now()""",
                    a.hito_id, body.semana, a.cantidad_acum,
                )
            for r in body.hh_gastadas:
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1,$2,$3,'manual')
                       ON CONFLICT (partida_id, semana) DO UPDATE SET hh=$3""",
                    r.partida_id, body.semana, r.hh,
                )
    return {"ok": True}



@router_campo.get("/semanas-auto")
async def semanas_auto():
    """Semanas reales del proyecto (Lun-Dom) desde el primer registro de tareo.
    Incluye semanas sin actividad para mostrar la línea de tiempo completa."""
    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
        if not base:
            return []

        hh_rows = await con.fetch("""
            SELECT DATE_TRUNC('week', fecha)::date AS lunes, SUM(hh) AS hh_total
            FROM tareo_partida WHERE hh IS NOT NULL AND hh > 0
            GROUP BY DATE_TRUNC('week', fecha)::date
            ORDER BY lunes
        """)
        if not hh_rows:
            # Sin registros aún — devolver semana 1 para que el panel no quede colgado
            lunes0  = base
            dom0    = lunes0 + timedelta(days=6)
            def _fm(d): return f"{d.day} {['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'][d.month-1]}"
            return [{
                "semana": 1, "inicio": lunes0.isoformat(), "fin": dom0.isoformat(),
                "hh": 0.0, "activa": False,
                "label": f"Sem 1  ·  {_fm(lunes0)} – {_fm(dom0)}  (sin actividad aún)"
            }]

        hh_map: dict = {}
        for r in hh_rows:
            n = _semana_de(r['lunes'], base)
            hh_map[n] = float(r['hh_total'])

        today = date.today()
        current_monday = today - timedelta(days=today.weekday())
        total = max(_semana_de(current_monday, base), max(hh_map.keys()))

        MESES = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
        def fmt(d: date) -> str:
            return f"{d.day} {MESES[d.month-1]}"

        result = []
        for n in range(1, total + 1):
            lunes  = base + timedelta(weeks=n - 1)
            domingo = lunes + timedelta(days=6)
            hh     = hh_map.get(n, 0.0)
            result.append({
                "semana": n,
                "inicio": lunes.isoformat(),
                "fin":    domingo.isoformat(),
                "hh":     round(hh, 1),
                "activa": hh > 0,
                "label":  f"Sem {n}  ·  {fmt(lunes)} – {fmt(domingo)}"
                          + ("" if hh > 0 else "  (sin actividad)"),
            })
        return result


