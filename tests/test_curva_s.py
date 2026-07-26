"""Curva S completa — el PV (línea base del plan) y los indicadores EVM.

`_pv_acum_por_semana` es pura: se prueba sin BD. Lo esencial es que el PV use
EXACTAMENTE la misma fórmula que el EV, porque si no SPI y SV comparan peras
con manzanas.
"""
import pytest
from fastapi.testclient import TestClient

from core import auth, config
from routers.ev._engine import _calcular
from routers.ev.curva_s import _indicadores, _pv_acum_por_semana
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# Partida: 100 m³ y 50 HH ⇒ estándar 0.5 HH/m³
PARTIDAS = [{"id": 1, "codigo": "01", "otm_id": "O1", "fase": "EST", "sistema": "A",
             "descripcion": "d", "unidad": "m3", "metrado_presup": 100.0,
             "metrado_proyec": None, "hh_presup": 50.0, "hh_actualizado": None,
             "tipo_costo": "DIRECTO", "naturaleza": "CONTRACTUAL"}]
# Dos etapas: 40% habilitado, 60% vaciado (el principal)
HITOS = [{"id": 10, "partida_id": 1, "numero": 1, "peso": 0.4, "es_principal": False},
         {"id": 11, "partida_id": 1, "numero": 2, "peso": 0.6, "es_principal": True}]


def test_pv_convierte_metrado_programado_a_hh():
    """50 m³ programados del hito de peso 0.6 ⇒ 0.6 × 50 × 0.5 = 15 HH."""
    prog = [{"partida_id": 1, "hito_id": 11, "semana": 1, "cantidad": 50.0}]
    pv = _pv_acum_por_semana(prog, PARTIDAS, HITOS, [1, 2])
    assert pv[1] == 15.0
    assert pv[2] == 15.0          # acumulado: no baja


def test_pv_acumula_entre_semanas():
    prog = [{"partida_id": 1, "hito_id": 11, "semana": 1, "cantidad": 50.0},
            {"partida_id": 1, "hito_id": 11, "semana": 2, "cantidad": 50.0}]
    pv = _pv_acum_por_semana(prog, PARTIDAS, HITOS, [1, 2])
    assert pv[1] == 15.0
    assert pv[2] == 30.0          # 100 m³ × 0.6 × 0.5 = todo el peso del hito


def test_pv_hito_null_va_al_principal():
    """La actividad sin hito explícito programa el hito PRINCIPAL."""
    prog = [{"partida_id": 1, "hito_id": None, "semana": 1, "cantidad": 50.0}]
    pv = _pv_acum_por_semana(prog, PARTIDAS, HITOS, [1])
    assert pv[1] == 15.0          # mismo resultado que apuntar al hito 11


def test_pv_topa_al_peso_del_hito():
    """Sobre-programar una etapa no puede inflar el PV por encima de su peso
    (mismo tope min(...,1.0) que aplica el EV)."""
    prog = [{"partida_id": 1, "hito_id": 11, "semana": 1, "cantidad": 500.0}]
    pv = _pv_acum_por_semana(prog, PARTIDAS, HITOS, [1])
    assert pv[1] == 30.0          # 0.6 × 50 HH, no 150


def test_pv_ignora_partidas_fuera_del_filtro():
    prog = [{"partida_id": 99, "hito_id": 11, "semana": 1, "cantidad": 50.0}]
    assert _pv_acum_por_semana(prog, PARTIDAS, HITOS, [1])[1] == 0.0


def test_pv_y_ev_usan_LA_MISMA_formula():
    """La prueba que da sentido al SPI: si lo programado se ejecuta EXACTO,
    PV y EV deben coincidir al céntimo. Si divergieran, el SPI mentiría."""
    avances = [{"hito_id": 11, "semana": 1, "cantidad_acum": 50.0}]
    filas = _calcular(PARTIDAS, HITOS, avances, [], {}, 1)
    ev = sum(f["hh_ganadas_acum"] for f in filas)

    prog = [{"partida_id": 1, "hito_id": 11, "semana": 1, "cantidad": 50.0}]
    pv = _pv_acum_por_semana(prog, PARTIDAS, HITOS, [1])[1]

    assert pv == ev == 15.0
    assert _indicadores(pv, ev, ev, 50.0, 50.0)["spi"] == 1.0


# ── Indicadores EVM ──────────────────────────────────────────
def test_indicadores_atraso_y_sobrecosto():
    """Plan 100, ganado 80, gastado 120 ⇒ atrasado y sobrecostado."""
    i = _indicadores(pv=100.0, ev=80.0, ac=120.0, bac=200.0, eac=250.0)
    assert i["sv"] == -20.0 and i["spi"] == 0.8      # atrasado
    assert i["cv"] == -40.0 and i["cpi"] == round(80 / 120, 3)
    assert i["etc"] == 130.0                          # 250 - 120
    assert i["vac"] == -50.0                          # 200 - 250 (se pasará)
    assert i["tcpi"] == round((200 - 80) / (200 - 120), 3)


def test_indicadores_sin_datos_no_revientan():
    i = _indicadores(pv=0.0, ev=0.0, ac=0.0, bac=0.0, eac=0.0)
    assert i["spi"] is None and i["cpi"] is None and i["tcpi"] is None


# ── Roles ────────────────────────────────────────────────────
def test_curva_s_sin_credenciales_401():
    assert _client().get("/ev/curva-s?hasta=1").status_code == 401


def test_curva_s_supervisor_403():
    tk = auth.make_token("u-sup", "supervisor", "supervisor", extra={"sup_id": "01"})
    r = _client().get("/ev/curva-s?hasta=1", headers={"Authorization": "Bearer " + tk})
    assert r.status_code == 403
