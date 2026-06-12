# ============================================================
# routers/valor_ganado.py
# Módulo Valor Ganado - lógica del ISP Fluor digitalizada
#
# Integración en main.py (2 líneas):
#   from routers.valor_ganado import router as ev_router
#   app.include_router(ev_router)
#
# Usa su propio pool asyncpg leyendo DATABASE_URL del entorno.
# Si prefieres reutilizar el pool de main.py, reemplaza db() abajo.
# ============================================================
import os
from collections import defaultdict
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/ev", tags=["valor-ganado"])

_pool: Optional[asyncpg.Pool] = None


async def db() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"], min_size=1, max_size=5
        )
    return _pool


# ---------------------- Modelos ----------------------
class HitoIn(BaseModel):
    numero: int = Field(ge=1, le=10)
    descripcion: str = ""
    peso: float = Field(gt=0, le=1)
    es_principal: bool = False


class PartidaIn(BaseModel):
    codigo: str
    fase: str
    sub_fase: Optional[str] = None
    descripcion: str
    unidad: str
    sistema: Optional[str] = None
    metrado_presup: float = 0
    metrado_proyec: Optional[float] = None
    hh_presup: float = 0
    hitos: list[HitoIn]


class AvanceIn(BaseModel):
    hito_id: int
    cantidad_acum: float = Field(ge=0)


class HHIn(BaseModel):
    partida_id: int
    hh: float = Field(ge=0)


class CapturaIn(BaseModel):
    semana: int
    avances: list[AvanceIn] = []
    hh_gastadas: list[HHIn] = []


def _validar_pesos(hitos: list[HitoIn]):
    total = round(sum(h.peso for h in hitos), 4)
    if abs(total - 1.0) > 0.0001:
        raise HTTPException(400, f"Los pesos de los hitos deben sumar 1.00 (suman {total})")
    if sum(1 for h in hitos if h.es_principal) != 1:
        raise HTTPException(400, "Debe haber exactamente un hito principal")
    numeros = [h.numero for h in hitos]
    if len(numeros) != len(set(numeros)):
        raise HTTPException(400, "Números de hito repetidos")


# ---------------------- CRUD Partidas ----------------------
@router.get("/partidas")
async def listar_partidas():
    pool = await db()
    async with pool.acquire() as con:
        partidas = await con.fetch(
            "SELECT * FROM ev_partidas WHERE activo ORDER BY codigo"
        )
        hitos = await con.fetch(
            "SELECT * FROM ev_hitos ORDER BY partida_id, numero"
        )
    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(dict(h))
    return [
        {**dict(p), "hitos": por_partida.get(p["id"], [])} for p in partidas
    ]


