"""
Cierre de la semana: la regla del corte y el veredicto (funciones puras).

El PPC se medía sobre el plan VIGENTE, así que el pasado se movía: reprogramar
una actividad no cumplida borraba su compromiso de la semana cerrada y el
indicador subía solo. Estas son las piezas que lo congelan.

Caso de Jean: la obra trabaja lunes a domingo en dos guardias, pero el reporte
lo pedían el VIERNES en una empresa y el LUNES siguiente en otra. El día del
corte no mueve la semana — solo dice cuándo se mira.
"""
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from routers.prog_cierre import (entra_al_cierre, es_no_planificada, fecha_corte,
                                 marcar_no_planificadas, ventana_corte, veredicto)
from routers.programacion import ventana_ppc

LUNES = date(2026, 7, 27)          # lunes ISO
DOMINGO = date(2026, 8, 2)


# ── Cuándo se corta ──────────────────────────────────────────
def test_corte_domingo_es_el_defecto():
    assert fecha_corte(LUNES) == DOMINGO
    _, hasta, parcial = ventana_corte(LUNES)
    assert hasta == DOMINGO and parcial is False


def test_corte_viernes_es_parcial():
    """Empresa anterior de Jean: piden el PPC el viernes «para ver cómo va la
    semana», aunque la obra trabaje sábado y domingo."""
    corte, hasta, parcial = ventana_corte(LUNES, cierre_dia=5)
    assert corte == date(2026, 7, 31)
    assert hasta == corte          # el sábado y el domingo NO se cuentan
    assert parcial is True


def test_corte_lunes_siguiente_no_es_parcial():
    """Empresa actual: el reporte se pide el lunes, con la semana ya terminada."""
    corte, hasta, parcial = ventana_corte(LUNES, cierre_dia=1, semana_siguiente=True)
    assert corte == date(2026, 8, 3)
    assert hasta == DOMINGO        # se cuenta la semana COMPLETA
    assert parcial is False


def test_corte_sabado():
    corte, hasta, parcial = ventana_corte(LUNES, cierre_dia=6)
    assert corte == date(2026, 8, 1) and hasta == corte and parcial is True


def test_dia_de_corte_invalido():
    for malo in (0, 8, -1):
        with pytest.raises(HTTPException) as e:
            fecha_corte(LUNES, cierre_dia=malo)
        assert e.value.status_code == 400


# ── El veredicto es SEMANAL, no día a día ────────────────────
def test_se_compara_el_total_de_la_semana():
    """El caso exacto de Jean: 100 el jueves y 100 el viernes; hizo 50 y 150.
    Día a día habría fallado el jueves; contra el total de la semana, cumple."""
    assert veredicto(comprometido=200, alcanzado=200, estado="PROGRAMADO") is True


def test_no_llega_al_total():
    assert veredicto(200, 180, "PROGRAMADO") is False


def test_sobrecumplir_cuenta_como_cumplida():
    assert veredicto(200, 260, "PROGRAMADO") is True


def test_tolerancia_de_redondeo():
    assert veredicto(200, 199.9999, "PROGRAMADO") is True


def test_no_cumplida_manda_siempre():
    """Es una declaración explícita de fracaso: nadie la pone por error."""
    assert veredicto(200, 500, "NO_CUMPLIDA") is False


def test_con_metrado_comprometido_manda_el_metrado_no_el_estado():
    """CAMBIO DE PRECEDENCIA (2026-07-30). Antes EJECUTADO ganaba sobre el
    metrado, y eso dejó de ser una decisión del planner el día que el parte de
    campo empezó a poner EJECUTADO solo: una partida con 71 m³ comprometidos y 0
    registrados salía «cumplida» por haber mandado el parte (el caso real que
    Jean vio en pantalla, «Valle Principal FL10 CP12»).

    En un sistema donde el metrado es la fuente única, no capturar el avance no
    puede equivaler a cumplir."""
    assert veredicto(200, 0, "EJECUTADO") is False
    assert veredicto(200, 200, "EJECUTADO") is True


# ── Actividades sin metrado programado ───────────────────────
# `/ppc` las juzga por estado en la semana de su F.Inicio; el cierre las
# descartaba. Cerrar movía el PPC que el planner acababa de ver.
def test_sin_metrado_no_se_da_por_cumplida_sola():
    """Con la regla del metrado, 0 ≥ 0 daría cumplidas todas las actividades de
    apoyo y el PPC subiría solo por tenerlas en el plan."""
    assert veredicto(0, 0, "PROGRAMADO") is False


def test_sin_metrado_cumple_si_alguien_la_marco_ejecutada():
    assert veredicto(0, 0, "EJECUTADO") is True


