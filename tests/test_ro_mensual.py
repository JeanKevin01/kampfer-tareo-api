# -*- coding: utf-8 -*-
"""FASE 2 — periodos, ajuste MO y (luego) motor RO mensual. Tests puros/sin BD."""
import pytest
from fastapi.testclient import TestClient

from core import auth, config
import main
from routers.ro import calcular_ajuste_mo


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── F2.1: reabrir es SOLO admin ───────────────────────────────
def test_reabrir_oficina_403():
    r = _client().post("/ev/periodos/1/reabrir", headers=_hdr("oficina"))
    assert r.status_code == 403


def test_periodos_supervisor_403():
    r = _client().get("/ev/periodos", headers=_hdr("supervisor"))
    assert r.status_code == 403


# ── F2.3: calcular_ajuste_mo (función pura) ───────────────────
_HH = [{"fase": "11", "cargo": "PEON", "hh": 100},
       {"fase": "11", "cargo": "OFICIAL", "hh": 50},
       {"fase": "12", "cargo": "PEON", "hh": 50}]
_TARIFAS = {"PEON": 10.0, "OFICIAL": 20.0}


def test_ajuste_mo_delta_y_por_fase():
    # MO tareo: fase 11 = 100×10 + 50×20 = 2000 · fase 12 = 500 → total 2500
    c = calcular_ajuste_mo(_HH, _TARIFAS, None, planilla_real=3000)
    assert c["mo_tareo"] == 2500.0 and c["delta"] == 500.0
    assert c["por_fase"] == {"11": 2000.0, "12": 500.0}
    # distribución proporcional: 11 → 400, 12 → 100 (el último absorbe redondeo)
    assert c["ajustes"] == [{"fase": "11", "monto": 400.0}, {"fase": "12", "monto": 100.0}]
    assert round(sum(a["monto"] for a in c["ajustes"]), 2) == 500.0


def test_ajuste_mo_sin_distribuir():
    c = calcular_ajuste_mo(_HH, _TARIFAS, None, planilla_real=2000, distribuir_por_fase=False)
    assert c["ajustes"] == [{"fase": None, "monto": -500.0}]


def test_ajuste_mo_sin_tareo():
    c = calcular_ajuste_mo([], {}, None, planilla_real=1000)
    assert c["mo_tareo"] == 0.0 and c["delta"] == 1000.0
    assert c["ajustes"] == [{"fase": None, "monto": 1000.0}]


def test_ajuste_mo_redondeo_cierra():
    hh = [{"fase": f, "cargo": "PEON", "hh": 1} for f in ("A", "B", "C")]
    c = calcular_ajuste_mo(hh, {"PEON": 10.0}, None, planilla_real=40.0)
    assert round(sum(a["monto"] for a in c["ajustes"]), 2) == c["delta"] == 10.0
