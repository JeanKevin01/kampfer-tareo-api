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
async def listar(proyecto_id: int = 1, tipo: Optional[str] = None):
    """Versiones del proyecto. `tipo` opcional: META | CONTRACTUAL (F1: dos líneas base)."""
    pool = await db()
    filtro = "AND p.tipo = $2" if tipo else ""
    args = [proyecto_id] + ([tipo.upper()] if tipo else [])
    rows = await pool.fetch(
        f"""SELECT p.*,
                  (SELECT count(*) FROM presupuesto_partidas pp WHERE pp.presupuesto_id = p.id) AS lineas
           FROM presupuestos p
           WHERE p.proyecto_id = $1 {filtro}
           ORDER BY p.tipo, p.version DESC""",
        *args,
    )
    return [dict(r) for r in rows]


@router.post("")
async def crear(body: CrearIn):
    """Crea una versión nueva CONTRACTUAL en BORRADOR (la META nace del import de
    la plantilla PU). Si sembrar=True, copia las partidas activas del proyecto
    desde ev_partidas como punto de partida."""
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            vrows = await con.fetch(
                "SELECT version FROM presupuestos WHERE proyecto_id = $1 AND tipo = 'CONTRACTUAL'",
                body.proyecto_id
            )
            version = _siguiente_version([r["version"] for r in vrows])
            pid = await con.fetchval(
                """INSERT INTO presupuestos (proyecto_id, version, estado, vigente, nota, tipo)
                   VALUES ($1, $2, 'BORRADOR', false, $3, 'CONTRACTUAL') RETURNING id""",
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


@router.get("/partida/{ppid}/apu")
async def apu_de_partida(ppid: int):
    """Recursos del APU de una línea del presupuesto (F1: lo usa el expandible
    del panel). Incluye el código de la subpartida enlazada si aplica."""
    pool = await db()
    rows = await pool.fetch(
        """SELECT ar.*, sp.codigo AS sub_codigo, sp.descripcion AS sub_descripcion
           FROM apu_recursos ar
           LEFT JOIN presupuesto_partidas sp ON sp.id = ar.sub_partida_id
           WHERE ar.presupuesto_partida_id = $1
           ORDER BY ar.orden""", ppid)
    return [dict(r) for r in rows]


async def _materializar_costo_meta(con, pid: int) -> int:
    """F1.3: recalcula presupuesto_costo_meta (delete + insert) desde el APU.
    Expande subpartidas a sus tipos reales vía las funciones puras de derivados."""
    from routers.presupuesto_derivados import costo_meta_por_fase_recurso

    pps = await con.fetch(
        "SELECT id, fase, metrado, nivel, precio_unitario "
        "FROM presupuesto_partidas WHERE presupuesto_id = $1", pid)
    recs = await con.fetch(
        """SELECT ar.presupuesto_partida_id AS pp_id, ar.tipo, ar.cantidad::float AS cantidad,
                  ar.precio::float AS precio, ar.parcial::float AS parcial, ar.sub_partida_id
           FROM apu_recursos ar
           JOIN presupuesto_partidas pp ON pp.id = ar.presupuesto_partida_id
           WHERE pp.presupuesto_id = $1 ORDER BY ar.orden""", pid)
    por_pp: dict = {}
    for r in recs:
        por_pp.setdefault(r["pp_id"], []).append(
            {"tipo": r["tipo"], "cantidad": r["cantidad"], "parcial": r["parcial"],
             "sub": r["sub_partida_id"]})
    partidas = [{"fase": p["fase"], "metrado": float(p["metrado"] or 0),
                 "pu": float(p["precio_unitario"] or 0),
                 "recursos": por_pp.get(p["id"], [])}
                for p in pps if (p["nivel"] or 1) >= 1]     # nivel 0 = subpartidas
    costos = costo_meta_por_fase_recurso(
        partidas, obtener_sub=lambda rec: por_pp.get(rec.get("sub")))

    await con.execute("DELETE FROM presupuesto_costo_meta WHERE presupuesto_id = $1", pid)
    # El esquema modela EQ y SUB por separado; tras la expansión solo quedan SUB
    # cuando la subpartida no se pudo resolver (se conservan visibles como SUB).
    for (fase, tipo), monto in sorted(costos.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        await con.execute(
            """INSERT INTO presupuesto_costo_meta (presupuesto_id, fase, tipo_recurso, monto)
               VALUES ($1, $2, $3, $4)""", pid, fase, tipo, round(monto, 2))
    return len(costos)


@router.get("/{pid}/costo-meta")
async def costo_meta(pid: int):
    """Costo meta materializado por (fase, tipo de recurso) — columna 'Meta' del RO."""
    pool = await db()
    rows = await pool.fetch(
        "SELECT fase, tipo_recurso, monto::float AS monto FROM presupuesto_costo_meta "
        "WHERE presupuesto_id = $1 ORDER BY fase, tipo_recurso", pid)
    return [dict(r) for r in rows]


@router.post("/{pid}/congelar")
async def congelar(pid: int):
    """Congela la versión, la marca vigente (desactivando la anterior DE SU TIPO)
    y sincroniza hacia ev_partidas según el tipo (F1.3):
      · META        → hh_presup (HH meta del APU) + materializa presupuesto_costo_meta.
      · CONTRACTUAL → metrado_presup + precio_unitario (la venta del RO)."""
    pool = await db()
    p = await pool.fetchrow("SELECT * FROM presupuestos WHERE id = $1", pid)
    if not p:
        raise HTTPException(404, "Presupuesto no encontrado")
    if p["estado"] == "CONGELADO":
        raise HTTPException(409, "El presupuesto ya está congelado.")
    proyecto_id, tipo = p["proyecto_id"], p["tipo"]
    celdas_costo = 0
    async with pool.acquire() as con:
        async with con.transaction():
            # 1) desactivar la versión vigente anterior DE ESTE TIPO
            #    (el índice parcial de 0012 garantiza un vigente por tipo)
            await con.execute(
                "UPDATE presupuestos SET vigente = false "
                "WHERE proyecto_id = $1 AND vigente AND tipo = $2",
                proyecto_id, tipo,
            )
            # 2) congelar + activar esta
            await con.execute(
                "UPDATE presupuestos SET estado = 'CONGELADO', vigente = true, congelado_en = now() WHERE id = $1",
                pid,
            )
            # 3) sincronizar a ev_partidas (match por código, SOLO en OTMs del proyecto —
            #    con unicidad por OTM (0008) el mismo código puede existir en otros proyectos)
            if tipo == "META":
                await con.execute(
                    """UPDATE ev_partidas ev SET hh_presup = pp.hh_meta
                       FROM presupuesto_partidas pp
                       WHERE pp.presupuesto_id = $1 AND pp.codigo = ev.codigo
                         AND COALESCE(pp.nivel, 1) >= 1
                         AND (ev.otm_id IN (SELECT id FROM otms WHERE proyecto_id = $2)
                              OR ev.otm_id IS NULL)""",
                    pid, proyecto_id,
                )
                celdas_costo = await _materializar_costo_meta(con, pid)
            else:
                await con.execute(
                    """UPDATE ev_partidas ev SET
                          metrado_presup  = pp.metrado,
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
        "ok": True, "estado": "CONGELADO", "vigente": True, "tipo": tipo,
        "lineas": total, "sincronizadas": sincronizadas,
        "no_encontradas": (total or 0) - (sincronizadas or 0),
        "celdas_costo_meta": celdas_costo,
    }
