"""
Correlativo sugerido para una partida nueva (`_siguiente_codigo`).

Encargo de Jean: al crear una partida desde Programación tenía que inventarse el
código a mano. Ahora se propone el que le toca según la jerarquía, y los
adicionales llevan su propia serie para reconocerlos de un vistazo.
"""
from routers.ev.partidas import _siguiente_codigo


def test_hijo_que_sigue_dentro_del_padre():
    cods = ["03", "03.01", "03.02", "03.02.01", "03.02.02", "04"]
    assert _siguiente_codigo(cods, "03.02") == "03.02.03"


def test_primer_hijo_de_un_padre_sin_hijos():
    assert _siguiente_codigo(["03", "03.01"], "03.01") == "03.01.01"


def test_respeta_el_ancho_de_los_ceros_del_presupuesto():
    assert _siguiente_codigo(["01.001", "01.002"], "01") == "01.003"
    # Con un solo dígito se mantiene el mínimo de 2 (01, 02… es lo habitual).
    assert _siguiente_codigo(["01.1", "01.2"], "01") == "01.03"


def test_solo_cuentan_los_hijos_DIRECTOS():
    # '03.02.01.05' es nieto: no debe influir en el correlativo de los hijos.
    cods = ["03.02.01", "03.02.01.05", "03.02.01.06"]
    assert _siguiente_codigo(cods, "03.02") == "03.02.02"


def test_los_tramos_no_numericos_se_ignoran():
    cods = ["03.02.01", "03.02.ADIC", "03.02.02"]
    assert _siguiente_codigo(cods, "03.02") == "03.02.03"


def test_separador_coma_para_presupuestos_numerados_con_coma():
    assert _siguiente_codigo(["03,02,01"], "03,02") == "03,02,02"


def test_raiz_cuando_no_hay_padre():
    assert _siguiente_codigo(["01", "02", "03", "03.01"]) == "04"


def test_primera_partida_del_proyecto():
    assert _siguiente_codigo([]) == "01"
    assert _siguiente_codigo([], "05.02") == "05.02.01"


def test_adicional_tiene_su_propia_serie():
    assert _siguiente_codigo([], None, "ADICIONAL") == "ADIC-01"
    assert _siguiente_codigo(["ADIC-01", "ADIC-02"], None, "ADICIONAL") == "ADIC-03"


def test_el_adicional_no_hereda_la_numeracion_del_contrato():
    # Aunque cuelgue de 03.02, su código sigue la serie de adicionales.
    cods = ["03.02.01", "03.02.02", "ADIC-01"]
    assert _siguiente_codigo(cods, "03.02", "ADICIONAL") == "ADIC-02"


def test_el_contrato_no_se_contamina_con_los_adicionales():
    cods = ["03.02.01", "ADIC-07"]
    assert _siguiente_codigo(cods, "03.02", "CONTRACTUAL") == "03.02.02"


def test_adicional_en_minusculas_o_con_espacios():
    assert _siguiente_codigo(["adic-01"], None, " adicional ") == "ADIC-02"
