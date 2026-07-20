# -*- coding: utf-8 -*-
"""Usuarios desde el padrón (2026-07-19): generación del username y roles."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import auth
from main import app
from routers.usuarios import slug_username

c = TestClient(app)


def _hdr(rol, extra=None):
    return {"Authorization": "Bearer " + auth.make_token("u-" + rol, rol, rol, extra=extra)}


# ── slug_username: formato peruano APELLIDO APELLIDO NOMBRES ──

@pytest.mark.parametrize("nombre,esperado", [
    ("MAMANI CCOPA DAVID", "dmamani"),            # 3 palabras: nombre = 3ª
    ("GARCIA FLORES JUAN PABLO", "jgarcia"),      # 4 palabras: nombre de pila = 3ª
    ("PEREZ JUAN", "jperez"),                     # 2 palabras
    ("JUAN", "juan"),                             # 1 palabra
    ("ÑAUPA QUISPE ÁNGEL", "anaupa"),             # tildes y Ñ normalizadas
    ("  ROJAS   VILA  LUIS  ", "lrojas"),         # espacios de sobra
    ("O'CONNOR SMITH ANA", "aoconnor"),           # signos fuera
])
def test_slug_username(nombre, esperado):
    assert slug_username(nombre) == esperado


def test_slug_username_vacio():
    assert slug_username("") == ""
    assert slug_username(None) == ""


# ── Los endpoints nuevos son solo de admin ──

def test_personal_elegible_requiere_admin():
    r = c.get("/api/admin/personal-elegible", headers=_hdr("oficina"))
    assert r.status_code == 403


def test_desde_personal_requiere_admin():
    r = c.post("/api/admin/usuarios/desde-personal",
               headers=_hdr("supervisor", {"sup_id": "01"}),
               json={"origen": "SUPERVISOR", "id": "01"})
    assert r.status_code == 403


def test_sincronizar_requiere_admin():
    r = c.post("/api/admin/usuarios/sincronizar-supervisores", headers=_hdr("oficina"))
    assert r.status_code == 403
