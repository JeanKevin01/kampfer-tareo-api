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

# F0.4: este router usa el pool asyncpg único (core.db); la lib `databases` salió de aquí.
from core.db import db

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
    pool = await db()
    rows = await pool.fetch(
        """SELECT p.*,
                  (SELECT count(*) FROM presupuesto_partidas pp WHERE pp.presupuesto_id = p.id) AS lineas
           FROM presupuestos p
           WHERE p.proyecto_id = $1
           ORDER BY p.version DESC""",
        proyecto_id,
    )
    return [dict(r) for r in rows]


@router.post("")
async def crear(body: CrearIn):
    """Crea una versión nueva en BORRADOR. Si sembrar=True, copia las partidas
    activas del proyecto desde ev_partidas como punto de partida."""
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            vrows = await con.fetch(
                "SELECT version FROM presupuestos WHERE proyecto_id = $1", body.proyecto_id
            )
            version = _siguiente_version([r["version"] for r in vrows])
            pid = await con.fetchval(
                """INSERT INTO presupuestos (proyecto_id, version, estado, vigente, nota)
                   VALUES ($1, $2, 'BORRADOR', false, $3) RETURNING id""",
                body.proyecto_id, version, body.nota,
            )
            sembradas = 0
            if body.sembrar:
                evs = await con.fetch(
                    """SELECT codigo, descripcion, unidad, fase, sub_fase,
                              metrado_presup, hh_presup, precio_unitario
                       FROM ev_partidas
                       WHERE activo
                         AND otm_id IN (SELECT id FROM otms WHERE proyecto_id = $1)""",
                    body.proyecto_id,
                )
                for e in evs:
                    await con.execute(
                        """INSERT INTO presupuesto_partidas
                           (presupuesto_id, codigo, descripcion, unidad, fase, sub_fase,
                            metrado, precio_unitario, hh_meta)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                           ON CONFLICT (presupuesto_id, codigo) DO NOTHING""",
                        pid, e["codigo"], e["descripcion"], e["unidad"],
                        e["fase"], e["sub_fase"], e["metrado_presup"],
                        e["precio_unitario"], e["hh_presup"],
                    )
                    sembradas += 1
    return {"id": pid, "version": version, "estado": "BORRADOR", "sembradas": sembradas}


@router.get("/{pid}")
async def obtener(pid: int):
    pool = await db()
    p = await pool.fetchrow("SELECT * FROM presupuestos WHERE id = $1", pid)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    lineas = await pool.fetch(
        "SELECT * FROM presupuesto_partidas WHERE presupuesto_id = $1 ORDER BY codigo", pid
    )
    return {"presupuesto": dict(p), "partidas": [dict(l) for l in lineas]}


@router.post("/{pid}/partidas")
async def guardar_lineas(pid: int, body: LineasIn):
    """Agrega/actualiza líneas (upsert por código). Solo permitido en BORRADOR."""
    pool = await db()
    p = await pool.fetchrow("SELECT estado FROM presupuestos WHERE id = $1", pid)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] != "BORRADOR":
        raise HTTPException(409, "El presupuesto está CONGELADO; crea una versión nueva para editar.")
    async with pool.acquire() as con:
        async with con.transaction():
            for ln in body.partidas:
                await con.execute(
                    """INSERT INTO presupuesto_partidas
                       (presupuesto_id, codigo, descripcion, unidad, fase, sub_fase,
                        metrado, precio_unitario, hh_meta)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                       ON CONFLICT (presupuesto_id, codigo) DO UPDATE SET
                         descripcion=EXCLUDED.descripcion, unidad=EXCLUDED.unidad,
                         fase=EXCLUDED.fase, sub_fase=EXCLUDED.sub_fase, metrado=EXCLUDED.metrado,
                         precio_unitario=EXCLUDED.precio_unitario, hh_meta=EXCLUDED.hh_meta""",
                    pid, ln.codigo, ln.descripcion, ln.unidad, ln.fase,
                    ln.sub_fase, ln.metrado, ln.precio_unitario, ln.hh_meta,
                )
    return {"ok": True, "guardadas": len(body.partidas)}


@router.delete("/{pid}/partidas/{codigo}")
async def borrar_linea(pid: int, codigo: str):
    pool = await db()
    p = await pool.fetchrow("SELECT estado FROM presupuestos WHERE id = $1", pid)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] != "BORRADOR":
        raise HTTPException(409, "El presupuesto está CONGELADO; crea una versión nueva para editar.")
    await pool.execute(
        "DELETE FROM presupuesto_partidas WHERE presupuesto_id = $1 AND codigo = $2", pid, codigo
    )
    return {"ok": True}


@router.post("/{pid}/congelar")
async def congelar(pid: int):
    """Congela la versión, la marca vigente (desactivando la anterior) y SINCRONIZA
    metrado/HH meta/PU hacia ev_partidas (match por código dentro del proyecto)."""
    pool = await db()
    p = await pool.fetchrow("SELECT * FROM presupuestos WHERE id = $1", pid)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] == "CONGELADO":
        raise HTTPException(409, "El presupuesto ya está congelado.")
    proyecto_id = p["proyecto_id"]
    async with pool.acquire() as con:
        async with con.transaction():
            # 1) desactivar la versión vigente anterior (respeta el índice 'un solo vigente')
            await con.execute(
                "UPDATE presupuestos SET vigente = false WHERE proyecto_id = $1 AND vigente",
                proyecto_id,
            )
            # 2) congelar + activar esta
            await con.execute(
                "UPDATE presupuestos SET estado = 'CONGELADO', vigente = true, congelado_en = now() WHERE id = $1",
                pid,
            )
            # 3) sincronizar a ev_partidas (match por código, SOLO en OTMs del proyecto —
            #    con unicidad por OTM (0008) el mismo código puede existir en otros proyectos)
            await con.execute(
                """UPDATE ev_partidas ev SET
                      metrado_presup  = pp.metrado,
                      hh_presup       = pp.hh_meta,
                      precio_unitario = pp.precio_unitario
                   FROM presupuesto_partidas pp
                   WHERE pp.presupuesto_id = $1 AND pp.codigo = ev.codigo
                     AND (ev.otm_id IN (SELECT id FROM otms WHERE proyecto_id = $2)
                          OR ev.otm_id IS NULL)""",
                pid, proyecto_id,
            )
            total = await con.fetchval(
                "SELECT count(*) FROM presupuesto_partidas WHERE presupuesto_id = $1", pid
            )
            sincronizadas = await con.fetchval(
                """SELECT count(*) FROM presupuesto_partidas pp
                   WHERE pp.presupuesto_id = $1
                     AND EXISTS (SELECT 1 FROM ev_partidas ev
                                 WHERE ev.codigo = pp.codigo
                                   AND (ev.otm_id IN (SELECT id FROM otms WHERE proyecto_id = $2)
                                        OR ev.otm_id IS NULL))""",
                pid, proyecto_id,
            )
    return {
        "ok": True, "estado": "CONGELADO", "vigente": True,
        "lineas": total, "sincronizadas": sincronizadas,
        "no_encontradas": (total or 0) - (sincronizadas or 0),
    }
