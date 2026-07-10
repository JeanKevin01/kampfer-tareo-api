# ============================================================
# routers/valorizaciones.py — F2.8: valorización mensual al cliente
#
# POST /ev/valorizaciones           → borrador prellenado (avance EV del mes × PU)
# GET  /ev/valorizaciones           → lista por proyecto
# GET  /ev/valorizaciones/{id}      → detalle con líneas
# PUT  /ev/valorizaciones/{id}/lineas    → editar (solo BORRADOR)
# POST /ev/valorizaciones/{id}/estado    → BORRADOR→PRESENTADA→APROBADA
#   (aprobar exige el periodo ABIERTO; al aprobar, el RO usa esta venta).
# ============================================================
from datetime import timedelta

from fastapi import APIRouter, HTTPException

from core.db import db
from routers.periodos import exigir_abierto

router = APIRouter(prefix="/ev/valorizaciones", tags=["valorizaciones"])

_TRANSICIONES = {"BORRADOR": "PRESENTADA", "PRESENTADA": "APROBADA"}


@router.get("")
async def listar(proyecto_id: int = 1):
    pool = await db()
    rows = await pool.fetch(
        """SELECT v.*, p.anio, p.mes,
                  (SELECT COALESCE(SUM(l.cantidad * l.pu), 0)::float
                   FROM valorizacion_lineas l WHERE l.valorizacion_id = v.id) AS total
           FROM valorizaciones v JOIN periodos p ON p.id = v.periodo_id
           WHERE v.proyecto_id = $1 ORDER BY p.anio DESC, p.mes DESC""", proyecto_id)
    return [dict(r) for r in rows]


@router.post("")
async def crear(data: dict):
    """{proyecto_id, periodo_id, nota?} → borrador prellenado con el avance del
    mes (cantidad instalada del EV en ese mes × PU contractual vigente)."""
    proyecto_id = int(data.get("proyecto_id") or 1)
    periodo_id = int(data.get("periodo_id") or 0)
    if not periodo_id:
        raise HTTPException(400, "periodo_id requerido")
    pool = await db()
    async with pool.acquire() as con:
        per = await con.fetchrow("SELECT * FROM periodos WHERE id=$1", periodo_id)
        if not per:
            raise HTTPException(404, "Periodo no encontrado")
        await exigir_abierto(con, periodo_id)
        async with con.transaction():
            try:
                vid = await con.fetchval(
                    """INSERT INTO valorizaciones (proyecto_id, periodo_id, nota)
                       VALUES ($1,$2,$3) RETURNING id""",
                    proyecto_id, periodo_id, data.get("nota"))
            except Exception:
                raise HTTPException(409, "Ya existe una valorización para ese periodo")

            # Prellenado: avance del MES (Δ cantidad_acum del hito principal dentro
            # del rango de semanas que caen en el mes) × PU de la partida.
            from routers.ev._datos import _fecha_base
            base = await _fecha_base(con)
            n = 0
            if base:
                # semanas cuyo lunes cae dentro del mes del periodo
                rows = await con.fetch(
                    """WITH avance AS (
                         SELECT h.partida_id, a.semana, a.cantidad_acum,
                                LAG(a.cantidad_acum) OVER (PARTITION BY a.hito_id ORDER BY a.semana)
                                  AS acum_prev
                         FROM ev_avances a
                         JOIN ev_hitos h ON h.id = a.hito_id AND h.es_principal
                         JOIN ev_partidas ev ON ev.id = h.partida_id
                         WHERE ev.otm_id IN (SELECT id FROM otms WHERE proyecto_id = $1)
                       )
                       SELECT partida_id, semana,
                              (cantidad_acum - COALESCE(acum_prev, 0))::float AS delta
                       FROM avance""", proyecto_id)
                por_partida: dict = {}
                for r in rows:
                    lunes = base + timedelta(days=(int(r["semana"]) - 1) * 7)
                    if lunes.year == per["anio"] and lunes.month == per["mes"]:
                        por_partida[r["partida_id"]] = por_partida.get(r["partida_id"], 0.0) \
                            + float(r["delta"] or 0)
                for pid, cant in por_partida.items():
                    if cant <= 0:
                        continue
                    pu = await con.fetchval(
                        "SELECT precio_unitario FROM ev_partidas WHERE id=$1", pid) or 0
                    await con.execute(
                        """INSERT INTO valorizacion_lineas (valorizacion_id, partida_id, cantidad, pu)
                           VALUES ($1,$2,$3,$4)""", vid, pid, round(cant, 4), pu)
                    n += 1
    return {"ok": True, "id": vid, "lineas_prellenadas": n}


