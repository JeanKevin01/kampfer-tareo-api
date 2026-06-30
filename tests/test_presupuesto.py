"""Pruebas de la lógica pura del módulo presupuesto (sin BD)."""
from routers.presupuesto import _siguiente_version, _clasificar_codigos


def test_siguiente_version_vacio():
    assert _siguiente_version([]) == 1


def test_siguiente_version_correlativa():
    assert _siguiente_version([1, 2, 3]) == 4
    assert _siguiente_version([5]) == 6
    assert _siguiente_version([2, 1, 3]) == 4   # no asume orden


def test_clasificar_codigos_match_y_faltantes():
    presup = ["A", "B", "C"]
    ev = ["A", "C", "Z"]
    enc, falt = _clasificar_codigos(presup, ev)
    assert enc == ["A", "C"]
    assert falt == ["B"]


def test_clasificar_codigos_todos_faltan():
    enc, falt = _clasificar_codigos(["X", "Y"], [])
    assert enc == []
    assert falt == ["X", "Y"]
