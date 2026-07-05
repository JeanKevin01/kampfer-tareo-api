"""Configuración por entorno (F0.5): variables de entorno y fail-closed de arranque.

Única fuente de ENV/API_KEY/JWT_SECRET/etc. Los tests monkeypatchean los atributos
de ESTE módulo (core.auth los lee siempre vía `config.X`, nunca por copia).
"""
import os

ENV = os.getenv("ENV", "dev").strip().lower()   # 'dev' | 'prod' — controla el modo fail-closed
API_KEY = os.getenv("API_KEY", "").strip()

# Orígenes permitidos por entorno (CSV). Default '*' = comportamiento actual hasta configurarlo.
_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = ["*"] if _origins_env in ("", "*") else [o.strip() for o in _origins_env.split(",") if o.strip()]

# ── Autenticación por usuario (JWT propio, sin dependencias) ──
_DEFAULT_JWT_SECRET = "kampfer-cambia-este-secreto-en-produccion"
_DEFAULT_ADMIN_PW   = "admin123"
JWT_SECRET = (os.getenv("JWT_SECRET", "").strip() or _DEFAULT_JWT_SECRET)
TOKEN_TTL  = int(os.getenv("TOKEN_TTL_SEG", str(60 * 60 * 12)))   # 12 h por defecto
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", _DEFAULT_ADMIN_PW)   # solo para el seed inicial


def validar_secretos_arranque():
    """Fail-closed (EPIC 0.4.1): en producción NO arrancar con secretos por defecto
    ni con la API abierta. En dev no hace nada (operación sin cortes)."""
    if ENV != "prod":
        return
    faltas = []
    if JWT_SECRET == _DEFAULT_JWT_SECRET:
        faltas.append("JWT_SECRET sin configurar")
    if not API_KEY:
        faltas.append("API_KEY obligatoria")
    if ADMIN_PASSWORD == _DEFAULT_ADMIN_PW:
        faltas.append("ADMIN_PASSWORD por defecto")
    if faltas:
        raise RuntimeError(
            "Arranque abortado por seguridad (ENV=prod): " + "; ".join(faltas) +
            ". Configura estas variables de entorno en Coolify."
        )
