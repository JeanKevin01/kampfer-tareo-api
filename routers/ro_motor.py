# ============================================================
# routers/ro_motor.py — F2.5: motor PURO del RO mensual
#
# Espejo del Excel "RO 2007" del ex-gerente (hojas R FASES y T OBRA):
#   · REAL(mes, fase, recurso) = documentos del mes (+ MO: tareo×tarifa + ajustes
#     de planilla, que entran como documentos MO).
#   · ACUM = Σ REAL hasta el corte · PROY = proyección de meses futuros ·
#     SALDO DE OBRA = Σ PROY · META/CONTRACTUAL = presupuestos congelados.
#   · VENTA por concepto (T OBRA): CONTRACTUAL (valorización×PU) + ajustes por
#     tipo con su margen previsto.
#   · COSTO APLICADO = venta_acum × (costo_total_proyectado / venta_total_proyectada)
#     · RESULTADO PENDIENTE = costo_acum − costo_aplicado.
#   · MARGEN = venta − costo · % = margen/venta · MARGEN C/CONTINGENCIA.
#   · US$: cada mes se convierte con SU tipo_cambio; los acumulados son la suma
#     de los mensuales convertidos.
#
# TODO PURO: sin BD. Los insumos los arma ro_datos.py.
# ============================================================
from collections import defaultdict

RECURSOS = ("MAT", "MO", "EQP", "EQT", "SUB", "DIR", "GG")
CONCEPTOS_VENTA = ("CONTRACTUAL", "DIF_METRADO", "NUEVAS_PARTIDAS",
                   "POR_APROBAR", "REAJUSTE", "TERCEROS")


def _r2(x):
    return round(float(x or 0), 2)


