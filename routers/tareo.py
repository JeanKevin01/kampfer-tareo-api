# ============================================================
# routers/tareo.py — flujo de campo (Session-First):
# sesiones, cuadrillas (simples + grupos), partidas por OTM,
# enviar-con-partidas (fuente única tareo_partida), cambio de
# partida y registros del día.
# ============================================================
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from core.auth import exigir_identidad_supervisor, require_role
from core.cuadrillas import marcar_habitual
from core.db import db as core_db
from core.ids import ids_unicos, norm_trab_id
from core.log import get_logger
from core.tiempo import fecha_lima, parse_fecha
from routers.ev.hoja import lineas_protegidas
from routers.jornada import resolver_jornada
from routers.valor_ganado import _fecha_base

log = get_logger("api")

router = APIRouter(tags=["tareo"])


# ── SESIONES (Session-First model) ───────────────────────────
@router.get("/api/sesion/hoy/{supervisor_id}")
async def sesiones_hoy(supervisor_id: str):
    pool = await core_db()
    sesiones = await pool.fetch(
        "SELECT s.id, s.supervisor_id, s.otm_id, s.estado, "
        "       s.hh_turno, s.created_at, "
        "       COUNT(st.id) AS total, "
        "       COUNT(st.id) FILTER (WHERE st.presente) AS presentes "
        "FROM sesiones s "
        "LEFT JOIN sesion_trabajadores st ON st.sesion_id = s.id "
        "WHERE s.supervisor_id = $1 AND s.fecha = $2 "
        "GROUP BY s.id ORDER BY s.created_at DESC",
        supervisor_id, fecha_lima(),
    )
    result = []
    for s in sesiones:
        d = dict(s)
        trabs = await pool.fetch(
            "SELECT st.trab_id, st.presente, st.hh_override, st.agregado_via, "
            "       t.nombre, t.cargo "
            "FROM sesion_trabajadores st "
            "JOIN trabajadores t ON t.id = st.trab_id "
            "WHERE st.sesion_id = $1 ORDER BY t.nombre",
            d["id"],
        )
        d["trabajadores"] = [dict(t) for t in trabs]
        result.append(d)
    return result


# ── PARTIDAS POR OTM (para la app móvil) ─────────────────────
@router.get("/api/partidas-otm/{otm_id}")
async def get_partidas_otm(otm_id: str):
    """Devuelve el árbol completo (nodos padre + hojas) de una OTM, para que
    la app muestre la jerarquía con color, pero solo las hojas (fase != null)
    son seleccionables para registrar tareo."""
    pool = await core_db()
    rows = await pool.fetch(
        """SELECT p.id, p.codigo, p.descripcion, p.fase, p.sub_fase,
                  p.unidad, p.hh_presup, p.metrado_presup,
                  p.nivel, p.parent_codigo,
                  (p.fase IS NOT NULL) AS es_hoja
           FROM ev_partidas p
           WHERE p.otm_id = $1 AND p.activo = true
           ORDER BY p.codigo""",
        otm_id,
    )
    return [dict(r) for r in rows]


@router.get("/api/partidas-otm/{otm_id}/hojas")
async def get_partidas_otm_hojas(otm_id: str):
    """Solo las partidas hoja — compat con clientes que no necesitan el árbol."""
    pool = await core_db()
    rows = await pool.fetch(
        """SELECT p.id, p.codigo, p.descripcion, p.fase, p.sub_fase,
                  p.unidad, p.hh_presup, p.metrado_presup
           FROM ev_partidas p
           WHERE p.otm_id = $1 AND p.activo = true AND p.fase IS NOT NULL
           ORDER BY p.codigo""",
        otm_id,
    )
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# CUADRILLAS DEL SUPERVISOR
#
# Fuente única desde 0046: `cuadrilla_grupos` + `cuadrilla_grupo_miembros`.
# Un supervisor tiene VARIAS cuadrillas nombradas y le sirven en cualquier
# proyecto. Es la misma tabla que lee la app de campo por
# `GET /ev/cuadrillas-plantilla`: lo que se guarda aquí aparece en el teléfono.
#
# Antes había tres modelos y cada mitad escribía en el suyo — el panel en
# `cuadrillas`, el campo en `cuadrilla_otm`, y estos endpoints sobre unas tablas
# que no leía nadie. Por eso crear una cuadrilla en oficina no llegaba al tareo.
# ═══════════════════════════════════════════════════════════════
MAX_CUADRILLAS = 30    # por supervisor: la pantalla del teléfono es una lista
MAX_MIEMBROS   = 100   # por cuadrilla


