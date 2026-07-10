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


# ── F2.5: motor RO mensual ────────────────────────────────────
from routers.ro_motor import ro_mensual
from tests.fixtures import ro2007


def _insumos_ro2007():
    """Convierte el fixture (R FASES) en insumos del motor: 1 periodo con todo."""
    RECS = ("MAT", "MO", "EQP", "EQT", "SUB", "DIR", "GG")
    docs, venta = [], {}
    for fases, directo in ((ro2007.FASES_DIR, True), (ro2007.FASES_IND, False)):
        for fase, vals in fases.items():
            for rec, monto in zip(RECS, vals[:7]):
                if monto:
                    docs.append({"periodo_id": 1, "fase": fase, "tipo_recurso": rec,
                                 "directo": directo, "monto": monto})
            venta.setdefault(1, {})[fase] = vals[7]
    return docs, venta


def test_motor_reproduce_ro_2007():
    """Aceptación (DoD F2.5): el motor reproduce los totales del Excel RO-2007."""
    docs, venta = _insumos_ro2007()
    out = ro_mensual(
        periodos=[{"id": 1, "anio": 2007, "mes": 9, "tipo_cambio": ro2007.TC}],
        corte_id=1, docs=docs, mo_tareo_mes={}, venta_fase_mes=venta,
        ajustes=[], costo_meta={}, costo_contractual={}, proyeccion=[],
        fases_indirectas={f for f in ro2007.FASES_IND},
    )
    t = out["totales"]
    par_dir = next(f for f in out["r_fases"] if f["descripcion"] == "PARCIAL DIRECTOS")
    tot = next(f for f in out["r_fases"] if f["descripcion"] == "TOTAL OBRA")
    assert abs(par_dir["total"] - ro2007.PARCIAL_DIRECTOS) < 0.5
    assert abs(tot["total"] - ro2007.TOTAL_OBRA) < 0.5
    assert abs(t["venta_acum"] - ro2007.VENTA_TOTAL) < 0.5
    assert abs(t["margen_total"] - ro2007.MARGEN) < 0.5
    assert abs(t["pct_margen"] - ro2007.PCT_MARGEN) < 0.001
    assert abs(out["usd"]["costo_acum"] - ro2007.TOTAL_OBRA_USD) < 1.0


def test_motor_costo_aplicado_y_pendiente():
    """Regla: aplicado = venta_acum × (costo_proy/venta_proy); pendiente = acum − aplicado."""
    out = ro_mensual(
        periodos=[{"id": 1, "anio": 2026, "mes": 1, "tipo_cambio": 1},
                  {"id": 2, "anio": 2026, "mes": 2, "tipo_cambio": 1}],
        corte_id=1,
        docs=[{"periodo_id": 1, "fase": "10", "tipo_recurso": "MAT", "directo": True, "monto": 800}],
        mo_tareo_mes={}, venta_fase_mes={1: {"10": 1000}}, ajustes=[],
        costo_meta={}, costo_contractual={},
        proyeccion=[{"periodo_id": 2, "fase": "10", "tipo_recurso": "MAT", "monto": 100}],
        venta_proyectada_extra=200,
    )
    t = out["totales"]
    # costo_proy = 800+100=900; venta_proy = 1000+200=1200 → factor 0.75
    assert t["costo_aplicado_acum"] == 750.0          # 1000 × 0.75
    assert t["resultado_pendiente"] == 50.0           # 800 − 750
    assert t["margen_economico_acum"] == 250.0        # 1000 − 750
    assert t["margen_total"] == 300.0 and t["pct_margen"] == 0.25
    assert out["t_obra"]["saldo_obra"] == 100.0


def test_motor_usd_mensualiza_tipo_cambio():
    """Los acumulados USD suman cada mes convertido con SU tc (no el del corte)."""
    out = ro_mensual(
        periodos=[{"id": 1, "anio": 2026, "mes": 1, "tipo_cambio": 2.0},
                  {"id": 2, "anio": 2026, "mes": 2, "tipo_cambio": 4.0}],
        corte_id=2,
        docs=[{"periodo_id": 1, "fase": "10", "tipo_recurso": "MAT", "directo": True, "monto": 100},
              {"periodo_id": 2, "fase": "10", "tipo_recurso": "MAT", "directo": True, "monto": 100}],
        mo_tareo_mes={}, venta_fase_mes={1: {"10": 200}, 2: {"10": 400}},
        ajustes=[], costo_meta={}, costo_contractual={}, proyeccion=[],
    )
    assert out["usd"]["costo_acum"] == 75.0     # 100/2 + 100/4
    assert out["usd"]["venta_acum"] == 200.0    # 200/2 + 400/4
    assert out["usd"]["tc_corte"] == 4.0


def test_motor_conceptos_venta_y_mo_tareo():
    out = ro_mensual(
        periodos=[{"id": 1, "anio": 2026, "mes": 1, "tipo_cambio": 1}],
        corte_id=1, docs=[],
        mo_tareo_mes={1: {"10": 500}},
        venta_fase_mes={1: {"10": 1000}},
        ajustes=[{"periodo_id": 1, "tipo": "NUEVAS_PARTIDAS", "fase": "10",
                  "monto": 300, "margen_previsto": 60},
                 {"periodo_id": 1, "tipo": "REAJUSTE", "fase": None, "monto": -50,
                  "margen_previsto": None}],
        costo_meta={("10", "MO"): 450}, costo_contractual={},
        proyeccion=[], contingencia=100,
    )
    v = {c["concepto"]: c for c in out["t_obra"]["venta"]}
    assert v["CONTRACTUAL"]["acum"] == 1000.0
    assert v["NUEVAS_PARTIDAS"]["acum"] == 300.0 and v["NUEVAS_PARTIDAS"]["margen_previsto_acum"] == 60.0
    assert v["REAJUSTE"]["acum"] == -50.0
    t = out["totales"]
    assert t["venta_acum"] == 1250.0 and t["costo_acum"] == 500.0
    assert t["margen_total"] == 750.0
    assert t["margen_con_contingencia"] == 650.0
    fila10 = next(f for f in out["r_fases"] if f["fase"] == "10")
    assert fila10["mo"] == 500.0 and fila10["venta"] == 1300.0 and fila10["meta"] == 450.0
