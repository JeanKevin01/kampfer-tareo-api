"""Sustento de valorización en PDF (fpdf2) + auth del endpoint ZIP.

Tests puros (sin BD): el generador arma PDFs válidos con datos sintéticos y la
sanitización latin-1 no rompe con viñetas/emojis. Los 401/403 usan TestClient.
"""
import pytest
from fastapi.testclient import TestClient

from core import auth, config
from core.pdf_partida import _fmt, _latin1, pdf_sustento_partida
from routers.programacion import _nombre_pdf
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


# ── Sanitización latin-1 (fuentes núcleo) ────────────────────
def test_latin1_mapea_puntuacion_y_descarta_emoji():
    assert _latin1("a — b") == "a - b"
    assert _latin1("• punto") == "· punto"
    assert _latin1("★ ✓ →") == "* OK ->"
    # Acentos castellanos SÍ están en latin-1: deben conservarse
    assert _latin1("Compactación ñ á é í ó ú") == "Compactación ñ á é í ó ú"
    # Los emojis (fuera de latin-1) no revientan: se reemplazan
    assert "?" in _latin1("obra 😀 lista")


def test_fmt_estilo_es_pe():
    assert _fmt(1234.5, 1) == "1.234,5"
    assert _fmt(0, 2) == "0,00"
    assert _fmt(None, 2) == "0,00"


# ── Nombre de archivo del ZIP: seguro y único ────────────────
def test_nombre_pdf_seguro_y_unico():
    usados: set = set()
    n1 = _nombre_pdf({"id": 1, "codigo": "02.01.01", "descripcion": "Relleno / zona norte"}, usados)
    n2 = _nombre_pdf({"id": 2, "codigo": "02.01.01", "descripcion": "Relleno / zona norte"}, usados)
    assert n1.endswith(".pdf") and "/" not in n1 and "\\" not in n1
    assert n1 != n2                      # colisión resuelta con sufijo
    vacio = _nombre_pdf({"id": 9, "codigo": "", "descripcion": ""}, usados)
    assert vacio.endswith(".pdf") and "/" not in vacio      # sin código → nombre seguro igual


# ── Generación del PDF ───────────────────────────────────────
def _bloque(con_reportes=True, sin_tareo=False):
    reps = []
    if con_reportes:
        reps = [{
            "id": 10, "fecha": "2026-07-07", "actividad": "Relleno con material propio",
            "supervisor": "Juan Pérez", "hh_dia": 48.0,
            "texto": "Fecha: 07/07\nResponsable: Juan Pérez\n* Relleno de zanja\n* Compactación",
            # Foto marcada como purgada + otra inexistente en disco → placeholders
            "fotos": [{"ruta": None, "ruta_thumb": None, "purgada": True,
                       "ancho": 4000, "alto": 3000, "bytes": 0},
                      {"ruta": "reportes/x/inexistente.jpg", "ruta_thumb": None,
                       "purgada": False, "ancho": 3000, "alto": 4000, "bytes": 1}],
        }]
    return {
        "partida": {"id": 1, "codigo": "02.01.01.01",
                    "descripcion": "Relleno y compactación — zona norte", "unidad": "m3",
                    "otm_id": "OTM-0003", "otm_desc": "Movimiento de tierras",
                    "metrado_presup": 1250.0, "metrado_ejec": 812.5, "avance": 0.65,
                    "hh_presup": 480.0, "hh_gastadas": 512.0, "hh_rango": 120.0,
                    "sin_tareo": sin_tareo},
        "reportes": reps,
    }


def test_pdf_es_valido_con_reportes():
    pdf = pdf_sustento_partida(_bloque(), "Periodo: 2026-07-01 — 2026-07-31")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_pdf_sin_reportes_y_sin_tareo():
    pdf = pdf_sustento_partida(_bloque(con_reportes=False, sin_tareo=True),
                               "Todo el historial registrado")
    assert pdf[:5] == b"%PDF-"


# ── Auth del endpoint ZIP (oficina) ──────────────────────────
def test_zip_sin_credenciales_401():
    assert _client().get("/ev/programacion/reporte-partida.zip?partidas=1").status_code == 401


def test_zip_supervisor_403():
    r = _client().get("/ev/programacion/reporte-partida.zip?partidas=1",
                      headers=_hdr("supervisor", "01"))
    assert r.status_code == 403