@router.get("/{vid}")
async def detalle(vid: int):
    pool = await db()
    v = await pool.fetchrow(
        """SELECT v.*, p.anio, p.mes, p.estado AS periodo_estado
           FROM valorizaciones v JOIN periodos p ON p.id = v.periodo_id WHERE v.id=$1""", vid)
    if not v:
        raise HTTPException(404, "Valorización no encontrada")
    lineas = await pool.fetch(
        """SELECT l.*, l.cantidad::float AS cantidad, l.pu::float AS pu,
                  (l.cantidad * l.pu)::float AS parcial,
                  ev.codigo, ev.descripcion, ev.unidad, ev.fase
           FROM valorizacion_lineas l JOIN ev_partidas ev ON ev.id = l.partida_id
           WHERE l.valorizacion_id = $1 ORDER BY ev.codigo""", vid)
    total = round(sum(float(r["parcial"]) for r in lineas), 2)
    return {"valorizacion": dict(v), "lineas": [dict(r) for r in lineas], "total": total}


@router.put("/{vid}/lineas")
async def editar_lineas(vid: int, data: dict):
    """Reemplaza líneas {lineas: [{partida_id, cantidad, pu}]} — solo BORRADOR."""
    pool = await db()
    async with pool.acquire() as con:
        v = await con.fetchrow("SELECT * FROM valorizaciones WHERE id=$1", vid)
        if not v:
            raise HTTPException(404, "Valorización no encontrada")
        if v["estado"] != "BORRADOR":
            raise HTTPException(409, "Solo se edita en BORRADOR")
        async with con.transaction():
            await con.execute("DELETE FROM valorizacion_lineas WHERE valorizacion_id=$1", vid)
            n = 0
            for ln in data.get("lineas") or []:
                await con.execute(
                    """INSERT INTO valorizacion_lineas (valorizacion_id, partida_id, cantidad, pu)
                       VALUES ($1,$2,$3,$4)
                       ON CONFLICT (valorizacion_id, partida_id)
                       DO UPDATE SET cantidad=$3, pu=$4""",
                    vid, int(ln["partida_id"]), float(ln.get("cantidad") or 0),
                    float(ln.get("pu") or 0))
                n += 1
    return {"ok": True, "lineas": n}


@router.post("/{vid}/estado")
async def transicionar(vid: int, data: dict):
    """{accion: 'presentar' | 'aprobar' | 'devolver'} — devolver regresa a BORRADOR."""
    accion = str(data.get("accion") or "").lower()
    pool = await db()
    async with pool.acquire() as con:
        v = await con.fetchrow("SELECT * FROM valorizaciones WHERE id=$1", vid)
        if not v:
            raise HTTPException(404, "Valorización no encontrada")
        if accion == "devolver":
            if v["estado"] == "APROBADA":
                raise HTTPException(409, "Una APROBADA no se devuelve (reabre el periodo y crea otra)")
            await con.execute("UPDATE valorizaciones SET estado='BORRADOR' WHERE id=$1", vid)
            return {"ok": True, "estado": "BORRADOR"}
        objetivo = {"presentar": "PRESENTADA", "aprobar": "APROBADA"}.get(accion)
        if not objetivo:
            raise HTTPException(400, "accion inválida (presentar | aprobar | devolver)")
        if _TRANSICIONES.get(v["estado"]) != objetivo:
            raise HTTPException(409, f"Transición inválida desde {v['estado']}")
        if objetivo == "APROBADA":
            await exigir_abierto(con, v["periodo_id"])
            await con.execute(
                "UPDATE valorizaciones SET estado='APROBADA', aprobado_en=now() WHERE id=$1", vid)
        else:
            await con.execute("UPDATE valorizaciones SET estado='PRESENTADA' WHERE id=$1", vid)
    return {"ok": True, "estado": objetivo}
