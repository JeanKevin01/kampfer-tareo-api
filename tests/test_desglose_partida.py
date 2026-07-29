"""
Desglose de una partida grande en áreas y capas (encargo de Jean 2026-07-28).

El caso: RELLENO ZONA 5 son 15 000 m³ presupuestados que NO se ejecutan de una
— se avanzan de 200 en 200 por áreas y por capas, y el lookahead se presenta
agrupado así. La partida sigue siendo UNA; lo que se subdivide es su
programación, y hace falta ver cuánto se le va quitando al presupuesto.
"""
from routers.programacion import _desglose, saldo_partida


# ── La etiqueta del tramo ────────────────────────────────────
def test_se_normaliza_para_poder_agrupar():
    """«área b» y «Área B » tienen que caer en el mismo grupo, o la vista por
    áreas —que es el motivo de todo esto— no sirve de nada."""
    assert _desglose("área b") == _desglose(" Área  B ") == "ÁREA B"


def test_vacio_es_ausencia_no_cadena_vacia():
    for v in ("", "   ", None):
        assert _desglose(v) is None


def test_se_recorta_a_lo_que_cabe_en_la_columna():
    assert len(_desglose("C" * 80)) == 40


def test_acepta_numeros_sueltos():
    """En obra la capa se escribe «3», no «Capa 3»."""
    assert _desglose(3) == "3"


# ── El saldo de la partida ───────────────────────────────────
def test_saldo_de_una_partida_a_medio_programar():
    s = saldo_partida(15000, 14800, 12300)
    assert s["saldo_por_programar"] == 200
    assert s["saldo_por_ejecutar"] == 2700
    assert s["excedido"] == 0
    assert s["pct_ejecutado"] == 0.82


def test_pasarse_del_presupuesto_se_informa_no_se_bloquea():
    """Decisión de Jean: la obra manda. El exceso se marca para sustentarlo
    después como mayor metrado, pero no frena la programación de la semana."""
    s = saldo_partida(15000, 15300, 0)
    assert s["excedido"] == 300
    assert s["saldo_por_programar"] == -300


def test_justo_en_el_limite_no_es_exceso():
    assert saldo_partida(15000, 15000, 15000)["excedido"] == 0


def test_tolerancia_de_redondeo():
    """40 tramos de 375 m³ no dan exactamente 15 000 en coma flotante; eso no
    puede aparecer como un exceso de 0.0000001 m³."""
    assert saldo_partida(15000, 15000.0004, 0)["excedido"] == 0


def test_sin_presupuesto_no_se_inventa_un_exceso():
    """Una partida sin metrado presupuestado (adicional que aún no se sustenta)
    no está «excedida» por programar trabajo: no hay contra qué comparar."""
    s = saldo_partida(0, 500, 100)
    assert s["excedido"] == 0
    assert s["pct_programado"] is None


def test_nada_programado_todavia():
    s = saldo_partida(15000, 0, 0)
    assert s["saldo_por_programar"] == 15000 and s["pct_programado"] == 0
