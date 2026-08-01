# ============================================================
# routers/plantillas.py — descarga de las plantillas Excel (oficina)
#
# El panel ya no arma los .xlsx: los pide aquí. Ver `plantillas/__init__.py`
# para el porqué (SheetJS community no escribe estilos, y aquí la plantilla
# puede traer los catálogos reales del proyecto).
# ============================================================
from io import BytesIO

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import plantillas
from core.db import db
from core.log import get_logger

log = get_logger("plantillas")

router = APIRouter(prefix="/ev/plantillas", tags=["plantillas"])

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _catalogos(necesita: list, proyecto_id: int) -> dict:
    """Los valores reales del proyecto que alimentan los desplegables.

    Cada consulta va por separado y tolera el vacío: una plantilla tiene que
    poder descargarse el primer día, cuando todavía no hay nada cargado (ahí
    cada catálogo cae a sus valores de ejemplo).
    """
    datos: dict = {}
    if not necesita:
        return datos
    pool = await db()
    if "fases" in necesita:
        filas = await pool.fetch(
            "SELECT codigo, nombre FROM fases WHERE proyecto_id = $1 AND activo "
            "ORDER BY orden NULLS LAST, codigo", proyecto_id)
        datos["fases"] = [(f["codigo"], f["nombre"] or "") for f in filas]
    if "cargos" in necesita:
        filas = await pool.fetch(
            "SELECT DISTINCT cargo FROM trabajadores "
            "WHERE cargo IS NOT NULL AND cargo <> '' AND activo ORDER BY cargo")
        datos["cargos"] = [f["cargo"] for f in filas]
    if "unidades" in necesita:
        filas = await pool.fetch(
            "SELECT DISTINCT unidad FROM ev_partidas "
            "WHERE unidad IS NOT NULL AND unidad <> '' ORDER BY unidad")
        datos["unidades"] = [f["unidad"] for f in filas]
    if "areas" in necesita:
        filas = await pool.fetch(
            "SELECT DISTINCT area FROM otms "
            "WHERE area IS NOT NULL AND area <> '' ORDER BY area")
        datos["areas"] = [f["area"] for f in filas]
    return datos


async def _nombre_proyecto(proyecto_id: int) -> str:
    """Va en el subtítulo de la plantilla: quien encuentre el archivo tres meses
    después sabe de qué obra es."""
    try:
        pool = await db()
        return await pool.fetchval(
            "SELECT nombre FROM proyectos WHERE id = $1", proyecto_id) or ""
    except Exception:
        log.exception("no se pudo leer el nombre del proyecto para la plantilla")
        return ""


# El rol lo exige el include de main.py (patrón del proyecto): estos endpoints
# son de oficina.
@router.get("")
async def catalogo_plantillas():
    """Qué plantillas hay, para qué sirve cada una y qué columnas exige."""
    return plantillas.listar()


@router.get("/{clave}")
async def descargar_plantilla(clave: str, proyecto_id: int = 1):
    if clave not in plantillas.CATALOGO:
        raise HTTPException(404, "No existe esa plantilla")
    datos = await _catalogos(plantillas.CATALOGO[clave]["necesita"], proyecto_id)
    wb, archivo = plantillas.generar(clave, datos, await _nombre_proyecto(proyecto_id))
    buf = BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(), media_type=MIME_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{archivo}"'})
