# ============================================================
# routers/presupuesto.py — Fase 1: presupuesto gobernado
#
# Versiones del presupuesto (BORRADOR → CONGELADO) por proyecto.
# Al CONGELAR, se activa (vigente) y se SINCRONIZA metrado/HH meta/PU
# hacia ev_partidas (match por código). El motor de Valor Ganado no cambia:
# sigue leyendo ev_partidas.
#
# Se monta en main.py con require_role("oficina") (todo es de oficina).
# ============================================================
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import database

router = APIRouter(prefix="/ev/presupuesto", tags=["presupuesto"])


# ── Modelos ───────────────────────────────────────────────────
class LineaIn(BaseModel):
    codigo: str
    descripcion: Optional[str] = None
    unidad: Optional[str] = None
    fase: Optional[str] = None
    sub_fase: Optional[str] = None
    metrado: float = 0
    precio_unitario: float = 0
    hh_meta: float = 0


class CrearIn(BaseModel):
    proyecto_id: int = 1
    nota: Optional[str] = None
    sembrar: bool = True          # copiar las partidas actuales de ev_partidas


class LineasIn(BaseModel):
    partidas: List[LineaIn]


# ── Lógica pura (testeable sin BD) ────────────────────────────
def _siguiente_version(versiones) -> int:
    """Próxima versión correlativa (1 si no hay ninguna)."""
    nums = [int(v) for v in versiones if v is not None]
    return (max(nums) + 1) if nums else 1


def _clasificar_codigos(presup_codigos, ev_codigos):
    """Separa los códigos del presupuesto en (sincronizables, no_encontrados en ev_partidas)."""
    evs = set(ev_codigos)
    enc = [c for c in presup_codigos if c in evs]
    falt = [c for c in presup_codigos if c not in evs]
    return enc, falt


# ── Endpoints ─────────────────────────────────────────────────
@router.get("")
async def listar(proyecto_id: int = 1):
    rows = await database.fetch_all(
        """SELECT p.*,
                  (SELECT count(*) FROM presupuesto_partidas pp WHERE pp.presupuesto_id = p.id) AS lineas
           FROM presupuestos p
           WHERE p.proyecto_id = :pid
           ORDER BY p.version DESC""",
        {"pid": proyecto_id},
    )
    return [dict(r) for r in rows]


@router.post("")
async def crear(body: CrearIn):
    """Crea una versión nueva en BORRADOR. Si sembrar=True, copia las partidas
    activas del proyecto desde ev_partidas como punto de partida."""
    async with database.transaction():
        vrows = await database.fetch_all(
            "SELECT version FROM presupuestos WHERE proyecto_id = :pid", {"pid": body.proyecto_id}
        )
        version = _siguiente_version([r["version"] for r in vrows])
        pid = await database.fetch_val(
            """INSERT INTO presupuestos (proyecto_id, version, estado, vigente, nota)
               VALUES (:pid, :v, 'BORRADOR', false, :nota) RETURNING id""",
            {"pid": body.proyecto_id, "v": version, "nota": body.nota},
        )
        sembradas = 0
        if body.sembrar:
            evs = await database.fetch_all(
                """SELECT codigo, descripcion, unidad, fase, sub_fase,
                          metrado_presup, hh_presup, precio_unitario
                   FROM ev_partidas
                   WHERE activo
                     AND otm_id IN (SELECT id FROM otms WHERE proyecto_id = :pid)""",
                {"pid": body.proyecto_id},
            )
            for e in evs:
                await database.execute(
                    """INSERT INTO presupuesto_partidas
                       (presupuesto_id, codigo, descripcion, unidad, fase, sub_fase,
                        metrado, precio_unitario, hh_meta)
                       VALUES (:pp,:c,:d,:u,:f,:sf,:m,:pu,:hh)
                       ON CONFLICT (presupuesto_id, codigo) DO NOTHING""",
                    {"pp": pid, "c": e["codigo"], "d": e["descripcion"], "u": e["unidad"],
                     "f": e["fase"], "sf": e["sub_fase"], "m": e["metrado_presup"],
                     "pu": e["precio_unitario"], "hh": e["hh_presup"]},
                )
                sembradas += 1
    return {"id": pid, "version": version, "estado": "BORRADOR", "sembradas": sembradas}


