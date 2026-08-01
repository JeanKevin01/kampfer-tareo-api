"""
El denominador del PPC congelado, y la bitácora que lo audita (migración 0041).

Cierra D1 y D2 del plan maestro v8. El defecto, en una frase: `_redistribuir()`
borra las celdas de `prog_metrado_dia` sin filtro de fecha, así que correr la
F.Inicio de una actividad que NO se hizo la dejaba con comprometido 0 y la sacaba
del PPC. Traducido a conducta: *si no cumpliste, muévela y el indicador se limpia
solo* — que es exactamente lo contrario de lo que el Last Planner mide.

0036 salvó las semanas cerradas y 0040 congeló QUÉ se comprometió; aquí se
congela el CUÁNTO, que es lo que faltaba para que el denominador de una semana
comprometida deje de depender del plan de hoy.

Todo lo de aquí es puro: son las reglas, sin BD. El humo de extremo a extremo
(comprometer → mover la fecha → el PPC no se mueve) vive en el E2E, bloque F-D1.
"""
import pytest
from fastapi import HTTPException

from routers.prog_cierre import (EVENTOS, denominador_comprometido,
                                 origen_denominador, resumir_historial, veredicto)


# ── El denominador: qué manda cuando la semana está comprometida ─────────
def test_sin_compromiso_manda_el_plan_vigente():
    """Semana nunca comprometida: el comportamiento de siempre."""
    assert denominador_comprometido({1: 10.0, 2: 5.0}, None) == {1: 10.0, 2: 5.0}


def test_con_compromiso_manda_el_congelado():
    """Aunque el plan de hoy diga otra cosa: es lo que se prometió."""
    assert denominador_comprometido({1: 3.0}, {1: 10.0}) == {1: 10.0}


def test_reprogramar_no_borra_el_compromiso():
    """EL CASO D1. La actividad 1 se comprometió con 10; el planner corrió su
    F.Inicio a la semana siguiente y `_redistribuir` le borró las celdas, así
    que el plan vigente ya no la tiene. Antes salía del denominador (PPC 100%);
    ahora sigue valiendo 10 y se juzgará como no cumplida."""
    vigente = {}                       # sin celdas: se las llevó la reprogramación
    assert denominador_comprometido(vigente, {1: 10.0}) == {1: 10.0}


def test_programar_hacia_atras_no_entra_al_denominador():
    """EL CASO D2. La actividad 2 se creó y programó DESPUÉS de comprometer la
    semana. Sigue en la lista —hay que poder contarla como trabajo no
    planificado y cobrarle sus HH— pero con su metrado de hoy, no como
    compromiso: quien la saca del PPC es `marcar_no_planificadas`."""
    d = denominador_comprometido({1: 10.0, 2: 7.0}, {1: 10.0})
    assert d == {1: 10.0, 2: 7.0}
    assert 2 not in {1: 10.0}          # no está en el compromiso → no planificada


def test_metrado_congelado_cero_cae_al_vigente():
    """Compromisos anteriores a 0041: `metrado` quedó en 0 y NO significa «se
    prometió cero». Sin esta salida, todas las semanas ya comprometidas se
    quedarían sin denominador el día del despliegue."""
    assert denominador_comprometido({1: 8.0}, {1: 0.0}) == {1: 8.0}


def test_metrado_cero_sin_celdas_sigue_en_cero():
    """Actividad de apoyo comprometida: no tiene metrado en ningún lado. Se
    conserva con 0 — se juzga por estado, en otra rama del PPC."""
    assert denominador_comprometido({}, {1: 0.0}) == {1: 0.0}


def test_compromiso_vacio_no_es_lo_mismo_que_no_comprometer():
    """Dict vacío = «se comprometió la semana sin nada»; None = «nunca se
    comprometió». Confundirlos haría que una semana comprometida en blanco
    heredara todo el plan vigente."""
    assert denominador_comprometido({1: 4.0}, {}) == {1: 4.0}   # entra como no planificada
    assert denominador_comprometido({1: 4.0}, None) == {1: 4.0}


def test_no_muta_las_entradas():
    vig, cong = {1: 3.0}, {1: 10.0}
    denominador_comprometido(vig, cong)
    assert vig == {1: 3.0} and cong == {1: 10.0}


def test_decimales_de_la_bd_salen_como_float():
    """asyncpg devuelve NUMERIC como Decimal; el resto del PPC compara con
    floats y `Decimal >= float` explota."""
    from decimal import Decimal
    d = denominador_comprometido({1: Decimal("3.5")}, {1: Decimal("10.25")})
    assert d == {1: 10.25}
    assert all(isinstance(v, float) for v in d.values())