def _nombre_cuadrilla(valor) -> str:
    """Nombre validado. Puro: se testea sin BD."""
    n = " ".join(str(valor or "").split())[:100]
    if not n:
        raise HTTPException(422, "La cuadrilla necesita un nombre")
    return n


async def _grupo_compat_supervisor(con, supervisor_id: str) -> int:
    """Id de «la cuadrilla de fulano», creándola si hace falta.

    Sostiene los tres endpoints `/api/cuadrilla/{sup}` de abajo, que son de
    antes de que existieran las cuadrillas nombradas y manejan UNA sola lista.
    El nombre lleva el supervisor porque desde 0048 es único en toda la empresa:
    una «Cuadrilla de siempre» a secas se la quedaría el primero que la creara.

    Nada que ver con las cuadrillas HABITUALES de 0049 (`cuadrilla_habituales`):
    esto es la lista única de un supervisor en el admin viejo; aquello es qué
    cuadrillas del catálogo usa normalmente cada uno.
    """
    return await _crear_o_reactivar(
        con, supervisor_id, f"Cuadrilla de {supervisor_id}")


# ── Compat: la lista plana de un supervisor (admin.html) ──────
# F0.3: path y shape intactos. Lo que cambia debajo es que ya no existe «la
# cuadrilla de X»: devuelve la gente de la lista que lleva su nombre.
@router.get("/api/cuadrilla/{supervisor_id}")
async def get_cuadrilla(supervisor_id: str):
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT m.trab_id, t.nombre, t.cargo "
        "FROM cuadrilla_grupos g "
        "JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id "
        "JOIN trabajadores t ON t.id = m.trab_id "
        "WHERE lower(g.nombre) = lower($1) AND g.activo = true AND t.activo = true "
        "GROUP BY m.trab_id, t.nombre, t.cargo "
        "ORDER BY t.nombre",
        f"Cuadrilla de {supervisor_id}",
    )
    return [dict(r) for r in rows]


@router.post("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def agregar_cuadrilla(supervisor_id: str, trab_id: str,
                            user: dict = Depends(require_role())):
    exigir_identidad_supervisor(user, supervisor_id)
    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            gid = await _grupo_compat_supervisor(con, supervisor_id)
            await con.execute(
                "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden) "
                "SELECT $1, $2, COALESCE(MAX(orden) + 1, 0) "
                "FROM cuadrilla_grupo_miembros WHERE grupo_id = $1 "
                "ON CONFLICT DO NOTHING",
                gid, norm_trab_id(trab_id),
            )
    return {"ok": True}


@router.delete("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def quitar_cuadrilla(supervisor_id: str, trab_id: str,
                           user: dict = Depends(require_role())):
    exigir_identidad_supervisor(user, supervisor_id)
    pool = await core_db()
    await pool.execute(
        "DELETE FROM cuadrilla_grupo_miembros m "
        "USING cuadrilla_grupos g "
        "WHERE m.grupo_id = g.id AND lower(g.nombre) = lower($1) AND m.trab_id = $2",
        f"Cuadrilla de {supervisor_id}", norm_trab_id(trab_id),
    )
    return {"ok": True}


