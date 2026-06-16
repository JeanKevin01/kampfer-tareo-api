# ============================================================
# routers/valor_ganado.py  —  v2
# Módulo Valor Ganado - lógica ISP Fluor digitalizada
#
# Novedades v2:
#   - OTM por encima de fase/sub-fase (ev_partidas.otm_id)
#   - POST /ev/importar: carga masiva de partidas (desde cero o con
#     histórico de avances y HH) en una sola transacción
#   - GET/POST /ev/plantillas: catálogo de rules of credit por tipo
#     de actividad
#   - HH automáticas desde el tareo QR (vista ev_hh_tareo) sumadas a
#     las HH manuales; mapeo fecha->semana vía ev_config.fecha_base
#   - POST /ev/asignar-hh: etiquetar registros del tareo con partida
#   - GET /ev/curva-fase: tendencia de PF por disciplina
#
# Integración en main.py (sin cambios respecto a v1):
#   from routers.valor_ganado import router as ev_router
#   app.include_router(ev_router)   # después de crear app
# ============================================================
import os
import json
from collections import defaultdict
from datetime import date, timedelta
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
    otm_id: Optional[str] = None
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


class PlantillaIn(BaseModel):
    tipo_actividad: str
    hitos: list[HitoIn]


class ImpPartida(BaseModel):
    codigo: str
    otm_id: Optional[str] = None
    fase: str
    sub_fase: Optional[str] = None
    descripcion: str
    unidad: str
    sistema: Optional[str] = None
    metrado_presup: float = 0
    metrado_proyec: Optional[float] = None
    hh_presup: float = 0
    tipo_actividad: Optional[str] = None      # busca hitos en el catálogo
    hitos: Optional[list[HitoIn]] = None      # o hitos explícitos


class ImpAvance(BaseModel):
    codigo: str
    semana: int
    hito: int = Field(ge=1, le=10)
    cantidad_acum: float = Field(ge=0)


class ImpHH(BaseModel):
    codigo: str
    semana: int
    hh: float = Field(ge=0)


class ImportarIn(BaseModel):
    partidas: list[ImpPartida]
    avances: list[ImpAvance] = []
    hh: list[ImpHH] = []


class AsignarHHIn(BaseModel):
    otm_id: str
    fecha: date
    partida_id: int


def _validar_pesos(hitos: list[HitoIn]):
    total = round(sum(h.peso for h in hitos), 4)
    if abs(total - 1.0) > 0.0001:
        raise HTTPException(400, f"Los pesos de los hitos deben sumar 1.00 (suman {total})")
    if sum(1 for h in hitos if h.es_principal) != 1:
        raise HTTPException(400, "Debe haber exactamente un hito principal")
    numeros = [h.numero for h in hitos]
    if len(numeros) != len(set(numeros)):
        raise HTTPException(400, "Números de hito repetidos")


# ---------------------- Config (fecha base) ----------------------
async def _fecha_base(con) -> Optional[date]:
    v = await con.fetchval("SELECT valor FROM ev_config WHERE clave='fecha_base'")
    if v:
        return date.fromisoformat(v)
    # Auto: lunes de la semana del primer registro de tareo con HH
    f = await con.fetchval(
        "SELECT MIN(fecha) FROM registros WHERE hh IS NOT NULL AND hh > 0"
    )
    if f:
        return f - timedelta(days=f.weekday())
    return None


def _semana_de(fecha: date, base: date) -> int:
    return (fecha - base).days // 7 + 1


@router.get("/config")
async def get_config():
    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
    return {"fecha_base": base.isoformat() if base else None}


@router.put("/config")
async def put_config(body: dict):
    fb = body.get("fecha_base")
    if not fb:
        raise HTTPException(400, "fecha_base requerida (YYYY-MM-DD)")
    date.fromisoformat(fb)  # valida formato
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO ev_config (clave, valor) VALUES ('fecha_base', $1)
               ON CONFLICT (clave) DO UPDATE SET valor=$1""", fb
        )
    return {"ok": True, "fecha_base": fb}


# ---------------------- Plantillas de hitos ----------------------
@router.get("/plantillas")
async def listar_plantillas():
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT * FROM ev_plantillas_hitos ORDER BY tipo_actividad")
    return [
        {"tipo_actividad": r["tipo_actividad"], "hitos": json.loads(r["hitos"])}
        for r in rows
    ]


@router.post("/plantillas")
async def guardar_plantilla(body: PlantillaIn):
    _validar_pesos(body.hitos)
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO ev_plantillas_hitos (tipo_actividad, hitos) VALUES ($1, $2)
               ON CONFLICT (tipo_actividad) DO UPDATE SET hitos=$2""",
            body.tipo_actividad.strip().upper(),
            json.dumps([h.model_dump() for h in body.hitos]),
        )
    return {"ok": True}


