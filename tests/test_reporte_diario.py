# -*- coding: utf-8 -*-
"""Parte diario del supervisor (2026-07-19): texto para el grupo de WhatsApp."""
from datetime import date

from routers.programacion import _lista_json, armar_texto_reporte


def _texto():
    return armar_texto_reporte(
        date(2026, 5, 29), "DIA", "Erick Valdivia",
        [{"cargo": "OFICIAL MECANICO", "n": 5}, {"cargo": "CONDUCTOR", "n": 3}],
        [{"area": "PLANTA SX / EW", "items": ["Se realizo el corte de esparragos.",
                                              "Perforaciones en plancha para cerco"]}],
        [{"cat": "EQUIPOS", "detalle": "No se tuvo camion grua adecuado"}],
    )


def test_cabecera_con_fecha_turno_y_responsable():
    t = _texto()
    assert t.startswith("Fecha: 29/05\nTurno: DIA\nResponsable: Erick Valdivia")


def test_personal_por_cargo_con_total_y_dos_digitos():
    t = _texto()
    assert "CANTIDAD TOTAL PERSONAL: 8" in t
    assert "* Oficial Mecanico: 05" in t
    assert "* Conductor: 03" in t


def test_actividades_por_area_en_vinetas():
    t = _texto()
    assert "ACTIVIDADES REALIZADAS" in t
    assert "AREA: PLANTA SX / EW" in t
    assert "* Se realizo el corte de esparragos." in t


def test_restriccion_con_detalle_y_categoria_legible():
    t = _texto()
    assert "RESTRICCIONES." in t
    assert "* No se tuvo camion grua adecuado (Falta de equipos)" in t


def test_sin_restricciones_no_pone_la_seccion():
    t = armar_texto_reporte(date(2026, 5, 29), "NOCHE", "Ana",
                            [{"cargo": "OPERARIO", "n": 1}],
                            [{"area": "", "items": ["Algo"]}], [])
    assert "RESTRICCIONES" not in t
    assert "Turno: NOCHE" in t


def test_restriccion_solo_categoria_usa_su_texto():
    t = armar_texto_reporte(date(2026, 5, 29), "DIA", "Ana", [],
                            [{"area": "", "items": ["Algo"]}],
                            [{"cat": "CLIMA", "detalle": ""}])
    assert "* Clima" in t


# ── _lista_json: los campos multipart no deben tumbar el reporte ──

def test_lista_json_tolera_basura():
    assert _lista_json("") == []
    assert _lista_json("no es json") == []
    assert _lista_json('{"a":1}') == []          # objeto, no lista
    assert _lista_json('["  ", "ok"]') == ["ok"]  # descarta vacíos
    assert _lista_json('[{"cat":"CLIMA"}]') == [{"cat": "CLIMA"}]