# ── Cuadrillas: listas de gente que usa CUALQUIER supervisor ──
# `creada_por` dice quién la armó, no de quién es (0048): son libres. Solo sirve
# para poner las tuyas primero en el teléfono.
#
# `en_otras` cuenta en cuántas cuadrillas activas MÁS está esa persona. Con
# listas compartidas eso es normal —la misma persona sale en varias— pero sigue
# valiendo como aviso: si dos supervisores la tarean el mismo día son HH
# duplicadas, y eso hoy solo se descubre después en /ev/conflictos.
#
# `habitual` (0049) es cuáles usa NORMALMENTE ese supervisor. Es un atajo de
# pantalla, no un permiso: la lista completa se ve igual, las habituales solo
# van arriba y separadas. Ordenar por `creada_por` era un apaño — quien armó la
# lista no es necesariamente quien la usa, y un supervisor que entra hoy no ha
# armado ninguna.
_ES_HABITUAL = ("EXISTS (SELECT 1 FROM cuadrilla_habituales h"
                "         WHERE h.grupo_id = g.id AND h.supervisor_id = $1)")

_CUADRILLAS_SQL = """
    SELECT g.id, g.nombre, g.activo, g.creado_en,
           g.creada_por, s.nombre AS creada_por_nombre,
           {habitual} AS habitual,
           m.trab_id, m.orden, t.nombre AS trab_nombre, t.cargo,
           (SELECT COUNT(*) FROM cuadrilla_grupo_miembros m2
              JOIN cuadrilla_grupos g2 ON g2.id = m2.grupo_id AND g2.activo
             WHERE m2.trab_id = m.trab_id) AS en_cuantas
      FROM cuadrilla_grupos g
      LEFT JOIN supervisores s ON s.id = g.creada_por
      LEFT JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id
      LEFT JOIN trabajadores t ON t.id = m.trab_id AND t.activo = true
     WHERE g.activo = true
     ORDER BY {orden}, m.orden, t.nombre
"""


def _ensamblar_cuadrillas(rows) -> list:
    """Filas planas → cuadrillas con sus miembros. Pura: se testea sin BD."""
    grupos: dict = {}
    for r in rows:
        g = grupos.setdefault(r["id"], {
            "id": r["id"], "nombre": r["nombre"], "activo": r["activo"],
            "creado_en": r["creado_en"],
            "creada_por": r.get("creada_por"),
            "creada_por_nombre": r.get("creada_por_nombre"),
            "habitual": bool(r.get("habitual")),
            "miembros": [],
        })
        # trab_nombre NULL = la fila del LEFT JOIN de un grupo vacío, o un
        # miembro dado de baja en el padrón (ya no puede tarear).
        if r["trab_id"] and r["trab_nombre"]:
            g["miembros"].append({
                "trab_id": r["trab_id"], "nombre": r["trab_nombre"],
                "cargo": r["cargo"], "orden": r["orden"],
                "en_otras": max(0, (r.get("en_cuantas") or 1) - 1),
            })
    salida = list(grupos.values())
    for g in salida:
        g["total"] = len(g["miembros"])
    return salida


@router.get("/api/cuadrillas")
async def catalogo_cuadrillas():
    """El catálogo entero, por orden alfabético.

    Sin supervisor en el path no hay a quién medirle lo habitual: `habitual`
    sale false para todas. Quién tiene cuáles se pide en bloque por
    `/api/cuadrillas-habituales`."""
    pool = await core_db()
    rows = await pool.fetch(
        _CUADRILLAS_SQL.format(habitual="FALSE", orden="lower(g.nombre)"))
    return _ensamblar_cuadrillas(rows)


@router.get("/api/cuadrillas/{supervisor_id}")
async def listar_cuadrilla_grupos(supervisor_id: str):
    """Las mismas cuadrillas que ve ese supervisor en su teléfono: TODAS, con
    sus habituales arriba. Path y shape intactos (`habitual` es aditivo)."""
    pool = await core_db()
    rows = await pool.fetch(
        _CUADRILLAS_SQL.format(
            habitual=_ES_HABITUAL,
            orden=f"(NOT {_ES_HABITUAL}), lower(g.nombre)"),
        supervisor_id)
    return _ensamblar_cuadrillas(rows)


