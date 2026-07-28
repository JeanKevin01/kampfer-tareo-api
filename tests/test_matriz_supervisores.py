"""
Matriz de cumplimiento del reporte por supervisor (funciones puras).

El estado diario ya decía si un supervisor reportó o no. Esto dice QUÉ reportó:
un parte con HH y nada más no es lo mismo que uno con fotos, descripción y las
trabas del día. Encargo de Jean (2026-07-28).
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import main
from core import auth, config
from routers.padron import _dias, _semanas_de, pivotar_reportes

L = date(2026, 7, 20)                       # lunes ISO
SEMANA = [date(2026, 7, 20 + i) for i in range(7)]
SUPS = [{"id": "01", "nombre": "MAMANI CCOPA DAVID"},
        {"id": "02", "nombre": "ARIAS YARI NURIA"}]


def _fila(filas, sid):
    return next(f for f in filas if f["supervisor_id"] == sid)


# ── La cuadrícula ────────────────────────────────────────────
def test_los_dias_son_continuos():
    assert _dias(L, date(2026, 7, 26)) == SEMANA


def test_las_semanas_se_agrupan_para_la_cabecera():
    fechas = _dias(L, date(2026, 8, 2))
    assert _semanas_de(fechas) == [{"lunes": "2026-07-20", "n": 7},
                                   {"lunes": "2026-07-27", "n": 7}]


def test_un_rango_que_arranca_a_media_semana_no_inventa_dias():
    """El bloque de esa semana vale lo que abarca; la cabecera no puede decir 7
    si solo se ven 3 días."""
    assert _semanas_de(_dias(date(2026, 7, 23), date(2026, 7, 26))) == \
        [{"lunes": "2026-07-20", "n": 4}]


# ── Qué reportó cada quien ───────────────────────────────────
def test_solo_hh_no_es_lo_mismo_que_reporte_completo():
    filas = pivotar_reportes(
        SEMANA, SUPS,
        hh=[("01", L, 34.5, 5), ("02", L, 20.0, 3)],
        partes=[("02", L, 1, True, 2)],
        fotos=[("02", L, 3)],
        nc=[])
    solo_hh = _fila(filas, "01")["celdas"]["2026-07-20"]
    completo = _fila(filas, "02")["celdas"]["2026-07-20"]
    assert (solo_hh["hh"], solo_hh["fotos"], solo_hh["desc"], solo_hh["rest"]) == (34.5, 0, False, 0)
    assert (completo["fotos"], completo["desc"], completo["rest"]) == (3, True, 2)


def test_las_hh_del_dia_se_suman_por_supervisor():
    filas = pivotar_reportes(SEMANA, SUPS,
                             hh=[("01", L, 10, 2), ("01", L, 5.5, 1)],
                             partes=[], fotos=[], nc=[])
    assert _fila(filas, "01")["celdas"]["2026-07-20"]["hh"] == 15.5


def test_un_dia_sin_nada_no_deja_celda():
    """La celda existe solo si hubo señal: así el vacío se ve como vacío y no
    como un cero que alguien pudo haber registrado."""
    filas = pivotar_reportes(SEMANA, SUPS, hh=[("01", L, 8, 1)], partes=[], fotos=[], nc=[])
    assert list(_fila(filas, "01")["celdas"]) == ["2026-07-20"]
    assert _fila(filas, "02")["celdas"] == {}


def test_no_se_hizo_se_registra_aunque_no_haya_tareo():
    filas = pivotar_reportes(SEMANA, SUPS, hh=[], partes=[], fotos=[],
                             nc=[("01", date(2026, 7, 22), 2)])
    assert _fila(filas, "01")["celdas"]["2026-07-22"]["nc"] == 2


# ── Totales ──────────────────────────────────────────────────
def test_dias_reportados_cuenta_cualquier_senal():
    """Un parte con fotos y descripción pero sin tareo también es un reporte:
    contar solo las HH castigaría al que sube el sustento y no el tareo."""
    filas = pivotar_reportes(
        SEMANA, SUPS,
        hh=[("01", L, 8, 1)],
        partes=[("01", date(2026, 7, 21), 1, True, 0)],
        fotos=[("01", date(2026, 7, 22), 2)],
        nc=[])
    assert _fila(filas, "01")["tot"]["dias"] == 3


def test_los_totales_suman_la_fila():
    filas = pivotar_reportes(
        SEMANA, SUPS,
        hh=[("01", L, 8, 2), ("01", date(2026, 7, 21), 7.5, 2)],
        partes=[("01", L, 1, True, 1), ("01", date(2026, 7, 21), 1, False, 2)],
        fotos=[("01", L, 4)], nc=[("01", L, 1)])
    tot = _fila(filas, "01")["tot"]
    assert (tot["hh"], tot["partes"], tot["fotos"], tot["rest"], tot["nc"]) == \
        (15.5, 2, 4, 3, 1)


# ── Robustez ─────────────────────────────────────────────────
def test_datos_fuera_del_rango_o_de_otro_supervisor_se_ignoran():
    """Una fila con una fecha que no está en la cuadrícula no puede romper la
    matriz ni colarse en los totales."""
    filas = pivotar_reportes(
        SEMANA, SUPS,
        hh=[("01", date(2026, 8, 10), 99, 9), ("99", L, 50, 5)],
        partes=[], fotos=[], nc=[])
    assert _fila(filas, "01")["tot"]["hh"] == 0
    assert len(filas) == 2


def test_las_filas_salen_ordenadas_por_nombre():
    assert [f["supervisor_id"] for f in
            pivotar_reportes(SEMANA, SUPS, hh=[], partes=[], fotos=[], nc=[])] == ["02", "01"]


# ── Permisos (sin BD: la dependencia corta antes del handler) ──
@pytest.fixture
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol)}


def test_la_matriz_es_de_oficina(_modo_prod):
    assert _modo_prod.get("/admin/supervisores/matriz").status_code == 401
    assert _modo_prod.get("/admin/supervisores/matriz",
                          headers=_hdr("supervisor")).status_code == 403


def test_nombrar_supervisor_es_de_oficina(_modo_prod):
    r = _modo_prod.post("/admin/supervisor/desde-trabajador",
                        json={"trabajador_id": "001"}, headers=_hdr("supervisor"))
    assert r.status_code == 403
