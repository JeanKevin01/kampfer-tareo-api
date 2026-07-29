# -*- coding: utf-8 -*-
"""El día de una partida dividida es la SUMA de sus frentes (0038).

Defecto que motiva estos tests: la vista de Avance diario armaba
{fecha: cantidad} ASIGNANDO en vez de sumando. Mientras hubo una fila por
(partida, etapa, día) daba igual — el unique lo garantizaba. Con frentes hay una
fila por frente, así que el día se quedaba con el avance de UNO: el LookAhead
decía 1 760 y el diario mostraba 260. La misma obra, dos cifras.
"""


def _pivotar(filas):
    """La reducción de la vista de Avance diario, aislada."""
    m: dict = {}
    for r in filas:
        k = (r["partida_id"], r["hito_id"])
        m.setdefault(k, {})[r["f"]] = m.get(k, {}).get(r["f"], 0) + float(r["cantidad_dia"])
    return m


def _fila(pid, f, cant, hito=None):
    return {"partida_id": pid, "hito_id": hito, "f": f, "cantidad_dia": cant}


def test_cuatro_frentes_el_mismo_dia_suman():
    # Caso real de Jean: 1500 + 260 en el mismo lunes, dos frentes distintos.
    m = _pivotar([_fila(1, "2026-07-27", 1500), _fila(1, "2026-07-27", 260)])
    assert m[(1, None)]["2026-07-27"] == 1760


def test_el_acumulado_de_la_semana_cuadra_con_el_lookahead():
    filas = [_fila(1, "2026-07-27", 1500), _fila(1, "2026-07-27", 260),
             _fila(1, "2026-07-28", 300), _fila(1, "2026-07-28", 600),
             _fila(1, "2026-07-28", 1000)]
    dias = _pivotar(filas)[(1, None)]
    assert dias == {"2026-07-27": 1760, "2026-07-28": 1900}
    assert sum(dias.values()) == 3660      # lo que muestra el rollup semanal


def test_las_etapas_siguen_separadas():
    # Sumar por día no puede mezclar etapas distintas de la misma partida.
    m = _pivotar([_fila(1, "2026-07-27", 100), _fila(1, "2026-07-27", 40, hito=7)])
    assert m[(1, None)]["2026-07-27"] == 100
    assert m[(1, 7)]["2026-07-27"] == 40


def test_una_partida_sin_frentes_da_lo_de_siempre():
    m = _pivotar([_fila(1, "2026-07-27", 12)])
    assert m[(1, None)] == {"2026-07-27": 12}
