"""
Pruebas de la lógica de Valor Ganado (la pieza crítica del sistema).

Estas pruebas protegen las fórmulas del PF (Performance Factor), el cálculo de
HH ganadas/gastadas y, sobre todo, la regla anti–doble-conteo (manual gana sobre
tareo automático). Son funciones PURAS: no tocan la base de datos, así que corren
en segundos y sin configuración.

Ejecutar:  pytest -v
"""
from datetime import date

import pytest

from routers.valor_ganado import (
    _calcular, _validar_pesos, _semana_de, _agrupar, HitoIn,
    _matriz_area_disciplina, _totales, _calc_costo_mo,
    _HH_POR_CARGO_SQL, _resultado_operativo,
)


# ── Helpers para construir filas como las que entrega la BD (asyncpg) ──
def partida(**kw):
    base = dict(
        id=1, codigo="01", otm_id="OTM1", fase="MEC", sistema="S1",
        descripcion="Montaje", unidad="m",
        metrado_presup=100, metrado_proyec=100, hh_presup=200,
        tipo_costo="DIRECTO", naturaleza="CONTRACTUAL",
    )
    base.update(kw)
    return base


def hito(**kw):
    base = dict(id=10, partida_id=1, numero=1, peso=1.0, es_principal=True)
    base.update(kw)
    return base


def avance(**kw):
    base = dict(hito_id=10, semana=1, cantidad_acum=50)
    base.update(kw)
    return base


# ════════════════════════════════════════════════════════════════
# _calcular — el corazón del Valor Ganado
# ════════════════════════════════════════════════════════════════

def test_calcular_caso_basico():
    """Partida con 50% de avance y 80 HH gastadas manualmente."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={},
        semana=1,
    )
    f = filas[0]
    assert f["hh_proyec"] == 200.0            # 100 m × (200/100) hh/m
    assert f["pct_avance"] == 0.5             # 50 / 100
    assert f["cantidad_instalada"] == 50.0
    assert f["hh_ganadas_acum"] == 100.0      # 0.5 × 200
    assert f["hh_gastadas_acum"] == 80.0
    assert f["pf_acum"] == 1.25               # 100 / 80


def test_calcular_partida_directa_sus_hh_son_directas():
    """Cambio #1: una partida DIRECTO carga todas sus HH como directas."""
    filas = _calcular(
        partidas=[partida(tipo_costo="DIRECTO")],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={},
        semana=1,
    )
    f = filas[0]
    assert f["tipo_costo"] == "DIRECTO"
    assert f["hh_gastadas_dir_acum"] == 80.0
    assert f["hh_gastadas_ind_acum"] == 0.0
    assert f["pf_dir_acum"] == 1.25            # ganadas/gastadas directas


def test_calcular_partida_indirecta_sus_hh_son_indirectas():
    """Cambio #1: una partida INDIRECTO carga todas sus HH como indirectas
    (método del ISP del gerente: indirecto se define por la partida/fase)."""
    filas = _calcular(
        partidas=[partida(tipo_costo="INDIRECTO")],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={},
        semana=1,
    )
    f = filas[0]
    assert f["tipo_costo"] == "INDIRECTO"
    assert f["hh_gastadas_dir_acum"] == 0.0
    assert f["hh_gastadas_ind_acum"] == 80.0
    assert f["pf_dir_acum"] == 0.0             # sin HH directas, PF directo = 0


def test_calcular_tipo_costo_por_defecto_es_directo():
    """Si la partida no trae tipo_costo, se trata como DIRECTO."""
    p = partida(); del p["tipo_costo"]
    filas = _calcular([p], [hito()], [avance(cantidad_acum=50)],
                      [{"partida_id": 1, "semana": 1, "hh": 80}], {}, 1)
    assert filas[0]["hh_gastadas_dir_acum"] == 80.0


def test_calcular_naturaleza_en_salida():
    """Cambio #3: la naturaleza (Contractual/Adicional) sale en cada fila."""
    fc = _calcular([partida(naturaleza="CONTRACTUAL")], [hito()], [avance()], [], {}, 1)
    fa = _calcular([partida(naturaleza="ADICIONAL")], [hito()], [avance()], [], {}, 1)
    assert fc[0]["naturaleza"] == "CONTRACTUAL"
    assert fa[0]["naturaleza"] == "ADICIONAL"


