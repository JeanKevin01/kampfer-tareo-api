# -*- coding: utf-8 -*-
"""Sub-filas del LookAhead (0038): qué hereda un «Frente / Tramo / Sector».

El caso que las motiva: RELLENO ZONA 5, 15 000 m3 que se ejecutan de 200 en 200
por áreas y capas. La sub-fila tiene que descontar del presupuesto de ESA
partida, así que lo que hereda no es cosmética: si eligiera partida propia, el
saldo dejaría de cuadrar contra el contractual.
"""
from routers.programacion import herencia_subfila

PADRE = {"id": 46, "partida_id": 7, "otm_id": "OTM-0004", "hito_id": None,
         "und": "m3", "metrado_prog": 200}


def test_la_subfila_no_elige_partida_ni_otm():
    h = herencia_subfila(PADRE, True, None, 80, None)
    assert h["partida_id"] == 7 and h["otm_id"] == "OTM-0004"


def test_el_frente_hereda_la_etapa_del_padre():
    # La necesita para alimentar el % de Valor Ganado del hito correcto.
    padre = {**PADRE, "hito_id": 12}
    assert herencia_subfila(padre, True, None, 80, None)["hito_id"] == 12


def test_una_subetapa_trae_su_propio_hito():
    padre = {**PADRE, "hito_id": 12}
    assert herencia_subfila(padre, False, None, 80, 34)["hito_id"] == 34


def test_sin_metrado_tecleado_se_hereda_el_del_padre():
    # Dividir en dos una fila que ya estaba programada: los 200 no se pierden.
    assert herencia_subfila(PADRE, True, None, None, None)["metrado"] == 200


def test_el_metrado_tecleado_manda():
    assert herencia_subfila(PADRE, True, None, 80, None)["metrado"] == 80


def test_metrado_cero_del_padre_no_inventa_metrado():
    padre = {**PADRE, "metrado_prog": 0}
    assert herencia_subfila(padre, True, None, None, None)["metrado"] is None


def test_la_unidad_tecleada_gana_a_la_heredada():
    assert herencia_subfila(PADRE, True, "und", 80, None)["und"] == "und"
    assert herencia_subfila(PADRE, True, None, 80, None)["und"] == "m3"
