# -*- coding: utf-8 -*-
"""F1.2/F1.3 — parser de la plantilla PU + derivados del APU.

El fixture pu_min.xls es SINTÉTICO (scripts/hacer_fixture_pu.py):
  01.01 Excavacion test m3 ×100  CUD 3.00 = MO 1.00 + EQ 0.05 + SUB 1.95
  01.02 Relleno test    m3 ×50   CUD 2.00 = MO 2.00
  Total 400 USD · HH meta 100 · 1 subpartida 'Sub test' (MO 3.9)

El test del archivo REAL solo corre si existe en el workspace local (no está
en el repo por confidencialidad); en CI se salta.
"""
import os

import pytest

from parsers.plantilla_pu import parsear_archivo, parsear_plantilla_pu
from routers.presupuesto_derivados import (
    costo_meta_por_fase_recurso, costo_por_tipo_unitario, hh_meta, ratio_meta,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pu_min.xls")
REAL = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                    "Plantillas base",
                    "PU - Mano de Obra, Materiales y Equipos - Rev01 Meta Metrado.xls")


@pytest.fixture(scope="module")
def res():
    return parsear_archivo(FIXTURE)


# ── Parser: fixture sintético ─────────────────────────────────
def test_fixture_sin_errores(res):
    assert res.errores == []
    assert res.avisos == []


def test_conteos(res):
    hojas = [p for p in res.partidas if p.es_hoja]
    padres = [p for p in res.partidas if not p.es_hoja]
    assert len(hojas) == 2 and len(padres) == 1
    assert len(res.subpartidas) == 1
    assert res.hh_dia == 10.0


def test_jerarquia(res):
    padre = next(p for p in res.partidas if p.codigo == "01")
    hoja = next(p for p in res.partidas if p.codigo == "01.01")
    assert padre.nivel == 1 and not padre.es_hoja
    assert hoja.nivel == 2 and hoja.parent == "01"
    assert hoja.fase == "11" and hoja.sub_fase == "11.02" and hoja.unidad == "m3"


def test_apu_cuadra_con_cud(res):
    for hoja in (p for p in res.partidas if p.es_hoja):
        suma = sum(r.parcial for r in hoja.recursos)
        assert abs(suma - hoja.cud) < 0.01, hoja.codigo


def test_subpartida_enlazada(res):
    hoja = next(p for p in res.partidas if p.codigo == "01.01")
    ref = next(r for r in hoja.recursos if r.tipo == "SUB")
    assert ref.sub is not None
    assert ref.sub.descripcion == "Sub test"
    assert ref.sub.codigo == "909000000001"
    assert ref.sub in res.subpartidas
    assert [x.tipo for x in ref.sub.recursos] == ["MO"]


def test_hh_y_rendimientos(res):
    hoja = next(p for p in res.partidas if p.codigo == "01.01")
    hh_calc = sum(r.cantidad for r in hoja.recursos if r.tipo == "MO") * hoja.metrado
    hh_decl = sum(r.hh_totales or 0 for r in hoja.recursos if r.tipo == "MO")
    assert abs(hh_calc - 50.0) < 0.01 and abs(hh_decl - 50.0) < 0.01
    assert hoja.rend_mo == 250.0 and hoja.rend_eq == 200.0


def test_total_presupuesto(res):
    hojas = [p for p in res.partidas if p.es_hoja]
    assert abs(sum(p.cud * p.metrado for p in hojas) - 400.0) < 0.01
    assert abs(sum(p.parcial_meta for p in hojas) - 400.0) < 0.01


def test_archivo_ilegible():
    with pytest.raises(Exception):
        parsear_plantilla_pu(b"esto no es un xls")


# ── Derivados (F1.3, funciones puras) ─────────────────────────
def _rec(tipo, cantidad=0.0, parcial=0.0, sub=None):
    return {"tipo": tipo, "cantidad": cantidad, "parcial": parcial, "sub": sub}


