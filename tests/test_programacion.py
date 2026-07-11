"""
Calendario de programación y reportes de campo — matriz de roles (sin BD).
"""
import pytest
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
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── Oficina ──────────────────────────────────────────────────
def test_programacion_sin_credenciales_401():
    assert _client().get("/ev/programacion/semana").status_code == 401


def test_supervisor_no_ve_calendario():
    r = _client().get("/ev/programacion/semana", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_supervisor_no_purga():
    r = _client().post("/ev/programacion/purgar", json={"semana_iso": "2026-W01"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_oficina_pasa_calendario():
    r = _client().get("/ev/programacion/semana", headers=_hdr("oficina"))
    assert r.status_code not in (401, 403)


def test_estado_invalido_422():
    r = _client().put("/ev/programacion/actividades/1", json={"estado": "TERMINADO"},
                      headers=_hdr("oficina"))
    assert r.status_code == 422


def test_crear_actividad_sin_titulo_400():
    r = _client().post("/ev/programacion/actividades", json={"fecha": "2026-07-06"},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


# ── Campo ────────────────────────────────────────────────────
def test_reporte_suplantado_403():
    r = _client().post("/campo/reportes",
                       data={"otm_id": "OTM-X", "supervisor_id": "02", "descripcion": "x"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_reporte_campo_pasa_autorizacion():
    # Con su propia identidad la autorización NO corta (sin BD el handler dará 500).
    r = _client().post("/campo/reportes",
                       data={"otm_id": "OTM-X", "supervisor_id": "01", "descripcion": "x"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code not in (401, 403)


def test_reporte_vacio_400():
    r = _client().post("/campo/reportes",
                       data={"otm_id": "OTM-X", "supervisor_id": "01", "descripcion": "  "},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 400


def test_mis_actividades_suplantado_403():
    r = _client().get("/campo/mis-actividades?supervisor_id=02",
                      headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_no_cumplida_suplantado_403():
    r = _client().post("/campo/actividades/1/no-cumplida",
                       json={"supervisor_id": "02", "causa": "lluvia"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_no_cumplida_sin_causa_400():
    r = _client().post("/campo/actividades/1/no-cumplida",
                       json={"supervisor_id": "01", "causa": "  "},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 400


def test_no_cumplida_categoria_invalida_422():
    r = _client().post("/campo/actividades/1/no-cumplida",
                       json={"supervisor_id": "01", "causa_cat": "FLOJERA"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 422


# ── Last Planner: lookahead / restricciones / PPC ────────────
def test_lookahead_supervisor_403():
    r = _client().get("/ev/programacion/lookahead", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_ppc_supervisor_403():
    r = _client().get("/ev/programacion/ppc", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_restriccion_supervisor_no_crea_403():
    r = _client().post("/ev/programacion/actividades/1/restricciones",
                       json={"descripcion": "x"}, headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_restriccion_tipo_invalido_422():
    r = _client().post("/ev/programacion/actividades/1/restricciones",
                       json={"descripcion": "acero", "tipo": "MAGIA"},
                       headers=_hdr("oficina"))
    assert r.status_code == 422


def test_restriccion_sin_descripcion_400():
    r = _client().post("/ev/programacion/actividades/1/restricciones",
                       json={"tipo": "MATERIALES"}, headers=_hdr("oficina"))
    assert r.status_code == 400


# ── Media firmada ────────────────────────────────────────────
def test_media_sin_firma_403():
    # /media/* no exige token (la firma es la credencial) pero la firma inválida corta.
    r = _client().get("/media/reportes/2026-W01/x.jpg")
    assert r.status_code == 403


def test_media_path_traversal_403():
    # El '..' plano lo normaliza el propio Starlette antes del routing; la
    # variante percent-encoded SÍ llega al handler y el confinamiento la corta.
    from core.media import _firma
    import time
    exp = int(time.time()) + 900
    sig = _firma("../main.py", exp)
    r = _client().get(f"/media/%2E%2E%2Fmain.py?exp={exp}&sig={sig}")
    assert r.status_code == 403
