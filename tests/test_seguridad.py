"""
Pruebas de la seguridad mínima (EPIC 0.4): fail-closed de secretos y matriz de roles.

Son pruebas de funciones/dependencias puras (sin BD ni servidor): se invoca la
dependencia que devuelve `require_role(...)` directamente con cabeceras simuladas,
y se monkeypatchean los atributos de `core.config` para cada escenario
(core.auth SIEMPRE lee la config vía atributo, por eso el patch surte efecto).

Ejecutar:  pytest -v
"""
import asyncio
import pytest

from fastapi import HTTPException

from core import auth, config


def _run(coro):
    return asyncio.run(coro)


# ── Fail-closed de secretos (0.4.1) ───────────────────────────────
def test_validar_secretos_prod_con_defaults_aborta(monkeypatch):
    monkeypatch.setattr(config, "ENV", "prod")
    monkeypatch.setattr(config, "JWT_SECRET", config._DEFAULT_JWT_SECRET)
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", config._DEFAULT_ADMIN_PW)
    with pytest.raises(RuntimeError):
        config.validar_secretos_arranque()


def test_validar_secretos_prod_configurado_ok(monkeypatch):
    monkeypatch.setattr(config, "ENV", "prod")
    monkeypatch.setattr(config, "JWT_SECRET", "secreto-real")
    monkeypatch.setattr(config, "API_KEY", "key-real")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "pw-real")
    config.validar_secretos_arranque()  # no debe levantar


def test_validar_secretos_dev_es_noop(monkeypatch):
    monkeypatch.setattr(config, "ENV", "dev")
    config.validar_secretos_arranque()  # aún con defaults, dev no corta


# ── Matriz de roles (0.4.2) ───────────────────────────────────────
def test_apikey_de_servicio_pasa(monkeypatch):
    """n8n con X-API-Key válida = principal de servicio (acceso pleno)."""
    monkeypatch.setattr(config, "API_KEY", "svc")
    dep = auth.require_role("oficina")
    res = _run(dep(x_api_key="svc", authorization=""))
    assert res["rol"] == "admin" and res["sub"] == "service"


def test_compat_dev_sin_apikey_no_bloquea(monkeypatch):
    """En dev sin API_KEY la seguridad global está inactiva → no bloquea (compat)."""
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "ENV", "dev")
    dep = auth.require_role("oficina")
    res = _run(dep(x_api_key="", authorization=""))
    assert res["rol"] == "admin"


def test_prod_sin_credenciales_401(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")
    dep = auth.require_role("oficina")
    with pytest.raises(HTTPException) as e:
        _run(dep(x_api_key="", authorization=""))
    assert e.value.status_code == 401


def test_supervisor_no_puede_endpoint_de_oficina(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")
    token = auth.make_token("u1", "supervisor", "Sup")
    dep = auth.require_role("oficina")
    with pytest.raises(HTTPException) as e:
        _run(dep(x_api_key="", authorization="Bearer " + token))
    assert e.value.status_code == 403


def test_oficina_si_puede_endpoint_de_oficina(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")
    token = auth.make_token("u2", "oficina", "Ofi")
    dep = auth.require_role("oficina")
    res = _run(dep(x_api_key="", authorization="Bearer " + token))
    assert res["rol"] == "oficina"


def test_admin_pasa_cualquier_rol_exigido(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "svc")
    monkeypatch.setattr(config, "ENV", "prod")
    token = auth.make_token("u3", "admin", "Adm")
    dep = auth.require_role("oficina")
    res = _run(dep(x_api_key="", authorization="Bearer " + token))
    assert res["rol"] == "admin"


# ── F4: TTL por rol — el supervisor de campo trabaja offline toda la semana ──

def test_token_supervisor_dura_7_dias():
    import time
    p = auth.verify_token(auth.make_token("u1", "supervisor", "Sup"))
    restante = p["exp"] - time.time()
    assert restante > 6.9 * 24 * 3600, "el token de supervisor debe durar ~7 días"


def test_token_oficina_conserva_ttl_corto():
    import time
    p = auth.verify_token(auth.make_token("u2", "oficina", "Ofi"))
    restante = p["exp"] - time.time()
    assert restante <= config.TOKEN_TTL + 5, "los roles de oficina no heredan el TTL largo"
