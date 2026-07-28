# ============================================================
# routers/prog_cierre.py — cierre de la semana (PPC que no se mueve)
#
# El PPC se calculaba siempre sobre el plan VIGENTE, así que el pasado cambiaba:
# reprogramar una actividad que no se hizo borraba su compromiso de la semana ya
# cerrada y el indicador subía solo; el trabajo creado a mitad de semana entraba
# al denominador como si se hubiera comprometido el lunes. En Last Planner el
# PPC se mide contra lo COMPROMETIDO, y por eso la semana se cierra.
#
# Cerrar una semana = congelar, actividad por actividad, el comprometido, el
# alcanzado y el veredicto. A partir de ahí ese PPC ya no se recalcula, pase lo
# que pase con la programación.
#
# CUÁNDO se cierra es configurable por proyecto (`prog_config.cierre_dia` +
# `cierre_semana_siguiente`), porque cada empresa lo pide un día distinto: en la
# obra de Jean se trabaja de lunes a domingo en dos guardias, pero el reporte lo
# pedían el viernes en una empresa y el lunes siguiente en otra.
#
# OJO — el día de corte NO es el día en que termina la semana. La semana sigue
# siendo lunes→domingo en todos los módulos (EV, tareo, LookAhead, curva S);
# el corte solo dice cuándo se mira. Desalinear el ancla entre módulos haría
# que los indicadores dejaran de ser comparables entre sí.
# ============================================================
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from core.db import db
from core.log import get_logger
from core.tiempo import fecha_lima, parse_fecha
# Import unidireccional (prog_cierre → programacion) para que el compromiso que
# se congela sea EXACTAMENTE el que el planner vio en el PPC.
from routers.programacion import CNC, _detalle_semana, _lunes_de as lunes_de

log = get_logger("programacion")

router = APIRouter(prefix="/ev/programacion", tags=["programacion"])

CIERRE_DEFECTO = {"cierre_dia": 7, "cierre_semana_siguiente": False}


def fecha_corte(lunes: date, cierre_dia: int = 7, semana_siguiente: bool = False) -> date:
    """Día en que se hace el corte del PPC de la semana que arranca en `lunes`.

    `cierre_dia` es ISO (1=lunes … 7=domingo). Con `semana_siguiente` el corte
    cae en esa misma jornada pero de la semana de después: «los lunes reporto la
    semana pasada» = cierre_dia 1 + semana_siguiente.
    """
    # `or 7` convertiría un 0 en domingo en silencio: el default se aplica solo
    # cuando de verdad no vino nada.
    d = int(7 if cierre_dia is None else cierre_dia)
    if d < 1 or d > 7:
        raise HTTPException(400, "cierre_dia debe estar entre 1 (lunes) y 7 (domingo)")
    corte = lunes + timedelta(days=d - 1)
    return corte + timedelta(days=7) if semana_siguiente else corte


def ventana_corte(lunes: date, cierre_dia: int = 7, semana_siguiente: bool = False) -> tuple:
    """(corte, hasta, parcial) de una semana.

    `hasta` es el último día que se CUENTA: el corte, o el domingo si el corte
    es posterior. `parcial` avisa de que la semana todavía no había terminado
    cuando se cortó — el caso del reporte que piden el viernes en una obra que
    trabaja también sábado y domingo. Un PPC parcial es legítimo (es un vistazo
    de gestión), pero tiene que decir que lo es.
    """
    corte = fecha_corte(lunes, cierre_dia, semana_siguiente)
    domingo = lunes + timedelta(days=6)
    hasta = min(corte, domingo)
    return corte, hasta, corte < domingo


def veredicto(comprometido: float, alcanzado: float, estado: str) -> bool:
    """¿La actividad cumplió su compromiso de la semana?

    Se compara el TOTAL de la semana, no día por día: si el plan decía 100 el
    jueves y 100 el viernes y se hicieron 50 y 150, cumplió. Los estados
    manuales mandan sobre el metrado, como en /ppc.
    """
    if estado == "NO_CUMPLIDA":
        return False
    if estado == "EJECUTADO":
        return True
    return float(alcanzado or 0) >= float(comprometido or 0) - 5e-4


