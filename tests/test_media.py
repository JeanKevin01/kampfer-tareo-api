"""
core/media.py — firma HMAC, procesado Pillow y confinamiento de rutas (puros).
"""
from datetime import date
from io import BytesIO

import pytest

from core import config
from core.media import (firma_valida, procesar_imagen, resolver_ruta,
                        semana_iso_de, url_firmada)


def _jpeg(px=2000, exif_rot=False) -> bytes:
    from PIL import Image
    img = Image.new("RGB", (px, px // 2), (200, 120, 40))
    buf = BytesIO()
    if exif_rot:
        exif = Image.Exif()
        exif[274] = 6   # Orientation = Rotate 90 CW
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


# ── Firma ────────────────────────────────────────────────────
def test_url_firmada_valida():
    url = url_firmada("reportes/2026-W28/x.jpg")
    ruta = url.split("?")[0].removeprefix("/media/")
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert firma_valida(ruta, q["exp"], q["sig"])


def test_firma_alterada_o_vencida_falla():
    url = url_firmada("reportes/2026-W28/x.jpg")
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert not firma_valida("reportes/2026-W28/OTRA.jpg", q["exp"], q["sig"])
    assert not firma_valida("reportes/2026-W28/x.jpg", q["exp"], "0" * 64)
    assert not firma_valida("reportes/2026-W28/x.jpg", "1000000", q["sig"])   # vencida
    assert not firma_valida("reportes/2026-W28/x.jpg", "no-numero", q["sig"])


def test_firma_depende_del_secreto(monkeypatch):
    url = url_firmada("a.jpg")
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    monkeypatch.setattr(config, "JWT_SECRET", "otro-secreto")
    assert not firma_valida("a.jpg", q["exp"], q["sig"])


# ── Procesado de imagen ──────────────────────────────────────
def test_procesar_reduce_y_reencodea():
    original = _jpeg(px=2000)
    principal, thumb, ancho, alto = procesar_imagen(original)
    from PIL import Image
    p = Image.open(BytesIO(principal))
    t = Image.open(BytesIO(thumb))
    assert max(p.size) <= 1600 and max(t.size) <= 320
    assert p.format == "JPEG" and t.format == "JPEG"
    assert (ancho, alto) == (2000, 1000)


def test_procesar_corrige_orientacion_exif():
    principal, _, ancho, alto = procesar_imagen(_jpeg(px=1000, exif_rot=True))
    # La imagen 1000x500 rotada 90° queda 500x1000 (vertical).
    assert (ancho, alto) == (500, 1000)


def test_procesar_rechaza_no_imagen():
    for basura in (b"", b"no soy una imagen", b"%PDF-1.4 algo"):
        with pytest.raises(ValueError):
            procesar_imagen(basura)


# ── Confinamiento y semana ISO ───────────────────────────────
def test_resolver_ruta_confina_en_media_dir():
    assert resolver_ruta("reportes/2026-W28/x.jpg") is not None
    assert resolver_ruta("../fuera.jpg") is None
    assert resolver_ruta("reportes/../../etc/passwd") is None


def test_semana_iso():
    assert semana_iso_de(date(2026, 7, 6)) == "2026-W28"    # lunes
    assert semana_iso_de(date(2026, 7, 12)) == "2026-W28"   # domingo de la misma
    assert semana_iso_de(date(2026, 1, 1)) == "2026-W01"


def test_semana_iso_a_lunes_ida_y_vuelta():
    from core.media import semana_iso_a_lunes
    assert semana_iso_a_lunes("2026-W28") == date(2026, 7, 6)
    for f in (date(2026, 7, 9), date(2026, 1, 1)):
        lunes = semana_iso_a_lunes(semana_iso_de(f))
        assert lunes is not None and lunes <= f and (f - lunes).days < 7
    assert semana_iso_a_lunes("basura") is None
    assert semana_iso_a_lunes("") is None