@router.get("/{pid}")
async def obtener(pid: int):
    p = await database.fetch_one("SELECT * FROM presupuestos WHERE id = :id", {"id": pid})
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    lineas = await database.fetch_all(
        "SELECT * FROM presupuesto_partidas WHERE presupuesto_id = :id ORDER BY codigo", {"id": pid}
    )
    return {"presupuesto": dict(p), "partidas": [dict(l) for l in lineas]}


@router.post("/{pid}/partidas")
async def guardar_lineas(pid: int, body: LineasIn):
    """Agrega/actualiza líneas (upsert por código). Solo permitido en BORRADOR."""
    p = await database.fetch_one("SELECT estado FROM presupuestos WHERE id = :id", {"id": pid})
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] != "BORRADOR":
        raise HTTPException(409, "El presupuesto está CONGELADO; crea una versión nueva para editar.")
    async with database.transaction():
        for ln in body.partidas:
            await database.execute(
                """INSERT INTO presupuesto_partidas
                   (presupuesto_id, codigo, descripcion, unidad, fase, sub_fase,
                    metrado, precio_unitario, hh_meta)
                   VALUES (:pp,:c,:d,:u,:f,:sf,:m,:pu,:hh)
                   ON CONFLICT (presupuesto_id, codigo) DO UPDATE SET
                     descripcion=EXCLUDED.descripcion, unidad=EXCLUDED.unidad,
                     fase=EXCLUDED.fase, sub_fase=EXCLUDED.sub_fase, metrado=EXCLUDED.metrado,
                     precio_unitario=EXCLUDED.precio_unitario, hh_meta=EXCLUDED.hh_meta""",
                {"pp": pid, "c": ln.codigo, "d": ln.descripcion, "u": ln.unidad, "f": ln.fase,
                 "sf": ln.sub_fase, "m": ln.metrado, "pu": ln.precio_unitario, "hh": ln.hh_meta},
            )
    return {"ok": True, "guardadas": len(body.partidas)}


@router.delete("/{pid}/partidas/{codigo}")
async def borrar_linea(pid: int, codigo: str):
    p = await database.fetch_one("SELECT estado FROM presupuestos WHERE id = :id", {"id": pid})
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] != "BORRADOR":
        raise HTTPException(409, "El presupuesto está CONGELADO; crea una versión nueva para editar.")
    await database.execute(
        "DELETE FROM presupuesto_partidas WHERE presupuesto_id = :id AND codigo = :c",
        {"id": pid, "c": codigo},
    )
    return {"ok": True}


@router.post("/{pid}/congelar")
async def congelar(pid: int):
    """Congela la versión, la marca vigente (desactivando la anterior) y SINCRONIZA
    metrado/HH meta/PU hacia ev_partidas (match por código dentro del proyecto)."""
    p = await database.fetch_one("SELECT * FROM presupuestos WHERE id = :id", {"id": pid})
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] == "CONGELADO":
        raise HTTPException(409, "El presupuesto ya está congelado.")
    proyecto_id = p["proyecto_id"]
    async with database.transaction():
        # 1) desactivar la versión vigente anterior (respeta el índice 'un solo vigente')
        await database.execute(
            "UPDATE presupuestos SET vigente = false WHERE proyecto_id = :pid AND vigente",
            {"pid": proyecto_id},
        )
        # 2) congelar + activar esta
        await database.execute(
            "UPDATE presupuestos SET estado = 'CONGELADO', vigente = true, congelado_en = now() WHERE id = :id",
            {"id": pid},
        )
        # 3) sincronizar a ev_partidas (match por código)
        await database.execute(
            """UPDATE ev_partidas ev SET
                  metrado_presup  = pp.metrado,
                  hh_presup       = pp.hh_meta,
                  precio_unitario = pp.precio_unitario
               FROM presupuesto_partidas pp
               WHERE pp.presupuesto_id = :id AND pp.codigo = ev.codigo""",
            {"id": pid},
        )
        total = await database.fetch_val(
            "SELECT count(*) FROM presupuesto_partidas WHERE presupuesto_id = :id", {"id": pid}
        )
        sincronizadas = await database.fetch_val(
            """SELECT count(*) FROM presupuesto_partidas pp
               WHERE pp.presupuesto_id = :id
                 AND EXISTS (SELECT 1 FROM ev_partidas ev WHERE ev.codigo = pp.codigo)""",
            {"id": pid},
        )
    return {
        "ok": True, "estado": "CONGELADO", "vigente": True,
        "lineas": total, "sincronizadas": sincronizadas,
        "no_encontradas": (total or 0) - (sincronizadas or 0),
    }
