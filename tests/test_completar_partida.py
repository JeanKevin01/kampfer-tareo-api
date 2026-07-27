"""
Bandeja «por completar»: los datos que le faltan a una partida creada en obra
(`PUT /ev/partidas/{id}/completar`).

El PU es el que motiva el endpoint: la venta del RO es Σ(cantidad × PU), así que
una partida sin PU entra al resultado como COSTO PURO SIN VENTA. La bandeja lo
rellena celda a celda, y por eso la regla dura es que un campo ausente no se
toca: rellenar el PU no puede borrar de rebote las HH.
"""
import pytest
from fastapi import HTTPException

from routers.ev.partidas import _campos_completar


def test_solo_viajan_los_campos_mandados():
    assert _campos_completar({"precio_unitario": 45}) == {"precio_unitario": 45.0}
    assert _campos_completar({"hh_presup": 1200}) == {"hh_presup": 1200.0}


def test_los_tres_a_la_vez():
    assert _campos_completar(
        {"hh_presup": 1200, "precio_unitario": 45.5, "metrado_presup": 800}
    ) == {"hh_presup": 1200.0, "precio_unitario": 45.5, "metrado_presup": 800.0}


def test_un_campo_ausente_o_none_no_se_toca():
    """Lo importante: completar el PU NO puede poner las HH en 0."""
    out = _campos_completar({"precio_unitario": 45, "hh_presup": None})
    assert out == {"precio_unitario": 45.0}
    assert "hh_presup" not in out


def test_cero_si_es_un_valor_valido():
    # 0 no es «ausente»: el planner puede querer dejar el PU en 0 a propósito.
    assert _campos_completar({"precio_unitario": 0}) == {"precio_unitario": 0.0}


def test_texto_numerico_se_acepta():
    # El panel manda lo que hay en el input, que es un string.
    assert _campos_completar({"precio_unitario": "45.5"}) == {"precio_unitario": 45.5}


def test_no_numerico_es_400():
    with pytest.raises(HTTPException) as e:
        _campos_completar({"precio_unitario": "cuarenta"})
    assert e.value.status_code == 400


def test_negativo_es_400():
    with pytest.raises(HTTPException) as e:
        _campos_completar({"hh_presup": -1})
    assert e.value.status_code == 400


def test_body_vacio_es_400():
    with pytest.raises(HTTPException) as e:
        _campos_completar({})
    assert e.value.status_code == 400


def test_campos_desconocidos_se_ignoran():
    """La bandeja no es un editor de partidas: no puede cambiar fase ni código."""
    out = _campos_completar({"precio_unitario": 45, "fase": "EST", "codigo": "99.99"})
    assert out == {"precio_unitario": 45.0}
