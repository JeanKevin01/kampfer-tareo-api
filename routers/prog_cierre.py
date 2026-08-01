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
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from core.db import db
from core.log import get_logger
from core.tiempo import fecha_lima, parse_fecha
# Import unidireccional (prog_cierre → programacion) para que el compromiso que
# se congela sea EXACTAMENTE el que el planner vio en el PPC.
from routers.programacion import (
    CNC, _detalle_semana, _foto_out, _lunes_de as lunes_de,
    armar_texto_reporte, restricciones_semana, ventana_ppc)

log = get_logger("programacion")

router = APIRouter(prefix="/ev/programacion", tags=["programacion"])

CIERRE_DEFECTO = {"cierre_dia": 7, "cierre_semana_siguiente": False}

# ── Por qué entró trabajo que no estaba comprometido (0040) ──────────────
# Cuatro motivos y no más: un catálogo largo no se llena. La distinción no es
# cosmética — cada uno dispara una acción distinta, y mezclarlos vuelve el
# indicador inservible en la reunión (nadie acepta un número que lo culpa del
# cambio de prioridad de un cliente).
NO_PLAN_MOTIVOS = {
    "OMISION_PLANNER": "Omisión del planner — era previsible",
    "EMERGENCIA": "Imprevisto o emergencia en obra",
    "CLIENTE": "Pedido o cambio del cliente",
    "ADELANTO": "Estaba planificada para otra semana — se adelantó",
}

# El ADELANTO entró fuera del compromiso, pero NO es improvisación: es
# resecuenciamiento, y es justo la conducta que se quiere fomentar. Contarlo
# como desorden castigaría al planner por ser flexible.
MOTIVOS_IMPROVISACION = ("OMISION_PLANNER", "EMERGENCIA", "CLIENTE")

# ── Bitácora de la semana (0041) ─────────────────────────────────────────
# Los cuatro actos que mueven —o podrían mover— un indicador ya publicado.
# Cada uno queda registrado con quién y cuándo, y la tabla es append-only.
EVENTOS = {
    "COMPROMETIDA": "Semana comprometida",
    "DESCOMPROMETIDA": "Compromiso deshecho",
    "CERRADA": "Semana cerrada",
    "REABIERTA": "Semana reabierta",
}


def validar_motivo_no_plan(m):
    """None o uno de los cuatro; cualquier otra cosa es 422."""
    if m in (None, ""):
        return None
    if m not in NO_PLAN_MOTIVOS:
        raise HTTPException(422, f"Motivo inválido: {m}")
    return m


def cuenta_como_improvisacion(no_planificada: bool, motivo) -> bool:
    """¿Esta fila cuenta en el indicador de trabajo no planificado?

    Fuera del PPC están TODAS las que entraron después del compromiso, porque
    ninguna se prometió. Pero el indicador mide cuánto IMPROVISA la obra, y un
    adelanto no es improvisar. Sin motivo todavía se cuenta: lo pendiente de
    clasificar no puede desaparecer del número — desaparecería el incentivo a
    clasificarlo.
    """
    return bool(no_planificada) and motivo != "ADELANTO"


def ratio_no_planificado(hh_no_plan, hh_total):
    """Fracción de las horas de la semana que se fue en trabajo no planificado.

    Es el indicador que de verdad habla: contar actividades hace pesar igual
    una de 4 HH y una de 200. Sin horas registradas devuelve None (que la UI
    muestra como «—»), nunca 0: un cero diría «no improvisamos nada» cuando lo
    que pasa es que no hay tareo.
    """
    t = float(hh_total or 0)
    if t <= 0:
        return None
    return round(float(hh_no_plan or 0) / t, 4)


