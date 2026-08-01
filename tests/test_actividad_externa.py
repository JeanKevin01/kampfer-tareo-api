"""
Trabajo de terceros en el LookAhead (migración 0042).

El caso de Jean: otra empresa da su plazo —«el montaje nos toma 10 días»— y de
eso dependen actividades nuestras. Se anota como una fila normal del LookAhead
para que arrastre fechas con los vínculos que ya existen (FS/SS/FF + lag, días
medios, saltos), pero marcada: **no es un compromiso nuestro y no entra al PPC**.

Lo que estos tests fijan es la frontera. La marca vive en una COLUMNA y no en el
título —Jean escribe la empresa en el nombre, que es lo cómodo de leer— porque
de ella depende que la fila quede fuera del indicador, y deducir eso del texto
sería frágil: el día que el nombre se escriba distinto, un compromiso ajeno se
cuela en el PPC propio sin que nadie lo note.
"""
import pytest
from fastapi import HTTPException

from routers.programacion import _exigir_partida, _normalizar_externa


# ── Fila propia: nada cambia ─────────────────────────────────
def test_actividad_propia_pasa_intacta():
    ext, emp, met, pid, sup = _normalizar_externa({}, 25.0, 7, "S01")
    assert ext is False and emp is None
    assert (met, pid, sup) == (25.0, 7, "S01")


def test_empresa_se_puede_anotar_sin_ser_externa():
    """Anotar la empresa no marca la fila: son dos cosas distintas. Un
    subcontratista que ejecuta UNA PARTIDA NUESTRA sigue siendo trabajo nuestro
    —el metrado y las HH son nuestras— y debe contar en el PPC."""
    ext, emp, met, pid, _ = _normalizar_externa(
        {"empresa": "ELECTRO SAC"}, 25.0, 7)
    assert ext is False and emp == "ELECTRO SAC"
    assert (met, pid) == (25.0, 7)


# ── Fila de terceros ─────────────────────────────────────────
def test_externa_sin_metrado_ni_partida():
    ext, emp, met, pid, sup = _normalizar_externa(
        {"externa": True, "empresa": "  ELECTRO SAC  "}, None, None, "S01")
    assert ext is True and emp == "ELECTRO SAC"       # recortada
    assert met is None and pid is None
    # El supervisor se descarta en silencio: nuestro supervisor no tarea trabajo
    # ajeno, y rechazarlo con 400 sería pedantería (la UI lo arrastra del form).
    assert sup is None


def test_externa_con_partida_es_400():
    """El error que importa: si una fila externa colgara de una partida, su
    avance entraría a NUESTRO valor ganado."""
    with pytest.raises(HTTPException) as e:
        _normalizar_externa({"externa": True}, None, 7)
    assert e.value.status_code == 400
    assert "partida" in str(e.value.detail).lower()


def test_externa_con_metrado_es_400():
    """Lo que se anota de un tercero es cuánto TARDA, no cuánto produce."""
    with pytest.raises(HTTPException) as e:
        _normalizar_externa({"externa": True}, 30.0, None)
    assert e.value.status_code == 400
    assert "metrado" in str(e.value.detail).lower()


def test_externa_sin_empresa_es_valida():
    """La empresa es opcional: Jean la escribe en el título. El campo aparte
    existe solo para poder agrupar («¿cuántos días nos corrió esta empresa?»)."""
    ext, emp, _m, _p, _s = _normalizar_externa({"externa": True}, None, None)
    assert ext is True and emp is None


def test_empresa_vacia_es_none_no_cadena():
    """Una cadena vacía en la BD partiría el agrupado en dos: '' y NULL."""
    for v in ("", "   ", None):
        _e, emp, _m, _p, _s = _normalizar_externa({"externa": True, "empresa": v}, None, None)
        assert emp is None


def test_empresa_se_recorta_a_80():
    _e, emp, _m, _p, _s = _normalizar_externa(
        {"externa": True, "empresa": "X" * 200}, None, None)
    assert len(emp) == 80


def test_externa_admite_metrado_cero_y_falsy():
    """0 y None son «sin metrado», no un metrado que haya que rechazar."""
    for v in (None, 0, 0.0):
        ext, _e, met, _p, _s = _normalizar_externa({"externa": True}, v, None)
        assert ext is True and met is None


# ── La regla convive con la que ya existía ───────────────────
def test_una_fila_sin_metrado_sigue_sin_necesitar_partida():
    """`_exigir_partida` ya permitía la actividad de apoyo sin metrado (reunión,
    traslado). La fila externa entra por esa misma puerta — lo que 0042 añade es
    sacarla del PPC, no permitir su existencia."""
    _exigir_partida(None, None)          # no levanta


def test_metrado_sin_partida_sigue_prohibido_en_filas_propias():
    with pytest.raises(HTTPException) as e:
        _exigir_partida(25.0, None)
    assert e.value.status_code == 400


def test_el_check_de_la_migracion_dice_lo_mismo_que_el_router():
    """Si el router y la BD divergen, la combinación prohibida se cuela por
    cualquier ruta futura que no pase por `_normalizar_externa` — o revienta con
    un 500 mudo en producción. La migración es la última línea de defensa."""
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "versions"
           / "0042_actividad_externa.py").read_text(encoding="utf-8")
    assert "prog_act_externa_sin_metrado" in sql
    assert "partida_id IS NULL" in sql
    assert "COALESCE(metrado_prog, 0) = 0" in sql
