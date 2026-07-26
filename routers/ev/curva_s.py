# ============================================================
# routers/ev/curva_s.py — Curva S completa (PV · EV · AC) + indicadores EVM
#
# Hasta ahora /ev/curva devolvía solo EV y AC: dos curvas REALES, ninguna
# línea base. Sin PV no existe el eje de tiempo, así que el sistema sabía
# decir "gasté bien o mal" (CPI) pero NO "voy adelantado o atrasado" (SPI).
#
# El PV se pudo construir gracias al módulo de Programación: `prog_metrado_dia`
# guarda el metrado programado DÍA a DÍA y `prog_actividades` lo ata a una
# partida y a un hito. La conversión a HH usa EXACTAMENTE la misma fórmula que
# el EV (peso del hito × cantidad × productividad presupuestada), así que PV y
# EV son perfectamente comparables — mismo denominador, mismos pesos.
#
#     PV(hito) = peso × min(cantidad_PROGRAMADA_acum, metrado) × prod_presup
#     EV(hito) = peso × min(cantidad_REAL_acum,       metrado) × prod_presup
#
# Las semanas del Lookahead y las del EV coinciden (ambas arrancan en el lunes
# ISO que ancla `fecha_base`), por eso no hace falta ninguna conversión rara.
#
# HONESTIDAD DEL DATO: el PV solo existe donde el planner programó. Mientras no
# haya línea base congelada, esta curva es el PLAN VIGENTE (rodante), no un
# PMB: por eso el endpoint devuelve `cobertura`, para que la UI lo rotule sin
# mentir. El SPI real llega con la línea base (paso 4).
# ============================================================
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, HTTPException

from core.db import db
from core.log import get_logger
from core.tiempo import semana_de as _semana_de
from routers.ev._datos import _datos_base, _fecha_base
from routers.ev._engine import _calcular

log = get_logger("ev")

router = APIRouter()
router_campo = APIRouter()


# ── Función pura: metrado programado → PV en HH ──────────────
def _pv_acum_por_semana(prog, partidas, hitos, semanas) -> dict:
    """{semana: PV acumulado en HH}. Función PURA (sin BD) → testeable.

    prog: [{partida_id, hito_id, semana, cantidad}] — hito_id None = principal.
    Espeja `_calcular`: mismo peso por hito, mismo tope `min(...,1.0)` para que
    sobre-programar una etapa no infle el PV por encima de su peso.
    """
    pinfo = {}
    for p in partidas:
        mp = float(p["metrado_proyec"] or p["metrado_presup"] or 0)
        m_presup = float(p["metrado_presup"] or 0)
        hh_presup = float(p["hh_presup"] or 0)
        pinfo[p["id"]] = {
            "mp": mp,
            "prod": (hh_presup / m_presup) if m_presup > 0 else 0.0,
        }

    peso_de: dict = {}
    principal: dict = {}
    for h in hitos:
        peso_de[(h["partida_id"], h["id"])] = float(h["peso"])
        if h["es_principal"]:
            principal.setdefault(h["partida_id"], h["id"])

    # cantidad programada por (partida, hito) y semana
    acum: dict = defaultdict(lambda: defaultdict(float))
    for r in prog:
        pid = r["partida_id"]
        if pid not in pinfo:
            continue
        hid = r["hito_id"] or principal.get(pid)
        if hid is None:      # partida sin hitos definidos aún
            continue
        acum[(pid, hid)][r["semana"]] += float(r["cantidad"] or 0)

    out = {}
    for s in semanas:
        tot = 0.0
        for (pid, hid), por_sem in acum.items():
            cant = sum(v for sem, v in por_sem.items() if sem <= s)
            info = pinfo[pid]
            if info["mp"] <= 0:
                continue
            frac = min(cant / info["mp"], 1.0)
            tot += peso_de.get((pid, hid), 0.0) * frac * info["mp"] * info["prod"]
        out[s] = round(tot, 2)
    return out


def _indicadores(pv: float, ev: float, ac: float, bac: float, eac: float) -> dict:
    """Los indicadores EVM del diagrama clásico, todos sobre el MISMO BAC."""
    return {
        "pv": round(pv, 2), "ev": round(ev, 2), "ac": round(ac, 2),
        "bac": round(bac, 2), "eac": round(eac, 2),
        # Cronograma: ¿voy adelantado o atrasado? (lo que faltaba)
        "sv": round(ev - pv, 2),
        "spi": round(ev / pv, 3) if pv > 0 else None,
        # Costo: ¿rindo mejor o peor de lo presupuestado?
        "cv": round(ev - ac, 2),
        "cpi": round(ev / ac, 3) if ac > 0 else None,
        # Proyección
        "etc": round(eac - ac, 2),
        "vac": round(bac - eac, 2),
        "tcpi": round((bac - ev) / (bac - ac), 3) if (bac - ac) > 0 else None,
    }


