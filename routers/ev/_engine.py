# ============================================================
# routers/ev/_engine.py — motor puro del Valor Ganado (F0.5b)
#
# SOLO funciones puras (sin BD, sin FastAPI salvo HTTPException para
# validación). Es el activo más valioso del sistema: NO tocar sin
# tests verdes (tests/test_valor_ganado.py las cubre).
# ============================================================
from collections import defaultdict

from fastapi import HTTPException

from routers.ev._datos import _get


def _validar_pesos(hitos: list):
    total = round(sum(h.peso for h in hitos), 4)
    if abs(total - 1.0) > 0.0001:
        raise HTTPException(400, f"Los pesos de los hitos deben sumar 1.00 (suman {total})")
    if sum(1 for h in hitos if h.es_principal) != 1:
        raise HTTPException(400, "Debe haber exactamente un hito principal")
    numeros = [h.numero for h in hitos]
    if len(numeros) != len(set(numeros)):
        raise HTTPException(400, "Números de hito repetidos")


def _acum_a_semana(avances, semana: int) -> dict:
    acum = {}
    for a in avances:
        if a["semana"] <= semana:
            acum[a["hito_id"]] = float(a["cantidad_acum"])
    return acum


def _calcular(partidas, hitos, avances, hh_rows, tareo, semana: int, split=None):
    """hh_rows: ev_hh_gastadas (manual/importado). tareo: {(pid,sem):hh} del QR.
    split: {(pid,sem):{'dir','tot'}} de tareo_partida por tipo de trabajador.
    HH gastadas totales = manual + tareo; las directas = total × fracción directa."""
    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(h)

    acum_s = _acum_a_semana(avances, semana)
    acum_prev = _acum_a_semana(avances, semana - 1)

    # Directo/Indirecto se define por PARTIDA (campo tipo_costo), igual que el ISP del
    # gerente: toda la HH de una partida INDIRECTA es indirecta; de una DIRECTA, directa.
    # (El parámetro `split` por tipo de trabajador queda obsoleto y se ignora.)

    hh_acum, hh_sem = defaultdict(float), defaultdict(float)
    # Claves (partida_id, semana) con entrada manual — evita doble-conteo con tareo auto
    manual_keys: set = set()
    for r in hh_rows:
        if r["semana"] <= semana:
            hh_acum[r["partida_id"]] += float(r["hh"])
            manual_keys.add((r["partida_id"], r["semana"]))
        if r["semana"] == semana:
            hh_sem[r["partida_id"]] += float(r["hh"])
    # Solo agregar tareo automático cuando NO existe entrada manual para esa partida/semana
    for (pid, s), v in tareo.items():
        if (pid, s) not in manual_keys:
            if s <= semana:
                hh_acum[pid] += v
            if s == semana:
                hh_sem[pid] += v

    filas = []
    for p in partidas:
        pid = p["id"]
        mp = float(p["metrado_proyec"] or p["metrado_presup"])
        m_presup = float(p["metrado_presup"])
        hh_presup = float(p["hh_presup"])
        prod_presup = (hh_presup / m_presup) if m_presup > 0 else 0.0
        hh_proyec = mp * prod_presup

        pct, pct_prev, cant_inst = 0.0, 0.0, 0.0
        for h in por_partida.get(pid, []):
            avance_h = (acum_s.get(h["id"], 0.0) / mp) if mp > 0 else 0.0
            avance_h_prev = (acum_prev.get(h["id"], 0.0) / mp) if mp > 0 else 0.0
            pct += float(h["peso"]) * min(avance_h, 1.0)
            pct_prev += float(h["peso"]) * min(avance_h_prev, 1.0)
            if h["es_principal"]:
                cant_inst = acum_s.get(h["id"], 0.0)

        ganadas_acum = pct * hh_proyec
        ganadas_sem = ganadas_acum - (pct_prev * hh_proyec)
        gastadas_acum = hh_acum.get(pid, 0.0)
        gastadas_sem = hh_sem.get(pid, 0.0)

        pf_acum = (ganadas_acum / gastadas_acum) if gastadas_acum > 0 else 0.0
        pf_sem = (ganadas_sem / gastadas_sem) if gastadas_sem > 0 else 0.0

        # Directa vs indirecta según el tipo_costo de la PARTIDA
        es_indirecto = str(_get(p, "tipo_costo", "DIRECTO")).upper() == "INDIRECTO"
        frac_acum = 0.0 if es_indirecto else 1.0
        frac_sem = frac_acum
        gastadas_dir_acum = gastadas_acum * frac_acum
        gastadas_ind_acum = gastadas_acum - gastadas_dir_acum
        gastadas_dir_sem = gastadas_sem * frac_sem
        pf_dir_acum = (ganadas_acum / gastadas_dir_acum) if gastadas_dir_acum > 0 else 0.0
        pf_dir_sem = (ganadas_sem / gastadas_dir_sem) if gastadas_dir_sem > 0 else 0.0

        prod_real = (gastadas_acum / cant_inst) if cant_inst > 0 else 0.0
        saldo_met = max(mp - cant_inst, 0.0)
        eac_hh = (prod_real * saldo_met + gastadas_acum) if cant_inst > 0 else hh_proyec

        filas.append({
            "partida_id": pid,
            "codigo": p["codigo"],
            "otm_id": p["otm_id"],
            "fase": p["fase"],
            "tipo_costo": "INDIRECTO" if es_indirecto else "DIRECTO",
            "naturaleza": str(_get(p, "naturaleza", "CONTRACTUAL")).upper(),
            "sistema": p["sistema"],
            "descripcion": p["descripcion"],
            "unidad": p["unidad"],
            "metrado_proyec": round(mp, 2),
            "cantidad_instalada": round(cant_inst, 2),
            "pct_avance": round(pct, 4),
            "hh_presup": round(hh_presup, 2),
            # #6: presupuesto actualizado (denominador del % avance del proyecto).
            # Si la partida no lo trae, cae a hh_presup.
            "hh_actualizado": round(float(_get(p, "hh_actualizado", None) or hh_presup), 2),
            "hh_proyec": round(hh_proyec, 2),
            "hh_ganadas_sem": round(ganadas_sem, 2),
            "hh_ganadas_acum": round(ganadas_acum, 2),
            "hh_gastadas_sem": round(gastadas_sem, 2),
            "hh_gastadas_acum": round(gastadas_acum, 2),
            "hh_gastadas_dir_acum": round(gastadas_dir_acum, 2),
            "hh_gastadas_ind_acum": round(gastadas_ind_acum, 2),
            "hh_gastadas_dir_sem": round(gastadas_dir_sem, 2),
            "pf_sem": round(pf_sem, 3),
            "pf_acum": round(pf_acum, 3),
            "pf_dir_acum": round(pf_dir_acum, 3),
            "pf_dir_sem": round(pf_dir_sem, 3),
            "prod_presup": round(prod_presup, 4),
            "prod_real": round(prod_real, 4),
            "eac_hh": round(eac_hh, 2),
            "desvio_hh": round(eac_hh - hh_proyec, 2),
        })
    return filas