@router.post("/partidas")
async def crear_partida(body: PartidaIn):
    _validar_pesos(body.hitos)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            try:
                pid = await con.fetchval(
                    """INSERT INTO ev_partidas
                       (codigo, fase, sub_fase, descripcion, unidad, sistema,
                        metrado_presup, metrado_proyec, hh_presup)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                    body.codigo, body.fase, body.sub_fase, body.descripcion,
                    body.unidad, body.sistema, body.metrado_presup,
                    body.metrado_proyec, body.hh_presup,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, f"Ya existe una partida con código {body.codigo}")
            for h in body.hitos:
                await con.execute(
                    """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
                       VALUES ($1,$2,$3,$4,$5)""",
                    pid, h.numero, h.descripcion, h.peso, h.es_principal,
                )
    return {"id": pid, "ok": True}


@router.put("/partidas/{partida_id}")
async def actualizar_partida(partida_id: int, body: PartidaIn):
    _validar_pesos(body.hitos)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            res = await con.execute(
                """UPDATE ev_partidas SET codigo=$2, fase=$3, sub_fase=$4,
                   descripcion=$5, unidad=$6, sistema=$7, metrado_presup=$8,
                   metrado_proyec=$9, hh_presup=$10 WHERE id=$1""",
                partida_id, body.codigo, body.fase, body.sub_fase,
                body.descripcion, body.unidad, body.sistema,
                body.metrado_presup, body.metrado_proyec, body.hh_presup,
            )
            if res == "UPDATE 0":
                raise HTTPException(404, "Partida no encontrada")
            # Reemplazo del set de hitos preservando avances cuando el número coincide
            existentes = await con.fetch(
                "SELECT id, numero FROM ev_hitos WHERE partida_id=$1", partida_id
            )
            por_numero = {e["numero"]: e["id"] for e in existentes}
            nuevos = {h.numero for h in body.hitos}
            for e in existentes:
                if e["numero"] not in nuevos:
                    await con.execute("DELETE FROM ev_hitos WHERE id=$1", e["id"])
            for h in body.hitos:
                if h.numero in por_numero:
                    await con.execute(
                        """UPDATE ev_hitos SET descripcion=$2, peso=$3, es_principal=$4
                           WHERE id=$1""",
                        por_numero[h.numero], h.descripcion, h.peso, h.es_principal,
                    )
                else:
                    await con.execute(
                        """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
                           VALUES ($1,$2,$3,$4,$5)""",
                        partida_id, h.numero, h.descripcion, h.peso, h.es_principal,
                    )
    return {"ok": True}


@router.delete("/partidas/{partida_id}")
async def eliminar_partida(partida_id: int):
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE ev_partidas SET activo=FALSE WHERE id=$1", partida_id
        )
    return {"ok": True}


# ---------------------- Captura semanal ----------------------
@router.get("/semanas")
async def semanas():
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT DISTINCT semana FROM (
                 SELECT semana FROM ev_avances
                 UNION SELECT semana FROM ev_hh_gastadas
               ) s ORDER BY semana"""
        )
    return [r["semana"] for r in rows]


@router.get("/captura")
async def captura(semana: int):
    """Estructura para el formulario de registro: por partida, cada hito con su
    acumulado de la semana anterior (carry-forward) y el de la semana actual."""
    pool = await db()
    async with pool.acquire() as con:
        partidas = await con.fetch(
            "SELECT * FROM ev_partidas WHERE activo ORDER BY codigo"
        )
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
        avances = await con.fetch(
            """SELECT hito_id, semana, cantidad_acum FROM ev_avances
               WHERE semana <= $1 ORDER BY hito_id, semana""", semana
        )
        hh = await con.fetch(
            """SELECT partida_id, semana, hh FROM ev_hh_gastadas
               WHERE semana <= $1 ORDER BY partida_id, semana""", semana
        )

    ult_av, av_actual = {}, {}
    for a in avances:
        if a["semana"] == semana:
            av_actual[a["hito_id"]] = float(a["cantidad_acum"])
        else:
            ult_av[a["hito_id"]] = float(a["cantidad_acum"])  # queda el de mayor semana < actual

    hh_actual = {}
    for r in hh:
        if r["semana"] == semana:
            hh_actual[r["partida_id"]] = float(r["hh"])

    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(h)

    out = []
    for p in partidas:
        out.append({
            "partida_id": p["id"],
            "codigo": p["codigo"],
            "descripcion": p["descripcion"],
            "unidad": p["unidad"],
            "metrado_proyec": float(p["metrado_proyec"] or p["metrado_presup"]),
            "hh_semana": hh_actual.get(p["id"], 0.0),
            "hitos": [
                {
                    "hito_id": h["id"],
                    "numero": h["numero"],
                    "descripcion": h["descripcion"],
                    "peso": float(h["peso"]),
                    "es_principal": h["es_principal"],
                    "cant_anterior": ult_av.get(h["id"], 0.0),
                    "cant_actual": av_actual.get(h["id"], ult_av.get(h["id"], 0.0)),
                }
                for h in por_partida.get(p["id"], [])
            ],
        })
    return out


