"""
Pruebas de la seguridad mínima (EPIC 0.4): fail-closed de secretos y matriz de roles.

Son pruebas de funciones/dependencias puras (sin BD ni servidor): se invoca la
dependencia que devuelve `require_role(...)` directamente con cabeceras simuladas,
y se monkeypatchean los globales de `main` (ENV, API_KEY) para cada escenario.

Ejecutar:  pytest -v
"""
import asyncio
import pytest

import main


def _run(coro):
    return asyncio.run(coro)


# ── Fail-closed de secretos (0.4.1) ───────────────────────────────
def test_validar_secretos_prod_con_defaults_aborta(monkeypatch):
    monkeypatch.setattr(main, "ENV", "prod")
    monkeypatch.setattr(main, "JWT_SECRET", main._DEFAULT_JWT_SECRET)
    monkeypatch.setattr(main, "API_KEY", "")
    monkeypatch.setattr(main, "ADMIN_PASSWORD", main._DEFAULT_ADMIN_PW)
    with pytest.raises(RuntimeError):
        main.validar_secretos_arranque()


def test_validar_secretos_prod_configurado_ok(monkeypatch):
    monkeypatch.setattr(main, "ENV", "prod")
    monkeypatch.setattr(main, "JWT_SECRET", "secreto-real")
    monkeypatch.setattr(main, "API_KEY", "key-real")
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "pw-real")
    main.validar_secretos_arranque()  # no debe levantar


def test_validar_secretos_dev_es_noop(monkeypatch):
    monkeypatch.setattr(main, "ENV", "dev")
    main.validar_secretos_arranque()  # aún con defaults, dev no corta


# ── Matriz de roles (0.4.2) ───────────────────────────────────────
def test_apikey_de_servicio_pasa(monkeypatch):
    """n8n con X-API-Key válida = principal de servicio (acceso pleno)."""
    monkeypatch.setattr(main, "API_KEY", "svc")
    dep = main.require_role("oficina")
    res = _run(dep(x_api_key="svc", authorization=""))
    assert res["rol"] == "admin" and res["sub"] == "service"


def test_compat_dev_sin_apikey_no_bloquea(monkeypatch):
    """En dev sin API_KEY la seguridad global está inactiva → no bloquea (compat)."""
    monkeypatch.setattr(main, "API_KEY", "")
    monkeypatch.setattr(main, "ENV", "dev")
    dep = main.require_role("oficina")
    res = _run(dep(x_api_key="", authorization=""))
    assert res["rol"] == "admin"


def test_prod_sin_credenciales_401(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "svc")
    monkeypatch.setattr(main, "ENV", "prod")
    dep = main.require_role("oficina")
    with pytest.raises(main.HTTPException) as e:
        _run(dep(x_api_key="", authorization=""))
    assert e.value.status_code == 401


def test_supervisor_no_puede_endpoint_de_oficina(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "svc")
    monkeypatch.setattr(main, "ENV", "prod")
    token = main.make_token("u1", "supervisor", "Sup")
    dep = main.require_role("oficina")
    with pytest.raises(main.HTTPException) as e:
        _run(dep(x_api_key="", authorization="Bearer " + token))
    assert e.value.status_code == 403


def test_oficina_si_puede_endpoint_de_oficina(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "svc")
    monkeypatch.setattr(main, "ENV", "prod")
    token = main.make_token("u2", "oficina", "Ofi")
    dep = main.require_role("oficina")
    res = _run(dep(x_api_key="", authorization="Bearer " + token))
    assert res["rol"] == "oficina"


def test_admin_pasa_cualquier_rol_exigido(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "svc")
    monkeypatch.setattr(main, "ENV", "prod")
    token = main.make_token("u3", "admin", "Adm")
    dep = main.require_role("oficina")
    res = _run(dep(x_api_key="", authorization="Bearer " + token))
    assert res["rol"] == "admin"
