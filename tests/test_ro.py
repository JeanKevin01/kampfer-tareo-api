"""Pruebas de la lógica pura del RO (sin BD)."""
from routers.ro import _calc_mo_por_fase, _armar_ro


def test_mo_por_fase_usa_tarifa_y_default():
    hh = [
        {"fase": "10", "cargo": "Operario", "hh": 100},
        {"fase": "10", "cargo": "Peon", "hh": 50},
        {"fase": "20", "cargo": "Capataz", "hh": 10},   # sin tarifa propia → default
    ]
    tar = {"Operario": 30.0, "Peon": 20.0}
    mo = _calc_mo_por_fase(hh, tar, default=40.0)
    assert mo["10"] == 100 * 30 + 50 * 20      # 4000
    assert mo["20"] == 10 * 40                  # usa default


def test_armar_ro_margen_por_fase_y_totales():
    mo = {"10": 4000.0}
    costos = [
        {"fase": "10", "tipo_recurso": "MAT", "directo": True, "monto": 1000},
        {"fase": "10", "tipo_recurso": "EQP", "directo": True, "monto": 500},
        {"fase": None, "tipo_recurso": "GG",  "directo": False, "monto": 800},   # indirecto
    ]
    venta_real = {"10": 7000.0}
    ajustes = [{"fase": "10", "tipo": "ADICIONAL", "monto": 300}]
    ro = _armar_ro(mo, costos, venta_real, ajustes, fases_desc={"10": "Trabajos"})

    fila = ro["fases"][0]
    assert fila["fase"] == "10"
    assert fila["mo"] == 4000 and fila["mat"] == 1000 and fila["eqp"] == 500
    assert fila["costo"] == 5500                 # 4000 + 1000 + 500
    assert fila["venta"] == 7300                 # 7000 + 300 ajuste
    assert fila["margen"] == 1800                # 7300 - 5500

    assert ro["indirectos"]["GG"] == 800
    t = ro["totales"]
    assert t["costo_directo"] == 5500
    assert t["costo_indirecto"] == 800
    assert t["costo_total"] == 6300
    assert t["venta"] == 7300
    assert t["margen"] == 1000                   # 7300 - 6300
    assert round(t["pct_margen"], 4) == round(1000 / 7300, 4)


def test_armar_ro_vacio_no_explota():
    ro = _armar_ro({}, [], {}, [], {})
    assert ro["fases"] == []
    assert ro["totales"]["venta"] == 0 and ro["totales"]["margen"] == 0
