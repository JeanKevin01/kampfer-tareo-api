# ============================================================
# routers/otms.py — PROYECTOS (la entidad sigue siendo la tabla `otms` por
# compatibilidad de API y FKs; el rename a "Proyecto" es de UI).
#
# Rediseño 2026-07-18 (Jean): id automático PROY-#### (ya no se pide), campos
# del formulario reducidos (nombre, área, centro de costo, estado, F.Inicio,
# plazo → F.Fin calculada, moneda, montos), catálogo cerrado de estados y
# detección de similares (nombre parecido o monto contractual ±100) para
# avisar antes de duplicar — al crear y al importar.
# ============================================================
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db as core_db
from core.tiempo import parse_fecha

router = APIRouter(tags=["otms"])

# Catálogo cerrado (decisión Jean 2026-07-18):
#   POR INICIAR · EJECUCION · CONCLUIDO (obra terminada, aún sin valorizar)
#   · CERRADO (valorizado y documentación enviada) · STAND BY
ESTADOS_PROYECTO = ("POR INICIAR", "EJECUCION", "CONCLUIDO", "CERRADO", "STAND BY")
MONEDAS = ("PEN", "USD")

# Margen para considerar "similar" el monto contractual de dos proyectos
_MARGEN_MONTO = 100

_OTM_UPSERT_SQL = """
    INSERT INTO otms (id, sdp, descripcion, centro_costo, area, estado,
                      plazo, fecha_inicio, fecha_fin, monto_contractual,
                      monto_valorizado, moneda)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (id) DO UPDATE SET
      estado = EXCLUDED.estado,
      descripcion = EXCLUDED.descripcion,
      area = EXCLUDED.area,
      sdp = COALESCE(NULLIF(EXCLUDED.sdp, ''), otms.sdp),
      centro_costo = EXCLUDED.centro_costo,
      plazo = COALESCE(EXCLUDED.plazo, otms.plazo),
      fecha_inicio = COALESCE(EXCLUDED.fecha_inicio, otms.fecha_inicio),
      fecha_fin = COALESCE(EXCLUDED.fecha_fin, otms.fecha_fin),
      monto_contractual = COALESCE(EXCLUDED.monto_contractual, otms.monto_contractual),
      monto_valorizado = COALESCE(EXCLUDED.monto_valorizado, otms.monto_valorizado),
      moneda = EXCLUDED.moneda
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


def _estado(v, default: str = "POR INICIAR") -> str:
    e = str(v or "").strip().upper() or default
    # Alias frecuentes del vocabulario viejo
    e = {"CULMINADO": "CONCLUIDO", "STANDBY": "STAND BY", "STAND-BY": "STAND BY",
         "EN EJECUCION": "EJECUCION", "EN EJECUCIÓN": "EJECUCION",
         "EJECUCIÓN": "EJECUCION"}.get(e, e)
    if e not in ESTADOS_PROYECTO:
        raise HTTPException(
            422, f"Estado inválido '{e}'. Usa: {' / '.join(ESTADOS_PROYECTO)}")
    return e


def _moneda(v) -> str:
    m = str(v or "PEN").strip().upper()
    m = {"SOLES": "PEN", "S/": "PEN", "SOL": "PEN",
         "DOLARES": "USD", "DÓLARES": "USD", "US$": "USD", "$": "USD"}.get(m, m)
    if m not in MONEDAS:
        raise HTTPException(422, "Moneda inválida: usa PEN (soles) o USD (dólares)")
    return m


async def _nuevo_id(con) -> str:
    """Correlativo automático PROY-#### (el usuario ya no digita el id)."""
    n = await con.fetchval(
        r"""SELECT COALESCE(MAX(substring(id FROM '^PROY-(\d+)$')::int), 0)
            FROM otms WHERE id ~ '^PROY-\d+$'""")
    return f"PROY-{(int(n) + 1):04d}"


async def _similares(con, nombre: str, monto: Optional[float],
                     excluir: str = "") -> list:
    """Proyectos con nombre igual/parecido o monto contractual dentro de ±100
    (decisión Jean: avisar y dejar elegir actualizar/crear)."""
    nom = (nombre or "").strip().upper()
    rows = await con.fetch(
        """SELECT id, descripcion, monto_contractual, estado FROM otms
           WHERE id <> $3 AND (
             ($1 <> '' AND (upper(descripcion) LIKE '%' || $1 || '%'
                            OR $1 LIKE '%' || upper(descripcion) || '%'))
             OR ($2::numeric IS NOT NULL AND monto_contractual IS NOT NULL
                 AND abs(monto_contractual - $2::numeric) <= $4)
           ) ORDER BY id LIMIT 10""",
        nom, monto, (excluir or "").strip().upper(), _MARGEN_MONTO)
    out = []
    for r in rows:
        motivo = []
        if nom and (nom in (r["descripcion"] or "").upper()
                    or (r["descripcion"] or "").upper() in nom):
            motivo.append("nombre similar")
        if (monto is not None and r["monto_contractual"] is not None
                and abs(float(r["monto_contractual"]) - monto) <= _MARGEN_MONTO):
            motivo.append(f"monto contractual a menos de {_MARGEN_MONTO}")
        out.append({"id": r["id"], "nombre": r["descripcion"],
                    "monto_contractual": float(r["monto_contractual"]) if r["monto_contractual"] is not None else None,
                    "estado": r["estado"], "motivo": " y ".join(motivo) or "similar"})
    return out