def test_calcular_naturaleza_por_defecto_contractual():
    """Si la partida no trae naturaleza, se trata como CONTRACTUAL."""
    p = partida(); del p["naturaleza"]
    filas = _calcular([p], [hito()], [avance()], [], {}, 1)
    assert filas[0]["naturaleza"] == "CONTRACTUAL"


def test_agrupar_por_naturaleza():
    """El reporte puede agrupar por naturaleza (contractual vs adicional)."""
    filas = [
        {"naturaleza": "CONTRACTUAL", "hh_proyec": 100, "hh_ganadas_acum": 80, "hh_gastadas_acum": 90, "eac_hh": 110},
        {"naturaleza": "ADICIONAL",   "hh_proyec": 50,  "hh_ganadas_acum": 40, "hh_gastadas_acum": 50, "eac_hh": 60},
    ]
    grupos = _agrupar(filas, "naturaleza")
    assert {g["grupo"] for g in grupos} == {"CONTRACTUAL", "ADICIONAL"}


def test_calcular_usa_tareo_automatico_si_no_hay_manual():
    """Sin HH manual, toma las HH del tareo automático."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[],
        tareo={(1, 1): 40.0},
        semana=1,
    )
    f = filas[0]
    assert f["hh_gastadas_acum"] == 40.0
    assert f["pf_acum"] == 2.5                # 100 / 40


def test_calcular_manual_gana_sobre_tareo_sin_doble_conteo():
    """REGLA CRÍTICA: si hay HH manual para (partida, semana), el tareo
    automático NO se suma encima (evita el doble conteo)."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={(1, 1): 40.0},   # debe IGNORARSE
        semana=1,
    )
    f = filas[0]
    assert f["hh_gastadas_acum"] == 80.0      # NO 120 — no se duplica


