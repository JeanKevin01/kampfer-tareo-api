"""
Plazo (duración) como dato de primera clase y tipos de vínculo FS/SS/FF — 0034.

Todo lo de este archivo es PURO (sin BD) salvo los checks de validación de los
endpoints, que usan TestClient. Calendario de referencia: se trabaja de lunes a
sábado (domingo NO laborable), sin feriados salvo que el test los ponga.

Fechas de referencia (2026): lun 13, mar 14, mié 15, jue 16, vie 17, sáb 18,
dom 19 (NO laborable), lun 20, mar 21, mié 22.
"""
from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import auth, config
import main
from routers.programacion import (
    _fin_desde_plazo, _habil_desplazado, _inicio_desde_plazo, _parse_plazo,
    _plazo_de, _resolver_fechas, _restriccion_dep, _siguiente_habil,
)

_LS = {1, 2, 3, 4, 5, 6}


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str, sup_id: str = None):
    extra = {"sup_id": sup_id} if sup_id else None
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol, extra=extra)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── El plazo: Σ de los pesos de los días hábiles ─────────────
def test_plazo_de_suma_pesos_con_medio_y_salto():
    # Lun 13 → Sáb 18: 6 hábiles; el 15 es salto (pesa 0) y el 16 medio (0.5)
    p = _plazo_de(date(2026, 7, 13), date(2026, 7, 18), _LS, set(),
                  saltos={date(2026, 7, 15)}, medios={date(2026, 7, 16)})
    assert p == 4.5


def test_fin_desde_plazo_entero_salta_el_domingo():
    # 6 días hábiles desde el jueves 16: J V S — (dom no) — L M X
    fin, medios = _fin_desde_plazo(date(2026, 7, 16), 6.0, _LS, set(), set(), set())
    assert fin == date(2026, 7, 22)
    assert medios == []


def test_fin_desde_plazo_medio_dia_marca_el_ultimo():
    # Plazo 1.5 desde el lunes: lunes completo + martes a media jornada.
    # Esto es lo que el planner pidió y antes no era programable.
    fin, medios = _fin_desde_plazo(date(2026, 7, 13), 1.5, _LS, set(), set(), set())
    assert fin == date(2026, 7, 14)
    assert medios == [date(2026, 7, 14)]


def test_fin_desde_plazo_respeta_el_medio_que_puso_el_planner():
    # El planner ya marcó el LUNES como medio: 2 días de plazo llegan al
    # miércoles a media jornada (0.5 + 1 + 0.5), no al martes.
    fin, medios = _fin_desde_plazo(date(2026, 7, 13), 2.0, _LS, set(), set(),
                                  medios={date(2026, 7, 13)})
    assert fin == date(2026, 7, 15)
    assert medios == [date(2026, 7, 13), date(2026, 7, 15)]


def test_fin_desde_plazo_salta_los_dias_de_salto():
    fin, _ = _fin_desde_plazo(date(2026, 7, 13), 3.0, _LS, set(),
                              saltos={date(2026, 7, 14)}, medios=set())
    assert fin == date(2026, 7, 16)          # L, (mar salto), X, J


def test_plazo_ida_y_vuelta_es_invariante():
    """La invariante del modelo: si de un inicio + plazo sale un fin, ese rango
    vuelve a medir el mismo plazo. Sin esto la cascada deformaría las
    actividades cada vez que las empuja."""
    feriados = {date(2026, 7, 17)}
    for plazo in (0.5, 1.0, 1.5, 3.0, 7.5, 12.0):
        fin, medios = _fin_desde_plazo(date(2026, 7, 13), plazo, _LS, feriados, set(), set())
        assert _plazo_de(date(2026, 7, 13), fin, _LS, feriados, set(), set(medios)) == plazo


def test_inicio_desde_plazo_es_el_espejo_del_fin():
    # 3 días hábiles que terminan el lunes 20 → L 20, S 18, V 17 (el dom no cuenta)
    ini, medios = _inicio_desde_plazo(date(2026, 7, 20), 3.0, _LS, set(), set(), set())
    assert ini == date(2026, 7, 17)
    assert _plazo_de(ini, date(2026, 7, 20), _LS, set(), set(), set(medios)) == 3.0


def test_inicio_desde_plazo_medio_dia_marca_el_primero():
    ini, medios = _inicio_desde_plazo(date(2026, 7, 15), 1.5, _LS, set(), set(), set())
    assert ini == date(2026, 7, 14)
    assert medios == [date(2026, 7, 14)]