# ---------------------- CRUD Partidas ----------------------
@router.get("/partidas")
async def listar_partidas(otm: Optional[str] = None):
    pool = await db()
    async with pool.acquire() as con:
        if otm:
            partidas = await con.fetch(
                "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
            )
        else:
            partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(dict(h))
    return [{**dict(p), "hitos": por_partida.get(p["id"], [])} for p in partidas]


@router.get("/otms")
async def listar_otms_ev():
    """OTMs que tienen partidas en el módulo EV."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT COALESCE(otm_id,'SIN OTM') AS otm_id, COUNT(*) AS partidas
               FROM ev_partidas WHERE activo GROUP BY otm_id ORDER BY otm_id"""
        )
    return [dict(r) for r in rows]


@router.post("/partidas")
async def crear_partida(body: PartidaIn):
    _validar_pesos(body.hitos)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            try:
                pid = await con.fetchval(
                    """INSERT INTO ev_partidas
                       (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                        metrado_presup, metrado_proyec, hh_presup)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                    body.codigo, body.otm_id, body.fase, body.sub_fase, body.descripcion,
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
                """UPDATE ev_partidas SET codigo=$2, otm_id=$3, fase=$4, sub_fase=$5,
                   descripcion=$6, unidad=$7, sistema=$8, metrado_presup=$9,
                   metrado_proyec=$10, hh_presup=$11 WHERE id=$1""",
                partida_id, body.codigo, body.otm_id, body.fase, body.sub_fase,
                body.descripcion, body.unidad, body.sistema,
                body.metrado_presup, body.metrado_proyec, body.hh_presup,
            )
            if res == "UPDATE 0":
                raise HTTPException(404, "Partida no encontrada")
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
                        "UPDATE ev_hitos SET descripcion=$2, peso=$3, es_principal=$4 WHERE id=$1",
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
        await con.execute("UPDATE ev_partidas SET activo=FALSE WHERE id=$1", partida_id)
    return {"ok": True}


# ---------------------- Importador masivo ----------------------
@router.post("/importar")
async def importar(body: ImportarIn):
    """Carga masiva en UNA transacción: partidas (upsert por código) +
    histórico opcional de avances y HH. Si una fila falla, nada se guarda."""
    pool = await db()
    creadas, actualizadas = 0, 0
    errores: list[str] = []

    async with pool.acquire() as con:
        pl_rows = await con.fetch("SELECT * FROM ev_plantillas_hitos")
        plantillas = {r["tipo_actividad"]: json.loads(r["hitos"]) for r in pl_rows}

        async with con.transaction():
            codigo_a_id: dict[str, int] = {}

            for i, p in enumerate(body.partidas, start=1):
                # Resolver hitos: explícitos > plantilla > GENERICO
                if p.hitos:
                    hitos_raw = [h.model_dump() for h in p.hitos]
                elif p.tipo_actividad:
                    hitos_raw = plantillas.get(p.tipo_actividad.strip().upper())
                    if hitos_raw is None:
                        errores.append(
                            f"Fila {i} ({p.codigo}): tipo_actividad '{p.tipo_actividad}' no existe en el catálogo"
                        )
                        continue
                else:
                    hitos_raw = plantillas.get("GENERICO", [
                        {"numero": 1, "descripcion": "Ejecución", "peso": 1.0, "es_principal": True}
                    ])
                try:
                    hitos = [HitoIn(**h) for h in hitos_raw]
                    _validar_pesos(hitos)
                except HTTPException as e:
                    errores.append(f"Fila {i} ({p.codigo}): {e.detail}")
                    continue
                except Exception as e:
                    errores.append(f"Fila {i} ({p.codigo}): hitos inválidos ({e})")
                    continue

                existente = await con.fetchval(
                    "SELECT id FROM ev_partidas WHERE codigo=$1", p.codigo
                )
                if existente:
                    await con.execute(
                        """UPDATE ev_partidas SET otm_id=$2, fase=$3, sub_fase=$4, descripcion=$5,
                           unidad=$6, sistema=$7, metrado_presup=$8, metrado_proyec=$9,
                           hh_presup=$10, activo=TRUE WHERE id=$1""",
                        existente, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                    )
                    await con.execute("DELETE FROM ev_hitos WHERE partida_id=$1", existente)
                    pid = existente
                    actualizadas += 1
                else:
                    pid = await con.fetchval(
                        """INSERT INTO ev_partidas
                           (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                            metrado_presup, metrado_proyec, hh_presup)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                        p.codigo, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                    )
                    creadas += 1
                codigo_a_id[p.codigo] = pid
                for h in hitos:
                    await con.execute(
                        """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
                           VALUES ($1,$2,$3,$4,$5)""",
                        pid, h.numero, h.descripcion, h.peso, h.es_principal,
                    )

            if errores:
                raise HTTPException(400, {"errores": errores})

            # mapa hito (partida, numero) -> id para el histórico
            hitos_db = await con.fetch("SELECT id, partida_id, numero FROM ev_hitos")
            hito_id = {(h["partida_id"], h["numero"]): h["id"] for h in hitos_db}

            av_ins, hist_err = 0, []
            for a in body.avances:
                pid = codigo_a_id.get(a.codigo) or await con.fetchval(
                    "SELECT id FROM ev_partidas WHERE codigo=$1", a.codigo
                )
                if not pid:
                    hist_err.append(f"Avance: código {a.codigo} no existe")
                    continue
                hid = hito_id.get((pid, a.hito))
                if not hid:
                    hist_err.append(f"Avance: {a.codigo} no tiene hito {a.hito}")
                    continue
                await con.execute(
                    """INSERT INTO ev_avances (hito_id, semana, cantidad_acum)
                       VALUES ($1,$2,$3)
                       ON CONFLICT (hito_id, semana) DO UPDATE SET cantidad_acum=$3""",
                    hid, a.semana, a.cantidad_acum,
                )
                av_ins += 1

            hh_ins = 0
            for r in body.hh:
                pid = codigo_a_id.get(r.codigo) or await con.fetchval(
                    "SELECT id FROM ev_partidas WHERE codigo=$1", r.codigo
                )
                if not pid:
                    hist_err.append(f"HH: código {r.codigo} no existe")
                    continue
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1,$2,$3,'importado')
                       ON CONFLICT (partida_id, semana) DO UPDATE SET hh=$3""",
                    pid, r.semana, r.hh,
                )
                hh_ins += 1

            if hist_err:
                raise HTTPException(400, {"errores": hist_err})

    return {
        "ok": True,
        "partidas_creadas": creadas,
        "partidas_actualizadas": actualizadas,
        "avances_importados": av_ins,
        "hh_importadas": hh_ins,
    }


# ---------------------- Tareo QR → partida ----------------------
@router.get("/hh-sin-asignar")
async def hh_sin_asignar(desde: Optional[date] = None):
    """Días × OTM con HH del tareo aún sin partida asignada."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT otm_id, fecha, SUM(hh) AS hh, COUNT(*) AS registros
               FROM registros
               WHERE partida_id IS NULL AND hh IS NOT NULL
                 AND ($1::date IS NULL OR fecha >= $1)
               GROUP BY otm_id, fecha ORDER BY fecha DESC, otm_id""",
            desde,
        )
    return [
        {"otm_id": r["otm_id"], "fecha": r["fecha"].isoformat(),
         "hh": float(r["hh"]), "registros": r["registros"]}
        for r in rows
    ]


@router.post("/asignar-hh")
async def asignar_hh(body: AsignarHHIn):
    """Etiqueta los registros del tareo de una OTM en una fecha con la partida trabajada."""
    pool = await db()
    async with pool.acquire() as con:
        ok = await con.fetchval(
            "SELECT id FROM ev_partidas WHERE id=$1 AND activo", body.partida_id
        )
        if not ok:
            raise HTTPException(404, "Partida no encontrada")
        res = await con.execute(
            "UPDATE registros SET partida_id=$1 WHERE otm_id=$2 AND fecha=$3",
            body.partida_id, body.otm_id, body.fecha,
        )
    return {"ok": True, "registros_actualizados": int(res.split()[-1])}


# ---------------------- Captura semanal ----------------------
@router.get("/semanas")
async def semanas():
    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
        rows = await con.fetch(
            """SELECT DISTINCT semana FROM (
                 SELECT semana FROM ev_avances
                 UNION SELECT semana FROM ev_hh_gastadas
               ) s"""
        )
        sem = {r["semana"] for r in rows}
        if base:
            tareo = await con.fetch("SELECT DISTINCT fecha FROM ev_hh_tareo")
            for t in tareo:
                sem.add(_semana_de(t["fecha"], base))
    return sorted(sem)


async def _hh_tareo_por_semana(con) -> dict:
    """{(partida_id, semana): hh} — auto-distribuido desde registros por OTM.
    Distribuye las HH de cada OTM proporcionalmente al presupuesto de cada partida.
    Si no hay partidas para un OTM, sus HH no se asignan (no cuentan en EV).
    """
    base = await _fecha_base(con)
    out: dict = defaultdict(float)
    if not base:
        return out

    # HH registradas por OTM por día
    rows_reg = await con.fetch("""
        SELECT otm_id, fecha, SUM(hh) AS hh_total
        FROM registros
        WHERE hh IS NOT NULL AND hh > 0
        GROUP BY otm_id, fecha
    """)

    # Peso de cada partida activa dentro de su OTM (proporcional a hh_presup)
    rows_peso = await con.fetch("""
        SELECT id AS partida_id, otm_id,
               hh_presup::float /
               NULLIF(SUM(hh_presup) OVER (PARTITION BY otm_id), 0.0) AS peso
        FROM ev_partidas
        WHERE activo = true AND hh_presup > 0
    """)

    otm_pesos: dict = defaultdict(list)
    for p in rows_peso:
        otm_pesos[p['otm_id']].append((p['partida_id'], float(p['peso'] or 0)))

    for r in rows_reg:
        hh      = float(r['hh_total'])
        semana  = _semana_de(r['fecha'], base)
        for pid, peso in otm_pesos.get(r['otm_id'], []):
            out[(pid, semana)] += round(hh * peso, 4)

    return out


@router.get("/captura")
async def captura(semana: int):
    pool = await db()
    async with pool.acquire() as con:
        partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
        avances = await con.fetch(
            """SELECT hito_id, semana, cantidad_acum FROM ev_avances
               WHERE semana <= $1 ORDER BY hito_id, semana""", semana
        )
        hh_man = await con.fetch(
            "SELECT partida_id, semana, hh FROM ev_hh_gastadas WHERE semana = $1", semana
        )
        tareo = await _hh_tareo_por_semana(con)

    ult_av, av_actual = {}, {}
    for a in avances:
        if a["semana"] == semana:
            av_actual[a["hito_id"]] = float(a["cantidad_acum"])
        else:
            ult_av[a["hito_id"]] = float(a["cantidad_acum"])

    hh_manual = {r["partida_id"]: float(r["hh"]) for r in hh_man}

    por_partida = defaultdict(list)
    for h in hitos:
        por_partida[h["partida_id"]].append(h)

    out = []
    for p in partidas:
        out.append({
            "partida_id": p["id"],
            "codigo": p["codigo"],
            "otm_id": p["otm_id"],
            "descripcion": p["descripcion"],
            "unidad": p["unidad"],
            "metrado_proyec": float(p["metrado_proyec"] or p["metrado_presup"]),
            "hh_tareo": round(tareo.get((p["id"], semana), 0.0), 2),
            "hh_semana": hh_manual.get(p["id"], 0.0),
            "hitos": [
                {
                    "hito_id": h["id"], "numero": h["numero"],
                    "descripcion": h["descripcion"], "peso": float(h["peso"]),
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
                       ON CONFLICT (partida_id, semana) DO UPDATE SET hh=$3""",
                    r.partida_id, body.semana, r.hh,
                )
    return {"ok": True}


