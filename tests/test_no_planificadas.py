"""
Trabajo no planificado: clasificación, indicador de HH y compromiso congelado
(funciones puras de 0040).

El PPC mide la confiabilidad de la PROMESA, así que el trabajo que nadie
prometió no puede entrar ni al numerador ni al denominador — si contara como
cumplido premiaría improvisar, y si contara como fallido el supervisor dejaría
de reportarlo, que es peor porque se pierde el dato. Pero sacarlo del PPC y no
medirlo en ningún otro sitio era tirar la señal: estas son las piezas que la
convierten en algo que se puede atacar.
"""
from datetime import date

import pytest
from fastapi import HTTPException

from routers.prog_cierre import (NO_PLAN_MOTIVOS, atribuir_hh,
                                 cuenta_como_improvisacion,
                                 marcar_no_planificadas, ratio_no_planificado,
                                 validar_motivo_no_plan)


# ── El catálogo de motivos ───────────────────────────────────
def test_los_cuatro_motivos_y_nada_mas():
    """Cuatro y no más: un catálogo largo no se llena. Cada uno dispara una
    acción distinta, y por eso no pueden ir en la misma bolsa."""
    assert set(NO_PLAN_MOTIVOS) == {
        "OMISION_PLANNER", "EMERGENCIA", "CLIENTE", "ADELANTO"}


def test_motivo_vacio_es_sin_clasificar():
    assert validar_motivo_no_plan(None) is None
    assert validar_motivo_no_plan("") is None


def test_motivo_inventado_es_422():
    with pytest.raises(HTTPException) as e:
        validar_motivo_no_plan("PORQUE_SI")
    assert e.value.status_code == 422


# ── Qué cuenta como improvisación ────────────────────────────
def test_el_adelanto_no_es_improvisacion():
    """Entró fuera del compromiso, pero adelantar trabajo de otra semana es
    resecuenciamiento — justo la conducta que se quiere fomentar. Contarlo como
    desorden castigaría al planner por ser flexible."""
    assert cuenta_como_improvisacion(True, "ADELANTO") is False


def test_los_otros_tres_si_cuentan():
    for m in ("OMISION_PLANNER", "EMERGENCIA", "CLIENTE"):
        assert cuenta_como_improvisacion(True, m) is True


def test_sin_clasificar_todavia_cuenta():
    """Si lo pendiente de clasificar desapareciera del número, desaparecería el
    incentivo a clasificarlo."""
    assert cuenta_como_improvisacion(True, None) is True


def test_lo_comprometido_nunca_cuenta():
    assert cuenta_como_improvisacion(False, "OMISION_PLANNER") is False


# ── El indicador de HH ───────────────────────────────────────
def test_ratio_de_horas():
    assert ratio_no_planificado(18, 100) == 0.18


def test_sin_tareo_el_ratio_es_none_no_cero():
    """Un cero diría «no improvisamos nada» cuando lo que pasa es que no hay
    tareo. El «—» es honesto; el 0 miente."""
    assert ratio_no_planificado(0, 0) is None
    assert ratio_no_planificado(5, None) is None


# ── Atribución de las HH del tareo a las actividades ─────────
# El tareo conoce partidas, no actividades: misma partida, mismo supervisor, día
# dentro del rango.
def _act(i, pid, sup, ini, fin):
    return {"id": i, "partida_id": pid, "supervisor_id": sup,
            "fecha": date(2026, 7, ini), "fecha_fin": date(2026, 7, fin)}


def test_una_sola_candidata_se_lleva_las_horas():
    claves = {(10, "S1", date(2026, 7, 21)): 9.5}
    por_act, huerf = atribuir_hh(claves, [_act(1, 10, "S1", 20, 24)])
    assert por_act == {1: 9.5}
    assert huerf == 0.0


def test_horas_de_otra_partida_no_se_atribuyen():
    claves = {(99, "S1", date(2026, 7, 21)): 8.0}
    por_act, huerf = atribuir_hh(claves, [_act(1, 10, "S1", 20, 24)])
    assert por_act == {}
    assert huerf == 8.0, "las horas sin actividad se informan, no se diluyen"


def test_horas_de_otro_supervisor_no_se_atribuyen():
    claves = {(10, "S2", date(2026, 7, 21)): 8.0}
    _por_act, huerf = atribuir_hh(claves, [_act(1, 10, "S1", 20, 24)])
    assert huerf == 8.0


def test_dia_fuera_del_rango_de_la_actividad():
    claves = {(10, "S1", date(2026, 7, 28)): 8.0}
    _por_act, huerf = atribuir_hh(claves, [_act(1, 10, "S1", 20, 24)])
    assert huerf == 8.0


def test_empate_lo_gana_el_rango_mas_corto():
    """Dos tramos de la misma partida el mismo día: un tramo de UN día describe
    mejor lo que pasó ese día que una actividad de un mes. Determinista y
    explicable; repartir a medias inventaría datos."""
    claves = {(10, "S1", date(2026, 7, 22)): 9.0}
    largo = _act(1, 10, "S1", 1, 31)
    corto = _act(2, 10, "S1", 22, 22)
    por_act, _h = atribuir_hh(claves, [largo, corto])
    assert por_act == {2: 9.0}


def test_empate_de_rango_lo_gana_el_id_menor():
    claves = {(10, "S1", date(2026, 7, 22)): 9.0}
    por_act, _h = atribuir_hh(
        claves, [_act(7, 10, "S1", 22, 22), _act(3, 10, "S1", 22, 22)])
    assert por_act == {3: 9.0}


def test_varios_dias_se_acumulan_en_la_misma_actividad():
    claves = {(10, "S1", date(2026, 7, 21)): 9.5,
              (10, "S1", date(2026, 7, 22)): 8.0}
    por_act, _h = atribuir_hh(claves, [_act(1, 10, "S1", 20, 24)])
    assert por_act == {1: 17.5}


# ── El compromiso congelado manda sobre la deducción ─────────
def test_con_compromiso_congelado_la_respuesta_es_exacta():
    """Con el conjunto comprometido no hay nada que deducir: no planificada = no
    está en el conjunto. Ni falsos positivos ni desmarques que discutir."""
    marcas = marcar_no_planificadas(
        creados=[None, None, None], referencia=date(2026, 7, 20),
        ids=[1, 2, 3], comprometidos={1, 3})
    assert marcas == [False, True, False]


def test_con_compromiso_vacio_todas_son_no_planificadas():
    """Comprometer la semana SIN actividades es una decisión válida: todo lo que
    entre después es no planificado. Aquí no aplica la excepción del «todas»,
    porque no hay nada que deducir."""
    marcas = marcar_no_planificadas([None, None], date(2026, 7, 20),
                                    ids=[1, 2], comprometidos=set())
    assert marcas == [True, True]


def test_sin_compromiso_congelado_cae_a_la_deduccion():
    lun = date(2026, 7, 20)
    marcas = marcar_no_planificadas(
        creados=[date(2026, 7, 17), date(2026, 7, 23)], referencia=lun)
    assert marcas == [False, True]


def test_la_excepcion_del_todas_sigue_viva_sin_compromiso():
    """Si TODAS son posteriores casi siempre significa que el plan se cargó al
    sistema después de la semana, no que la obra improvisara siete días."""
    lun = date(2026, 7, 20)
    marcas = marcar_no_planificadas(
        creados=[date(2026, 7, 23), date(2026, 7, 24)], referencia=lun)
    assert marcas == [False, False]
