# ============================================================
# routers/padron.py — padrón de trabajadores y supervisores
# (CRUD, búsqueda, duplicados y fusión)
# ============================================================
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db as core_db

router = APIRouter(tags=["padron"])


# ── TRABAJADORES (lectura) ───────────────────────────────────
@router.get("/api/trabajadores")
async def get_trabajadores():
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT id, nombre, cargo FROM trabajadores WHERE activo = true ORDER BY nombre"
    )
    return [dict(r) for r in rows]


@router.get("/api/buscar")
async def buscar(q: str):
    if len(q) < 2:
        return []
    pool = await core_db()
    rows = await pool.fetch(
        """SELECT id, nombre, cargo FROM trabajadores
           WHERE activo = true AND (
             nombre ILIKE $1 OR cargo ILIKE $1 OR id = $2
           ) ORDER BY nombre LIMIT 8""",
        f"%{q}%", q.zfill(3),
    )
    return [dict(r) for r in rows]


# ── INTEGRIDAD: duplicados y fusión ──────────────────────────
def _norm_nombre(s: Optional[str]) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


@router.get("/api/trabajadores/duplicados")
async def trabajadores_duplicados():
    """Agrupa trabajadores con el mismo nombre normalizado pero distinto id,
    con su actividad (para decidir cuál conservar)."""
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT t.id, t.nombre, t.cargo, t.activo, "
        "  (SELECT COUNT(*) FROM tareo_partida tp WHERE tp.trabajador_id = t.id) AS n_tareo, "
        "  (SELECT COUNT(*) FROM registros r WHERE r.trab_id = t.id)             AS n_reg "
        "FROM trabajadores t ORDER BY t.nombre, t.id"
    )
    grupos: dict = {}
    for r in rows:
        grupos.setdefault(_norm_nombre(r["nombre"]), []).append({
            "id": r["id"], "nombre": r["nombre"], "cargo": r["cargo"],
            "activo": r["activo"], "n_tareo": int(r["n_tareo"] or 0), "n_reg": int(r["n_reg"] or 0),
        })
    dup = [{"nombre": g[0]["nombre"], "miembros": g} for g in grupos.values() if len(g) > 1]
    dup.sort(key=lambda x: x["nombre"])
    return {"total_grupos": len(dup), "grupos": dup}