def test_calcular_avance_se_topa_en_100_por_ciento():
    """Aunque la cantidad supere el metrado, el % de avance no pasa de 100%."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=150)],   # 150 > 100 metrado
        hh_rows=[{"partida_id": 1, "semana": 1, "hh": 80}],
        tareo={},
        semana=1,
    )
    f = filas[0]
    assert f["pct_avance"] == 1.0
    assert f["hh_ganadas_acum"] == 200.0       # tope = hh_proyec


def test_calcular_pf_cero_si_no_hay_hh_gastadas():
    """Sin HH gastadas, el PF es 0 (no división por cero)."""
    filas = _calcular(
        partidas=[partida()],
        hitos=[hito()],
        avances=[avance(cantidad_acum=50)],
        hh_rows=[],
        tareo={},
        semana=1,
    )
    assert filas[0]["pf_acum"] == 0.0


def test_calcular_dos_hitos_ponderados():
    """Dos hitos con pesos 0.4 / 0.6: el % de avance los combina."""
    hitos = [
        hito(id=10, numero=1, peso=0.4, es_principal=False),
        hito(id=11, numero=2, peso=0.6, es_principal=True),
    ]
    avances = [
        {"hito_id": 10, "semana": 1, "cantidad_acum": 100},   # hito 1 al 100%
        {"hito_id": 11, "semana": 1, "cantidad_acum": 50},    # hito 2 al 50%
    ]
    filas = _calcular([partida()], hitos, avances, [], {}, 1)
    # pct = 0.4×1.0 + 0.6×0.5 = 0.7
    assert filas[0]["pct_avance"] == 0.7


# ════════════════════════════════════════════════════════════════
# _validar_pesos — los hitos deben sumar 1.00 con un solo principal
# ════════════════════════════════════════════════════════════════

def test_validar_pesos_ok():
    _validar_pesos([
        HitoIn(numero=1, peso=0.6, es_principal=True),
        HitoIn(numero=2, peso=0.4, es_principal=False),
    ])  # no lanza


def test_validar_pesos_no_suman_uno():
    with pytest.raises(Exception):
        _validar_pesos([HitoIn(numero=1, peso=0.5, es_principal=True)])


def test_validar_pesos_dos_principales():
    with pytest.raises(Exception):
        _validar_pesos([
            HitoIn(numero=1, peso=0.5, es_principal=True),
            HitoIn(numero=2, peso=0.5, es_principal=True),
        ])


def test_validar_pesos_numeros_repetidos():
    with pytest.raises(Exception):
        _validar_pesos([
            HitoIn(numero=1, peso=0.5, es_principal=True),
            HitoIn(numero=1, peso=0.5, es_principal=False),
        ])


# ════════════════════════════════════════════════════════════════
# _semana_de — mapeo fecha → número de semana del proyecto
# ════════════════════════════════════════════════════════════════

def test_semana_de():
    base = date(2026, 1, 1)
    assert _semana_de(date(2026, 1, 1), base) == 1
    assert _semana_de(date(2026, 1, 8), base) == 2
    assert _semana_de(date(2026, 1, 15), base) == 3


# ════════════════════════════════════════════════════════════════
# _agrupar — totales por fase/OTM/sistema
# ════════════════════════════════════════════════════════════════

def test_agrupar_suma_por_fase():
    filas = [
        {"fase": "MEC", "hh_proyec": 100, "hh_ganadas_acum": 80, "hh_gastadas_acum": 90, "eac_hh": 110},
        {"fase": "MEC", "hh_proyec": 50,  "hh_ganadas_acum": 40, "hh_gastadas_acum": 50, "eac_hh": 60},
    ]
    grupos = _agrupar(filas, "fase")
    assert len(grupos) == 1
    g = grupos[0]
    assert g["grupo"] == "MEC"
    assert g["hh_proyec"] == 150.0
    assert g["hh_ganadas"] == 120.0
    assert g["hh_gastadas"] == 140.0
    assert g["pct_avance"] == 0.8             # 120 / 150
    assert g["pf"] == 0.857                    # round(120 / 140, 3)


# ════════════════════════════════════════════════════════════════
# #6 — % avance del proyecto = ganadas / presupuesto ACTUALIZADO
# ════════════════════════════════════════════════════════════════

def test_calcular_emite_hh_actualizado_default():
    """Si la partida no trae hh_actualizado, el motor cae a hh_presup."""
    filas = _calcular([partida(hh_presup=200)], [hito()], [avance()], [], {}, 1)
    assert filas[0]["hh_actualizado"] == 200.0


def test_calcular_emite_hh_actualizado_explicito():
    """Si la partida trae hh_actualizado, se respeta (≠ hh_presup)."""
    filas = _calcular([partida(hh_presup=200, hh_actualizado=250)], [hito()], [avance()], [], {}, 1)
    assert filas[0]["hh_actualizado"] == 250.0


def test_totales_pct_avance_usa_presupuesto_actualizado():
    """Golden ISP Sem34: % avance del proyecto = ganadas / actualizado (no proyectada).
    Resumen: ganadas 10175.82 / actualizado 140740.18 = 0.0723 (J26).
    PF productivo del proyecto = 10175.82 / 12819.5 = 0.7938 (L27)."""
    hoja = {
        "hh_proyec": 156048.47, "hh_presup": 138793.39, "hh_actualizado": 140740.18,
        "hh_ganadas_acum": 10175.82, "hh_gastadas_acum": 12819.5,
        "hh_gastadas_dir_acum": 12819.5, "hh_gastadas_ind_acum": 0,
        "hh_ganadas_sem": 0, "hh_gastadas_sem": 0, "eac_hh": 0,
    }
    t = _totales([hoja], improd_acum=0.0)
    assert t["pct_avance"] == 0.0723                 # ganadas / actualizado (método del gerente)
    assert t["pct_avance_proyec"] == 0.0652          # ganadas / proyectada (referencia, distinto)
    assert t["pf_acum_productivo"] == 0.794          # 10175.82 / 12819.5
    # con presupuesto ORIGINAL daría otro número (demuestra que el denominador importa)
    assert round(10175.82 / 138793.39, 4) == 0.0733


# ════════════════════════════════════════════════════════════════
# #5 — HH improductivas: suman al consumo y bajan el PF del proyecto
# ════════════════════════════════════════════════════════════════

def test_totales_improductivas_bajan_pf_del_proyecto():
    hoja = {
        "hh_proyec": 156048.47, "hh_presup": 138793.39, "hh_actualizado": 140740.18,
        "hh_ganadas_acum": 10175.82, "hh_gastadas_acum": 12819.5,
        "hh_gastadas_dir_acum": 12819.5, "hh_gastadas_ind_acum": 0,
        "hh_ganadas_sem": 0, "hh_gastadas_sem": 0, "eac_hh": 0,
    }
    sin = _totales([hoja], improd_acum=0.0)
    con = _totales([hoja], improd_acum=1000.0)
    # el titular de PF incluye improductivas y baja respecto al productivo
    assert con["pf_acum"] < con["pf_acum_productivo"]
    assert con["pf_acum_productivo"] == sin["pf_acum"]           # productivo no cambia
    assert con["hh_consumidas_acum"] == 13819.5                  # 12819.5 + 1000
    assert con["index_improductividad"] == round(1000 / 13819.5, 4)
    assert con["pf_acum"] == round(10175.82 / 13819.5, 3)


# ════════════════════════════════════════════════════════════════
# Matriz Área × Disciplina (hoja "Resumen Ejecutivo" del gerente)
# ════════════════════════════════════════════════════════════════

def _hoja_disc(sistema, fase, contractual, proyec, ganadas, gastadas):
    return {
        "sistema": sistema, "fase": fase, "hh_presup": contractual,
        "hh_proyec": proyec, "hh_ganadas_acum": ganadas,
        "hh_gastadas_acum": gastadas, "eac_hh": 0,
    }


def test_matriz_sem34_sistema1():
    """Golden ISP Sem34 'Resumen Ejecutivo' — Sistema 1: Lauder 3B.
    MOV01: contractual 4245.196 · proyec 4588.736 · ganadas 3202.877 · gastadas 4318
      ⇒ % avance 0.698 · PF 0.742
    Subtotal Sistema 1: proyec 12770.40 · ganadas 3811.806 ⇒ % avance 0.2985."""
    s1 = "Sistema 1: Lauder 3B"
    hojas = [
        _hoja_disc(s1, "Movimiento de Tierras", 4245.196, 4588.736, 3202.877, 4318),
        _hoja_disc(s1, "Concreto",              866.766, 1515.771, 0, 0),
        _hoja_disc(s1, "Encofrado",             2431,    2694.273, 0, 0),
        _hoja_disc(s1, "Acero",                 2106.369, 2435.718, 608.929, 567),
        _hoja_disc(s1, "Instalación de Anclajes", 928.185, 1535.902, 0, 0),
    ]
    m = _matriz_area_disciplina(hojas)
    assert len(m["areas"]) == 1
    area = m["areas"][0]
    assert area["area"] == s1
    assert len(area["disciplinas"]) == 5

    mov = next(d for d in area["disciplinas"] if d["disciplina"] == "Movimiento de Tierras")
    assert mov["hh_contractual"] == 4245.2
    assert mov["pct_avance"] == 0.698          # 3202.877 / 4588.736
    assert mov["pf"] == 0.742                   # 3202.877 / 4318

    sub = area["subtotal"]
    assert sub["hh_proyec"] == 12770.4
    assert sub["hh_ganadas"] == 3811.81
    assert sub["pct_avance"] == 0.2985          # 3811.806 / 12770.40


def test_calc_costo_mo_por_cargo():
    """Costo MO = Σ HH×tarifa; cargos sin tarifa NI respaldo quedan marcados."""
    hh = {"Operario": 100, "Peon": 200, "Capataz": 50}
    tar = {"Operario": 30.0, "Peon": 20.0}   # Capataz sin tarifa
    costo, hh_sin = _calc_costo_mo(hh, tar, default=None)   # sin respaldo
    assert costo == 100 * 30 + 200 * 20 + 0   # 7000
    assert hh_sin == 50                        # las HH de Capataz quedan marcadas


def test_calc_costo_mo_usa_default():
    """Con tarifa de respaldo, los cargos sin tarifa propia la usan y no quedan sin contar."""
    hh = {"Operario": 100, "Capataz": 50}
    tar = {"Operario": 30.0}
    costo, hh_sin = _calc_costo_mo(hh, tar, default=15.0)
    assert costo == 100 * 30 + 50 * 15         # 3750
    assert hh_sin == 0


def test_calc_costo_mo_tarifa_cero_explicita_se_respeta():
    """Una tarifa 0.0 explícita (subcontrata/no facturable) cuesta 0 y NO se marca."""
    hh = {"Operario": 100, "Subcontrata": 50}
    tar = {"Operario": 30.0, "Subcontrata": 0.0}   # 0 a propósito
    costo, hh_sin = _calc_costo_mo(hh, tar, default=99.0)
    assert costo == 100 * 30 + 0               # NO usa el default para Subcontrata
    assert hh_sin == 0                          # 0 explícito ≠ "sin tarifa"


def test_calc_costo_mo_default_cero_explicito_no_marca():
    """Default=0.0 explícito ⇒ los cargos sin tarifa cuestan 0 y no se marcan."""
    hh = {"Operario": 100, "Capataz": 50}
    tar = {"Operario": 30.0}
    costo, hh_sin = _calc_costo_mo(hh, tar, default=0.0)
    assert costo == 100 * 30
    assert hh_sin == 0


# ── Resultado operativo (rentabilidad): unión de OTMs + cuadre HH/costo ──
def _otm(desc, valorizado, contractual=0):
    return {"descripcion": desc, "monto_valorizado": valorizado, "monto_contractual": contractual}


def test_resultado_incluye_otm_con_ingreso_sin_tareo():
    """🔴 Una OTM con ingreso valorizado pero sin tareo SÍ aparece y suma al total."""
    por_otm = {"OTM-A": {"Operario": 100}}                 # solo A tiene tareo
    tar = {"Operario": 30.0}
    otm_info = {"OTM-A": _otm("A", 5000), "OTM-B": _otm("B", 8000)}  # B sin tareo
    out, total = _resultado_operativo(por_otm, tar, None, otm_info)
    otms = {r["otm"]: r for r in out}
    assert "OTM-B" in otms                                  # antes desaparecía
    assert otms["OTM-B"]["hh_total"] == 0
    assert otms["OTM-B"]["costo_mo"] == 0
    assert otms["OTM-B"]["ingreso_valorizado"] == 8000
    assert total["ingreso_valorizado"] == 13000            # 5000 + 8000 (no se pierde)


def test_resultado_excluye_otm_sin_ingreso_ni_tareo():
    """Una OTM sin tareo y sin ingreso no ensucia la tabla."""
    por_otm = {"OTM-A": {"Operario": 100}}
    otm_info = {"OTM-A": _otm("A", 5000), "OTM-Z": _otm("Z", 0)}
    out, _ = _resultado_operativo(por_otm, {"Operario": 30.0}, None, otm_info)
    assert {r["otm"] for r in out} == {"OTM-A"}


def test_resultado_hh_y_costo_cuadran_y_son_auditables():
    """🟡 HH entera por cargo: el total cuadra con la suma y el costo es auditable."""
    por_otm = {"OTM-A": {"Operario": 142.6, "Peon": 10.4}}   # con decimales
    tar = {"Operario": 30.0, "Peon": 20.0}
    out, total = _resultado_operativo(por_otm, tar, None, {"OTM-A": _otm("A", 0)})
    fila = out[0]
    assert fila["hh_total"] == 143 + 10                      # suma de HH enteras
    assert fila["costo_mo"] == 143 * 30 + 10 * 20            # HH×tarifa exacto
    assert total["hh_total"] == fila["hh_total"]


def _group_by_clause(sql: str) -> str:
    """Extrae el texto del GROUP BY (hasta el fin) para inspeccionarlo."""
    return sql.upper().split("GROUP BY", 1)[1]


def test_hh_por_cargo_sql_tarifas_sin_otm():
    """/tarifas formatea sin columna OTM y produce un GROUP BY válido."""
    sql = _HH_POR_CARGO_SQL.format(sel="", grpby="")
    assert "TP.OTM_ID" not in sql.upper()
    # un GROUP BY no puede contener un alias `AS` — sería sintaxis inválida
    assert " AS " not in _group_by_clause(sql)


def test_matriz_dos_areas_y_total():
    """Dos áreas con una disciplina cada una: total general suma ambas."""
    hojas = [
        _hoja_disc("A1", "Acero", 100, 120, 60, 50),
        _hoja_disc("A2", "Concreto", 200, 240, 120, 100),
    ]
    m = _matriz_area_disciplina(hojas)
    assert [a["area"] for a in m["areas"]] == ["A1", "A2"]
    assert m["total"]["hh_proyec"] == 360.0
    assert m["total"]["hh_ganadas"] == 180.0
    assert m["total"]["pct_avance"] == 0.5      # 180 / 360
    # incidencia proyectada de A1 = 120 / 360
    a1 = m["areas"][0]["disciplinas"][0]
    assert a1["inc_proyec"] == round(120 / 360, 4)