# ── Desplazamiento en días hábiles (base de los lags) ────────
def test_habil_desplazado_cero_uno_y_negativo():
    sab = date(2026, 7, 18)
    assert _habil_desplazado(sab, 0, _LS, set()) == sab                  # ya es hábil
    assert _habil_desplazado(sab, 1, _LS, set()) == date(2026, 7, 20)    # salta el domingo
    assert _habil_desplazado(date(2026, 7, 19), 0, _LS, set()) == date(2026, 7, 20)
    assert _habil_desplazado(date(2026, 7, 20), -1, _LS, set()) == sab


# ── Tipos de vínculo: qué restricción impone cada uno ────────
def test_restriccion_dep_los_tres_tipos():
    ini_p, fin_p = date(2026, 7, 13), date(2026, 7, 16)
    # FS lag 0: la sucesora arranca el día hábil SIGUIENTE al fin
    assert _restriccion_dep("FS", 0, ini_p, fin_p, _LS, set()) == ("inicio", date(2026, 7, 17))
    # SS lag 0: arrancan el mismo día · lag 1: un día hábil después (traslape)
    assert _restriccion_dep("SS", 0, ini_p, fin_p, _LS, set()) == ("inicio", ini_p)
    assert _restriccion_dep("SS", 1, ini_p, fin_p, _LS, set()) == ("inicio", date(2026, 7, 14))
    # FF lag 0: no puede TERMINAR antes que la antecesora
    assert _restriccion_dep("FF", 0, ini_p, fin_p, _LS, set()) == ("fin", fin_p)


def test_restriccion_fs_lag_cero_es_el_comportamiento_de_siempre():
    """Antes de 0034 el lag se sumaba en días calendario; con lag 0 el
    resultado tiene que ser idéntico (siguiente hábil tras el fin), que es el
    99% de los vínculos ya cargados."""
    fin_p = date(2026, 7, 18)                       # sábado
    _, minimo = _restriccion_dep("FS", 0, date(2026, 7, 13), fin_p, _LS, set())
    assert minimo == _siguiente_habil(fin_p, _LS, set())


def test_restriccion_lag_negativo_adelanta_el_minimo():
    # FS-1 = traslape de un día: la sucesora puede arrancar el mismo día que
    # termina la antecesora.
    _, minimo = _restriccion_dep("FS", -1, date(2026, 7, 13), date(2026, 7, 16), _LS, set())
    assert minimo == date(2026, 7, 16)


# ── El modo de programación (la tabla de P6/Project) ─────────
def test_resolver_fechas_inicio_plazo_deriva_el_fin():
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_PLAZO", "plazo", date(2026, 7, 13), date(2026, 7, 13), 3.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 15), 3.0)


def test_resolver_fechas_inicio_plazo_al_mover_el_inicio_mueve_la_barra():
    # Mover el inicio NO estira la actividad: la desplaza conservando el plazo.
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_PLAZO", "inicio", date(2026, 7, 16), date(2026, 7, 15), 3.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 16), date(2026, 7, 18), 3.0)


def test_resolver_fechas_inicio_plazo_editar_el_fin_recalcula_el_plazo():
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_PLAZO", "fin", date(2026, 7, 13), date(2026, 7, 16), 3.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 16), 4.0)


def test_resolver_fechas_fin_plazo_deriva_el_inicio():
    ini, fin, plazo, _ = _resolver_fechas(
        "FIN_PLAZO", "plazo", date(2026, 7, 20), date(2026, 7, 20), 3.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 17), date(2026, 7, 20), 3.0)


def test_resolver_fechas_inicio_fin_manda_el_rango():
    # En INICIO_FIN el plazo es derivado: aunque llegue 99, gana el rango.
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_FIN", "fin", date(2026, 7, 13), date(2026, 7, 15), 99.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 15), 3.0)


def test_resolver_fechas_ambas_fechas_mandan_sobre_el_plazo():
    # Enviar inicio Y fin juntos es el gesto de "este es el rango, punto",
    # incluso en modo INICIO_PLAZO. Es lo que hace el modal de siempre.
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_PLAZO", "ambas", date(2026, 7, 13), date(2026, 7, 14), 10.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 14), 2.0)


def test_resolver_fechas_sin_plazo_lo_deduce_del_rango():
    # Compatibilidad: una actividad sin plazo (alta de siempre) lo obtiene del
    # rango sin cambiar ninguna fecha.
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_PLAZO", "inicio", date(2026, 7, 13), date(2026, 7, 15), None,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 15), 3.0)


