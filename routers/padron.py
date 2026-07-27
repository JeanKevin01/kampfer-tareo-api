# ============================================================
# routers/padron.py — padrón de trabajadores y supervisores
# (CRUD, búsqueda, duplicados y fusión)
# ============================================================
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db as core_db
from core.personal import alta_persona

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
    """Alta de personal. TODA persona entra al padrón de trabajadores —directos,
    indirectos y supervisores—; `es_supervisor=true` añade el ROL (ficha de
    supervisor ligada + acceso a la app con la clave inicial).

    Idempotente: si la persona ya existe (por DNI o nombre) se REUTILIZA su
    perfil —y su contraseña actual si ya tenía acceso— en vez de duplicarla.
    """
    nombre = data.get("nombre", "").strip().upper()
    cargo  = data.get("cargo",  "").strip().upper()
    dni    = data.get("dni",    "").strip()
    tipo   = data.get("tipo",   "").strip().upper()
    es_sup = bool(data.get("es_supervisor", False))

    if not nombre or not cargo:
        raise HTTPException(400, "Nombre y cargo son requeridos")

    if tipo not in ("DIRECTO", "INDIRECTO"):
        tipo = "DIRECTO"

    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            r = await alta_persona(con, nombre, cargo, dni, tipo,
                                   es_supervisor=es_sup, email=data.get("email", ""))
    return {"status": "ok", "id": r["id"], "nombre": r["nombre"], "cargo": cargo, "tipo": tipo,
            "nuevo": r["nuevo"], "supervisor_id": r["supervisor_id"],
            "usuario": r["usuario"], "password": r["password"]}


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
        "SELECT id, nombre, email, activo, trabajador_id FROM supervisores "
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
    async with pool.acquire() as con:
        async with con.transaction():
            # Un supervisor es personal del proyecto: entra al padrón de
            # trabajadores (reutilizando su ficha si ya estaba) y encima recibe
            # el rol y su acceso a la app con la clave inicial.
            r = await alta_persona(con, nombre, data.get("cargo", "SUPERVISOR"),
                                   data.get("dni", ""), "INDIRECTO",
                                   es_supervisor=True, email=email)
    return {"status": "ok", "id": r["supervisor_id"], "nombre": r["nombre"],
            "trabajador_id": r["id"], "usuario": r["usuario"], "password": r["password"]}


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


# ── Histograma de personal (encargo Jean 2026-07-26) ─────────
# El histograma de MO del Anexo 01 solo existía por DÍA y en ventanas de
# semanas. Para mirar la curva de personal de toda la obra hace falta poder
# agrupar por semana y por MES, que es como se conversa con la gerencia
# («en agosto tuvimos 40 personas»).
_AGRUPACIONES = ("dia", "semana", "mes")


def _clave_periodo(agrupar: str) -> str:
    """Expresión SQL que reduce la fecha al inicio de su periodo."""
    if agrupar == "mes":
        return "date_trunc('month', tp.fecha)::date"
    if agrupar == "semana":                      # lunes ISO
        return "(tp.fecha - ((EXTRACT(ISODOW FROM tp.fecha)::int) - 1))"
    return "tp.fecha"


