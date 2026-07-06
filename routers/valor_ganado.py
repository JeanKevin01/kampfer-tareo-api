# ============================================================
# routers/valor_ganado.py  —  v2
# Módulo Valor Ganado - lógica ISP Fluor digitalizada
#
# Novedades v2:
#   - OTM por encima de fase/sub-fase (ev_partidas.otm_id)
#   - POST /ev/importar: carga masiva de partidas (desde cero o con
#     histórico de avances y HH) en una sola transacción
#   - GET/POST /ev/plantillas: catálogo de rules of credit por tipo
#     de actividad
#   - HH del tareo QR leídas DIRECTO de tareo_partida (fuente única desde F0.3);
#     mapeo fecha->semana vía ev_config.fecha_base (auto-derivada del primer tareo)
#   - GET /ev/curva-fase: tendencia de PF por disciplina
#
# Integración en main.py (sin cambios respecto a v1):
#   from routers.valor_ganado import router as ev_router
#   app.include_router(ev_router)   # después de crear app
# ============================================================
import os
import json
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.log import get_logger

log = get_logger("ev")

router = APIRouter(prefix="/ev", tags=["valor-ganado"])
# F0.6: endpoints /ev que usa el FLUJO DE CAMPO (app del supervisor). Se montan en
# main.py con require_role() a secas (autenticado), mientras `router` exige 'oficina'.
router_campo = APIRouter(prefix="/ev", tags=["valor-ganado-campo"])

# F0.4: zona horaria y semana_de desde core/tiempo (implementación única)
from core.tiempo import LIMA, semana_de as _semana_de  # noqa: F401,E402
# F0.4: el pool privado de este módulo fue reemplazado por el pool ÚNICO del API.
from core.db import db  # noqa: E402

# F0.5b: el motor puro y la capa de datos del EV viven en routers/ev/.
# Estos re-exports mantienen a los tests (y a routers/tareo) importando de aquí.
from routers.ev._datos import (  # noqa: E402,F401
    _hoy_lima, _as_date, _get, _norm_tipo_costo, _norm_naturaleza, _fecha_base,
    _hh_real_por_semana, _hh_real_split, _hh_gastadas_unificada, _improductivas,
    _datos_base,
)
from routers.ev._engine import (  # noqa: E402,F401
    _validar_pesos, _acum_a_semana, _calcular, _agrupar, _matriz_area_disciplina,
    _calc_costo_mo, _totales,
)



# (F0.5b: _validar_pesos y _fecha_base viven en routers/ev/ — re-exportadas arriba.)




# ── F0.5b: ensamblado de los sub-routers de /ev ───────────────
# Cada sub-módulo define `router` (oficina) y `router_campo` (app del supervisor)
# SIN prefijo; aquí se agregan bajo /ev. main.py sigue montando este módulo.
from routers.ev import (  # noqa: E402
    anomalias as _ev_anomalias, avance_diario as _ev_avance, captura as _ev_captura,
    conflictos as _ev_conflictos, historico as _ev_historico,
    improductivas as _ev_improd, isp as _ev_isp, partidas as _ev_partidas,
    rendimiento as _ev_rend, tarifas as _ev_tarifas, valorizado as _ev_valorizado,
)
from routers.ev._modelos import *  # noqa: E402,F401,F403 (compat: modelos re-exportados)
from routers.ev.tarifas import _HH_POR_CARGO_SQL, _resultado_operativo  # noqa: E402,F401 (tests)

for _m in (_ev_partidas, _ev_captura, _ev_isp, _ev_anomalias, _ev_improd,
           _ev_valorizado, _ev_tarifas, _ev_avance, _ev_rend, _ev_historico,
           _ev_conflictos):
    router.include_router(_m.router)
    router_campo.include_router(_m.router_campo)
