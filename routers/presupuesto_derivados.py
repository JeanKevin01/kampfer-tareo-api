# ============================================================
# routers/presupuesto_derivados.py — derivados PUROS del APU (F1.3)
#
# Funciones sin BD, testeables. Trabajan sobre dicts genéricos:
#   recurso  = {tipo: MO|MAT|EQ|SUB, cantidad, precio, parcial, sub?}
#              (sub = clave/objeto que `obtener_sub` sabe resolver a la lista
#               de recursos de la subpartida)
#   partida  = {fase, metrado, recursos: [recurso]}
#
# Decisiones (espejo del Excel del gerente):
#   · hh_meta = SOLO la MO directa de la partida (Σ cantidad_MO × metrado) —
#     coincide con la columna R de PU-Meta (validado: 65,946.3 HH en el real).
#   · El costo por (fase, tipo de recurso) SÍ expande las subpartidas: el
#     dinero de una SUB se reparte en los tipos reales (MO/MAT/EQ) de su
#     receta, escalado por cantidad de uso (recursivo).
# ============================================================
from collections import defaultdict
from typing import Callable, Optional


def hh_meta(recursos, metrado: float) -> float:
    """HH meta de una partida = Σ(cantidad de recursos MO) × metrado."""
    hh_und = sum(float(r.get("cantidad") or 0) for r in recursos if r.get("tipo") == "MO")
    return round(hh_und * float(metrado or 0), 2)


def costo_por_tipo_unitario(recursos, obtener_sub: Optional[Callable] = None,
                            _profundidad: int = 0) -> dict:
    """Costo por tipo de recurso PARA 1 UNIDAD de la partida: {tipo: monto}.

    Las SUB se expanden a la receta de su subpartida, pero la receta solo aporta
    la PROPORCIÓN entre tipos: el monto total de la SUB es SIEMPRE su parcial
    declarado (así una receta inconsistente de la plantilla no infla el costo).
    Si una SUB no se puede resolver, su parcial se queda como tipo 'SUB'."""
    out: dict = defaultdict(float)
    if _profundidad > 8:          # protección ante ciclos imprevistos
        return dict(out)
    for r in recursos:
        tipo = r.get("tipo")
        if tipo != "SUB":
            out[tipo] += float(r.get("parcial") or 0)
            continue
        objetivo = float(r.get("parcial") or 0)
        sub_rec = obtener_sub(r) if obtener_sub else None
        interno = costo_por_tipo_unitario(sub_rec, obtener_sub, _profundidad + 1) if sub_rec else {}
        tot_interno = sum(interno.values())
        if tot_interno <= 0:
            out["SUB"] += objetivo
            continue
        factor = objetivo / tot_interno
        for t, m in interno.items():
            out[t] += m * factor
    return dict(out)


def costo_meta_por_fase_recurso(partidas, obtener_sub: Optional[Callable] = None) -> dict:
    """{(fase, tipo): Σ costo_unitario × metrado} sobre las partidas hoja.

    Si la partida trae `pu` (su CUD declarado), el costo unitario se ESCALA para
    que sume exactamente ese PU: el total por tipos reproduce el total del
    presupuesto (PtoMeta) al centavo, y una partida con PU 0 ("no considerada
    en la meta") aporta 0 aunque su APU liste recursos."""
    out: dict = defaultdict(float)
    for p in partidas:
        unit = costo_por_tipo_unitario(p.get("recursos") or [], obtener_sub)
        pu = p.get("pu")
        if pu is not None:
            tot = sum(unit.values())
            factor = (float(pu) / tot) if tot > 0 else 0.0
            unit = {t: m * factor for t, m in unit.items()}
        met = float(p.get("metrado") or 0)
        fase = p.get("fase")
        for t, m in unit.items():
            out[(fase, t)] += m * met
    return {k: round(v, 2) for k, v in out.items() if abs(v) > 0.005}


def productividad_meta(partida) -> Optional[float]:
    """und/día — directo del APU (rendimiento MO)."""
    v = partida.get("rendimiento_mo")
    return float(v) if v is not None else None


def ratio_meta(partida) -> Optional[float]:
    """HH/und meta = hh_meta / metrado."""
    met = float(partida.get("metrado") or 0)
    if met <= 0:
        return None
    return round(float(partida.get("hh_meta") or 0) / met, 4)