# ── El rótulo: contra qué plan se está midiendo ──────────────────────────
def test_origen_vigente_comprometido_cerrada():
    assert origen_denominador(None, False) == "VIGENTE"
    assert origen_denominador({1: 5.0}, False) == "COMPROMETIDO"
    assert origen_denominador({1: 5.0}, True) == "CERRADA"


def test_cerrada_manda_sobre_comprometido():
    """Una semana cerrada ya no se recalcula ni contra el compromiso."""
    assert origen_denominador(None, True) == "CERRADA"


# ── Cancelar tampoco puede limpiar el indicador ──────────────────────────
def test_cancelar_una_comprometida_es_incumplir():
    """La otra puerta del mismo defecto: si cancelar sacara la actividad del
    PPC, «no lo hice → la cancelo» tendría el mismo efecto que mover la fecha."""
    assert veredicto(10.0, 0.0, "CANCELADO") is False
    assert veredicto(10.0, 12.0, "CANCELADO") is False   # ni con avance ajeno
    assert veredicto(0.0, 0.0, "CANCELADO") is False


def test_cancelado_no_desplaza_al_resto_del_veredicto():
    assert veredicto(10.0, 10.0, "PROGRAMADO") is True
    assert veredicto(10.0, 3.0, "EJECUTADO") is False    # manda el metrado
    assert veredicto(0.0, 0.0, "EJECUTADO") is True      # sin metrado, el estado


# ── La bitácora ──────────────────────────────────────────────
def _ev(evento, ppc=None):
    return {"evento": evento, "ppc": ppc}


def test_resumen_de_una_semana_normal():
    r = resumir_historial([_ev("COMPROMETIDA"), _ev("CERRADA", 0.8)])
    assert r["veces_comprometida"] == 1 and r["veces_cerrada"] == 1
    assert r["reaperturas"] == 0
    assert r["ppc_primero"] == 0.8 and r["ppc_ultimo"] == 0.8
    assert r["ppc_cambio"] is None          # nunca cambió: no hay nada que avisar


def test_resumen_delata_un_ppc_que_cambio():
    """El caso que la bitácora existe para responder: se publicó 60%, alguien
    reabrió, registró avance y volvió a cerrar en 90%. El número de hoy no es el
    que se entregó, y eso tiene que verse."""
    r = resumir_historial([
        _ev("COMPROMETIDA"), _ev("CERRADA", 0.6),
        _ev("REABIERTA", 0.6), _ev("CERRADA", 0.9)])
    assert r["reaperturas"] == 1 and r["veces_cerrada"] == 2
    assert r["ppc_primero"] == 0.6 and r["ppc_ultimo"] == 0.9
    assert r["ppc_cambio"] == 0.3


def test_resumen_cuenta_los_descompromisos():
    """Descomprometer → reprogramar → volver a comprometer es la vía más
    directa para maquillar el indicador. Queda contada."""
    r = resumir_historial([
        _ev("COMPROMETIDA"), _ev("DESCOMPROMETIDA"), _ev("COMPROMETIDA")])
    assert r["descompromisos"] == 1 and r["veces_comprometida"] == 2
    assert r["ppc_primero"] is None


def test_resumen_vacio():
    r = resumir_historial([])
    assert r["eventos"] == 0 and r["ppc_cambio"] is None


def test_resumen_ignora_eventos_desconocidos():
    """Si un día se agrega un evento nuevo, el resumen no debe reventar."""
    r = resumir_historial([_ev("COMPROMETIDA"), {"evento": "ALGO_NUEVO"}])
    assert r["eventos"] == 2 and r["veces_comprometida"] == 1


def test_catalogo_de_eventos_cubre_los_cuatro_actos():
    """Los cuatro que pueden mover un indicador ya publicado. El CHECK de la
    migración 0041 tiene esta misma lista: si divergen, el INSERT falla en
    producción y no en los tests."""
    assert set(EVENTOS) == {"COMPROMETIDA", "DESCOMPROMETIDA", "CERRADA", "REABIERTA"}


def test_catalogo_coincide_con_el_check_de_la_migracion():
    import re
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "versions"
           / "0041_metrado_comprometido.py").read_text(encoding="utf-8")
    m = re.search(r"evento IN \(([^)]+)\)", sql)
    assert m, "la migración ya no declara el CHECK de eventos"
    assert set(re.findall(r"'([A-Z_]+)'", m.group(1))) == set(EVENTOS)


# ── Contrato del endpoint (sin BD) ───────────────────────────
def test_historial_rechaza_lunes_invalido():
    from routers.prog_cierre import _lunes_arg
    with pytest.raises(HTTPException) as e:
        _lunes_arg("no-es-fecha")
    assert e.value.status_code == 400