def atribuir_hh(claves: dict, actividades: list) -> tuple:
    """Reparte las HH del tareo entre las actividades de la semana.

    `claves` es {(partida_id, supervisor_id, fecha): hh} tal como sale de
    `tareo_partida`; `actividades` son dicts con id/partida_id/supervisor_id/
    fecha/fecha_fin. Devuelve ({actividad_id: hh}, hh_sin_actividad).

    El tareo no conoce actividades: conoce partidas. La atribución que se usa es
    la misma que el sustento por partida —misma partida, mismo supervisor, día
    dentro del rango—, que en la práctica identifica una sola actividad porque
    `_actividad_para_parte` reusa la fila viva en vez de crear otra.

    Cuando aun así hay varias candidatas (dos tramos de la misma partida con el
    mismo supervisor el mismo día) se elige la de RANGO MÁS CORTO y, a igualdad,
    el id menor: un tramo de un día describe mejor lo que pasó ese día que una
    actividad de un mes. Es determinista y explicable; repartir a medias
    inventaría datos.

    Las horas que no caen bajo ninguna actividad se devuelven aparte en vez de
    diluirse: son horas sin plan al que atribuirlas, y callarlas dejaría el
    indicador corto sin que nadie lo note.
    """
    por_act: dict = {}
    huerfanas = 0.0
    for (pid, sup, f), hh in claves.items():
        cand = [a for a in actividades
                if a["partida_id"] == pid and a["supervisor_id"] == sup
                and a["fecha"] <= f <= a["fecha_fin"]]
        if not cand:
            huerfanas += float(hh or 0)
            continue
        elegida = min(cand, key=lambda a: ((a["fecha_fin"] - a["fecha"]).days, a["id"]))
        por_act[elegida["id"]] = por_act.get(elegida["id"], 0.0) + float(hh or 0)
    return {k: round(v, 2) for k, v in por_act.items()}, round(huerfanas, 2)


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
    jueves y 100 el viernes y se hicieron 50 y 150, cumplió.

    PRECEDENCIA (corregida 2026-07-30). NO_CUMPLIDA manda siempre: es una
    declaración explícita de fracaso y nadie la pone por error. Pero si hay
    metrado comprometido, manda el METRADO — antes EJECUTADO ganaba, y eso dejó
    de ser una decisión del planner el día que el parte de campo empezó a poner
    EJECUTADO solo: una partida con 71 m³ comprometidos y 0 registrados salía
    «cumplida» por haber mandado el parte. En un sistema donde el metrado es la
    fuente única, no capturar el avance no puede equivaler a cumplir.

    Sin metrado comprometido no hay nada contra qué comparar y el proxy es si
    hubo ejecución: avance registrado o marca de EJECUTADO. Los dos extremos
    importan — con la regla del metrado, 0 ≥ 0 daría por cumplidas todas las
    actividades de apoyo y el PPC subiría solo por tenerlas en el plan; pero
    juzgarlas solo por estado declara NO CUMPLIDA a la que termina su metrado
    antes de tiempo, porque el re-prorrateo le deja el compromiso en cero.

    CANCELADO se juzga igual que NO_CUMPLIDA, y explícito y no por descarte: una
    actividad comprometida que se cancela después es un compromiso incumplido.
    Solo llega aquí si estaba en el compromiso congelado (0041) — si no, se
    filtró antes.
    """
    if estado in ("NO_CUMPLIDA", "CANCELADO"):
        return False
    if float(comprometido or 0) <= 0:
        return estado == "EJECUTADO" or float(alcanzado or 0) > 0
    return float(alcanzado or 0) >= float(comprometido or 0) - 5e-4


def marcar_no_planificadas(creados: list, referencia, ids=None,
                           comprometidos=None) -> list:
    """Cuáles de las actividades de la semana entraron después del compromiso.

    Si la semana tiene su compromiso CONGELADO (`comprometidos` = el conjunto de
    ids que el planner comprometió, § prog_semana_plan) la respuesta es exacta y
    no hay nada que deducir: no planificada = no está en el conjunto. Ese es el
    camino bueno, porque no admite discusión ni requiere desmarcar nada.

    Sin compromiso congelado se cae a la deducción por fecha de creación, con una
    excepción: si TODAS son posteriores, ninguna se marca. Una marca que le cae
    al 100% de las filas no distingue nada — y casi siempre significa que el plan
    se cargó al sistema después de la semana (arranque, carga histórica, datos de
    prueba), no que la obra improvisara siete días seguidos. Marcarlas todas
    dejaría la semana con cero comprometidas y sin PPC, que es peor que no
    avisar: el planner ve un «—» y no sabe por qué.
    """
    if comprometidos is not None and ids is not None:
        return [i not in comprometidos for i in ids]
    v = [es_no_planificada(c, referencia) for c in creados]
    return [False] * len(v) if v and all(v) else v


def denominador_comprometido(vigente: dict, congelado=None) -> dict:
    """Metrado que se juzga en la semana, actividad por actividad (0041).

    `vigente` es lo que hoy dice `prog_metrado_dia`; `congelado` lo que quedó
    guardado al comprometer la semana, o None si nunca se comprometió.

    Con la semana comprometida MANDA el congelado, y eso es lo que cierra D1/D2:
    el denominador del PPC deja de depender del plan de hoy, así que correr la
    F.Inicio de una actividad que no se hizo ya no la saca del indicador ni
    programar hacia atrás lo reescribe.

    Un metrado congelado en 0 significa «este compromiso es anterior a 0041»
    —no «se prometió cero»— y cae al vigente, que es el comportamiento de
    siempre. Sin esa salida, las semanas ya comprometidas se quedarían sin
    denominador y su PPC pasaría a None de golpe al desplegar.

    Las actividades vigentes que NO están en el compromiso se conservan con su
    metrado de hoy: se las juzga como no planificadas (§ marcar_no_planificadas)
    y sacarlas de esta lista las borraría también del indicador de trabajo no
    planificado y de sus HH, que es justo lo que se quiere medir.
    """
    if congelado is None:
        return {k: float(v or 0) for k, v in vigente.items()}
    out = {}
    for aid, m in congelado.items():
        m = float(m or 0)
        out[aid] = m if m > 0 else float(vigente.get(aid, 0) or 0)
    for aid, m in vigente.items():
        out.setdefault(aid, float(m or 0))
    return out


def origen_denominador(congelado=None, cerrada: bool = False) -> str:
    """Contra qué plan se está midiendo el PPC de la semana.

    · CERRADA      — el veredicto ya está congelado y no se recalcula.
    · COMPROMETIDO — el denominador no se mueve aunque se reprograme.
    · VIGENTE      — se recalcula con el plan de hoy: todavía puede cambiar.

    La UI tiene que decirlo. Un PPC sobre plan vigente no es una promesa medida,
    es una foto provisional, y presentarlos igual fue exactamente el problema.
    """
    if cerrada:
        return "CERRADA"
    return "COMPROMETIDO" if congelado is not None else "VIGENTE"


def entra_al_cierre(comprometido: float, fecha_inicio, lunes: date) -> bool:
    """¿Esta actividad se juzga al cerrar esta semana?

    Con metrado programado en la ventana, sí. Sin metrado —actividad de apoyo, o
    una a la que el re-prorrateo no le dejó saldo— se juzga en la semana de su
    F.Inicio, que es exactamente lo que hace `/ppc`. El cierre las descartaba, y
    por eso cerrar movía el PPC que el planner acababa de mirar: el congelado
    salía calculado sobre menos actividades que el propuesto.
    """
    if float(comprometido or 0) > 0:
        return True
    return fecha_inicio is not None and lunes_de(fecha_inicio) == lunes


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
    # También en la semana cerrada: una cuyo compromiso se congeló en la reunión
    # es más defendible que una cuya marca se dedujo de fechas de creación, y eso
    # hay que poder verlo después, no solo antes de cerrar.
    plan = await con.fetchrow(
        "SELECT comprometido_en FROM prog_semana_plan WHERE proyecto_id=$1 AND lunes=$2",
        proyecto_id, lun)
    return {
        "cerrada": True, "lunes": str(cab["lunes"]), "hasta": str(cab["hasta"]),
        "compromiso": "congelado" if plan else "deducido",
        "comprometido_en": str(plan["comprometido_en"]) if plan else None,
        "sin_clasificar": sum(1 for r in det
                              if r["no_planificada"] and not r["no_plan_motivo"]),
        "parcial": cab["parcial"], "cerrado_en": str(cab["cerrado_en"]),
        "cerrado_por": cab["cerrado_por"], "nota": cab["nota"],
        "comprometidas": cab["comprometidas"], "cumplidas": cab["cumplidas"],
        "no_cumplidas": cab["no_cumplidas"], "no_planificadas": cab["no_planificadas"],
        "ppc": (round(cab["cumplidas"] / cab["comprometidas"], 4)
                if cab["comprometidas"] else None),
        "hh_total": float(cab["hh_total"] or 0),
        "hh_no_planificadas": float(cab["hh_no_planificadas"] or 0),
        "hh_sin_actividad": float(cab["hh_sin_actividad"] or 0),
        "ratio_no_planificado": ratio_no_planificado(
            cab["hh_no_planificadas"], cab["hh_total"]),
        "motivos": NO_PLAN_MOTIVOS,
        "actividades": [{
            "actividad_id": r["actividad_id"], "titulo": r["titulo"],
            "partida_id": r["partida_id"], "supervisor_id": r["supervisor_id"],
            "supervisor_nombre": r["supervisor_nombre"],
            "comprometido": float(r["comprometido"] or 0),
            "alcanzado": float(r["alcanzado"] or 0),
            "cumplida": r["cumplida"], "no_planificada": r["no_planificada"],
            "no_plan_motivo": r["no_plan_motivo"],
            "no_plan_desplaza_a": r["no_plan_desplaza_a"],
            "no_plan_desplaza_tit": r["no_plan_desplaza_tit"],
            # Si el sistema propuso otra cosa, alguien lo cambió al cerrar:
            # con cerrado_por/cerrado_en queda quién y cuándo.
            "no_plan_auto": r["no_plan_auto"],
            "no_plan_editada": bool(r["no_plan_auto"]) != bool(r["no_planificada"]),
            "hh": float(r["hh"] or 0),
            "causa_cat": r["causa_cat"], "causa": r["causa"],
        } for r in det],
    }


async def _hh_semana(con, proyecto_id: int, lun: date, hasta: date,
                     actividades: list) -> tuple:
    """HH del tareo de la semana: por actividad, del proyecto, y las huérfanas.

    El total se toma sobre TODAS las partidas del proyecto y no solo las que
    tienen actividad: el denominador del indicador tiene que ser las horas de la
    obra en la semana, no las horas que alguien acordó programar.
    """
    filas = await con.fetch(
        """SELECT tp.partida_id, tp.supervisor_id, tp.fecha, SUM(tp.hh) AS hh
             FROM tareo_partida tp
             JOIN ev_partidas p ON p.id = tp.partida_id
             JOIN otms o ON o.id = p.otm_id
            WHERE o.proyecto_id = $1 AND tp.fecha BETWEEN $2 AND $3
            GROUP BY 1,2,3""", proyecto_id, lun, hasta)
    claves = {(r["partida_id"], r["supervisor_id"], r["fecha"]): float(r["hh"] or 0)
              for r in filas}
    total = round(sum(claves.values()), 2)
    por_act, huerfanas = atribuir_hh(claves, actividades)
    return por_act, total, huerfanas


# ── Compromiso congelado de la semana (0040) ─────────────────────────────
async def _comprometidos(con, proyecto_id: int, lun: date):
    """El compromiso congelado de la semana: ({actividad_id: metrado}, plan).

    None si la semana nunca se comprometió — distinto de un dict vacío, que es
    «se comprometió sin nada» y sí congela un denominador (vacío).

    Devuelve un dict y no un set porque desde 0041 el compromiso incluye el
    CUÁNTO. Donde solo importa la pertenencia (`marcar_no_planificadas`,
    `en_plan`) el dict se comporta igual que el set que había antes.
    """
    plan = await con.fetchrow(
        "SELECT id, comprometido_en, comprometido_por, nota FROM prog_semana_plan "
        "WHERE proyecto_id=$1 AND lunes=$2", proyecto_id, lun)
    if not plan:
        return None, None
    comp = {r["actividad_id"]: float(r["metrado"] or 0) for r in await con.fetch(
        "SELECT actividad_id, metrado FROM prog_semana_plan_det WHERE plan_id=$1",
        plan["id"])}
    return comp, dict(plan)


async def _evento(con, proyecto_id: int, lun: date, evento: str, actor=None,
                  n: int = 0, metrado: float = 0, ppc=None, nota=None,
                  detalle=None) -> None:
    """Anota un hito de la semana en la bitácora (0041).

    Append-only y a propósito FUERA de cualquier `try`: si el evento no se puede
    escribir, la operación entera falla. Una bitácora con huecos silenciosos es
    peor que no tenerla — se leería como «esta semana nunca se reabrió».
    """
    await con.execute(
        """INSERT INTO prog_semana_eventos
             (proyecto_id, lunes, evento, actor, n_actividades, metrado, ppc,
              nota, detalle)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        proyecto_id, lun, evento, actor, int(n or 0), float(metrado or 0), ppc,
        nota, json.dumps(detalle) if detalle is not None else None)


