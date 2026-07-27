"""
Lugar de una partida en el WBS (`_wbs_de`) — el cálculo que faltaba en el alta
de a una y que dejaba huérfanas las partidas creadas fuera del importador.

Regla: el separador del código define la jerarquía; un `parent_codigo`
explícito (selector «cuelga de» del panel) manda sobre el código, porque un
adicional puede llamarse 'ADIC-01' y colgar igual de '03.02'.
"""
from routers.ev.partidas import _wbs_de


def test_codigo_con_puntos_deduce_padre_y_nivel():
    assert _wbs_de("03.02.15") == (3, "03.02")
    assert _wbs_de("03.02") == (2, "03")


def test_codigo_con_comas_usa_la_coma_como_separador():
    # El importador acepta ambos: hay presupuestos numerados con coma.
    assert _wbs_de("03,02,15") == (3, "03,02")


def test_codigo_de_un_solo_tramo_es_raiz():
    assert _wbs_de("ADIC-01") == (1, None)
    assert _wbs_de("03") == (1, None)


def test_padre_explicito_manda_sobre_el_codigo():
    # Caso real: adicional 'ADIC-01' que cuelga de la partida 03.02.
    assert _wbs_de("ADIC-01", None, "03.02") == (3, "03.02")
    # Y también cuando el código sí tiene jerarquía propia pero el planner
    # decide colgarlo de otro sitio.
    assert _wbs_de("09.01.01", None, "03") == (2, "03")


def test_nivel_explicito_se_respeta():
    assert _wbs_de("03.02.15", 4, None) == (4, "03.02")


def test_codigo_vacio_no_revienta():
    assert _wbs_de("") == (1, None)
    assert _wbs_de(None) == (1, None)


def test_separadores_repetidos_no_generan_tramos_vacios():
    # '03..02' no debe deducir un padre '03.' (tramo vacío).
    assert _wbs_de("03..02") == (2, "03")
