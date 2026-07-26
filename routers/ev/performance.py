# ============================================================
# routers/ev/performance.py — historial de performance (tab Performance)
#
# Serie semanal ACUMULADA tipo plantilla ISP del ex-gerente: avance %,
# HH ganadas/gastadas, PF, productividad y EAC por semana — 100% derivada
# del motor EV puro (_calcular) sobre los datos ya registrados: no se
# llena ni se guarda nada, se alimenta sola del avance diario (rollup
# 0025) y del tareo.
# ============================================================
from typing import Optional

from fastapi import APIRouter, HTTPException

from core.log import get_logger
from routers.ev._datos import _datos_base, _get
from routers.ev._engine import _calcular

log = get_logger("ev")

router = APIRouter()
router_campo = APIRouter()


@router.get("/performance")
async def performance(hasta: int, otm: Optional[str] = None):
    """Historial semanal de performance (S1..hasta). Cada punto se calcula
    con el corte del motor EV en esa semana — mismas fórmulas del Resumen."""
    try:
        partidas, hitos, avances, hh, tareo, _split = await _datos_base(hasta, otm)
        if not partidas:
            return {"hasta": hasta, "otm": otm, "serie": []}
        # Denominador = BAC único (mismo criterio del Resumen: hh_actualizado si
        # oficina lo fijó, si no la reproyección de metrado). Antes esta serie
        # usaba `hh_actualizado or hh_presup` y podía dar un % distinto al del
        # tablero cuando había metrado reproyectado.
        filas_bac = _calcular(partidas, hitos, avances, hh, tareo, hasta)
        hh_presup_total = sum(f["hh_bac"] for f in filas_bac)
        semanas_set = sorted(
            {a["semana"] for a in avances} | {r["semana"] for r in hh}
            | {s for (_, s) in tareo.keys()} | {hasta}
        )
        serie = []
        for s in semanas_set:
            if s > hasta:
                continue
            filas = _calcular(partidas, hitos, avances, hh, tareo, s)
            g = sum(f["hh_ganadas_acum"] for f in filas)
            c = sum(f["hh_gastadas_acum"] for f in filas)
            gs = sum(f["hh_ganadas_sem"] for f in filas)
            cs = sum(f["hh_gastadas_sem"] for f in filas)
            inst = sum(f["cantidad_instalada"] for f in filas)
            eac = sum(f["eac_hh"] for f in filas)
            serie.append({
                "semana": s,
                "pct_acum": round(g / hh_presup_total, 4) if hh_presup_total > 0 else None,
                "hh_ganadas_sem": round(gs, 2),
                "hh_ganadas_acum": round(g, 2),
                "hh_gastadas_sem": round(cs, 2),
                "hh_gastadas_acum": round(c, 2),
                "pf_sem": round(gs / cs, 3) if cs > 0 else None,
                "pf_acum": round(g / c, 3) if c > 0 else None,
                "cant_instalada": round(inst, 2),
                "eac_hh": round(eac, 2),
                "desvio_hh": round(eac - hh_presup_total, 2),
            })
        return {"hasta": hasta, "otm": otm,
                "hh_presup_total": round(hh_presup_total, 2), "serie": serie}
    except HTTPException:
        raise
    except Exception:
        log.exception("error calculando historial de performance")
        raise HTTPException(500, "Error interno calculando performance")