def ro_mensual(*, periodos, corte_id, docs, mo_tareo_mes, venta_fase_mes,
               ajustes, costo_meta, costo_contractual, proyeccion,
               fases_indirectas=frozenset(), fases_desc=None,
               venta_proyectada_extra=0.0, prev=None, contingencia=0.0) -> dict:
    """Calcula el RO del mes de corte.

    periodos: [{id, anio, mes, tipo_cambio}] ASC (todos los del proyecto).
    corte_id: id del periodo de corte (debe estar en `periodos`).
    docs: [{periodo_id, fase, tipo_recurso, directo, monto}] (histórico completo).
    mo_tareo_mes: {periodo_id: {fase: monto}} — MO del tareo×tarifa por mes.
    venta_fase_mes: {periodo_id: {fase: monto}} — venta contractual del mes.
    ajustes: [{periodo_id, tipo, fase, monto, margen_previsto}] (venta_ajustes).
    costo_meta / costo_contractual: {(fase, recurso): monto} de los presupuestos.
    proyeccion: [{periodo_id, fase, tipo_recurso, monto, origen}] (meses > corte).
    fases_indirectas: set de fases que van al bloque indirecto.
    prev: {"venta": x, "costo": y, "por_recurso": {(dir, rec): monto}} — snapshot
          de proyección para el mes de corte (F2.6); None = sin PREV.
    """
    fases_desc = fases_desc or {}
    orden = [p["id"] for p in periodos]
    if corte_id not in orden:
        raise ValueError(f"corte {corte_id} no está en los periodos")
    idx_corte = orden.index(corte_id)
    hasta_corte = set(orden[:idx_corte + 1])
    tc = {p["id"]: float(p.get("tipo_cambio") or 1) or 1 for p in periodos}
    per_info = {p["id"]: p for p in periodos}

    # ── costo por (fase, recurso, periodo) = docs + MO del tareo ──
    celda = defaultdict(float)          # (fase, rec, periodo) -> monto
    dir_por_fase: dict = {}             # fase -> True si algún doc la marca directa
    for d in docs:
        f, rec, per = d.get("fase"), d["tipo_recurso"], d["periodo_id"]
        celda[(f, rec, per)] += float(d["monto"] or 0)
        if d.get("directo") is False:
            dir_por_fase.setdefault(f, False)
        else:
            dir_por_fase[f] = True
    for per, fases in (mo_tareo_mes or {}).items():
        for f, m in fases.items():
            celda[(f, "MO", per)] += float(m or 0)

    def _es_indirecta(f):
        if f in fases_indirectas:
            return True
        return dir_por_fase.get(f) is False

    # ── acumulados y mes por fase×recurso ──
    fases = sorted({f for (f, _, _) in celda} |
                   {f for m in (venta_fase_mes or {}).values() for f in m} |
                   {a.get("fase") for a in ajustes if a.get("fase")} |
                   {f for (f, _) in (costo_meta or {})} - {None},
                   key=lambda x: str(x))

    def _suma(f, rec, pers):
        return sum(v for (ff, rr, pp), v in celda.items()
                   if ff == f and rr == rec and pp in pers)

    r_fases = []
    for f in fases:
        fila = {"fase": f, "descripcion": fases_desc.get(f),
                "indirecta": _es_indirecta(f)}
        total = 0.0
        for rec in RECURSOS:
            v = _suma(f, rec, hasta_corte)
            fila[rec.lower()] = _r2(v)
            total += v
        venta_f = sum((venta_fase_mes.get(p, {}) or {}).get(f, 0) for p in hasta_corte) \
            + sum(float(a["monto"] or 0) for a in ajustes
                  if a.get("fase") == f and a.get("periodo_id") in hasta_corte)
        fila["total"] = _r2(total)
        fila["venta"] = _r2(venta_f)
        fila["margen"] = _r2(venta_f - total)
        fila["pct_margen"] = round((venta_f - total) / venta_f, 4) if venta_f else 0
        # META / CONTRACTUAL de la fase (Σ recursos del presupuesto)
        fila["meta"] = _r2(sum(m for (ff, _), m in (costo_meta or {}).items() if ff == f))
        fila["contractual"] = _r2(sum(m for (ff, _), m in (costo_contractual or {}).items() if ff == f))
        r_fases.append(fila)

    def _parcial(indirecta: bool):
        filas = [x for x in r_fases if x["indirecta"] == indirecta]
        out = {"fase": None, "descripcion": "PARCIAL INDIRECTOS" if indirecta else "PARCIAL DIRECTOS",
               "indirecta": indirecta}
        for k in [r.lower() for r in RECURSOS] + ["total", "venta", "margen", "meta", "contractual"]:
            out[k] = _r2(sum(x[k] for x in filas))
        out["pct_margen"] = round(out["margen"] / out["venta"], 4) if out["venta"] else 0
        return out

    parcial_dir = _parcial(False)
    parcial_ind = _parcial(True)
    total_obra = {"fase": None, "descripcion": "TOTAL OBRA", "indirecta": None}
    for k in [r.lower() for r in RECURSOS] + ["total", "venta", "margen", "meta", "contractual"]:
        total_obra[k] = _r2(parcial_dir[k] + parcial_ind[k])
    total_obra["pct_margen"] = round(total_obra["margen"] / total_obra["venta"], 4) \
        if total_obra["venta"] else 0

    # ── T OBRA: conceptos de venta (mes REAL / ACUM / SALDO) ──
    def _venta_contractual(pers):
        return sum(sum((venta_fase_mes.get(p, {}) or {}).values()) for p in pers) \
            + sum(float(a["monto"] or 0) for a in ajustes
                  if a["tipo"] == "CONTRACTUAL" and a.get("periodo_id") in pers)

    conceptos = []
    venta_real_mes = venta_acum = 0.0
    for tipo in CONCEPTOS_VENTA:
        if tipo == "CONTRACTUAL":
            mes_v = _venta_contractual({corte_id})
            acum_v = _venta_contractual(hasta_corte)
            margen_mes = margen_acum = 0.0
        else:
            propios = [a for a in ajustes if a["tipo"] == tipo]
            mes_v = sum(float(a["monto"] or 0) for a in propios if a.get("periodo_id") == corte_id)
            acum_v = sum(float(a["monto"] or 0) for a in propios if a.get("periodo_id") in hasta_corte)
            margen_mes = sum(float(a.get("margen_previsto") or 0) for a in propios
                             if a.get("periodo_id") == corte_id)
            margen_acum = sum(float(a.get("margen_previsto") or 0) for a in propios
                              if a.get("periodo_id") in hasta_corte)
        conceptos.append({"concepto": tipo, "mes": _r2(mes_v), "acum": _r2(acum_v),
                          "margen_previsto_mes": _r2(margen_mes),
                          "margen_previsto_acum": _r2(margen_acum)})
        venta_real_mes += mes_v
        venta_acum += acum_v

    # ── costo por recurso (directo / indirecto) mes y acumulado ──
    def _costo_rec(indirecta: bool, pers):
        out = {}
        for rec in RECURSOS:
            out[rec] = _r2(sum(v for (f, rr, pp), v in celda.items()
                               if rr == rec and pp in pers and _es_indirecta(f) == indirecta))
        return out

    costo_dir_mes = _costo_rec(False, {corte_id})
    costo_dir_acum = _costo_rec(False, hasta_corte)
    costo_ind_mes = _costo_rec(True, {corte_id})
    costo_ind_acum = _costo_rec(True, hasta_corte)
    costo_mes = sum(costo_dir_mes.values()) + sum(costo_ind_mes.values())
    costo_acum = sum(costo_dir_acum.values()) + sum(costo_ind_acum.values())

    # ── proyección / saldo de obra ──
    proy_meses = defaultdict(float)     # periodo futuro -> costo proyectado
    for pr in proyeccion or []:
        if pr["periodo_id"] not in hasta_corte:
            proy_meses[pr["periodo_id"]] += float(pr["monto"] or 0)
    saldo_obra = _r2(sum(proy_meses.values()))
    costo_total_proy = costo_acum + saldo_obra
    venta_total_proy = venta_acum + float(venta_proyectada_extra or 0)

    # ── costo aplicado / resultado pendiente / margen ──
    factor = (costo_total_proy / venta_total_proy) if venta_total_proy else 0.0
    costo_aplicado_acum = _r2(venta_acum * factor)
    resultado_pendiente = _r2(costo_acum - costo_aplicado_acum)
    margen_econ_acum = _r2(venta_acum - costo_aplicado_acum)
    margen_total = _r2(venta_total_proy - costo_total_proy)
    pct_margen = round(margen_total / venta_total_proy, 4) if venta_total_proy else 0
    margen_con_cont = _r2(margen_total - float(contingencia or 0))
    pct_con_cont = round(margen_con_cont / venta_total_proy, 4) if venta_total_proy else 0

    # ── USD: mensual con el tc de cada mes; acumulado = Σ mensual convertido ──
    def _usd_acum_costo():
        tot = 0.0
        for (f, rec, per), v in celda.items():
            if per in hasta_corte:
                tot += v / tc[per]
        return _r2(tot)

    def _usd_acum_venta():
        tot = 0.0
        for per in hasta_corte:
            mes_v = sum((venta_fase_mes.get(per, {}) or {}).values()) \
                + sum(float(a["monto"] or 0) for a in ajustes if a.get("periodo_id") == per)
            tot += mes_v / tc[per]
        return _r2(tot)

    usd = {"venta_acum": _usd_acum_venta(), "costo_acum": _usd_acum_costo(),
           "tc_corte": tc[corte_id]}
    usd["margen_acum"] = _r2(usd["venta_acum"] - usd["costo_acum"])

    return {
        "corte": {"periodo_id": corte_id, "anio": per_info[corte_id]["anio"],
                  "mes": per_info[corte_id]["mes"], "tipo_cambio": tc[corte_id]},
        "r_fases": r_fases + [parcial_dir, parcial_ind, total_obra],
        "t_obra": {
            "venta": conceptos,
            "venta_total": {"mes": _r2(venta_real_mes), "acum": _r2(venta_acum),
                            "proyectada": _r2(venta_total_proy)},
            "costo_directo": {"mes": costo_dir_mes, "acum": costo_dir_acum},
            "costo_indirecto": {"mes": costo_ind_mes, "acum": costo_ind_acum},
            "costo_total": {"mes": _r2(costo_mes), "acum": _r2(costo_acum),
                            "proyectado": _r2(costo_total_proy)},
            "proyeccion_meses": {str(k): _r2(v) for k, v in sorted(proy_meses.items())},
            "saldo_obra": saldo_obra,
            "prev": prev,
        },
        "totales": {
            "venta_acum": _r2(venta_acum), "costo_acum": _r2(costo_acum),
            "costo_aplicado_acum": costo_aplicado_acum,
            "resultado_pendiente": resultado_pendiente,
            "margen_economico_acum": margen_econ_acum,
            "margen_total": margen_total, "pct_margen": pct_margen,
            "contingencia": _r2(contingencia),
            "margen_con_contingencia": margen_con_cont,
            "pct_margen_con_contingencia": pct_con_cont,
            "meta_total": total_obra["meta"], "contractual_total": total_obra["contractual"],
        },
        "usd": usd,
    }
