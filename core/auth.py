"""Autenticación y autorización (F0.5): API key global, JWT propio y roles.

Modelo (EPIC 0.4 / Fase 2):
  · Compuerta global `require_key` (dependencia de la app): con API_KEY configurada,
    TODA petición debe traer X-API-Key válida (integraciones) o Bearer JWT (usuarios).
  · `require_role(*roles)`: dependencia por endpoint. 'admin' siempre pasa; API key
    válida = principal de servicio con acceso pleno; en dev sin API_KEY no bloquea.

Lee la configuración SIEMPRE vía `config.X` (atributo), para que los tests puedan
monkeypatchear core.config sin recargar módulos.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from fastapi import Header, HTTPException, Request

from core import config

# Rutas que NUNCA exigen credenciales (login incluido: sin él no se podría obtener el token).
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/api/auth/login"}


def _api_key_ok(x_api_key: str) -> bool:
    """True si la X-API-Key coincide con la configurada (comparación en tiempo constante)."""
    return bool(config.API_KEY) and bool(x_api_key) and hmac.compare_digest(x_api_key, config.API_KEY)


async def require_key(
    request: Request,
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    # Compat (EPIC 0.4.3): en dev sin API_KEY no se exige nada → operación sin cortes.
    # En prod, validar_secretos_arranque() garantiza API_KEY, así que la API queda cerrada.
    if not config.API_KEY and config.ENV != "prod":
        return
    if request.url.path in _PUBLIC_PATHS or request.method == "OPTIONS":
        return
    # 1) Integraciones (n8n): API key compartida en X-API-Key
    if _api_key_ok(x_api_key):
        return
    # 2) Usuarios (panel / app de campo): token JWT válido
    if authorization[:7].lower() == "bearer " and verify_token(authorization[7:]):
        return
    raise HTTPException(401, "No autenticado: se requiere token de sesión o API key")


# ── Hash de contraseñas (PBKDF2, sin dependencias) ───────────
def _hash_pw(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}${dk.hex()}"


def _check_pw(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


# ── JWT propio (HMAC-SHA256) ─────────────────────────────────
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(sub: str, rol: str, nombre: str = "", extra: Optional[dict] = None) -> str:
    payload = {"sub": sub, "rol": rol, "nombre": nombre,
               "exp": int(time.time()) + config.TOKEN_TTL}
    if extra:
        payload.update(extra)
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(config.JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".")
        esperado = _b64u(hmac.new(config.JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, esperado):
            return None
        p = json.loads(_b64u_dec(body))
        if int(p.get("exp", 0)) < time.time():
            return None
        return p
    except Exception:
        return None


async def current_user(authorization: str = Header(default="")) -> Optional[dict]:
    """No-enforcing: devuelve el usuario del token si es válido, o None. No rompe nada."""
    if authorization[:7].lower() == "bearer ":
        return verify_token(authorization[7:])
    return None


def require_role(*roles: str):
    """Dependencia que EXIGE estar autenticado y (opcionalmente) un rol. 'admin' siempre pasa.
    · X-API-Key válida → principal de servicio (n8n): acceso pleno (no rompe integraciones).
    · En dev sin API_KEY → no bloquea (compat con la operación actual).
    · roles vacío = basta con estar autenticado."""
    async def dep(
        x_api_key: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> dict:
        if _api_key_ok(x_api_key):
            return {"sub": "service", "rol": "admin"}
        user = verify_token(authorization[7:]) if authorization[:7].lower() == "bearer " else None
        if not user:
            if not config.API_KEY and config.ENV != "prod":   # seguridad global inactiva (compat)
                return {"sub": "compat", "rol": "admin"}
            raise HTTPException(401, "No autenticado")
        if roles and user.get("rol") != "admin" and user.get("rol") not in roles:
            raise HTTPException(403, "No tienes permiso para esta acción")
        return user
    return dep
