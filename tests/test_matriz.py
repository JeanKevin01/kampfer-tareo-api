"""
Matriz histórica — _pivotear (pura) + roles y validaciones del endpoint.
"""
import pytest
from fastapi.testclient import TestClient

from core import auth, config
from routers.ev.matriz import _pivotear
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── _pivotear (pura) ─────────────────────────────────────────
_FECHAS = ["2026-06-01", "2026-06-02", "2026-06-03"]


def _row(id, etiqueta, fecha, valor, grupo=None):
    return {"id": id, "etiqueta": etiqueta, "grupo": grupo, "fecha": fecha, "valor": valor}


def test_pivotear_sparse_y_totales():
    filas, tot_col, _ = _pivotear([
        _row(1, "P1", "2026-06-01", 8), _row(1, "P1", "2026-06-03", 4.5),
        _row(2, "P2", "2026-06-01", 2),
    ], _FECHAS)
    f1 = next(f for f in filas if f["id"] == "1")
    assert f1["celdas"] == {"2026-06-01": 8.0, "2026-06-03": 4.5}
    assert "2026-06-02" not in f1["celdas"]          # sparse: sin ceros
    assert f1["total"] == 12.5
    assert tot_col == {"2026-06-01": 10.0, "2026-06-03": 4.5}


def test_pivotear_orden_por_grupo_y_etiqueta():
    filas, _, _ = _pivotear([
        _row(1, "Z", "2026-06-01", 1, grupo="B"),
        _row(2, "A", "2026-06-01", 1, grupo="B"),
        _row(3, "M", "2026-06-01", 1, grupo="A"),
        _row(4, "SinGrupo", "2026-06-01", 1),
    ], _FECHAS)
    assert [f["etiqueta"] for f in filas] == ["M", "A", "Z", "SinGrupo"]


def test_pivotear_max_celda_p95_ignora_outlier():
    rows = [_row(i, f"P{i}", "2026-06-01", 10) for i in range(20)]
    rows.append(_row(99, "OUT", "2026-06-02", 1000))
    _, _, mx = _pivotear(rows, _FECHAS)
    assert mx == 10                                   # el outlier no aplana la escala


def test_pivotear_vacio():
    filas, tot_col, mx = _pivotear([], _FECHAS)
    assert filas == [] and tot_col == {} and mx == 0.0


# ── Endpoint: roles y validaciones ───────────────────────────
def test_matriz_sin_credenciales_401():
    assert _client().get("/ev/matriz").status_code == 401


def test_matriz_supervisor_403():
    assert _client().get("/ev/matriz", headers=_hdr("supervisor")).status_code == 403


def test_matriz_modo_invalido_400():
    r = _client().get("/ev/matriz?modo=cuadrillas", headers=_hdr("oficina"))
    assert r.status_code == 400


def test_matriz_cantidad_solo_partidas_400():
    r = _client().get("/ev/matriz?modo=trabajadores&celda=cantidad", headers=_hdr("oficina"))
    assert r.status_code == 400


def test_matriz_rango_invertido_400():
    r = _client().get("/ev/matriz?desde=2026-07-01&hasta=2026-06-01", headers=_hdr("oficina"))
    assert r.status_code == 400


def test_matriz_rango_excesivo_400():
    r = _client().get("/ev/matriz?desde=2024-01-01&hasta=2026-01-01", headers=_hdr("oficina"))
    assert r.status_code == 400