@router.get("/api/histograma-personal")
async def histograma_personal(desde: str = "", hasta: str = "", agrupar: str = "dia",
                              otm: str = "", _u: dict = Depends(require_role("oficina"))):
    """Cuánta gente trabajó y cuántas HH, por día / semana / mes.

    Fuente: `tareo_partida` (el tareo QR real). Por periodo devuelve:
      · `trabajadores`  personas DISTINTAS que aparecieron (no la suma de días:
        el mismo obrero en 20 días de agosto cuenta una vez en el mes);
      · `pico` y `promedio_dia`  máximo y promedio de personal por día dentro
        del periodo — lo que de verdad describe la curva de MO de un mes;
      · `hh` y el desglose `por_cargo` (personas distintas por cargo).

    Sin fechas toma los últimos 12 meses de datos que existan."""
    from datetime import date, timedelta
    agrupar = str(agrupar or "dia").strip().lower()
    if agrupar not in _AGRUPACIONES:
        raise HTTPException(422, f"agrupar inválido (usa {'/'.join(_AGRUPACIONES)})")
    pool = await core_db()
    async with pool.acquire() as con:
        lim = await con.fetchrow(
            "SELECT MIN(fecha) AS ini, MAX(fecha) AS fin FROM tareo_partida WHERE hh > 0")
        if not lim or lim["fin"] is None:
            return {"desde": None, "hasta": None, "agrupar": agrupar, "periodos": [],
                    "totales": {"trabajadores": 0, "hh": 0.0, "dias": 0}}
        try:
            f_hasta = date.fromisoformat(hasta) if hasta else lim["fin"]
            f_desde = date.fromisoformat(desde) if desde else max(
                lim["ini"], f_hasta - timedelta(days=365))
        except ValueError:
            raise HTTPException(400, "Fechas inválidas: usa AAAA-MM-DD")
        if f_desde > f_hasta:
            raise HTTPException(400, "«desde» no puede ser posterior a «hasta»")
        otm_f = otm.strip() or None
        clave = _clave_periodo(agrupar)

        filas = await con.fetch(
            f"""SELECT {clave} AS periodo,
                       COUNT(DISTINCT tp.trabajador_id) AS trabajadores,
                       COUNT(DISTINCT tp.fecha) AS dias,
                       SUM(tp.hh) AS hh
                  FROM tareo_partida tp
                 WHERE tp.fecha BETWEEN $1 AND $2 AND tp.hh IS NOT NULL AND tp.hh > 0
                   AND ($3::text IS NULL OR tp.otm_id = $3)
                 GROUP BY 1 ORDER BY 1""", f_desde, f_hasta, otm_f)
        # Personal por DÍA dentro de cada periodo: de aquí salen el pico y el
        # promedio (con agrupar=dia coincide con `trabajadores`).
        por_dia = await con.fetch(
            f"""SELECT {clave} AS periodo, tp.fecha,
                       COUNT(DISTINCT tp.trabajador_id) AS n
                  FROM tareo_partida tp
                 WHERE tp.fecha BETWEEN $1 AND $2 AND tp.hh IS NOT NULL AND tp.hh > 0
                   AND ($3::text IS NULL OR tp.otm_id = $3)
                 GROUP BY 1, 2""", f_desde, f_hasta, otm_f)
        cargos = await con.fetch(
            f"""SELECT {clave} AS periodo,
                       COALESCE(NULLIF(TRIM(t.cargo), ''), 'SIN CARGO') AS cargo,
                       COUNT(DISTINCT tp.trabajador_id) AS n
                  FROM tareo_partida tp
                  LEFT JOIN trabajadores t ON t.id = tp.trabajador_id
                 WHERE tp.fecha BETWEEN $1 AND $2 AND tp.hh IS NOT NULL AND tp.hh > 0
                   AND ($3::text IS NULL OR tp.otm_id = $3)
                 GROUP BY 1, 2""", f_desde, f_hasta, otm_f)
        total = await con.fetchrow(
            """SELECT COUNT(DISTINCT trabajador_id) AS trabajadores,
                      COUNT(DISTINCT fecha) AS dias, COALESCE(SUM(hh), 0) AS hh
                 FROM tareo_partida
                WHERE fecha BETWEEN $1 AND $2 AND hh IS NOT NULL AND hh > 0
                  AND ($3::text IS NULL OR otm_id = $3)""", f_desde, f_hasta, otm_f)

    dias_de: dict = {}
    for r in por_dia:
        dias_de.setdefault(r["periodo"], []).append(r["n"])
    cargos_de: dict = {}
    for r in cargos:
        cargos_de.setdefault(r["periodo"], {})[r["cargo"]] = r["n"]

    periodos = []
    for r in filas:
        ns = dias_de.get(r["periodo"], [])
        periodos.append({
            "periodo": str(r["periodo"]),
            "trabajadores": r["trabajadores"],
            "dias": r["dias"],
            "hh": round(float(r["hh"] or 0), 2),
            "pico": max(ns) if ns else 0,
            "promedio_dia": round(sum(ns) / len(ns), 1) if ns else 0.0,
            "por_cargo": dict(sorted(cargos_de.get(r["periodo"], {}).items(),
                                     key=lambda kv: -kv[1])),
        })
    return {
        "desde": str(f_desde), "hasta": str(f_hasta), "agrupar": agrupar,
        "otm": otm_f, "periodos": periodos,
        "totales": {"trabajadores": total["trabajadores"], "dias": total["dias"],
                    "hh": round(float(total["hh"] or 0), 2)},
    }
