"""
Atribución del avance real cuando una partida se programa en VARIOS TRAMOS
(`_dueno_del_real`, migración 0035).

El bug que motiva esto, reproducido antes de arreglarlo: una partida de 100 m²
programada en dos tramos de 50, con 50 ejecutados solo en el primero, mostraba
el mismo real en las dos filas, dejaba al segundo tramo sin plan al
re-prorratearse y daba las DOS por cumplidas en el PPC.

Orden de la regla: dueño registrado > rango que cubre el día (el último
programado si varios) > la última que terminó antes > la primera de todas.
"""
from datetime import date

from routers.programacion import _dueno_del_real


def _act(i, ini, fin):
    return {"id": i, "fecha": date.fromisoformat(ini), "fecha_fin": date.fromisoformat(fin)}


A = _act(1, "2026-06-01", "2026-06-03")
B = _act(2, "2026-06-04", "2026-06-06")


def test_cada_dia_es_del_tramo_que_lo_cubre():
    d = _dueno_del_real([date(2026, 6, 2), date(2026, 6, 5)], [A, B])
    assert d == {date(2026, 6, 2): 1, date(2026, 6, 5): 2}


def test_el_dueno_registrado_manda_sobre_las_fechas():
    # El planner registró el avance DESDE el tramo B aunque el día cae en A.
    d = _dueno_del_real([(date(2026, 6, 2), 2)], [A, B])
    assert d == {date(2026, 6, 2): 2}


def test_un_dueno_registrado_que_ya_no_existe_se_ignora():
    # La actividad se borró (ON DELETE SET NULL) o se canceló: decide el rango.
    d = _dueno_del_real([(date(2026, 6, 2), 99)], [A, B])
    assert d == {date(2026, 6, 2): 1}


def test_si_varios_tramos_cubren_el_dia_gana_el_ultimo_programado():
    # Reprogramar encima: lo último que dijo el planner manda.
    encima = _act(7, "2026-06-01", "2026-06-10")
    d = _dueno_del_real([date(2026, 6, 2)], [A, encima])
    assert d == {date(2026, 6, 2): 7}


def test_dia_fuera_de_todo_rango_va_al_ultimo_que_termino_antes():
    # Se trabajó el domingo 07, después de que ambos tramos cerraran.
    d = _dueno_del_real([date(2026, 6, 7)], [A, B])
    assert d == {date(2026, 6, 7): 2}


def test_dia_anterior_a_todo_va_al_primer_tramo():
    # Se adelantó trabajo antes de lo programado: no se pierde.
    d = _dueno_del_real([date(2026, 5, 20)], [A, B])
    assert d == {date(2026, 5, 20): 1}


def test_con_un_solo_tramo_todo_es_suyo():
    dias = [date(2026, 5, 1), date(2026, 6, 2), date(2026, 12, 31)]
    assert set(_dueno_del_real(dias, [A]).values()) == {1}


def test_sin_actividades_no_atribuye_nada():
    assert _dueno_del_real([date(2026, 6, 2)], []) == {}


def test_actividad_de_un_solo_dia_sin_fecha_fin():
    suelta = {"id": 5, "fecha": date(2026, 7, 1), "fecha_fin": None}
    d = _dueno_del_real([date(2026, 7, 1), date(2026, 7, 2)], [suelta])
    assert d == {date(2026, 7, 1): 5, date(2026, 7, 2): 5}