def test_resolver_fechas_agregar_un_salto_estira_y_conserva_el_plazo():
    # INICIO_PLAZO + salto el martes: la actividad se estira al jueves para
    # seguir durando 3 días, en vez de perder uno.
    ini, fin, plazo, _ = _resolver_fechas(
        "INICIO_PLAZO", "dias", date(2026, 7, 13), date(2026, 7, 15), 3.0,
        _LS, set(), saltos={date(2026, 7, 14)}, medios=set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 16), 3.0)


def test_resolver_fechas_cambiar_de_modo_no_mueve_nada():
    ini, fin, plazo, _ = _resolver_fechas(
        "FIN_PLAZO", "modo", date(2026, 7, 13), date(2026, 7, 15), 3.0,
        _LS, set(), set(), set())
    assert (ini, fin, plazo) == (date(2026, 7, 13), date(2026, 7, 15), 3.0)


# ── Validaciones ─────────────────────────────────────────────
def test_parse_plazo_rechaza_fracciones_no_representables():
    assert _parse_plazo(1.5) == 1.5
    assert _parse_plazo(None) is None
    assert _parse_plazo("") is None
    for malo in (0, -2, 0.3, 1.25, 5000, "abc"):
        with pytest.raises(HTTPException):
            _parse_plazo(malo)


def test_crear_actividad_plazo_invalido_400():
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-13", "titulo": "x", "plazo_dias": 1.3},
                       headers=_hdr("oficina"))
    assert r.status_code == 400


def test_crear_actividad_modo_fecha_invalido_422():
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-13", "titulo": "x", "modo_fecha": "LO_QUE_SEA"},
                       headers=_hdr("oficina"))
    assert r.status_code == 422


def test_dependencia_tipo_invalido_422():
    r = _client().post("/ev/programacion/actividades/1/dependencias",
                       json={"predecesora_id": 2, "tipo": "XX"}, headers=_hdr("oficina"))
    assert r.status_code == 422


def test_dependencia_lag_desmedido_400():
    r = _client().post("/ev/programacion/actividades/1/dependencias",
                       json={"predecesora_id": 2, "lag_dias": 9999}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_encadenar_necesita_dos_actividades_400():
    r = _client().post("/ev/programacion/dependencias/encadenar",
                       json={"ids": [1]}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_encadenar_rechaza_ids_repetidos_400():
    r = _client().post("/ev/programacion/dependencias/encadenar",
                       json={"ids": [1, 2, 1]}, headers=_hdr("oficina"))
    assert r.status_code == 400


def test_encadenar_tipo_invalido_422():
    r = _client().post("/ev/programacion/dependencias/encadenar",
                       json={"ids": [1, 2], "tipo": "ZZ"}, headers=_hdr("oficina"))
    assert r.status_code == 422


def test_encadenar_supervisor_403():
    r = _client().post("/ev/programacion/dependencias/encadenar",
                       json={"ids": [1, 2]}, headers=_hdr("supervisor", "S1"))
    assert r.status_code == 403


# ── Metrado sin partida: el error silencioso que castigaba el PPC ──
def test_exigir_partida_deja_pasar_lo_legitimo():
    from routers.programacion import _exigir_partida
    _exigir_partida(None, None)      # actividad de apoyo (reunión, traslado)
    _exigir_partida(0, None)         # metrado vacío
    _exigir_partida(90, 5)           # producción con su partida
    _exigir_partida(None, 5)


def test_exigir_partida_bloquea_metrado_huerfano():
    from routers.programacion import _exigir_partida
    with pytest.raises(HTTPException) as e:
        _exigir_partida(90, None)
    assert e.value.status_code == 400
    assert "partida" in e.value.detail.lower()


def test_crear_actividad_con_metrado_sin_partida_400():
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-13", "titulo": "Relleno", "metrado_prog": 90},
                       headers=_hdr("oficina"))
    assert r.status_code == 400
    assert "partida" in r.json()["detail"].lower()


def test_crear_actividad_de_apoyo_sin_metrado_pasa_la_validacion():
    """Sin metrado NO es un error: es una actividad de apoyo. Llega hasta la
    BD (que no hay en este test), o sea que la validación la dejó pasar."""
    r = _client().post("/ev/programacion/actividades",
                       json={"fecha": "2026-07-13", "titulo": "Charla de seguridad"},
                       headers=_hdr("oficina"))
    assert r.status_code != 400