def test_sin_metrado_pero_con_avance_registrado_cumple():
    """La actividad que termina su metrado ANTES de tiempo: el re-prorrateo le
    deja el compromiso de la semana en cero, justo por haber cumplido. Juzgarla
    solo por estado la declaraba no cumplida con 100 de 100 hechos."""
    assert veredicto(0, 100, "PROGRAMADO") is True


def test_sin_metrado_con_avance_pero_marcada_a_mano_no_cumple():
    assert veredicto(0, 100, "NO_CUMPLIDA") is False


def test_con_metrado_siempre_entra():
    assert entra_al_cierre(150, date(2026, 7, 20), LUNES) is True


def test_sin_metrado_entra_en_la_semana_de_su_inicio():
    assert entra_al_cierre(0, date(2026, 7, 29), LUNES) is True


def test_sin_metrado_no_entra_en_una_semana_que_solo_atraviesa():
    """Una actividad larga sin metrado se juzga UNA vez, en su semana de inicio;
    si no, la misma actividad bajaría el PPC de cada semana que atraviesa."""
    assert entra_al_cierre(0, date(2026, 7, 20), LUNES) is False
    assert entra_al_cierre(0, None, LUNES) is False


# ── Trabajo que entró después del compromiso ─────────────────
def test_creada_antes_del_lunes_es_planificada():
    creada = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    assert es_no_planificada(creada, LUNES) is False


def test_creada_a_mitad_de_semana_es_no_planificada():
    """El adicional de urgencia: se crea el jueves y se ejecuta el viernes.
    No estaba comprometido, así que no puede juzgar el cumplimiento del plan."""
    creada = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
    assert es_no_planificada(creada, LUNES) is True


def test_creada_el_mismo_dia_de_la_referencia_es_planificada():
    """Armar el plan el lunes por la mañana no puede marcar todo como
    improvisado: la comparación es estrictamente posterior."""
    creada = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)
    assert es_no_planificada(creada, LUNES) is False


def test_sin_fecha_de_creacion_no_se_inventa():
    assert es_no_planificada(None, LUNES) is False
    assert es_no_planificada(datetime(2026, 7, 30), None) is False


def test_acepta_datetime_naive():
    assert es_no_planificada(datetime(2026, 7, 30, 15, 0), LUNES) is True


# ── Cuando TODAS entraron después del compromiso ─────────────
# Jean: «¿por qué me aparece como no planificado la primera semana que tengo?».
# Porque las actividades se cargaron al sistema después de esa semana. Una marca
# que le cae al 100% de las filas no distingue nada, y además deja la semana con
# cero comprometidas y sin PPC.
TARDE = [datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)] * 3
A_TIEMPO = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


def test_si_todas_entraron_despues_no_se_marca_ninguna():
    assert marcar_no_planificadas(TARDE, LUNES) == [False, False, False]


def test_con_una_a_tiempo_las_demas_si_se_marcan():
    """Aquí la marca SÍ informa: hubo un plan y entró trabajo encima."""
    assert marcar_no_planificadas([A_TIEMPO] + TARDE[:2], LUNES) == [False, True, True]


def test_semana_vacia_no_rompe():
    assert marcar_no_planificadas([], LUNES) == []


# ── Ventana del PPC: el rango pedido manda ───────────────────
# Un reporte del 13 al 26 salía con el histograma de las últimas 8 semanas,
# metiendo la semana en curso —a medio correr— y torciendo la tendencia.
HOY = date(2026, 7, 28)


def test_sin_rango_son_las_ultimas_n_semanas():
    ini, fin = ventana_ppc(HOY, semanas=2)
    assert (ini, fin) == (date(2026, 7, 20), date(2026, 8, 2))


def test_con_rango_manda_el_rango_pedido():
    ini, fin = ventana_ppc(HOY, semanas=8, desde="2026-07-13", hasta="2026-07-20")
    assert (ini, fin) == (date(2026, 7, 13), date(2026, 7, 26))


def test_el_rango_se_alinea_a_semanas_iso():
    """Pedir de un miércoles a un jueves da semanas completas, no días sueltos:
    el PPC solo tiene sentido por semana."""
    ini, fin = ventana_ppc(HOY, desde="2026-07-15", hasta="2026-07-23")
    assert (ini, fin) == (date(2026, 7, 13), date(2026, 7, 26))


def test_rango_invertido_no_devuelve_vacio():
    ini, fin = ventana_ppc(HOY, desde="2026-07-20", hasta="2026-07-13")
    assert (ini, fin) == (date(2026, 7, 13), date(2026, 7, 26))


def test_rango_enorme_se_recorta_a_26_semanas():
    ini, fin = ventana_ppc(HOY, desde="2020-01-06", hasta="2026-07-20")
    assert fin == date(2026, 7, 26)
    assert (fin - ini).days == 26 * 7 - 1