def _campos(data: dict) -> dict:
    """Normaliza el payload del formulario nuevo (acepta también el shape viejo)."""
    nombre = str(data.get("nombre") or data.get("descripcion") or "").strip().upper()
    f_inicio = parse_fecha(data.get("fecha_inicio"))
    plazo = _entero(data.get("plazo"))
    # F.Fin = F.Inicio + plazo (calculada; ya no se pide en el formulario)
    f_fin = (f_inicio + timedelta(days=plazo)) if (f_inicio and plazo) \
        else parse_fecha(data.get("fecha_fin"))
    return {
        "nombre": nombre,
        "area": str(data.get("area") or "").strip(),
        "cc": str(data.get("centro_costo") or "").strip(),
        "estado": _estado(data.get("estado")),
        "plazo": plazo,
        "f_inicio": f_inicio,
        "f_fin": f_fin,
        "monto_c": _num(data.get("monto_contractual")),
        "monto_v": _num(data.get("monto_valorizado")) or 0,
        "moneda": _moneda(data.get("moneda")),
        "sdp": str(data.get("sdp") or "").strip(),   # compat: ya no se pide en UI
    }


@router.get("/api/otms")
async def get_otms(activas: bool = False):
    # activas=true (app móvil) -> solo proyectos en EJECUCION.
    where = "WHERE estado = 'EJECUCION'" if activas else ""
    pool = await core_db()
    rows = await pool.fetch(
        f"SELECT id, descripcion, area, estado, centro_costo, sdp, plazo, "
        f"       fecha_inicio, fecha_fin, monto_contractual, monto_valorizado, moneda "
        f"FROM otms {where} ORDER BY id"
    )
    return [dict(r) for r in rows]


@router.get("/api/otms/similares")
async def buscar_similares(nombre: str = "", monto: str = "", excluir: str = "",
                           _u: dict = Depends(require_role("oficina"))):
    """Aviso pre-creación: proyectos ya cargados con nombre parecido o monto
    contractual ±100. El panel muestra la notificación Actualizar/Crear."""
    if not nombre.strip() and not _num(monto):
        return []
    pool = await core_db()
    async with pool.acquire() as con:
        return await _similares(con, nombre, _num(monto), excluir)


@router.post("/admin/otm")
async def crear_otm(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Crea o actualiza un proyecto. Sin `id` genera PROY-#### automático.
    Si hay similares y no viene `forzar: true`, responde 409 con la lista
    para que el panel pregunte (Actualizar el existente / Crear igual)."""
    c = _campos(data)
    if not c["nombre"]:
        raise HTTPException(400, "El nombre del proyecto es requerido")
    otm_id = str(data.get("id") or "").strip().upper()
    es_nuevo = not otm_id

    pool = await core_db()
    async with pool.acquire() as con:
        if es_nuevo and not data.get("forzar"):
            sim = await _similares(con, c["nombre"], c["monto_c"])
            if sim:
                raise HTTPException(409, {
                    "mensaje": "Puede que este proyecto ya exista",
                    "similares": sim})
        async with con.transaction():
            if es_nuevo:
                otm_id = await _nuevo_id(con)
            await con.execute(
                _OTM_UPSERT_SQL,
                otm_id, c["sdp"], c["nombre"], c["cc"], c["area"], c["estado"],
                c["plazo"], c["f_inicio"], c["f_fin"], c["monto_c"], c["monto_v"],
                c["moneda"],
            )
    return {"status": "ok", "id": otm_id, "nuevo": es_nuevo}


@router.post("/admin/otms/bulk")
async def crear_otms_bulk(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Importación masiva de proyectos — {otms: [{nombre|descripcion, area,
    estado, centro_costo, plazo, fecha_inicio, monto_contractual,
    monto_valorizado, moneda, id?}]}.

    Reconocimiento de ya cargados (pedido Jean): fila con `id` existente →
    ACTUALIZADA; fila sin id con similar (nombre/monto ±100) → va a
    `requieren_confirmacion` y NO se crea, salvo que traiga `forzar: true`
    (crear igual) o `actualizar_id` (actualizar ese proyecto existente)."""
    otms = data.get("otms", [])
    if not otms:
        raise HTTPException(400, "Lista de proyectos vacía")

    creadas, actualizadas, confirmar, errores = [], [], [], []
    pool = await core_db()
    async with pool.acquire() as con:
        for i, o in enumerate(otms, 1):
            try:
                c = _campos(o)
                if not c["nombre"]:
                    errores.append({"fila": i, "error": "Nombre vacío"})
                    continue
                otm_id = str(o.get("id") or o.get("actualizar_id") or "").strip().upper()
                if otm_id:
                    existia = await con.fetchval(
                        "SELECT 1 FROM otms WHERE id = $1", otm_id)
                else:
                    existia = None
                    if not o.get("forzar"):
                        sim = await _similares(con, c["nombre"], c["monto_c"])
                        if sim:
                            confirmar.append({"fila": i, "nombre": c["nombre"],
                                              "similares": sim})
                            continue
                    otm_id = await _nuevo_id(con)
                await con.execute(
                    _OTM_UPSERT_SQL,
                    otm_id, c["sdp"], c["nombre"], c["cc"], c["area"], c["estado"],
                    c["plazo"], c["f_inicio"], c["f_fin"], c["monto_c"], c["monto_v"],
                    c["moneda"],
                )
                (actualizadas if existia else creadas).append(otm_id)
            except HTTPException as e:
                errores.append({"fila": i, "error": str(e.detail)})
    return {"status": "ok", "creadas": len(creadas), "actualizadas": len(actualizadas),
            "ids_creados": creadas, "ids_actualizados": actualizadas,
            "requieren_confirmacion": confirmar, "errores": errores}


@router.put("/admin/otm/{otm_id}/estado")
async def actualizar_estado_otm(otm_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
    estado = _estado(data.get("estado"), default="")
    pool = await core_db()
    await pool.execute("UPDATE otms SET estado = $1 WHERE id = $2", estado, otm_id)
    return {"status": "ok"}
