# -*- coding: utf-8 -*-
"""Genera tests/fixtures/pu_min.xls — mini plantilla PU SINTÉTICA (F1.2).

Replica el formato real de "PU Rev01 Meta Metrado" con datos inventados
(NADA del Excel de la empresa entra al repo): 1 padre + 2 hojas + 1 subpartida.
Valores diseñados para que los invariantes cierren exactos:
  01.01 Excavacion test  m3 x100  CUD 3.00 = MO 1.00 + EQ 0.05 + SUB 1.95
  01.02 Relleno test     m3 x50   CUD 2.00 = MO 2.00
  Total presupuesto = 300 + 100 = 400 USD · HH meta = 50 + 50 = 100

Uso (solo para regenerar el fixture; requiere `pip install xlwt`):
  python scripts/hacer_fixture_pu.py
"""
import xlwt


def main():
    wb = xlwt.Workbook()

    # ── PtoMeta ──────────────────────────────────────────────
    pt = wb.add_sheet("PtoMeta")
    hdr = ["Fase", "Sub-fase", "Sub Item", "Item", "Descripción", "Und.",
           "Metrado Meta", "Precio $ Meta", "Parcial $ Meta", "Total $ Meta",
           "", "PU Oferta", "Parcial Oferta", "Diferencia"]
    for c, v in enumerate(hdr):
        pt.write(8, c, v)
    # padre
    pt.write(9, 3, "01"); pt.write(9, 4, "OBRAS CIVILES TEST"); pt.write(9, 9, 400.0)
    # hojas
    pt.write(10, 0, "11"); pt.write(10, 1, "11.02"); pt.write(10, 2, "11.02")
    pt.write(10, 3, "01.01"); pt.write(10, 4, "Excavacion test"); pt.write(10, 5, "m3")
    pt.write(10, 6, 100.0); pt.write(10, 7, 3.0); pt.write(10, 8, 300.0)
    pt.write(10, 11, 3.5); pt.write(10, 12, 350.0); pt.write(10, 13, -50.0)
    pt.write(11, 0, "12"); pt.write(11, 1, "12.01"); pt.write(11, 2, "12.01")
    pt.write(11, 3, "01.02"); pt.write(11, 4, "Relleno test"); pt.write(11, 5, "m3")
    pt.write(11, 6, 50.0); pt.write(11, 7, 2.0); pt.write(11, 8, 100.0)
    pt.write(11, 11, 2.5); pt.write(11, 12, 125.0); pt.write(11, 13, -25.0)

    # ── PU-Meta ──────────────────────────────────────────────
    pu = wb.add_sheet("PU-Meta")
    pu.write(5, 7, "HH - Dia"); pu.write(5, 8, 10.0)
    hdr2 = ["P or SP", "Area", "Descripción Area", "Insumo", "Fase", "Sub-fase",
            "Sub Item", "Código", "Descripción", "Detalle ", "", "Unidad",
            "Cuadrilla", "Cantidad", "Precio", "Parcial", "Metrado",
            "Productividad", "Costo", "Costo Partida", "ST"]
    for c, v in enumerate(hdr2):
        pu.write(7, c, v)

    r = 8
    def w(fila_vals):
        nonlocal r
        for c, v in fila_vals.items():
            pu.write(r, c, v)
        r += 1

    ctx = {1: "Area 1", 4: "11", 5: "11.02", 6: "11.02"}
    # Bloque P 01.01 (con MO + EQ + SUB)
    w({**ctx, 0: "P", 7: "Partida", 8: "01.01", 9: "Excavacion test", 16: 100.0, 19: 300.0})
    w({**ctx, 7: "Rendimiento", 8: "MO.", 9: 250.0, 10: "EQ.", 11: 200.0,
       14: "Costo unitario directo por : m3", 15: 3.0})
    w({**ctx, 7: "Código", 8: "Descripción Recurso", 11: "Unidad", 13: "Cantidad",
       14: "Precio $", 15: "Parcial $"})
    w({**ctx, 8: "Mano de Obra"})
    w({**ctx, 3: "MO", 7: "0147010004", 8: "PEON", 11: "hh", 12: 2.0, 13: 0.5,
       14: 2.0, 15: 1.0, 17: 50.0, 18: 100.0})
    w({**ctx, 15: 1.0})
    w({**ctx, 8: "Equipos"})
    w({**ctx, 3: "EQ", 7: "0337010001", 8: "HERRAMIENTAS MANUALES", 11: "%MO",
       13: 5.0, 14: 1.0, 15: 0.05})
    w({**ctx, 15: 0.05})
    w({**ctx, 8: "Subpartidas"})
    w({**ctx, 7: "909000000001", 8: "Sub test", 11: "m2", 13: 0.5, 14: 3.9, 15: 1.95})
    # SP 'Sub test'
    w({**ctx, 0: "SP", 7: "Partida", 8: "Sub test"})
    w({**ctx, 7: "Rendimiento", 8: "MO.", 9: 100.0, 10: "EQ.", 11: 100.0,
       14: "Costo unitario directo por : m2", 15: 3.9})
    w({**ctx, 7: "Código", 8: "Descripción Recurso", 11: "Unidad", 13: "Cantidad",
       14: "Precio $", 15: "Parcial $"})
    w({**ctx, 8: "Mano de Obra"})
    w({**ctx, 3: "MO", 7: "0147010003", 8: "OFICIAL", 11: "hh", 12: 1.0, 13: 1.0,
       14: 3.9, 15: 3.9, 17: 50.0, 18: 195.0})
    w({**ctx, 15: 3.9})
    # cierre del bloque 01.01 (P = CUD del padre)
    w({**ctx, 15: 3.0})

    ctx2 = {1: "Area 1", 4: "12", 5: "12.01", 6: "12.01"}
    # Bloque P 01.02 (solo MO)
    w({**ctx2, 0: "P", 7: "Partida", 8: "01.02", 9: "Relleno test", 16: 50.0, 19: 100.0})
    w({**ctx2, 7: "Rendimiento", 8: "MO.", 9: 125.0, 10: "EQ.", 11: 125.0,
       14: "Costo unitario directo por : m3", 15: 2.0})
    w({**ctx2, 7: "Código", 8: "Descripción Recurso", 11: "Unidad", 13: "Cantidad",
       14: "Precio $", 15: "Parcial $"})
    w({**ctx2, 8: "Mano de Obra"})
    w({**ctx2, 3: "MO", 7: "0147010004", 8: "PEON", 11: "hh", 12: 1.0, 13: 1.0,
       14: 2.0, 15: 2.0, 17: 50.0, 18: 100.0})
    w({**ctx2, 15: 2.0})

    wb.save("tests/fixtures/pu_min.xls")
    print("fixture escrito: tests/fixtures/pu_min.xls")


if __name__ == "__main__":
    main()