def es_no_planificada(creado_en, referencia) -> bool:
    """¿La actividad entró DESPUÉS de comprometerse la semana?

    `referencia` es el corte de la semana anterior (cuando esa semana está
    cerrada) o el lunes de esta. Lo que nació después no estaba en el plan que
    se comprometió, así que no puede juzgar su cumplimiento: se cuenta aparte
    como trabajo no planificado — la medida de cuánto improvisa la obra.

    El sistema PROPONE y el planner confirma al cerrar: si el plan se armó el
    lunes por la mañana, desmarca y listo.
    """
    if creado_en is None or referencia is None:
        return False
    c = creado_en
    if isinstance(c, datetime):
        c = c.astimezone(timezone.utc).date() if c.tzinfo else c.date()
    return c > referencia


# ── Configuración del corte ──────────────────────────────────
async def leer_config_cierre(con, proyecto_id: int) -> dict:
    row = await con.fetchrow(
        "SELECT cierre_dia, cierre_semana_siguiente FROM prog_config WHERE proyecto_id = $1",
        proyecto_id)
    if not row:
        return dict(CIERRE_DEFECTO)
    return {"cierre_dia": int(row["cierre_dia"] or 7),
            "cierre_semana_siguiente": bool(row["cierre_semana_siguiente"])}


@router.get("/cierre-config")
async def ver_cierre_config(proyecto_id: int = 1):
    """Cuándo se corta el PPC en este proyecto."""
    pool = await db()
    async with pool.acquire() as con:
        cfg = await leer_config_cierre(con, proyecto_id)
    hoy = fecha_lima()
    lun = lunes_de(hoy)
    corte, hasta, parcial = ventana_corte(lun, **_kw(cfg))
    return {**cfg, "proyecto_id": proyecto_id,
            "ejemplo": {"lunes": str(lun), "corte": str(corte),
                        "hasta": str(hasta), "parcial": parcial}}


def _kw(cfg: dict) -> dict:
    return {"cierre_dia": cfg["cierre_dia"],
            "semana_siguiente": cfg["cierre_semana_siguiente"]}


