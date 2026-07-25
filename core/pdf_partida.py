"""Sustento de valorización por partida en PDF (fpdf2, sin navegador headless).

Se genera UN PDF por partida y el endpoint los empaqueta en un ZIP (un archivo
por partida, para adjuntar a cada línea de la valorización). Las fotos se
embeben directo desde el disco del VPS (MEDIA_DIR) — no pasa por URLs firmadas.

Fuentes núcleo (Helvetica / Courier) = latin-1: el texto se sanea con `_latin1`
para no romper con viñetas/em-dash/emojis (mismo criterio del parte de campo).
La identidad la cargan el color ámbar + tinta carbón + el wordmark «K», igual
que la vista imprimible del panel (BrandDoc), pero sin fuentes embebidas para
mantener la imagen liviana. Cambiar a Geist en el futuro = soltar un TTF y
`add_font`.
"""
from __future__ import annotations

from io import BytesIO
from typing import Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from core.media import resolver_ruta

# Paleta (misma que BrandDoc del panel)
AMBAR = (245, 158, 11)
CARBON = (16, 21, 31)
TINTA = (34, 40, 52)
TINTA2 = (85, 96, 111)
TINTA3 = (140, 150, 162)
LINEA = (226, 230, 237)
LINEA2 = (210, 216, 226)
VERDE = (10, 125, 79)
FONDO_CIFRA = (247, 249, 252)
FONDO_PARTE = (248, 250, 252)
FONDO_AVISO = (253, 246, 230)
BORDE_AVISO = (240, 214, 154)

# Puntuación unicode → equivalente latin-1 (las fuentes núcleo no la traen).
_MAP = {"—": "-", "–": "-", "•": "·", "★": "*", "…": "...",
        "“": '"', "”": '"', "‘": "'", "’": "'", "→": "->", "←": "<-",
        "≥": ">=", "≤": "<=", "⛔": "!", "✓": "OK", "→": "->"}

MARGEN = 16.0
ANCHO_CONT = 210.0 - 2 * MARGEN   # 178 mm útiles


def _latin1(s) -> str:
    """Texto seguro para fuentes núcleo (latin-1): mapea puntuación unicode
    frecuente y reemplaza lo que no entra (emojis) por '?'."""
    s = str(s or "")
    for k, v in _MAP.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _fmt(v, d: int = 2) -> str:
    """Número estilo es-PE: 1.234,50 (punto miles, coma decimal)."""
    s = f"{float(v or 0):,.{d}f}"                       # 1,234.50 (estilo US)
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _fmt_m(v, unidad: Optional[str]) -> str:
    return f"{_fmt(v)} {unidad}".strip() if unidad else _fmt(v)


def _hh_txt(p: dict) -> str:
    hh = float(p.get("hh_gastadas") or 0)
    rango = float(p.get("hh_rango") or 0)
    return f"{_fmt(hh, 1)}  ({_fmt(rango, 1)} en el periodo)" if rango != hh else _fmt(hh, 1)


