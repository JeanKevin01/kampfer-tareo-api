# ============================================================
# routers/padron.py — padrón de trabajadores y supervisores
# (CRUD, búsqueda, duplicados y fusión)
# ============================================================
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db as core_db
from core.personal import (alta_persona, asegurar_supervisor,
                           crear_usuario_supervisor)
from core.tiempo import fecha_lima, parse_fecha

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


@router.post("/admin/supervisor/desde-trabajador")
async def nombrar_supervisor(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Da el rol de supervisor a alguien que YA está en el padrón.

    El camino natural: el supervisor es personal del proyecto, así que primero
    existe como trabajador y después se le nombra. Escribir el nombre a mano
    crea una segunda ficha de la misma persona con otro id — y a partir de ahí
    sus HH y sus partes viven en dos sitios.
    """
    trab_id = str(data.get("trabajador_id") or "").strip()
    if not trab_id:
        raise HTTPException(400, "trabajador_id es requerido")
    pool = await core_db()
    async with pool.acquire() as con:
        t = await con.fetchrow(
            "SELECT id, nombre, tipo FROM trabajadores WHERE id = $1", trab_id)
        if not t:
            raise HTTPException(404, "Ese trabajador no está en el padrón")
        async with con.transaction():
            sup = await asegurar_supervisor(con, t["id"], t["nombre"],
                                            str(data.get("email") or "").strip())
            acceso = await crear_usuario_supervisor(con, sup["id"], t["nombre"])
            # Quien reporta es staff: si estaba como DIRECTO fue un default del
            # alta, no una decisión.
            if (t["tipo"] or "DIRECTO") != "INDIRECTO":
                await con.execute(
                    "UPDATE trabajadores SET tipo = 'INDIRECTO' WHERE id = $1", t["id"])
    return {"status": "ok", "id": sup["id"], "nombre": t["nombre"],
            "trabajador_id": t["id"], "nuevo": sup["nuevo"],
            "usuario": acceso["username"] if acceso else None,
            "password": acceso["password"] if acceso else None}


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


# ── Matriz de cumplimiento del reporte (encargo Jean 2026-07-28) ──
# «Quiero ver por semanas qué fechas reportaron los supervisores y si solo
#  reportaron HH, si subieron imágenes, si describieron sus actividades, si
#  pusieron restricciones o si marcaron que no se hizo tal actividad.»
#
# El estado diario ya decía si reportó o no. Esto dice QUÉ reportó: un parte con
# HH y nada más no es lo mismo que uno con fotos, descripción y las trabas del
# día. Sin verlo por semanas no se distingue al que reporta completo del que
# manda las horas y se olvida del resto.
def _dias(desde: date, hasta: date) -> list:
    return [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]


def _semanas_de(fechas: list) -> list:
    """Bloques de semana ISO para la cabecera doble de la matriz."""
    out: list = []
    for f in fechas:
        lun = f - timedelta(days=f.isoweekday() - 1)
        if out and out[-1]["lunes"] == str(lun):
            out[-1]["n"] += 1
        else:
            out.append({"lunes": str(lun), "n": 1})
    return out


CELDA_VACIA = {"hh": 0.0, "trab": 0, "partes": 0, "fotos": 0,
               "desc": False, "rest": 0, "nc": 0}


def pivotar_reportes(fechas: list, supervisores: list, hh: list, partes: list,
                     fotos: list, nc: list) -> list:
    """Arma las filas (supervisor × fecha) de la matriz.

    Cada entrada llega como (supervisor_id, fecha, …). Función pura: el SQL de
    arriba solo agrega, y aquí se decide qué significa cada señal.
    """
    idx = {str(f): f for f in fechas}
    filas = {}
    for s in supervisores:
        filas[s["id"]] = {"supervisor_id": s["id"], "nombre": s["nombre"],
                          "celdas": {}, "tot": dict(CELDA_VACIA, dias=0)}

    def celda(sid, fecha):
        fila = filas.get(sid)
        if fila is None or str(fecha) not in idx:
            return None
        return fila["celdas"].setdefault(str(fecha), dict(CELDA_VACIA))

    for sid, fecha, horas, ntrab in hh:
        c = celda(sid, fecha)
        if c is not None:
            c["hh"] += float(horas or 0)
            c["trab"] += int(ntrab or 0)
    for sid, fecha, n, con_desc, n_rest in partes:
        c = celda(sid, fecha)
        if c is not None:
            c["partes"] += int(n or 0)
            c["desc"] = c["desc"] or bool(con_desc)
            c["rest"] += int(n_rest or 0)
    for sid, fecha, n in fotos:
        c = celda(sid, fecha)
        if c is not None:
            c["fotos"] += int(n or 0)
    for sid, fecha, n in nc:
        c = celda(sid, fecha)
        if c is not None:
            c["nc"] += int(n or 0)

    for fila in filas.values():
        tot = fila["tot"]
        for c in fila["celdas"].values():
            tot["hh"] += c["hh"]; tot["trab"] += c["trab"]
            tot["partes"] += c["partes"]; tot["fotos"] += c["fotos"]
            tot["rest"] += c["rest"]; tot["nc"] += c["nc"]
            tot["desc"] = tot["desc"] or c["desc"]
            # Un día cuenta como reportado si hubo CUALQUIER señal, no solo HH:
            # el parte con fotos y descripción pero sin tareo también es reporte.
            if c["hh"] or c["partes"] or c["fotos"] or c["nc"]:
                tot["dias"] += 1
        tot["hh"] = round(tot["hh"], 2)
        for c in fila["celdas"].values():
            c["hh"] = round(c["hh"], 2)
    return sorted(filas.values(), key=lambda f: f["nombre"])


@router.get("/admin/supervisores/matriz")
async def matriz_supervisores(desde: str = "", hasta: str = "", proyecto_id: int = 1,
                              _u: dict = Depends(require_role("oficina"))):
    """Qué reportó cada supervisor, día por día, en el rango pedido."""
    f_hasta = parse_fecha(hasta) or fecha_lima()
    f_desde = parse_fecha(desde) or (f_hasta - timedelta(days=27))
    if f_desde > f_hasta:
        f_desde, f_hasta = f_hasta, f_desde
    # Tope de 16 semanas: más no cabe en pantalla ni se lee.
    if (f_hasta - f_desde).days > 16 * 7:
        f_desde = f_hasta - timedelta(days=16 * 7 - 1)
    # La cuadrícula arranca en lunes y termina en domingo: es una vista semanal.
    f_desde -= timedelta(days=f_desde.isoweekday() - 1)
    f_hasta += timedelta(days=7 - f_hasta.isoweekday())
    fechas = _dias(f_desde, f_hasta)

    pool = await core_db()
    async with pool.acquire() as con:
        sups = [dict(r) for r in await con.fetch(
            "SELECT id, nombre FROM supervisores WHERE activo = true ORDER BY nombre")]
        hh = [(r["supervisor_id"], r["fecha"], r["h"], r["n"]) for r in await con.fetch(
            """SELECT supervisor_id, fecha, SUM(hh) AS h,
                      COUNT(DISTINCT trabajador_id) AS n
                 FROM tareo_partida
                WHERE fecha BETWEEN $1 AND $2 AND supervisor_id IS NOT NULL
                GROUP BY 1,2""", f_desde, f_hasta)]
        partes = [(r["supervisor_id"], r["fecha"], r["n"], r["desc"], r["rest"])
                  for r in await con.fetch(
            """SELECT supervisor_id, fecha, count(*) AS n,
                      bool_or(COALESCE(descripcion,'') <> ''
                              OR jsonb_array_length(COALESCE(anotaciones,'[]'::jsonb)) > 0) AS desc,
                      COALESCE(SUM(jsonb_array_length(COALESCE(restricciones,'[]'::jsonb))),0) AS rest
                 FROM campo_reportes
                WHERE proyecto_id = $3 AND fecha BETWEEN $1 AND $2
                  AND supervisor_id IS NOT NULL
                GROUP BY 1,2""", f_desde, f_hasta, proyecto_id)]
        fotos = [(r["supervisor_id"], r["fecha"], r["n"]) for r in await con.fetch(
            """SELECT r.supervisor_id, r.fecha, count(*) AS n
                 FROM campo_fotos f JOIN campo_reportes r ON r.id = f.reporte_id
                WHERE r.proyecto_id = $3 AND r.fecha BETWEEN $1 AND $2
                  AND r.supervisor_id IS NOT NULL
                GROUP BY 1,2""", f_desde, f_hasta, proyecto_id)]
        # «No se hizo»: la actividad se cuenta en su F.INICIO, el día en que
        # debía arrancar — el mismo criterio con el que /ppc juzga las
        # actividades sin metrado.
        nc = [(r["supervisor_id"], r["fecha"], r["n"]) for r in await con.fetch(
            """SELECT supervisor_id, fecha, count(*) AS n
                 FROM prog_actividades
                WHERE proyecto_id = $3 AND estado = 'NO_CUMPLIDA'
                  AND fecha BETWEEN $1 AND $2 AND supervisor_id IS NOT NULL
                GROUP BY 1,2""", f_desde, f_hasta, proyecto_id)]

    return {"desde": str(f_desde), "hasta": str(f_hasta),
            "fechas": [str(f) for f in fechas], "semanas": _semanas_de(fechas),
            "filas": pivotar_reportes(fechas, sups, hh, partes, fotos, nc)}


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