@router.put("/cierre-config")
async def guardar_cierre_config(data: dict):
    """Día en que se hace el corte del PPC. No mueve la semana: la semana sigue
    siendo lunes→domingo para todos los módulos."""
    dia = int(7 if data.get("cierre_dia") is None else data["cierre_dia"])
    if dia < 1 or dia > 7:
        raise HTTPException(400, "cierre_dia debe estar entre 1 (lunes) y 7 (domingo)")
    siguiente = bool(data.get("cierre_semana_siguiente"))
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO prog_config (proyecto_id, cierre_dia, cierre_semana_siguiente)
               VALUES ($1,$2,$3) ON CONFLICT (proyecto_id) DO UPDATE
                 SET cierre_dia = $2, cierre_semana_siguiente = $3, actualizado_en = now()""",
            proyecto_id, dia, siguiente)
    return {"ok": True, "cierre_dia": dia, "cierre_semana_siguiente": siguiente}


# ── La semana: ver, cerrar, reabrir ──────────────────────────
def _lunes_arg(lunes: str) -> date:
    f = parse_fecha(lunes) if lunes else fecha_lima()
    if not f:
        raise HTTPException(400, "lunes inválido (usa aaaa-mm-dd)")
    return lunes_de(f)


async def _leer_cierre(con, proyecto_id: int, lun: date) -> dict:
    cab = await con.fetchrow(
        "SELECT * FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
        proyecto_id, lun)
    if not cab:
        return {}
    det = await con.fetch(
        """SELECT d.*, s.nombre AS supervisor_nombre
             FROM prog_semana_cierre_det d
             LEFT JOIN supervisores s ON s.id = d.supervisor_id
            WHERE d.cierre_id = $1 ORDER BY d.cumplida, d.titulo""", cab["id"])
    return {
        "cerrada": True, "lunes": str(cab["lunes"]), "hasta": str(cab["hasta"]),
        "parcial": cab["parcial"], "cerrado_en": str(cab["cerrado_en"]),
        "cerrado_por": cab["cerrado_por"], "nota": cab["nota"],
        "comprometidas": cab["comprometidas"], "cumplidas": cab["cumplidas"],
        "no_cumplidas": cab["no_cumplidas"], "no_planificadas": cab["no_planificadas"],
        "ppc": (round(cab["cumplidas"] / cab["comprometidas"], 4)
                if cab["comprometidas"] else None),
        "actividades": [{
            "actividad_id": r["actividad_id"], "titulo": r["titulo"],
            "partida_id": r["partida_id"], "supervisor_id": r["supervisor_id"],
            "supervisor_nombre": r["supervisor_nombre"],
            "comprometido": float(r["comprometido"] or 0),
            "alcanzado": float(r["alcanzado"] or 0),
            "cumplida": r["cumplida"], "no_planificada": r["no_planificada"],
            "causa_cat": r["causa_cat"], "causa": r["causa"],
        } for r in det],
    }


@router.get("/cierre-semana")
async def ver_cierre_semana(lunes: str = "", proyecto_id: int = 1):
    """La semana lista para cerrar, o el cierre ya congelado si existe.

    Sin cerrar devuelve la PROPUESTA: qué comprometió cada actividad, cuánto
    alcanzó, el veredicto y cuáles entraron después del compromiso. El planner
    confirma, corrige lo que haga falta y recién ahí se congela."""
    lun = _lunes_arg(lunes)
    pool = await db()
    async with pool.acquire() as con:
        ya = await _leer_cierre(con, proyecto_id, lun)
        if ya:
            return {**ya, "cnc_catalogo": CNC}
        cfg = await leer_config_cierre(con, proyecto_id)
        corte, hasta, parcial = ventana_corte(lun, **_kw(cfg))
        filas = await _detalle_semana(con, proyecto_id, lun, hasta)
        # Referencia del compromiso: el corte de la semana ANTERIOR si esa
        # semana se cerró; si no, el lunes de esta.
        prev = await con.fetchval(
            "SELECT hasta FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
            proyecto_id, lun - timedelta(days=7))
        referencia = prev or lun
        nombres = {r["id"]: r["nombre"] for r in await con.fetch(
            "SELECT id, nombre FROM supervisores")}
    props = []
    for f in filas:
        if f["comprometido"] <= 0:
            continue
        props.append({
            **{k: v for k, v in f.items() if k != "creado_en"},
            "supervisor_nombre": nombres.get(f["supervisor_id"]),
            "cumplida": veredicto(f["comprometido"], f["alcanzado"], f["estado"]),
            "no_planificada": es_no_planificada(f["creado_en"], referencia),
        })
    comprometidas = [p for p in props if not p["no_planificada"]]
    cumplidas = [p for p in comprometidas if p["cumplida"]]
    return {
        "cerrada": False, "lunes": str(lun), "corte": str(corte), "hasta": str(hasta),
        "parcial": parcial, "hoy": str(fecha_lima()),
        "puede_cerrarse": fecha_lima() >= corte,
        "referencia_compromiso": str(referencia),
        "comprometidas": len(comprometidas), "cumplidas": len(cumplidas),
        "no_cumplidas": len(comprometidas) - len(cumplidas),
        "no_planificadas": sum(1 for p in props if p["no_planificada"]),
        "ppc": (round(len(cumplidas) / len(comprometidas), 4) if comprometidas else None),
        "actividades": props, "cnc_catalogo": CNC, **cfg,
    }


@router.post("/cierre-semana")
async def cerrar_semana(data: dict):
    """Congela el PPC de la semana. A partir de aquí no se recalcula: reprogramar
    o agregar trabajo ya no puede cambiar lo que pasó.

    body: {lunes, proyecto_id?, nota?, actividades: [{actividad_id, cumplida?,
    no_planificada?, causa_cat?, causa?}]} — lo que el planner corrigió sobre la
    propuesta. Lo que no venga se toma tal cual del cálculo.
    """
    lun = _lunes_arg(str(data.get("lunes") or ""))
    proyecto_id = int(data.get("proyecto_id") or 1)
    ajustes = {}
    for a in (data.get("actividades") or []):
        if isinstance(a, dict) and a.get("actividad_id"):
            ajustes[int(a["actividad_id"])] = a
    pool = await db()
    async with pool.acquire() as con:
        if await con.fetchval(
                "SELECT 1 FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
                proyecto_id, lun):
            raise HTTPException(
                409, "Esa semana ya está cerrada. Reábrela si necesitas corregirla.")
        cfg = await leer_config_cierre(con, proyecto_id)
        corte, hasta, parcial = ventana_corte(lun, **_kw(cfg))
        if fecha_lima() < corte:
            raise HTTPException(
                400, f"La semana se corta el {corte}: todavía no se puede cerrar.")
        filas = await _detalle_semana(con, proyecto_id, lun, hasta)
        prev = await con.fetchval(
            "SELECT hasta FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
            proyecto_id, lun - timedelta(days=7))
        referencia = prev or lun
        det = []
        for f in filas:
            if f["comprometido"] <= 0:
                continue
            aj = ajustes.get(f["actividad_id"], {})
            cumplida = bool(aj["cumplida"]) if "cumplida" in aj else veredicto(
                f["comprometido"], f["alcanzado"], f["estado"])
            no_plan = (bool(aj["no_planificada"]) if "no_planificada" in aj
                       else es_no_planificada(f["creado_en"], referencia))
            cat = str(aj.get("causa_cat") or "").strip().upper() or None
            if cat and cat not in CNC:
                raise HTTPException(400, f"Causa desconocida: {cat}")
            det.append({**f, "cumplida": cumplida, "no_planificada": no_plan,
                        "causa_cat": cat,
                        "causa": (str(aj.get("causa") or "").strip() or None)})
        comprometidas = [d for d in det if not d["no_planificada"]]
        cumplidas = sum(1 for d in comprometidas if d["cumplida"])
        async with con.transaction():
            cid = await con.fetchval(
                """INSERT INTO prog_semana_cierre
                     (proyecto_id, lunes, hasta, parcial, comprometidas, cumplidas,
                      no_cumplidas, no_planificadas, cerrado_por, nota)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                proyecto_id, lun, hasta, parcial, len(comprometidas), cumplidas,
                len(comprometidas) - cumplidas, len(det) - len(comprometidas),
                str(data.get("cerrado_por") or "").strip() or None,
                str(data.get("nota") or "").strip() or None)
            for d in det:
                await con.execute(
                    """INSERT INTO prog_semana_cierre_det
                         (cierre_id, actividad_id, titulo, partida_id, supervisor_id,
                          comprometido, alcanzado, cumplida, no_planificada,
                          causa_cat, causa)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                    cid, d["actividad_id"], d["titulo"], d["partida_id"],
                    d["supervisor_id"], d["comprometido"], d["alcanzado"],
                    d["cumplida"], d["no_planificada"], d["causa_cat"], d["causa"])
    log.info("semana cerrada", extra={"proyecto": proyecto_id, "lunes": str(lun),
                                      "comprometidas": len(comprometidas),
                                      "cumplidas": cumplidas})
    return {"ok": True, "lunes": str(lun), "hasta": str(hasta), "parcial": parcial,
            "comprometidas": len(comprometidas), "cumplidas": cumplidas,
            "no_cumplidas": len(comprometidas) - cumplidas,
            "no_planificadas": len(det) - len(comprometidas),
            "ppc": round(cumplidas / len(comprometidas), 4) if comprometidas else None}


@router.delete("/cierre-semana")
async def reabrir_semana(lunes: str = "", proyecto_id: int = 1):
    """Reabre una semana cerrada (se equivocaron, faltaba registrar un avance).
    Queda en el log: cerrar de nuevo vuelve a congelar con los datos de ahora."""
    lun = _lunes_arg(lunes)
    pool = await db()
    n = await pool.execute(
        "DELETE FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
        proyecto_id, lun)
    if n == "DELETE 0":
        raise HTTPException(404, "Esa semana no está cerrada")
    log.info("semana reabierta", extra={"proyecto": proyecto_id, "lunes": str(lun)})
    return {"ok": True, "lunes": str(lun), "reabierta": True}
