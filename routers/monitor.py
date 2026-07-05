# ============================================================
# routers/monitor.py — seguimiento diario de HH e integridad del tareo
# ============================================================
from typing import Optional

from fastapi import APIRouter

from core.db import db as core_db
from core.tiempo import fecha_lima, parse_fecha
from routers.jornada import resolver_jornada

router = APIRouter(tags=["monitor"])


@router.get("/api/monitor/hh-diario")
async def monitor_hh_diario(fecha: Optional[str] = None):
    """Por cada trabajador en la fecha: total de HH registradas sumando TODAS sus
    OTMs/partidas, comparado con la jornada vigente. Semáforo de alertas para
    detectar errores en el tareo (sub-registro o horas extra)."""
    f = parse_fecha(fecha) or fecha_lima()
    jornada = await resolver_jornada(f)
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT tp.trabajador_id, t.nombre, tp.otm_id, "
        "       SUM(tp.hh) AS hh, COUNT(*) AS n "
        "FROM tareo_partida tp "
        "LEFT JOIN trabajadores t ON t.id = tp.trabajador_id "
        "WHERE tp.fecha = $1 "
        "GROUP BY tp.trabajador_id, t.nombre, tp.otm_id "
        "ORDER BY t.nombre, tp.otm_id",
        f,
    )
    por_trab: dict = {}
    for r in rows:
        tid = r["trabajador_id"]
        d = por_trab.setdefault(tid, {
            "trab_id": tid, "nombre": r["nombre"] or tid,
            "total_hh": 0.0, "n_partidas": 0, "otms": [],
        })
        hh = float(r["hh"] or 0)
        d["total_hh"]   += hh
        d["n_partidas"] += int(r["n"] or 0)
        d["otms"].append({"otm_id": r["otm_id"], "hh": round(hh, 2), "n_partidas": int(r["n"] or 0)})

    filas = []
    for d in por_trab.values():
        d["total_hh"] = round(d["total_hh"], 2)
        d["jornada"]  = jornada
        diff = d["total_hh"] - jornada
        d["estado"]    = "ok" if abs(diff) < 0.15 else ("bajo" if diff < 0 else "extra")
        d["diff"]      = round(diff, 2)
        d["multi_otm"] = len(d["otms"]) > 1
        filas.append(d)
    # Alertas primero, luego por nombre
    filas.sort(key=lambda x: (x["estado"] == "ok", x["nombre"]))

    resumen = {
        "fecha": f.isoformat(), "jornada": jornada, "trabajadores": len(filas),
        "ok":    sum(1 for d in filas if d["estado"] == "ok"),
        "bajo":  sum(1 for d in filas if d["estado"] == "bajo"),
        "extra": sum(1 for d in filas if d["estado"] == "extra"),
    }
    return {"resumen": resumen, "filas": filas}


@router.get("/api/monitor/duplicados-hh")
async def monitor_duplicados_hh(fecha: Optional[str] = None):
    """Trabajadores con HH registradas en más de una sesión el mismo día
    (posible doble envío entre supervisores)."""
    f = parse_fecha(fecha) or fecha_lima()
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT tp.trabajador_id, t.nombre, "
        "       COUNT(DISTINCT tp.sesion_id)    AS n_sesiones, "
        "       COUNT(DISTINCT tp.supervisor_id) AS n_supervisores, "
        "       COUNT(DISTINCT tp.otm_id)        AS n_otms, "
        "       SUM(tp.hh)                       AS total_hh "
        "FROM tareo_partida tp "
        "LEFT JOIN trabajadores t ON t.id = tp.trabajador_id "
        "WHERE tp.fecha = $1 "
        "GROUP BY tp.trabajador_id, t.nombre "
        "HAVING COUNT(DISTINCT tp.sesion_id) > 1 "
        "ORDER BY SUM(tp.hh) DESC",
        f,
    )
    filas = [{
        "trab_id": r["trabajador_id"], "nombre": r["nombre"] or r["trabajador_id"],
        "n_sesiones": int(r["n_sesiones"]), "n_supervisores": int(r["n_supervisores"]),
        "n_otms": int(r["n_otms"]), "total_hh": round(float(r["total_hh"] or 0), 2),
    } for r in rows]
    return {"fecha": f.isoformat(), "total": len(filas), "filas": filas}
