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

from routers.prog_cierre import (es_no_planificada, fecha_corte, ventana_corte,
                                 veredicto)

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


def test_estados_manuales_mandan():
    assert veredicto(200, 500, "NO_CUMPLIDA") is False
    assert veredicto(200, 0, "EJECUTADO") is True


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