@router.get("/plan-semana")
async def ver_plan_semana(lunes: str = "", proyecto_id: int = 1):
    """¿Está comprometida esta semana, y con qué actividades?

    Sin compromiso devuelve la PROPUESTA: lo que hoy está programado en la
    semana. Es lo que el planner confirma en la reunión."""
    lun = _lunes_arg(lunes)
    pool = await db()
    async with pool.acquire() as con:
        cfg = await leer_config_cierre(con, proyecto_id)
        _corte, hasta, _p = ventana_corte(lun, **_kw(cfg))
        # Comprometida: se muestra el metrado CONGELADO, que es contra el que se
        # va a juzgar. Mostrar el vigente aquí sería enseñar un compromiso que
        # el planner nunca hizo (§ denominador_comprometido).
        comp, plan = await _comprometidos(con, proyecto_id, lun)
        filas = await _detalle_semana(con, proyecto_id, lun, hasta, comp)
        juzgadas = [f for f in filas if entra_al_cierre(f["comprometido"], f["fecha"], lun)]
    return {
        "lunes": str(lun), "comprometida": plan is not None,
        "comprometido_en": str(plan["comprometido_en"]) if plan else None,
        "comprometido_por": plan["comprometido_por"] if plan else None,
        "nota": plan["nota"] if plan else None,
        "origen": origen_denominador(comp),
        # Cuántas del compromiso congelado ya no tienen metrado en el plan de
        # hoy: es la señal de que alguien reprogramó lo comprometido. El PPC ya
        # no se mueve por eso (0041), pero el planner debería enterarse.
        "reprogramadas": sum(1 for f in juzgadas
                             if comp and f["actividad_id"] in comp
                             and not f["comprometido_vigente"]),
        "actividades": [{
            "actividad_id": f["actividad_id"], "titulo": f["titulo"],
            "supervisor_id": f["supervisor_id"], "unidad": f["unidad"],
            "comprometido": f["comprometido"],
            "comprometido_vigente": f["comprometido_vigente"],
            "en_plan": (f["actividad_id"] in comp) if comp is not None else True,
        } for f in juzgadas],
    }