def test_hh_meta_solo_mo_directa():
    recursos = [_rec("MO", cantidad=0.5), _rec("MO", cantidad=0.3),
                _rec("EQ", cantidad=9), _rec("SUB", cantidad=1)]
    assert hh_meta(recursos, 100) == 80.0


def test_costo_unitario_expande_sub():
    sub_recs = [_rec("MO", parcial=2.0), _rec("MAT", parcial=1.0)]
    recursos = [_rec("MO", parcial=1.0), _rec("SUB", cantidad=0.5, parcial=1.5, sub="S1")]
    unit = costo_por_tipo_unitario(recursos, obtener_sub=lambda r: sub_recs)
    assert unit == {"MO": 1.0 + 0.5 * 2.0, "MAT": 0.5 * 1.0}


def test_costo_unitario_sub_sin_resolver_queda_como_sub():
    recursos = [_rec("SUB", cantidad=2, parcial=7.5)]
    assert costo_por_tipo_unitario(recursos, obtener_sub=lambda r: None) == {"SUB": 7.5}


def test_costo_meta_por_fase():
    partidas = [
        {"fase": "11", "metrado": 10, "recursos": [_rec("MO", parcial=2.0)]},
        {"fase": "11", "metrado": 5,  "recursos": [_rec("MAT", parcial=4.0)]},
        {"fase": "12", "metrado": 1,  "recursos": [_rec("MO", parcial=3.0)]},
    ]
    out = costo_meta_por_fase_recurso(partidas)
    assert out == {("11", "MO"): 20.0, ("11", "MAT"): 20.0, ("12", "MO"): 3.0}


def test_costo_meta_fixture(res):
    """Sobre el fixture: MO de 01.01 (1.0) + MO expandida de la SUB (0.5×3.9=1.95)
    + MO de 01.02 (2.0) — por metrado."""
    hojas = [{"fase": p.fase, "metrado": p.metrado,
              "recursos": [{"tipo": r.tipo, "cantidad": r.cantidad,
                            "parcial": r.parcial, "sub": r.sub} for r in p.recursos]}
             for p in res.partidas if p.es_hoja]
    out = costo_meta_por_fase_recurso(
        hojas, obtener_sub=lambda r: [
            {"tipo": x.tipo, "cantidad": x.cantidad, "parcial": x.parcial, "sub": x.sub}
            for x in r["sub"].recursos] if r.get("sub") else None)
    # 01.01 (fase 11): MO 1.0×100 + SUB→MO 0.5×3.9×100 = 295 · EQ 0.05×100 = 5
    assert out[("11", "MO")] == 100.0 + 195.0
    assert out[("11", "EQ")] == 5.0
    # 01.02 (fase 12): MO 2×50
    assert out[("12", "MO")] == 100.0
    # el total por tipos = total del presupuesto
    assert abs(sum(out.values()) - 400.0) < 0.01


def test_ratio_meta():
    assert ratio_meta({"hh_meta": 50, "metrado": 100}) == 0.5
    assert ratio_meta({"hh_meta": 50, "metrado": 0}) is None


# ── Archivo REAL (solo local; validación de la Fase 1 completa) ──
@pytest.mark.skipif(not os.path.exists(REAL), reason="Excel real no disponible (CI)")
def test_archivo_real():
    r = parsear_archivo(REAL)
    hojas = [p for p in r.partidas if p.es_hoja]
    assert r.errores == []
    assert len(hojas) == 153 and len(r.subpartidas) == 62
    refs = [rec for p in hojas + r.subpartidas for rec in p.recursos if rec.tipo == "SUB"]
    assert all(rec.sub is not None for rec in refs) and len(refs) == 124
    total = sum((p.cud or 0) * p.metrado for p in hojas)
    assert abs(total - sum(p.parcial_meta for p in hojas)) < 0.01
    hh = sum(sum(rec.cantidad for rec in p.recursos if rec.tipo == "MO") * p.metrado
             for p in hojas)
    hh_decl = sum(sum(rec.hh_totales or 0 for rec in p.recursos if rec.tipo == "MO")
                  for p in hojas)
    assert abs(hh - hh_decl) < 1.0