@router.post("/api/trabajadores/merge")
async def trabajadores_merge(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Fusiona un trabajador duplicado en otro: reasigna TODAS las referencias
    (vía information_schema) y desactiva el origen. Transaccional: si hay colisión
    de claves únicas, revierte todo y reporta la tabla, sin pérdida de datos."""
    origen  = str(data.get("from_id", "")).strip()
    destino = str(data.get("to_id", "")).strip()
    if not origen or not destino or origen == destino:
        raise HTTPException(400, "from_id y to_id deben ser válidos y distintos")

    pool = await core_db()
    cols = await pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name <> 'trabajadores' "
        "AND column_name IN ('trab_id','trabajador_id','miembro_id')"
    )
    async with pool.acquire() as con:
        async with con.transaction():
            for c in cols:
                tn, cn = c["table_name"], c["column_name"]
                try:
                    # Savepoint por tabla: una colisión no aborta la transacción entera
                    # hasta decidir el mensaje (el raise de abajo revierte todo igual).
                    async with con.transaction():
                        await con.execute(
                            f'UPDATE "{tn}" SET "{cn}" = $1 WHERE "{cn}" = $2',
                            destino, origen,
                        )
                except Exception as e:
                    raise HTTPException(
                        409, f"Colisión al reasignar {tn}.{cn} ({e}). "
                             f"El trabajador {origen} ya tiene datos que chocan con {destino} "
                             f"en esa tabla. Revisa/elimina ese registro y reintenta.")
            await con.execute(
                "UPDATE trabajadores SET activo = false WHERE id = $1", origen)
    return {"ok": True, "fusionado": origen, "en": destino, "tablas": len(cols)}


# ── TRABAJADORES (admin CRUD) ────────────────────────────────
@router.post("/admin/trabajador")
async def crear_trabajador(data: dict, _u: dict = Depends(require_role("oficina"))):
    nombre = data.get("nombre", "").strip().upper()
    cargo  = data.get("cargo",  "").strip().upper()
    dni    = data.get("dni",    "").strip()
    tipo   = data.get("tipo",   "").strip().upper()

    if not nombre or not cargo:
        raise HTTPException(400, "Nombre y cargo son requeridos")

    if tipo not in ("DIRECTO", "INDIRECTO"):
        tipo = "DIRECTO"

    pool = await core_db()
    max_id = await pool.fetchval(
        r"SELECT MAX(CAST(id AS INTEGER)) FROM trabajadores WHERE id ~ '^\d+$'")
    next_id = str((max_id or 0) + 1).zfill(3)

    await pool.execute(
        "INSERT INTO trabajadores (id, nombre, cargo, dni, tipo) "
        "VALUES ($1, $2, $3, $4, $5)",
        next_id, nombre, cargo, dni, tipo,
    )
    return {"status": "ok", "id": next_id, "nombre": nombre, "cargo": cargo, "tipo": tipo}


@router.get("/admin/trabajadores")
async def listar_trabajadores():
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT id, nombre, cargo, dni, COALESCE(tipo,'DIRECTO') AS tipo, activo "
        r"FROM trabajadores ORDER BY (CASE WHEN id ~ '^\d+$' THEN CAST(id AS INTEGER) END) "
        "NULLS LAST, id"
    )
    return [dict(r) for r in rows]


@router.put("/admin/trabajador/{trab_id}/baja")
async def dar_baja(trab_id: str, _u: dict = Depends(require_role("oficina"))):
    pool = await core_db()
    await pool.execute(
        "UPDATE trabajadores SET activo = false WHERE id = $1", trab_id.zfill(3))
    return {"status": "ok"}


@router.put("/admin/trabajador/{trab_id}")
async def editar_trabajador(trab_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
    pool = await core_db()
    row = await pool.fetchrow("SELECT id FROM trabajadores WHERE id = $1", trab_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    tipo = data.get("tipo", "").strip().upper()
    if tipo not in ("DIRECTO", "INDIRECTO"):
        tipo = None
    nombre = data.get("nombre", "").upper().strip()
    cargo  = data.get("cargo",  "").upper().strip()
    dni    = data.get("dni",    "")
    if tipo:
        await pool.execute(
            "UPDATE trabajadores SET nombre = $1, cargo = $2, dni = $3, tipo = $4 WHERE id = $5",
            nombre, cargo, dni, tipo, trab_id,
        )
    else:
        await pool.execute(
            "UPDATE trabajadores SET nombre = $1, cargo = $2, dni = $3 WHERE id = $4",
            nombre, cargo, dni, trab_id,
        )
    updated = await pool.fetchrow(
        "SELECT id, nombre, cargo, dni, COALESCE(tipo,'DIRECTO') AS tipo "
        "FROM trabajadores WHERE id = $1", trab_id)
    return dict(updated)


# ── SUPERVISORES ─────────────────────────────────────────────
@router.get("/api/supervisores")
async def get_supervisores():
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT id, nombre, email FROM supervisores WHERE activo = true ORDER BY nombre"
    )
    return [dict(r) for r in rows]


@router.get("/admin/supervisores")
async def listar_supervisores_admin():
    pool = await core_db()
    # Orden numérico solo para ids numéricos (un id atípico no debe romper el listado).
    rows = await pool.fetch(
        "SELECT id, nombre, email, activo FROM supervisores "
        r"ORDER BY (CASE WHEN id ~ '^\d+$' THEN CAST(id AS INTEGER) END) NULLS LAST, id"
    )
    return [dict(r) for r in rows]


@router.post("/admin/supervisor")
async def crear_supervisor(data: dict, _u: dict = Depends(require_role("oficina"))):
    nombre = data.get("nombre", "").strip().upper()
    email  = data.get("email",  "").strip()

    if not nombre:
        raise HTTPException(400, "Nombre es requerido")

    pool = await core_db()
    max_id = await pool.fetchval(
        r"SELECT MAX(CAST(id AS INTEGER)) FROM supervisores WHERE id ~ '^\d+$'")
    next_id = str((max_id or 0) + 1).zfill(2)

    await pool.execute(
        "INSERT INTO supervisores (id, nombre, email, activo) VALUES ($1, $2, $3, true)",
        next_id, nombre, email,
    )
    return {"status": "ok", "id": next_id, "nombre": nombre}


@router.put("/admin/supervisor/{sup_id}")
async def editar_supervisor(sup_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
    pool = await core_db()
    row = await pool.fetchrow("SELECT id FROM supervisores WHERE id = $1", sup_id)
    if not row:
        raise HTTPException(404, "Supervisor no encontrado")
    await pool.execute(
        "UPDATE supervisores SET nombre = $1, email = $2 WHERE id = $3",
        data.get("nombre", "").upper().strip(), data.get("email", "").strip(), sup_id,
    )
    updated = await pool.fetchrow(
        "SELECT id, nombre, email FROM supervisores WHERE id = $1", sup_id)
    return dict(updated)


@router.put("/admin/supervisor/{sup_id}/baja")
async def dar_baja_supervisor(sup_id: str, _u: dict = Depends(require_role("oficina"))):
    pool = await core_db()
    await pool.execute("UPDATE supervisores SET activo = false WHERE id = $1", sup_id)
    return {"status": "ok"}