@router.post("/plan-semana")
async def comprometer_semana(data: dict):
    """Congela el compromiso de la semana: a partir de aquí, lo que entre es
    trabajo no planificado POR CONSTRUCCIÓN.

    Es el reemplazo del proxy «nació después del lunes», que tenía dos fallas
    opuestas: marcaba como no planificado un plan acordado el lunes y tecleado
    el martes, y permitía desmarcar al cerrar —convirtiendo la omisión propia en
    compromiso cumplido— sin dejar rastro.

    Desde 0041 se congela también el METRADO de cada actividad, no solo el
    conjunto: sin el cuánto, el denominador del PPC seguía saliendo del plan
    vigente y correr una F.Inicio sacaba del indicador a la actividad que no se
    hizo (D1). Y el acto queda en la bitácora — re-comprometer sobrescribe el
    plan, así que sin ella no habría forma de saber qué se prometió primero.

    Body: {proyecto_id?, lunes, actividades?: [id], nota?}. Sin `actividades` se
    compromete todo lo que hoy está programado en la semana.
    """
    proyecto_id = int(data.get("proyecto_id") or 1)
    lun = _lunes_arg(str(data.get("lunes") or ""))
    nota = (str(data.get("nota") or "").strip() or None)
    por = (str(data.get("comprometido_por") or "").strip() or None)
    pedidas = data.get("actividades")
    pool = await db()
    async with pool.acquire() as con:
        if await con.fetchval("SELECT 1 FROM prog_semana_cierre "
                              "WHERE proyecto_id=$1 AND lunes=$2", proyecto_id, lun):
            raise HTTPException(409, "Esa semana ya está cerrada")
        cfg = await leer_config_cierre(con, proyecto_id)
        _corte, hasta, _p = ventana_corte(lun, **_kw(cfg))
        # Sin `congelado`: aquí se lee el plan VIGENTE a propósito, que es
        # justamente lo que se va a congelar. Pasarle el compromiso anterior
        # haría que re-comprometer copiara el metrado viejo.
        filas = await _detalle_semana(con, proyecto_id, lun, hasta)
        juzgadas = [f for f in filas if entra_al_cierre(f["comprometido"], f["fecha"], lun)]
        disponibles = {f["actividad_id"]: f for f in juzgadas}
        if pedidas is None:
            ids = sorted(disponibles)
        else:
            ids = sorted({int(x) for x in pedidas} & set(disponibles))
        # El metrado que se congela, actividad por actividad (0041).
        metrados = {i: float(disponibles[i]["comprometido"] or 0) for i in ids}
        total = round(sum(metrados.values()), 3)
        async with con.transaction():
            plan_id = await con.fetchval(
                """INSERT INTO prog_semana_plan (proyecto_id, lunes, comprometido_por, nota)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (proyecto_id, lunes) DO UPDATE
                     SET comprometido_en = now(), comprometido_por = EXCLUDED.comprometido_por,
                         nota = EXCLUDED.nota
                   RETURNING id""", proyecto_id, lun, por, nota)
            await con.execute("DELETE FROM prog_semana_plan_det WHERE plan_id=$1", plan_id)
            if ids:
                await con.executemany(
                    "INSERT INTO prog_semana_plan_det (plan_id, actividad_id, metrado)"
                    " VALUES ($1,$2,$3)",
                    [(plan_id, i, metrados[i]) for i in ids])
            await _evento(
                con, proyecto_id, lun, "COMPROMETIDA", por, len(ids), total,
                nota=nota,
                detalle=[{"id": i, "titulo": disponibles[i]["titulo"],
                          "metrado": metrados[i],
                          "unidad": disponibles[i]["unidad"]} for i in ids])
    log.info("semana comprometida", extra={"lunes": str(lun), "n": len(ids),
                                           "metrado": total})
    return {"ok": True, "lunes": str(lun), "comprometidas": len(ids),
            "metrado_comprometido": total}


@router.delete("/plan-semana")
async def descomprometer_semana(lunes: str = "", proyecto_id: int = 1,
                                actor: str = "", nota: str = ""):
    """Deshace el compromiso (solo si la semana no está cerrada).

    Borra el plan pero NO la bitácora: queda el COMPROMETIDA original y el
    DESCOMPROMETIDA que lo deshizo. Es el gesto que más podría usarse para
    maquillar el indicador —descomprometer, reprogramar y volver a comprometer
    con lo que sí se hizo—, así que tiene que dejar rastro."""
    lun = _lunes_arg(lunes)
    pool = await db()
    async with pool.acquire() as con:
        if await con.fetchval("SELECT 1 FROM prog_semana_cierre "
                              "WHERE proyecto_id=$1 AND lunes=$2", proyecto_id, lun):
            raise HTTPException(409, "Esa semana ya está cerrada: reábrela primero")
        comp, _plan = await _comprometidos(con, proyecto_id, lun)
        async with con.transaction():
            n = await con.execute("DELETE FROM prog_semana_plan "
                                  "WHERE proyecto_id=$1 AND lunes=$2", proyecto_id, lun)
            if comp is not None:
                await _evento(con, proyecto_id, lun, "DESCOMPROMETIDA",
                              str(actor or "").strip() or None, len(comp),
                              round(sum(comp.values()), 3),
                              nota=str(nota or "").strip() or None)
    return {"ok": True, "borradas": n}


