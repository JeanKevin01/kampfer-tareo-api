"""
Libro mayor de fiabilidad — _resumen/_celda/_percentil (puros) + roles.
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from core import auth, config
from routers.fiabilidad import (_celda, _percentil, _resumen, _norm, _validar_lote,
                                MAX_LOTE, N_MINIMO)
import main

HOY = date(2026, 8, 2)


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


def _f(tipo="MATERIALES", resp=(1, "LOGISTICA"), liberada=True, req=date(2026, 7, 1),
       latencia=3, derivada=False, otm="OTM-0001", duracion=None, antiguedad=None):
    return {"tipo": tipo, "responsable_id": resp[0], "responsable": resp[1],
            "liberada": liberada, "fecha_requerida": req,
            "latencia": latencia, "derivada": derivada, "otm_id": otm,
            "duracion": duracion, "antiguedad": antiguedad}


# ── percentil ────────────────────────────────────────────────
def test_percentil_interpola_y_aguanta_los_bordes():
    assert _percentil([], 0.5) is None
    assert _percentil([7], 0.5) == 7.0
    assert _percentil([1, 2, 3, 4], 0.5) == 2.5
    # Un decimal: son días, y 3.25 días no significa nada más que 3.2.
    assert _percentil([1, 2, 3, 4], 0.75) == 3.2


# ── celda ────────────────────────────────────────────────────
def test_celda_separa_pendientes_vencidas_y_medidas():
    filas = [
        _f(latencia=2),
        _f(latencia=10),
        _f(liberada=False, latencia=None, req=date(2026, 7, 20)),   # vencida
        _f(liberada=False, latencia=None, req=date(2026, 9, 1)),    # pendiente en plazo
    ]
    c = _celda(filas, HOY)
    assert c["n"] == 4 and c["n_liberadas"] == 2 and c["n_pendientes"] == 2
    assert c["n_vencidas"] == 1
    assert c["n_medidas"] == 2 and c["mediana_dias"] == 6.0 and c["peor_dias"] == 10


def test_a_tiempo_cuenta_la_latencia_no_positiva():
    """Liberar el mismo día que se pedía es cumplir, no incumplir por poco."""
    c = _celda([_f(latencia=0), _f(latencia=-2), _f(latencia=5)], HOY)
    assert c["pct_a_tiempo"] == round(100 * 2 / 3, 1)


def test_derivadas_se_cuentan_aparte_sin_salir_del_total():
    c = _celda([_f(latencia=3, derivada=True), _f(latencia=5)], HOY)
    assert c["n_medidas"] == 2 and c["n_derivadas"] == 1
    assert c["mediana_dias"] == 4.0


def test_suficiente_marca_el_minimo_pero_devuelve_el_numero_igual():
    """Con pocas observaciones la mediana se devuelve marcada, no se oculta:
    esconderla dejaría al planner sin nada."""
    pocas = _celda([_f(latencia=3)] * (N_MINIMO - 1), HOY)
    assert pocas["suficiente"] is False and pocas["mediana_dias"] == 3.0
    muchas = _celda([_f(latencia=3)] * N_MINIMO, HOY)
    assert muchas["suficiente"] is True


def test_celda_sin_liberadas_no_inventa_mediana():
    c = _celda([_f(liberada=False, latencia=None)], HOY)
    assert c["mediana_dias"] is None and c["pct_a_tiempo"] is None


# ── duración y antigüedad (0045) ─────────────────────────────
def test_duracion_es_distinta_de_la_latencia():
    """El caso que justifica la columna: liberada A TIEMPO respecto de lo que
    pidió el planner (latencia 0) pero después de bloquear 40 días."""
    c = _celda([_f(latencia=0, duracion=40), _f(latencia=0, duracion=20)], HOY)
    assert c["pct_a_tiempo"] == 100.0          # impecable según el plazo pedido
    assert c["duracion_mediana"] == 30.0       # y aun así vivió un mes
    assert c["duracion_peor"] == 40 and c["n_duracion"] == 2


def test_la_duracion_solo_cuenta_lo_cerrado_y_la_antiguedad_solo_lo_abierto():
    """Si se mezclaran, cerrar una restricción vieja «mejoraría» la métrica
    exactamente igual que dejarla abierta."""
    c = _celda([_f(duracion=10), _f(liberada=False, latencia=None, antiguedad=90)], HOY)
    assert c["n_duracion"] == 1 and c["duracion_mediana"] == 10.0
    assert c["antiguedad_peor"] == 90


def test_sin_fechas_no_se_inventan_duraciones():
    c = _celda([_f(duracion=None), _f(liberada=False, latencia=None, antiguedad=None)], HOY)
    assert c["duracion_mediana"] is None and c["duracion_peor"] is None
    assert c["antiguedad_peor"] is None and c["n_duracion"] == 0


# ── resumen ──────────────────────────────────────────────────
def test_resumen_agrupa_por_tipo_y_responsable():
    filas = [
        _f(tipo="MATERIALES", resp=(1, "LOGISTICA"), latencia=9),
        _f(tipo="MATERIALES", resp=(1, "LOGISTICA"), latencia=11),
        _f(tipo="INFORMACION", resp=(2, "INGENIERIA"), latencia=1),
    ]
    r = _resumen(filas, HOY)
    assert {t["tipo"] for t in r["por_tipo"]} == {"MATERIALES", "INFORMACION"}
    mat = next(t for t in r["por_tipo"] if t["tipo"] == "MATERIALES")
    assert mat["n"] == 2 and mat["mediana_dias"] == 10.0
    log = next(p for p in r["por_responsable"] if p["responsable"] == "LOGISTICA")
    assert log["n"] == 2


def test_reincidencia_solo_lista_lo_repetido_y_ordena_por_frecuencia():
    """Es el dato que sirve desde la primera semana: «tercera vez con la misma
    causa y el mismo responsable» es un conteo, no una distribución."""
    filas = ([_f(tipo="MATERIALES", resp=(1, "LOGISTICA"))] * 3
             + [_f(tipo="EQUIPOS", resp=(2, "MANTENIMIENTO"))] * 2
             + [_f(tipo="CLIMA", resp=(3, "NADIE"))])
    r = _resumen(filas, HOY)
    assert [(c["tipo"], c["n"]) for c in r["reincidencia"]] == [
        ("MATERIALES", 3), ("EQUIPOS", 2)]        # el caso único no aparece


def test_resumen_separa_por_proyecto():
    """Con varias OTM abiertas, una mediana global promedia obras que no se
    parecen: «no llegó el fierro» no significa lo mismo en cada una."""
    r = _resumen([_f(otm="OTM-0001", latencia=2), _f(otm="OTM-0001", latencia=4),
                  _f(otm="OTM-0009", latencia=30)], HOY)
    por = {o["otm_id"]: o for o in r["por_otm"]}
    assert por["OTM-0001"]["n"] == 2 and por["OTM-0001"]["mediana_dias"] == 3.0
    assert por["OTM-0009"]["mediana_dias"] == 30.0


def test_restriccion_sin_proyecto_no_desaparece_del_corte():
    r = _resumen([_f(otm=None)], HOY)
    assert [o["otm_id"] for o in r["por_otm"]] == ["(sin proyecto)"]


def test_resumen_vacio_no_revienta():
    r = _resumen([], HOY)
    assert r["total"]["n"] == 0 and r["por_tipo"] == [] and r["reincidencia"] == []
    assert r["por_otm"] == []


# ── normalización del nombre ─────────────────────────────────
def test_norm_agrupa_lo_que_el_texto_libre_separaba():
    # La tilde es el caso que de verdad importa: sin quitarla, «LOGÍSTICA» y
    # «LOGISTICA» pasan el UNIQUE como áreas distintas y el GROUP BY las cuenta
    # por separado — el duplicado que el catálogo viene a cerrar.
    assert _norm(" logística ") == _norm("LOGÍSTICA") == _norm("Logistica") == "LOGISTICA"
    assert _norm("almacén   central") == "ALMACEN CENTRAL"
    assert _norm("diseño") == "DISENO"
    assert _norm(None) == ""


# ── lote de liberación ───────────────────────────────────────
def test_lote_toma_la_fecha_de_cada_item_y_completa_con_hoy():
    """Cada restricción lleva SU fecha: el viernes se declara que una se liberó
    el martes y otra el jueves, que es justo el caso que hace falta cubrir."""
    assert _validar_lote(
        [{"id": 7, "liberada_el": "2026-07-28"}, {"id": "3"}], HOY) == [
        (3, HOY), (7, date(2026, 7, 28))]


def test_lote_rechaza_la_fecha_futura():
    """Un 2027 tecleado por error no se ve en la lista pero deja la mediana del
    responsable inservible para siempre."""
    with pytest.raises(Exception) as e:
        _validar_lote([{"id": 1, "liberada_el": "2027-01-01"}], HOY)
    assert e.value.status_code == 422


def test_lote_vacio_o_gigante_no_pasa():
    for malo in (None, [], "3", [{"id": 1}] * (MAX_LOTE + 1)):
        with pytest.raises(Exception):
            _validar_lote(malo, HOY)


def test_lote_deduplica_por_id():
    """Doble clic en la misma fila no debe intentar liberarla dos veces; manda
    la última fecha elegida."""
    assert _validar_lote(
        [{"id": 5, "liberada_el": "2026-07-01"}, {"id": 5, "liberada_el": "2026-07-30"}],
        HOY) == [(5, date(2026, 7, 30))]


def test_lote_con_id_no_numerico_es_422():
    with pytest.raises(Exception) as e:
        _validar_lote([{"id": "abc"}], HOY)
    assert e.value.status_code == 422


# ── roles ────────────────────────────────────────────────────
def test_catalogo_y_libro_son_de_oficina():
    c = _client()
    assert c.get("/ev/responsables").status_code == 401
    assert c.get("/ev/responsables", headers=_hdr("supervisor")).status_code == 403
    assert c.post("/ev/responsables", json={"nombre": "X"},
                  headers=_hdr("supervisor")).status_code == 403
    assert c.get("/ev/fiabilidad/restricciones",
                 headers=_hdr("supervisor")).status_code == 403
    assert c.get("/ev/fiabilidad/pendientes",
                 headers=_hdr("supervisor")).status_code == 403
    assert c.post("/ev/fiabilidad/liberar", json={"items": [{"id": 1}]},
                  headers=_hdr("supervisor")).status_code == 403


def test_tipo_de_responsable_invalido_es_422():
    r = _client().post("/ev/responsables", json={"nombre": "LOGISTICA", "tipo": "MARCIANO"},
                       headers=_hdr("oficina"))
    assert r.status_code == 422


def test_nombre_vacio_es_400():
    r = _client().post("/ev/responsables", json={"nombre": "   "}, headers=_hdr("oficina"))
    assert r.status_code == 400


# ── El día de Lima de un sello UTC (bug destapado por el E2E) ─────────
def test_fecha_de_devuelve_el_dia_de_lima_no_el_de_utc():
    """Postgres devuelve los TIMESTAMPTZ en UTC. Una restricción registrada a las
    20:26 de Lima llega como 01:26 del día SIGUIENTE, y `.date()` a secas corría
    la medición un día entero — la antigüedad salía en -1."""
    from datetime import datetime, timezone
    from core.tiempo import fecha_de
    sello = datetime(2026, 8, 3, 1, 26, tzinfo=timezone.utc)   # = 2-ago 20:26 Lima
    assert fecha_de(sello) == date(2026, 8, 2)
    assert fecha_de(datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)) == date(2026, 8, 2)
    assert fecha_de(None) is None
