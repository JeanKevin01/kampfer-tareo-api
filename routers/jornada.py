# ============================================================
# routers/jornada.py — reglas de HH por día (configurables, con vigencia)
#
# `resolver_jornada` es la función de resolución que también usan tareo.py
# y monitor.py (puntual > semanal > fallback; regla de OTM gana a la global).
# ============================================================
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db as core_db
from core.tiempo import fecha_lima, parse_fecha

router = APIRouter(tags=["jornada"])

DIAS_SEM = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


async def resolver_jornada(fecha: date, otm_id: Optional[str] = None) -> float:
    """HH de jornada vigentes para una fecha (y opcionalmente una OTM):
    En cada nivel, una regla de la OTM específica gana sobre la global (otm_id NULL).
    1) excepción puntual exacta de ese día,
    2) regla semanal del día-de-semana con la mayor 'desde' <= fecha,
    3) fallback (Miércoles 10, resto 9.5)."""
    dow = fecha.weekday()
    pool = await core_db()
    # 1) Puntual: OTM específica → global
    if otm_id:
        v = await pool.fetchval(
            "SELECT hh FROM ev_jornada_reglas WHERE tipo='puntual' AND desde=$1 AND otm_id=$2 "
            "ORDER BY id DESC LIMIT 1", fecha, otm_id)
        if v is not None:
            return float(v)
    v = await pool.fetchval(
        "SELECT hh FROM ev_jornada_reglas WHERE tipo='puntual' AND desde=$1 AND otm_id IS NULL "
        "ORDER BY id DESC LIMIT 1", fecha)
    if v is not None:
        return float(v)
    # 2) Semanal: OTM específica → global
    if otm_id:
        v = await pool.fetchval(
            "SELECT hh FROM ev_jornada_reglas WHERE tipo='semanal' AND dia_semana=$1 AND desde<=$2 "
            "AND otm_id=$3 ORDER BY desde DESC, id DESC LIMIT 1", dow, fecha, otm_id)
        if v is not None:
            return float(v)
    v = await pool.fetchval(
        "SELECT hh FROM ev_jornada_reglas WHERE tipo='semanal' AND dia_semana=$1 AND desde<=$2 "
        "AND otm_id IS NULL ORDER BY desde DESC, id DESC LIMIT 1", dow, fecha)
    if v is not None:
        return float(v)
    return 10.0 if dow == 2 else 9.5


@router.get("/api/jornada")
async def jornada_listar(otm: Optional[str] = None):
    """Reglas + HH resueltas para los 7 días de hoy. 'otm' (opcional) calcula
    los vigentes para esa OTM (regla de la OTM gana sobre la global)."""
    pool = await core_db()
    reglas = await pool.fetch(
        "SELECT id, tipo, desde::text AS desde, dia_semana, hh, nota, otm_id, "
        "       creado_en::text AS creado_en "
        "FROM ev_jornada_reglas ORDER BY tipo, otm_id NULLS FIRST, desde DESC, dia_semana"
    )
    otm_id = (otm or "").strip() or None
    hoy = fecha_lima()
    lunes = hoy - timedelta(days=hoy.weekday())
    vigentes = []
    for dow in range(7):
        f = lunes + timedelta(days=dow)
        vigentes.append({
            "dia_semana": dow,
            "dia": DIAS_SEM[dow],
            "hh": await resolver_jornada(f, otm_id),
        })
    return {
        "otm": otm_id,
        "vigentes": vigentes,
        "puntuales": [dict(r) for r in reglas if r["tipo"] == "puntual"],
        "semanal":   [dict(r) for r in reglas if r["tipo"] == "semanal"],
    }


@router.get("/api/jornada/resolver")
async def jornada_resolver(fecha: Optional[str] = None, otm: Optional[str] = None):
    """HH referenciales para una fecha y OTM (la usa el panel del supervisor)."""
    f = parse_fecha(fecha) or fecha_lima()
    otm_id = (otm or "").strip() or None
    return {"fecha": f.isoformat(), "dia_semana": f.weekday(), "otm": otm_id,
            "hh": await resolver_jornada(f, otm_id)}


@router.post("/api/jornada")
async def jornada_guardar(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Crea reglas de jornada. 'otm_id' opcional (null/ausente = todas las OTMs).
    Semanal: {tipo:'semanal', desde:'YYYY-MM-DD', dias:{0:9.5,...,2:10}, otm_id?}
    Puntual: {tipo:'puntual', fecha:'YYYY-MM-DD', hh:12, nota?, otm_id?}"""
    tipo = str(data.get("tipo", "semanal"))
    otm_id = (str(data.get("otm_id") or "").strip()) or None
    if tipo == "puntual":
        f = parse_fecha(data.get("fecha"))
        if not f:
            raise HTTPException(400, "fecha requerida para excepción puntual")
        hh = float(data.get("hh", 0))
        if hh <= 0:
            raise HTTPException(400, "hh debe ser > 0")
        pool = await core_db()
        await pool.execute(
            "INSERT INTO ev_jornada_reglas (tipo, desde, dia_semana, hh, nota, otm_id) "
            "VALUES ('puntual', $1, NULL, $2, $3, $4)",
            f, hh, data.get("nota"), otm_id,
        )
        return {"ok": True}

    # semanal
    desde = parse_fecha(data.get("desde")) or fecha_lima()
    dias  = data.get("dias", {})
    if not dias:
        raise HTTPException(400, "dias requerido (ej. {\"0\":9.5,\"2\":10})")
    nota = data.get("nota")
    n = 0
    pool = await core_db()
    for dow, hh in dias.items():
        try:
            dow_i = int(dow); hh_f = float(hh)
        except (TypeError, ValueError):
            continue
        if dow_i < 0 or dow_i > 6 or hh_f <= 0:
            continue
        await pool.execute(
            "INSERT INTO ev_jornada_reglas (tipo, desde, dia_semana, hh, nota, otm_id) "
            "VALUES ('semanal', $1, $2, $3, $4, $5)",
            desde, dow_i, hh_f, nota, otm_id,
        )
        n += 1
    return {"ok": True, "reglas_creadas": n}


@router.delete("/api/jornada/{regla_id}")
async def jornada_eliminar(regla_id: int, _u: dict = Depends(require_role("oficina"))):
    pool = await core_db()
    await pool.execute("DELETE FROM ev_jornada_reglas WHERE id = $1", regla_id)
    return {"ok": True}