async def _referencia_campo(con, proyecto_id: int, lun: date, hasta: date,
                            act_ids: list) -> dict:
    """Lo que campo dejó escrito esa semana, actividad por actividad.

    Es REFERENCIA, no dato del indicador: la causa que reportó el supervisor el
    día que no salió y las restricciones que anotó aunque el trabajo sí se
    hiciera. El planner las lee para redactar la explicación del cliente en vez
    de escribir «no se pudo» de memoria.
    """
    rest = await restricciones_semana(con, proyecto_id, lun, hasta)
    causas: dict = {}
    if act_ids:
        for r in await con.fetch(
                """SELECT id, causa_nc_cat, causa_nc FROM prog_actividades
                    WHERE id = ANY($1) AND (causa_nc_cat IS NOT NULL OR causa_nc IS NOT NULL)""",
                list(act_ids)):
            causas[r["id"]] = {"cat": r["causa_nc_cat"], "detalle": r["causa_nc"]}
    out: dict = {}
    for aid in act_ids:
        c, rs = causas.get(aid), rest.get(aid) or []
        if c or rs:
            out[str(aid)] = {"causa": c, "restricciones": rs}
    return out


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
            # La referencia de campo sigue viva aunque el veredicto esté
            # congelado: el número no cambia, pero el sustento se puede releer.
            campo = await _referencia_campo(
                con, proyecto_id, lun, parse_fecha(ya["hasta"]) or lun,
                [a["actividad_id"] for a in ya["actividades"] if a["actividad_id"]])
            return {**ya, "cnc_catalogo": CNC, "campo": campo}
        cfg = await leer_config_cierre(con, proyecto_id)
        corte, hasta, parcial = ventana_corte(lun, **_kw(cfg))
        # El compromiso congelado manda sobre la deducción por fecha (0040) y,
        # desde 0041, también sobre el metrado: lo que se cierra es lo que se
        # prometió, no lo que el plan diga hoy.
        comprom_ids, plan = await _comprometidos(con, proyecto_id, lun)
        filas = await _detalle_semana(con, proyecto_id, lun, hasta, comprom_ids)
        # Referencia del compromiso: el corte de la semana ANTERIOR si esa
        # semana se cerró; si no, el lunes de esta.
        prev = await con.fetchval(
            "SELECT hasta FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
            proyecto_id, lun - timedelta(days=7))
        referencia = prev or lun
        nombres = {r["id"]: r["nombre"] for r in await con.fetch(
            "SELECT id, nombre FROM supervisores")}
        juzgadas = [f for f in filas if entra_al_cierre(f["comprometido"], f["fecha"], lun)]
        campo = await _referencia_campo(
            con, proyecto_id, lun, hasta, [f["actividad_id"] for f in juzgadas])
        hh_act, hh_total, hh_huerf = await _hh_semana(
            con, proyecto_id, lun, hasta,
            [{"id": f["actividad_id"], "partida_id": f["partida_id"],
              "supervisor_id": f["supervisor_id"], "fecha": f["fecha"],
              "fecha_fin": f["fecha_fin"]} for f in juzgadas])
    no_plan = marcar_no_planificadas(
        [f["creado_en"] for f in juzgadas], referencia,
        [f["actividad_id"] for f in juzgadas], comprom_ids)
    props = []
    for f, np in zip(juzgadas, no_plan):
        props.append({
            **{k: v for k, v in f.items() if k not in ("creado_en", "fecha", "fecha_fin")},
            "supervisor_nombre": nombres.get(f["supervisor_id"]),
            "cumplida": veredicto(f["comprometido"], f["alcanzado"], f["estado"]),
            "no_planificada": np,
            "hh": hh_act.get(f["actividad_id"], 0.0),
        })
    comprometidas = [p for p in props if not p["no_planificada"]]
    cumplidas = [p for p in comprometidas if p["cumplida"]]
    hh_no_plan = round(sum(p["hh"] for p in props
                          if cuenta_como_improvisacion(p["no_planificada"],
                                                       p["no_plan_motivo"])), 2)
    return {
        "cerrada": False, "lunes": str(lun), "corte": str(corte), "hasta": str(hasta),
        "parcial": parcial, "hoy": str(fecha_lima()),
        "puede_cerrarse": fecha_lima() >= corte,
        "referencia_compromiso": str(referencia),
        # De dónde sale la marca de «no planificada»: del compromiso congelado
        # (exacto) o de la fecha de creación (deducido). La UI lo dice, porque
        # cambia cuánto se puede discutir el número.
        "compromiso": "congelado" if comprom_ids is not None else "deducido",
        "comprometido_en": str(plan["comprometido_en"]) if plan else None,
        # Contra qué plan se está midiendo, y cuántas comprometidas ya no tienen
        # metrado en el plan de hoy (alguien las reprogramó o las canceló
        # después de prometerlas). El PPC ya no se mueve por eso (0041), pero es
        # justo lo que el planner tiene que mirar antes de cerrar.
        "origen": origen_denominador(comprom_ids),
        "reprogramadas": sum(1 for p in props
                             if comprom_ids and p["actividad_id"] in comprom_ids
                             and not p["comprometido_vigente"]),
        "comprometidas": len(comprometidas), "cumplidas": len(cumplidas),
        "no_cumplidas": len(comprometidas) - len(cumplidas),
        "no_planificadas": sum(1 for p in props if p["no_planificada"]),
        "sin_clasificar": sum(1 for p in props
                              if p["no_planificada"] and not p["no_plan_motivo"]),
        "ppc": (round(len(cumplidas) / len(comprometidas), 4) if comprometidas else None),
        "hh_total": hh_total, "hh_no_planificadas": hh_no_plan,
        "hh_sin_actividad": hh_huerf,
        "ratio_no_planificado": ratio_no_planificado(hh_no_plan, hh_total),
        "motivos": NO_PLAN_MOTIVOS,
        "actividades": props, "cnc_catalogo": CNC, "campo": campo, **cfg,
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
        # El mismo compromiso que vio el planner en la propuesta: si aquí se
        # calculara distinto, cerraría algo diferente de lo que revisó.
        comprom_ids, _plan = await _comprometidos(con, proyecto_id, lun)
        filas = await _detalle_semana(con, proyecto_id, lun, hasta, comprom_ids)
        prev = await con.fetchval(
            "SELECT hasta FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
            proyecto_id, lun - timedelta(days=7))
        referencia = prev or lun
        juzgadas = [f for f in filas if entra_al_cierre(f["comprometido"], f["fecha"], lun)]
        no_plan_auto = marcar_no_planificadas(
            [f["creado_en"] for f in juzgadas], referencia,
            [f["actividad_id"] for f in juzgadas], comprom_ids)
        hh_act, hh_total, hh_huerf = await _hh_semana(
            con, proyecto_id, lun, hasta,
            [{"id": f["actividad_id"], "partida_id": f["partida_id"],
              "supervisor_id": f["supervisor_id"], "fecha": f["fecha"],
              "fecha_fin": f["fecha_fin"]} for f in juzgadas])
        det = []
        for f, np_auto in zip(juzgadas, no_plan_auto):
            aj = ajustes.get(f["actividad_id"], {})
            cumplida = bool(aj["cumplida"]) if "cumplida" in aj else veredicto(
                f["comprometido"], f["alcanzado"], f["estado"])
            no_plan = (bool(aj["no_planificada"]) if "no_planificada" in aj
                       else np_auto)
            # Lo que el planner escriba manda; si no toca nada, se conserva la
            # causa que la actividad ya traía de la evaluación semanal.
            cat = str(aj.get("causa_cat") if "causa_cat" in aj
                      else (f.get("causa_cat") or "")).strip().upper() or None
            if cat and cat not in CNC:
                raise HTTPException(400, f"Causa desconocida: {cat}")
            texto = str(aj.get("causa") if "causa" in aj
                        else (f.get("causa") or "")).strip() or None
            motivo = validar_motivo_no_plan(
                aj["no_plan_motivo"] if "no_plan_motivo" in aj else f["no_plan_motivo"])
            det.append({**f, "cumplida": cumplida, "no_planificada": no_plan,
                        "no_plan_auto": np_auto, "no_plan_motivo": motivo,
                        "hh": hh_act.get(f["actividad_id"], 0.0),
                        "causa_cat": cat, "causa": texto})
        comprometidas = [d for d in det if not d["no_planificada"]]
        cumplidas = sum(1 for d in comprometidas if d["cumplida"])
        hh_no_plan = round(sum(d["hh"] for d in det
                              if cuenta_como_improvisacion(d["no_planificada"],
                                                           d["no_plan_motivo"])), 2)
        async with con.transaction():
            cid = await con.fetchval(
                """INSERT INTO prog_semana_cierre
                     (proyecto_id, lunes, hasta, parcial, comprometidas, cumplidas,
                      no_cumplidas, no_planificadas, cerrado_por, nota,
                      hh_total, hh_no_planificadas, hh_sin_actividad)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id""",
                proyecto_id, lun, hasta, parcial, len(comprometidas), cumplidas,
                len(comprometidas) - cumplidas, len(det) - len(comprometidas),
                str(data.get("cerrado_por") or "").strip() or None,
                str(data.get("nota") or "").strip() or None,
                hh_total, hh_no_plan, hh_huerf)
            for d in det:
                await con.execute(
                    """INSERT INTO prog_semana_cierre_det
                         (cierre_id, actividad_id, titulo, partida_id, supervisor_id,
                          comprometido, alcanzado, cumplida, no_planificada,
                          causa_cat, causa, no_plan_motivo, no_plan_desplaza_a,
                          no_plan_desplaza_tit, no_plan_auto, hh)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
                    cid, d["actividad_id"], d["titulo"], d["partida_id"],
                    d["supervisor_id"], d["comprometido"], d["alcanzado"],
                    d["cumplida"], d["no_planificada"], d["causa_cat"], d["causa"],
                    d["no_plan_motivo"], d["no_plan_desplaza_a"],
                    d["no_plan_desplaza_tit"], d["no_plan_auto"], d["hh"])
            await _evento(
                con, proyecto_id, lun, "CERRADA",
                str(data.get("cerrado_por") or "").strip() or None,
                len(comprometidas), round(sum(d["comprometido"] for d in comprometidas), 3),
                ppc=(round(cumplidas / len(comprometidas), 4) if comprometidas else None),
                nota=str(data.get("nota") or "").strip() or None,
                detalle={"hasta": str(hasta), "parcial": parcial,
                         "cumplidas": cumplidas,
                         "no_cumplidas": len(comprometidas) - cumplidas,
                         "no_planificadas": len(det) - len(comprometidas),
                         # Cuántos veredictos difieren de lo que el sistema
                         # propuso: es la medida de cuánto se corrigió a mano.
                         "ajustes": sum(1 for d in det
                                        if d["no_planificada"] != d["no_plan_auto"])})
    log.info("semana cerrada", extra={"proyecto": proyecto_id, "lunes": str(lun),
                                      "comprometidas": len(comprometidas),
                                      "cumplidas": cumplidas,
                                      "hh_no_planificadas": hh_no_plan})
    return {"ok": True, "lunes": str(lun), "hasta": str(hasta), "parcial": parcial,
            "comprometidas": len(comprometidas), "cumplidas": cumplidas,
            "no_cumplidas": len(comprometidas) - cumplidas,
            "no_planificadas": len(det) - len(comprometidas),
            "hh_total": hh_total, "hh_no_planificadas": hh_no_plan,
            "ratio_no_planificado": ratio_no_planificado(hh_no_plan, hh_total),
            "ppc": round(cumplidas / len(comprometidas), 4) if comprometidas else None}


# ── Panel de trabajo no planificado (0040) ───────────────────────────────
async def _no_plan_de_semana(con, proyecto_id: int, lun: date) -> tuple:
    """Las no planificadas de UNA semana + las comprometidas que no cumplieron.

    Si la semana está cerrada manda lo congelado (§ prog_semana_cierre); si no,
    se calcula igual que la propuesta del cierre. Las no cumplidas viajan porque
    son las candidatas naturales cuando el planner dice a quién desplazó este
    trabajo: la cuadrilla salió de alguna de ellas.
    """
    cab = await con.fetchrow(
        "SELECT id, hasta FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
        proyecto_id, lun)
    if cab:
        det = await con.fetch(
            """SELECT actividad_id, titulo, partida_id, supervisor_id, hh,
                      no_planificada, cumplida, no_plan_motivo, no_plan_desplaza_a,
                      no_plan_desplaza_tit, comprometido, alcanzado
                 FROM prog_semana_cierre_det WHERE cierre_id=$1""", cab["id"])
        np = [dict(r) for r in det if r["no_planificada"] and r["actividad_id"]]
        fallidas = [{"actividad_id": r["actividad_id"], "titulo": r["titulo"]}
                    for r in det if not r["no_planificada"] and not r["cumplida"]
                    and r["actividad_id"]]
        return np, fallidas, cab["hasta"], True

    cfg = await leer_config_cierre(con, proyecto_id)
    _corte, hasta, _p = ventana_corte(lun, **_kw(cfg))
    # Mismo compromiso congelado que usan el cierre y el PPC: las fallidas que
    # este panel ofrece como «a quién desplazó» tienen que ser las mismas que el
    # PPC cuenta como no cumplidas.
    comprom_ids, _plan = await _comprometidos(con, proyecto_id, lun)
    filas = await _detalle_semana(con, proyecto_id, lun, hasta, comprom_ids)
    juzgadas = [f for f in filas if entra_al_cierre(f["comprometido"], f["fecha"], lun)]
    if not juzgadas:
        return [], [], hasta, False
    prev = await con.fetchval(
        "SELECT hasta FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
        proyecto_id, lun - timedelta(days=7))
    marcas = marcar_no_planificadas(
        [f["creado_en"] for f in juzgadas], prev or lun,
        [f["actividad_id"] for f in juzgadas], comprom_ids)
    hh_act, _tot, _h = await _hh_semana(
        con, proyecto_id, lun, hasta,
        [{"id": f["actividad_id"], "partida_id": f["partida_id"],
          "supervisor_id": f["supervisor_id"], "fecha": f["fecha"],
          "fecha_fin": f["fecha_fin"]} for f in juzgadas])
    np, fallidas = [], []
    for f, m in zip(juzgadas, marcas):
        if m:
            np.append({**f, "hh": hh_act.get(f["actividad_id"], 0.0)})
        elif not veredicto(f["comprometido"], f["alcanzado"], f["estado"]):
            fallidas.append({"actividad_id": f["actividad_id"], "titulo": f["titulo"]})
    return np, fallidas, hasta, False


@router.get("/no-planificadas")
async def panel_no_planificadas(proyecto_id: int = 1, semanas: int = 4,
                                p_desde: str = Query("", alias="desde"),
                                p_hasta: str = Query("", alias="hasta")):
    """Bandeja de trabajo no planificado, con el parte y las FOTOS de campo.

    Encargo de Jean (2026-07-30): el supervisor reporta una partida que el
    planner no programó y ese parte —lo que se hizo, qué lo frenó, las fotos—
    se queda en la app de campo. Aquí llega a oficina para lo único que hace
    falta hacer con él: clasificar POR QUÉ entró y A QUIÉN desplazó, que es lo
    que convierte «3 no planificadas» en un dato que se puede atacar.

    No cambia el PPC: estas actividades siguen fuera. Alimentan el indicador de
    HH no planificadas y el Pareto de motivos.
    """
    hoy = fecha_lima()
    desde, hasta_rng = ventana_ppc(hoy, max(1, min(int(semanas or 4), 26)),
                                  p_desde, p_hasta)
    pool = await db()
    async with pool.acquire() as con:
        nombres = {r["id"]: r["nombre"] for r in await con.fetch(
            "SELECT id, nombre FROM supervisores")}
        semanas_out = []
        todos: list = []
        lun = lunes_de(desde)
        while lun <= hasta_rng:
            np, fallidas, hasta_sem, cerrada = await _no_plan_de_semana(con, proyecto_id, lun)
            if np:
                semanas_out.append({
                    "lunes": str(lun), "hasta": str(hasta_sem), "cerrada": cerrada,
                    "candidatas": fallidas,
                    "actividades": [a["actividad_id"] for a in np],
                })
                todos.extend(np)
            lun = lun + timedelta(days=7)

        ids = [a["actividad_id"] for a in todos]
        reps, fotos, personal = {}, {}, {}
        if ids:
            for r in await con.fetch(
                    """SELECT r.*, s.nombre AS supervisor_nombre, o.area AS otm_area
                         FROM campo_reportes r
                         LEFT JOIN supervisores s ON s.id = r.supervisor_id
                         LEFT JOIN otms o ON o.id = r.otm_id
                        WHERE r.actividad_id = ANY($1) ORDER BY r.fecha, r.id""", ids):
                reps.setdefault(r["actividad_id"], []).append(dict(r))
            for f in await con.fetch(
                    """SELECT f.*, r.actividad_id FROM campo_fotos f
                         JOIN campo_reportes r ON r.id = f.reporte_id
                        WHERE r.actividad_id = ANY($1) ORDER BY f.id""", ids):
                fotos.setdefault(f["reporte_id"], []).append(dict(f))
            for r in await con.fetch(
                    """SELECT tp.fecha, tp.supervisor_id, tp.partida_id,
                              COALESCE(t.cargo,'SIN CARGO') AS cargo,
                              COUNT(DISTINCT tp.trabajador_id) AS n
                         FROM tareo_partida tp
                         LEFT JOIN trabajadores t ON t.id = tp.trabajador_id
                        WHERE tp.partida_id = ANY($1) AND tp.fecha BETWEEN $2 AND $3
                        GROUP BY 1,2,3,4 ORDER BY n DESC""",
                    [a["partida_id"] for a in todos if a["partida_id"]],
                    desde, hasta_rng):
                personal.setdefault((r["fecha"], r["supervisor_id"], r["partida_id"]),
                                    []).append({"cargo": r["cargo"], "n": r["n"]})

    out = []
    for a in todos:
        bloques = []
        for r in reps.get(a["actividad_id"], []):
            notas = json.loads(r["anotaciones"]) if r["anotaciones"] else [
                ln.lstrip("•- ").strip()
                for ln in (r["descripcion"] or "").split("\n") if ln.strip()]
            rest = json.loads(r["restricciones"]) if r["restricciones"] else []
            per = personal.get((r["fecha"], r["supervisor_id"], a["partida_id"]), [])
            bloques.append({
                "id": r["id"], "fecha": str(r["fecha"]), "turno": r["turno"],
                "frente": r["frente"],
                "supervisor": r["supervisor_nombre"] or r["supervisor_id"],
                "anotaciones": notas, "restricciones": rest,
                "texto": armar_texto_reporte(
                    r["fecha"], r["turno"] or "DIA",
                    r["supervisor_nombre"] or r["supervisor_id"], per,
                    [{"area": r["area"] or r["otm_area"] or "",
                      "frente": r["frente"], "items": notas}], rest),
                "fotos": [_foto_out(f) for f in fotos.get(r["id"], [])],
            })
        out.append({
            "actividad_id": a["actividad_id"], "titulo": a["titulo"],
            "partida_id": a["partida_id"], "partida_codigo": a.get("partida_codigo"),
            "supervisor_id": a["supervisor_id"],
            "supervisor_nombre": nombres.get(a["supervisor_id"]),
            "comprometido": float(a["comprometido"] or 0),
            "alcanzado": float(a["alcanzado"] or 0),
            "unidad": a.get("unidad"), "hh": float(a["hh"] or 0),
            "no_plan_motivo": a["no_plan_motivo"],
            "no_plan_desplaza_a": a["no_plan_desplaza_a"],
            "no_plan_desplaza_tit": a["no_plan_desplaza_tit"],
            # Lo que hace falta hacer con esta fila. Es el contador del panel.
            "pendiente": not a["no_plan_motivo"],
            "reportes": bloques,
            "sin_parte": not bloques,
        })
    return {
        "desde": str(desde), "hasta": str(hasta_rng), "motivos": NO_PLAN_MOTIVOS,
        "improvisacion": list(MOTIVOS_IMPROVISACION),
        "semanas": semanas_out, "actividades": out,
        "pendientes": sum(1 for a in out if a["pendiente"]),
    }


@router.put("/no-planificadas/{act_id}")
async def clasificar_no_planificada(act_id: int, data: dict):
    """Clasifica una no planificada: por qué entró y a qué comprometida desplazó.

    Body: {motivo?, desplaza_a?}. `null` en cualquiera de los dos la deja sin
    clasificar otra vez. No toca el PPC ni el veredicto — solo el indicador de
    trabajo no planificado y el Pareto de motivos.
    """
    motivo = validar_motivo_no_plan(data.get("motivo"))
    desplaza = data.get("desplaza_a")
    desplaza = int(desplaza) if desplaza not in (None, "", 0) else None
    if desplaza == act_id:
        raise HTTPException(422, "Una actividad no puede desplazarse a sí misma")
    pool = await db()
    async with pool.acquire() as con:
        if not await con.fetchval("SELECT 1 FROM prog_actividades WHERE id=$1", act_id):
            raise HTTPException(404, "Actividad no encontrada")
        if desplaza and not await con.fetchval(
                "SELECT 1 FROM prog_actividades WHERE id=$1", desplaza):
            raise HTTPException(422, "La actividad desplazada no existe")
        await con.execute(
            "UPDATE prog_actividades SET no_plan_motivo=$2, no_plan_desplaza_a=$3 "
            "WHERE id=$1", act_id, motivo, desplaza)
    return {"ok": True, "actividad_id": act_id, "motivo": motivo,
            "desplaza_a": desplaza}


def resumir_historial(eventos: list) -> dict:
    """Lectura rápida de una bitácora: cuántas veces se comprometió, se cerró y
    se reabrió, y si el PPC publicado llegó a cambiar. PURA (testeable).

    `reaperturas` es el número que importa: una semana que se cerró tres veces
    tuvo tres PPC distintos, y quien lea el indicador tiene derecho a saberlo.
    `ppc_cambio` compara el primer PPC congelado con el último — si difieren,
    el número que se publicó en su día no es el que se ve hoy.
    """
    conteo = {k: 0 for k in EVENTOS}
    ppcs = []
    for e in eventos:
        ev = e.get("evento")
        if ev in conteo:
            conteo[ev] += 1
        if ev == "CERRADA" and e.get("ppc") is not None:
            ppcs.append(float(e["ppc"]))
    return {
        "eventos": len(eventos),
        "veces_comprometida": conteo["COMPROMETIDA"],
        "veces_cerrada": conteo["CERRADA"],
        "reaperturas": conteo["REABIERTA"],
        "descompromisos": conteo["DESCOMPROMETIDA"],
        "ppc_primero": ppcs[0] if ppcs else None,
        "ppc_ultimo": ppcs[-1] if ppcs else None,
        "ppc_cambio": (round(ppcs[-1] - ppcs[0], 4)
                       if len(ppcs) > 1 and ppcs[-1] != ppcs[0] else None),
    }


@router.get("/semana-historial")
async def semana_historial(lunes: str = "", proyecto_id: int = 1, limite: int = 100):
    """Bitácora de congelamiento: cuándo se comprometió y se cerró cada semana,
    quién lo hizo y qué cambió entre una vez y la siguiente (0041).

    Con `lunes` devuelve la historia de ESA semana en orden cronológico (con el
    detalle congelado de cada evento). Sin `lunes`, los últimos movimientos del
    proyecto — que es la vista de auditoría: si alguien reabrió una semana de
    hace un mes, se ve aquí arriba.

    Es de solo lectura y no existe forma de borrar un evento por API: una
    bitácora que se puede editar no sirve para sustentar un indicador.
    """
    n = max(1, min(int(limite or 100), 500))
    lun = _lunes_arg(lunes) if str(lunes or "").strip() else None
    pool = await db()
    async with pool.acquire() as con:
        if lun is not None:
            rows = await con.fetch(
                """SELECT * FROM prog_semana_eventos
                    WHERE proyecto_id=$1 AND lunes=$2 ORDER BY id""",
                proyecto_id, lun)
        else:
            rows = await con.fetch(
                """SELECT * FROM prog_semana_eventos
                    WHERE proyecto_id=$1 ORDER BY id DESC LIMIT $2""",
                proyecto_id, n)
    eventos = []
    for r in rows:
        d = r["detalle"]
        if isinstance(d, str):                 # asyncpg devuelve JSONB como texto
            try:
                d = json.loads(d)
            except (ValueError, TypeError):
                d = None
        eventos.append({
            "id": r["id"], "lunes": str(r["lunes"]), "evento": r["evento"],
            "etiqueta": EVENTOS.get(r["evento"], r["evento"]),
            "actor": r["actor"], "n_actividades": r["n_actividades"],
            "metrado": float(r["metrado"] or 0),
            "ppc": float(r["ppc"]) if r["ppc"] is not None else None,
            "nota": r["nota"], "detalle": d,
            # Eventos reconstruidos por la migración 0041 desde las fechas que
            # ya estaban en BD: la fecha es real, el detalle por actividad no
            # existe porque nunca se guardó.
            "backfill": bool(isinstance(d, dict) and d.get("backfill")),
            "creado_en": str(r["creado_en"]),
        })
    return {"proyecto_id": proyecto_id, "lunes": str(lun) if lun else None,
            "catalogo": EVENTOS, "eventos": eventos,
            "resumen": resumir_historial(eventos) if lun is not None else None}


@router.delete("/cierre-semana")
async def reabrir_semana(lunes: str = "", proyecto_id: int = 1,
                         actor: str = "", nota: str = ""):
    """Reabre una semana cerrada (se equivocaron, faltaba registrar un avance).

    El cierre se borra —cerrar de nuevo vuelve a congelar con los datos de
    ahora— pero el PPC que tenía queda en la bitácora antes de irse. Sin eso,
    reabrir y volver a cerrar podía cambiar un indicador ya publicado sin dejar
    ningún rastro de cuál era antes."""
    lun = _lunes_arg(lunes)
    pool = await db()
    async with pool.acquire() as con:
        prev = await con.fetchrow(
            """SELECT comprometidas, cumplidas, no_cumplidas, no_planificadas,
                      hasta, parcial, cerrado_en, cerrado_por
                 FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2""",
            proyecto_id, lun)
        if not prev:
            raise HTTPException(404, "Esa semana no está cerrada")
        async with con.transaction():
            await con.execute(
                "DELETE FROM prog_semana_cierre WHERE proyecto_id=$1 AND lunes=$2",
                proyecto_id, lun)
            await _evento(
                con, proyecto_id, lun, "REABIERTA",
                str(actor or "").strip() or None, prev["comprometidas"],
                ppc=(round(prev["cumplidas"] / prev["comprometidas"], 4)
                     if prev["comprometidas"] else None),
                nota=str(nota or "").strip() or None,
                detalle={"ppc_que_se_reabre": (
                             round(prev["cumplidas"] / prev["comprometidas"], 4)
                             if prev["comprometidas"] else None),
                         "cumplidas": prev["cumplidas"],
                         "no_cumplidas": prev["no_cumplidas"],
                         "no_planificadas": prev["no_planificadas"],
                         "hasta": str(prev["hasta"]), "parcial": prev["parcial"],
                         "cerrado_en": str(prev["cerrado_en"]),
                         "cerrado_por": prev["cerrado_por"]})
    log.info("semana reabierta", extra={"proyecto": proyecto_id, "lunes": str(lun)})
    return {"ok": True, "lunes": str(lun), "reabierta": True}
