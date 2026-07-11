"""Media en disco del VPS (mejoras UX pre-F4): fotos de los reportes de campo.

Decisiones (acordadas con Jean):
  · Almacenamiento = disco del VPS (volumen persistente de Coolify montado en
    MEDIA_DIR; en dev, ./media). Sin S3/R2: es un MVP y el costo manda.
  · Layout: MEDIA_DIR/reportes/<semana_iso>/<uuid>.jpg y <uuid>_t.jpg — carpeta
    por semana ISO para que el indicador de uso y la purga mapeen 1:1.
  · Toda imagen se RE-ENCODEA con Pillow (JPEG ≤1600px q80 + thumb ≤320px q70,
    orientación EXIF corregida): controla el disco y neutraliza payloads
    disfrazados de imagen.
  · Los <img> del panel no llevan header Authorization → las URLs van FIRMADAS
    (HMAC-SHA256 con JWT_SECRET, TTL corto). La firma es por-recurso y expira:
    no filtra nada reutilizable.
"""
import hashlib
import hmac
import os
import time
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from core import config

MAX_FOTO_BYTES = 8 * 1024 * 1024   # 8 MB por archivo subido
MAX_FOTOS_POR_REPORTE = 5
_TTL_FIRMA = 900                   # 15 min


def media_dir() -> Path:
    return Path(config.MEDIA_DIR)


def semana_iso_de(fecha: date) -> str:
    """'2026-W28' — carpeta física y clave del indicador/purga."""
    y, w, _ = fecha.isocalendar()
    return f"{y}-W{w:02d}"


def semana_iso_a_lunes(semana_iso: str) -> Optional[date]:
    """'2026-W28' → el lunes de esa semana (None si el formato no es válido)."""
    try:
        y, w = semana_iso.split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, AttributeError):
        return None


# ── Procesado de imagen (Pillow) ─────────────────────────────
def procesar_imagen(data: bytes) -> Tuple[bytes, bytes, int, int]:
    """(jpg_principal, jpg_thumb, ancho, alto). ValueError si no es imagen válida."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("El archivo no es una imagen legible (JPEG/PNG/WebP)")
    if (img.format or "").upper() not in ("JPEG", "PNG", "WEBP"):
        raise ValueError(f"Formato {img.format} no soportado (usa JPEG/PNG/WebP)")

    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    def _encode(im, lado_max: int, calidad: int) -> bytes:
        copia = im.copy()
        copia.thumbnail((lado_max, lado_max))
        buf = BytesIO()
        copia.save(buf, "JPEG", quality=calidad, optimize=True)
        return buf.getvalue()

    principal = _encode(img, 1600, 80)
    thumb = _encode(img, 320, 70)
    ancho, alto = img.size
    return principal, thumb, ancho, alto


def guardar_foto(fecha: date, data: bytes) -> dict:
    """Procesa y escribe la foto en disco. Devuelve el dict para campo_fotos."""
    principal, thumb, ancho, alto = procesar_imagen(data)
    semana = semana_iso_de(fecha)
    carpeta = media_dir() / "reportes" / semana
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = uuid.uuid4().hex
    ruta = f"reportes/{semana}/{nombre}.jpg"
    ruta_thumb = f"reportes/{semana}/{nombre}_t.jpg"
    (media_dir() / ruta).write_bytes(principal)
    (media_dir() / ruta_thumb).write_bytes(thumb)
    return {"semana_iso": semana, "ruta": ruta, "ruta_thumb": ruta_thumb,
            "bytes": len(principal), "bytes_thumb": len(thumb),
            "ancho": ancho, "alto": alto}


# ── URLs firmadas ────────────────────────────────────────────
def _firma(ruta: str, exp: int) -> str:
    msg = f"{ruta}|{exp}".encode()
    return hmac.new(config.JWT_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def url_firmada(ruta: str, ttl: int = _TTL_FIRMA) -> str:
    exp = int(time.time()) + ttl
    return f"/media/{ruta}?exp={exp}&sig={_firma(ruta, exp)}"


def firma_valida(ruta: str, exp, sig: str) -> bool:
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(str(sig or ""), _firma(ruta, exp))


def resolver_ruta(ruta: str) -> Optional[Path]:
    """Path absoluto CONFINADO a MEDIA_DIR (anti path-traversal), o None."""
    base = media_dir().resolve()
    try:
        destino = (base / ruta).resolve()
    except (OSError, ValueError):
        return None
    if not str(destino).startswith(str(base) + os.sep):
        return None
    return destino
