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
from datetime import date, timedelta, datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/ev", tags=["valor-ganado"])

# Zona horaria de Perú (UTC-5) — sin dependencias externas (no usar pytz)
LIMA = timezone(timedelta(hours=-5))
def _hoy_lima() -> date:
    return datetime.now(LIMA).date()

def _as_date(v) -> Optional[date]:
    """Convierte v a date (o None). asyncpg exige date, no str, en parámetros
    comparados con columnas date o casteados con ::date."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])

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
    fase: Optional[str] = None           # None para nodos padre del WBS
    sub_fase: Optional[str] = None
    descripcion: str
    unidad: Optional[str] = None         # None para nodos padre
    sistema: Optional[str] = None
    metrado_presup: float = 0
    metrado_proyec: Optional[float] = None
    hh_presup: float = 0
    tipo_actividad: Optional[str] = None
    hitos: Optional[list[HitoIn]] = None
    nivel: Optional[int] = None          # profundidad en el WBS (calculado si None)
    parent_codigo: Optional[str] = None  # código del nodo padre (calculado si None)


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
    """TODAS las OTMs registradas, con su cantidad de partidas en el módulo EV (0 si aún no tiene)."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT o.id AS otm_id, o.descripcion, o.estado,
                      COUNT(p.id) FILTER (WHERE p.activo) AS partidas
               FROM otms o
               LEFT JOIN ev_partidas p ON p.otm_id = o.id
               GROUP BY o.id, o.descripcion, o.estado
               ORDER BY o.id"""
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
                # Calcular nivel y parent_codigo si no vienen en el payload
                sep = '.' if '.' in p.codigo else ','
                nivel = p.nivel or len(p.codigo.split(sep))
                parent_codigo = p.parent_codigo
                if parent_codigo is None and nivel > 1:
                    parent_codigo = sep.join(p.codigo.split(sep)[:-1])

                # Resolver hitos según tipo de nodo
                if p.fase is None:
                    # Nodo PADRE del WBS: sin hitos (rollup calculado desde hijos)
                    hitos = []
                elif p.hitos:
                    try:
                        hitos = [HitoIn(**h.model_dump()) for h in p.hitos]
                        _validar_pesos(hitos)
                    except HTTPException as e:
                        errores.append(f"Fila {i} ({p.codigo}): {e.detail}"); continue
                elif p.tipo_actividad:
                    hitos_raw = plantillas.get(p.tipo_actividad.strip().upper())
                    if hitos_raw is None:
                        errores.append(
                            f"Fila {i} ({p.codigo}): tipo_actividad '{p.tipo_actividad}' no existe"
                        ); continue
                    try:
                        hitos = [HitoIn(**h) for h in hitos_raw]; _validar_pesos(hitos)
                    except Exception as e:
                        errores.append(f"Fila {i} ({p.codigo}): hitos inválidos ({e})"); continue
                else:
                    hitos_raw = plantillas.get("GENERICO", [
                        {"numero": 1, "descripcion": "Ejecución", "peso": 1.0, "es_principal": True}
                    ])
                    try:
                        hitos = [HitoIn(**h) for h in hitos_raw]; _validar_pesos(hitos)
                    except Exception as e:
                        errores.append(f"Fila {i} ({p.codigo}): {e}"); continue

                existente = await con.fetchval(
                    "SELECT id FROM ev_partidas WHERE codigo=$1", p.codigo
                )
                if existente:
                    await con.execute(
                        """UPDATE ev_partidas SET otm_id=$2, fase=$3, sub_fase=$4, descripcion=$5,
                           unidad=$6, sistema=$7, metrado_presup=$8, metrado_proyec=$9,
                           hh_presup=$10, nivel=$11, parent_codigo=$12, activo=TRUE WHERE id=$1""",
                        existente, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo,
                    )
                    await con.execute("DELETE FROM ev_hitos WHERE partida_id=$1", existente)
                    pid = existente; actualizadas += 1
                else:
                    pid = await con.fetchval(
                        """INSERT INTO ev_partidas
                           (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                            metrado_presup, metrado_proyec, hh_presup, nivel, parent_codigo)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id""",
                        p.codigo, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo,
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
        WITH hoja AS (
            -- Solo nodos hoja: su codigo NO aparece como parent_codigo de nadie
            SELECT id, otm_id, hh_presup
            FROM ev_partidas p
            WHERE activo = true AND hh_presup > 0
              AND NOT EXISTS (
                  SELECT 1 FROM ev_partidas ch
                  WHERE ch.parent_codigo = p.codigo
                    AND ch.otm_id = p.otm_id
                    AND ch.activo = true
              )
        )
        SELECT id AS partida_id, otm_id,
               hh_presup::float /
               NULLIF(SUM(hh_presup) OVER (PARTITION BY otm_id), 0.0) AS peso
        FROM hoja
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


async def _hh_real_por_semana(con) -> dict:
    """{(partida_id, semana): hh} — HH EXACTAS del tareo de la app (tareo_partida).
    La semana se recalcula desde la fecha con la misma base (lunes) que usa el
    ISP, para no depender de tareo_partida.semana (que pudo guardarse con otra
    lógica en filas antiguas)."""
    out: dict = defaultdict(float)
    base = await _fecha_base(con)
    if not base:
        return out
    rows = await con.fetch(
        """SELECT partida_id, fecha, SUM(hh) AS hh
           FROM tareo_partida
           WHERE hh IS NOT NULL
           GROUP BY partida_id, fecha"""
    )
    for r in rows:
        out[(r['partida_id'], _semana_de(r['fecha'], base))] += float(r['hh'])
    return out


async def _hh_gastadas_unificada(con) -> dict:
    """FUENTE ÚNICA de HH gastadas por (partida, semana) para árbol/ISP/reporte.

    Precedencia (de mayor a menor):
      1. override manual del residente  (ev_hh_gastadas fuente 'manual'/'distribucion')
      2. tareo_partida REAL              (lo que captura la app — fuente principal)
      3. migración histórica             (ev_hh_gastadas fuente 'historico'/'importado')
      4. estimación proporcional         (desde registros, fallback legacy)

    Las filas 'diario'/'tareo' de ev_hh_gastadas (que producía el botón
    "Volcar al ISP") se IGNORAN: ahora se lee tareo_partida directamente, así que
    el volcado manual ya no es necesario y no hay doble conteo.
    """
    out: dict = {}
    # 4) proporcional (más bajo)
    for k, v in (await _hh_tareo_por_semana(con)).items():
        out[k] = v
    # separar overrides y migración de ev_hh_gastadas
    rows = await con.fetch("SELECT partida_id, semana, hh, fuente FROM ev_hh_gastadas")
    migr, override = {}, {}
    for r in rows:
        key = (r["partida_id"], r["semana"])
        f = (r["fuente"] or "").lower()
        if f in ("manual", "distribucion"):
            override[key] = float(r["hh"])
        elif f in ("historico", "importado"):
            migr[key] = float(r["hh"])
    # 3) migración histórica
    for k, v in migr.items():
        out[k] = v
    # 2) tareo real (gana sobre proporcional y migración)
    for k, v in (await _hh_real_por_semana(con)).items():
        out[k] = v
    # 1) override manual (gana sobre todo)
    for k, v in override.items():
        out[k] = v
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
        tareo = await _hh_gastadas_unificada(con)
    # hh_rows se devuelve vacío: las HH gastadas ya vienen unificadas en `tareo`
    # (manual > tareo_partida real > histórico > proporcional). Ver _hh_gastadas_unificada.
    return partidas, hitos, avances, [], tareo



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
            # Sin registros aún — devolver semana 1 para que el panel no quede colgado
            lunes0  = base
            dom0    = lunes0 + timedelta(days=6)
            def _fm(d): return f"{d.day} {['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'][d.month-1]}"
            return [{
                "semana": 1, "inicio": lunes0.isoformat(), "fin": dom0.isoformat(),
                "hh": 0.0, "activa": False,
                "label": f"Sem 1  ·  {_fm(lunes0)} – {_fm(dom0)}  (sin actividad aún)"
            }]

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



@router.get("/arbol")
async def arbol_wbs(otm: Optional[str] = None, semana: int = 1):
    """Árbol WBS completo (padre + hoja) con valores EV calculados.
    Nodos padre tienen hh_ganadas/gastadas = 0 — el rollup lo hace el frontend."""
    try:
        pool = await db()
        async with pool.acquire() as con:
            if otm:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
                )
            else:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo ORDER BY codigo"
                )
            hitos   = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
            avances = await con.fetch(
                "SELECT hito_id, semana, cantidad_acum FROM ev_avances WHERE semana <= $1", semana
            )
            tareo   = await _hh_gastadas_unificada(con)

        filas_ev = _calcular(list(partidas), list(hitos), list(avances), [], tareo, semana)
        ev_por_id = {f["partida_id"]: f for f in filas_ev}

        result = []
        for p in partidas:
            ev = ev_por_id.get(p["id"], {})
            result.append({
                "id":              p["id"],
                "codigo":          p["codigo"],
                "otm_id":          p["otm_id"],
                "fase":            p["fase"],
                "sub_fase":        p["sub_fase"],
                "descripcion":     p["descripcion"],
                "unidad":          p["unidad"],
                "hh_presup":       float(p["hh_presup"] or 0),
                "metrado_presup":  float(p["metrado_presup"] or 0),
                "metrado_proyec":  float(p["metrado_proyec"]) if p["metrado_proyec"] is not None else None,
                "nivel":           int(p["nivel"] or 1),
                "parent_codigo":   p["parent_codigo"],
                "es_hoja":         p["fase"] is not None,
                "hh_ganadas_acum": ev.get("hh_ganadas_acum", 0.0),
                "hh_gastadas_acum":ev.get("hh_gastadas_acum", 0.0),
                "pct_avance":      ev.get("pct_avance", 0.0),
                "pf_acum":         ev.get("pf_acum", 0.0),
            })
        return {"semana": semana, "otm": otm, "filas": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error calculando árbol WBS: {e}")



@router.post("/distribuir-hh")
async def distribuir_hh(body: dict):
    """Distribuye las HH de un OTM/fecha entre múltiples partidas.
    Reemplaza la distribución automática proporcional para ese OTM/fecha/semana.
    
    Body: {otm_id, fecha, distribuciones: [{partida_id, hh}]}
    """
    otm_id       = str(body.get("otm_id", "")).strip()
    fecha_str    = str(body.get("fecha", _hoy_lima().isoformat()))
    distribs     = body.get("distribuciones", [])
    if not otm_id or not distribs:
        raise HTTPException(400, "otm_id y distribuciones son requeridos")

    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
        if not base:
            raise HTTPException(400, "No hay semanas configuradas")
        from datetime import date
        fecha = date.fromisoformat(fecha_str)
        semana = _semana_de(fecha, base)

        asignados = 0
        for d in distribs:
            pid = int(d.get("partida_id", 0))
            hh  = float(d.get("hh", 0))
            if pid <= 0 or hh <= 0:
                continue
            await con.execute(
                """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                   VALUES ($1, $2, $3, 'distribucion')
                   ON CONFLICT (partida_id, semana) DO UPDATE SET hh=$3, fuente='distribucion'""",
                pid, semana, hh
            )
            asignados += 1

    return {"ok": True, "semana": semana, "asignados": asignados}



@router.get("/isp")
async def isp_reporte(otm: Optional[str] = None):
    """ISP completo estilo Fluor: ResPorSubFase + Productividades + Resumen.
    Devuelve datos por partida × semana para el periodo completo del proyecto."""
    try:
        pool = await db()
        async with pool.acquire() as con:
            base = await _fecha_base(con)
            if not base:
                return {"semanas": [], "partidas": []}
            today = date.today()
            total = max(_semana_de(today, base), 1)

            if otm:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo AND otm_id=$1 ORDER BY codigo", otm
                )
            else:
                partidas = await con.fetch(
                    "SELECT * FROM ev_partidas WHERE activo ORDER BY codigo"
                )
            hitos   = await con.fetch("SELECT * FROM ev_hitos ORDER BY partida_id, numero")
            avances = await con.fetch("SELECT * FROM ev_avances ORDER BY semana")
            tareo   = await _hh_gastadas_unificada(con)

        # Calcular EV para cada semana (una llamada por semana, datos cargados en memoria)
        result_por_partida: dict = {}
        for p in partidas:
            pid = p["id"]
            mp  = float(p["metrado_proyec"] or p["metrado_presup"] or 0)
            hp  = float(p["hh_presup"] or 0)
            fc  = round(hp / mp, 4) if mp > 0 else 0.0
            result_por_partida[pid] = {
                "partida_id":   pid,
                "codigo":       p["codigo"],
                "otm_id":       p["otm_id"],
                "descripcion":  p["descripcion"],
                "unidad":       p["unidad"],
                "fase":         p["fase"],
                "hh_presup":    hp,
                "metrado_presup": float(p["metrado_presup"] or 0),
                "metrado_proyec": mp,
                "factor_conv":  fc,
                "es_hoja":      p["fase"] is not None,
                "semanas":      {},
            }

        for s in range(1, total + 1):
            filas = _calcular(list(partidas), list(hitos), list(avances), [], tareo, s)
            for f in filas:
                pid = f["partida_id"]
                if pid in result_por_partida:
                    result_por_partida[pid]["semanas"][s] = {
                        "hh_gan_acum":  round(f["hh_ganadas_acum"],   2),
                        "hh_gan_sem":   round(f["hh_ganadas_sem"],    2),
                        "hh_gast_acum": round(f["hh_gastadas_acum"],  2),
                        "hh_gast_sem":  round(f["hh_gastadas_sem"],   2),
                        "pf_acum":      round(f["pf_acum"],           4),
                        "pf_sem":       round(f["pf_sem"],            4),
                        "pct_avance":   round(f["pct_avance"],        4),
                        "cant_acum":    round(
                            f["hh_ganadas_acum"] / (result_por_partida[pid]["factor_conv"] or 1), 2
                        ) if result_por_partida[pid]["factor_conv"] > 0 else 0,
                    }

        # Semanas con labels
        MESES = ["Ene","Feb","Mar","Abr","May","Jun","Jul","Ago","Sep","Oct","Nov","Dic"]
        def fmt(d: date) -> str: return f"{d.day} {MESES[d.month-1]}"
        semanas_out = []
        for n in range(1, total + 1):
            lunes  = base + timedelta(weeks=n-1)
            domingo = lunes + timedelta(days=6)
            semanas_out.append({
                "semana": n,
                "label": f"Sem {n}",
                "inicio": lunes.isoformat(),
                "fin": domingo.isoformat(),
                "label_full": f"Sem {n} · {fmt(lunes)}–{fmt(domingo)}",
            })

        return {"semanas": semanas_out, "partidas": list(result_por_partida.values())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error calculando ISP: {e}")


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


# ═══════════════════════════════════════════════════════════════
# SPRINT 2: Control diario por partida
# ═══════════════════════════════════════════════════════════════

@router.get("/semana-grid")
async def semana_grid(
    semana: int,
    otm: Optional[str] = None,
    lunes: Optional[str] = None,   # ISO date del lunes — override de fecha_base
):
    """
    Grilla semanal: partidas × días (Lun-Dom).
    Incluye HH reales (tareo_partida), HH estimadas (registros histórico)
    y cant_ejecutada (ev_avances_diarios).
    """
    try:
        pool = await db()
        async with pool.acquire() as con:
            # ── Fechas del período ───────────────────────────────
            if lunes:
                lunes_date = date.fromisoformat(lunes)
            else:
                base = await _fecha_base(con)
                if not base:
                    raise HTTPException(
                        400,
                        "Fecha base no configurada. Ve a Configuración en el panel, "
                        "o pasa ?lunes=YYYY-MM-DD como parámetro."
                    )
                lunes_date = base + timedelta(weeks=semana - 1)

            domingo_date = lunes_date + timedelta(days=6)
            fechas = [lunes_date + timedelta(days=i) for i in range(7)]
            fechas_str = [f.isoformat() for f in fechas]

            # ── Partidas hoja de la OTM ──────────────────────────
            q = """
                SELECT p.id, p.codigo, p.descripcion, p.fase, p.sub_fase,
                       p.unidad, p.hh_presup, p.metrado_presup,
                       CASE WHEN p.metrado_presup > 0
                            THEN p.hh_presup / p.metrado_presup
                            ELSE 0 END AS factor_conv
                FROM ev_partidas p
                WHERE p.activo = true AND p.fase IS NOT NULL
            """
            args: list = []
            if otm:
                args.append(otm)
                q += f" AND p.otm_id = ${len(args)}"
            q += " ORDER BY p.codigo"

            partidas = await con.fetch(q, *args)
            if not partidas:
                return {
                    "semana":   semana,
                    "otm":      otm,
                    "lunes":    lunes_date.isoformat(),
                    "fechas":   fechas_str,
                    "partidas": [],
                }

            p_ids         = [p["id"] for p in partidas]
            total_hh_pres = sum(float(p["hh_presup"] or 0) for p in partidas)

            # ── HH exactas: tareo_partida (nuevo flujo) ──────────
            hh_rows = await con.fetch(
                """SELECT partida_id, fecha::text AS f, SUM(hh) AS hh_total
                   FROM tareo_partida
                   WHERE partida_id = ANY($1)
                     AND fecha >= $2::date AND fecha <= $3::date
                     AND hh IS NOT NULL
                   GROUP BY partida_id, fecha""",
                p_ids,
                lunes_date,
                domingo_date,
            )
            hh_map = {
                (r["partida_id"], r["f"]): float(r["hh_total"] or 0)
                for r in hh_rows
            }

            # ── HH estimadas: registros histórico (fallback) ─────
            # Total HH del OTM por día (tareo viejo, sin asignación a partida)
            fallback_by_date: dict = {}
            if otm and total_hh_pres > 0:
                fb = await con.fetch(
                    """SELECT fecha::text AS f, SUM(hh) AS hh_dia
                       FROM registros
                       WHERE otm_id = $1
                         AND fecha >= $2::date AND fecha <= $3::date
                         AND hh IS NOT NULL AND hh > 0
                       GROUP BY fecha""",
                    otm,
                    lunes_date,
                    domingo_date,
                )
                fallback_by_date = {r["f"]: float(r["hh_dia"] or 0) for r in fb}

            # ── Avances diarios (cant_ejecutada) ─────────────────
            cant_rows = await con.fetch(
                """SELECT partida_id, fecha::text AS f, cantidad_dia
                   FROM ev_avances_diarios
                   WHERE partida_id = ANY($1)
                     AND fecha >= $2::date AND fecha <= $3::date""",
                p_ids,
                lunes_date,
                domingo_date,
            )
            cant_map   = {(r["partida_id"], r["f"]): float(r["cantidad_dia"] or 0) for r in cant_rows}
            cant_exist = {(r["partida_id"], r["f"]) for r in cant_rows}

            # ── Construir resultado ───────────────────────────────
            result = []
            for p in partidas:
                pid    = p["id"]
                factor = float(p["factor_conv"] or 0)
                hh_p   = float(p["hh_presup"]  or 0)
                dias   = {}

                for fecha in fechas:
                    fs  = fecha.isoformat()
                    key = (pid, fs)

                    # HH real (nuevo tareo)
                    hh_real = hh_map.get(key, 0)

                    # HH estimada (tareo viejo proporcional)
                    hh_est = 0.0
                    if hh_real == 0 and hh_p > 0 and total_hh_pres > 0:
                        hh_dia_otm = fallback_by_date.get(fs, 0)
                        if hh_dia_otm > 0:
                            hh_est = round(hh_p / total_hh_pres * hh_dia_otm, 2)

                    # Cant ejecutada
                    cant       = cant_map.get(key, None) if key in cant_exist else None
                    hh_activa  = hh_real if hh_real > 0 else hh_est
                    hh_ganadas = round(cant * factor, 4) if cant is not None and factor > 0 else None
                    pf         = round(hh_ganadas / hh_activa, 3) if hh_ganadas and hh_activa > 0 else None

                    if hh_real > 0 or hh_est > 0 or key in cant_exist:
                        dias[fs] = {
                            "hh_gastadas":    round(hh_real, 2),   # nuevo tareo (exacto)
                            "hh_estimada":    hh_est,               # tareo viejo (proporcional)
                            "cant_ejecutada": cant,                  # None = no ingresada aún
                            "hh_ganadas":     hh_ganadas,
                            "pf":             pf,
                        }

                result.append({
                    "id":             pid,
                    "codigo":         p["codigo"],
                    "descripcion":    p["descripcion"],
                    "fase":           p["fase"],
                    "sub_fase":       p["sub_fase"],
                    "unidad":         p["unidad"],
                    "factor_conv":    round(factor, 4),
                    "hh_presup":      float(p["hh_presup"]      or 0),
                    "metrado_presup": float(p["metrado_presup"] or 0),
                    "dias":           dias,
                })

            return {
                "semana":   semana,
                "otm":      otm,
                "lunes":    lunes_date.isoformat(),
                "fechas":   fechas_str,
                "partidas": result,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error calculando grilla diaria: {e}")


@router.post("/avance-diario")
async def guardar_avance_diario(data: dict):
    """Guarda o actualiza la cantidad ejecutada de una partida en un día."""
    partida_id   = data.get("partida_id")
    fecha_str    = data.get("fecha")
    cantidad_dia = data.get("cantidad_dia")  # puede ser None para borrar
    notas        = data.get("notas")
    if not partida_id or not fecha_str:
        raise HTTPException(400, "partida_id y fecha son requeridos")

    pool = await db()
    async with pool.acquire() as con:
        base = await _fecha_base(con)
        semana = 1
        if base:
            delta  = (date.fromisoformat(fecha_str) - base).days
            semana = max(1, delta // 7 + 1)

        await con.execute(
            """INSERT INTO ev_avances_diarios
                 (partida_id, fecha, semana, cantidad_dia, notas, registrado_en)
               VALUES ($1, $2::date, $3, $4, $5, NOW())
               ON CONFLICT (partida_id, fecha)
               DO UPDATE SET cantidad_dia=$4, notas=$5, registrado_en=NOW()""",
            partida_id, _as_date(fecha_str), semana, cantidad_dia, notas
        )
    return {"ok": True}




@router.post("/volcar-diario-a-isp")
async def volcar_diario_a_isp(data: dict):
    """
    Agrega las HH del control diario (tareo_partida) hacia ev_hh_gastadas,
    que es la tabla que alimenta el ISP semanal y el PF.
    Solo vuelca si hay datos reales en tareo_partida (no estimaciones).
    Devuelve cuántas partidas fueron volcadas.
    """
    semana = data.get("semana")
    otm    = data.get("otm")
    lunes  = data.get("lunes")  # solo para validar rango de fechas alternativo

    if not semana:
        raise HTTPException(400, "semana es requerido")

    pool = await db()
    async with pool.acquire() as con:
        # Sumar HH por partida para la semana
        if otm:
            rows = await con.fetch(
                """SELECT partida_id, SUM(hh) AS hh_total
                   FROM tareo_partida
                   WHERE semana = $1 AND otm_id = $2 AND hh IS NOT NULL
                   GROUP BY partida_id""",
                semana, otm
            )
        else:
            rows = await con.fetch(
                """SELECT partida_id, SUM(hh) AS hh_total
                   FROM tareo_partida
                   WHERE semana = $1 AND hh IS NOT NULL
                   GROUP BY partida_id""",
                semana
            )

        if not rows:
            raise HTTPException(
                404,
                f"Sin datos de tareo_partida para semana {semana}"
                + (f" / {otm}" if otm else "")
                + ". ¿El nuevo flujo de la app ya está en uso?"
            )

        volcados = 0
        async with con.transaction():
            for r in rows:
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1, $2, $3, 'diario')
                       ON CONFLICT (partida_id, semana)
                       DO UPDATE SET hh = $3, fuente = 'diario'""",
                    r["partida_id"], semana, round(float(r["hh_total"]), 2)
                )
                volcados += 1

        return {
            "ok":              True,
            "semana":          semana,
            "otm":             otm,
            "partidas_volcadas": volcados,
            "mensaje": f"{volcados} partidas volcadas al ISP semana {semana}. "
                       "Los valores del ISP se actualizarán en el siguiente cálculo."
        }