@router.get("/curva-s")
async def curva_s(hasta: int, otm: Optional[str] = None):
    """Curva S completa: PV (plan) · EV (ganado) · AC (gastado) + indicadores.

    `hasta` = semana de corte. Devuelve la serie acumulada semana a semana, el
    BAC como línea horizontal, la proyección EAC y la cobertura del plan.
    """
    if hasta < 1:
        raise HTTPException(400, "La semana de corte debe ser >= 1")
    try:
        partidas, hitos, avances, hh, tareo, _split = await _datos_base(hasta, otm)
        if not partidas:
            return {"hasta": hasta, "otm": otm, "serie": [], "indicadores": None,
                    "cobertura": None}

        pids = {p["id"] for p in partidas}
        pool = await db()
        async with pool.acquire() as con:
            base = await _fecha_base(con)
            prog_rows = await con.fetch(
                """SELECT a.partida_id, a.hito_id, pm.fecha, pm.cantidad
                   FROM prog_metrado_dia pm
                   JOIN prog_actividades a ON a.id = pm.actividad_id
                   WHERE a.partida_id IS NOT NULL
                     AND a.estado <> 'CANCELADO'""")

        # fecha → semana del proyecto (misma base lunes que usa el ISP)
        prog = []
        if base:
            for r in prog_rows:
                if r["partida_id"] in pids:
                    prog.append({"partida_id": r["partida_id"], "hito_id": r["hito_id"],
                                 "semana": _semana_de(r["fecha"], base),
                                 "cantidad": r["cantidad"]})

        # Semanas a graficar: las que tienen algún dato (real o programado)
        semanas = sorted({a["semana"] for a in avances} | {r["semana"] for r in hh}
                         | {s for (_, s) in tareo.keys()}
                         | {r["semana"] for r in prog if r["semana"] >= 1}
                         | {hasta})
        semanas = [s for s in semanas if 1 <= s <= hasta]

        pv_acum = _pv_acum_por_semana(prog, partidas, hitos, semanas)

        serie, prev = [], {"pv": 0.0, "ev": 0.0, "ac": 0.0}
        filas_corte = None
        for s in semanas:
            filas = _calcular(partidas, hitos, avances, hh, tareo, s)
            if s == hasta:
                filas_corte = filas
            ev = sum(f["hh_ganadas_acum"] for f in filas)
            ac = sum(f["hh_gastadas_acum"] for f in filas)
            pv = pv_acum.get(s, 0.0)
            serie.append({
                "semana": s,
                "pv": round(pv, 2), "ev": round(ev, 2), "ac": round(ac, 2),
                "pv_sem": round(pv - prev["pv"], 2),
                "ev_sem": round(ev - prev["ev"], 2),
                "ac_sem": round(ac - prev["ac"], 2),
                # Variaciones acumuladas, para las bandas del gráfico
                "sv": round(ev - pv, 2),
                "cv": round(ev - ac, 2),
                "spi": round(ev / pv, 3) if pv > 0 else None,
                "cpi": round(ev / ac, 3) if ac > 0 else None,
            })
            prev = {"pv": pv, "ev": ev, "ac": ac}

        filas_corte = filas_corte if filas_corte is not None else []
        bac = sum(f["hh_bac"] for f in filas_corte)
        eac = sum(f["eac_hh"] for f in filas_corte)
        ult = serie[-1] if serie else {"pv": 0, "ev": 0, "ac": 0}
        ind = _indicadores(ult["pv"], ult["ev"], ult["ac"], bac, eac)

        # ── Cobertura: qué parte del presupuesto TIENE plan ──
        # Sin esto la curva azul parecería una línea base completa cuando en
        # realidad solo cubre lo programado. Se rotula en la UI.
        con_plan = {r["partida_id"] for r in prog}
        bac_con_plan = sum(f["hh_bac"] for f in filas_corte
                           if f["partida_id"] in con_plan)
        cobertura = {
            "partidas_con_plan": len(con_plan & pids),
            "partidas_total": len(pids),
            "bac_con_plan": round(bac_con_plan, 2),
            "pct_bac_planificado": round(bac_con_plan / bac, 4) if bac > 0 else 0,
        }

        return {"hasta": hasta, "otm": otm, "serie": serie,
                "indicadores": ind, "cobertura": cobertura}
    except HTTPException:
        raise
    except Exception:
        log.exception("error calculando la curva S")
        raise HTTPException(500, "Error interno calculando la curva S")
