# ============================================================
# plantillas/ — las plantillas Excel que se reparten desde el panel
#
# Se generan en el API, y no en el panel, por dos razones:
#   1. La librería del panel (SheetJS community) descarta los estilos de celda
#      en silencio: se le puede pedir una cabecera de color y escribe una hoja
#      plana. Por eso ninguna plantilla se veía profesional.
#   2. Aquí la plantilla nace con los CATÁLOGOS REALES del proyecto dentro
#      (fases, áreas, cargos, unidades), así que el usuario elige de un
#      desplegable en vez de teclear y acertar.
# ============================================================
from datetime import date
from typing import Callable, Optional

from openpyxl import Workbook

from . import definiciones as d
from ._estilo import Plantilla, construir
from .pu_meta import construir_pu

# clave → (constructor, qué catálogos necesita de la BD)
CATALOGO: dict[str, dict] = {
    "personal":    {"necesita": ["cargos"],           "fabrica": d.personal},
    "partidas":    {"necesita": ["fases", "unidades"], "fabrica": d.partidas},
    "proyectos":   {"necesita": ["areas"],            "fabrica": d.proyectos},
    "presupuesto": {"necesita": ["fases"],            "fabrica": d.presupuesto},
    "costos":      {"necesita": ["fases"],            "fabrica": d.costos},
    "costos_ro":   {"necesita": ["fases"],            "fabrica": d.costos_ro},
    "pu":          {"necesita": [],                   "fabrica": None},
}


def definir(clave: str, datos: dict) -> Plantilla:
    """Arma la definición de una plantilla con los catálogos ya resueltos."""
    entrada = CATALOGO[clave]
    args = [datos.get(n, []) for n in entrada["necesita"]]
    return entrada["fabrica"](*args)


def generar(clave: str, datos: dict, proyecto: str = "",
            hoy: Optional[date] = None) -> tuple[Workbook, str]:
    """Devuelve (libro, nombre de archivo) listo para enviar."""
    if clave not in CATALOGO:
        raise KeyError(clave)
    if clave == "pu":
        return construir_pu(proyecto, hoy), "plantilla_presupuesto_meta_pu.xlsx"
    p = definir(clave, datos)
    return construir(p, proyecto, hoy), p.archivo


def listar() -> list[dict]:
    """Catálogo para el panel: qué plantillas hay y para qué sirve cada una."""
    out = []
    for clave in CATALOGO:
        if clave == "pu":
            out.append({
                "clave": "pu", "titulo": "Presupuesto META (PU)",
                "proposito": "El presupuesto meta con su análisis de precios "
                             "unitarios: de qué está hecho el precio de cada partida.",
                "archivo": "plantilla_presupuesto_meta_pu.xlsx", "hoja": "PtoMeta",
                "columnas": 0,
            })
            continue
        p = definir(clave, {})
        out.append({
            "clave": clave, "titulo": p.titulo, "proposito": p.proposito,
            "archivo": p.archivo, "hoja": p.hoja,
            "columnas": len(p.cols),
            "obligatorias": [c.clave for c in p.cols if c.nivel == "obligatorio"],
        })
    return out