def _agrupar(filas, clave):
    grupos = defaultdict(lambda: {"hh_presup": 0.0, "hh_proyec": 0.0,
                                  "ganadas": 0.0, "gastadas": 0.0, "eac": 0.0})
    for f in filas:
        k = f[clave] or "SIN ASIGNAR"
        g = grupos[k]
        g["hh_presup"] += f.get("hh_presup", 0.0)
        g["hh_proyec"] += f["hh_proyec"]
        g["ganadas"] += f["hh_ganadas_acum"]
        g["gastadas"] += f["hh_gastadas_acum"]
        g["eac"] += f["eac_hh"]
    out = []
    for k, g in sorted(grupos.items()):
        out.append({
            "grupo": k,
            "hh_presup": round(g["hh_presup"], 2),
            "hh_proyec": round(g["hh_proyec"], 2),
            "hh_ganadas": round(g["ganadas"], 2),
            "hh_gastadas": round(g["gastadas"], 2),
            "pct_avance": round(g["ganadas"] / g["hh_proyec"], 4) if g["hh_proyec"] > 0 else 0,
            "pf": round(g["ganadas"] / g["gastadas"], 3) if g["gastadas"] > 0 else 0,
            "eac_hh": round(g["eac"], 2),
        })
    return out


def _matriz_area_disciplina(hojas):
    """Replica la hoja 'Resumen Ejecutivo' del gerente: matriz Área (sistema) ×
    Disciplina (fase). Por celda: HH contractual/proyec/ganada/gastada, % avance
    (ganadas/proyec), PF (ganadas/gastadas) e incidencia (proyec/Σproyec).
    Subtotal por sistema y total general usan el mismo criterio (proyectada).
    """
    tot_proyec_global = sum(f["hh_proyec"] for f in hojas) or 0.0

    def _celda():
        return {"hh_contractual": 0.0, "hh_proyec": 0.0, "hh_ganadas": 0.0,
                "hh_gastadas": 0.0, "eac": 0.0}

    # areas[sistema][disciplina] = celda
    areas: dict = defaultdict(lambda: defaultdict(_celda))
    for f in hojas:
        area = f["sistema"] or "SIN ÁREA"
        disc = f["fase"] or "SIN DISCIPLINA"
        c = areas[area][disc]
        c["hh_contractual"] += f.get("hh_presup", 0.0)
        c["hh_proyec"] += f["hh_proyec"]
        c["hh_ganadas"] += f["hh_ganadas_acum"]
        c["hh_gastadas"] += f["hh_gastadas_acum"]
        c["eac"] += f["eac_hh"]

    def _fmt(grupo, c, denom_inc):
        return {
            **grupo,
            "hh_contractual": round(c["hh_contractual"], 2),
            "hh_proyec": round(c["hh_proyec"], 2),
            "hh_ganadas": round(c["hh_ganadas"], 2),
            "hh_gastadas": round(c["hh_gastadas"], 2),
            "eac_hh": round(c["eac"], 2),
            "pct_avance": round(c["hh_ganadas"] / c["hh_proyec"], 4) if c["hh_proyec"] > 0 else 0,
            "pf": round(c["hh_ganadas"] / c["hh_gastadas"], 3) if c["hh_gastadas"] > 0 else 0,
            "inc_proyec": round(c["hh_proyec"] / denom_inc, 4) if denom_inc > 0 else 0,
        }

    out_areas = []
    total = _celda()
    for area in sorted(areas.keys()):
        sub = _celda()
        disciplinas = []
        for disc in sorted(areas[area].keys()):
            c = areas[area][disc]
            disciplinas.append(_fmt({"disciplina": disc}, c, tot_proyec_global))
            for k in sub:
                sub[k] += c[k]
        for k in total:
            total[k] += sub[k]
        out_areas.append({
            "area": area,
            "disciplinas": disciplinas,
            "subtotal": _fmt({}, sub, tot_proyec_global),
        })

    return {"areas": out_areas, "total": _fmt({}, total, tot_proyec_global)}


