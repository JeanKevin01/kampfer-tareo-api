"""Cuadrillas del supervisor (0046) — normalización, validación e identidad.

El bug que originó este módulo no era de lógica: el panel escribía en la tabla
`cuadrillas` y el tareo leía `cuadrilla_otm`, dos circuitos que nunca se
cruzaban. Ningún test unitario podía cazar eso —cada mitad funcionaba— así que
la prueba que de verdad protege es el bloque F-CUADRILLA del E2E: guardar por
el endpoint del PANEL y leer por el del CAMPO. Aquí quedan las piezas que sí se
pueden verificar sin BD.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import auth, config
from core.ids import ids_unicos, norm_trab_id
from routers.tareo import _nombre_cuadrilla
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str, sup_id: str = None):
    extra = {"sup_id": sup_id} if sup_id else None
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol, extra=extra)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── Normalización de ids ─────────────────────────────────────
# El panel guardaba '007' (zfill) y el teléfono '7' (str a secas): la misma
# persona en dos filas distintas de la misma cuadrilla.
def test_id_corto_se_rellena():
    assert norm_trab_id("7") == "007"


def test_id_largo_no_se_toca():
    assert norm_trab_id("1234567") == "1234567"


def test_id_con_espacios():
    assert norm_trab_id("  12 ") == "012"


def test_vacio_no_se_convierte_en_000():
    """'' .zfill(3) da '000', que es el id legítimo de OTRA persona."""
    assert norm_trab_id("") == ""
    assert norm_trab_id(None) == ""


def test_ids_unicos_conserva_el_orden():
    """El orden ES el dato: se guarda como `orden` y es el que ve el supervisor."""
    assert ids_unicos(["3", "1", "2"]) == ["003", "001", "002"]


def test_ids_unicos_deduplica_tras_normalizar():
    assert ids_unicos(["7", "007", "  7"]) == ["007"]


def test_ids_unicos_descarta_vacios():
    assert ids_unicos(["1", "", None, "2"]) == ["001", "002"]


def test_ids_unicos_tolera_none():
    assert ids_unicos(None) == []


# ── Nombre de la cuadrilla ───────────────────────────────────
def test_nombre_colapsa_espacios():
    assert _nombre_cuadrilla("  Cuadrilla   de   excavación ") == "Cuadrilla de excavación"


def test_nombre_se_recorta_a_100():
    assert len(_nombre_cuadrilla("x" * 300)) == 100


def test_nombre_vacio_422():
    for v in ("", "   ", None):
        with pytest.raises(HTTPException) as e:
            _nombre_cuadrilla(v)
        assert e.value.status_code == 422


# ── Identidad: nadie toca la cuadrilla de otro ───────────────
def test_crear_cuadrilla_ajena_403():
    r = _client().post("/api/cuadrillas/02", json={"nombre": "X"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_guardar_plantilla_ajena_403():
    """El agujero que tenía el endpoint de campo: el supervisor_id venía en el
    cuerpo y nadie comprobaba que fuera el del token."""
    r = _client().post("/ev/cuadrillas-plantilla",
                       json={"supervisor_id": "02", "nombre": "X", "trabajadores": []},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_guardar_plantilla_propia_pasa():
    r = _client().post("/ev/cuadrillas-plantilla",
                       json={"supervisor_id": "01", "nombre": "X", "trabajadores": []},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code not in (401, 403)


def test_oficina_gestiona_cualquier_cuadrilla():
    r = _client().post("/api/cuadrillas/02", json={"nombre": "X"}, headers=_hdr("oficina"))
    assert r.status_code not in (401, 403)