@router.post("/captura")
async def guardar_captura(body: CapturaIn):
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for a in body.avances:
                await con.execute(
                    """INSERT INTO ev_avances (hito_id, semana, cantidad_acum)
                       VALUES ($1,$2,$3)
                       ON CONFLICT (hito_id, semana)
                       DO UPDATE SET cantidad_acum=$3, registrado_en=now()""",
                    a.hito_id, body.semana, a.cantidad_acum,
                )
            for r in body.hh_gastadas:
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1,$2,$3,'manual')
                       ON CONFLICT (partida_id, semana)
                       DO UPDATE SET hh=$3""",
                    r.partida_id, body.semana, r.hh,
                )
    return {"ok": True}


# ---------------------- Motor de cálculo ----------------------
def _acum_a_semana(avances, semana: int) -> dict:
    """cantidad acumulada por hito con carry-forward hasta `semana`."""
    acum = {}
    for a in avances:  # vienen ordenados por semana ascendente
        if a["semana"] <= semana:
            acum[a["hito_id"]] = float(a["cantidad_acum"])
    return acum


def _calcular(partidas, hitos, avances, hh_rows, semana: int):
    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(h)

    acum_s = _acum_a_semana(avances, semana)
    acum_prev = _acum_a_semana(avances, semana - 1)

    hh_acum, hh_sem = defaultdict(float), defaultdict(float)
    for r in hh_rows:
        if r["semana"] <= semana:
            hh_acum[r["partida_id"]] += float(r["hh"])
        if r["semana"] == semana:
            hh_sem[r["partida_id"]] += float(r["hh"])

    filas = []
    for p in partidas:
        pid = p["id"]
        mp = float(p["metrado_proyec"] or p["metrado_presup"])
        m_presup = float(p["metrado_presup"])
        hh_presup = float(p["hh_presup"])
        prod_presup = (hh_presup / m_presup) if m_presup > 0 else 0.0
        hh_proyec = mp * prod_presup  # T = N x L del ISP

        pct, pct_prev, cant_inst = 0.0, 0.0, 0.0
        for h in por_partida.get(pid, []):
            avance_h = (acum_s.get(h["id"], 0.0) / mp) if mp > 0 else 0.0
            avance_h_prev = (acum_prev.get(h["id"], 0.0) / mp) if mp > 0 else 0.0
            pct += float(h["peso"]) * min(avance_h, 1.0)
            pct_prev += float(h["peso"]) * min(avance_h_prev, 1.0)
            if h["es_principal"]:
                cant_inst = acum_s.get(h["id"], 0.0)

        ganadas_acum = pct * hh_proyec            # Y = Z x T del ISP
        ganadas_sem = ganadas_acum - (pct_prev * hh_proyec)
        gastadas_acum = hh_acum.get(pid, 0.0)
        gastadas_sem = hh_sem.get(pid, 0.0)

        pf_acum = (ganadas_acum / gastadas_acum) if gastadas_acum > 0 else 0.0
        pf_sem = (ganadas_sem / gastadas_sem) if gastadas_sem > 0 else 0.0
        prod_real = (gastadas_acum / cant_inst) if cant_inst > 0 else 0.0
        # EAC del ISP: HH proyectadas al cierre = prod real x saldo + gastadas
        saldo_met = max(mp - cant_inst, 0.0)
        eac_hh = (prod_real * saldo_met + gastadas_acum) if cant_inst > 0 else hh_proyec

        filas.append({
            "partida_id": pid,
            "codigo": p["codigo"],
            "fase": p["fase"],
            "sistema": p["sistema"],
            "descripcion": p["descripcion"],
            "unidad": p["unidad"],
            "metrado_proyec": round(mp, 2),
            "cantidad_instalada": round(cant_inst, 2),
            "pct_avance": round(pct, 4),
            "hh_presup": round(hh_presup, 2),
            "hh_proyec": round(hh_proyec, 2),
            "hh_ganadas_sem": round(ganadas_sem, 2),
            "hh_ganadas_acum": round(ganadas_acum, 2),
            "hh_gastadas_sem": round(gastadas_sem, 2),
            "hh_gastadas_acum": round(gastadas_acum, 2),
            "pf_sem": round(pf_sem, 3),
            "pf_acum": round(pf_acum, 3),
            "prod_presup": round(prod_presup, 4),
            "prod_real": round(prod_real, 4),
            "eac_hh": round(eac_hh, 2),
            "desvio_hh": round(eac_hh - hh_proyec, 2),
        })
    return filas


def _agrupar(filas, clave):
    grupos = defaultdict(lambda: {"hh_proyec": 0.0, "ganadas": 0.0, "gastadas": 0.0, "eac": 0.0})
    for f in filas:
        k = f[clave] or "SIN ASIGNAR"
        g = grupos[k]
        g["hh_proyec"] += f["hh_proyec"]
        g["ganadas"] += f["hh_ganadas_acum"]
        g["gastadas"] += f["hh_gastadas_acum"]
        g["eac"] += f["eac_hh"]
    out = []
    for k, g in sorted(grupos.items()):
        out.append({
            "grupo": k,
            "hh_proyec": round(g["hh_proyec"], 2),
            "hh_ganadas": round(g["ganadas"], 2),
            "hh_gastadas": round(g["gastadas"], 2),
            "pct_avance": round(g["ganadas"] / g["hh_proyec"], 4) if g["hh_proyec"] > 0 else 0,
            "pf": round(g["ganadas"] / g["gastadas"], 3) if g["gastadas"] > 0 else 0,
            "eac_hh": round(g["eac"], 2),
        })
    return out


async def _datos_base(semana: int):
    pool = await db()
    async with pool.acquire() as con:
        partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
        avances = await con.fetch(
            "SELECT hito_id, semana, cantidad_acum FROM ev_avances WHERE semana <= $1 ORDER BY semana",
            semana,
        )
        hh = await con.fetch(
            "SELECT partida_id, semana, hh FROM ev_hh_gastadas WHERE semana <= $1", semana
        )
    return partidas, hitos, avances, hh


@router.get("/reporte")
async def reporte(semana: int):
    partidas, hitos, avances, hh = await _datos_base(semana)
    filas = _calcular(partidas, hitos, avances, hh, semana)

    tot_proyec = sum(f["hh_proyec"] for f in filas)
    tot_ganadas = sum(f["hh_ganadas_acum"] for f in filas)
    tot_gastadas = sum(f["hh_gastadas_acum"] for f in filas)
    tot_gan_sem = sum(f["hh_ganadas_sem"] for f in filas)
    tot_gas_sem = sum(f["hh_gastadas_sem"] for f in filas)
    tot_eac = sum(f["eac_hh"] for f in filas)

    return {
        "semana": semana,
        "totales": {
            "hh_proyec": round(tot_proyec, 2),
            "hh_ganadas_acum": round(tot_ganadas, 2),
            "hh_gastadas_acum": round(tot_gastadas, 2),
            "hh_ganadas_sem": round(tot_gan_sem, 2),
            "hh_gastadas_sem": round(tot_gas_sem, 2),
            "pct_avance": round(tot_ganadas / tot_proyec, 4) if tot_proyec > 0 else 0,
            "pf_acum": round(tot_ganadas / tot_gastadas, 3) if tot_gastadas > 0 else 0,
            "pf_sem": round(tot_gan_sem / tot_gas_sem, 3) if tot_gas_sem > 0 else 0,
            "eac_hh": round(tot_eac, 2),
            "desvio_hh": round(tot_eac - tot_proyec, 2),
        },
        "por_fase": _agrupar(filas, "fase"),
        "por_sistema": _agrupar(filas, "sistema"),
        "partidas": filas,
    }


@router.get("/curva")
async def curva(hasta: int):
    """Serie semanal para la Curva S y la tendencia de PF (gráficos del ISP)."""
    partidas, hitos, avances, hh = await _datos_base(hasta)
    semanas_set = sorted(
        {a["semana"] for a in avances} | {r["semana"] for r in hh} | {hasta}
    )
    serie = []
    for s in semanas_set:
        filas = _calcular(partidas, hitos, avances, hh, s)
        g = sum(f["hh_ganadas_acum"] for f in filas)
        c = sum(f["hh_gastadas_acum"] for f in filas)
        gs = sum(f["hh_ganadas_sem"] for f in filas)
        cs = sum(f["hh_gastadas_sem"] for f in filas)
        serie.append({
            "semana": s,
            "hh_ganadas_acum": round(g, 2),
            "hh_gastadas_acum": round(c, 2),
            "pf_acum": round(g / c, 3) if c > 0 else None,
            "pf_sem": round(gs / cs, 3) if cs > 0 else None,
        })
    return serie
