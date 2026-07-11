# ============================================================
# routers/fases.py — catálogo de fases por proyecto (mejoras UX pre-F4)
#
# GET/POST /ev/fases · PUT /ev/fases/{id}
# El codigo es INMUTABLE tras crearse (es la clave del cruce por string en
# costos, meta y RO). No hay DELETE: se desactiva con activo=false, porque
# puede haber datos históricos que referencien el string.
# Se monta en main.py con rol oficina.
# ============================================================
from fastapi import APIRouter, HTTPException

from core.db import db

router = APIRouter(prefix="/ev/fases", tags=["fases"])

# Descripciones de fase para el RO ($1 = proyecto_id): el nombre del catálogo
# es el fallback y la descripción de la partida-padre (si existe) gana —
# mismo comportamiento de antes, pero sin fases anónimas.
FASES_DESC_SQL = """
    SELECT fase, descripcion FROM (
      SELECT codigo AS fase, nombre AS descripcion, 1 AS pri
      FROM fases WHERE proyecto_id = $1
      UNION ALL
      SELECT codigo, descripcion, 2 FROM ev_partidas
      WHERE descripcion IS NOT NULL
        AND codigo IN (SELECT DISTINCT fase FROM ev_partidas WHERE fase IS NOT NULL)
    ) t ORDER BY pri
"""


def _norm_codigo(codigo) -> str:
    """Normaliza el código de fase: sin espacios extremos, en MAYÚSCULAS."""
    c = str(codigo or "").strip().upper()
    if not c or len(c) > 20:
        raise HTTPException(400, "codigo de fase inválido (1-20 caracteres)")
    return c


@router.get("")
async def listar_fases(proyecto_id: int = 1, incluir_inactivas: bool = False):
    pool = await db()
    sql = "SELECT * FROM fases WHERE proyecto_id = $1"
    if not incluir_inactivas:
        sql += " AND activo"
    rows = await pool.fetch(sql + " ORDER BY orden, codigo", proyecto_id)
    return [dict(r) for r in rows]


@router.post("")
async def crear_fase(data: dict):
    codigo = _norm_codigo(data.get("codigo"))
    nombre = str(data.get("nombre") or "").strip()
    if not nombre:
        raise HTTPException(400, "nombre requerido")
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    row = await pool.fetchrow(
        """INSERT INTO fases (proyecto_id, codigo, nombre, descripcion, color, orden)
           VALUES ($1,$2,$3,$4,$5,$6)
           ON CONFLICT (proyecto_id, codigo) DO NOTHING RETURNING *""",
        proyecto_id, codigo, nombre,
        (str(data["descripcion"]).strip() or None) if data.get("descripcion") else None,
        (str(data["color"]).strip() or None) if data.get("color") else None,
        int(data.get("orden") or 999))
    if not row:
        raise HTTPException(409, f"La fase {codigo} ya existe en el proyecto")
    return dict(row)


@router.put("/{fase_id}")
async def editar_fase(fase_id: int, data: dict):
    """Edita metadatos. El codigo NO se puede cambiar (cruce por string)."""
    if "codigo" in data:
        raise HTTPException(400, "El codigo de una fase es inmutable; crea otra fase")
    campos, valores = [], []
    for k in ("nombre", "descripcion", "color"):
        if k in data:
            v = str(data[k]).strip() if data[k] is not None else None
            if k == "nombre" and not v:
                raise HTTPException(400, "nombre no puede quedar vacío")
            campos.append(k)
            valores.append(v or None)
    if "orden" in data:
        campos.append("orden")
        valores.append(int(data["orden"] or 999))
    if "activo" in data:
        campos.append("activo")
        valores.append(bool(data["activo"]))
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(campos))
    pool = await db()
    row = await pool.fetchrow(
        f"UPDATE fases SET {sets} WHERE id = $1 RETURNING *", fase_id, *valores)
    if not row:
        raise HTTPException(404, "Fase no encontrada")
    return dict(row)