# ---------------------- Motor de cálculo ----------------------
def _acum_a_semana(avances, semana: int) -> dict:
    acum = {}
    for a in avances:
        if a["semana"] <= semana:
            acum[a["hito_id"]] = float(a["cantidad_acum"])
    return acum


def _calcular(partidas, hitos, avances, hh_rows, tareo, semana: int):
    """hh_rows: ev_hh_gastadas (manual/importado). tareo: {(pid,sem):hh} del QR.
    HH gastadas totales = manual + tareo."""
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
    for (pid, s), v in tareo.items():
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
        prod_real = (gastadas_acum / cant_inst) if cant_inst > 0 else 0.0
        saldo_met = max(mp - cant_inst, 0.0)
        eac_hh = (prod_real * saldo_met + gastadas_acum) if cant_inst > 0 else hh_proyec

        filas.append({
            "partida_id": pid,
            "codigo": p["codigo"],
            "otm_id": p["otm_id"],
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


async def _datos_base(semana: int, otm: Optional[str] = None):
    pool = await db()
    async with pool.acquire() as con:
        if otm:
            partidas = await con.fetch(
                "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
            )
        else:
            partidas = await con.fetch("SELECT * FROM ev_partidas WHERE activo ORDER BY codigo")
        hitos = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
        avances = await con.fetch(
            "SELECT hito_id, semana, cantidad_acum FROM ev_avances WHERE semana <= $1 ORDER BY semana",
            semana,
        )
        hh = await con.fetch(
            "SELECT partida_id, semana, hh FROM ev_hh_gastadas WHERE semana <= $1", semana
        )
        tareo = await _hh_tareo_por_semana(con)
    return partidas, hitos, avances, hh, tareo



@router.get("/semanas-auto")
async def semanas_auto():
    """Semanas reales del proyecto (Lun-Dom) desde el primer registro de tareo.
    Incluye semanas sin actividad para mostrar la línea de tiempo completa."""
    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
        if not base:
            return []

        hh_rows = await con.fetch("""
            SELECT DATE_TRUNC('week', fecha)::date AS lunes, SUM(hh) AS hh_total
            FROM registros WHERE hh IS NOT NULL AND hh > 0
            GROUP BY DATE_TRUNC('week', fecha)::date
            ORDER BY lunes
        """)
        if not hh_rows:
            return []

        hh_map: dict = {}
        for r in hh_rows:
            n = _semana_de(r['lunes'], base)
            hh_map[n] = float(r['hh_total'])

        today = date.today()
        current_monday = today - timedelta(days=today.weekday())
        total = max(_semana_de(current_monday, base), max(hh_map.keys()))

        MESES = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
        def fmt(d: date) -> str:
            return f"{d.day} {MESES[d.month-1]}"

        result = []
        for n in range(1, total + 1):
            lunes  = base + timedelta(weeks=n - 1)
            domingo = lunes + timedelta(days=6)
            hh     = hh_map.get(n, 0.0)
            result.append({
                "semana": n,
                "inicio": lunes.isoformat(),
                "fin":    domingo.isoformat(),
                "hh":     round(hh, 1),
                "activa": hh > 0,
                "label":  f"Sem {n}  ·  {fmt(lunes)} – {fmt(domingo)}"
                          + ("" if hh > 0 else "  (sin actividad)"),
            })
        return result


@router.get("/reporte")
async def reporte(semana: int, otm: Optional[str] = None):
    partidas, hitos, avances, hh, tareo = await _datos_base(semana, otm)
    filas = _calcular(partidas, hitos, avances, hh, tareo, semana)

    tot_proyec = sum(f["hh_proyec"] for f in filas)
    tot_ganadas = sum(f["hh_ganadas_acum"] for f in filas)
    tot_gastadas = sum(f["hh_gastadas_acum"] for f in filas)
    tot_gan_sem = sum(f["hh_ganadas_sem"] for f in filas)
    tot_gas_sem = sum(f["hh_gastadas_sem"] for f in filas)
    tot_eac = sum(f["eac_hh"] for f in filas)

    return {
        "semana": semana,
        "otm": otm,
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
        "por_otm": _agrupar(filas, "otm_id"),
        "por_fase": _agrupar(filas, "fase"),
        "por_sistema": _agrupar(filas, "sistema"),
        "partidas": filas,
    }


@router.get("/curva")
async def curva(hasta: int, otm: Optional[str] = None):
    partidas, hitos, avances, hh, tareo = await _datos_base(hasta, otm)
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
        serie.append({
            "semana": s,
            "hh_ganadas_acum": round(g, 2),
            "hh_gastadas_acum": round(c, 2),
            "pf_acum": round(g / c, 3) if c > 0 else None,
            "pf_sem": round(gs / cs, 3) if cs > 0 else None,
        })
    return serie


@router.get("/curva-fase")
async def curva_fase(hasta: int, otm: Optional[str] = None):
    """Serie semanal de PF acumulado por fase — gráficos por disciplina."""
    partidas, hitos, avances, hh, tareo = await _datos_base(hasta, otm)
    semanas_set = sorted(
        {a["semana"] for a in avances} | {r["semana"] for r in hh}
        | {s for (_, s) in tareo.keys()} | {hasta}
    )
    fases = sorted({p["fase"] for p in partidas})
    serie = []
    for s in semanas_set:
        if s > hasta:
            continue
        filas = _calcular(partidas, hitos, avances, hh, tareo, s)
        punto: dict = {"semana": s}
        for fase in fases:
            ff = [f for f in filas if f["fase"] == fase]
            g = sum(f["hh_ganadas_acum"] for f in ff)
            c = sum(f["hh_gastadas_acum"] for f in ff)
            punto[f"pf_{fase}"] = round(g / c, 3) if c > 0 else None
        serie.append(punto)
    return {"fases": fases, "serie": serie}