@router.get("/rendimiento-trabajador")
async def rendimiento_trabajador(
    trabajador_id: str,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
):
    """HH y PF de un trabajador desglosado por partida."""
    pool = await db()
    async with pool.acquire() as con:
        conds = ["tp.trabajador_id = $1"]
        args: list = [trabajador_id]
        if desde:
            args.append(_as_date(desde))
            conds.append(f"tp.fecha >= ${len(args)}::date")
        if hasta:
            args.append(_as_date(hasta))
            conds.append(f"tp.fecha <= ${len(args)}::date")
        where = " AND ".join(conds)

        # Info del trabajador
        trab = await con.fetchrow(
            "SELECT nombre, cargo FROM trabajadores WHERE id = $1", trabajador_id
        )

        rows = await con.fetch(
            f"""SELECT p.id AS partida_id, p.codigo, p.descripcion,
                       p.fase, p.unidad,
                       CASE WHEN p.metrado_presup > 0
                            THEN p.hh_presup / p.metrado_presup ELSE 0
                       END AS factor_presup,
                       SUM(tp.hh)              AS hh_total,
                       COUNT(DISTINCT tp.fecha) AS dias_trabajados
                FROM tareo_partida tp
                JOIN ev_partidas p ON p.id = tp.partida_id
                WHERE {where} AND tp.hh IS NOT NULL
                GROUP BY p.id, p.codigo, p.descripcion, p.fase, p.unidad,
                         p.hh_presup, p.metrado_presup
                ORDER BY hh_total DESC""",
            *args
        )

        # Cant ejecutada acumulada por partida en el mismo rango de fechas.
        # OJO: se deduplica (partida, fecha) ANTES de unir con ev_avances_diarios;
        # de lo contrario la cantidad del día se multiplicaría por cada trabajador.
        cant_rows = await con.fetch(
            f"""SELECT d.partida_id,
                       SUM(COALESCE(ad.cantidad_dia, 0)) AS cant_acum
                FROM (
                    SELECT DISTINCT tp.partida_id, tp.fecha
                    FROM tareo_partida tp
                    WHERE {where}
                ) d
                LEFT JOIN ev_avances_diarios ad
                  ON ad.partida_id = d.partida_id AND ad.fecha = d.fecha
                GROUP BY d.partida_id""",
            *args
        )
        cant_by_pid = {r["partida_id"]: float(r["cant_acum"] or 0)
                       for r in cant_rows}

        partidas = []
        for r in rows:
            hh      = float(r["hh_total"] or 0)
            factor  = float(r["factor_presup"] or 0)
            cant    = cant_by_pid.get(r["partida_id"], 0)
            hh_gan  = round(cant * factor, 2) if factor > 0 else None
            pf      = round(hh_gan / hh, 3) if hh_gan and hh > 0 else None
            partidas.append({
                "partida_id":     r["partida_id"],
                "codigo":         r["codigo"],
                "descripcion":    r["descripcion"],
                "fase":           r["fase"],
                "unidad":         r["unidad"],
                "factor_presup":  round(factor, 4),
                "hh_total":       round(hh, 2),
                "cant_acum":      round(cant, 2),
                "hh_ganadas":     hh_gan,
                "pf_promedio":    pf,
                "dias_trabajados": int(r["dias_trabajados"]),
            })

        return {
            "trabajador_id": trabajador_id,
            "nombre":  trab["nombre"] if trab else "—",
            "cargo":   trab["cargo"]  if trab else "—",
            "partidas": partidas,
            "hh_total_global": round(sum(p["hh_total"] for p in partidas), 2),
        }


