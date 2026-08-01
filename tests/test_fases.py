"""
Catálogo de fases (mejoras UX pre-F4) — roles y normalización.

Los 401/403 se prueban con TestClient sin BD (la dependencia corta antes del
handler); la normalización del código es función pura.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import auth, config
from routers.fases import _norm_codigo
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── Roles ────────────────────────────────────────────────────
def test_fases_sin_credenciales_401():
    assert _client().get("/ev/fases").status_code == 401


def test_supervisor_no_lista_fases():
    assert _client().get("/ev/fases", headers=_hdr("supervisor")).status_code == 403


def test_supervisor_no_crea_fase():
    r = _client().post("/ev/fases", json={"codigo": "X", "nombre": "X"},
                       headers=_hdr("supervisor"))
    assert r.status_code == 403


def test_oficina_pasa_fases():
    r = _client().get("/ev/fases", headers=_hdr("oficina"))
    assert r.status_code not in (401, 403)


def test_plantilla_pu_supervisor_403():
    r = _client().get("/ev/presupuesto/plantilla-pu", headers=_hdr("supervisor"))
    assert r.status_code == 403


def test_plantilla_pu_oficina_descarga_xlsx():
    """La ruta se conserva, pero ya no sirve el .xls estático —que era el fixture
    de los tests, con datos «Excavacion test»— sino la plantilla .xlsx generada,
    con formato e instrucciones (2026-08-01)."""
    r = _client().get("/ev/presupuesto/plantilla-pu", headers=_hdr("oficina"))
    assert r.status_code == 200
    assert r.content[:2] == b"PK"                 # .xlsx es un zip
    assert ".xlsx" in r.headers.get("content-disposition", "")


# ── Normalización (pura) ─────────────────────────────────────
def test_norm_codigo_upper_y_trim():
    assert _norm_codigo("  fab ") == "FAB"
    assert _norm_codigo("11") == "11"


def test_norm_codigo_invalido():
    for malo in ("", "   ", None, "X" * 21):
        with pytest.raises(HTTPException) as e:
            _norm_codigo(malo)
        assert e.value.status_code == 400