class _Doc(FPDF):
    """A4 con cabecera de marca + pie con paginación en cada página."""

    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(True, margin=18)
        self.set_margins(MARGEN, 26, MARGEN)

    def header(self):
        # Wordmark: «K» blanca sobre cuadro ámbar
        self.set_fill_color(*AMBAR)
        self.rect(MARGEN, 11, 8, 8, style="F")
        self.set_xy(MARGEN, 11)
        self.set_text_color(255, 255, 255)
        self.set_font("helvetica", "B", 13)
        self.cell(8, 8, "K", align="C")
        # KAMPFER + subtítulo
        self.set_xy(MARGEN + 10, 10.5)
        self.set_text_color(*CARBON)
        self.set_font("helvetica", "B", 12)
        self.cell(0, 5, "KAMPFER", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(MARGEN + 10)
        self.set_font("helvetica", "", 8.5)
        self.set_text_color(*TINTA3)
        self.cell(0, 4, "Sustento de valorización", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        # Línea ámbar
        self.set_draw_color(*AMBAR)
        self.set_line_width(0.6)
        self.line(MARGEN, 22, 210 - MARGEN, 22)
        self.set_line_width(0.2)

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*LINEA)
        self.set_line_width(0.2)
        self.line(MARGEN, self.get_y(), 210 - MARGEN, self.get_y())
        self.set_y(-11)
        self.set_font("helvetica", "", 7.5)
        self.set_text_color(*TINTA3)
        self.cell(0, 5, _latin1("KAMPFER · Sistema del tareo al Resultado Operativo"), align="L")
        self.set_y(-11)
        self.cell(0, 5, f"Pag. {self.page_no()}", align="R")


def _cifras(pdf: _Doc, p: dict) -> None:
    """Franja de 5 cifras (metrado presup / ejec / % avance / HH presup / HH gastadas)."""
    cols = [
        ("METRADO PRESUP.", _fmt_m(p.get("metrado_presup"), p.get("unidad")), False),
        ("METRADO EJECUTADO", _fmt_m(p.get("metrado_ejec"), p.get("unidad")), True),
        ("% AVANCE", "-" if p.get("avance") is None else f"{float(p['avance']) * 100:.1f}%", True),
        ("HH PRESUP.", _fmt(p.get("hh_presup"), 1), False),
        ("HH GASTADAS", _hh_txt(p), False),
    ]
    n = len(cols)
    cw = ANCHO_CONT / n
    y = pdf.get_y()
    h = 15.0
    pdf.set_draw_color(*LINEA)
    for i, (label, val, fuerte) in enumerate(cols):
        x = MARGEN + i * cw
        pdf.set_fill_color(*FONDO_CIFRA)
        pdf.rect(x, y, cw, h, style="DF")
        pdf.set_xy(x + 2.5, y + 2.6)
        pdf.set_font("helvetica", "", 6.5)
        pdf.set_text_color(*TINTA3)
        pdf.cell(cw - 5, 3, label)
        # Valor (encoge la fuente si no cabe)
        pdf.set_xy(x + 2.5, y + 7)
        pdf.set_text_color(*(VERDE if fuerte else TINTA))
        txt = _latin1(val)
        size = 10.5
        pdf.set_font("helvetica", "B", size)
        while size > 6 and pdf.get_string_width(txt) > cw - 5:
            size -= 0.5
            pdf.set_font("helvetica", "B", size)
        pdf.cell(cw - 5, 5, txt)
    pdf.set_y(y + h + 5)


def _aviso_sin_tareo(pdf: _Doc) -> None:
    txt = ("Hay reportes de campo pero ninguna HH del tareo quedó cargada a esta partida. "
           "Revísalo en «Registros y HH» del día: el tareo pudo enviarse sin partida, con "
           "0 HH, o lo reemplazó un envío posterior del mismo supervisor/OTM/día.")
    pdf.set_font("helvetica", "", 9)
    ancho = ANCHO_CONT - 8
    y0 = pdf.get_y()
    # Alto aproximado del bloque para pintar el fondo antes del texto
    lineas = pdf.multi_cell(ancho, 4.6, _latin1(txt), dry_run=True, output="LINES")
    h = len(lineas) * 4.6 + 6
    pdf.set_fill_color(*FONDO_AVISO)
    pdf.set_draw_color(*BORDE_AVISO)
    pdf.rect(MARGEN, y0, ANCHO_CONT, h, style="DF")
    pdf.set_xy(MARGEN + 4, y0 + 3)
    pdf.set_text_color(138, 90, 6)
    pdf.multi_cell(ancho, 4.6, _latin1(txt))
    pdf.set_y(y0 + h + 4)


def _parte(pdf: _Doc, texto: str) -> None:
    """El parte diario en monoespaciada dentro de un recuadro claro."""
    pdf.set_font("courier", "", 8.5)
    ancho = ANCHO_CONT - 8
    lineas = pdf.multi_cell(ancho, 4.2, _latin1(texto), dry_run=True, output="LINES")
    h = len(lineas) * 4.2 + 6
    # Salto de página si el recuadro no entra completo
    if pdf.get_y() + h > pdf.page_break_trigger:
        pdf.add_page()
    y0 = pdf.get_y()
    pdf.set_fill_color(*FONDO_PARTE)
    pdf.set_draw_color(*LINEA2)
    pdf.rect(MARGEN, y0, ANCHO_CONT, h, style="DF")
    pdf.set_xy(MARGEN + 4, y0 + 3)
    pdf.set_text_color(*TINTA2)
    pdf.multi_cell(ancho, 4.2, _latin1(texto))
    pdf.set_y(y0 + h + 3)


def _galeria(pdf: _Doc, fotos: list) -> None:
    """Fotos en filas de 2 con altura de fila uniforme (mismo criterio premium
    que la galería del panel): cada imagen se escala a la altura de la fila sin
    recortar; si es muy apaisada se limita al ancho de la celda y se centra."""
    if not fotos:
        return
    gap = 6.0
    cw = (ANCHO_CONT - gap) / 2          # ancho de celda ~ 86 mm
    rowH = 52.0                           # altura de fila uniforme
    for i in range(0, len(fotos), 2):
        par = fotos[i:i + 2]
        if pdf.get_y() + rowH > pdf.page_break_trigger:
            pdf.add_page()
        y = pdf.get_y()
        for j, f in enumerate(par):
            x = MARGEN + j * (cw + gap)
            _foto_celda(pdf, f, x, y, cw, rowH)
        pdf.set_y(y + rowH + gap)


def _foto_celda(pdf: _Doc, f: dict, x: float, y: float, cw: float, ch: float) -> None:
    purgada = bool(f.get("purgada"))
    ruta = f.get("ruta")
    destino = resolver_ruta(ruta) if (ruta and not purgada) else None
    disponible = bool(destino and destino.exists())
    if not disponible:
        # Hueco honesto: la foto fue purgada o no está en disco
        pdf.set_fill_color(244, 246, 249)
        pdf.set_draw_color(*LINEA)
        pdf.rect(x, y, cw, ch, style="DF")
        pdf.set_xy(x, y + ch / 2 - 2)
        pdf.set_font("helvetica", "I", 8)
        pdf.set_text_color(*TINTA3)
        pdf.cell(cw, 4, _latin1("foto purgada" if purgada else "foto no disponible"), align="C")
        return
    ancho = float(f.get("ancho") or 0)
    alto = float(f.get("alto") or 0)
    aspect = (ancho / alto) if (ancho > 0 and alto > 0) else 4 / 3
    # Escala a la altura de la fila; si se pasa de ancho, limita por ancho.
    h = ch
    w = h * aspect
    if w > cw:
        w = cw
        h = w / aspect
    ox = x + (cw - w) / 2
    oy = y + (ch - h) / 2
    try:
        pdf.image(str(destino), x=ox, y=oy, w=w, h=h)
    except Exception:
        pdf.set_fill_color(244, 246, 249)
        pdf.set_draw_color(*LINEA)
        pdf.rect(x, y, cw, ch, style="DF")
        pdf.set_xy(x, y + ch / 2 - 2)
        pdf.set_font("helvetica", "I", 8)
        pdf.set_text_color(*TINTA3)
        pdf.cell(cw, 4, _latin1("foto ilegible"), align="C")


def _fecha_larga(iso: str) -> str:
    meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    try:
        from datetime import date as _d
        y, m, d = (int(x) for x in iso.split("-"))
        f = _d(y, m, d)
        return f"{dias[f.weekday()]} {d} de {meses[m]} de {y}"
    except Exception:
        return iso


def pdf_sustento_partida(bloque: dict, periodo: str) -> bytes:
    """Genera el PDF (bytes) del sustento de UNA partida.

    `bloque` = estructura de `_datos_reporte_partida` (partida + reportes con
    fotos crudas que traen `ruta`). `periodo` = texto ya formateado del rango.
    """
    p = bloque["partida"]
    reps = bloque.get("reportes", [])
    pdf = _Doc()
    pdf.set_title(_latin1(f"Sustento {p.get('codigo')} — {p.get('descripcion')}"))
    pdf.set_author("KAMPFER")
    pdf.add_page()

    # Encabezado de la partida
    pdf.set_text_color(*CARBON)
    pdf.set_font("helvetica", "B", 14)
    pdf.multi_cell(ANCHO_CONT, 6.5, _latin1(f"{p.get('codigo')} — {p.get('descripcion')}"))
    pdf.set_font("helvetica", "", 9.5)
    pdf.set_text_color(*TINTA2)
    otm = p.get("otm_id") or ""
    if p.get("otm_desc"):
        otm = f"{otm} · {p['otm_desc']}"
    pdf.cell(0, 5, _latin1(otm), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*TINTA3)
    pdf.set_font("helvetica", "", 8.5)
    n = len(reps)
    pdf.cell(0, 4.5, _latin1(f"{periodo} · {n} reporte{'s' if n != 1 else ''} de campo"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    _cifras(pdf, p)

    if p.get("sin_tareo"):
        _aviso_sin_tareo(pdf)

    if not reps:
        pdf.set_font("helvetica", "I", 10)
        pdf.set_text_color(*TINTA3)
        pdf.cell(0, 6, "Sin reportes de campo en el periodo.")
    for r in reps:
        # Cabecera del día (evita quedar huérfana al pie de página)
        if pdf.get_y() + 16 > pdf.page_break_trigger:
            pdf.add_page()
        pdf.set_draw_color(*LINEA)
        pdf.set_line_width(0.2)
        pdf.line(MARGEN, pdf.get_y(), 210 - MARGEN, pdf.get_y())
        pdf.ln(2)
        titulo = _fecha_larga(str(r.get("fecha")))
        if r.get("actividad"):
            titulo += f" · {r['actividad']}"
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(*CARBON)
        hh = float(r.get("hh_dia") or 0)
        hh_txt = f"{_fmt(hh, 1)} HH" if hh > 0 else ""
        pdf.cell(ANCHO_CONT - 30, 6, _latin1(titulo))
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(*TINTA2)
        pdf.cell(30, 6, _latin1(hh_txt), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1.5)
        _parte(pdf, r.get("texto") or "")
        _galeria(pdf, r.get("fotos") or [])
        pdf.ln(2)

    return bytes(pdf.output())
