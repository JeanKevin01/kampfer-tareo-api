# ============================================================
# routers/tareo.py — flujo de campo (Session-First):
# sesiones, cuadrillas (simples + grupos), partidas por OTM,
# enviar-con-partidas (fuente única tareo_partida), cambio de
# partida y registros del día.
# ============================================================
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from core.auth import exigir_identidad_supervisor, require_role
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


async def _grupo_habitual(con, supervisor_id: str) -> int:
    """Id de la cuadrilla por defecto, creándola si hace falta.

    Sostiene los tres endpoints `/api/cuadrilla/{sup}` de abajo, que no saben de
    cuadrillas nombradas.
    """
    return await con.fetchval(
        "INSERT INTO cuadrilla_grupos (supervisor_id, nombre) VALUES ($1, $2) "
        "ON CONFLICT (supervisor_id, nombre) DO UPDATE SET activo = true "
        "RETURNING id",
        supervisor_id, "Cuadrilla habitual",
    )


# ── Compat: cuadrilla única del supervisor (la usa admin.html) ─
# F0.3: path y shape intactos. Lo que cambia es el sustrato — antes escribían la
# tabla `cuadrillas`, que era un sumidero: nada la leía.
@router.get("/api/cuadrilla/{supervisor_id}")
async def get_cuadrilla(supervisor_id: str):
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT m.trab_id, t.nombre, t.cargo "
        "FROM cuadrilla_grupos g "
        "JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id "
        "JOIN trabajadores t ON t.id = m.trab_id "
        "WHERE g.supervisor_id = $1 AND g.activo = true AND t.activo = true "
        "GROUP BY m.trab_id, t.nombre, t.cargo "
        "ORDER BY t.nombre",
        supervisor_id,
    )
    return [dict(r) for r in rows]


@router.post("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def agregar_cuadrilla(supervisor_id: str, trab_id: str,
                            user: dict = Depends(require_role())):
    exigir_identidad_supervisor(user, supervisor_id)
    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            gid = await _grupo_habitual(con, supervisor_id)
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
        "WHERE m.grupo_id = g.id AND g.supervisor_id = $1 AND m.trab_id = $2",
        supervisor_id, norm_trab_id(trab_id),
    )
    return {"ok": True}


# ── Cuadrillas nombradas (varias por supervisor) ──────────────
@router.get("/api/cuadrillas/{supervisor_id}")
async def listar_cuadrilla_grupos(supervisor_id: str):
    """Todas las cuadrillas del supervisor con sus miembros, en orden."""
    pool = await core_db()
    rows = await pool.fetch(
        """SELECT g.id, g.nombre, g.activo, g.creado_en,
                  m.trab_id, m.orden, t.nombre AS trab_nombre, t.cargo
           FROM cuadrilla_grupos g
           LEFT JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id
           LEFT JOIN trabajadores t ON t.id = m.trab_id AND t.activo = true
           WHERE g.supervisor_id = $1 AND g.activo = true
           ORDER BY g.creado_en, m.orden, t.nombre""",
        supervisor_id,
    )
    grupos: dict = {}
    for r in rows:
        g = grupos.setdefault(r["id"], {
            "id": r["id"], "nombre": r["nombre"], "activo": r["activo"],
            "creado_en": r["creado_en"], "miembros": [],
        })
        # trab_nombre NULL = la fila del LEFT JOIN de un grupo vacío, o un
        # miembro dado de baja en el padrón (ya no puede tarear).
        if r["trab_id"] and r["trab_nombre"]:
            g["miembros"].append({
                "trab_id": r["trab_id"], "nombre": r["trab_nombre"],
                "cargo": r["cargo"], "orden": r["orden"],
            })
    salida = list(grupos.values())
    for g in salida:
        g["total"] = len(g["miembros"])
    return salida