# ── Quién usa habitualmente qué ───────────────────────────────
@router.get("/api/cuadrillas-habituales")
async def cuadrillas_habituales(user: dict = Depends(require_role("oficina"))):
    """La asignación entera, un renglón por supervisor activo.

    En bloque y no por supervisor porque la pantalla de oficina las pinta todas
    a la vez, y así una sola consulta sirve para el sentido contrario («de quién
    es habitual esta cuadrilla») sin pedir nada más."""
    pool = await core_db()
    rows = await pool.fetch(
        # Se agrega `g.id` y no `h.grupo_id`: el LEFT JOIN filtra por activo
        # dejando g en NULL, pero h.grupo_id sigue lleno. Agregando por h una
        # cuadrilla BORRADA seguiría saliendo marcada en la pantalla de oficina,
        # con un nombre que ya no existe.
        """SELECT s.id, s.nombre,
                  COALESCE(ARRAY_AGG(g.id ORDER BY g.id)
                           FILTER (WHERE g.id IS NOT NULL),
                           '{}')::int[] AS grupos
             FROM supervisores s
             LEFT JOIN cuadrilla_habituales h ON h.supervisor_id = s.id
             LEFT JOIN cuadrilla_grupos g ON g.id = h.grupo_id AND g.activo
            WHERE s.activo = true
            GROUP BY s.id, s.nombre
            ORDER BY s.nombre""")
    return [{"supervisor_id": r["id"], "nombre": r["nombre"],
             "grupos": list(r["grupos"])} for r in rows]