@router.get("/rendimiento-cuadrillas")
async def rendimiento_cuadrillas(
    semana:        Optional[int] = None,
    supervisor_id: Optional[str] = None,
):
    """Comparativa de PF por cuadrilla (supervisor) y partida."""
    pool = await db()
    async with pool.acquire() as con:
        conds = ["tp.hh IS NOT NULL"]
        args: list = []
        if semana:
            args.append(semana)
            conds.append(f"tp.semana = ${len(args)}")
        if supervisor_id:
            args.append(supervisor_id)
            conds.append(f"tp.supervisor_id = ${len(args)}")
        where = " AND ".join(conds)

        rows = await con.fetch(
            f"""SELECT tp.supervisor_id,
                       s.nombre AS supervisor_nombre,
                       p.id AS partida_id, p.codigo, p.descripcion,
                       p.fase, p.unidad,
                       CASE WHEN p.metrado_presup > 0
                            THEN p.hh_presup / p.metrado_presup ELSE 0
                       END AS factor_presup,
                       SUM(tp.hh) AS hh_total,
                       COUNT(DISTINCT tp.trabajador_id) AS n_trabajadores,
                       COUNT(DISTINCT tp.fecha) AS dias
                FROM tareo_partida tp
                JOIN ev_partidas p  ON p.id  = tp.partida_id
                JOIN supervisores s ON s.id  = tp.supervisor_id
                WHERE {where}
                GROUP BY tp.supervisor_id, s.nombre, p.id, p.codigo,
                         p.descripcion, p.fase, p.unidad,
                         p.hh_presup, p.metrado_presup
                ORDER BY tp.supervisor_id, p.codigo""",
            *args
        )

        # Cant ejecutada por (supervisor, partida) en el rango.
        # Se deduplica (supervisor, partida, fecha) ANTES de unir con
        # ev_avances_diarios para no multiplicar la cantidad del día por el
        # número de trabajadores de la cuadrilla.
        cant_args = list(args)
        cant_rows = await con.fetch(
            f"""SELECT d.supervisor_id, d.partida_id,
                       SUM(COALESCE(ad.cantidad_dia, 0)) AS cant_acum
                FROM (
                    SELECT DISTINCT tp.supervisor_id, tp.partida_id, tp.fecha
                    FROM tareo_partida tp
                    WHERE {where}
                ) d
                LEFT JOIN ev_avances_diarios ad
                  ON ad.partida_id = d.partida_id AND ad.fecha = d.fecha
                GROUP BY d.supervisor_id, d.partida_id""",
            *cant_args
        )
        cant_map = {(r["supervisor_id"], r["partida_id"]): float(r["cant_acum"] or 0)
                    for r in cant_rows}

        # Agrupar por supervisor
        por_sup: dict = {}
        for r in rows:
            sid = r["supervisor_id"]
            if sid not in por_sup:
                por_sup[sid] = {"supervisor_id": sid,
                                "nombre": r["supervisor_nombre"],
                                "partidas": []}
            hh     = float(r["hh_total"] or 0)
            factor = float(r["factor_presup"] or 0)
            cant   = cant_map.get((sid, r["partida_id"]), 0)
            hh_gan = round(cant * factor, 2) if factor > 0 else None
            pf     = round(hh_gan / hh, 3) if hh_gan and hh > 0 else None
            por_sup[sid]["partidas"].append({
                "partida_id":    r["partida_id"],
                "codigo":        r["codigo"],
                "descripcion":   r["descripcion"],
                "fase":          r["fase"],
                "unidad":        r["unidad"],
                "hh_total":      round(hh, 2),
                "cant_acum":     round(cant, 2),
                "hh_ganadas":    hh_gan,
                "pf":            pf,
                "n_trabajadores": int(r["n_trabajadores"]),
                "dias":          int(r["dias"]),
            })

        return list(por_sup.values())


