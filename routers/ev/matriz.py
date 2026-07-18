# ============================================================
# routers/ev/matriz.py — matriz histórica fechas × filas (mejoras UX pre-F4)
#
# GET /ev/matriz?desde&hasta&modo=partidas|trabajadores|supervisores&celda=hh|cantidad&otm=
# La vista longitudinal "estilo Excel": columnas = días, filas = partidas /
# personal / supervisores, celdas = HH del tareo (o cantidad ejecutada).
# Fuentes: tareo_partida (HH por fecha, ya indexada) y ev_avances_diarios.
# `_pivotear` es pura (testeable sin BD). Shape sparse: celdas = {fecha: valor}.
# ============================================================
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException

from core.db import db
from core.tiempo import parse_fecha, semana_de

router = APIRouter()
router_campo = APIRouter()

_MODOS = ("partidas", "trabajadores", "supervisores")
_MAX_DIAS = 366


def _pivotear(rows, fechas):
    """rows [{id, etiqueta, grupo, fecha, valor}] → (filas, tot_col, max_celda).

    max_celda = percentil 95 de las celdas (para el heat-coloring del panel:
    un outlier no debe aplanar la escala del resto)."""
    filas_map: dict = {}
    tot_col = {f: 0.0 for f in fechas}
    valores = []
    for r in rows:
        fid = str(r["id"])
        fila = filas_map.setdefault(fid, {
            "id": fid, "etiqueta": r["etiqueta"], "grupo": r.get("grupo"),
            "celdas": {}, "total": 0.0})
        f = str(r["fecha"])
        v = round(float(r["valor"] or 0), 2)
        if not v:
            continue
        fila["celdas"][f] = round(fila["celdas"].get(f, 0) + v, 2)
        fila["total"] = round(fila["total"] + v, 2)
        if f in tot_col:
            tot_col[f] = round(tot_col[f] + v, 2)
        valores.append(v)
    filas = sorted(filas_map.values(), key=lambda x: (x["grupo"] or "~", x["etiqueta"]))
    tot_col = {f: v for f, v in tot_col.items() if v}
    valores.sort()
    max_celda = valores[int(0.95 * (len(valores) - 1))] if valores else 0.0
    return filas, tot_col, max_celda


_SQL = {
    ("partidas", "hh"): """
        SELECT tp.partida_id AS id, ev.codigo || ' — ' || COALESCE(ev.descripcion,'') AS etiqueta,
               ev.fase AS grupo, tp.fecha, SUM(tp.hh) AS valor
        FROM tareo_partida tp JOIN ev_partidas ev ON ev.id = tp.partida_id
        WHERE tp.hh IS NOT NULL AND tp.fecha BETWEEN $1 AND $2 {otm}
        GROUP BY tp.partida_id, ev.codigo, ev.descripcion, ev.fase, tp.fecha""",
    ("partidas", "cantidad"): """
        SELECT ad.partida_id AS id, ev.codigo || ' — ' || COALESCE(ev.descripcion,'') AS etiqueta,
               ev.fase AS grupo, ad.fecha, SUM(ad.cantidad_dia) AS valor
        FROM ev_avances_diarios ad JOIN ev_partidas ev ON ev.id = ad.partida_id
        WHERE ad.fecha BETWEEN $1 AND $2 AND ad.hito_id IS NULL {otm_ev}
        GROUP BY ad.partida_id, ev.codigo, ev.descripcion, ev.fase, ad.fecha""",
    ("trabajadores", "hh"): """
        SELECT tp.trabajador_id AS id,
               COALESCE(t.nombre, 'Trab. ' || tp.trabajador_id) AS etiqueta,
               t.cargo AS grupo, tp.fecha, SUM(tp.hh) AS valor
        FROM tareo_partida tp LEFT JOIN trabajadores t ON t.id = tp.trabajador_id
        WHERE tp.hh IS NOT NULL AND tp.fecha BETWEEN $1 AND $2 {otm}
        GROUP BY tp.trabajador_id, t.nombre, t.cargo, tp.fecha""",
    ("supervisores", "hh"): """
        SELECT tp.supervisor_id AS id,
               COALESCE(s.nombre, 'Sup. ' || tp.supervisor_id) AS etiqueta,
               NULL::text AS grupo, tp.fecha, SUM(tp.hh) AS valor
        FROM tareo_partida tp LEFT JOIN supervisores s ON s.id = tp.supervisor_id
        WHERE tp.hh IS NOT NULL AND tp.supervisor_id IS NOT NULL
          AND tp.fecha BETWEEN $1 AND $2 {otm}
        GROUP BY tp.supervisor_id, s.nombre, tp.fecha""",
}


@router.get("/matriz")
async def matriz(desde: str = "", hasta: str = "", modo: str = "partidas",
                 celda: str = "hh", otm: str = "", proyecto_id: int = 1):
    if modo not in _MODOS:
        raise HTTPException(400, f"modo inválido (usa {'/'.join(_MODOS)})")
    if celda not in ("hh", "cantidad"):
        raise HTTPException(400, "celda inválida (usa hh o cantidad)")
    if celda == "cantidad" and modo != "partidas":
        raise HTTPException(400, "celda=cantidad solo aplica con modo=partidas")

    f_hasta = parse_fecha(hasta) or date.today()
    f_desde = parse_fecha(desde) or (f_hasta - timedelta(days=27))
    if f_hasta < f_desde:
        raise HTTPException(400, "hasta debe ser >= desde")
    if (f_hasta - f_desde).days > _MAX_DIAS:
        raise HTTPException(400, f"Rango máximo: {_MAX_DIAS} días")

    fechas = [str(f_desde + timedelta(days=i)) for i in range((f_hasta - f_desde).days + 1)]

    sql = _SQL[(modo, celda)]
    args = [f_desde, f_hasta]
    if otm:
        args.append(otm)
        sql = sql.format(otm="AND tp.otm_id = $3", otm_ev="AND ev.otm_id = $3")
    else:
        sql = sql.format(otm="", otm_ev="")

    pool = await db()
    async with pool.acquire() as con:
        rows = [dict(r) for r in await con.fetch(sql, *args)]
        # Nombres de fase del catálogo para el agrupador (modo partidas)
        if modo == "partidas":
            nombres = {r["codigo"]: r["nombre"] for r in await con.fetch(
                "SELECT codigo, nombre FROM fases WHERE proyecto_id = $1", proyecto_id)}
            for r in rows:
                if r.get("grupo"):
                    n = nombres.get(r["grupo"])
                    r["grupo"] = f"{r['grupo']} — {n}" if n else r["grupo"]
        # Numeración de semanas kampfer para los separadores de columnas
        from routers.ev._datos import _fecha_base
        base = await _fecha_base(con)

    filas, tot_col, max_celda = _pivotear(rows, fechas)

    semanas, _ult = [], None
    for f in fechas:
        d = date.fromisoformat(f)
        num = semana_de(d, base) if base else d.isocalendar()[1]
        if _ult and _ult["semana"] == num:
            _ult["n"] += 1
        else:
            _ult = {"semana": num, "inicio": f, "n": 1}
            semanas.append(_ult)

    return {"desde": str(f_desde), "hasta": str(f_hasta), "modo": modo, "celda": celda,
            "fechas": fechas, "semanas": semanas, "filas": filas,
            "tot_col": tot_col, "max_celda": max_celda}
