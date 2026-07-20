# ============================================================
# routers/tareo.py — flujo de campo (Session-First):
# sesiones, cuadrillas (simples + grupos), partidas por OTM,
# enviar-con-partidas (fuente única tareo_partida), cambio de
# partida y registros del día.
# ============================================================
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from core.auth import exigir_identidad_supervisor, require_role
from core.db import db as core_db
from core.log import get_logger
from core.tiempo import fecha_lima, parse_fecha
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


# ── CUADRILLA SIMPLE (una por supervisor) ────────────────────
@router.get("/api/cuadrilla/{supervisor_id}")
async def get_cuadrilla(supervisor_id: str):
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT c.trab_id, t.nombre, t.cargo "
        "FROM cuadrillas c "
        "JOIN trabajadores t ON t.id = c.trab_id "
        "WHERE c.supervisor_id = $1 AND t.activo = true "
        "ORDER BY t.nombre",
        supervisor_id,
    )
    return [dict(r) for r in rows]


@router.post("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def agregar_cuadrilla(supervisor_id: str, trab_id: str,
                            user: dict = Depends(require_role())):
    exigir_identidad_supervisor(user, supervisor_id)
    pool = await core_db()
    await pool.execute(
        "INSERT INTO cuadrillas (supervisor_id, trab_id) "
        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
        supervisor_id, trab_id.zfill(3),
    )
    return {"ok": True}


@router.delete("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def quitar_cuadrilla(supervisor_id: str, trab_id: str,
                           user: dict = Depends(require_role())):
    exigir_identidad_supervisor(user, supervisor_id)
    pool = await core_db()
    await pool.execute(
        "DELETE FROM cuadrillas WHERE supervisor_id = $1 AND trab_id = $2",
        supervisor_id, trab_id.zfill(3),
    )
    return {"ok": True}


# ── CUADRILLA GRUPOS (múltiples por supervisor) ───────────────
@router.get("/api/cuadrillas/{supervisor_id}")
async def listar_cuadrilla_grupos(supervisor_id: str):
    """Lista todos los grupos de cuadrilla del supervisor con sus miembros."""
    pool = await core_db()
    grupos = await pool.fetch(
        """SELECT g.id, g.nombre, g.activo, g.creado_en,
                  COUNT(m.trab_id) AS total
           FROM cuadrilla_grupos g
           LEFT JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id
           WHERE g.supervisor_id = $1 AND g.activo = true
           GROUP BY g.id ORDER BY g.creado_en""",
        supervisor_id,
    )
    result = []
    for g in grupos:
        gd = dict(g)
        miembros = await pool.fetch(
            """SELECT m.trab_id, t.nombre, t.cargo
               FROM cuadrilla_grupo_miembros m
               JOIN trabajadores t ON t.id = m.trab_id AND t.activo = true
               WHERE m.grupo_id = $1 ORDER BY t.nombre""",
            gd["id"],
        )
        gd["miembros"] = [dict(m) for m in miembros]
        gd["total"]    = int(gd["total"])
        result.append(gd)
    return result


@router.post("/api/cuadrillas/{supervisor_id}")
async def crear_cuadrilla_grupo(supervisor_id: str, data: dict,
                                user: dict = Depends(require_role())):
    """Crea un nuevo grupo de cuadrilla con su lista de miembros."""
    exigir_identidad_supervisor(user, supervisor_id)
    nombre   = data.get("nombre", "").strip()
    trab_ids = data.get("trab_ids", [])
    if not nombre:
        raise HTTPException(400, "El nombre es requerido")
    pool = await core_db()
    grupo_id = await pool.fetchval(
        "INSERT INTO cuadrilla_grupos (supervisor_id, nombre) "
        "VALUES ($1, $2) ON CONFLICT (supervisor_id, nombre) "
        "DO UPDATE SET activo = true RETURNING id",
        supervisor_id, nombre,
    )
    for tid in trab_ids:
        await pool.execute(
            "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id) "
            "VALUES ($1, $2) ON CONFLICT DO NOTHING",
            grupo_id, str(tid).zfill(3),
        )
    return {"ok": True, "id": grupo_id, "nombre": nombre}


async def _exigir_dueno_grupo(pool, grupo_id: int, user: dict) -> None:
    """F0.6: las escrituras sobre un grupo solo las hace su supervisor dueño
    (o un rol de oficina/admin/servicio)."""
    if user and user.get("rol") == "supervisor":
        dueno = await pool.fetchval(
            "SELECT supervisor_id FROM cuadrilla_grupos WHERE id = $1", grupo_id)
        exigir_identidad_supervisor(user, dueno)


@router.put("/api/cuadrilla-grupo/{grupo_id}/miembros")
async def reemplazar_miembros_grupo(grupo_id: int, data: dict,
                                    user: dict = Depends(require_role())):
    """Reemplaza la lista completa de miembros del grupo."""
    trab_ids = data.get("trab_ids", [])
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = $1", grupo_id)
            for tid in trab_ids:
                await con.execute(
                    "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    grupo_id, str(tid).zfill(3),
                )
    return {"ok": True, "total": len(trab_ids)}


@router.post("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def agregar_miembro_grupo(grupo_id: int, trab_id: str,
                                user: dict = Depends(require_role())):
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
    await pool.execute(
        "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id) "
        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
        grupo_id, trab_id.zfill(3),
    )
    return {"ok": True}


@router.delete("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def quitar_miembro_grupo(grupo_id: int, trab_id: str,
                               user: dict = Depends(require_role())):
    pool = await core_db()
    await _exigir_dueno_grupo(pool, grupo_id, user)
    await pool.execute(
        "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = $1 AND trab_id = $2",
        grupo_id, trab_id.zfill(3),
    )
    return {"ok": True}


@router.delete("/api/cuadrilla-grupo/{grupo_id}")
async def eliminar_cuadrilla_grupo(grupo_id: int,
                                   user: dict = Depends(require_role())):
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

    try:
        # Todo el registro va en UNA transacción: o entra completo o no entra nada
        # (evita estados a medias y condiciones de carrera en el reenvío).
        async with pool.acquire() as con:
            async with con.transaction():
                # Idempotencia: un reenvío del mismo supervisor/OTM/día REEMPLAZA el anterior.
                await con.execute(
                    "DELETE FROM tareo_partida WHERE supervisor_id=$1 AND otm_id=$2 AND fecha=$3",
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
            "tareo_omitidos": omitidos, "sesion_id": sesion_id, "hh_dia": hh_dia}