# ═══════════════════════════════════════════════════════════════
# FASE 1 — Control Maestro: Cuadrillas + Asignación + Histórico
# ═══════════════════════════════════════════════════════════════

# ── Cuadrillas típicas por OTM ────────────────────────────────

@router.get("/cuadrillas-plantilla")
async def listar_cuadrillas_plantilla(
    supervisor_id: str,
    otm_id: str,
):
    """Devuelve las cuadrillas típicas del supervisor para esa OTM."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT c.trabajador_id, t.nombre, t.cargo,
                      COALESCE(t.tipo,'DIRECTO') AS tipo,
                      c.nombre AS plantilla, c.orden
               FROM cuadrilla_otm c
               JOIN trabajadores t ON t.id = c.trabajador_id
               WHERE c.supervisor_id = $1 AND c.otm_id = $2 AND c.activo = TRUE
               ORDER BY c.nombre, c.orden""",
            supervisor_id, otm_id
        )
        plantillas: dict = {}
        for r in rows:
            n = r["plantilla"]
            if n not in plantillas:
                plantillas[n] = []
            plantillas[n].append({
                "trabajador_id": r["trabajador_id"],
                "nombre":        r["nombre"],
                "cargo":         r["cargo"],
                "tipo":          r["tipo"],
                "orden":         r["orden"],
            })
        return plantillas


