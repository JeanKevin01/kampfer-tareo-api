# ============================================================
# routers/otms.py — OTMs (lectura para app/panel + admin CRUD/bulk)
# ============================================================
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db as core_db
from core.tiempo import parse_fecha

router = APIRouter(tags=["otms"])

_OTM_UPSERT_SQL = """
    INSERT INTO otms (id, sdp, descripcion, centro_costo, area, estado,
                      plazo, fecha_inicio, fecha_fin, monto_contractual, monto_valorizado)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (id) DO UPDATE SET
      estado = EXCLUDED.estado,
      descripcion = EXCLUDED.descripcion,
      area = EXCLUDED.area,
      sdp = EXCLUDED.sdp,
      centro_costo = EXCLUDED.centro_costo,
      plazo = COALESCE(EXCLUDED.plazo, otms.plazo),
      fecha_inicio = COALESCE(EXCLUDED.fecha_inicio, otms.fecha_inicio),
      fecha_fin = COALESCE(EXCLUDED.fecha_fin, otms.fecha_fin),
      monto_contractual = COALESCE(EXCLUDED.monto_contractual, otms.monto_contractual),
      monto_valorizado = COALESCE(EXCLUDED.monto_valorizado, otms.monto_valorizado)
"""


def _num(v) -> Optional[float]:
    """Coerción segura a número (asyncpg no acepta strings en columnas numéricas)."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _entero(v) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None else None


@router.get("/api/otms")
async def get_otms(activas: bool = False):
    # activas=true (app móvil) -> solo OTMs en EJECUCION.
    # Caso general (panel) -> TODAS las OTMs, sin filtrar por estado, porque el
    # vocabulario real de estados es abierto (ej. 'CULMINADO', 'GENERAR NUEVO SDP')
    # y no queremos ocultar OTMs por un estado no previsto.
    where = "WHERE estado = 'EJECUCION'" if activas else ""
    pool = await core_db()
    rows = await pool.fetch(
        f"SELECT id, descripcion, area, estado, centro_costo, sdp, plazo, "
        f"       fecha_inicio, fecha_fin, monto_contractual, monto_valorizado "
        f"FROM otms {where} ORDER BY id"
    )
    return [dict(r) for r in rows]


@router.post("/admin/otm")
async def crear_otm(data: dict, _u: dict = Depends(require_role("oficina"))):
    otm_id      = data.get("id", "").strip().upper()
    descripcion = data.get("descripcion", "").strip().upper()
    area        = data.get("area", "").strip()
    estado      = data.get("estado", "POR INICIAR").strip()
    sdp         = data.get("sdp", "").strip()
    cc          = data.get("centro_costo", "").strip()
    plazo       = _entero(data.get("plazo"))
    f_inicio    = parse_fecha(data.get("fecha_inicio"))
    f_fin       = parse_fecha(data.get("fecha_fin"))
    monto_c     = _num(data.get("monto_contractual"))
    monto_v     = _num(data.get("monto_valorizado")) or 0

    if not otm_id or not descripcion:
        raise HTTPException(400, "ID y descripción son requeridos")

    pool = await core_db()
    await pool.execute(
        _OTM_UPSERT_SQL,
        otm_id, sdp, descripcion, cc, area, estado, plazo, f_inicio, f_fin,
        monto_c, monto_v,
    )
    return {"status": "ok", "id": otm_id}


@router.post("/admin/otms/bulk")
async def crear_otms_bulk(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Importación masiva de OTMs — recibe {otms: [{id,descripcion,area,estado,sdp,centro_costo,
    plazo,fecha_inicio,fecha_fin,monto_contractual,monto_valorizado}]}"""
    otms = data.get("otms", [])
    if not otms:
        raise HTTPException(400, "Lista de OTMs vacía")

    creadas, errores = [], []
    pool = await core_db()

    for o in otms:
        otm_id      = str(o.get("id", "")).strip().upper()
        descripcion = str(o.get("descripcion", "")).strip().upper()
        area        = str(o.get("area", "")).strip()
        # Se conserva el estado tal como viene (en mayúsculas). NO se fuerza a
        # 'POR INICIAR' si es desconocido: el vocabulario real es abierto
        # (ej. 'CULMINADO', 'GENERAR NUEVO SDP') y forzarlo falsearía el dato.
        estado      = str(o.get("estado", "")).strip().upper() or "POR INICIAR"
        sdp         = str(o.get("sdp", "")).strip()
        cc          = str(o.get("centro_costo", "")).strip()
        plazo       = _entero(o.get("plazo"))
        f_inicio    = parse_fecha(o.get("fecha_inicio"))
        f_fin       = parse_fecha(o.get("fecha_fin"))
        monto_c     = _num(o.get("monto_contractual"))
        monto_v     = _num(o.get("monto_valorizado")) or 0

        if not otm_id or not descripcion:
            errores.append({"id": otm_id or "—", "error": "ID o descripción vacíos"})
            continue

        try:
            await pool.execute(
                _OTM_UPSERT_SQL,
                otm_id, sdp, descripcion, cc, area, estado, plazo, f_inicio, f_fin,
                monto_c, monto_v,
            )
            creadas.append(otm_id)
        except Exception as e:
            errores.append({"id": otm_id, "error": str(e)})

    return {"status": "ok", "creadas": len(creadas), "errores": errores}


@router.put("/admin/otm/{otm_id}/estado")
async def actualizar_estado_otm(otm_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
    estado = data.get("estado", "").strip()
    validos = ["EJECUCION", "POR INICIAR", "CERRADO", "CONCLUIDO", "STAND BY"]
    if estado not in validos:
        raise HTTPException(400, f"Estado inválido. Válidos: {validos}")
    pool = await core_db()
    await pool.execute("UPDATE otms SET estado = $1 WHERE id = $2", estado, otm_id)
    return {"status": "ok"}
