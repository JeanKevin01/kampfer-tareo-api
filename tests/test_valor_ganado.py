"""
Pruebas de la lógica de Valor Ganado (la pieza crítica del sistema).

Estas pruebas protegen las fórmulas del PF (Performance Factor), el cálculo de
HH ganadas/gastadas y, sobre todo, la regla anti–doble-conteo (manual gana sobre
tareo automático). Son funciones PURAS: no tocan la base de datos, así que corren
en segundos y sin configuración.

Ejecutar:  pytest -v
"""
from datetime import date

import pytest

from routers.valor_ganado import (
    _calcular, _validar_pesos, _semana_de, _agrupar, HitoIn,
)


# ── Helpers para construir filas como las que entrega la BD (asyncpg) ──
def partida(**kw):
    base = dict(
        id=1, codigo="01", otm_id="OTM1", fase="MEC", sistema="S1",
        descripcion="Montaje", unidad="m",
        metrado_presup=100, metrado_proyec=100, hh_presup=200,
    )
    base.update(kw)
    return base


def hito(**kw):
    base = dict(id=10, partida_id=1, numero=1, peso=1.0, es_principal=True)
    base.update(kw)
    return base


def avance(**kw):
    base = dict(hito_id=10, semana=1, cantidad_acum=50)
    base.update(kw)
    return base


# ════════════════════════════════════════════════════════════════
# _calcular — el corazón del Valor Ganado
# ════════════════════════════════════════════════════════════════

def test_calcular_caso_basico():
    """Partida con 50% de avance y 80 HH gastadas manualmente."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={},
        semana=1,
    )
    f = filas[0]
    assert f["hh_proyec"] == 200.0            # 100 m × (200/100) hh/m
    assert f["pct_avance"] == 0.5             # 50 / 100
    assert f["cantidad_instalada"] == 50.0
    assert f["hh_ganadas_acum"] == 100.0      # 0.5 × 200
    assert f["hh_gastadas_acum"] == 80.0
    assert f["pf_acum"] == 1.25               # 100 / 80


def test_calcular_usa_tareo_automatico_si_no_hay_manual():
    """Sin HH manual, toma las HH del tareo automático."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[],
        tareo={(1, 1): 40.0},
        semana=1,
    )
    f = filas[0]
    assert f["hh_gastadas_acum"] == 40.0
    assert f["pf_acum"] == 2.5                # 100 / 40


def test_calcular_manual_gana_sobre_tareo_sin_doble_conteo():
    """REGLA CRÍTICA: si hay HH manual para (partida, semana), el tareo
    automático NO se suma encima (evita el doble conteo)."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={(1, 1): 40.0},   # debe IGNORARSE
        semana=1,
    )
    f = filas[0]
    assert f["hh_gastadas_acum"] == 80.0      # NO 120 — no se duplica


def test_calcular_avance_se_topa_en_100_por_ciento():
    """Aunque la cantidad supere el metrado, el % de avance no pasa de 100%."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=150)],   # 150 > 100 metrado
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={},
        semana=1,
    )
    f = filas[0]
    assert f["pct_avance"] == 1.0
    assert f["hh_ganadas_acum"] == 200.0       # tope = hh_proyec


def test_calcular_pf_cero_si_no_hay_hh_gastadas():
    """Sin HH gastadas, el PF es 0 (no división por cero)."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[],
        tareo={},
        semana=1,
    )
    assert filas[0]["pf_acum"] == 0.0


def test_calcular_dos_hitos_ponderados():
    """Dos hitos con pesos 0.4 / 0.6: el % de avance los combina."""
    hitos = [
        hito(id=10, numero=1, peso=0.4, es_principal=False),
        hito(id=11, numero=2, peso=0.6, es_principal=True),
    ]
    avances = [
        {"hito_id": 10, "semana": 1, "cantidad_acum": 100},   # hito 1 al 100%
        {"hito_id": 11, "semana": 1, "cantidad_acum": 50},    # hito 2 al 50%
    ]
    filas = _calcular([partida()], hitos, avances, [], {}, 1)
    # pct = 0.4×1.0 + 0.6×0.5 = 0.7
    assert filas[0]["pct_avance"] == 0.7


# ════════════════════════════════════════════════════════════════
# _validar_pesos — los hitos deben sumar 1.00 con un solo principal
# ════════════════════════════════════════════════════════════════

def test_validar_pesos_ok():
    _validar_pesos([
        HitoIn(numero=1, peso=0.6, es_principal=True),
        HitoIn(numero=2, peso=0.4, es_principal=False),
    ])  # no lanza


def test_validar_pesos_no_suman_uno():
    with pytest.raises(Exception):
        _validar_pesos([HitoIn(numero=1, peso=0.5, es_principal=True)])


def test_validar_pesos_dos_principales():
    with pytest.raises(Exception):
        _validar_pesos([
            HitoIn(numero=1, peso=0.5, es_principal=True),
            HitoIn(numero=2, peso=0.5, es_principal=True),
        ])


def test_validar_pesos_numeros_repetidos():
    with pytest.raises(Exception):
        _validar_pesos([
            HitoIn(numero=1, peso=0.5, es_principal=True),
            HitoIn(numero=1, peso=0.5, es_principal=False),
        ])


# ════════════════════════════════════════════════════════════════
# _semana_de — mapeo fecha → número de semana del proyecto
# ════════════════════════════════════════════════════════════════

def test_semana_de():
    base = date(2026, 1, 1)
    assert _semana_de(date(2026, 1, 1), base) == 1
    assert _semana_de(date(2026, 1, 8), base) == 2
    assert _semana_de(date(2026, 1, 15), base) == 3


# ════════════════════════════════════════════════════════════════
# _agrupar — totales por fase/OTM/sistema
# ════════════════════════════════════════════════════════════════

def test_agrupar_suma_por_fase():
    filas = [
        {"fase": "MEC", "hh_proyec": 100, "hh_ganadas_acum": 80, "hh_gastadas_acum": 90, "eac_hh": 110},
        {"fase": "MEC", "hh_proyec": 50,  "hh_ganadas_acum": 40, "hh_gastadas_acum": 50, "eac_hh": 60},
    ]
    grupos = _agrupar(filas, "fase")
    assert len(grupos) == 1
    g = grupos[0]
    assert g["grupo"] == "MEC"
    assert g["hh_proyec"] == 150.0
    assert g["hh_ganadas"] == 120.0
    assert g["hh_gastadas"] == 140.0
    assert g["pct_avance"] == 0.8             # 120 / 150
    assert g["pf"] == 0.857                    # round(120 / 140, 3)