@router.post("/cuadrillas-plantilla")
async def guardar_cuadrilla_plantilla(data: dict):
    """Crea o reemplaza una cuadrilla típica para supervisor+OTM."""
    supervisor_id = data.get("supervisor_id")
    otm_id        = data.get("otm_id")
    nombre        = data.get("nombre", "Principal")
    trabajadores  = data.get("trabajadores", [])

    if not supervisor_id or not otm_id:
        raise HTTPException(400, "supervisor_id y otm_id son requeridos")

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "DELETE FROM cuadrilla_otm WHERE supervisor_id=$1 AND otm_id=$2 AND nombre=$3",
                supervisor_id, otm_id, nombre
            )
            for idx, tid in enumerate(trabajadores):
                await con.execute(
                    """INSERT INTO cuadrilla_otm
                         (supervisor_id, otm_id, nombre, trabajador_id, orden)
                       VALUES ($1,$2,$3,$4,$5)""",
                    supervisor_id, otm_id, nombre, str(tid), idx
                )
    return {"ok": True, "nombre": nombre, "total": len(trabajadores)}


# ── Sesiones del día para asignación a partidas ───────────────

@router.get("/sesiones-sin-asignar")
async def sesiones_sin_asignar(
    fecha:  Optional[str] = None,
    otm_id: Optional[str] = None,
):
    """Sesiones enviadas del día con detalle de trabajadores y partidas."""
    pool = await db()
    async with pool.acquire() as con:
        if not fecha:
            fecha = _hoy_lima().isoformat()

        conds = ["s.estado = 'enviada'", "s.fecha = $1"]
        args: list = [_as_date(fecha)]
        if otm_id:
            args.append(otm_id)
            conds.append(f"s.otm_id = ${len(args)}")
        where = " AND ".join(conds)

        rows = await con.fetch(
            f"""SELECT s.id AS sesion_id, s.supervisor_id,
                       sup.nombre AS supervisor,
                       s.otm_id, s.fecha::text,
                       s.hh_turno,
                       COUNT(st.id)  AS n_trabajadores,
                       s.hh_turno * COUNT(st.id) AS hh_total,
                       COALESCE(SUM(stp.hh), 0)  AS hh_asignadas
                FROM sesiones s
                JOIN supervisores sup ON sup.id = s.supervisor_id
                LEFT JOIN sesion_trabajadores st  ON st.sesion_id = s.id
                LEFT JOIN sesion_trabajador_partidas stp ON stp.sesion_id = s.id
                WHERE {where}
                GROUP BY s.id, s.supervisor_id, sup.nombre,
                         s.otm_id, s.fecha, s.hh_turno
                ORDER BY s.id DESC""",
            *args
        )

        result = []
        for r in rows:
            hh_total = float(r["hh_total"] or 0)
            hh_asig  = float(r["hh_asignadas"] or 0)

            # Trabajadores con HH ya asignadas
            trab_rows = await con.fetch(
                """SELECT st.trab_id AS trabajador_id,
                          t.nombre, t.cargo,
                          COALESCE(t.tipo,'DIRECTO') AS tipo,
                          COALESCE(st.hh_override, s.hh_turno, 9.5) AS hh_registradas,
                          COALESCE(SUM(stp.hh), 0) AS hh_asignadas
                   FROM sesion_trabajadores st
                   JOIN sesiones s    ON s.id  = st.sesion_id
                   JOIN trabajadores t ON t.id = st.trab_id
                   LEFT JOIN sesion_trabajador_partidas stp
                     ON stp.sesion_id     = st.sesion_id
                    AND stp.trabajador_id = st.trab_id
                   WHERE st.sesion_id = $1
                   GROUP BY st.trab_id, t.nombre, t.cargo, t.tipo,
                            st.hh_override, s.hh_turno
                   ORDER BY t.nombre""",
                r["sesion_id"]
            )

            # Partidas hoja de la OTM
            part_rows = await con.fetch(
                """SELECT id, codigo, descripcion, fase, unidad,
                          hh_presup, metrado_presup
                   FROM ev_partidas
                   WHERE otm_id = $1 AND fase IS NOT NULL AND activo = TRUE
                   ORDER BY codigo""",
                r["otm_id"]
            )

            trabajadores = []
            for t in trab_rows:
                hh_reg  = float(t["hh_registradas"] or 9.5)
                hh_asig_t = float(t["hh_asignadas"] or 0)
                trabajadores.append({
                    "trabajador_id": t["trabajador_id"],
                    "nombre":        t["nombre"],
                    "cargo":         t["cargo"],
                    "tipo":          t["tipo"],
                    "hh_registradas": round(hh_reg, 2),
                    "hh_asignadas":   round(hh_asig_t, 2),
                    "hh_pendientes":  round(hh_reg - hh_asig_t, 2),
                })

            result.append({
                "sesion_id":   r["sesion_id"],
                "supervisor":  r["supervisor"],
                "supervisor_id": r["supervisor_id"],
                "otm_id":      r["otm_id"],
                "fecha":       r["fecha"],
                "hh_turno":    float(r["hh_turno"] or 9.5),
                "hh_total":    round(hh_total, 2),
                "hh_asignadas": round(hh_asig, 2),
                "hh_pendientes": round(hh_total - hh_asig, 2),
                "trabajadores": trabajadores,
                "partidas":     [dict(p) for p in part_rows],
            })

        return result


