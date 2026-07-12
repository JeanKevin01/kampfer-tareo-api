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


# ── Lookahead-grid / metrado diario (vista Excel del ex-gerente) ──
def test_lookahead_grid_supervisor_403():
    r = _client().get("/ev/programacion/lookahead-grid", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_metrado_dias_sin_dias_400():
    r = _client().put("/ev/programacion/actividades/1/metrado-dias",
                      json={}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_metrado_dias_fecha_invalida_400():
    r = _client().put("/ev/programacion/actividades/1/metrado-dias",
                      json={"dias": {"no-es-fecha": 5}}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_metrado_dias_cantidad_negativa_400():
    r = _client().put("/ev/programacion/actividades/1/metrado-dias",
                      json={"dias": {"2026-07-13": -1}}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_avance_dia_sin_partida_400():
    r = _client().post("/ev/programacion/avance-dia",
                       json={"fecha": "2026-07-13", "cantidad": 5}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_avance_dia_supervisor_403():
    r = _client().post("/ev/programacion/avance-dia",
                       json={"partida_id": 1, "fecha": "2026-07-13", "cantidad": 5},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_lote_supervisor_403():
    r = _client().post("/ev/programacion/actividades-lote",
                       json={"otm_id": "OTM-X", "items": [{"partida_id": 1, "fecha": "2026-07-13"}]},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_lote_sin_items_400():
    r = _client().post("/ev/programacion/actividades-lote",
                       json={"otm_id": "OTM-X", "items": []}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_lote_item_sin_fecha_400():
    r = _client().post("/ev/programacion/actividades-lote",
                       json={"otm_id": "OTM-X", "items": [{"partida_id": 1}]},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


def test_lote_rango_invertido_400():
    r = _client().post("/ev/programacion/actividades-lote",
                       json={"otm_id": "OTM-X", "items": [
                           {"partida_id": 1, "fecha": "2026-07-15", "fecha_fin": "2026-07-13"}]},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


def test_crear_actividad_rango_invertido_400():
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-15", "fecha_fin": "2026-07-13", "titulo": "x"},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


def test_crear_actividad_metrado_negativo_400():
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-13", "titulo": "x", "metrado_prog": -3},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


def _dias(*ss):
    from datetime import date
    return [date.fromisoformat(s) for s in ss]


def test_distribuir_uniforme_suma_exacta():
    from routers.programacion import _distribuir
    d = _distribuir(100.0, _dias("2026-07-13", "2026-07-14", "2026-07-15"))
    assert len(d) == 3
    assert d[_dias("2026-07-13")[0]] == 33.333
    assert d[_dias("2026-07-15")[0]] == 33.334     # el último día absorbe el redondeo
    assert round(sum(d.values()), 3) == 100.0


def test_distribuir_un_dia():
    from routers.programacion import _distribuir
    d = _distribuir(87.593, _dias("2026-07-13"))
    assert d == {_dias("2026-07-13")[0]: 87.593}


def test_dias_habiles_salta_domingo_feriado_y_salto():
    # Lun 13 a Dom 19: sin domingo (calendario L-S), feriado el 15, salto el 14
    from datetime import date
    from routers.programacion import _dias_habiles
    h = _dias_habiles(date(2026, 7, 13), date(2026, 7, 19),
                      dias_semana={1, 2, 3, 4, 5, 6},
                      feriados={date(2026, 7, 15)},
                      saltos={date(2026, 7, 14)})
    assert h == _dias("2026-07-13", "2026-07-16", "2026-07-17", "2026-07-18")


def test_dias_habiles_todo_bloqueado():
    from datetime import date
    from routers.programacion import _dias_habiles
    assert _dias_habiles(date(2026, 7, 12), date(2026, 7, 12),
                         dias_semana={1, 2, 3, 4, 5, 6}, feriados=set(), saltos=set()) == []


def test_config_dias_semana_invalidos_400():
    r = _client().put("/ev/programacion/config", json={"dias_semana": [0, 8]},
                      headers=_hdr("oficina"))
    assert r.status_code == 400


def test_config_supervisor_403():
    r = _client().put("/ev/programacion/config", json={"dias_semana": [1, 2]},
                      headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_feriado_sin_fecha_400():
    r = _client().post("/ev/programacion/feriados", json={"motivo": "x"},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


def test_avance_actividad_sin_fecha_400():
    r = _client().post("/ev/programacion/actividades/1/avance-dia",
                       json={"cantidad": 5}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_avance_actividad_supervisor_403():
    r = _client().post("/ev/programacion/actividades/1/avance-dia",
                       json={"fecha": "2026-07-13", "cantidad": 5},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_dias_salto_invalidos_400():
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-13", "titulo": "x", "dias_salto": ["no-fecha"]},
                       headers=_hdr("oficina"))
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
