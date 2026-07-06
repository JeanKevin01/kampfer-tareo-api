# ============================================================
# routers/ev/anomalias.py — monitor de anomalias del EV (F0.5b)
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


@router.get("/monitor/anomalias")
async def monitor_anomalias(otm: Optional[str] = None, semana: int = 1,
                            pf_min: float = 0.85, pf_max: float = 1.20):
    """Detecta errores en el tareo a nivel de partida (hoja): HH sin avance,
    avance sin HH, PF fuera de rango y avances que superan el 100%."""
    try:
        pool = await db()
        async with pool.acquire() as con:
            if otm:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm)
            else:
                partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
            hitos   = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
            avances = await con.fetch(
                "SELECT hito_id, semana, cantidad_acum FROM ev_avances WHERE semana <= $1", semana)
            tareo   = await _hh_gastadas_unificada(con)

        filas = _calcular(list(partidas), list(hitos), list(avances), [], tareo, semana)
        anomalias = []
        for f in filas:
            if f["fase"] is None:          # padres del WBS: sin PF propio
                continue
            gast = f["hh_gastadas_acum"]; cant = f["cantidad_instalada"]
            pf   = f["pf_acum"];          pct  = f["pct_avance"]
            flags = []
            if gast > 0 and cant <= 0:
                flags.append({"tipo": "hh_sin_avance", "sev": "alta",
                              "msg": "HH gastadas sin metrado ejecutado"})
            if gast <= 0 and cant > 0:
                flags.append({"tipo": "avance_sin_hh", "sev": "media",
                              "msg": "Metrado ejecutado sin HH registradas"})
            if gast > 0 and 0 < pf < pf_min:
                flags.append({"tipo": "pf_bajo", "sev": "alta",
                              "msg": f"PF {pf:.2f} bajo (< {pf_min}) — más HH que avance"})
            if pf > pf_max:
                flags.append({"tipo": "pf_alto", "sev": "media",
                              "msg": f"PF {pf:.2f} alto (> {pf_max}) — revisar avance/HH"})
            if pct > 1.001:
                flags.append({"tipo": "avance_excede", "sev": "media",
                              "msg": f"Avance {pct*100:.0f}% supera el 100%"})
            if flags:
                anomalias.append({
                    "partida_id": f["partida_id"], "codigo": f["codigo"], "otm_id": f["otm_id"],
                    "descripcion": f["descripcion"], "fase": f["fase"], "unidad": f["unidad"],
                    "hh_gastadas": round(gast, 1), "hh_ganadas": round(f["hh_ganadas_acum"], 1),
                    "metrado_ejec": round(cant, 2), "pf_acum": round(pf, 2),
                    "pct_avance": round(pct, 4), "flags": flags,
                })
        # Más severas primero
        anomalias.sort(key=lambda a: (0 if any(x["sev"] == "alta" for x in a["flags"]) else 1, a["codigo"]))
        return {"otm": otm, "semana": semana, "total": len(anomalias), "anomalias": anomalias}
    except HTTPException:
        raise
    except Exception:
        log.exception("error en monitor de anomalías")
        raise HTTPException(500, "Error interno en monitor de anomalías")


