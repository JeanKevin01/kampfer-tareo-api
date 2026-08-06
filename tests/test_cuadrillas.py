"""Cuadrillas libres (0046→0048) — normalización, validación e identidad.

El bug que originó este módulo no era de lógica: el panel escribía en la tabla
`cuadrillas` y el tareo leía `cuadrilla_otm`, dos circuitos que nunca se
cruzaban. Ningún test unitario podía cazar eso —cada mitad funcionaba— así que
la prueba que de verdad protege es el bloque F-CUADRILLA del E2E: guardar por
el endpoint del PANEL y leer por el del CAMPO. Aquí quedan las piezas que sí se
pueden verificar sin BD.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import auth, config
from core.ids import ids_unicos, norm_trab_id
from routers.tareo import _ensamblar_cuadrillas, _nombre_cuadrilla, _ya_existe
import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _hdr(rol: str, sup_id: str = None):
    extra = {"sup_id": sup_id} if sup_id else None
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol, extra=extra)}


@pytest.fixture(autouse=True)
def _modo_prod(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")


# ── Normalización de ids ─────────────────────────────────────
# El panel guardaba '007' (zfill) y el teléfono '7' (str a secas): la misma
# persona en dos filas distintas de la misma cuadrilla.
def test_id_corto_se_rellena():
    assert norm_trab_id("7") == "007"


def test_id_largo_no_se_toca():
    assert norm_trab_id("1234567") == "1234567"


def test_id_con_espacios():
    assert norm_trab_id("  12 ") == "012"


def test_vacio_no_se_convierte_en_000():
    """'' .zfill(3) da '000', que es el id legítimo de OTRA persona."""
    assert norm_trab_id("") == ""
    assert norm_trab_id(None) == ""


def test_ids_unicos_conserva_el_orden():
    """El orden ES el dato: se guarda como `orden` y es el que ve el supervisor."""
    assert ids_unicos(["3", "1", "2"]) == ["003", "001", "002"]


def test_ids_unicos_deduplica_tras_normalizar():
    assert ids_unicos(["7", "007", "  7"]) == ["007"]


def test_ids_unicos_descarta_vacios():
    assert ids_unicos(["1", "", None, "2"]) == ["001", "002"]


def test_ids_unicos_tolera_none():
    assert ids_unicos(None) == []


# ── Nombre de la cuadrilla ───────────────────────────────────
def test_nombre_colapsa_espacios():
    assert _nombre_cuadrilla("  Cuadrilla   de   excavación ") == "Cuadrilla de excavación"


def test_nombre_se_recorta_a_100():
    assert len(_nombre_cuadrilla("x" * 300)) == 100


def test_nombre_vacio_422():
    for v in ("", "   ", None):
        with pytest.raises(HTTPException) as e:
            _nombre_cuadrilla(v)
        assert e.value.status_code == 422


# ── Identidad: nadie toca la cuadrilla de otro ───────────────
def test_crear_cuadrilla_ajena_403():
    r = _client().post("/api/cuadrillas/02", json={"nombre": "X"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_guardar_plantilla_ajena_403():
    """El agujero que tenía el endpoint de campo: el supervisor_id venía en el
    cuerpo y nadie comprobaba que fuera el del token."""
    r = _client().post("/ev/cuadrillas-plantilla",
                       json={"supervisor_id": "02", "nombre": "X", "trabajadores": []},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_guardar_plantilla_propia_pasa():
    r = _client().post("/ev/cuadrillas-plantilla",
                       json={"supervisor_id": "01", "nombre": "X", "trabajadores": []},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code not in (401, 403)


def test_oficina_gestiona_cualquier_cuadrilla():
    r = _client().post("/api/cuadrillas/02", json={"nombre": "X"}, headers=_hdr("oficina"))
    assert r.status_code not in (401, 403)


# ── Cuadrillas libres (0048) ─────────────────────────────────
# La cuadrilla no es de nadie: es una lista preestablecida para que el registro
# diario sea de un toque. Por eso cualquier supervisor puede editar cualquiera
# —antes se exigía ser el dueño, y ese dueño ya no existe— y solo oficina crea
# desde el catálogo.
def test_supervisor_edita_cualquier_cuadrilla():
    r = _client().patch("/api/cuadrilla-grupo/1", json={"nombre": "Encofrado"},
                        headers=_hdr("supervisor", "01"))
    assert r.status_code not in (401, 403)


def test_supervisor_duplica():
    r = _client().post("/api/cuadrilla-grupo/1/duplicar", json={"nombre": "Otra"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code not in (401, 403)


def test_supervisor_no_crea_en_el_catalogo():
    """El alta suelta sigue siendo de oficina; el supervisor crea la suya por
    /api/cuadrillas/{sup}, que deja constancia de quién la armó."""
    r = _client().post("/api/cuadrillas", json={"nombre": "X"},
                       headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_patch_sin_nombre_422():
    r = _client().patch("/api/cuadrilla-grupo/1", json={}, headers=_hdr("oficina"))
    assert r.status_code == 422


def test_mensaje_de_choque_es_unico():
    """El nombre es único en toda la empresa: ya no hay «el tuyo» y «el mío»."""
    assert "Encofrado" in _ya_existe("Encofrado")


# ── Ensamblado de filas planas → cuadrillas ──────────────────
def _fila(**kw):
    base = {"id": 1, "nombre": "Encofrado", "activo": True, "creado_en": None,
            "creada_por": None, "creada_por_nombre": None, "habitual": False,
            "trab_id": None, "orden": None, "trab_nombre": None, "cargo": None,
            "en_cuantas": None}
    base.update(kw)
    return base


def test_ensamblar_agrupa_y_ordena():
    filas = [
        _fila(trab_id="002", orden=0, trab_nombre="B", cargo="OFICIAL", en_cuantas=1),
        _fila(trab_id="001", orden=1, trab_nombre="A", cargo="PEON", en_cuantas=1),
    ]
    [c] = _ensamblar_cuadrillas(filas)
    assert c["total"] == 2
    assert [m["trab_id"] for m in c["miembros"]] == ["002", "001"]


def test_ensamblar_conserva_la_cuadrilla_vacia():
    """El LEFT JOIN de un grupo sin miembros trae una fila con todo en NULL: la
    cuadrilla tiene que seguir apareciendo, vacía, no desaparecer."""
    [c] = _ensamblar_cuadrillas([_fila()])
    assert c["total"] == 0 and c["miembros"] == []


def test_ensamblar_descarta_al_dado_de_baja():
    """trab_id con trab_nombre NULL = el JOIN filtró por activo: ya no puede tarear."""
    [c] = _ensamblar_cuadrillas([_fila(trab_id="009", orden=0, en_cuantas=1)])
    assert c["total"] == 0


def test_en_otras_descuenta_la_propia():
    filas = [_fila(trab_id="001", orden=0, trab_nombre="A", cargo="PEON", en_cuantas=3)]
    [c] = _ensamblar_cuadrillas(filas)
    assert c["miembros"][0]["en_otras"] == 2


def test_en_otras_nunca_es_negativo():
    """Si el conteo llegara en 0 o NULL, restar 1 daría «-1 cuadrillas»."""
    for n in (None, 0):
        filas = [_fila(trab_id="001", orden=0, trab_nombre="A", cargo="PEON", en_cuantas=n)]
        [c] = _ensamblar_cuadrillas(filas)
        assert c["miembros"][0]["en_otras"] == 0


# ── Cuadrillas habituales (0049) ─────────────────────────────
# Cuáles usa NORMALMENTE cada supervisor: le salen arriba y aparte en el
# teléfono. No es propiedad ni permiso — la lista entera se sigue viendo.
def test_habitual_se_propaga_al_ensamblar():
    [c] = _ensamblar_cuadrillas([_fila(habitual=True)])
    assert c["habitual"] is True


def test_habitual_es_bool_aunque_falte_la_columna():
    """El catálogo sin supervisor no la trae; el panel no debe recibir None."""
    fila = _fila()
    del fila["habitual"]
    [c] = _ensamblar_cuadrillas([fila])
    assert c["habitual"] is False


def test_fijar_habituales_ajenas_403():
    r = _client().put("/api/supervisor/02/cuadrillas-habituales",
                      json={"grupos": [1]}, headers=_hdr("supervisor", "01"))
    assert r.status_code == 403


def test_fijar_habituales_sin_lista_422():
    """Sin la lista no se distingue «déjalo vacío» de «no me la mandes»: con un
    reemplazo eso es la diferencia entre borrarlas todas y no tocar nada."""
    for cuerpo in ({}, {"grupos": None}, {"grupos": "1,2"}):
        r = _client().put("/api/supervisor/01/cuadrillas-habituales",
                          json=cuerpo, headers=_hdr("oficina"))
        assert r.status_code == 422, cuerpo


def test_fijar_habituales_vacias_no_es_422():
    """Quitarle todas las habituales es una operación legítima."""
    r = _client().put("/api/supervisor/01/cuadrillas-habituales",
                      json={"grupos": []}, headers=_hdr("oficina"))
    assert r.status_code not in (401, 403, 422)


def test_supervisor_no_lee_la_asignacion_completa():
    """Quién usa qué es vista de oficina; el teléfono pide solo la suya."""
    r = _client().get("/api/cuadrillas-habituales", headers=_hdr("supervisor", "01"))
    assert r.status_code == 403
