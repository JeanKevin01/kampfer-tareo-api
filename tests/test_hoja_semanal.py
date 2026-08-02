"""
Hoja semanal de HH — _armar_hoja (pura) + roles de los endpoints de edición.
"""
import pytest
from fastapi.testclient import TestClient

from core import auth, config
from routers.ev.hoja import _armar_hoja
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── _armar_hoja (pura) ───────────────────────────────────────
_FECHAS = ["2026-07-27", "2026-07-28", "2026-07-29"]
_JOR = {f: 9.5 for f in _FECHAS}


def _row(id, trab, partida, otm, fecha, hh, sup="01", editado=None, codigo=None):
    return {
        "id": id, "trabajador_id": trab, "partida_id": partida, "otm_id": otm,
        "fecha": fecha, "hh": hh, "supervisor_id": sup,
        "editado_por": editado, "editado_en": None, "motivo_edicion": None,
        "trab_nombre": "TRAB " + trab, "cargo": "OPERARIO",
        "codigo": codigo or f"02.01.{partida}", "partida_desc": "PARTIDA " + str(partida),
        "otm_desc": "PROYECTO " + otm, "sup_nombre": "SUP " + sup,
    }


def test_arbol_otm_partida_persona():
    proyectos, _ = _armar_hoja([
        _row(1, "001", 10, "OTM-1", "2026-07-27", 9.5),
        _row(2, "002", 10, "OTM-1", "2026-07-27", 9.5),
        _row(3, "001", 11, "OTM-1", "2026-07-28", 4.0),
    ], _FECHAS, _JOR)
    assert [p["otm_id"] for p in proyectos] == ["OTM-1"]
    p = proyectos[0]
    assert p["total"] == 23.0
    assert [pa["partida_id"] for pa in p["partidas"]] == [10, 11]
    # la partida 10 tiene dos personas ese día y su subtotal las suma
    assert p["partidas"][0]["celdas"]["2026-07-27"] == 19.0
    assert len(p["partidas"][0]["personas"]) == 2
    # el bloque "personal del proyecto" agrega a la persona a través de partidas
    assert {x["trab_id"]: x["total"] for x in p["personal"]} == {"001": 13.5, "002": 9.5}


def test_exceso_cruza_proyectos_aunque_se_filtre_uno():
    """El caso que justifica la vista: 9.5 en un proyecto y 4 en otro. Filtrando
    OTM-1 el árbol trae solo ese proyecto, pero el total del día debe seguir
    viendo los 13.5 — si no, el exceso que se busca queda invisible."""
    rows = [
        _row(1, "001", 10, "OTM-1", "2026-07-27", 9.5),
        _row(2, "001", 20, "OTM-2", "2026-07-27", 4.0),
    ]
    proyectos, tot = _armar_hoja(rows, _FECHAS, _JOR, otm_filtro="OTM-1")
    assert [p["otm_id"] for p in proyectos] == ["OTM-1"]          # el árbol sí se filtra
    d = tot[("001", "2026-07-27")]
    assert d["hh"] == 13.5 and d["n_otms"] == 2                    # el total NO
    assert d["estado"] == "extra" and d["diff"] == 4.0


def test_estados_ok_bajo_extra_con_tolerancia():
    _, tot = _armar_hoja([
        _row(1, "001", 10, "OTM-1", "2026-07-27", 9.4),   # dentro de la tolerancia
        _row(2, "002", 10, "OTM-1", "2026-07-27", 6.0),
        _row(3, "003", 10, "OTM-1", "2026-07-27", 12.0),
    ], _FECHAS, _JOR)
    assert tot[("001", "2026-07-27")]["estado"] == "ok"
    assert tot[("002", "2026-07-27")]["estado"] == "bajo"
    assert tot[("003", "2026-07-27")]["estado"] == "extra"


def test_celda_con_dos_registros_se_distingue():
    """Duplicado puro: dos supervisores en la MISMA partida y día. La celda suma
    pero conserva las dos líneas para poder borrar la que sobra."""
    proyectos, _ = _armar_hoja([
        _row(1, "001", 10, "OTM-1", "2026-07-27", 9.5, sup="01"),
        _row(2, "001", 10, "OTM-1", "2026-07-27", 9.5, sup="02"),
    ], _FECHAS, _JOR)
    celda = proyectos[0]["partidas"][0]["personas"][0]["celdas"]["2026-07-27"]
    assert celda["hh"] == 19.0 and celda["n"] == 2
    assert [l["supervisor_id"] for l in celda["lineas"]] == ["01", "02"]


def test_marca_de_edicion_llega_a_la_celda():
    proyectos, _ = _armar_hoja([
        _row(1, "001", 10, "OTM-1", "2026-07-27", 6.0, editado="jean"),
    ], _FECHAS, _JOR)
    celda = proyectos[0]["partidas"][0]["personas"][0]["celdas"]["2026-07-27"]
    assert celda["editado"] is True and celda["lineas"][0]["editado_por"] == "jean"


def test_dia_sin_registros_no_crea_celdas():
    proyectos, tot = _armar_hoja([], _FECHAS, _JOR)
    assert proyectos == [] and tot == {}


# ── roles y validaciones ─────────────────────────────────────
def test_edicion_exige_oficina():
    c = _client()
    assert c.post("/ev/tareo-linea", json={}).status_code == 401
    assert c.post("/ev/tareo-linea", json={}, headers=_hdr("supervisor")).status_code == 403
    assert c.patch("/ev/tareo-linea/1", json={}, headers=_hdr("supervisor")).status_code == 403
    assert c.delete("/ev/tareo-linea/1", headers=_hdr("supervisor")).status_code == 403


def test_hoja_es_de_oficina():
    c = _client()
    assert c.get("/ev/hoja-semanal?lunes=2026-07-27").status_code == 401
    assert c.get("/ev/hoja-semanal?lunes=2026-07-27",
                 headers=_hdr("supervisor")).status_code == 403


def test_lunes_invalido_es_400():
    r = _client().get("/ev/hoja-semanal?lunes=noesunafecha", headers=_hdr("oficina"))
    assert r.status_code == 400


def test_hh_fuera_de_rango_es_400():
    c = _client()
    for hh in (-1, 25):
        r = c.post("/ev/tareo-linea",
                   json={"trabajador_id": "001", "partida_id": 1,
                         "fecha": "2026-07-27", "hh": hh},
                   headers=_hdr("oficina"))
        assert r.status_code == 400, hh