@router.post("/api/cuadrillas/{supervisor_id}")
async def crear_cuadrilla_grupo(supervisor_id: str, data: dict,
                                user: dict = Depends(require_role())):
    """Crea una cuadrilla nombrada con su lista de miembros."""
    exigir_identidad_supervisor(user, supervisor_id)
    nombre   = _nombre_cuadrilla(data.get("nombre"))
    trab_ids = ids_unicos(data.get("trab_ids", []))
    if len(trab_ids) > MAX_MIEMBROS:
        raise HTTPException(422, f"Máximo {MAX_MIEMBROS} personas por cuadrilla")

    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            vivas = await con.fetchval(
                "SELECT COUNT(*) FROM cuadrilla_grupos "
                "WHERE supervisor_id = $1 AND activo = true", supervisor_id)
            if vivas >= MAX_CUADRILLAS:
                raise HTTPException(
                    422, f"Máximo {MAX_CUADRILLAS} cuadrillas por supervisor")
            # Reactivar una borrada con ese nombre es lo esperable; machacar una
            # que está en uso, no.
            existente = await con.fetchrow(
                "SELECT id, activo FROM cuadrilla_grupos "
                "WHERE supervisor_id = $1 AND nombre = $2", supervisor_id, nombre)
            if existente and existente["activo"]:
                raise HTTPException(409, f"Ya existe una cuadrilla «{nombre}»")
            grupo_id = await _crear_o_reactivar(con, supervisor_id, nombre)
            await _reemplazar_miembros(con, grupo_id, trab_ids)
    return {"ok": True, "id": grupo_id, "nombre": nombre, "total": len(trab_ids)}


async def _crear_o_reactivar(con, supervisor_id: str, nombre: str) -> int:
    return await con.fetchval(
        "INSERT INTO cuadrilla_grupos (supervisor_id, nombre) VALUES ($1, $2) "
        "ON CONFLICT (supervisor_id, nombre) DO UPDATE SET activo = true "
        "RETURNING id",
        supervisor_id, nombre,
    )


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


async def _exigir_dueno_grupo(pool, grupo_id: int, user: dict) -> None:
    """F0.6: las escrituras sobre un grupo solo las hace su supervisor dueño
    (o un rol de oficina/admin/servicio)."""
    if user and user.get("rol") == "supervisor":
        dueno = await pool.fetchval(
            "SELECT supervisor_id FROM cuadrilla_grupos WHERE id = $1", grupo_id)
        exigir_identidad_supervisor(user, dueno)


@router.patch("/api/cuadrilla-grupo/{grupo_id}")
async def renombrar_cuadrilla_grupo(grupo_id: int, data: dict,
                                    user: dict = Depends(require_role())):
    """Renombra la cuadrilla. Sin esto, un nombre mal escrito era permanente."""
    nombre = _nombre_cuadrilla(data.get("nombre"))
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
    try:
        actualizado = await pool.fetchval(
            "UPDATE cuadrilla_grupos SET nombre = $2 WHERE id = $1 RETURNING id",
            grupo_id, nombre)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"Ya existe una cuadrilla «{nombre}»")
    if not actualizado:
        raise HTTPException(404, "Esa cuadrilla no existe")
    return {"ok": True, "id": grupo_id, "nombre": nombre}


@router.put("/api/cuadrilla-grupo/{grupo_id}/miembros")
async def reemplazar_miembros_grupo(grupo_id: int, data: dict,
                                    user: dict = Depends(require_role())):
    """Reemplaza la lista completa de miembros del grupo."""
    trab_ids = ids_unicos(data.get("trab_ids", []))
    if len(trab_ids) > MAX_MIEMBROS:
        raise HTTPException(422, f"Máximo {MAX_MIEMBROS} personas por cuadrilla")
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
    async with pool.acquire() as con:
        async with con.transaction():
            await _reemplazar_miembros(con, grupo_id, trab_ids)
    return {"ok": True, "total": len(trab_ids)}


@router.post("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def agregar_miembro_grupo(grupo_id: int, trab_id: str,
                                user: dict = Depends(require_role())):
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
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
    await _exigir_dueno_grupo(pool, grupo_id, user)
    await pool.execute(
        "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = $1 AND trab_id = $2",
        grupo_id, norm_trab_id(trab_id),
    )
    return {"ok": True}


@router.delete("/api/cuadrilla-grupo/{grupo_id}")
async def eliminar_cuadrilla_grupo(grupo_id: int,
                                   user: dict = Depends(require_role())):
    """Baja lógica: el nombre queda libre para reutilizarse y los partes ya
    enviados no dependen de esta tabla."""
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
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