@router.post("/asignar-sesion-partidas")
async def asignar_sesion_partidas(data: dict):
    """Guarda la asignación de HH de una sesión → partidas específicas."""
    sesion_id    = data.get("sesion_id")
    asignaciones = data.get("asignaciones", [])  # [{trabajador_id, partida_id, hh}]

    if not sesion_id:
        raise HTTPException(400, "sesion_id es requerido")

    pool = await db()
    async with pool.acquire() as con:
        ses = await con.fetchrow(
            "SELECT id, fecha, supervisor_id, otm_id FROM sesiones WHERE id=$1",
            sesion_id
        )
        if not ses:
            raise HTTPException(404, "Sesión no encontrada")

        async with con.transaction():
            # Borrar asignaciones previas
            await con.execute(
                "DELETE FROM sesion_trabajador_partidas WHERE sesion_id=$1",
                sesion_id
            )
            for a in asignaciones:
                hh = float(a.get("hh", 0))
                if hh <= 0:
                    continue
                await con.execute(
                    """INSERT INTO sesion_trabajador_partidas
                         (sesion_id, trabajador_id, partida_id, hh,
                          fecha, supervisor_id, otm_id)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    sesion_id,
                    str(a["trabajador_id"]),
                    a.get("partida_id"),
                    hh,
                    ses["fecha"],
                    ses["supervisor_id"],
                    ses["otm_id"],
                )

            # Detectar conflictos de HH duplicadas
            for a in asignaciones:
                tid   = str(a["trabajador_id"])
                total = await con.fetchval(
                    """SELECT COALESCE(SUM(hh),0)
                       FROM sesion_trabajador_partidas
                       WHERE trabajador_id=$1 AND fecha=$2""",
                    tid, ses["fecha"]
                )
                if float(total or 0) > 11:   # umbral razonable
                    otras = await con.fetch(
                        """SELECT DISTINCT s.supervisor_id
                           FROM sesion_trabajador_partidas stp
                           JOIN sesiones s ON s.id = stp.sesion_id
                           WHERE stp.trabajador_id=$1 AND stp.fecha=$2
                             AND stp.sesion_id != $3 LIMIT 1""",
                        tid, ses["fecha"], sesion_id
                    )
                    if otras:
                        await con.execute(
                            """INSERT INTO hh_conflictos
                                 (trabajador_id, fecha,
                                  supervisor_id_1, supervisor_id_2, hh_1, hh_2)
                               VALUES ($1,$2,$3,$4,$5,$6)
                               ON CONFLICT DO NOTHING""",
                            tid, ses["fecha"],
                            otras[0]["supervisor_id"], ses["supervisor_id"],
                            9.5, float(total or 0) - 9.5
                        )

    return {"ok": True, "sesion_id": sesion_id, "asignados": len(asignaciones)}


# ── Validación HH duplicadas ──────────────────────────────────

@router.get("/hh-trabajador-dia")
async def hh_trabajador_dia(trabajador_id: str, fecha: str):
    """HH registradas para un trabajador en un día específico."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT s.id AS sesion_id, s.supervisor_id,
                      sup.nombre AS supervisor,
                      s.otm_id, stp.partida_id, p.codigo, p.descripcion,
                      stp.hh
               FROM sesion_trabajador_partidas stp
               JOIN sesiones s    ON s.id  = stp.sesion_id
               JOIN supervisores sup ON sup.id = s.supervisor_id
               LEFT JOIN ev_partidas p ON p.id = stp.partida_id
               WHERE stp.trabajador_id=$1 AND stp.fecha=$2::date
               ORDER BY s.id""",
            trabajador_id, _as_date(fecha)
        )
        total = sum(float(r["hh"] or 0) for r in rows)
        return {
            "trabajador_id": trabajador_id,
            "fecha":  fecha,
            "hh_total": round(total, 2),
            "alerta": total > 11,
            "detalle": [dict(r) for r in rows],
        }


# ── Carga histórica manual ────────────────────────────────────

@router.post("/historico/cargar")
async def cargar_historico(data: dict):
    """
    Carga acumulados históricos de HH y cantidades para una OTM/semana.
    Popula ev_hh_gastadas (fuente='historico') y ev_avances (hito principal).
    """
    otm_id = data.get("otm_id")
    semana = data.get("semana")
    filas  = data.get("filas", [])   # [{partida_id, hh_gastadas_acum, cantidad_ejecutada_acum}]

    if not otm_id or not semana:
        raise HTTPException(400, "otm_id y semana son requeridos")

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for fila in filas:
                pid  = fila["partida_id"]
                hh   = float(fila.get("hh_gastadas_acum", 0))
                cant = float(fila.get("cantidad_ejecutada_acum", 0))

                # ev_historico_carga (trazabilidad)
                await con.execute(
                    """INSERT INTO ev_historico_carga
                         (otm_id, partida_id, semana,
                          hh_gastadas_acum, cantidad_ejecutada_acum)
                       VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (otm_id, partida_id, semana)
                       DO UPDATE SET
                         hh_gastadas_acum        = EXCLUDED.hh_gastadas_acum,
                         cantidad_ejecutada_acum = EXCLUDED.cantidad_ejecutada_acum,
                         fecha_carga             = NOW()""",
                    otm_id, pid, semana, hh, cant
                )

                # ev_hh_gastadas (fuente='historico' — puede ser sobreescrito por manual)
                await con.execute(
                    """INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
                       VALUES ($1,$2,$3,'historico')
                       ON CONFLICT (partida_id, semana)
                       DO UPDATE SET hh=$3, fuente='historico'
                       WHERE ev_hh_gastadas.fuente NOT IN ('manual')""",
                    pid, semana, hh
                )

                # ev_avances con el hito principal (para cálculo de % avance)
                hito = await con.fetchrow(
                    """SELECT id FROM ev_hitos
                       WHERE partida_id=$1
                       ORDER BY peso DESC NULLS LAST, id
                       LIMIT 1""",
                    pid
                )
                if hito and cant > 0:
                    await con.execute(
                        """INSERT INTO ev_avances (hito_id, semana, cantidad_acum)
                           VALUES ($1,$2,$3)
                           ON CONFLICT (hito_id, semana)
                           DO UPDATE SET cantidad_acum=$3""",
                        hito["id"], semana, cant
                    )

    return {"ok": True, "otm_id": otm_id, "semana": semana, "partidas": len(filas)}


@router.get("/historico/lista")
async def listar_historico(otm_id: str, semana: int):
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT h.*, p.codigo, p.descripcion, p.fase, p.unidad
               FROM ev_historico_carga h
               JOIN ev_partidas p ON p.id = h.partida_id
               WHERE h.otm_id=$1 AND h.semana=$2
               ORDER BY p.codigo""",
            otm_id, semana
        )
        return [dict(r) for r in rows]