def _calc_costo_mo(hh_por_cargo: dict, tarifas: dict, default=None):
    """Costo de Mano de Obra = Σ (HH del cargo × tarifa del cargo).
    Reglas:
      • Tarifa propia presente (incluido 0.0 explícito) → se respeta.
      • Cargo sin tarifa propia → usa el respaldo `default`.
      • Ni tarifa propia ni respaldo (ambos None) → esas HH NO se cuentan
        como 0 en silencio: se acumulan en hh_sin_tarifa para poder avisar.
    Convención: `None` = "no configurado"; `0.0` = "configurado en cero"
    (p.ej. cargo subcontratado / no facturable).
    Función pura → testeable. Devuelve (costo, hh_sin_tarifa)."""
    costo = 0.0
    hh_sin = 0.0
    for cargo, hh in hh_por_cargo.items():
        rate = tarifas.get(cargo)        # None ⇒ cargo sin tarifa configurada
        if rate is None:
            rate = default               # respaldo (puede ser None)
        if rate is None:                 # ni propia ni respaldo
            hh_sin += hh
            rate = 0.0
        costo += hh * rate
    return round(costo, 2), round(hh_sin, 2)


def _totales(hojas, improd_acum: float = 0.0):
    """Totales del proyecto (función pura, sobre las hojas del WBS).
    #6: % avance del proyecto = ganadas / presupuesto ACTUALIZADO (no proyectada).
    #5: las HH improductivas entran al consumo total y bajan el PF del proyecto.
    """
    tot_proyec = sum(f["hh_proyec"] for f in hojas)
    tot_actualizado = sum(f["hh_actualizado"] for f in hojas)
    tot_presup = sum(f.get("hh_presup", 0.0) for f in hojas)
    tot_ganadas = sum(f["hh_ganadas_acum"] for f in hojas)
    tot_gastadas = sum(f["hh_gastadas_acum"] for f in hojas)
    tot_gas_dir = sum(f["hh_gastadas_dir_acum"] for f in hojas)
    tot_gas_ind = sum(f["hh_gastadas_ind_acum"] for f in hojas)
    tot_gan_sem = sum(f["hh_ganadas_sem"] for f in hojas)
    tot_gas_sem = sum(f["hh_gastadas_sem"] for f in hojas)
    tot_eac = sum(f["eac_hh"] for f in hojas)
    tot_consumidas = tot_gastadas + improd_acum
    tot_consumidas_dir = tot_gas_dir + improd_acum
    return {
        "hh_proyec": round(tot_proyec, 2),
        "hh_presup": round(tot_presup, 2),
        "hh_actualizado": round(tot_actualizado, 2),
        "hh_ganadas_acum": round(tot_ganadas, 2),
        "hh_gastadas_acum": round(tot_gastadas, 2),
        "hh_ganadas_sem": round(tot_gan_sem, 2),
        "hh_gastadas_sem": round(tot_gas_sem, 2),
        "hh_gastadas_dir_acum": round(tot_gas_dir, 2),
        "hh_gastadas_ind_acum": round(tot_gas_ind, 2),
        # #5: improductivas y consumo total
        "hh_improductivas_acum": round(improd_acum, 2),
        "hh_consumidas_acum": round(tot_consumidas, 2),
        "index_improductividad": round(improd_acum / tot_consumidas, 4) if tot_consumidas > 0 else 0,
        # #6: % avance del proyecto = ganadas / presupuesto ACTUALIZADO (método del gerente)
        "pct_avance": round(tot_ganadas / tot_actualizado, 4) if tot_actualizado > 0 else 0,
        "pct_avance_proyec": round(tot_ganadas / tot_proyec, 4) if tot_proyec > 0 else 0,
        # PF del proyecto: titular incluye improductivas + variante productiva (referencia)
        "pf_acum": round(tot_ganadas / tot_consumidas, 3) if tot_consumidas > 0 else 0,
        "pf_acum_productivo": round(tot_ganadas / tot_gastadas, 3) if tot_gastadas > 0 else 0,
        "pf_dir_acum": round(tot_ganadas / tot_consumidas_dir, 3) if tot_consumidas_dir > 0 else 0,
        "pf_sem": round(tot_gan_sem / tot_gas_sem, 3) if tot_gas_sem > 0 else 0,
        "eac_hh": round(tot_eac, 2),
        "desvio_hh": round(tot_eac - tot_proyec, 2),
    }