@router.put("/api/supervisor/{supervisor_id}/cuadrillas-habituales")
async def fijar_cuadrillas_habituales(supervisor_id: str, data: dict,
                                      user: dict = Depends(require_role())):
    """Deja a ese supervisor exactamente con esas habituales.

    Reemplazo y no alta/baja suelta porque la pantalla es una lista de casillas:
    manda el estado final y no hay que reconstruir qué se marcó y qué se
    desmarcó. Marcar una cuadrilla no da ningún permiso — todos las ven todas."""
    exigir_identidad_supervisor(user, supervisor_id)
    grupos = data.get("grupos")
    if not isinstance(grupos, list):
        raise HTTPException(422, "Falta la lista de cuadrillas («grupos»)")
    ids = sorted({int(g) for g in grupos if str(g).strip().lstrip("-").isdigit()})

    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            if not await con.fetchval(
                    "SELECT 1 FROM supervisores WHERE id = $1", supervisor_id):
                raise HTTPException(404, "Ese supervisor no existe")
            # Solo cuadrillas vivas: marcar una borrada dejaría una habitual
            # invisible que reaparece si alguien recrea ese nombre.
            vivas = [r["id"] for r in await con.fetch(
                "SELECT id FROM cuadrilla_grupos WHERE id = ANY($1::int[]) AND activo",
                ids)]
            await con.execute(
                "DELETE FROM cuadrilla_habituales WHERE supervisor_id = $1",
                supervisor_id)
            for gid in vivas:
                await con.execute(
                    "INSERT INTO cuadrilla_habituales (supervisor_id, grupo_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    supervisor_id, gid)
    return {"ok": True, "supervisor_id": supervisor_id, "grupos": vivas}


@router.get("/api/trabajadores-sin-cuadrilla")
async def trabajadores_sin_cuadrilla():
    """Personal activo que no está en NINGUNA cuadrilla.

    Es el reverso del aviso de solapamiento: a estos nadie los va a encontrar en
    una lista guardada, así que o se tarean a mano cada día o —lo que de verdad
    pasa— se quedan sin tarear."""
    pool = await core_db()
    rows = await pool.fetch(
        """SELECT t.id, t.nombre, t.cargo, COALESCE(t.tipo,'DIRECTO') AS tipo
           FROM trabajadores t
          WHERE t.activo = true
            AND NOT EXISTS (
                SELECT 1 FROM cuadrilla_grupo_miembros m
                  JOIN cuadrilla_grupos g ON g.id = m.grupo_id AND g.activo
                 WHERE m.trab_id = t.id)
          ORDER BY t.nombre""")
    return [dict(r) for r in rows]


@router.post("/api/cuadrillas")
async def crear_cuadrilla_catalogo(data: dict,
                                   user: dict = Depends(require_role("oficina"))):
    """Crea una cuadrilla desde oficina. Queda disponible para todos."""
    return await _crear_cuadrilla(None, data)


@router.post("/api/cuadrillas/{supervisor_id}")
async def crear_cuadrilla_grupo(supervisor_id: str, data: dict,
                                user: dict = Depends(require_role())):
    """Crea una cuadrilla anotando quién la armó (para ordenarle su pantalla).

    Path y shape intactos: `supervisor_id` era el dueño y ahora es solo el
    autor — la cuadrilla la puede usar cualquiera."""
    exigir_identidad_supervisor(user, supervisor_id)
    return await _crear_cuadrilla(supervisor_id, data)


async def _crear_cuadrilla(creada_por, data: dict) -> dict:
    nombre   = _nombre_cuadrilla(data.get("nombre"))
    trab_ids = ids_unicos(data.get("trab_ids", []))
    if len(trab_ids) > MAX_MIEMBROS:
        raise HTTPException(422, f"Máximo {MAX_MIEMBROS} personas por cuadrilla")

    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            await _verificar_cupo(con)
            # Reactivar una borrada con ese nombre es lo esperable; machacar una
            # que está en uso, no.
            existente = await con.fetchrow(
                "SELECT id, activo FROM cuadrilla_grupos WHERE lower(nombre) = lower($1)",
                nombre)
            if existente and existente["activo"]:
                raise HTTPException(409, _ya_existe(nombre))
            grupo_id = await _crear_o_reactivar(con, creada_por, nombre)
            await _reemplazar_miembros(con, grupo_id, trab_ids)
            await marcar_habitual(con, creada_por, grupo_id)
    return {"ok": True, "id": grupo_id, "nombre": nombre,
            "creada_por": creada_por, "total": len(trab_ids)}


def _ya_existe(nombre: str) -> str:
    """El nombre es único en toda la empresa desde 0048: las cuadrillas se ven
    todas juntas y dos «Encofrado» son indistinguibles al elegir."""
    return f"Ya existe una cuadrilla «{nombre}»"


async def _verificar_cupo(con) -> None:
    vivas = await con.fetchval(
        "SELECT COUNT(*) FROM cuadrilla_grupos WHERE activo = true")
    if vivas >= MAX_CUADRILLAS:
        raise HTTPException(
            422, f"Máximo {MAX_CUADRILLAS} cuadrillas; borra alguna que no uses")


async def _crear_o_reactivar(con, creada_por, nombre: str) -> int:
    """Una borrada con ese nombre se revive en vez de duplicarse: el índice
    único no distingue activas de inactivas, así que el nombre sigue ocupado."""
    gid = await con.fetchval(
        "UPDATE cuadrilla_grupos SET activo = true, nombre = $1 "
        " WHERE lower(nombre) = lower($1) RETURNING id", nombre)
    if gid:
        return gid
    return await con.fetchval(
        "INSERT INTO cuadrilla_grupos (creada_por, nombre) VALUES ($1, $2) "
        "RETURNING id", creada_por, nombre)


@router.post("/api/cuadrilla-grupo/{grupo_id}/duplicar")
async def duplicar_cuadrilla_grupo(grupo_id: int, data: dict,
                                   user: dict = Depends(require_role())):
    """Copia la cuadrilla con toda su gente bajo otro nombre.

    Sirve para las variantes de siempre —«Encofrado» y «Encofrado sin los dos
    que están de descanso»— sin volver a armarla. Copia la FOTO de hoy: después
    las dos viven por su cuenta y editar una no toca a la otra."""
    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            origen = await con.fetchrow(
                "SELECT id, nombre FROM cuadrilla_grupos WHERE id = $1", grupo_id)
            if not origen:
                raise HTTPException(404, "Esa cuadrilla no existe")
            await _verificar_cupo(con)
            nombre = _nombre_cuadrilla(data.get("nombre") or f"{origen['nombre']} (copia)")
            existente = await con.fetchval(
                "SELECT id FROM cuadrilla_grupos "
                " WHERE lower(nombre) = lower($1) AND activo = true", nombre)
            if existente:
                raise HTTPException(409, _ya_existe(nombre))
            nuevo = await _crear_o_reactivar(con, data.get("creada_por"), nombre)
            await con.execute(
                "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden) "
                "SELECT $1, trab_id, orden FROM cuadrilla_grupo_miembros "
                " WHERE grupo_id = $2 ON CONFLICT DO NOTHING",
                nuevo, grupo_id)
            total = await con.fetchval(
                "SELECT COUNT(*) FROM cuadrilla_grupo_miembros WHERE grupo_id = $1",
                nuevo)
    return {"ok": True, "id": nuevo, "nombre": nombre, "total": total}


async def _reemplazar_miembros(con, grupo_id: int, trab_ids: list) -> None:
    """Deja el grupo exactamente con esa lista, en ese orden."""
    await con.execute(
        "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = $1", grupo_id)
    for idx, tid in enumerate(trab_ids):
        await con.execute(
            "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            grupo_id, tid, idx,
        )


@router.patch("/api/cuadrilla-grupo/{grupo_id}")
async def editar_cuadrilla_grupo(grupo_id: int, data: dict,
                                 user: dict = Depends(require_role())):
    """Renombra la cuadrilla.

    Ya no hay a quién reasignársela: desde 0048 las cuadrillas son de todos. Y
    por lo mismo cualquier supervisor puede editar cualquiera — antes se exigía
    ser el dueño, y ese dueño ya no existe."""
    # Sin BD, primero: un nombre inválido no merece una conexión (y así el 422
    # no depende de que la haya).
    nombre = _nombre_cuadrilla(data.get("nombre"))
    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            actual = await con.fetchval(
                "SELECT nombre FROM cuadrilla_grupos WHERE id = $1", grupo_id)
            if actual is None:
                raise HTTPException(404, "Esa cuadrilla no existe")
            choque = await con.fetchval(
                "SELECT id FROM cuadrilla_grupos "
                " WHERE lower(nombre) = lower($1) AND activo = true AND id <> $2",
                nombre, grupo_id)
            if choque:
                raise HTTPException(409, _ya_existe(nombre))
            await con.execute(
                "UPDATE cuadrilla_grupos SET nombre = $2 WHERE id = $1",
                grupo_id, nombre)
    return {"ok": True, "id": grupo_id, "nombre": nombre}


@router.put("/api/cuadrilla-grupo/{grupo_id}/miembros")
async def reemplazar_miembros_grupo(grupo_id: int, data: dict,
                                    user: dict = Depends(require_role())):
    """Reemplaza la lista completa de miembros del grupo."""
    trab_ids = ids_unicos(data.get("trab_ids", []))
    if len(trab_ids) > MAX_MIEMBROS:
        raise HTTPException(422, f"Máximo {MAX_MIEMBROS} personas por cuadrilla")
    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            await _reemplazar_miembros(con, grupo_id, trab_ids)
    return {"ok": True, "total": len(trab_ids)}


@router.post("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def agregar_miembro_grupo(grupo_id: int, trab_id: str,
                                user: dict = Depends(require_role())):
    pool = await core_db()
    await pool.execute(
        "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden) "
        "SELECT $1, $2, COALESCE(MAX(orden) + 1, 0) "
        "FROM cuadrilla_grupo_miembros WHERE grupo_id = $1 "
        "ON CONFLICT DO NOTHING",
        grupo_id, norm_trab_id(trab_id),
    )
    return {"ok": True}


@router.delete("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def quitar_miembro_grupo(grupo_id: int, trab_id: str,
                               user: dict = Depends(require_role())):
    pool = await core_db()
    await pool.execute(
        "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = $1 AND trab_id = $2",
        grupo_id, norm_trab_id(trab_id),
    )
    return {"ok": True}


@router.delete("/api/cuadrilla-grupo/{grupo_id}")
async def eliminar_cuadrilla_grupo(grupo_id: int,
                                   user: dict = Depends(require_role())):
    """Baja lógica: la cuadrilla desaparece de las listas y los partes ya
    enviados no dependen de esta tabla. Crear otra con el mismo nombre la
    revive con su gente en vez de duplicarla."""
    pool = await core_db()
    await pool.execute(
        "UPDATE cuadrilla_grupos SET activo = false WHERE id = $1", grupo_id)
    return {"ok": True}


# ── REGISTROS DEL DÍA ─────────────────────────────────────────
# F0.3: estos dos endpoints CONSERVAN su path y shape (los usan 7 páginas del panel)
# pero leen de tareo_partida (fuente única) en vez de la tabla legacy `registros`.
# Una fila por (trabajador, OTM, supervisor) con la suma de HH del día — mismo shape
# que producía la tabla legacy.
_REGISTROS_DIA_SQL = """
    SELECT tp.trabajador_id AS trab_id, t.nombre, t.cargo, tp.otm_id,
           to_char(MIN(tp.hora_registro) AT TIME ZONE 'America/Lima', 'HH24:MI:SS') AS hora,
           tp.supervisor_id, SUM(tp.hh) AS hh
    FROM tareo_partida tp
    JOIN trabajadores t ON t.id = tp.trabajador_id
    WHERE tp.fecha = $1 AND tp.hh IS NOT NULL
    GROUP BY tp.trabajador_id, t.nombre, t.cargo, tp.otm_id, tp.supervisor_id
    ORDER BY trab_id, hora
"""


@router.get("/api/registros/hoy")
async def registros_hoy():
    pool = await core_db()
    rows = await pool.fetch(_REGISTROS_DIA_SQL, fecha_lima())
    return [dict(r) for r in rows]


@router.get("/api/registros/{fecha}")
async def registros_por_fecha(fecha: str):
    f = parse_fecha(fecha)
    if not f:
        raise HTTPException(400, "fecha inválida")
    pool = await core_db()
    rows = await pool.fetch(_REGISTROS_DIA_SQL, f)
    return [dict(r) for r in rows]


# ── ENVIAR CON PARTIDAS (nuevo flujo) ─────────────────────────
async def _semana_para(pool, fecha_obj: date) -> int:
    """Semana del proyecto para una fecha. Usa la MISMA fecha_base del motor EV
    (auto-derivada del primer tareo y persistida — F0.3), alineada a lunes."""
    async with pool.acquire() as con:
        base = await _fecha_base(con)
    if not base:
        return 1
    return max(1, (fecha_obj - base).days // 7 + 1)


@router.post("/api/sesion/enviar-con-partidas")
async def enviar_con_partidas(data: dict, user: dict = Depends(require_role())):
    """
    Flujo nuevo: cada trabajador tiene 1+ asignaciones a partidas con HH.
    Soporta ambos formatos:
      - Nuevo: {trab_id, asignaciones:[{partida_id, hh}], via}
      - Viejo: {trab_id, partida_id, via}  (compat)
    """
    supervisor_id = str(data.get("supervisor_id", "")).strip()
    otm_id        = str(data.get("otm_id", "")).strip()
    fecha_str     = str(data.get("fecha", fecha_lima().isoformat()))
    trabajadores  = data.get("trabajadores", [])

    if not supervisor_id or not otm_id:
        raise HTTPException(400, "supervisor_id y otm_id son requeridos")
    exigir_identidad_supervisor(user, supervisor_id)   # F0.6: anti-suplantación
    if not trabajadores:
        raise HTTPException(400, "Lista de trabajadores vacía")

    fecha_obj = parse_fecha(fecha_str)
    if not fecha_obj:
        raise HTTPException(400, "fecha inválida")
    hh_dia = await resolver_jornada(fecha_obj, otm_id)

    pool = await core_db()
    semana = await _semana_para(pool, fecha_obj)

    enviados = 0
    fallidos = 0
    omitidos = 0
    respetados = 0   # líneas ya corregidas en oficina que el reenvío no toca

    try:
        # Todo el registro va en UNA transacción: o entra completo o no entra nada
        # (evita estados a medias y condiciones de carrera en el reenvío).
        async with pool.acquire() as con:
            async with con.transaction():
                # La corrección de oficina gana sobre todo (decisión de Jean,
                # 2026-08-02): las líneas ya corregidas ni se borran ni se
                # reescriben con lo que trae la app. Sin esto, corregir el lunes
                # desde el panel y que el supervisor reenviara ese día deshacía
                # la corrección en silencio.
                protegidas = await lineas_protegidas(con, supervisor_id, otm_id, fecha_obj)

                # Idempotencia: un reenvío del mismo supervisor/OTM/día REEMPLAZA el anterior.
                await con.execute(
                    "DELETE FROM tareo_partida WHERE supervisor_id=$1 AND otm_id=$2 AND fecha=$3 "
                    "AND editado_por IS NULL",
                    supervisor_id, otm_id, fecha_obj,
                )

                sesion_id = await con.fetchval(
                    "INSERT INTO sesiones "
                    "(supervisor_id, otm_id, fecha, hh_turno, estado, enviada_at) "
                    "VALUES ($1, $2, $3, $4, 'enviada', now()) RETURNING id",
                    supervisor_id, otm_id, fecha_obj, hh_dia,
                )

                for t in trabajadores:
                    trab_id = str(t.get("trab_id", "")).zfill(3)
                    via     = t.get("via", "app")

                    # Normalizar asignaciones — soportar ambos formatos
                    asignaciones = t.get("asignaciones")
                    if asignaciones is None:
                        pid_old = t.get("partida_id")
                        asignaciones = [{"partida_id": pid_old, "hh": hh_dia}] if pid_old else []

                    await con.execute(
                        "INSERT INTO sesion_trabajadores "
                        "(sesion_id, trab_id, presente, hh_override, agregado_via) "
                        "VALUES ($1, $2, true, null, $3)",
                        sesion_id, trab_id, via,
                    )

                    # F0.3: se retiró la doble escritura a `registros` (tabla congelada como
                    # histórico). tareo_partida es la única fuente de HH del tareo.

                    # tareo_partida — una fila por cada asignación a partida.
                    # Savepoint por fila (transacción anidada): una asignación inválida no
                    # aborta el envío completo, solo se cuenta en `fallidos`.
                    for asig in asignaciones:
                        pid = asig.get("partida_id")
                        try:
                            hh = float(asig.get("hh") or hh_dia)
                        except (TypeError, ValueError):
                            hh = 0.0
                        if (trab_id, pid) in protegidas:
                            # Oficina ya fijó esta línea: se deja como está y se
                            # informa, para que la app pueda decírselo al
                            # supervisor en vez de que el cambio se pierda mudo.
                            respetados += 1
                            continue
                        if not pid or hh <= 0:
                            # Antes se descartaba en SILENCIO: la app decía
                            # "enviado" y la partida se quedaba con 0 HH en el
                            # ISP y en el sustento. Ahora se cuenta y se avisa.
                            omitidos += 1
                            log.warning(
                                f"[tareo_partida] omitida asignación trab={trab_id} "
                                f"pid={pid} hh={asig.get('hh')}")
                            continue
                        try:
                            async with con.transaction():
                                await con.execute(
                                    "INSERT INTO tareo_partida "
                                    "(trabajador_id, partida_id, otm_id, fecha, semana, "
                                    " hora_registro, hh, supervisor_id, sesion_id, fuente) "
                                    "VALUES ($1, $2, $3, $4, $5, NOW(), $6, $7, $8, 'tareo')",
                                    trab_id, pid, otm_id, fecha_obj, semana, hh,
                                    supervisor_id, sesion_id,
                                )
                        except Exception as e:
                            fallidos += 1
                            log.warning(f"[tareo_partida] trab={trab_id} pid={pid}: {e}")

                    enviados += 1
    except HTTPException:
        raise
    except Exception as e:
        # Cualquier error de BD se devuelve como 500 CON cabeceras CORS y mensaje legible
        # (así el front muestra el error real en vez de un opaco "Failed to fetch").
        raise HTTPException(500, f"Error al registrar tareo: {e}")

    return {"ok": True, "enviados": enviados, "tareo_fallidos": fallidos,
            "tareo_omitidos": omitidos, "tareo_respetados": respetados,
            "sesion_id": sesion_id, "hh_dia": hh_dia}


