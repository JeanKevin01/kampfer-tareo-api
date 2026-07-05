"""
F0.6 — matriz de roles /ev e identidad de supervisor (anti-suplantación).

Los casos 403 se prueban con TestClient sin tocar la BD (la dependencia corta
antes del handler). Los casos "autorizado" solo verifican que la respuesta NO
sea 401/403 (si no hay BD el handler devolverá 500, pero la autorización pasó).
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import auth, config
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str, sup_id: str = None):
    extra = {"sup_id": sup_id} if sup_id else None
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol, extra=extra)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    """API cerrada (como prod): con API_KEY configurada la compuerta exige credenciales."""
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── Matriz /ev: oficina vs campo ─────────────────────────────
def test_supervisor_no_entra_a_ev_de_oficina():
    r = _client().get("/ev/tarifas", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_supervisor_no_puede_captura():
    r = _client().post("/ev/captura", json={}, headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_supervisor_si_pasa_endpoints_de_campo():
    # /ev/semanas-auto es de campo: la autorización NO debe cortarlo (401/403).
    r = _client().get("/ev/semanas-auto", headers=_hdr("supervisor", "01"))
    assert r.status_code not in (401, 403)


def test_oficina_pasa_ev():
    r = _client().get("/ev/tarifas", headers=_hdr("oficina"))
    assert r.status_code not in (401, 403)


def test_sin_credenciales_401():
    r = _client().get("/ev/tarifas")
    assert r.status_code == 401


# ── Identidad de supervisor (anti-suplantación) ──────────────
def test_enviar_con_partidas_suplantado_403():
    body = {"supervisor_id": "02", "otm_id": "OTM-X",
            "trabajadores": [{"trab_id": "1", "asignaciones": [{"partida_id": 1, "hh": 8}]}]}
    r = _client().post("/api/sesion/enviar-con-partidas", json=body,
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_crear_sesion_suplantado_403():
    r = _client().post("/api/sesion", json={"supervisor_id": "02", "otm_id": "OTM-X"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_cuadrilla_ajena_403():
    r = _client().post("/api/cuadrilla/02/001", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_cambio_partida_suplantado_403():
    body = {"trabajador_ids": ["001"], "partida_id": 1,
            "otm_id": "OTM-X", "supervisor_id": "02"}
    r = _client().post("/api/tareo-partida/cambio", json=body,
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


# ── Función pura ─────────────────────────────────────────────
def test_identidad_propia_pasa():
    auth.exigir_identidad_supervisor({"rol": "supervisor", "sup_id": "01"}, "01")


def test_identidad_ajena_403():
    with pytest.raises(HTTPException) as e:
        auth.exigir_identidad_supervisor({"rol": "supervisor", "sup_id": "01"}, "02")
    assert e.value.status_code == 403


def test_oficina_no_se_restringe():
    auth.exigir_identidad_supervisor({"rol": "oficina"}, "cualquiera")
    auth.exigir_identidad_supervisor({"rol": "admin"}, "otro")