# ── Conflictos HH duplicadas ──────────────────────────────────

@router.get("/conflictos")
async def listar_conflictos(
    estado: Optional[str] = None,
    fecha:  Optional[str] = None,
):
    pool = await db()
    async with pool.acquire() as con:
        conds, args = ["1=1"], []
        if estado:
            args.append(estado);  conds.append(f"c.estado = ${len(args)}")
        if fecha:
            args.append(_as_date(fecha));   conds.append(f"c.fecha  = ${len(args)}::date")
        where = " AND ".join(conds)
        rows = await con.fetch(
            f"""SELECT c.*,
                       t.nombre   AS trabajador_nombre,
                       s1.nombre  AS sup1_nombre,
                       s2.nombre  AS sup2_nombre
                FROM hh_conflictos c
                JOIN trabajadores t   ON t.id  = c.trabajador_id
                LEFT JOIN supervisores s1 ON s1.id = c.supervisor_id_1
                LEFT JOIN supervisores s2 ON s2.id = c.supervisor_id_2
                WHERE {where}
                ORDER BY c.fecha DESC, c.created_at DESC""",
            *args
        )
        return [dict(r) for r in rows]


@router.post("/conflictos/resolver")
async def resolver_conflicto(data: dict):
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE hh_conflictos
               SET estado='RESUELTO', resolucion=$1, notas=$2
               WHERE id=$3""",
            data.get("resolucion"), data.get("notas"), data.get("conflicto_id")
        )
    return {"ok": True}
