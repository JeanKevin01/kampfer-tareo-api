# ============================================================
# routers/programacion.py — calendario combinado plan/ejecutado (mejoras UX pre-F4)
#
# Oficina (`router`, /ev/programacion): semana del calendario, CRUD de
#   actividades, indicador de almacenamiento por semana y purga manual.
# Campo (`router_campo`, /campo): el supervisor envía su reporte del día
#   (descripción + fotos, opcionalmente contra una actividad programada).
#
# Regla del calendario combinado: cuando llega un reporte vinculado a una
# actividad PROGRAMADO, la actividad pasa sola a EJECUTADO.
# ============================================================
import asyncio
import json
from datetime import date, timedelta
from typing import List, Optional

import asyncpg
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)
from fastapi.responses import Response

from core.auth import exigir_identidad_supervisor, require_role
from core.db import db
from core.log import get_logger
from core.media import (MAX_FOTO_BYTES, MAX_FOTOS_POR_REPORTE, guardar_foto,
                        media_dir, semana_iso_a_lunes, url_firmada)
from core.tiempo import fecha_lima, parse_fecha, semana_de

log = get_logger("programacion")

router = APIRouter(prefix="/ev/programacion", tags=["programacion"])
router_campo = APIRouter(prefix="/campo", tags=["programacion"])

_ESTADOS = ("PROGRAMADO", "EJECUTADO", "CANCELADO", "NO_CUMPLIDA")

# Errores de datos del usuario (FK inexistente, texto demasiado largo…): deben
# responder 400 — un 500 del handler global sale sin headers CORS y el panel
# lo muestra como "Failed to fetch".
_ERRORES_DATO = (asyncpg.IntegrityConstraintViolationError, asyncpg.DataError)


class _ReporteYaExiste(Exception):
    """Dos reintentos del outbox llegaron a la vez con el mismo id_local (F4)."""


def _lista_json(txt: str) -> list:
    """Campo multipart que viaja como JSON (viñetas, restricciones). Tolerante:
    un valor vacío o malformado no tumba el reporte del supervisor."""
    if not (txt or "").strip():
        return []
    try:
        v = json.loads(txt)
    except (ValueError, TypeError):
        return []
    if not isinstance(v, list):
        return []
    return [x for x in v if (isinstance(x, str) and x.strip()) or isinstance(x, dict)]

# Catálogo de Causas de No Cumplimiento (Last Planner): categoría cerrada para
# poder hacer el Pareto; el detalle libre acompaña en causa_nc.
CNC = {
    "MATERIALES": "Falta de materiales",
    "MANO_OBRA": "Falta de mano de obra",
    "EQUIPOS": "Falta de equipos",
    "INFORMACION": "Falta de información / ingeniería",
    "CLIMA": "Clima",
    "INTERFERENCIA": "Interferencia con otra disciplina",
    "PRERREQUISITO": "Prerrequisito no terminado",
    "CLIENTE": "Cambio de prioridad del cliente",
    "PROGRAMACION": "Mala programación / estimación",
    "OTROS": "Otros",
}
_TIPOS_RESTRICCION = ("MATERIALES", "MANO_OBRA", "EQUIPOS", "INFORMACION",
                      "PRERREQUISITO", "PERMISOS", "ESPACIO", "OTROS")


def _validar_cnc(cat) -> Optional[str]:
    c = str(cat or "").strip().upper() or None
    if c and c not in CNC:
        raise HTTPException(422, f"Categoría de causa inválida (usa {'/'.join(CNC)})")
    return c


def _lunes_de(fecha: date) -> date:
    return fecha - timedelta(days=fecha.weekday())


def _distribuir(metrado: float, dias: list, medios: set = frozenset()) -> dict:
    """Distribución del metrado entre los días dados, como la fórmula del
    LookAhead del ex-gerente, con PESOS: un día en `medios` pesa 0.5 (recibe
    la mitad que un día completo, F4 v2). El último día absorbe el redondeo."""
    if not dias:
        return {}
    peso = lambda d: 0.5 if d in medios else 1.0          # noqa: E731
    cuota = metrado / sum(peso(d) for d in dias)
    out, acum = {}, 0.0
    for d in dias[:-1]:
        v = round(cuota * peso(d), 3)
        out[d] = v
        acum += v
    out[dias[-1]] = round(metrado - acum, 3)
    return out


async def _calendario(con, proyecto_id: int, desde: date, hasta: date) -> tuple:
    """(días ISO de la semana que se trabajan, feriados del rango)."""
    ds = await con.fetchval(
        "SELECT dias_semana FROM prog_config WHERE proyecto_id = $1", proyecto_id)
    fer = await con.fetch(
        "SELECT fecha FROM prog_feriados WHERE proyecto_id = $1 AND fecha BETWEEN $2 AND $3",
        proyecto_id, desde, hasta)
    return (set(ds) if ds else {1, 2, 3, 4, 5, 6, 7}), {r["fecha"] for r in fer}


def _dias_habiles(desde: date, hasta: date, dias_semana: set, feriados: set,
                  saltos: set) -> list:
    return [d for i in range((hasta - desde).days + 1)
            if (d := desde + timedelta(days=i)).isoweekday() in dias_semana
            and d not in feriados and d not in saltos]


# ── Plazo (duración) — el dato con el que razona el planner (0034) ──
# El plazo es la duración en DÍAS HÁBILES PONDERADOS del calendario del
# proyecto: día completo 1, medio día 0.5, salto 0. Con él, "arranca el lunes
# y dura 1.5 días" es programable, y la cascada puede mover una actividad
# conservando su duración en vez de deducirla contando celdas.
_EPS = 1e-9
_MAX_PLAZO = 999.0
_MAX_BUSQUEDA = 2000          # tope de días recorridos: evita bucles infinitos
                              # si el calendario no tuviera ningún día hábil


def _es_habil(d: date, dias_semana: set, feriados: set, saltos: set = frozenset()) -> bool:
    return d.isoweekday() in dias_semana and d not in feriados and d not in saltos


def _habil_anterior(d: date, dias_semana: set, feriados: set) -> date:
    """Primer día ESTRICTAMENTE anterior a d que es hábil del calendario."""
    x = d - timedelta(days=1)
    for _ in range(_MAX_BUSQUEDA):
        if _es_habil(x, dias_semana, feriados):
            return x
        x -= timedelta(days=1)
    return x


def _habil_desplazado(d: date, n: int, dias_semana: set, feriados: set) -> date:
    """El día hábil que resulta de moverse n días hábiles desde d.
    n = 0 → el propio d (o el siguiente hábil si d no lo es); n < 0 va atrás.
    Es la base de los lags: un lag de 0 en FS = "el día hábil siguiente al
    fin de la antecesora", que es exactamente el comportamiento anterior."""
    x = d
    for _ in range(_MAX_BUSQUEDA):
        if _es_habil(x, dias_semana, feriados):
            break
        x += timedelta(days=1)
    paso = _siguiente_habil if n > 0 else _habil_anterior
    for _ in range(abs(int(n))):
        x = paso(x, dias_semana, feriados)
    return x


def _plazo_de(desde: date, hasta: date, dias_semana: set, feriados: set,
              saltos: set, medios: set) -> float:
    """Plazo del rango = Σ de los pesos de sus días hábiles."""
    return round(sum(0.5 if d in medios else 1.0
                     for d in _dias_habiles(desde, hasta, dias_semana, feriados, saltos)), 1)


def _fin_desde_plazo(inicio: date, plazo: float, dias_semana: set, feriados: set,
                     saltos: set, medios: set) -> tuple:
    """Avanza desde `inicio` acumulando pesos hasta completar `plazo`.
    Devuelve (fecha_fin, medios) — `medios` puede crecer: si el plazo termina
    en .5 y el último día todavía pesaba completo, ese día se marca como medio
    para que el rango cuadre. El planner puede mover luego esa marca a
    cualquier otro día del rango: el plazo se recalcula solo (§ _plazo_de)."""
    medios = set(medios)
    acum, d, ultimo = 0.0, inicio, inicio
    for _ in range(_MAX_BUSQUEDA):
        if acum >= plazo - _EPS:
            break
        if _es_habil(d, dias_semana, feriados, saltos):
            peso = 0.5 if d in medios else 1.0
            falta = plazo - acum
            if peso > falta + _EPS:        # solo ocurre con falta == 0.5
                medios.add(d)
                peso = falta
            acum += peso
            ultimo = d
        d += timedelta(days=1)
    return ultimo, sorted(medios)


def _inicio_desde_plazo(fin: date, plazo: float, dias_semana: set, feriados: set,
                        saltos: set, medios: set) -> tuple:
    """Espejo de _fin_desde_plazo hacia atrás (modo FIN_PLAZO): fija el fin y
    calcula desde qué día hay que arrancar para que quepa el plazo."""
    medios = set(medios)
    acum, d, primero = 0.0, fin, fin
    for _ in range(_MAX_BUSQUEDA):
        if acum >= plazo - _EPS:
            break
        if _es_habil(d, dias_semana, feriados, saltos):
            peso = 0.5 if d in medios else 1.0
            falta = plazo - acum
            if peso > falta + _EPS:
                medios.add(d)
                peso = falta
            acum += peso
            primero = d
        d -= timedelta(days=1)
    return primero, sorted(medios)


def _parse_plazo(v) -> Optional[float]:
    """El plazo se expresa en múltiplos de medio día: 1, 1.5, 2… Cualquier
    otra fracción no es representable con los pesos del prorrateo."""
    if v in (None, ""):
        return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "plazo_dias debe ser un número")
    if p <= 0:
        raise HTTPException(400, "plazo_dias debe ser mayor que cero")
    if p > _MAX_PLAZO:
        raise HTTPException(400, f"plazo_dias no puede pasar de {_MAX_PLAZO:g} días")
    if abs(p * 2 - round(p * 2)) > 1e-6:
        raise HTTPException(400, "plazo_dias debe ser múltiplo de 0.5 (medio día)")
    return round(p, 1)


def _resolver_fechas(modo: str, campo: str, inicio: date, fin: date, plazo: Optional[float],
                     dias_semana: set, feriados: set, saltos: set, medios: set) -> tuple:
    """Dado el modo de programación y QUÉ tocó el planner, recalcula el dato
    derivado. Devuelve (inicio, fin, plazo, medios). Es la tabla de P6/Project:

      modo          editas inicio/fin/plazo → se recalcula
      INICIO_PLAZO  inicio|plazo|dias → fin     · fin → plazo
      FIN_PLAZO     fin|plazo|dias    → inicio  · inicio → plazo
      INICIO_FIN    inicio|fin|dias   → plazo   · plazo → fin

    `campo` ∈ inicio | fin | plazo | dias (saltos/medios) | modo.
    Con campo='dias' la actividad CONSERVA su plazo y estira el rango (agregar
    un salto la alarga), salvo en INICIO_FIN donde manda el rango."""
    def _podar(i: date, f: date, p: float, m) -> tuple:
        """Los medios días que quedaron fuera del rango nuevo se descartan:
        si no, al reprogramar la actividad arrastraría marcas viejas que ya no
        pinta nadie y el plazo dejaría de cuadrar con lo que se ve."""
        return i, f, p, sorted(d for d in m if i <= d <= f)

    recalc_plazo = lambda: _podar(                                          # noqa: E731
        inicio, fin, _plazo_de(inicio, fin, dias_semana, feriados, saltos, medios), medios)
    if plazo is None or campo in ("modo", "ambas"):
        return recalc_plazo()
    if modo == "INICIO_PLAZO":
        if campo == "fin":
            return recalc_plazo()
        f, m = _fin_desde_plazo(inicio, plazo, dias_semana, feriados, saltos, medios)
        return _podar(inicio, f, plazo, m)
    if modo == "FIN_PLAZO":
        if campo == "inicio":
            return recalc_plazo()
        i, m = _inicio_desde_plazo(fin, plazo, dias_semana, feriados, saltos, medios)
        return _podar(i, fin, plazo, m)
    # INICIO_FIN: mandan las dos fechas; el plazo solo se lee…
    if campo == "plazo":
        f, m = _fin_desde_plazo(inicio, plazo, dias_semana, feriados, saltos, medios)
        return _podar(inicio, f, plazo, m)
    return recalc_plazo()                    # …salvo que se escriba a propósito


# ── Atribución del avance real cuando una partida se programa en varios tramos ──
# El avance real vive en `ev_avances_diarios` por (partida, fecha, etapa): NO
# sabe de qué actividad del LookAhead vino. Mientras una partida-etapa tenga una
# sola actividad da igual, pero programarla en dos tramos (lo normal en un
# lookahead rodante y en obras de misceláneos) hacía que las dos se repartieran
# MAL el mismo real: las dos lo mostraban en la cuadrícula, las dos lo
# descontaban de su saldo (la segunda se quedaba sin plan) y en el PPC las dos
# se daban por cumplidas con el trabajo de una.
#
# Regla, en orden y pensada para ser explicable al planner:
#   1. si el registro dice de qué actividad vino (`actividad_id`, migración
#      0035 — lo pone el avance registrado DESDE una actividad), manda eso;
#   2. si no, es de la actividad cuyo rango CUBRE ese día; si varias lo cubren
#      (la partida se reprogramó encima), de la ÚLTIMA programada;
#   3. si ninguna lo cubre (se trabajó fuera de lo planificado), de la última
#      que terminó antes, y si no hay ninguna antes, de la primera de todas.
# Así ningún real se pierde y con una sola actividad el resultado es el de
# siempre.
def _dueno_del_real(items, acts: list) -> dict:
    """{fecha: actividad_id}. `items` es un iterable de fechas o de pares
    (fecha, actividad_id) — con el par se respeta el dueño ya registrado."""
    if not acts:
        return {}
    vivas = {a["id"] for a in acts}
    orden = sorted(acts, key=lambda a: (a["fecha"], a["id"]))
    out = {}
    for it in items:
        f, explicito = it if isinstance(it, tuple) else (it, None)
        if explicito in vivas:
            out[f] = explicito
            continue
        dentro = [a for a in orden if a["fecha"] <= f <= (a["fecha_fin"] or a["fecha"])]
        if dentro:
            out[f] = max(dentro, key=lambda a: a["id"])["id"]
            continue
        antes = sorted((a for a in orden if (a["fecha_fin"] or a["fecha"]) < f),
                       key=lambda a: (a["fecha_fin"] or a["fecha"], a["id"]))
        out[f] = antes[-1]["id"] if antes else orden[0]["id"]
    return out


_HITO_CLAVE = """
    COALESCE(hito_id, (SELECT id FROM ev_hitos h WHERE h.partida_id = $1
                        ORDER BY h.es_principal DESC, h.peso DESC, h.id LIMIT 1))
      = COALESCE($2, (SELECT id FROM ev_hitos h WHERE h.partida_id = $1
                        ORDER BY h.es_principal DESC, h.peso DESC, h.id LIMIT 1))
"""


# Un TRAMO (sub-fila «Frente / Tramo / Sector», 0038) no entra en este reparto:
# guarda su propio real con tramo_id y por eso no compite por el del día. Su
# PADRE tampoco: es un contenedor, no tiene celdas propias — lo que se ve en su
# fila es la suma de los hijos.
_CLASICAS = """
      AND NOT a.es_frente
      AND NOT EXISTS (SELECT 1 FROM prog_actividades h WHERE h.padre_id = a.id)
"""


async def _hermanas(con, partida_id: int, hito_id: Optional[int]) -> list:
    """Actividades vivas de la MISMA partida y etapa (incluida la propia), sin
    contar tramos ni contenedores: solo las que se reparten el real del día."""
    return [dict(r) for r in await con.fetch(
        f"""SELECT a.id, a.fecha, COALESCE(a.fecha_fin, a.fecha) AS fecha_fin
              FROM prog_actividades a
             WHERE a.partida_id = $1 AND a.estado <> 'CANCELADO'
               {_CLASICAS} AND {_HITO_CLAVE}""",
        partida_id, hito_id)]


async def _es_contenedor(con, act_id: int) -> bool:
    """¿La fila tiene sub-filas colgando? Entonces no se programa ni se avanza
    en ella: eso se hace en sus hijos."""
    return bool(await con.fetchval(
        "SELECT EXISTS (SELECT 1 FROM prog_actividades WHERE padre_id = $1)", act_id))


async def _redistribuir(con, act: dict, solo_despues_de: Optional[date] = None) -> None:
    """Recalcula la distribución diaria del metrado de la actividad. El metrado
    META es inmutable aquí (solo se cambia desde el formulario):
      · salta los días no laborables (prog_config + prog_feriados) y los
        saltos intencionales de la actividad (dias_salto);
      · los días que YA tienen avance real registrado quedan CONGELADOS
        (su programado es la línea contra la que se compara el cumplimiento);
      · las celdas MANUALES dentro del rango (escritas por el planner vía
        metrado-dias, 0027) se respetan: su cantidad descuenta del saldo y
        no se recalculan; fuera del rango vigente (reprogramación) el plan
        manual viejo se descarta salvo que el día tenga real;
      · el SALDO (metrado − real acumulado − plan manual pendiente) se
        re-prorratea entre los días hábiles restantes — así la actividad
        sigue apuntando a terminar en su F.Fin con lo que falta.
    Con solo_despues_de (al registrar un avance): los días ANTERIORES no se
    tocan ("eso ya se hizo") y el saldo cae solo en los días posteriores."""
    if await _es_contenedor(con, act["id"]):
        # La fila padre no tiene plan propio: lo que muestra por día es la suma
        # de sus sub-filas. Si tuviera celdas, el metrado se contaría dos veces.
        await con.execute("DELETE FROM prog_metrado_dia WHERE actividad_id = $1", act["id"])
        return
    desde, hasta = act["fecha"], act["fecha_fin"] or act["fecha"]
    dias_semana, feriados = await _calendario(con, act["proyecto_id"], desde, hasta)
    saltos = set(act.get("dias_salto") or [])
    habiles = _dias_habiles(desde, hasta, dias_semana, feriados, saltos)

    reales: dict = {}
    if act.get("partida_id"):
        # TODOS los reales de la MISMA etapa (hito) de la actividad, SIN
        # acotar al rango vigente: al reprogramar fechas, lo ya anotado en el
        # rango viejo sigue descontando del saldo (si no, el metrado completo
        # reaparecería prorrateado como si nada se hubiera hecho). Una
        # actividad del hito principal equivale a una sin hito (NULL).
        if act.get("es_frente"):
            # Sub-fila (0038): su real es suyo y de nadie más — no hay reparto
            # que adivinar. Es lo que hace verdadero el historial por área.
            filas = await con.fetch(
                """SELECT fecha, cantidad_dia FROM ev_avances_diarios
                   WHERE tramo_id = $1 AND cantidad_dia IS NOT NULL""", act["id"])
            reales = {r["fecha"]: float(r["cantidad_dia"]) for r in filas}
        else:
            filas = await con.fetch(
                f"""SELECT fecha, cantidad_dia, actividad_id FROM ev_avances_diarios ad
                   WHERE partida_id = $1 AND cantidad_dia IS NOT NULL
                     AND tramo_id IS NULL AND {_HITO_CLAVE}""",
                act["partida_id"], act.get("hito_id"))
            reales = {r["fecha"]: float(r["cantidad_dia"]) for r in filas}
            # Si la partida-etapa se programó en varios tramos, cada uno descuenta
            # SOLO el real que le toca: si no, el avance de un tramo le vacía el
            # plan al otro (§ _dueno_del_real).
            hermanas = await _hermanas(con, act["partida_id"], act.get("hito_id"))
            if len(hermanas) > 1:
                dueno = _dueno_del_real(
                    [(r["fecha"], r["actividad_id"]) for r in filas], hermanas)
                reales = {f: c for f, c in reales.items() if dueno.get(f) == act["id"]}

    # Celdas manuales del rango vigente: plan fino del planner, se protege.
    manuales = {r["fecha"]: float(r["cantidad"]) for r in await con.fetch(
        """SELECT fecha, cantidad FROM prog_metrado_dia
           WHERE actividad_id = $1 AND manual AND fecha BETWEEN $2 AND $3""",
        act["id"], desde, hasta)}

    intactos = set(reales) | set(manuales)
    if solo_despues_de:
        intactos |= {d for d in habiles if d <= solo_despues_de}
    # Se borran solo las celdas que se van a recalcular.
    await con.execute(
        "DELETE FROM prog_metrado_dia WHERE actividad_id = $1"
        " AND NOT (fecha = ANY($2::date[]))", act["id"], list(intactos))
    metrado = float(act["metrado_prog"] or 0)
    if metrado <= 0:
        return
    # El plan manual PENDIENTE (días sin real, aún por delante) descuenta del
    # saldo; un día manual ya pasado sin real es línea base congelada (como
    # cualquier día congelado) y no descuenta.
    plan_manual = sum(c for d, c in manuales.items()
                      if d not in reales
                      and (solo_despues_de is None or d > solo_despues_de))
    saldo = round(metrado - sum(reales.values()) - plan_manual, 3)
    restantes = [d for d in habiles if d not in intactos]
    if saldo <= 0 or not restantes:
        return
    medios = set(act.get("dias_medio") or [])             # pesan 0.5 (F4 v2)
    await con.executemany(
        "INSERT INTO prog_metrado_dia (actividad_id, fecha, cantidad) VALUES ($1,$2,$3)"
        " ON CONFLICT (actividad_id, fecha) DO UPDATE SET cantidad = $3, manual = false",
        [(act["id"], f, c) for f, c in _distribuir(saldo, restantes, medios).items() if c > 0])


def _parse_saltos(v) -> list:
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise HTTPException(400, "dias_salto debe ser una lista de fechas")
    out = []
    for s in v:
        f = parse_fecha(s)
        if not f:
            raise HTTPException(400, f"dias_salto: fecha inválida {s}")
        out.append(f)
    return sorted(set(out))


# SELECT canónico de actividades: partida de control, supervisor y el resumen
# de restricciones (rest_pend > 0 = aún no está "sana" para comprometerse).
_ACT_SQL = """
    SELECT a.*, o.descripcion AS otm_desc, s.nombre AS supervisor_nombre,
           ev.codigo AS partida_codigo, ev.descripcion AS partida_desc,
           ev.hh_presup AS partida_hh_presup, ev.naturaleza AS partida_naturaleza,
           -- PU de venta: sin él la partida entra al RO como costo sin venta,
           -- y el LookAhead es donde el planner puede darse cuenta a tiempo.
           ev.precio_unitario AS partida_pu, ev.otm_id AS partida_otm_id,
           (SELECT count(*) FROM prog_restricciones pr
             WHERE pr.actividad_id = a.id) AS rest_total,
           (SELECT count(*) FROM prog_restricciones pr
             WHERE pr.actividad_id = a.id AND NOT pr.liberada) AS rest_pend,
           (SELECT count(*) FROM prog_dependencias pd
             WHERE pd.actividad_id = a.id OR pd.predecesora_id = a.id) AS dep_total
    FROM prog_actividades a
    LEFT JOIN otms o ON o.id = a.otm_id
    LEFT JOIN supervisores s ON s.id = a.supervisor_id
    LEFT JOIN ev_partidas ev ON ev.id = a.partida_id
"""


def _norm_frente(v) -> Optional[str]:
    """Normaliza el frente/zona: MAYÚSCULAS, sin espacios repetidos, máx 60.

    Es lo que evita que el catálogo se llene de variantes de lo mismo
    ('Bahia 4', 'BAHIA  4', 'bahia 4' → 'BAHIA 4'). Devuelve None si viene
    vacío (el frente es opcional: no todo parte lo necesita)."""
    t = " ".join(str(v or "").split()).upper()
    return t[:60] or None


def _foto_out(f: dict) -> dict:
    purgada = f["purgada"]
    # ancho/alto (los guarda Pillow al subir) viajan SIEMPRE, aun purgada: el
    # documento arma la galería justificada con la forma de cada foto sin tener
    # que descargarla, y reserva el hueco correcto de las que ya no están.
    return {"id": f["id"], "purgada": purgada, "bytes": f["bytes"],
            "ancho": f.get("ancho"), "alto": f.get("alto"),
            "url": None if purgada else url_firmada(f["ruta"]),
            "url_thumb": None if purgada else url_firmada(f["ruta_thumb"])}


# ── Calendario (oficina) ─────────────────────────────────────
@router.get("/semana")
async def semana(proyecto_id: int = 1, lunes: str = ""):
    """Actividades + reportes (con URLs de foto firmadas) de la semana Lun-Dom."""
    base = _lunes_de(parse_fecha(lunes) or fecha_lima())
    fechas = [base + timedelta(days=i) for i in range(7)]
    pool = await db()
    async with pool.acquire() as con:
        # Solapamiento, no fecha de inicio: una actividad que arrancó el viernes
        # pasado y sigue hasta el martes TIENE que verse esta semana (antes
        # desaparecía del calendario en cuanto pasaba su semana de arranque).
        acts = [dict(r) for r in await con.fetch(
            _ACT_SQL + " WHERE a.proyecto_id = $1"
                       " AND a.fecha <= $3 AND COALESCE(a.fecha_fin, a.fecha) >= $2"
                       " ORDER BY a.fecha, a.id", proyecto_id, fechas[0], fechas[6])]
        reps = [dict(r) for r in await con.fetch(
            """SELECT r.*, s.nombre AS supervisor_nombre
               FROM campo_reportes r LEFT JOIN supervisores s ON s.id = r.supervisor_id
               WHERE r.proyecto_id = $1 AND r.fecha BETWEEN $2 AND $3
               ORDER BY r.fecha, r.id""", proyecto_id, fechas[0], fechas[6])]
        fotos = [dict(r) for r in await con.fetch(
            """SELECT f.* FROM campo_fotos f
               JOIN campo_reportes r ON r.id = f.reporte_id
               WHERE r.proyecto_id = $1 AND r.fecha BETWEEN $2 AND $3
               ORDER BY f.id""", proyecto_id, fechas[0], fechas[6])]

    fotos_por_rep: dict = {}
    for f in fotos:
        fotos_por_rep.setdefault(f["reporte_id"], []).append(_foto_out(f))
    for r in reps:
        r["fotos"] = fotos_por_rep.get(r["id"], [])
    reps_por_act: dict = {}
    for r in reps:
        if r["actividad_id"]:
            reps_por_act.setdefault(r["actividad_id"], []).append(r["id"])
    for a in acts:
        a["reportes"] = reps_por_act.get(a["id"], [])

    return {"lunes": str(fechas[0]), "fechas": [str(f) for f in fechas],
            "actividades": acts, "reportes": reps}


def _exigir_partida(metrado: Optional[float], partida_id: Optional[int]) -> None:
    """Una actividad CON metrado tiene que colgar de una partida de control.

    Sin partida el metrado programado es un espejismo: el avance real se guarda
    en `ev_avances_diarios` POR PARTIDA, así que no hay dónde anotarlo; la
    actividad no suma al valor ganado ni a la curva S; y —lo peor, porque es
    silencioso— el PPC la toma como comprometida con alcanzado 0 y la cuenta
    como NO CUMPLIDA al cerrar la semana, aunque el trabajo se haya hecho.

    Sin metrado sí es legítima: es una actividad de apoyo (reunión, traslado,
    capacitación) y el PPC la evalúa por estado."""
    if metrado and not partida_id:
        raise HTTPException(400,
            "Una actividad con metrado tiene que colgar de una partida de control: "
            "sin ella no se puede registrar el avance real, no suma al valor ganado "
            "y el PPC la contará como no cumplida. Elige la partida, o deja el "
            "metrado vacío si es una actividad de apoyo (reunión, traslado…).")


_MODOS_FECHA = ("INICIO_PLAZO", "FIN_PLAZO", "INICIO_FIN")


def _parse_modo(v) -> str:
    m = str(v or "").strip().upper() or "INICIO_PLAZO"
    if m not in _MODOS_FECHA:
        raise HTTPException(422, f"modo_fecha inválido (usa {'/'.join(_MODOS_FECHA)})")
    return m


async def _resolver_act(con, act: dict, campo: str) -> tuple:
    """Envuelve _resolver_fechas con el calendario del proyecto cargado.
    La ventana del calendario se abre generosamente a ambos lados porque el
    fin puede caer bastante más allá del rango que la actividad tenía."""
    inicio = act["fecha"]
    fin = act["fecha_fin"] or inicio
    plazo = None if act.get("plazo_dias") is None else float(act["plazo_dias"])
    dias_semana, feriados = await _calendario(
        con, act["proyecto_id"],
        min(inicio, fin) - timedelta(days=400), max(inicio, fin) + timedelta(days=400))
    return _resolver_fechas(act.get("modo_fecha") or "INICIO_PLAZO", campo, inicio, fin,
                            plazo, dias_semana, feriados,
                            set(act.get("dias_salto") or []), set(act.get("dias_medio") or []))


def _parse_metrado(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        m = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "metrado_prog debe ser un número")
    if m < 0:
        raise HTTPException(400, "metrado_prog no puede ser negativo")
    return m or None


def _desglose(v) -> Optional[str]:
    """Etiqueta de área/capa del tramo (0037). PURA.

    Texto libre pero normalizado en mayúsculas y sin espacios de sobra: «área b»
    y «Área B » tienen que agrupar juntas o la vista por áreas no sirve de nada.
    """
    s = " ".join(str(v or "").split()).upper()
    return s[:40] or None


def herencia_subfila(padre: dict, es_frente: bool, und, metrado, hito_id) -> dict:
    """Qué hereda una sub-fila de la fila que la contiene. PURA (0038).

    La sub-fila NO elige partida ni OTM: son las de su padre, o su metrado se
    descontaría del presupuesto de otra partida y el saldo dejaría de cuadrar.
    Un «Frente / Tramo / Sector» hereda además la etapa (hito) del padre —la
    necesita para alimentar el % de Valor Ganado—, mientras que una sub-etapa
    trae la suya. Y si no se teclea metrado, se hereda el del padre: es el caso
    de dividir en dos una fila que ya estaba programada.
    """
    return {
        "partida_id": padre["partida_id"],
        "otm_id": padre["otm_id"],
        "und": und or padre.get("und"),
        "hito_id": padre.get("hito_id") if es_frente else hito_id,
        "metrado": metrado if metrado is not None else (float(padre.get("metrado_prog") or 0) or None),
    }


async def _padre_para_hijo(con, padre_id: int, proyecto_id: int) -> dict:
    """Valida la fila de la que cuelga una sub-fila y devuelve lo que se hereda.

    Un solo nivel a propósito: el árbol del LookAhead se lee de un vistazo en la
    reunión y con nietos deja de leerse. Área y capa (0037) ya dan las dos
    dimensiones dentro del mismo nivel.
    """
    p = await con.fetchrow(
        """SELECT id, proyecto_id, otm_id, partida_id, hito_id, und, padre_id,
                  metrado_prog, titulo
             FROM prog_actividades WHERE id = $1""", padre_id)
    if not p:
        raise HTTPException(404, "La fila de la que quieres colgar no existe")
    if p["padre_id"] is not None:
        raise HTTPException(400, "Una sub-fila no se puede volver a subdividir")
    if p["proyecto_id"] != proyecto_id:
        raise HTTPException(400, "La sub-fila tiene que ser del mismo proyecto")
    if not p["partida_id"]:
        raise HTTPException(
            400, "Asígnale una partida a la fila antes de dividirla: el metrado de las "
                 "sub-filas se descuenta de esa partida")
    return dict(p)


async def _mudar_al_contenedor(con, padre: dict, hijo_id: int, es_frente: bool) -> None:
    """El padre pasa a ser contenedor al aparecer su primera sub-fila: su plan
    diario y su metrado dejan de ser propios (lo que muestra es la suma de los
    hijos) y el avance que ya tuviera se le atribuye al primer hijo, para que el
    historial no se pierda al dividir una fila que ya estaba en marcha."""
    await con.execute("UPDATE prog_actividades SET metrado_prog = 0 WHERE id = $1", padre["id"])
    await con.execute("DELETE FROM prog_metrado_dia WHERE actividad_id = $1", padre["id"])
    if es_frente:
        await con.execute(
            """UPDATE ev_avances_diarios SET tramo_id = $2
                WHERE actividad_id = $1 AND tramo_id IS NULL""", padre["id"], hijo_id)


@router.post("/actividades")
async def crear_actividad(data: dict, user: dict = Depends(require_role("oficina"))):
    fecha = parse_fecha(data.get("fecha"))
    titulo = str(data.get("titulo") or "").strip()
    if not fecha or not titulo:
        raise HTTPException(400, "fecha y titulo son obligatorios")
    fecha_fin = parse_fecha(data.get("fecha_fin"))
    if fecha_fin and fecha_fin < fecha:
        raise HTTPException(400, "fecha_fin no puede ser anterior a fecha")
    metrado = _parse_metrado(data.get("metrado_prog"))
    und = (str(data.get("und") or "").strip()[:10] or None)
    saltos = _parse_saltos(data.get("dias_salto"))
    medios = _parse_saltos(data.get("dias_medio"))
    if set(saltos) & set(medios):
        raise HTTPException(400, "Un día no puede ser salto y medio día a la vez")
    partida_id = int(data["partida_id"]) if data.get("partida_id") else None
    hito_id = int(data["hito_id"]) if data.get("hito_id") else None
    padre_id = int(data["padre_id"]) if data.get("padre_id") else None
    if padre_id is None:
        _exigir_partida(metrado, partida_id)
    proyecto_id = int(data.get("proyecto_id") or 1)
    modo = _parse_modo(data.get("modo_fecha"))
    plazo = _parse_plazo(data.get("plazo_dias"))
    otm_id = (str(data["otm_id"]).strip() or None) if data.get("otm_id") else None
    # Sub-fila (0038): por defecto es un «Frente / Tramo / Sector»; con
    # es_frente=false es una sub-etapa (un hito de la partida).
    es_frente = bool(data.get("es_frente", True)) if padre_id else False
    pool = await db()
    async with pool.acquire() as con:
        padre = None
        if padre_id:
            padre = await _padre_para_hijo(con, padre_id, proyecto_id)
            h = herencia_subfila(padre, es_frente, und, metrado, hito_id)
            partida_id, otm_id = h["partida_id"], h["otm_id"]
            und, hito_id, metrado = h["und"], h["hito_id"], h["metrado"]
        if hito_id:
            await _validar_hito(con, partida_id, hito_id)
        # Con plazo se deriva la fecha que falte (0034); sin él, el plazo sale
        # del rango — así el alta de siempre (inicio+fin) sigue igual.
        fecha, fecha_fin, plazo, medios = await _resolver_act(
            con, {"proyecto_id": proyecto_id, "fecha": fecha, "fecha_fin": fecha_fin,
                  "plazo_dias": plazo, "modo_fecha": modo,
                  "dias_salto": saltos, "dias_medio": medios},
            "plazo" if plazo is not None else "dias")
        async with con.transaction():
            try:
                row = await con.fetchrow(
                    """INSERT INTO prog_actividades
                       (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo, descripcion,
                        responsable, supervisor_id, metrado_prog, und, dias_salto, dias_medio,
                        hito_id, creado_por, plazo_dias, modo_fecha, desglose_1, desglose_2,
                        padre_id, es_frente)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,
                               $20,$21)
                       RETURNING *""",
                    proyecto_id, fecha, fecha_fin, otm_id, partida_id,
                    titulo, data.get("descripcion") or None, data.get("responsable") or None,
                    (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None,
                    metrado, und, saltos, medios, hito_id, user.get("sub"), plazo, modo,
                    _desglose(data.get("desglose_1")), _desglose(data.get("desglose_2")),
                    padre_id, es_frente)
            except _ERRORES_DATO:
                raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
            if padre is not None:
                await _mudar_al_contenedor(con, padre, row["id"], es_frente)
            await _redistribuir(con, dict(row))
            if padre is not None:
                # El padre acaba de perder su plan propio: sus celdas se borran
                # y desde ahora la fila muestra la suma de sus sub-filas.
                await _redistribuir(con, {**padre, "fecha": fecha, "fecha_fin": fecha_fin,
                                          "metrado_prog": 0})
    return dict(row)


async def _validar_hito(con, partida_id: Optional[int], hito_id: int) -> None:
    if not partida_id:
        raise HTTPException(400, "hito_id requiere partida_id")
    pid = await con.fetchval("SELECT partida_id FROM ev_hitos WHERE id = $1", hito_id)
    if pid is None:
        raise HTTPException(404, "Hito no encontrado")
    if pid != partida_id:
        raise HTTPException(400, "El hito no pertenece a la partida indicada")


@router.put("/actividades/{act_id}")
async def editar_actividad(act_id: int, data: dict):
    campos, valores = [], []
    if "estado" in data:
        if data["estado"] not in _ESTADOS:
            raise HTTPException(422, f"estado inválido (usa {'/'.join(_ESTADOS)})")
        campos.append("estado"); valores.append(data["estado"])
    if "fecha" in data:
        f = parse_fecha(data["fecha"])
        if not f:
            raise HTTPException(400, "fecha inválida")
        campos.append("fecha"); valores.append(f)
    if "fecha_fin" in data:
        campos.append("fecha_fin"); valores.append(parse_fecha(data["fecha_fin"]))
    if "metrado_prog" in data:
        # El metrado de un contenedor es la suma de sus sub-filas: si se pudiera
        # escribir aquí, el mismo metrado se contaría dos veces contra la partida.
        pool_g = await db()
        async with pool_g.acquire() as con_g:
            if await _es_contenedor(con_g, act_id) and _parse_metrado(data["metrado_prog"]):
                raise HTTPException(
                    409, "Esta fila está dividida: el metrado se edita en cada sub-fila")
        campos.append("metrado_prog"); valores.append(_parse_metrado(data["metrado_prog"]))
    if "und" in data:
        campos.append("und")
        valores.append(str(data["und"]).strip()[:10] or None if data["und"] is not None else None)
    if "dias_salto" in data:
        campos.append("dias_salto"); valores.append(_parse_saltos(data["dias_salto"]))
    if "dias_medio" in data:
        campos.append("dias_medio"); valores.append(_parse_saltos(data["dias_medio"]))
    if "plazo_dias" in data:
        campos.append("plazo_dias"); valores.append(_parse_plazo(data["plazo_dias"]))
    if "modo_fecha" in data:
        campos.append("modo_fecha"); valores.append(_parse_modo(data["modo_fecha"]))
    if "causa_nc_cat" in data:
        campos.append("causa_nc_cat"); valores.append(_validar_cnc(data["causa_nc_cat"]))
    if "causa_nc_planner_cat" in data:
        campos.append("causa_nc_planner_cat"); valores.append(_validar_cnc(data["causa_nc_planner_cat"]))
    for k in ("desglose_1", "desglose_2"):
        if k in data:
            campos.append(k); valores.append(_desglose(data[k]))
    for k in ("titulo", "descripcion", "responsable", "otm_id", "supervisor_id",
              "causa_nc", "causa_nc_planner"):
        if k in data:
            v = str(data[k]).strip() if data[k] is not None else None
            if k == "titulo" and not v:
                raise HTTPException(400, "titulo no puede quedar vacío")
            campos.append(k); valores.append(v or None)
    if "partida_id" in data:
        campos.append("partida_id")
        valores.append(int(data["partida_id"]) if data["partida_id"] else None)
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    # Modo de programación (0034): según QUÉ tocó el planner se deriva el tercer
    # dato. Enviar las dos fechas juntas siempre manda sobre el plazo (es el
    # gesto de "este es el rango, punto").
    campo = ("ambas" if {"fecha", "fecha_fin"} <= data.keys()
             else "plazo" if "plazo_dias" in data
             else "inicio" if "fecha" in data
             else "fin" if "fecha_fin" in data
             else "dias" if {"dias_salto", "dias_medio"} & data.keys()
             else "modo" if "modo_fecha" in data else "")
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            actual = await con.fetchrow(
                "SELECT * FROM prog_actividades WHERE id = $1 FOR UPDATE", act_id)
            if not actual:
                raise HTTPException(404, "Actividad no encontrada")
            # Solo se valida cuando el patch toca alguno de los dos campos: así
            # una edición inocua (el título) sobre una actividad que YA venía
            # mal no queda bloqueada, pero cualquier cambio de metrado o de
            # partida obliga a dejarla coherente.
            if {"metrado_prog", "partida_id"} & data.keys():
                fus = {**dict(actual), **dict(zip(campos, valores))}
                _exigir_partida(fus.get("metrado_prog"), fus.get("partida_id"))
            # Las fechas se resuelven ANTES de escribir, en un solo UPDATE: si
            # se escribiera la F.Inicio primero, mover una actividad más allá de
            # su antiguo fin violaría el CHECK (fecha_fin >= fecha) de 0019.
            if campo:
                fusion = {**dict(actual), **dict(zip(campos, valores))}
                ini, fin, plz, med = await _resolver_act(con, fusion, campo)
                for col, val in (("fecha", ini), ("fecha_fin", fin),
                                 ("plazo_dias", plz), ("dias_medio", med)):
                    if col in campos:
                        valores[campos.index(col)] = val
                    else:
                        campos.append(col); valores.append(val)
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(campos))
            try:
                row = await con.fetchrow(
                    f"UPDATE prog_actividades SET {sets}, actualizado_en = now() "
                    f"WHERE id = $1 RETURNING *", act_id, *valores)
            except _ERRORES_DATO:
                raise HTTPException(400, "OTM, partida, supervisor o rango de fechas inválido: revisa los datos")
            if set(row["dias_salto"] or []) & set(row["dias_medio"] or []):
                raise HTTPException(400, "Un día no puede ser salto y medio día a la vez")
            # Si cambió el rango, el metrado, los saltos o los medios días, la
            # distribución diaria se recalcula (las ediciones celda a celda van
            # por /actividades/{id}/metrado-dias y NO pasan por aquí).
            movidas: list = []
            if {"fecha", "fecha_fin", "metrado_prog", "dias_salto", "dias_medio",
                    "plazo_dias", "modo_fecha"} & data.keys():
                await _redistribuir(con, dict(row))
            # Auto-cascada: mover el rango empuja a las sucesoras (FS/SS/FF).
            if {"fecha", "fecha_fin", "plazo_dias", "dias_salto", "dias_medio"} & data.keys():
                movidas = await recalcular_cascada(con, act_id)
    return {**dict(row), "movidas": movidas}


@router.delete("/actividades/{act_id}")
async def borrar_actividad(act_id: int, con_subfilas: bool = False):
    pool = await db()
    async with pool.acquire() as con:
        n_reps = await con.fetchval(
            "SELECT count(*) FROM campo_reportes WHERE actividad_id = $1", act_id)
        if n_reps:
            raise HTTPException(409, "La actividad tiene reportes de campo; cancélala en vez de borrarla")
        # Borrar el padre se lleva a los hijos (CASCADE, 0038): que no pase sin
        # que quien lo pide sepa cuántas sub-filas se van con él.
        n_sub = await con.fetchval(
            "SELECT count(*) FROM prog_actividades WHERE padre_id = $1", act_id)
        if n_sub and not con_subfilas:
            raise HTTPException(
                409, f"Esta fila tiene {n_sub} sub-fila(s): al borrarla se borran también")
        n = await con.execute("DELETE FROM prog_actividades WHERE id = $1", act_id)
    if n == "DELETE 0":
        raise HTTPException(404, "Actividad no encontrada")
    return {"ok": True}


# ── Last Planner: lookahead, restricciones y PPC/CNC ─────────
@router.get("/lookahead")
async def lookahead(proyecto_id: int = 1, desde: str = "", semanas: int = 4):
    """Actividades de las próximas N semanas con su estado de restricciones —
    el nivel '¿qué se PUEDE hacer?' del Last Planner."""
    semanas = max(1, min(int(semanas or 4), 8))
    base = _lunes_de(parse_fecha(desde) or fecha_lima())
    pool = await db()
    # Solapamiento (igual que lookahead-grid): el Lookahead responde "¿qué se
    # puede hacer en las próximas N semanas?", así que una actividad de 3
    # semanas tiene que aparecer en las 3 — antes solo salía en la de su
    # F.Inicio y el conteo de restricciones de las otras dos daba 0.
    acts = [dict(r) for r in await pool.fetch(
        _ACT_SQL + " WHERE a.proyecto_id = $1"
                   " AND a.fecha <= $3 AND COALESCE(a.fecha_fin, a.fecha) >= $2"
                   " ORDER BY a.fecha, a.id",
        proyecto_id, base, base + timedelta(days=semanas * 7 - 1))]
    out = []
    for i in range(semanas):
        lun = base + timedelta(days=i * 7)
        dom = lun + timedelta(days=6)
        out.append({"lunes": str(lun), "domingo": str(dom),
                    "actividades": [a for a in acts
                                    if a["fecha"] <= dom
                                    and (a["fecha_fin"] or a["fecha"]) >= lun]})
    return {"desde": str(base), "semanas": out, "cnc": CNC,
            "tipos_restriccion": list(_TIPOS_RESTRICCION)}


# ── Calendario laboral (por proyecto — multi-empresa) ────────
async def _reprorratear_programadas(con, proyecto_id: int) -> int:
    """Tras cambiar el calendario, se recalcula la distribución de todas las
    actividades aún PROGRAMADO con metrado (las ejecutadas no se tocan)."""
    acts = await con.fetch(
        """SELECT * FROM prog_actividades
           WHERE proyecto_id = $1 AND estado = 'PROGRAMADO' AND metrado_prog IS NOT NULL""",
        proyecto_id)
    for a in acts:
        await _redistribuir(con, dict(a))
    return len(acts)


# Cómo se llaman por defecto las dos etiquetas con que se clasifica una porción
# de partida. La segunda NO puede ser «Capa»: eso solo existe en movimiento de
# tierras y el sistema tiene que servir igual en estructuras o en tuberías
# (0039). Ambas se renombran por proyecto desde `PUT /config/desglose`.
DESGLOSE_DEFECTO = ("Área", "Frente / Tramo / Sector")


@router.get("/config")
async def ver_config(proyecto_id: int = 1):
    pool = await db()
    async with pool.acquire() as con:
        cfg = await con.fetchrow(
            """SELECT dias_semana, etiqueta_desglose_1, etiqueta_desglose_2
                 FROM prog_config WHERE proyecto_id = $1""", proyecto_id)
        fer = await con.fetch(
            "SELECT id, fecha, motivo FROM prog_feriados WHERE proyecto_id = $1 ORDER BY fecha",
            proyecto_id)
    ds = cfg["dias_semana"] if cfg else None
    return {"dias_semana": sorted(ds) if ds else [1, 2, 3, 4, 5, 6, 7],
            # Cómo llama ESTE proyecto a las dos dimensiones del desglose: en
            # tierras «Área» y «Capa», en estructuras «Eje» y «Nivel».
            "etiqueta_desglose_1": (cfg and cfg["etiqueta_desglose_1"]) or DESGLOSE_DEFECTO[0],
            "etiqueta_desglose_2": (cfg and cfg["etiqueta_desglose_2"]) or DESGLOSE_DEFECTO[1],
            "feriados": [{"id": r["id"], "fecha": str(r["fecha"]), "motivo": r["motivo"]}
                         for r in fer]}


@router.put("/config/desglose")
async def guardar_etiquetas_desglose(data: dict):
    """Cómo se llaman en este proyecto las dos dimensiones en que se subdivide
    una partida grande al programarla."""
    def _et(v, defecto):
        return " ".join(str(v or "").split())[:40] or defecto
    e1 = _et(data.get("etiqueta_desglose_1"), DESGLOSE_DEFECTO[0])
    e2 = _et(data.get("etiqueta_desglose_2"), DESGLOSE_DEFECTO[1])
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    await pool.execute(
        """INSERT INTO prog_config (proyecto_id, etiqueta_desglose_1, etiqueta_desglose_2)
           VALUES ($1,$2,$3) ON CONFLICT (proyecto_id)
           DO UPDATE SET etiqueta_desglose_1 = $2, etiqueta_desglose_2 = $3,
                         actualizado_en = now()""", proyecto_id, e1, e2)
    return {"ok": True, "etiqueta_desglose_1": e1, "etiqueta_desglose_2": e2}


def saldo_partida(metrado_presup: float, programado: float, ejecutado: float) -> dict:
    """Cuánto queda de una partida grande que se ejecuta en porciones. PURA.

    El caso de Jean: RELLENO ZONA 5 son 15 000 m³ que se avanzan de 200 en 200
    por áreas y capas. Sin este saldo, «se va quitando de a pocos» es una cuenta
    que alguien lleva de memoria o en un Excel aparte, y nadie se entera de que
    se pasó hasta que el RO no cuadra.

    Excedido se informa, NO se bloquea (decisión de Jean): la obra manda y el
    mayor metrado se sustenta después: bloquear frenaría la programación de la
    semana por un trámite.
    """
    presup = float(metrado_presup or 0)
    prog = round(float(programado or 0), 3)
    ejec = round(float(ejecutado or 0), 3)
    return {
        "metrado_presup": presup, "programado": prog, "ejecutado": ejec,
        "saldo_por_programar": round(presup - prog, 3),
        "saldo_por_ejecutar": round(presup - ejec, 3),
        # Con presupuesto 0 no hay contra qué comparar: no se inventa un exceso.
        "excedido": round(prog - presup, 3) if presup > 0 and prog > presup + 5e-4 else 0.0,
        "pct_programado": round(prog / presup, 4) if presup > 0 else None,
        "pct_ejecutado": round(ejec / presup, 4) if presup > 0 else None,
    }


@router.get("/saldo-partida")
async def ver_saldo_partida(partida_id: int, proyecto_id: int = 1, excluir: int = 0):
    """Presupuestado vs programado vs ejecutado de una partida, con su desglose
    por área/capa. `excluir` deja fuera una actividad (la que se está editando),
    para que su propio metrado no cuente dos veces en el aviso."""
    pool = await db()
    async with pool.acquire() as con:
        p = await con.fetchrow(
            """SELECT id, codigo, descripcion, unidad, COALESCE(metrado_presup,0) AS metrado_presup
                 FROM ev_partidas WHERE id = $1 AND activo""", partida_id)
        if not p:
            raise HTTPException(404, "Partida no encontrada")
        prog = await con.fetchval(
            """SELECT COALESCE(SUM(COALESCE(metrado_prog,0)),0) FROM prog_actividades
                WHERE partida_id = $1 AND estado <> 'CANCELADO' AND id <> $2""",
            partida_id, excluir)
        ejec = await con.fetchval(
            """SELECT COALESCE(SUM(cantidad_dia),0) FROM ev_avances_diarios
                WHERE partida_id = $1 AND cantidad_dia IS NOT NULL""", partida_id)
        det = await con.fetch(
            """SELECT desglose_1, desglose_2,
                      COALESCE(SUM(COALESCE(metrado_prog,0)),0) AS prog, count(*) AS n
                 FROM prog_actividades
                WHERE partida_id = $1 AND estado <> 'CANCELADO'
                GROUP BY 1,2 ORDER BY 1 NULLS LAST, 2 NULLS LAST""", partida_id)
    return {
        "partida_id": p["id"], "codigo": p["codigo"], "descripcion": p["descripcion"],
        "unidad": p["unidad"], "proyecto_id": proyecto_id,
        **saldo_partida(p["metrado_presup"], prog, ejec),
        "desglose": [{"desglose_1": r["desglose_1"], "desglose_2": r["desglose_2"],
                      "programado": round(float(r["prog"] or 0), 3), "actividades": int(r["n"])}
                     for r in det],
    }


@router.put("/renombrar-desglose")
async def renombrar_desglose(data: dict):
    """Cambia el nombre de un área (o capa) en TODAS las sub-filas que la usan.

    Renombrar «AREA A» a mano en seis frentes no solo es tedioso: basta con
    equivocarse en uno para quedarse con dos áreas donde había una, y entonces
    la banda —y el saldo agrupado— se parten en dos. Se renombra la banda de UNA
    fila padre, que es el gesto que se hace en pantalla; devuelve cuántas
    cambiaron para poder decirlo antes de confirmar.
    """
    campo = str(data.get("campo") or "desglose_1")
    if campo not in ("desglose_1", "desglose_2"):
        raise HTTPException(400, "campo debe ser desglose_1 o desglose_2")
    padre_id = int(data["padre_id"]) if data.get("padre_id") else None
    if not padre_id:
        raise HTTPException(400, "padre_id requerido")
    de = _desglose(data.get("de"))
    a = _desglose(data.get("a"))
    if not a:
        raise HTTPException(400, "El nombre nuevo no puede quedar vacío")
    if de == a:
        return {"ok": True, "n": 0}
    pool = await db()
    async with pool.acquire() as con:
        n = await con.fetchval(
            f"""WITH t AS (
                  UPDATE prog_actividades SET {campo} = $1, actualizado_en = NOW()
                   WHERE padre_id = $2
                     AND COALESCE({campo}, '') = COALESCE($3, '')
                  RETURNING 1)
                SELECT count(*) FROM t""", a, padre_id, de)
    return {"ok": True, "n": int(n or 0), "de": de, "a": a}


@router.get("/historial-partida")
async def historial_partida(partida_id: int, proyecto_id: int = 1):
    """Todo lo que se programó de una partida y cómo terminó cada sub-fila.

    Es la pregunta que se hace en la reunión cuando una partida grande lleva
    meses avanzando de a pocos: «¿qué áreas ya cerré y cuánto me queda?». Trae
    también las TERMINADAS, que en la cuadrícula se ocultan justo para que no
    estorben, y el avance por día de cada una — el de verdad, no el repartido
    (0038).
    """
    pool = await db()
    async with pool.acquire() as con:
        p = await con.fetchrow(
            """SELECT id, codigo, descripcion, unidad, COALESCE(metrado_presup,0) AS metrado_presup
                 FROM ev_partidas WHERE id = $1""", partida_id)
        if not p:
            raise HTTPException(404, "Partida no encontrada")
        filas = [dict(r) for r in await con.fetch(
            """SELECT a.id, a.padre_id, a.es_frente, a.titulo, a.estado, a.fecha,
                      COALESCE(a.fecha_fin, a.fecha) AS fecha_fin, a.metrado_prog,
                      a.desglose_1, a.desglose_2, a.supervisor_id, a.responsable,
                      s.nombre AS supervisor_nombre, h.descripcion AS hito_desc,
                      (SELECT COALESCE(SUM(cantidad_dia),0) FROM ev_avances_diarios d
                        WHERE d.tramo_id = a.id) AS real_tramo
                 FROM prog_actividades a
                 LEFT JOIN supervisores s ON s.id = a.supervisor_id
                 LEFT JOIN ev_hitos h ON h.id = a.hito_id
                WHERE a.partida_id = $1 AND a.proyecto_id = $2
                ORDER BY a.desglose_1 NULLS LAST, a.desglose_2 NULLS LAST, a.fecha, a.id""",
            partida_id, proyecto_id)]
        ids = [f["id"] for f in filas]
        dias = await con.fetch(
            """SELECT tramo_id, fecha::text AS f, cantidad_dia FROM ev_avances_diarios
                WHERE tramo_id = ANY($1) AND cantidad_dia IS NOT NULL
                ORDER BY fecha""", ids) if ids else []
        ejec = float(await con.fetchval(
            """SELECT COALESCE(SUM(cantidad_dia),0) FROM ev_avances_diarios
                WHERE partida_id = $1 AND cantidad_dia IS NOT NULL""", partida_id) or 0)
    por_dia: dict = {}
    for r in dias:
        por_dia.setdefault(r["tramo_id"], {})[r["f"]] = float(r["cantidad_dia"])
    prog = sum(float(f["metrado_prog"] or 0) for f in filas if f["estado"] != "CANCELADO")
    return {
        "partida_id": p["id"], "codigo": p["codigo"], "descripcion": p["descripcion"],
        "unidad": p["unidad"],
        **saldo_partida(p["metrado_presup"], prog, ejec),
        "filas": [{
            "id": f["id"], "padre_id": f["padre_id"], "es_frente": f["es_frente"],
            "titulo": f["titulo"], "estado": f["estado"],
            "fecha": str(f["fecha"]), "fecha_fin": str(f["fecha_fin"]),
            "metrado_prog": float(f["metrado_prog"]) if f["metrado_prog"] is not None else None,
            "desglose_1": f["desglose_1"], "desglose_2": f["desglose_2"],
            "hito_desc": f["hito_desc"],
            "responsable": f["supervisor_nombre"] or f["responsable"],
            "real": round(float(f["real_tramo"] or 0), 3),
            "dias": por_dia.get(f["id"], {}),
        } for f in filas],
    }


@router.get("/desgloses")
async def valores_desglose(partida_id: int = 0, proyecto_id: int = 1):
    """Áreas y capas ya usadas — para autocompletar en vez de teclear.

    Se ofrecen primero las de la partida (lo que se está subdividiendo) y luego
    las del resto del proyecto: escribir «AREA B» de dos maneras distintas
    rompería la agrupación, que es justo lo que la vista tiene que evitar.
    """
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT desglose_1, desglose_2,
                      bool_or(partida_id = $2) AS de_la_partida
                 FROM prog_actividades
                WHERE proyecto_id = $1 AND (desglose_1 IS NOT NULL OR desglose_2 IS NOT NULL)
                GROUP BY 1,2""", proyecto_id, partida_id or -1)
    d1, d2 = {}, {}
    for r in rows:
        for col, acc in ((r["desglose_1"], d1), (r["desglose_2"], d2)):
            if col:
                acc[col] = acc.get(col, False) or bool(r["de_la_partida"])
    def _orden(acc):
        return [k for k, propio in sorted(acc.items(), key=lambda kv: (not kv[1], kv[0]))]
    return {"desglose_1": _orden(d1), "desglose_2": _orden(d2)}


@router.put("/config")
async def guardar_config(data: dict):
    """Días de la semana que se trabajan (ISO: 1=Lun … 7=Dom). Re-prorratea
    las actividades PROGRAMADO para que el plan salte los días nuevos."""
    dias = data.get("dias_semana")
    if (not isinstance(dias, list) or not dias
            or any(not isinstance(d, int) or d < 1 or d > 7 for d in dias)):
        raise HTTPException(400, "dias_semana debe ser una lista no vacía con valores 1..7")
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                """INSERT INTO prog_config (proyecto_id, dias_semana)
                   VALUES ($1, $2) ON CONFLICT (proyecto_id)
                   DO UPDATE SET dias_semana = $2, actualizado_en = now()""",
                proyecto_id, sorted(set(dias)))
            n = await _reprorratear_programadas(con, proyecto_id)
    return {"ok": True, "dias_semana": sorted(set(dias)), "reprorrateadas": n}


@router.post("/feriados")
async def crear_feriado(data: dict):
    f = parse_fecha(data.get("fecha"))
    if not f:
        raise HTTPException(400, "fecha requerida")
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                """INSERT INTO prog_feriados (proyecto_id, fecha, motivo)
                   VALUES ($1,$2,$3) ON CONFLICT (proyecto_id, fecha)
                   DO UPDATE SET motivo = $3 RETURNING id, fecha, motivo""",
                proyecto_id, f, (str(data.get("motivo") or "").strip() or None))
            n = await _reprorratear_programadas(con, proyecto_id)
    return {"id": row["id"], "fecha": str(row["fecha"]), "motivo": row["motivo"],
            "reprorrateadas": n}


@router.delete("/feriados/{fer_id}")
async def borrar_feriado(fer_id: int):
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            row = await con.fetchrow(
                "DELETE FROM prog_feriados WHERE id = $1 RETURNING proyecto_id", fer_id)
            if not row:
                raise HTTPException(404, "Feriado no encontrado")
            n = await _reprorratear_programadas(con, row["proyecto_id"])
    return {"ok": True, "reprorrateadas": n}


def _parse_cantidad(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        c = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "cantidad debe ser un número")
    if c < 0:
        raise HTTPException(400, "la cantidad no puede ser negativa")
    return c


async def _hito_principal(con, partida_id: int) -> Optional[int]:
    """Id del hito principal de la partida. Si la partida NO tiene hitos, lo
    crea silenciosamente ('Ejecución', peso 100%) — así el % EV de cualquier
    partida queda conectado al avance diario sin trabajo extra del planner."""
    hid = await con.fetchval(
        """SELECT id FROM ev_hitos WHERE partida_id = $1
           ORDER BY es_principal DESC, peso DESC, id LIMIT 1""", partida_id)
    if hid is None:
        hid = await con.fetchval(
            """INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal)
               VALUES ($1, 1, 'Ejecución', 1.0, true) RETURNING id""", partida_id)
    return hid


async def _rollup_ev_avances(con, partida_id: int) -> None:
    """Fuente única: deriva ev_avances (acumulado semanal por hito — la entrada
    del motor EV) del registro diario. Por hito: las semanas desde su primer
    registro diario se recalculan como base_manual_previa + Σ diario; las
    semanas anteriores (captura manual / carga histórica) no se tocan."""
    principal = await _hito_principal(con, partida_id)
    rows = await con.fetch(
        """SELECT COALESCE(hito_id, $2) AS hid, semana, SUM(cantidad_dia) AS c
           FROM ev_avances_diarios
           WHERE partida_id = $1 AND cantidad_dia IS NOT NULL
           GROUP BY 1, 2""", partida_id, principal)
    por_hito: dict = {}
    for r in rows:
        por_hito.setdefault(r["hid"], {})[r["semana"]] = float(r["c"] or 0)
    for hid, sems in por_hito.items():
        primera = min(sems)
        base = float(await con.fetchval(
            """SELECT COALESCE(MAX(cantidad_acum), 0) FROM ev_avances
               WHERE hito_id = $1 AND semana < $2""", hid, primera) or 0)
        existentes = {r["semana"] for r in await con.fetch(
            "SELECT semana FROM ev_avances WHERE hito_id = $1 AND semana >= $2",
            hid, primera)}
        for s in sorted(set(sems) | existentes):
            acum = base + sum(c for w, c in sems.items() if w <= s)
            await con.execute(
                """INSERT INTO ev_avances (hito_id, semana, cantidad_acum, registrado_en)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (hito_id, semana)
                   DO UPDATE SET cantidad_acum = $3, registrado_en = NOW()""",
                hid, s, round(acum, 4))


async def registrar_avance_partida(con, partida_id: int, fecha: date, cantidad,
                                   notas=None, actualizar_notas: bool = False,
                                   hito_id: Optional[int] = None,
                                   actividad_id: Optional[int] = None,
                                   tramo_id: Optional[int] = None) -> None:
    """Escritura ÚNICA del avance real diario (F1 LookAhead v2 / auditoría F-2):
    upsert (o DELETE si cantidad es None) en ev_avances_diarios y re-prorrateo
    de TODA actividad del LookAhead vinculada a la partida cuyo rango cubre la
    fecha — venga el avance de programación o del módulo Valor Ganado, el dato
    y sus consecuencias son los mismos. La semana usa core.tiempo.semana_de.
    hito_id = etapa de la partida a la que pertenece el registro (NULL = hito
    principal). Tras escribir, _rollup_ev_avances deriva ev_avances (la entrada
    del motor EV): un solo dato alimenta LookAhead, VG diario y % de avance.
    actividad_id = de qué actividad del LookAhead vino (0035); NULL cuando el
    avance se carga desde Valor Ganado y entonces se atribuye por fechas.
    tramo_id = sub-fila «Frente / Tramo / Sector» dueña del registro (0038):
    con él, dos áreas que avanzan el MISMO día guardan cada una su cifra en vez
    de repartirse un total. NULL = avance de la partida, como siempre."""
    from routers.ev._datos import _fecha_base
    # Convención dura: el hito principal SIEMPRE se guarda como NULL (las
    # vistas por partida — semana-grid, matriz — leen NULL = cant. instalada).
    principal = await _hito_principal(con, partida_id)
    if hito_id is not None and hito_id == principal:
        hito_id = None
    if cantidad is None:
        await con.execute(
            """DELETE FROM ev_avances_diarios WHERE partida_id = $1 AND fecha = $2
               AND COALESCE(hito_id, 0) = COALESCE($3, 0)
               AND COALESCE(tramo_id, 0) = COALESCE($4, 0)""",
            partida_id, fecha, hito_id, tramo_id)
    else:
        base = await _fecha_base(con)
        semana = max(1, semana_de(fecha, base)) if base else 1
        # El dueño solo se PISA cuando el avance viene de una actividad; si se
        # carga desde Valor Ganado (actividad_id NULL) se conserva el que ya
        # tenía, para no borrar una atribución explícita al corregir la cifra.
        if actualizar_notas:
            await con.execute(
                """INSERT INTO ev_avances_diarios
                     (partida_id, fecha, semana, cantidad_dia, notas, hito_id,
                      actividad_id, tramo_id, registrado_en)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                   ON CONFLICT (partida_id, fecha, COALESCE(hito_id, 0), COALESCE(tramo_id, 0))
                   DO UPDATE SET cantidad_dia = $4, notas = $5, registrado_en = NOW(),
                     actividad_id = COALESCE($7, ev_avances_diarios.actividad_id)""",
                partida_id, fecha, semana, cantidad, notas, hito_id, actividad_id,
                tramo_id)
        else:
            await con.execute(
                """INSERT INTO ev_avances_diarios
                     (partida_id, fecha, semana, cantidad_dia, hito_id,
                      actividad_id, tramo_id, registrado_en)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                   ON CONFLICT (partida_id, fecha, COALESCE(hito_id, 0), COALESCE(tramo_id, 0))
                   DO UPDATE SET cantidad_dia = $4, registrado_en = NOW(),
                     actividad_id = COALESCE($6, ev_avances_diarios.actividad_id)""",
                partida_id, fecha, semana, cantidad, hito_id, actividad_id, tramo_id)
    await _rollup_ev_avances(con, partida_id)
    # Los días anteriores al registrado no se tocan; el saldo para cumplir el
    # metrado meta se re-prorratea en los días siguientes de cada actividad
    # de la MISMA etapa (hito) de la partida — una actividad apuntando al
    # hito principal equivale a una sin hito ($4 = id del principal).
    # Se re-prorratean TODAS las actividades vivas de la partida-etapa, no solo
    # la que cubre la fecha: el día registrado puede cambiar de dueño (§
    # _dueno_del_real) y el tramo que lo pierde tiene que recuperar su saldo.
    # Con tramo (0038) no hay nada que repartir: el real es de esa sub-fila, así
    # que solo ella recalcula su saldo. Sin tramo, sigue el reparto de siempre
    # entre las actividades clásicas de la partida-etapa.
    if tramo_id is not None:
        acts = await con.fetch(
            "SELECT * FROM prog_actividades WHERE id = $1 AND estado <> 'CANCELADO'",
            tramo_id)
    else:
        acts = await con.fetch(
            f"""SELECT a.* FROM prog_actividades a
               WHERE a.partida_id = $1 AND a.estado <> 'CANCELADO'
                 {_CLASICAS}
                 AND COALESCE(a.hito_id, $3) = COALESCE($2, $3)""",
            partida_id, hito_id, principal)
    for a in acts:
        await _redistribuir(con, dict(a), solo_despues_de=fecha)


@router.post("/actividades/{act_id}/avance-dia")
async def avance_dia_actividad(act_id: int, data: dict):
    """El avance REAL del día contra una actividad del LookAhead: escribe en
    ev_avances_diarios (la partida de control de la actividad) y RE-PRORRATEA
    el saldo entre los días hábiles restantes — la actividad sigue apuntando
    a terminar en su F.Fin. El programado del día avanzado queda congelado
    como línea base de comparación (celeste→verde/ámbar/rojo en el panel)."""
    f = parse_fecha(data.get("fecha"))
    if not f:
        raise HTTPException(400, "fecha requerida")
    cantidad = _parse_cantidad(data.get("cantidad"))
    pool = await db()
    async with pool.acquire() as con:
        act = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", act_id)
        if not act:
            raise HTTPException(404, "Actividad no encontrada")
        if not act["partida_id"]:
            raise HTTPException(400, "La actividad no tiene partida de control: asígnala para registrar avance")
        if await _es_contenedor(con, act_id):
            raise HTTPException(
                409, "Esta fila está dividida en sub-filas: anota el avance en la sub-fila que trabajó")
        async with con.transaction():
            # El avance queda ligado a ESTA actividad (0035): si la partida se
            # programó en varios tramos, el real no se le atribuye a otro. Y si
            # es una sub-fila (0038), además guarda cifra propia (tramo_id).
            await registrar_avance_partida(con, act["partida_id"], f, cantidad,
                                           hito_id=act["hito_id"], actividad_id=act_id,
                                           tramo_id=act_id if act["es_frente"] else None)
    return {"ok": True, "cantidad": cantidad}


@router.post("/actividades-lote")
async def crear_actividades_lote(data: dict, user: dict = Depends(require_role("oficina"))):
    """Programación por partidas (flujo LookAhead): el planner elige una OTM,
    marca varias partidas del presupuesto y cada una se vuelve UNA actividad
    con su rango F.Inic-F.Fin y su metrado meta. Si el item no trae metrado,
    se toma el metrado del presupuesto de la partida; en ambos casos se
    prorratea equitativamente entre los días del rango."""
    otm_id = str(data.get("otm_id") or "").strip()
    items = data.get("items") or []
    if not otm_id or not isinstance(items, list) or not items:
        raise HTTPException(400, "otm_id e items son obligatorios")
    if len(items) > 50:
        raise HTTPException(422, "Máximo 50 partidas por lote")

    parsed = []
    for i, it in enumerate(items, 1):
        pid = it.get("partida_id")
        fecha = parse_fecha(it.get("fecha"))
        if not pid or not fecha:
            raise HTTPException(400, f"Item {i}: partida_id y fecha son obligatorios")
        fecha_fin = parse_fecha(it.get("fecha_fin"))
        if fecha_fin and fecha_fin < fecha:
            raise HTTPException(400, f"Item {i}: fecha_fin anterior a fecha")
        parsed.append((int(pid), fecha, fecha_fin, _parse_metrado(it.get("metrado_prog")),
                       int(it["hito_id"]) if it.get("hito_id") else None))

    proyecto_id = int(data.get("proyecto_id") or 1)
    supervisor_id = (str(data["supervisor_id"]).strip() or None) if data.get("supervisor_id") else None
    responsable = data.get("responsable") or None
    descripcion = data.get("descripcion") or None

    encadenar = bool(data.get("encadenar_hitos"))
    pool = await db()
    creadas = []
    async with pool.acquire() as con:
        pinfo = {r["id"]: dict(r) for r in await con.fetch(
            "SELECT id, descripcion, metrado_presup, unidad FROM ev_partidas WHERE id = ANY($1)",
            [p[0] for p in parsed])}
        faltan = [str(p[0]) for p in parsed if p[0] not in pinfo]
        if faltan:
            raise HTTPException(400, f"Partidas inexistentes: {', '.join(faltan)}")
        hinfo = {}
        for pid, _f, _ff, _m, hid in parsed:
            if hid:
                await _validar_hito(con, pid, hid)
                hinfo[hid] = await con.fetchrow(
                    "SELECT descripcion, peso FROM ev_hitos WHERE id = $1", hid)
        async with con.transaction():
            # id de la actividad anterior de la MISMA partida (para encadenar FS
            # las etapas desplegadas por hitos, en el orden en que llegan).
            prev_de_partida: dict = {}
            for pid, fecha, fecha_fin, metrado, hid in parsed:
                p = pinfo[pid]
                if metrado is None and p["metrado_presup"] is not None:
                    metrado = float(p["metrado_presup"]) or None
                titulo = (p["descripcion"] or f"Partida {pid}")[:170]
                if hid and hinfo.get(hid):
                    titulo = f"{titulo} — {hinfo[hid]['descripcion'] or 'Etapa'}"[:200]
                # El plazo del alta por lotes sale del rango recibido (0034):
                # así las actividades nacen con duración y la cascada puede
                # empujarlas sin deformarlas.
                _i, _f, plazo, _m = await _resolver_act(
                    con, {"proyecto_id": proyecto_id, "fecha": fecha, "fecha_fin": fecha_fin,
                          "plazo_dias": None, "modo_fecha": "INICIO_PLAZO",
                          "dias_salto": [], "dias_medio": []}, "dias")
                try:
                    row = await con.fetchrow(
                        """INSERT INTO prog_actividades
                           (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo,
                            descripcion, responsable, supervisor_id, metrado_prog, hito_id,
                            creado_por, plazo_dias)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
                        proyecto_id, fecha, fecha_fin, otm_id, pid, titulo,
                        descripcion, responsable, supervisor_id, metrado, hid,
                        user.get("sub"), plazo)
                except _ERRORES_DATO:
                    raise HTTPException(400, "OTM, partida o supervisor inválido: revisa los datos")
                await _redistribuir(con, dict(row))
                if encadenar and hid and prev_de_partida.get(pid):
                    await con.execute(
                        """INSERT INTO prog_dependencias (actividad_id, predecesora_id, lag_dias)
                           VALUES ($1, $2, 0) ON CONFLICT DO NOTHING""",
                        row["id"], prev_de_partida[pid])
                if hid:
                    prev_de_partida[pid] = row["id"]
                creadas.append(dict(row))
    return {"creadas": len(creadas), "actividades": creadas}


# ── Dependencias (F5 v2 · tipos FS/SS/FF en 0034) con auto-cascada ──
_TIPOS_DEP = ("FS", "SS", "FF")


async def _hay_ciclo(con, predecesora_id: int, actividad_id: int) -> bool:
    """¿Agregar 'predecesora_id precede a actividad_id' crearía un ciclo?
    Sí, cuando predecesora_id es alcanzable desde actividad_id siguiendo
    la relación 'precede a' (BFS sobre las sucesoras)."""
    cola, vistas = [actividad_id], set()
    while cola:
        actual = cola.pop(0)
        if actual == predecesora_id:
            return True
        if actual in vistas:
            continue
        vistas.add(actual)
        cola += [r["actividad_id"] for r in await con.fetch(
            "SELECT actividad_id FROM prog_dependencias WHERE predecesora_id = $1", actual)]
    return False


def _siguiente_habil(d: date, dias_semana: set, feriados: set) -> date:
    """Primer día ESTRICTAMENTE posterior a d que es hábil del calendario."""
    x = d + timedelta(days=1)
    while not (x.isoweekday() in dias_semana and x not in feriados):
        x += timedelta(days=1)
    return x


def _restriccion_dep(tipo: str, lag: int, pred_ini: date, pred_fin: date,
                     dias_semana: set, feriados: set) -> tuple:
    """Traduce un vínculo a la restricción que impone sobre la sucesora:
    ('inicio'|'fin', fecha mínima). El lag se cuenta en DÍAS HÁBILES; con
    lag 0 el resultado es el de siempre.

      FS  la sucesora no puede EMPEZAR antes del día hábil siguiente al fin
          de la antecesora (+lag)                    → el clásico
      SS  no puede EMPEZAR antes de que empiece la antecesora (+lag)
          → traslapes: "el encofrado arranca 1 día después del habilitado"
      FF  no puede TERMINAR antes de que termine la antecesora (+lag)
          → "el curado no cierra antes que el vaciado"
    """
    if tipo == "SS":
        return "inicio", _habil_desplazado(pred_ini, lag, dias_semana, feriados)
    if tipo == "FF":
        return "fin", _habil_desplazado(pred_fin, lag, dias_semana, feriados)
    return "inicio", _habil_desplazado(pred_fin, lag + 1, dias_semana, feriados)


async def recalcular_cascada(con, actividad_id_movida: int,
                             forzar: Optional[set] = None) -> list:
    """Auto-cascada de la red (FS/SS/FF, 0034): al mover una actividad, cada
    sucesora se desplaza hasta cumplir TODAS sus antecesoras — se evalúa la
    restricción más exigente de todas, no una a una, porque una sucesora puede
    colgar de varias con tipos distintos.

    Dos comportamientos, a propósito distintos:

    · **Arrastre** (por defecto): la sucesora SOLO se empuja hacia adelante,
      nunca se adelanta. Protege el plan: acortar una antecesora no debe
      arrastrar media programación hacia atrás sola.
    · **`forzar`** (ids): la actividad se reprograma EXACTAMENTE sobre su
      restricción, también hacia atrás. Es para cuando el planner edita el
      vínculo a propósito (lo crea, le cambia el tipo o el lag): ahí sí espera
      que la actividad se acomode, como en MS Project. Sin esto, pasar un FS a
      SS «no hacía nada» — la restricción nueva era más temprana y la regla de
      arrastre la descartaba.

    El rango nuevo conserva el PLAZO de la sucesora (esa es la invariante:
    reprogramar una actividad no la estira ni la encoge); los saltos y medios
    que caen fuera se descartan y el metrado se re-prorratea. Solo se fuerza a
    la actividad editada: sus propias sucesoras siguen la regla de arrastre.
    BFS en orden; los ciclos ya están vetados al crear el vínculo."""
    forzar = forzar or set()
    movidas: list = []
    cola, vistas = [actividad_id_movida], set()
    while cola:
        actual = cola.pop(0)
        if actual in vistas:
            continue
        vistas.add(actual)
        sucesoras = [r["actividad_id"] for r in await con.fetch(
            "SELECT actividad_id FROM prog_dependencias WHERE predecesora_id = $1", actual)]
        for suc_id in sucesoras:
            suc = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", suc_id)
            if not suc or suc["estado"] == "CANCELADO":
                continue
            deps = await con.fetch(
                """SELECT d.tipo, d.lag_dias, p.fecha AS pred_ini,
                          COALESCE(p.fecha_fin, p.fecha) AS pred_fin
                     FROM prog_dependencias d
                     JOIN prog_actividades p ON p.id = d.predecesora_id
                    WHERE d.actividad_id = $1 AND p.estado <> 'CANCELADO'""", suc_id)
            if not deps:
                continue
            ini_suc = suc["fecha"]
            fin_suc = suc["fecha_fin"] or ini_suc
            ancla = min(min(d["pred_ini"] for d in deps), ini_suc)
            dias_semana, feriados = await _calendario(
                con, suc["proyecto_id"], ancla - timedelta(days=30),
                max(max(d["pred_fin"] for d in deps), fin_suc) + timedelta(days=400))
            saltos = set(suc["dias_salto"] or [])
            medios = set(suc["dias_medio"] or [])
            plazo = (float(suc["plazo_dias"]) if suc["plazo_dias"] is not None
                     else _plazo_de(ini_suc, fin_suc, dias_semana, feriados, saltos, medios))

            # La restricción que manda es la más tardía de todas.
            req_ini = req_fin = None
            for d in deps:
                borde, minimo = _restriccion_dep(
                    d["tipo"] or "FS", int(d["lag_dias"] or 0),
                    d["pred_ini"], d["pred_fin"], dias_semana, feriados)
                if borde == "inicio":
                    req_ini = minimo if req_ini is None else max(req_ini, minimo)
                else:
                    req_fin = minimo if req_fin is None else max(req_fin, minimo)
            # Cada restricción propone un rango COMPLETO que conserva el plazo;
            # gana la que arranque más tarde. Así la duración es invariante
            # aunque concurran un FS y un FF sobre la misma actividad. En
            # arrastre solo cuentan las que EMPUJAN; con `forzar`, todas.
            exacto = suc_id in forzar
            candidatos = []
            if req_ini is not None and (exacto or req_ini > ini_suc):
                f, m = _fin_desde_plazo(req_ini, plazo, dias_semana, feriados, saltos, medios)
                candidatos.append((req_ini, f, m))
            if req_fin is not None and (exacto or req_fin > fin_suc):
                i, m = _inicio_desde_plazo(req_fin, plazo, dias_semana, feriados, saltos, medios)
                candidatos.append((i, req_fin, m))
            if not candidatos:
                continue                                  # ya cumple: no se toca
            nuevo_ini, nuevo_fin, medios_n = max(candidatos, key=lambda c: c[0])
            if (nuevo_ini, nuevo_fin) == (ini_suc, fin_suc):
                continue                                  # ya estaba donde toca

            rango = {nuevo_ini + timedelta(days=i)
                     for i in range((nuevo_fin - nuevo_ini).days + 1)}
            row = await con.fetchrow(
                """UPDATE prog_actividades
                   SET fecha = $2, fecha_fin = $3, dias_salto = $4, dias_medio = $5,
                       plazo_dias = $6, actualizado_en = now()
                   WHERE id = $1 RETURNING *""",
                suc_id, nuevo_ini, nuevo_fin,
                sorted(d for d in saltos if d in rango),
                sorted(d for d in set(medios_n) if d in rango), plazo)
            await _redistribuir(con, dict(row))
            movidas.append(suc_id)
            cola.append(suc_id)
    return movidas


@router.get("/actividades")
async def listar_actividades(proyecto_id: int = 1, q: str = "", limite: int = 200,
                             otm: str = ""):
    """Listado ligero para el selector de antecesoras del modal. Con otm= el
    selector trabaja en 2 pasos (primero la OTM, luego sus actividades)."""
    limite = max(1, min(int(limite or 200), 500))
    filtro = f"%{q.strip()}%" if q.strip() else None
    otm_f = otm.strip() or None
    pool = await db()
    rows = await pool.fetch(
        """SELECT id, titulo, otm_id, fecha, COALESCE(fecha_fin, fecha) AS fecha_fin, estado
           FROM prog_actividades
           WHERE proyecto_id = $1 AND estado <> 'CANCELADO'
             AND ($2::text IS NULL OR titulo ILIKE $2 OR otm_id ILIKE $2)
             AND ($4::text IS NULL OR otm_id = $4)
           ORDER BY fecha DESC, id DESC LIMIT $3""",
        proyecto_id, filtro, limite, otm_f)
    return [{**dict(r), "fecha": str(r["fecha"]), "fecha_fin": str(r["fecha_fin"])} for r in rows]


# ── Hitos (rules of credit) vistos desde Programación ────────
@router.get("/partidas/{partida_id}/hitos")
async def hitos_de_partida(partida_id: int):
    """Hitos de la partida con su % automático (acum de ev_avances / metrado
    proyectado) y si su avance viene del registro DIARIO (auto) o de un
    checkpoint manual. Si la partida no tiene hitos aún, devuelve el hito
    'Ejecución 100%' virtual (se materializa recién al primer registro)."""
    pool = await db()
    async with pool.acquire() as con:
        p = await con.fetchrow(
            """SELECT id, COALESCE(metrado_proyec, metrado_presup) AS mp, unidad
               FROM ev_partidas WHERE id = $1""", partida_id)
        if not p:
            raise HTTPException(404, "Partida no encontrada")
        mp = float(p["mp"] or 0)
        hitos = [dict(r) for r in await con.fetch(
            """SELECT h.id, h.numero, h.descripcion, h.peso, h.es_principal,
                      (SELECT cantidad_acum FROM ev_avances a
                        WHERE a.hito_id = h.id ORDER BY a.semana DESC LIMIT 1) AS acum,
                      EXISTS (SELECT 1 FROM ev_avances_diarios d
                        WHERE d.partida_id = h.partida_id AND d.hito_id = h.id) AS con_diario,
                      EXISTS (SELECT 1 FROM prog_actividades pa
                        WHERE pa.hito_id = h.id) AS con_actividad
               FROM ev_hitos h WHERE h.partida_id = $1
               ORDER BY h.numero""", partida_id)]
    if not hitos:
        return {"partida_id": partida_id, "metrado": mp, "unidad": p["unidad"],
                "hitos": [{"id": None, "numero": 1, "descripcion": "Ejecución",
                           "peso": 1.0, "es_principal": True, "pct": None,
                           "auto": True, "con_actividad": False, "virtual": True}]}
    principal = max(hitos, key=lambda h: (h["es_principal"], float(h["peso"]), -h["id"]))["id"]
    out = []
    for h in hitos:
        acum = float(h["acum"] or 0)
        out.append({
            "id": h["id"], "numero": h["numero"], "descripcion": h["descripcion"],
            "peso": float(h["peso"]), "es_principal": h["es_principal"],
            "pct": round(min(acum / mp, 1.0), 4) if mp > 0 else None,
            "acum": round(acum, 4),
            # auto = su acumulado sale del registro diario (principal o etapa
            # con sub-actividad); si no, se marca con checkpoint manual.
            "auto": h["id"] == principal or h["con_diario"],
            "con_actividad": h["con_actividad"], "virtual": False,
        })
    return {"partida_id": partida_id, "metrado": mp, "unidad": p["unidad"], "hitos": out}


@router.post("/hitos/{hito_id}/checkpoint")
async def checkpoint_hito(hito_id: int, data: dict,
                          user: dict = Depends(require_role("oficina"))):
    """Marca el avance de un hito SECUNDARIO (etapa sin registro diario):
    pct 0..1 del metrado que ya pasó por esa etapa (1 = etapa completa) en la
    fecha dada — escribe ev_avances en la semana canónica de esa fecha. Los
    hitos alimentados por el diario se rechazan (los gobierna el rollup)."""
    from routers.ev._datos import _fecha_base
    f = parse_fecha(data.get("fecha")) or fecha_lima()
    try:
        pct = float(data.get("pct", 1.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "pct debe ser un número entre 0 y 1")
    if not (0 <= pct <= 1):
        raise HTTPException(400, "pct debe estar entre 0 y 1")
    pool = await db()
    async with pool.acquire() as con:
        h = await con.fetchrow(
            """SELECT h.id, h.partida_id,
                      COALESCE(p.metrado_proyec, p.metrado_presup) AS mp
               FROM ev_hitos h JOIN ev_partidas p ON p.id = h.partida_id
               WHERE h.id = $1""", hito_id)
        if not h:
            raise HTTPException(404, "Hito no encontrado")
        principal = await _hito_principal(con, h["partida_id"])
        con_diario = await con.fetchval(
            """SELECT EXISTS (SELECT 1 FROM ev_avances_diarios
               WHERE partida_id = $1 AND COALESCE(hito_id, $2) = $3)""",
            h["partida_id"], principal, hito_id)
        if con_diario:
            raise HTTPException(409, "Este hito se alimenta del registro diario; corrige las celdas del día")
        mp = float(h["mp"] or 0)
        if mp <= 0:
            raise HTTPException(400, "La partida no tiene metrado: define el presupuesto primero")
        base = await _fecha_base(con)
        semana = max(1, semana_de(f, base)) if base else 1
        async with con.transaction():
            await con.execute(
                """INSERT INTO ev_avances (hito_id, semana, cantidad_acum, registrado_en)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (hito_id, semana)
                   DO UPDATE SET cantidad_acum = $3, registrado_en = NOW()""",
                hito_id, semana, round(pct * mp, 4))
            # El acumulado no puede DECRECER en semanas posteriores ya escritas.
            await con.execute(
                """UPDATE ev_avances SET cantidad_acum = $3, registrado_en = NOW()
                   WHERE hito_id = $1 AND semana > $2 AND cantidad_acum < $3""",
                hito_id, semana, round(pct * mp, 4))
    return {"ok": True, "hito_id": hito_id, "semana": semana, "pct": pct}


@router.get("/actividades/{act_id}/dependencias")
async def listar_dependencias(act_id: int):
    pool = await db()
    rows = await pool.fetch(
        """SELECT d.id, d.predecesora_id, d.tipo, d.lag_dias,
                  p.titulo AS pred_titulo, p.fecha AS pred_fecha,
                  COALESCE(p.fecha_fin, p.fecha) AS pred_fecha_fin, p.estado AS pred_estado
           FROM prog_dependencias d
           JOIN prog_actividades p ON p.id = d.predecesora_id
           WHERE d.actividad_id = $1 ORDER BY d.id""", act_id)
    return [{**dict(r), "pred_fecha": str(r["pred_fecha"]),
             "pred_fecha_fin": str(r["pred_fecha_fin"])} for r in rows]


@router.post("/actividades/{act_id}/dependencias")
async def crear_dependencia(act_id: int, data: dict):
    pred_id = data.get("predecesora_id")
    if not pred_id:
        raise HTTPException(400, "predecesora_id requerida")
    pred_id = int(pred_id)
    if pred_id == act_id:
        raise HTTPException(400, "Una actividad no puede ser su propia antecesora")
    try:
        lag = int(data.get("lag_dias") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "lag_dias debe ser un entero")
    if abs(lag) > 365:
        raise HTTPException(400, "lag_dias fuera de rango (±365 días)")
    tipo = str(data.get("tipo") or "FS").strip().upper()
    if tipo not in _TIPOS_DEP:
        raise HTTPException(422, f"tipo de vínculo inválido (usa {'/'.join(_TIPOS_DEP)})")
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            if await _hay_ciclo(con, pred_id, act_id):
                raise HTTPException(409, "La dependencia crearía un ciclo (la actividad ya precede a esa antecesora)")
            try:
                row = await con.fetchrow(
                    """INSERT INTO prog_dependencias (actividad_id, predecesora_id, tipo, lag_dias)
                       VALUES ($1, $2, $4, $3)
                       ON CONFLICT (actividad_id, predecesora_id)
                       DO UPDATE SET lag_dias = $3, tipo = $4 RETURNING *""",
                    act_id, pred_id, lag, tipo)
            except _ERRORES_DATO:
                raise HTTPException(400, "Actividad o antecesora inexistente")
            # El planner acaba de decidir este vínculo: la sucesora se acomoda
            # EXACTAMENTE sobre él (también hacia atrás — cambiar un FS por un
            # SS tiene que mover la actividad, no quedarse quieto).
            movidas = await recalcular_cascada(con, pred_id, forzar={act_id})
    return {**dict(row), "movidas": movidas}


@router.post("/dependencias/encadenar")
async def encadenar_dependencias(data: dict):
    """Encadena en secuencia una lista ORDENADA de actividades: 1→2→3→4.

    Es el caso masivo del planner (las etapas de una partida: habilitado →
    encofrado → vaciado → desencofrado) y hasta ahora costaba 4 gestos por
    vínculo. Los pares que ya existen se actualizan (mismo upsert que el alta
    de a una); los que crearían un ciclo se informan y NO abortan el resto."""
    ids = data.get("ids") or []
    if not isinstance(ids, list) or len(ids) < 2:
        raise HTTPException(400, "ids: se necesitan al menos 2 actividades en orden")
    if len(ids) > 100:
        raise HTTPException(400, "ids: máximo 100 actividades por encadenado")
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        raise HTTPException(400, "ids debe ser una lista de enteros")
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "ids: hay actividades repetidas en la secuencia")
    tipo = str(data.get("tipo") or "FS").strip().upper()
    if tipo not in _TIPOS_DEP:
        raise HTTPException(422, f"tipo de vínculo inválido (usa {'/'.join(_TIPOS_DEP)})")
    try:
        lag = int(data.get("lag_dias") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "lag_dias debe ser un entero")
    if abs(lag) > 365:
        raise HTTPException(400, "lag_dias fuera de rango (±365 días)")

    creados, omitidos = 0, []
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for pred_id, act_id in zip(ids, ids[1:]):
                if await _hay_ciclo(con, pred_id, act_id):
                    omitidos.append({"predecesora_id": pred_id, "actividad_id": act_id,
                                     "motivo": "crearía un ciclo"})
                    continue
                try:
                    await con.execute(
                        """INSERT INTO prog_dependencias (actividad_id, predecesora_id, tipo, lag_dias)
                           VALUES ($1, $2, $3, $4)
                           ON CONFLICT (actividad_id, predecesora_id)
                           DO UPDATE SET tipo = $3, lag_dias = $4""",
                        act_id, pred_id, tipo, lag)
                except _ERRORES_DATO:
                    raise HTTPException(400, f"Actividad inexistente en la secuencia (#{act_id} o #{pred_id})")
                creados += 1
            # Toda la secuencia se acomoda sobre los vínculos recién decididos.
            movidas = (await recalcular_cascada(con, ids[0], forzar=set(ids[1:]))
                       if creados else [])
    return {"vinculos": creados, "omitidos": omitidos, "movidas": movidas}


@router.delete("/dependencias/{dep_id}")
async def borrar_dependencia(dep_id: int):
    pool = await db()
    n = await pool.execute("DELETE FROM prog_dependencias WHERE id = $1", dep_id)
    if n == "DELETE 0":
        raise HTTPException(404, "Dependencia no encontrada")
    return {"ok": True}


# ── Lookahead-grid: la vista tipo Excel del ex-gerente ───────
# Réplica del "Anexo 01 - LookAhead" / "F030b - Planeamiento": filas =
# actividades agrupadas por OTM, columnas = días de N semanas, con el
# metrado PROGRAMADO por día (prog_metrado_dia) y el REAL por día
# (ev_avances_diarios — la MISMA tabla del módulo de Valor Ganado, así el
# avance ingresado aquí o en EV es uno solo).
@router.get("/lookahead-grid")
async def lookahead_grid(proyecto_id: int = 1, desde: str = "", semanas: int = 4):
    semanas = max(1, min(int(semanas or 4), 8))
    base = _lunes_de(parse_fecha(desde) or fecha_lima())
    fin = base + timedelta(days=semanas * 7 - 1)
    pool = await db()
    async with pool.acquire() as con:
        acts = [dict(r) for r in await con.fetch(
            _ACT_SQL + """ WHERE a.proyecto_id = $1
                AND a.fecha <= $3 AND COALESCE(a.fecha_fin, a.fecha) >= $2
                ORDER BY a.otm_id NULLS LAST, a.fecha, a.id""",
            proyecto_id, base, fin)]
        ids = [a["id"] for a in acts]
        pids = sorted({a["partida_id"] for a in acts if a["partida_id"]})
        prog_rows = await con.fetch(
            """SELECT actividad_id, fecha::text AS f, cantidad, manual FROM prog_metrado_dia
               WHERE actividad_id = ANY($1) AND fecha BETWEEN $2 AND $3""",
            ids, base, fin) if ids else []
        real_rows = await con.fetch(
            """SELECT partida_id, hito_id, fecha, fecha::text AS f, cantidad_dia,
                      actividad_id, tramo_id
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3""",
            pids, base, fin) if pids else []
        partidas = {r["id"]: dict(r) for r in await con.fetch(
            """SELECT id, unidad, metrado_presup FROM ev_partidas WHERE id = ANY($1)""",
            pids)} if pids else {}
        # Acumulado de la partida-etapa SIN los tramos: cada sub-fila lleva el
        # suyo aparte (0038) y si se sumaran aquí, la fila clásica mostraría
        # como propio el avance de las sub-filas.
        acum = {(r["partida_id"], r["hito_id"]): float(r["total"] or 0)
                for r in await con.fetch(
            """SELECT partida_id, hito_id, SUM(cantidad_dia) AS total
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND tramo_id IS NULL
               GROUP BY partida_id, hito_id""",
            pids)} if pids else {}
        acum_tramo = {r["tramo_id"]: float(r["total"] or 0) for r in await con.fetch(
            """SELECT tramo_id, SUM(cantidad_dia) AS total FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND tramo_id IS NOT NULL
               GROUP BY tramo_id""", pids)} if pids else {}
        # Ejecutado TOTAL de la partida (todas sus etapas y todos sus frentes):
        # es contra esto que se mide el saldo del presupuesto. Restarle a la base
        # de la partida solo lo de UN frente daba un saldo distinto en cada fila
        # y ninguno era el de verdad.
        acum_part = {r["partida_id"]: float(r["total"] or 0) for r in await con.fetch(
            """SELECT partida_id, SUM(cantidad_dia) AS total FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND cantidad_dia IS NOT NULL
               GROUP BY partida_id""", pids)} if pids else {}
        hitos_rows = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, descripcion, peso, es_principal FROM ev_hitos
               WHERE partida_id = ANY($1)
               ORDER BY partida_id, es_principal DESC, peso DESC, id""",
            pids)] if pids else []
        # TODAS las actividades de esas partidas (también las de fuera de la
        # ventana): hacen falta para repartir bien el real entre los tramos.
        todas_acts = [dict(r) for r in await con.fetch(
            f"""SELECT a.id, a.partida_id, a.hito_id, a.fecha,
                       COALESCE(a.fecha_fin, a.fecha) AS fecha_fin
                 FROM prog_actividades a
                WHERE a.partida_id = ANY($1) AND a.estado <> 'CANCELADO' {_CLASICAS}""",
            pids)] if pids else []
        dias_semana, feriados = await _calendario(con, proyecto_id, base, fin)
        # Sub-filas de las filas visibles (0038), TAMBIÉN las que caen fuera de
        # la ventana: una fila dividida no se edita ni se avanza, y eso tiene
        # que saberse aunque sus hijos estén en otra semana.
        hijos_rows = await con.fetch(
            "SELECT id, padre_id FROM prog_actividades WHERE padre_id = ANY($1)",
            ids) if ids else []
        deps = await con.fetch(
            """SELECT d.id AS dep_id, d.actividad_id, d.predecesora_id, d.lag_dias, d.tipo,
                      p.titulo AS pred_titulo, COALESCE(p.fecha_fin, p.fecha) AS pred_fin
               FROM prog_dependencias d
               JOIN prog_actividades p ON p.id = d.predecesora_id
               WHERE d.actividad_id = ANY($1) OR d.predecesora_id = ANY($1)""",
            ids) if ids else []

    hijos_de: dict = {}
    for r in hijos_rows:
        hijos_de.setdefault(r["padre_id"], []).append(r["id"])

    preds_map: dict = {}
    sucs_map: dict = {}
    for r in deps:
        preds_map.setdefault(r["actividad_id"], []).append({
            "id": r["predecesora_id"], "dep_id": r["dep_id"], "titulo": r["pred_titulo"],
            "fecha_fin": str(r["pred_fin"]), "lag_dias": r["lag_dias"],
            "tipo": r["tipo"] or "FS"})
        sucs_map.setdefault(r["predecesora_id"], []).append(r["actividad_id"])

    prog_map: dict = {}
    manual_map: dict = {}
    for r in prog_rows:
        prog_map.setdefault(r["actividad_id"], {})[r["f"]] = float(r["cantidad"])
        if r["manual"]:
            manual_map.setdefault(r["actividad_id"], []).append(r["f"])
    # Reales por (partida, etapa): NULL = hito principal (convención 0025).
    real_map: dict = {}
    dueno_reg: dict = {}
    real_tramo: dict = {}          # sub-fila (0038) → {fecha: cantidad}, sin reparto
    for r in real_rows:
        if r["cantidad_dia"] is None:
            continue
        if r["tramo_id"] is not None:
            real_tramo.setdefault(r["tramo_id"], {})[r["f"]] = float(r["cantidad_dia"])
            continue
        real_map.setdefault((r["partida_id"], r["hito_id"]), {})[r["f"]] = \
            float(r["cantidad_dia"])
        dueno_reg[(r["partida_id"], r["hito_id"], r["fecha"])] = r["actividad_id"]
    principal_de: dict = {}
    hitos_de: dict = {}
    for h in hitos_rows:
        hitos_de.setdefault(h["partida_id"], []).append(h)
        principal_de.setdefault(h["partida_id"], h["id"])   # 1º = principal (ORDER BY)
    hito_info = {h["id"]: h for h in hitos_rows}

    # Dueño de cada día con real, por (partida, etapa) — solo hace falta cuando
    # la partida-etapa tiene más de un tramo programado.
    def _hk(pid, hid):
        return None if hid is not None and hid == principal_de.get(pid) else hid

    tramos: dict = {}
    for x in todas_acts:
        tramos.setdefault((x["partida_id"], _hk(x["partida_id"], x["hito_id"])), []).append(x)
    dueno_de: dict = {}
    for clave, acts_clave in tramos.items():
        if len(acts_clave) < 2:
            continue
        items = [(d, dueno_reg.get((clave[0], clave[1], d)))
                 for d in (date.fromisoformat(f) for f in real_map.get(clave, {}))]
        dueno_de[clave] = {str(f): aid
                           for f, aid in _dueno_del_real(items, acts_clave).items()}

    grupos: list = []
    idx: dict = {}
    for a in acts:
        pinfo = partidas.get(a["partida_id"]) or {}
        met_base = float(pinfo["metrado_presup"]) if pinfo.get("metrado_presup") is not None else None
        # Clave de etapa de la actividad: el hito principal se guarda como NULL.
        hkey = a["hito_id"]
        if hkey is not None and hkey == principal_de.get(a["partida_id"]):
            hkey = None
        subfilas = hijos_de.get(a["id"], [])
        if a["es_frente"]:
            # Sub-fila: su real es suyo, sin reparto que adivinar (0038).
            acum_real = acum_tramo.get(a["id"], 0.0)
            real_act = real_tramo.get(a["id"], {})
        elif subfilas:
            # Contenedor: lo que muestra por día es la suma de sus sub-filas, y
            # eso lo arma el panel, que las tiene todas en la misma respuesta.
            acum_real = round(sum(acum_tramo.get(h, 0.0) for h in subfilas), 4)
            real_act = {}
        else:
            acum_real = acum.get((a["partida_id"], hkey)) if a["partida_id"] else None
            # Real de ESTA actividad (no el de toda la partida): si la partida se
            # programó en varios tramos, cada fila muestra lo suyo.
            real_act = real_map.get((a["partida_id"], hkey), {}) if a["partida_id"] else {}
            dueno = dueno_de.get((a["partida_id"], hkey))
            if dueno:
                real_act = {f: v for f, v in real_act.items() if dueno.get(f) == a["id"]}
        act_out = {
            "id": a["id"], "titulo": a["titulo"], "estado": a["estado"],
            "descripcion": a["descripcion"],
            # Área y capa del tramo (0037): con qué porción de la partida grande
            # se corresponde esta fila.
            "desglose_1": a["desglose_1"], "desglose_2": a["desglose_2"],
            "fecha": str(a["fecha"]), "fecha_fin": str(a["fecha_fin"] or a["fecha"]),
            "otm_id": a["otm_id"], "partida_id": a["partida_id"],
            "partida_codigo": a["partida_codigo"], "partida_desc": a["partida_desc"],
            # Adicional al que todavía no le llegó el presupuesto de HH: el
            # panel lo pinta en rojo para que sea fácil de completar después
            # (el dato del adicional se sabe al aprobarlo o al terminarlo).
            "partida_hh_presup": (float(a["partida_hh_presup"])
                                  if a["partida_hh_presup"] is not None else None),
            "partida_naturaleza": a["partida_naturaleza"],
            # PU de venta y OTM de la PARTIDA (no de la actividad): sin PU la
            # partida no vende en el RO; sin OTM no se puede tarear en campo.
            "partida_pu": (float(a["partida_pu"]) if a["partida_pu"] is not None else None),
            "partida_otm_id": a["partida_otm_id"],
            "responsable": a["responsable"], "supervisor_id": a["supervisor_id"],
            "supervisor_nombre": a["supervisor_nombre"],
            "causa_nc": a["causa_nc"], "causa_nc_cat": a["causa_nc_cat"],
            "causa_nc_planner": a["causa_nc_planner"],
            "causa_nc_planner_cat": a["causa_nc_planner_cat"],
            "rest_pend": a["rest_pend"], "rest_total": a["rest_total"],
            "dias_salto": [str(d) for d in (a["dias_salto"] or [])],
            "dias_medio": [str(d) for d in (a["dias_medio"] or [])],
            "plazo_dias": float(a["plazo_dias"]) if a["plazo_dias"] is not None else None,
            "modo_fecha": a["modo_fecha"] or "INICIO_PLAZO",
            "predecesoras": preds_map.get(a["id"], []),
            "sucesoras": sucs_map.get(a["id"], []),
            "dep_total": a["dep_total"],
            "und": pinfo.get("unidad") or a["und"],
            "metrado_prog": float(a["metrado_prog"]) if a["metrado_prog"] is not None else None,
            "metrado_base": met_base,
            "acum_real": acum_real,
            # En el árbol, el saldo del PRESUPUESTO es uno solo y es el de la
            # partida: la misma cifra en el padre y en todos sus frentes. Fuera
            # del árbol se conserva el saldo por etapa de siempre.
            "saldo": (round(met_base - (acum_part.get(a["partida_id"], 0.0)
                                        if (a["es_frente"] or subfilas) else acum_real), 3)
                      if met_base is not None and acum_real is not None else None),
            "acum_partida": acum_part.get(a["partida_id"]) if a["partida_id"] else None,
            "hito_id": a["hito_id"],
            "hito_desc": (hito_info.get(a["hito_id"]) or {}).get("descripcion") if a["hito_id"] else None,
            "hito_peso": float(hito_info[a["hito_id"]]["peso"]) if a["hito_id"] in hito_info else None,
            "prog": prog_map.get(a["id"], {}),
            "prog_manual": sorted(manual_map.get(a["id"], [])),
            "real": real_act,
            # Cuántos tramos comparten la partida-etapa (1 = el caso normal).
            # El panel lo usa para avisar de que el real está repartido.
            "tramos": len(tramos.get((a["partida_id"], hkey), [])) if a["partida_id"] else 0,
            # Árbol del LookAhead (0038): de qué fila cuelga, de qué tipo es y
            # cuántas sub-filas tiene (también las de fuera de la ventana, para
            # que la fila no se dibuje como editable cuando está dividida).
            "padre_id": a["padre_id"], "es_frente": a["es_frente"],
            "n_subfilas": len(subfilas),
        }
        clave = a["otm_id"] or ""
        if clave not in idx:
            idx[clave] = {"otm_id": a["otm_id"], "otm_desc": a["otm_desc"], "actividades": []}
            grupos.append(idx[clave])
        idx[clave]["actividades"].append(act_out)

    sem_out = [{"lunes": str(base + timedelta(days=i * 7)),
                "domingo": str(base + timedelta(days=i * 7 + 6)),
                "fechas": [str(base + timedelta(days=i * 7 + d)) for d in range(7)]}
               for i in range(semanas)]
    return {"desde": str(base), "hasta": str(fin), "semanas": sem_out,
            "fechas": [str(base + timedelta(days=i)) for i in range(semanas * 7)],
            "dias_semana": sorted(dias_semana), "feriados": sorted(str(f) for f in feriados),
            "grupos": grupos, "cnc": CNC}


@router.put("/actividades/{act_id}/metrado-dias")
async def editar_metrado_dias(act_id: int, data: dict):
    """Replanificar un día del PROGRAMADO (la misma lógica del avance real,
    aplicada al plan — encargo Jean 2026-07-19): la celda escrita queda
    MANUAL (protegida, 0027) y el saldo del metrado META (que NO cambia) se
    re-prorratea en los demás días hábiles sin real y sin celda manual.
    cantidad null/'' libera la celda: vuelve al prorrateo automático.
    cantidad 0 = día sin programación (protegido en 0)."""
    dias = data.get("dias") or {}
    if not isinstance(dias, dict) or not dias:
        raise HTTPException(400, "dias requerido: {\"YYYY-MM-DD\": cantidad}")
    celdas = []
    for k, v in dias.items():
        f = parse_fecha(k)
        if not f:
            raise HTTPException(400, f"fecha inválida: {k}")
        if v in (None, ""):
            celdas.append((f, None))
            continue
        try:
            cant = float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"cantidad inválida para {k}")
        if cant < 0:
            raise HTTPException(400, "la cantidad no puede ser negativa")
        celdas.append((f, cant))
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            act = await con.fetchrow("SELECT * FROM prog_actividades WHERE id = $1", act_id)
            if not act:
                raise HTTPException(404, "Actividad no encontrada")
            for f, cant in celdas:
                if cant is None:
                    await con.execute(
                        "DELETE FROM prog_metrado_dia WHERE actividad_id = $1 AND fecha = $2",
                        act_id, f)
                else:
                    await con.execute(
                        """INSERT INTO prog_metrado_dia (actividad_id, fecha, cantidad, manual)
                           VALUES ($1,$2,$3,true)
                           ON CONFLICT (actividad_id, fecha)
                           DO UPDATE SET cantidad = $3, manual = true""",
                        act_id, f, cant)
            await _redistribuir(con, dict(act))
    m = float(act["metrado_prog"]) if act["metrado_prog"] is not None else None
    return {"ok": True, "metrado_prog": m}


@router.post("/avance-dia")
async def avance_dia(data: dict):
    """Registra el metrado REAL ejecutado de una partida en un día — escribe
    en ev_avances_diarios, la misma tabla del módulo de Valor Ganado (2 vías,
    un solo dato) y re-prorratea la actividad vinculada si existe.
    cantidad null borra el registro del día."""
    partida_id = data.get("partida_id")
    f = parse_fecha(data.get("fecha"))
    if not partida_id or not f:
        raise HTTPException(400, "partida_id y fecha son obligatorios")
    cantidad = _parse_cantidad(data.get("cantidad"))
    hito_id = int(data["hito_id"]) if data.get("hito_id") else None
    pool = await db()
    async with pool.acquire() as con:
        if hito_id:
            await _validar_hito(con, int(partida_id), hito_id)
        async with con.transaction():
            try:
                await registrar_avance_partida(con, int(partida_id), f, cantidad,
                                               hito_id=hito_id)
            except _ERRORES_DATO:
                raise HTTPException(400, "Partida inexistente o datos inválidos")
    return {"ok": True, "cantidad": cantidad}


@router.get("/historial-grid")
async def historial_grid(otm: str, desde: str = "", semanas: int = 0):
    """Historial COMPLETO de avance de las partidas de una OTM, estilo Excel
    del LookAhead (tab «Avance diario» del VG): arranca en el primer registro
    (avance diario o HH de tareo, lo más antiguo) y llega hasta hoy o hasta
    el fin programado. Editable en todo el rango — misma fuente única.
    Por partida: fila por ETAPA (hito con actividad/registros propios; NULL =
    principal) con real diario, programado (prog_metrado_dia) y HH del tareo.
    desde/semanas permiten acotar la ventana (máx. 26 semanas por página)."""
    otm = otm.strip()
    if not otm:
        raise HTTPException(400, "otm requerida")
    pool = await db()
    async with pool.acquire() as con:
        partidas = [dict(r) for r in await con.fetch(
            """SELECT id, codigo, descripcion, unidad, hh_presup, metrado_presup,
                      COALESCE(metrado_proyec, metrado_presup) AS metrado,
                      CASE WHEN metrado_presup > 0 THEN hh_presup / metrado_presup
                           ELSE 0 END AS factor_conv
               FROM ev_partidas
               WHERE activo AND fase IS NOT NULL AND otm_id = $1
               ORDER BY codigo""", otm)]
        if not partidas:
            return {"otm": otm, "partidas": [], "fechas": [], "semanas": []}
        pids = [p["id"] for p in partidas]

        lim = await con.fetchrow(
            """SELECT LEAST((SELECT MIN(fecha) FROM ev_avances_diarios WHERE partida_id = ANY($1)),
                            (SELECT MIN(fecha) FROM tareo_partida WHERE partida_id = ANY($1))) AS ini,
                      GREATEST((SELECT MAX(fecha) FROM ev_avances_diarios WHERE partida_id = ANY($1)),
                               (SELECT MAX(COALESCE(fecha_fin, fecha)) FROM prog_actividades
                                WHERE partida_id = ANY($1))) AS fin""", pids)
        hoy = fecha_lima()
        ini = lim["ini"] or hoy
        fin = max(lim["fin"] or hoy, hoy)
        base = _lunes_de(parse_fecha(desde) or ini)
        fin_dom = fin + timedelta(days=6 - fin.weekday())
        n_sem = min(max(1, int(semanas) or ((fin_dom - base).days + 1) // 7), 26)
        fin_dom = min(fin_dom, base + timedelta(days=n_sem * 7 - 1))
        truncado = (base + timedelta(days=n_sem * 7 - 1)) < fin

        real_rows = await con.fetch(
            """SELECT partida_id, hito_id, fecha::text AS f, cantidad_dia
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3
                 AND cantidad_dia IS NOT NULL""", pids, base, fin_dom)
        hh_rows = await con.fetch(
            """SELECT partida_id, fecha::text AS f, SUM(hh) AS hh
               FROM tareo_partida
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3 AND hh IS NOT NULL
               GROUP BY partida_id, fecha""", pids, base, fin_dom)
        acts = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, hito_id, fecha, fecha_fin, estado,
                      dias_salto, dias_medio, metrado_prog
               FROM prog_actividades
               WHERE partida_id = ANY($1)
               ORDER BY fecha""", pids)]
        prog_rows = await con.fetch(
            """SELECT pm.actividad_id, pm.fecha::text AS f, pm.cantidad,
                      pa.partida_id, pa.hito_id
               FROM prog_metrado_dia pm
               JOIN prog_actividades pa ON pa.id = pm.actividad_id
               WHERE pa.partida_id = ANY($1) AND pm.fecha BETWEEN $2 AND $3""",
            pids, base, fin_dom)
        hitos_rows = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, descripcion, peso, es_principal FROM ev_hitos
               WHERE partida_id = ANY($1)
               ORDER BY partida_id, es_principal DESC, peso DESC, id""", pids)]
        # Partidas divididas en frentes: su avance se registra en el frente, no
        # aquí (si se escribiera aquí, el mismo trabajo se contaría dos veces).
        con_frentes = {r["partida_id"] for r in await con.fetch(
            """SELECT DISTINCT partida_id FROM prog_actividades
                WHERE partida_id = ANY($1) AND es_frente AND estado <> 'CANCELADO'""",
            pids)}
        primer_reg = {r["partida_id"]: r["ini"] for r in await con.fetch(
            """SELECT partida_id, LEAST(MIN(f1), MIN(f2)) AS ini FROM (
                 SELECT partida_id, fecha AS f1, NULL::date AS f2
                   FROM ev_avances_diarios WHERE partida_id = ANY($1)
                 UNION ALL
                 SELECT partida_id, NULL::date, fecha FROM tareo_partida
                  WHERE partida_id = ANY($1)) x GROUP BY partida_id""", pids)}
        dias_semana, feriados = await _calendario(con, 1, base, fin_dom)

    principal_de: dict = {}
    hito_info: dict = {}
    for h in hitos_rows:
        principal_de.setdefault(h["partida_id"], h["id"])
        hito_info[h["id"]] = h

    def _hkey(pid, hid):
        return None if hid is None or hid == principal_de.get(pid) else hid

    real_map: dict = {}
    for r in real_rows:
        # SUMA, no asignación: desde 0038 una partida-etapa puede tener varias
        # filas el mismo día, una por frente. Pisando el valor, el día se
        # quedaba con el avance de UN frente y el acumulado de esta vista
        # discrepaba del LookAhead (1 760 se veía como 260).
        k = (r["partida_id"], _hkey(r["partida_id"], r["hito_id"]))
        real_map.setdefault(k, {})[r["f"]] = \
            real_map.get(k, {}).get(r["f"], 0) + float(r["cantidad_dia"])
    hh_map: dict = {}
    for r in hh_rows:
        hh_map.setdefault(r["partida_id"], {})[r["f"]] = float(r["hh"])
    prog_map: dict = {}
    for r in prog_rows:
        k = (r["partida_id"], _hkey(r["partida_id"], r["hito_id"]))
        prog_map.setdefault(k, {})[r["f"]] = \
            prog_map.get(k, {}).get(r["f"], 0) + float(r["cantidad"])
    acts_map: dict = {}
    for a in acts:
        acts_map.setdefault((a["partida_id"], _hkey(a["partida_id"], a["hito_id"])),
                            []).append({
            "id": a["id"], "fecha": str(a["fecha"]),
            "fecha_fin": str(a["fecha_fin"] or a["fecha"]), "estado": a["estado"],
            "dias_salto": [str(d) for d in (a["dias_salto"] or [])],
            "dias_medio": [str(d) for d in (a["dias_medio"] or [])],
            "metrado_prog": float(a["metrado_prog"]) if a["metrado_prog"] is not None else None,
        })

    out = []
    for p in partidas:
        pid = p["id"]
        claves = sorted(
            {k[1] for m in (real_map, prog_map, acts_map) for k in m if k[0] == pid},
            key=lambda h: (h is not None, h or 0))
        if not claves:
            claves = [None]
        etapas = [{
            "hito_id": hk,
            "hito_desc": hito_info[hk]["descripcion"] if hk in hito_info else None,
            "hito_peso": float(hito_info[hk]["peso"]) if hk in hito_info else None,
            "real": real_map.get((pid, hk), {}),
            "prog": prog_map.get((pid, hk), {}),
            "actividades": acts_map.get((pid, hk), []),
        } for hk in claves]
        out.append({
            "id": pid, "codigo": p["codigo"], "descripcion": p["descripcion"],
            "unidad": p["unidad"], "metrado": float(p["metrado"] or 0),
            "factor_conv": round(float(p["factor_conv"] or 0), 4),
            "hh_presup": float(p["hh_presup"] or 0),
            "hh": hh_map.get(pid, {}),
            "primer_registro": str(primer_reg[pid]) if primer_reg.get(pid) else None,
            "sin_registros": primer_reg.get(pid) is None,
            # El día se suma de sus frentes: aquí se lee, no se escribe.
            "con_frentes": pid in con_frentes,
            "etapas": etapas,
        })

    n_dias = (fin_dom - base).days + 1
    sem_out = [{"lunes": str(base + timedelta(days=i * 7)),
                "domingo": str(base + timedelta(days=i * 7 + 6)),
                "fechas": [str(base + timedelta(days=i * 7 + d)) for d in range(7)]}
               for i in range(n_dias // 7)]
    return {"otm": otm, "desde": str(base), "hasta": str(fin_dom),
            "truncado": truncado, "hoy": str(hoy),
            "fechas": [str(base + timedelta(days=i)) for i in range(n_dias)],
            "semanas": sem_out, "dias_semana": sorted(dias_semana),
            "feriados": sorted(str(f) for f in feriados), "partidas": out}


@router.get("/actividades/{act_id}/restricciones")
async def listar_restricciones(act_id: int):
    pool = await db()
    rows = await pool.fetch(
        "SELECT * FROM prog_restricciones WHERE actividad_id = $1 ORDER BY liberada, id",
        act_id)
    return [dict(r) for r in rows]


@router.post("/actividades/{act_id}/restricciones")
async def crear_restriccion(act_id: int, data: dict):
    desc = str(data.get("descripcion") or "").strip()
    if not desc:
        raise HTTPException(400, "descripcion requerida")
    tipo = str(data.get("tipo") or "OTROS").strip().upper()
    if tipo not in _TIPOS_RESTRICCION:
        raise HTTPException(422, f"tipo inválido (usa {'/'.join(_TIPOS_RESTRICCION)})")
    pool = await db()
    try:
        row = await pool.fetchrow(
            """INSERT INTO prog_restricciones
               (actividad_id, descripcion, tipo, responsable, fecha_requerida)
               VALUES ($1,$2,$3,$4,$5) RETURNING *""",
            act_id, desc, tipo, data.get("responsable") or None,
            parse_fecha(data.get("fecha_requerida")))
    except _ERRORES_DATO:
        raise HTTPException(400, "Actividad inexistente o datos inválidos")
    return dict(row)


@router.put("/restricciones/{rest_id}")
async def editar_restriccion(rest_id: int, data: dict):
    campos, valores = [], []
    for k in ("descripcion", "responsable"):
        if k in data:
            campos.append(f"{k} = ${len(valores) + 2}")
            valores.append(str(data[k]).strip() or None)
    if "fecha_requerida" in data:
        campos.append(f"fecha_requerida = ${len(valores) + 2}")
        valores.append(parse_fecha(data["fecha_requerida"]))
    if "liberada" in data:
        lib = bool(data["liberada"])
        campos.append(f"liberada = ${len(valores) + 2}")
        valores.append(lib)
        campos.append("liberada_en = " + ("now()" if lib else "NULL"))
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    pool = await db()
    row = await pool.fetchrow(
        f"UPDATE prog_restricciones SET {', '.join(campos)} WHERE id = $1 RETURNING *",
        rest_id, *valores)
    if not row:
        raise HTTPException(404, "Restricción no encontrada")
    return dict(row)


@router.delete("/restricciones/{rest_id}")
async def borrar_restriccion(rest_id: int):
    pool = await db()
    n = await pool.execute("DELETE FROM prog_restricciones WHERE id = $1", rest_id)
    if n == "DELETE 0":
        raise HTTPException(404, "Restricción no encontrada")
    return {"ok": True}


async def _detalle_semana(con, proyecto_id: int, lunes: date, hasta: date) -> list:
    """Compromiso y avance de UNA semana, actividad por actividad, contando solo
    hasta `hasta` (el corte, que puede caer antes del domingo).

    Es la misma regla que usa `/ppc` —incluida la atribución del real entre
    tramos hermanos de la misma partida-etapa— y por eso vive aquí y no en el
    módulo de cierre: si el cierre congelara números calculados de otra manera,
    el PPC congelado no coincidiría con el que el planner acababa de ver.
    """
    # Las causas viajan para que el cierre PREcargue lo que el planner ya
    # escribió en la evaluación semanal: la misma precedencia del Pareto
    # (planner > campo). Sin esto habría que teclear la causa dos veces, y
    # dos textos distintos para el mismo hecho es peor que ninguno.
    # La causa de CAMPO viaja además cruda y aparte: es lo que el supervisor
    # escribió el día que no salió el trabajo, y es la materia prima con la que
    # el planner redacta la explicación del cliente. Sin tenerla a la vista al
    # cerrar habría que ir a buscarla a otra pantalla.
    acts = [dict(r) for r in await con.fetch(
        """SELECT a.id, a.titulo, a.partida_id, a.hito_id, a.estado, a.supervisor_id,
                  a.creado_en, a.fecha, COALESCE(a.fecha_fin, a.fecha) AS fecha_fin,
                  COALESCE(a.causa_nc_planner_cat, a.causa_nc_cat) AS causa_cat,
                  COALESCE(a.causa_nc_planner, a.causa_nc) AS causa,
                  a.causa_nc_cat AS causa_campo_cat, a.causa_nc AS causa_campo,
                  p.unidad
             FROM prog_actividades a
             LEFT JOIN ev_partidas p ON p.id = a.partida_id
            WHERE a.proyecto_id = $1 AND a.estado <> 'CANCELADO'
              AND a.fecha <= $3 AND COALESCE(a.fecha_fin, a.fecha) >= $2""",
        proyecto_id, lunes, hasta)]
    if not acts:
        return []
    ids = [a["id"] for a in acts]
    pids = sorted({a["partida_id"] for a in acts if a["partida_id"]})
    comp = {r["actividad_id"]: float(r["c"] or 0) for r in await con.fetch(
        """SELECT actividad_id, SUM(cantidad) AS c FROM prog_metrado_dia
            WHERE actividad_id = ANY($1) AND fecha BETWEEN $2 AND $3
            GROUP BY 1""", ids, lunes, hasta)}
    principal = {r["partida_id"]: r["id"] for r in await con.fetch(
        """SELECT DISTINCT ON (partida_id) partida_id, id FROM ev_hitos
            WHERE partida_id = ANY($1)
            ORDER BY partida_id, es_principal DESC, peso DESC, id""",
        pids)} if pids else {}
    # Tramos de la misma partida-etapa, también fuera de la semana: sin esto el
    # avance de un tramo daría por cumplido al otro.
    todas = [dict(r) for r in await con.fetch(
        """SELECT id, partida_id, hito_id, fecha, COALESCE(fecha_fin, fecha) AS fecha_fin
             FROM prog_actividades
            WHERE partida_id = ANY($1) AND estado <> 'CANCELADO'""",
        pids)] if pids else []
    filas = await con.fetch(
        """SELECT partida_id, hito_id, fecha, actividad_id, SUM(cantidad_dia) AS c
             FROM ev_avances_diarios
            WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3
              AND cantidad_dia IS NOT NULL
            GROUP BY 1,2,3,4""", pids, lunes, hasta) if pids else []

    def _etapa(pid, hid):
        return None if hid is not None and hid == principal.get(pid) else hid

    tramos: dict = {}
    for x in todas:
        tramos.setdefault((x["partida_id"], _etapa(x["partida_id"], x["hito_id"])), []).append(x)
    por_clave: dict = {}
    dueno_reg: dict = {}
    for r in filas:
        clave = (r["partida_id"], _etapa(r["partida_id"], r["hito_id"]))
        por_clave.setdefault(clave, {})
        por_clave[clave][r["fecha"]] = por_clave[clave].get(r["fecha"], 0.0) + float(r["c"] or 0)
        if r["actividad_id"]:
            dueno_reg[(clave, r["fecha"])] = r["actividad_id"]
    real_clave: dict = {}
    real_act: dict = {}
    for clave, dias in por_clave.items():
        hermanas = tramos.get(clave, [])
        dueno = (_dueno_del_real([(f, dueno_reg.get((clave, f))) for f in dias], hermanas)
                 if len(hermanas) > 1 else {})
        for f, c in dias.items():
            aid = dueno.get(f) if dueno else None
            if aid is None:
                real_clave[clave] = real_clave.get(clave, 0.0) + c
            else:
                real_act[aid] = real_act.get(aid, 0.0) + c

    out = []
    for a in acts:
        clave = (a["partida_id"], _etapa(a["partida_id"], a["hito_id"]))
        alcanzado = (real_act.get(a["id"], 0.0) if len(tramos.get(clave, [])) > 1
                     else real_clave.get(clave, 0.0)) if a["partida_id"] else 0.0
        out.append({
            "actividad_id": a["id"], "titulo": a["titulo"],
            "partida_id": a["partida_id"], "supervisor_id": a["supervisor_id"],
            "estado": a["estado"], "creado_en": a["creado_en"],
            "fecha": a["fecha"], "unidad": a["unidad"],
            "comprometido": round(comp.get(a["id"], 0.0), 3),
            "alcanzado": round(alcanzado, 3),
            "causa_cat": a["causa_cat"], "causa": a["causa"],
            "causa_campo_cat": a["causa_campo_cat"], "causa_campo": a["causa_campo"],
        })
    return out


async def restricciones_semana(con, proyecto_id: int, lunes: date, hasta: date) -> dict:
    """Restricciones que los supervisores reportaron en la semana, por actividad.

    La actividad SÍ se hizo pero algo le bajó el rendimiento (0032). No afectan
    el PPC, pero son lo que el planner necesita leer para escribir la causa: si
    campo reportó tres días seguidos que no llegó el concreto, la explicación
    del cliente se redacta sola.
    """
    out: dict = {}
    for r in await con.fetch(
            """SELECT actividad_id, fecha, restricciones FROM campo_reportes
                WHERE proyecto_id = $1 AND fecha BETWEEN $2 AND $3
                  AND actividad_id IS NOT NULL AND restricciones IS NOT NULL
                ORDER BY fecha""", proyecto_id, lunes, hasta):
        try:
            items = json.loads(r["restricciones"]) or []
        except (ValueError, TypeError):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            out.setdefault(r["actividad_id"], []).append({
                "cat": str(it.get("cat") or "OTROS").upper(),
                "detalle": str(it.get("detalle") or "").strip(),
                "fecha": str(r["fecha"])})
    return out


def ventana_ppc(hoy: date, semanas: int = 8, desde: str = "", hasta: str = "") -> tuple:
    """Rango de semanas del PPC: (primer lunes, último domingo).

    Con `desde`/`hasta` manda el rango pedido, alineado a semanas ISO; si no,
    las últimas N semanas hasta la actual. Sin esto, un reporte del 13 al 26 de
    julio salía con el histograma y la tendencia de las últimas 8 semanas —
    incluida la semana en curso, que arrastraba la recta hacia abajo por estar
    a medio correr.
    """
    f_desde, f_hasta = parse_fecha(desde), parse_fecha(hasta)
    if f_desde:
        ini = _lunes_de(f_desde)
        fin = _lunes_de(f_hasta or f_desde)
        if fin < ini:
            ini, fin = fin, ini
        # Mismo tope que `semanas`: 26 semanas de historia por consulta.
        if (fin - ini).days > 25 * 7:
            ini = fin - timedelta(days=25 * 7)
        return ini, fin + timedelta(days=6)
    n = max(1, min(int(semanas or 8), 26))
    lun = _lunes_de(hoy)
    return lun - timedelta(days=(n - 1) * 7), lun + timedelta(days=6)


@router.get("/ppc")
async def ppc(proyecto_id: int = 1, semanas: int = 8,
              p_desde: str = Query("", alias="desde"),
              p_hasta: str = Query("", alias="hasta")):
    """PPC (Porcentaje de Plan Cumplido) semanal + Pareto de causas + detalle
    por supervisor — el nivel de APRENDIZAJE del LPS.

    Cumplimiento AUTOMÁTICO por metrado (encargo Jean 2026-07-19, «al cierre
    + SI anticipado»): el compromiso de una actividad en una semana es su
    metrado PROGRAMADO de esa semana; se cumple apenas el real registrado lo
    alcanza o supera (aunque la semana siga corriendo) y recién cuenta como
    NO cumplida cuando la semana CERRÓ sin llegar — mientras corre queda "en
    curso" (comprometida sin veredicto). Los estados manuales mandan:
    NO_CUMPLIDA → no cumplida (su causa alimenta el Pareto) · EJECUTADO →
    cumplida · CANCELADO se excluye. Las actividades sin celdas programadas
    (sin metrado) se evalúan por estado en la semana de su F.Inicio."""
    hoy = fecha_lima()
    desde, hasta = ventana_ppc(hoy, semanas, p_desde, p_hasta)
    pool = await db()
    async with pool.acquire() as con:
        acts = [dict(r) for r in await con.fetch(
            """SELECT id, partida_id, hito_id, estado, fecha,
                      COALESCE(fecha_fin, fecha) AS fecha_fin, supervisor_id
               FROM prog_actividades
               WHERE proyecto_id = $1
                 AND fecha <= $3 AND COALESCE(fecha_fin, fecha) >= $2""",
            proyecto_id, desde, hasta)]
        ids = [a["id"] for a in acts]
        pids = sorted({a["partida_id"] for a in acts if a["partida_id"]})
        # Tramos de cada partida-etapa (también fuera de la ventana) para poder
        # atribuir el real a la actividad correcta.
        todas_acts = [dict(r) for r in await con.fetch(
            f"""SELECT a.id, a.partida_id, a.hito_id, a.fecha,
                       COALESCE(a.fecha_fin, a.fecha) AS fecha_fin
                 FROM prog_actividades a
                WHERE a.partida_id = ANY($1) AND a.estado <> 'CANCELADO' {_CLASICAS}""",
            pids)] if pids else []
        prog_rows = await con.fetch(
            """SELECT actividad_id,
                      (fecha - ((EXTRACT(ISODOW FROM fecha)::int) - 1)) AS lunes,
                      SUM(cantidad) AS c
               FROM prog_metrado_dia
               WHERE actividad_id = ANY($1) AND fecha BETWEEN $2 AND $3
               GROUP BY 1, 2""", ids, desde, hasta) if ids else []
        # Por FECHA (no agregado por semana): el real se atribuye primero a su
        # actividad y recién después se suma por semana.
        real_rows = await con.fetch(
            """SELECT partida_id, hito_id, fecha, actividad_id, SUM(cantidad_dia) AS c
               FROM ev_avances_diarios
               WHERE partida_id = ANY($1) AND fecha BETWEEN $2 AND $3
                 AND cantidad_dia IS NOT NULL
               GROUP BY 1, 2, 3, 4""", pids, desde, hasta) if pids else []
        principal = {r["partida_id"]: r["id"] for r in await con.fetch(
            """SELECT DISTINCT ON (partida_id) partida_id, id FROM ev_hitos
               WHERE partida_id = ANY($1)
               ORDER BY partida_id, es_principal DESC, peso DESC, id""",
            pids)} if pids else {}
        supervisores = {r["id"]: r["nombre"] for r in await con.fetch(
            "SELECT id, nombre FROM supervisores")}
        # Pareto (F3 v2): manda la causa del PLANNER; si no existe, la de campo.
        # Con el lunes, para poder descartar las semanas ya CERRADAS: ahí la
        # causa que vale es la que quedó congelada en el cierre.
        cnc_rows = await con.fetch(
            """SELECT (fecha - ((EXTRACT(ISODOW FROM fecha)::int) - 1)) AS lunes,
                      COALESCE(causa_nc_planner_cat, causa_nc_cat, 'OTROS') AS causa,
                      count(*) AS n
               FROM prog_actividades
               WHERE proyecto_id = $1 AND estado = 'NO_CUMPLIDA' AND fecha BETWEEN $2 AND $3
               GROUP BY 1, 2""", proyecto_id, desde, hasta)
        # Restricciones reportadas por el supervisor (0032): la actividad SÍ se
        # hizo pero algo le bajó el rendimiento. Van aparte de las causas de
        # no cumplimiento — son cosas distintas y mezclarlas ensucia el PPC.
        rest_rows = await con.fetch(
            """SELECT r.actividad_id, r.fecha, r.restricciones, r.supervisor_id
               FROM campo_reportes r
               WHERE r.proyecto_id = $1 AND r.fecha BETWEEN $2 AND $3
                 AND r.restricciones IS NOT NULL""", proyecto_id, desde, hasta)

    prog_de: dict = {}
    for r in prog_rows:
        prog_de.setdefault(r["actividad_id"], {})[r["lunes"]] = float(r["c"] or 0)

    def _etapa_de(pid, hid):
        """Etapa normalizada: el hito principal se guarda como NULL en el diario."""
        return None if hid is not None and hid == principal.get(pid) else hid

    def _etapa(a: dict):
        return _etapa_de(a["partida_id"], a["hito_id"])

    # Real por (actividad, semana). Cuando la partida-etapa tiene varios tramos,
    # cada día se le asigna al tramo que le corresponde (§ _dueno_del_real):
    # antes las dos actividades veían el mismo real y las dos se daban por
    # cumplidas con el trabajo de una sola.
    tramos: dict = {}
    for x in todas_acts:
        tramos.setdefault((x["partida_id"], _etapa_de(x["partida_id"], x["hito_id"])),
                          []).append(x)
    reales: dict = {}
    por_clave: dict = {}
    dueno_reg: dict = {}
    for r in real_rows:
        clave = (r["partida_id"], _etapa_de(r["partida_id"], r["hito_id"]))
        por_clave.setdefault(clave, {})[r["fecha"]] = \
            por_clave.get(clave, {}).get(r["fecha"], 0.0) + float(r["c"] or 0)
        if r["actividad_id"]:
            dueno_reg[(clave, r["fecha"])] = r["actividad_id"]
    for clave, dias in por_clave.items():
        hermanas = tramos.get(clave, [])
        dueno = (_dueno_del_real([(f, dueno_reg.get((clave, f))) for f in dias], hermanas)
                 if len(hermanas) > 1 else {})
        for f, c in dias.items():
            aid = dueno.get(f) if dueno else None
            lun = _lunes_de(f)
            if aid is None:                       # un solo tramo: como siempre
                reales[(clave[0], clave[1], lun)] = \
                    reales.get((clave[0], clave[1], lun), 0.0) + c
            else:
                reales[("act", aid, lun)] = reales.get(("act", aid, lun), 0.0) + c

    def _alcanzado(a: dict, lun) -> float:
        if not a["partida_id"]:
            return 0.0
        clave = (a["partida_id"], _etapa(a))
        if len(tramos.get(clave, [])) > 1:
            return reales.get(("act", a["id"], lun), 0.0)
        return reales.get((clave[0], clave[1], lun), 0.0)

    sem: dict = {}
    # Por (supervisor, semana), no por supervisor a secas: una semana CERRADA
    # manda con sus números congelados y hay que poder sustituir solo esa.
    sup_sem: dict = {}

    def _suma(lunes, sup_id, comp, cump, noc):
        s = sem.setdefault(lunes, {"comprometidas": 0, "cumplidas": 0, "no_cumplidas": 0})
        s["comprometidas"] += comp; s["cumplidas"] += cump; s["no_cumplidas"] += noc
        if sup_id:
            v = sup_sem.setdefault((sup_id, lunes), {"comprometidas": 0, "cumplidas": 0})
            v["comprometidas"] += comp; v["cumplidas"] += cump

    for a in acts:
        if a["estado"] == "CANCELADO":
            continue
        por_semana = prog_de.get(a["id"])
        if por_semana:
            for lun, comprom in por_semana.items():
                if comprom <= 0:
                    continue
                alcanz = _alcanzado(a, lun)
                cerrada = lun + timedelta(days=6) < hoy
                if a["estado"] == "NO_CUMPLIDA":
                    cump, noc = 0, 1
                elif a["estado"] == "EJECUTADO" or alcanz >= comprom - 5e-4:
                    cump, noc = 1, 0
                elif cerrada:
                    cump, noc = 0, 1
                else:
                    cump, noc = 0, 0                    # en curso: sin veredicto
                _suma(lun, a["supervisor_id"], 1, cump, noc)
        else:
            # Sin celdas programadas (actividad de apoyo, o una con metrado a la
            # que el re-prorrateo no le dejó saldo): se evalúa en la semana de su
            # F.Inicio, con la MISMA regla de cierre que la rama de arriba. Antes
            # se quedaba comprometida sin veredicto para siempre: bajaba el PPC
            # (contaba en el denominador y nunca en el numerador) y no aparecía
            # en `no_cumplidas`, así que las cifras no cuadraban.
            # Sin nada contra qué comparar, el proxy es si hubo ejecución: un
            # avance registrado vale tanto como la marca de EJECUTADO. Juzgar
            # solo por estado declaraba NO CUMPLIDA a la actividad que termina
            # su metrado antes de tiempo — el re-prorrateo le vacía el
            # compromiso justo por haber cumplido.
            lun = _lunes_de(a["fecha"])
            if not (desde <= lun <= hasta):
                continue
            cerrada = lun + timedelta(days=6) < hoy
            if a["estado"] == "NO_CUMPLIDA":       # la marca manual manda
                cump, noc = 0, 1
            elif a["estado"] == "EJECUTADO" or _alcanzado(a, lun) > 0:
                cump, noc = 1, 0
            elif cerrada:
                cump, noc = 0, 1
            else:
                cump, noc = 0, 0                        # en curso: sin veredicto
            _suma(lun, a["supervisor_id"], 1, cump, noc)

    # ── Semanas CERRADAS: manda lo congelado ──────────────────
    # Una vez cerrada la semana, su PPC no se recalcula: reprogramar o agregar
    # trabajo después ya no puede cambiar lo que pasó (§ prog_cierre.py). Las
    # semanas sin cierre siguen calculándose sobre el plan vigente y se rotulan
    # como tales, para no dar por firme algo que todavía se mueve.
    cerradas: dict = {}
    det_cerrado: dict = {}
    async with pool.acquire() as con:
        for r in await con.fetch(
                """SELECT * FROM prog_semana_cierre
                    WHERE proyecto_id = $1 AND lunes BETWEEN $2 AND $3""",
                proyecto_id, desde, hasta):
            cerradas[r["lunes"]] = dict(r)
        if cerradas:
            for r in await con.fetch(
                    """SELECT c.lunes, d.supervisor_id, d.cumplida, d.no_planificada,
                              d.causa_cat
                         FROM prog_semana_cierre_det d
                         JOIN prog_semana_cierre c ON c.id = d.cierre_id
                        WHERE c.proyecto_id = $1 AND c.lunes BETWEEN $2 AND $3""",
                    proyecto_id, desde, hasta):
                det_cerrado.setdefault(r["lunes"], []).append(dict(r))

    for lun, c in cerradas.items():
        sem[lun] = {"comprometidas": c["comprometidas"], "cumplidas": c["cumplidas"],
                    "no_cumplidas": c["no_cumplidas"]}
        for k in [k for k in sup_sem if k[1] == lun]:
            sup_sem.pop(k)
        for d in det_cerrado.get(lun, []):
            if d["no_planificada"] or not d["supervisor_id"]:
                continue
            v = sup_sem.setdefault((d["supervisor_id"], lun),
                                   {"comprometidas": 0, "cumplidas": 0})
            v["comprometidas"] += 1
            v["cumplidas"] += 1 if d["cumplida"] else 0

    sup: dict = {}
    for (sid, _lun), v in sup_sem.items():
        acc = sup.setdefault(sid, {"comprometidas": 0, "cumplidas": 0})
        acc["comprometidas"] += v["comprometidas"]
        acc["cumplidas"] += v["cumplidas"]

    # Pareto: la causa congelada manda en las semanas cerradas; en las abiertas,
    # la que tenga hoy la actividad. Nunca las dos, que duplicaría el conteo.
    cnc_cont: dict = {}
    for r in cnc_rows:
        if r["lunes"] in cerradas:
            continue
        cnc_cont[r["causa"]] = cnc_cont.get(r["causa"], 0) + int(r["n"])
    for lun, filas_det in det_cerrado.items():
        for d in filas_det:
            if d["cumplida"] or d["no_planificada"]:
                continue
            c = d["causa_cat"] or "OTROS"
            cnc_cont[c] = cnc_cont.get(c, 0) + 1

    def _ppc(c, e):
        return round(e / c, 4) if c else None

    # Restricciones: detalle por actividad (para la tabla F030b) + su propio
    # Pareto (qué problema se repite aunque el trabajo sí se haya hecho).
    rest_por_act: dict = {}
    rest_cont: dict = {}
    for r in rest_rows:
        try:
            items = json.loads(r["restricciones"]) or []
        except (ValueError, TypeError):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            cat = str(it.get("cat") or "OTROS").upper()
            det = str(it.get("detalle") or "").strip()
            if r["actividad_id"]:
                rest_por_act.setdefault(r["actividad_id"], []).append(
                    {"cat": cat, "detalle": det, "fecha": str(r["fecha"])})
            rest_cont[cat] = rest_cont.get(cat, 0) + 1

    return {
        "desde": str(desde), "hasta": str(hasta), "cnc_catalogo": CNC,
        "restricciones": {str(k): v for k, v in rest_por_act.items()},
        "pareto_restricciones": [
            {"causa": c, "etiqueta": CNC.get(c, c), "n": n}
            for c, n in sorted(rest_cont.items(), key=lambda kv: -kv[1])],
        "semanal": [{"lunes": str(lun), "comprometidas": v["comprometidas"],
                     "cumplidas": v["cumplidas"], "no_cumplidas": v["no_cumplidas"],
                     "ppc": _ppc(v["comprometidas"], v["cumplidas"]),
                     # Congelada = el número ya no se mueve. Sin cerrar, el PPC
                     # es sobre el plan VIGENTE y puede cambiar: hay que decirlo.
                     "congelada": lun in cerradas,
                     "parcial": bool(cerradas.get(lun, {}).get("parcial")),
                     "hasta": (str(cerradas[lun]["hasta"]) if lun in cerradas
                               else str(min(lun + timedelta(days=6), hoy))),
                     "no_planificadas": cerradas.get(lun, {}).get("no_planificadas", 0)}
                    for lun, v in sorted(sem.items())],
        "cnc": [{"causa": c, "etiqueta": CNC.get(c, c), "n": n}
                for c, n in sorted(cnc_cont.items(), key=lambda kv: -kv[1])],
        "por_supervisor": [{"supervisor_id": sid, "nombre": supervisores.get(sid),
                            "comprometidas": v["comprometidas"], "cumplidas": v["cumplidas"],
                            "ppc": _ppc(v["comprometidas"], v["cumplidas"])}
                           for sid, v in sorted(sup.items(),
                                                key=lambda kv: supervisores.get(kv[0]) or "")],
    }


def _parse_part_args(partidas: str, desde: str, hasta: str):
    pids = [int(x) for x in str(partidas).split(",") if str(x).strip().isdigit()]
    if not pids:
        raise HTTPException(400, "Elige al menos una partida")
    return (pids, parse_fecha(desde) or date(2000, 1, 1),
            parse_fecha(hasta) or date(2100, 1, 1))


def _periodo_txt(desde: str, hasta: str) -> str:
    if desde or hasta:
        return f"Periodo: {desde or 'inicio'} — {hasta or 'hoy'}"
    return "Todo el historial registrado"


async def _datos_reporte_partida(con, pids: list, f_desde: date, f_hasta: date) -> list:
    """Ensamblado compartido del sustento por partida: un bloque por partida con
    sus cifras + los partes de campo (orden cronológico, del más antiguo al más
    nuevo) y sus fotos CRUDAS (traen `ruta`/`purgada`/`ancho`/`alto`). El
    endpoint JSON mapea las fotos con `_foto_out` (URLs firmadas); el ZIP las
    lee del disco para embeberlas en el PDF."""
    parts = await con.fetch(
        """SELECT p.id, p.codigo, p.descripcion, p.unidad, p.otm_id,
                  p.metrado_presup, p.hh_presup, o.descripcion AS otm_desc
           FROM ev_partidas p LEFT JOIN otms o ON o.id = p.otm_id
           WHERE p.id = ANY($1) ORDER BY p.codigo""", pids)
    if not parts:
        raise HTTPException(404, "No encontré esas partidas")
    # Cantidad instalada = acumulado del hito principal (mismo criterio del motor EV)
    ejec = {r["partida_id"]: float(r["cant"] or 0) for r in await con.fetch(
        """SELECT DISTINCT ON (h.partida_id) h.partida_id, a.cantidad_acum AS cant
           FROM ev_avances a JOIN ev_hitos h ON h.id = a.hito_id
           WHERE h.partida_id = ANY($1)
           ORDER BY h.partida_id, h.es_principal DESC, a.semana DESC""", pids)}
    reps = await con.fetch(
        """SELECT r.*, s.nombre AS supervisor_nombre, a.partida_id,
                  a.titulo AS act_titulo, o.area AS otm_area
           FROM campo_reportes r
           JOIN prog_actividades a ON a.id = r.actividad_id
           LEFT JOIN supervisores s ON s.id = r.supervisor_id
           LEFT JOIN otms o ON o.id = r.otm_id
           WHERE a.partida_id = ANY($1) AND r.fecha BETWEEN $2 AND $3
           ORDER BY r.fecha, r.id""", pids, f_desde, f_hasta)
    fotos = [dict(r) for r in await con.fetch(
        """SELECT f.*, r.id AS rid FROM campo_fotos f
           JOIN campo_reportes r ON r.id = f.reporte_id
           JOIN prog_actividades a ON a.id = r.actividad_id
           WHERE a.partida_id = ANY($1) AND r.fecha BETWEEN $2 AND $3
           ORDER BY f.id""", pids, f_desde, f_hasta)]
    # Personal por (fecha, supervisor, PARTIDA) desde el tareo real. Ojo: se
    # acota a la partida a propósito — este documento sustenta UNA partida,
    # así que la cuadrilla que muestra tiene que ser la que trabajó en ella
    # (antes contaba todo el día del supervisor y podía mostrar gente de
    # otra OTM mientras las HH de la partida salían en 0).
    hh_dia = await con.fetch(
        """SELECT tp.fecha, tp.supervisor_id, tp.partida_id,
                  COALESCE(t.cargo,'SIN CARGO') AS cargo,
                  COUNT(DISTINCT tp.trabajador_id) AS n,
                  SUM(tp.hh) AS hh
           FROM tareo_partida tp LEFT JOIN trabajadores t ON t.id = tp.trabajador_id
           WHERE tp.partida_id = ANY($1) AND tp.fecha BETWEEN $2 AND $3
           GROUP BY 1,2,3,4 ORDER BY n DESC""", pids, f_desde, f_hasta)
    # HH gastadas por partida: fuente única del motor (manual > tareo > histórico)
    from routers.ev._datos import _hh_gastadas_unificada
    hh_unif = await _hh_gastadas_unificada(con)

    hh_por_partida: dict = {}
    for (pid, _sem), v in hh_unif.items():
        if pid in pids:
            hh_por_partida[pid] = hh_por_partida.get(pid, 0.0) + float(v or 0)

    out = []
    for p in parts:
        mp = float(p["metrado_presup"] or 0)
        me = ejec.get(p["id"], 0.0)
        bloques = []
        for r in [x for x in reps if x["partida_id"] == p["id"]]:
            notas = json.loads(r["anotaciones"]) if r["anotaciones"] else (
                [ln.lstrip("•- ").strip() for ln in (r["descripcion"] or "").split("\n") if ln.strip()])
            # Si el supervisor no escribió viñetas, el sustento no puede quedar
            # con "ACTIVIDADES REALIZADAS" en blanco: cae al título de la
            # actividad programada, que es lo que efectivamente se ejecutó.
            if not notas and r["act_titulo"]:
                notas = [r["act_titulo"]]
            filas_dia = [x for x in hh_dia
                         if x["fecha"] == r["fecha"] and x["partida_id"] == p["id"]
                         and x["supervisor_id"] == r["supervisor_id"]]
            personal = [{"cargo": x["cargo"], "n": x["n"]} for x in filas_dia]
            bloques.append({
                "id": r["id"], "fecha": str(r["fecha"]), "area": r["area"],
                "frente": r["frente"],
                "turno": r["turno"], "actividad": r["act_titulo"],
                "supervisor": r["supervisor_nombre"] or r["supervisor_id"],
                "hh_dia": round(sum(float(x["hh"] or 0) for x in filas_dia), 2),
                # Sin restricciones: el sustento de valorización acredita lo
                # EJECUTADO; las restricciones viven en el PPC, no aquí.
                "texto": armar_texto_reporte(
                    r["fecha"], r["turno"] or "DIA", r["supervisor_nombre"] or r["supervisor_id"],
                    personal, [{"area": r["area"] or r["otm_area"] or "",
                                "frente": r["frente"], "items": notas}], []),
                # Fotos CRUDAS (con `ruta`): cada endpoint las adapta a su salida.
                "fotos": [f for f in fotos if f["rid"] == r["id"]],
            })
        hh_rango = round(sum(float(x["hh"] or 0) for x in hh_dia if x["partida_id"] == p["id"]), 2)
        hh_tot = round(hh_por_partida.get(p["id"], 0.0), 2)
        out.append({
            "partida": {"id": p["id"], "codigo": p["codigo"], "descripcion": p["descripcion"],
                        "unidad": p["unidad"], "otm_id": p["otm_id"], "otm_desc": p["otm_desc"],
                        "metrado_presup": mp, "metrado_ejec": me,
                        "avance": round(me / mp, 4) if mp else None,
                        "hh_presup": float(p["hh_presup"] or 0),
                        "hh_gastadas": hh_tot, "hh_rango": hh_rango,
                        # Aviso honesto: hay partes de campo pero ninguna HH del
                        # tareo cayó en esta partida (el tareo se envió sin
                        # partida, con 0 HH, o lo reemplazó un envío posterior).
                        "sin_tareo": bool(bloques) and hh_tot == 0},
            "reportes": bloques,
        })
    return out


@router.get("/reporte-partida")
async def reporte_partida(partidas: str, desde: str = "", hasta: str = ""):
    """Sustento de valorización por partida (JSON): cabecera con las cifras +
    todos los partes de campo en orden cronológico con sus fotos (URLs
    firmadas). `partidas` = ids separados por coma; el rango de fechas es
    opcional (vacío = todo el historial).

    Las fotos purgadas (retención de disco) se devuelven marcadas para que el
    documento muestre el hueco en vez de mentir: el texto del parte queda.
    """
    pids, f_desde, f_hasta = _parse_part_args(partidas, desde, hasta)
    pool = await db()
    async with pool.acquire() as con:
        out = await _datos_reporte_partida(con, pids, f_desde, f_hasta)
    # Las fotos crudas se convierten aquí a la salida pública (URLs firmadas).
    for b in out:
        for rep in b["reportes"]:
            rep["fotos"] = [_foto_out(f) for f in rep["fotos"]]
    return {"desde": str(f_desde) if desde else None,
            "hasta": str(f_hasta) if hasta else None, "partidas": out}


@router.get("/reporte-partida.zip")
async def reporte_partida_zip(partidas: str, desde: str = "", hasta: str = "",
                              user: dict = Depends(require_role("oficina"))):
    """Sustento de valorización como ZIP: UN PDF por partida (para adjuntar a
    cada línea de la valorización). Mismos datos que `/reporte-partida` (por
    partida, dentro por día), pero las fotos se embeben directo desde el disco
    del VPS — sin URLs firmadas. El PDF se arma con fpdf2 (Python puro)."""
    import io
    import zipfile

    from core.pdf_partida import pdf_sustento_partida

    pids, f_desde, f_hasta = _parse_part_args(partidas, desde, hasta)
    pool = await db()
    async with pool.acquire() as con:
        out = await _datos_reporte_partida(con, pids, f_desde, f_hasta)
    periodo = _periodo_txt(desde, hasta)

    buf = io.BytesIO()
    usados: set = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for b in out:
            nombre = _nombre_pdf(b["partida"], usados)
            # Cada PDF (CPU + lectura de fotos del disco) fuera del event loop.
            pdf_bytes = await asyncio.to_thread(pdf_sustento_partida, b, periodo)
            z.writestr(nombre, pdf_bytes)

    fname = (f"sustento_{f_desde}_{f_hasta}.zip" if (desde or hasta)
             else "sustento_valorizacion.zip")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _nombre_pdf(p: dict, usados: set) -> str:
    """Nombre de archivo seguro y único dentro del ZIP: «CODIGO_Descripcion.pdf»."""
    import re
    base = f"{p.get('codigo') or 'partida'} {p.get('descripcion') or ''}".strip()
    base = re.sub(r"[^A-Za-z0-9._\- ]+", "", base).strip().replace(" ", "_")[:60]
    base = base or f"partida_{p.get('id')}"
    nombre = f"{base}.pdf"
    i = 2
    while nombre in usados:
        nombre = f"{base}_{i}.pdf"
        i += 1
    usados.add(nombre)
    return nombre


# ── Histograma MO + Ratios HH (espejo del Anexo 01, Fase S·S6) ──
@router.get("/histograma")
async def histograma(proyecto_id: int = 1, desde: str = "", semanas: int = 4,
                     otm: str = ""):
    """Espejo de las hojas 'Histograma MO' y 'Ratios HH' del Anexo 01 del
    ex-gerente. Solo LECTURA de datos que ya existen:
      · dias: HH del tareo y nº de trabajadores distintos por día (histograma
        de mano de obra);
      · ratios: por partida y semana, HH del tareo vs cantidad instalada
        (real del hito principal) → ratio HH/unidad, comparable contra el
        ratio presupuestado (hh_presup/metrado_presup)."""
    semanas = max(1, min(int(semanas or 4), 12))
    base = _lunes_de(parse_fecha(desde) or fecha_lima())
    fin = base + timedelta(days=semanas * 7 - 1)
    otm_f = otm.strip() or None
    pool = await db()
    async with pool.acquire() as con:
        dias = await con.fetch(
            """SELECT fecha, SUM(hh) AS hh, COUNT(DISTINCT trabajador_id) AS trabajadores
               FROM tareo_partida
               WHERE fecha BETWEEN $1 AND $2 AND hh IS NOT NULL AND hh > 0
                 AND ($3::text IS NULL OR otm_id = $3)
               GROUP BY fecha ORDER BY fecha""", base, fin, otm_f)
        hh_rows = await con.fetch(
            """SELECT partida_id,
                      (fecha - ((EXTRACT(ISODOW FROM fecha)::int) - 1)) AS lunes,
                      SUM(hh) AS hh
               FROM tareo_partida
               WHERE fecha BETWEEN $1 AND $2 AND hh IS NOT NULL AND hh > 0
                 AND partida_id IS NOT NULL
                 AND ($3::text IS NULL OR otm_id = $3)
               GROUP BY 1, 2""", base, fin, otm_f)
        cant_rows = await con.fetch(
            """SELECT ad.partida_id,
                      (ad.fecha - ((EXTRACT(ISODOW FROM ad.fecha)::int) - 1)) AS lunes,
                      SUM(ad.cantidad_dia) AS cant
               FROM ev_avances_diarios ad
               JOIN ev_partidas p ON p.id = ad.partida_id
               WHERE ad.fecha BETWEEN $1 AND $2 AND ad.cantidad_dia IS NOT NULL
                 AND ad.hito_id IS NULL
                 AND ($3::text IS NULL OR p.otm_id = $3)
               GROUP BY 1, 2""", base, fin, otm_f)
        pids = sorted({r["partida_id"] for r in hh_rows}
                      | {r["partida_id"] for r in cant_rows})
        pinfo = {r["id"]: dict(r) for r in await con.fetch(
            """SELECT id, codigo, descripcion, unidad,
                      CASE WHEN metrado_presup > 0 THEN hh_presup / metrado_presup
                      END AS ratio_presup
               FROM ev_partidas WHERE id = ANY($1)""", pids)} if pids else {}

    por_partida: dict = {}
    for r in hh_rows:
        por_partida.setdefault(r["partida_id"], {}).setdefault(
            str(r["lunes"]), {})["hh"] = float(r["hh"] or 0)
    for r in cant_rows:
        por_partida.setdefault(r["partida_id"], {}).setdefault(
            str(r["lunes"]), {})["cant"] = float(r["cant"] or 0)
    ratios = []
    for pid in pids:
        info = pinfo.get(pid) or {}
        sems = {}
        for lun, v in sorted(por_partida.get(pid, {}).items()):
            hh, cant = v.get("hh", 0.0), v.get("cant", 0.0)
            sems[lun] = {"hh": round(hh, 2), "cant": round(cant, 3),
                         "ratio": round(hh / cant, 3) if cant > 0 else None}
        rp = info.get("ratio_presup")
        ratios.append({
            "partida_id": pid, "codigo": info.get("codigo"),
            "descripcion": info.get("descripcion"), "unidad": info.get("unidad"),
            "ratio_presup": round(float(rp), 3) if rp is not None else None,
            "semanas": sems})
    return {
        "desde": str(base), "hasta": str(fin),
        "semanas": [{"lunes": str(base + timedelta(days=i * 7)),
                     "domingo": str(base + timedelta(days=i * 7 + 6))}
                    for i in range(semanas)],
        "dias": [{"fecha": str(r["fecha"]), "hh": round(float(r["hh"] or 0), 2),
                  "trabajadores": r["trabajadores"]} for r in dias],
        "ratios": ratios,
    }


# ── Almacenamiento (indicador semanal + purga manual) ────────
@router.get("/media-uso")
async def media_uso(proyecto_id: int = 1):
    """Uso de disco por semana ISO (los bytes viven en BD: no hay que escanear)."""
    pool = await db()
    rows = await pool.fetch(
        """SELECT f.semana_iso, count(*) AS n_fotos,
                  count(*) FILTER (WHERE f.purgada) AS n_purgadas,
                  COALESCE(SUM((f.bytes + f.bytes_thumb)) FILTER (WHERE NOT f.purgada), 0) AS bytes_en_disco
           FROM campo_fotos f JOIN campo_reportes r ON r.id = f.reporte_id
           WHERE r.proyecto_id = $1
           GROUP BY f.semana_iso ORDER BY f.semana_iso DESC""", proyecto_id)
    return [dict(r) for r in rows]


@router.post("/purgar")
async def purgar_semana(data: dict, user: dict = Depends(require_role("oficina"))):
    """Borra del disco las fotos de una semana (originales + thumbs) y las marca
    purgadas. El reporte (texto/fecha/autor) SE CONSERVA. Exportar el reporte
    semanal ANTES de purgar es responsabilidad del usuario (el panel lo recuerda)."""
    semana_iso = str(data.get("semana_iso") or "").strip()
    proyecto_id = int(data.get("proyecto_id") or 1)
    if not semana_iso:
        raise HTTPException(400, "semana_iso requerida (ej. 2026-W28)")
    pool = await db()
    async with pool.acquire() as con:
        fotos = await con.fetch(
            """SELECT f.id, f.ruta, f.ruta_thumb FROM campo_fotos f
               JOIN campo_reportes r ON r.id = f.reporte_id
               WHERE r.proyecto_id = $1 AND f.semana_iso = $2 AND NOT f.purgada""",
            proyecto_id, semana_iso)
        n, liberados = await _purgar_fotos(con, fotos)
    log.info("purga de media", extra={"semana": semana_iso, "fotos": n,
                                      "bytes": liberados, "por": user.get("sub")})
    return {"fotos_purgadas": n, "bytes_liberados": liberados}


async def _purgar_fotos(con, fotos) -> tuple:
    """Borra del disco (original + thumb) y marca purgada. Devuelve (n, bytes)."""
    liberados = 0
    for f in fotos:
        for ruta in (f["ruta"], f["ruta_thumb"]):
            p = media_dir() / ruta
            if p.is_file():
                liberados += p.stat().st_size
                p.unlink()
    if fotos:
        await con.execute(
            "UPDATE campo_fotos SET purgada = true WHERE id = ANY($1::int[])",
            [f["id"] for f in fotos])
    return len(fotos), liberados


async def purgar_semanas_antiguas() -> dict:
    """Purga AUTOMÁTICA por retención (decisión de Jean: conservar ~2 meses para
    que el ingeniero de costos pueda revisar; el PDF semanal es el archivo
    permanente). Purga las semanas ISO cuyo lunes es más viejo que
    MEDIA_RETENCION_SEMANAS. La corre el loop diario del lifespan."""
    from core import config
    corte = fecha_lima() - timedelta(weeks=config.MEDIA_RETENCION_SEMANAS)
    pool = await db()
    total_n, total_b = 0, 0
    async with pool.acquire() as con:
        semanas = [r["semana_iso"] for r in await con.fetch(
            "SELECT DISTINCT semana_iso FROM campo_fotos WHERE NOT purgada")]
        for semana in semanas:
            lunes = semana_iso_a_lunes(semana)
            if lunes is None or lunes >= corte:
                continue
            fotos = await con.fetch(
                "SELECT id, ruta, ruta_thumb FROM campo_fotos "
                "WHERE semana_iso = $1 AND NOT purgada", semana)
            n, b = await _purgar_fotos(con, fotos)
            total_n += n
            total_b += b
            log.info("purga automática", extra={"semana": semana, "fotos": n, "bytes": b})
    return {"fotos_purgadas": total_n, "bytes_liberados": total_b}


async def purga_automatica_loop():
    """Tarea de fondo (lifespan): purga por retención al arrancar y cada 24 h."""
    while True:
        try:
            await purgar_semanas_antiguas()
        except Exception:
            log.exception("purga automática falló (reintenta en 24h)")
        await asyncio.sleep(24 * 3600)


# ── Campo: el supervisor reporta con fotos ───────────────────
@router_campo.get("/programacion-dia")
async def programacion_dia(fecha: str = "", otm_id: str = ""):
    """Actividades programadas del día para una OTM (para reportar 'contra' ellas)."""
    f = parse_fecha(fecha) or fecha_lima()
    pool = await db()
    rows = await pool.fetch(
        """SELECT a.id, a.titulo, a.estado, a.desglose_1, a.desglose_2, a.padre_id
             FROM prog_actividades a
           WHERE $1 BETWEEN a.fecha AND COALESCE(a.fecha_fin, a.fecha)
             AND NOT ($1 = ANY(a.dias_salto))
             AND a.estado <> 'CANCELADO'
             AND (a.otm_id = $2 OR a.otm_id IS NULL)
             -- Una fila dividida no se reporta: lo que se trabaja es el frente.
             AND NOT EXISTS (SELECT 1 FROM prog_actividades h WHERE h.padre_id = a.id)
           ORDER BY a.id""", f, otm_id or None)
    return [dict(r) for r in rows]


@router_campo.get("/mis-actividades")
async def mis_actividades(supervisor_id: str, fecha: str = "",
                          user: dict = Depends(require_role())):
    """Las actividades del día asignadas a ESTE supervisor (todas sus OTMs) —
    el flujo A de la app de campo: la agenda que el planner le dejó."""
    exigir_identidad_supervisor(user, supervisor_id)
    f = parse_fecha(fecha) or fecha_lima()
    pool = await db()
    rows = await pool.fetch(
        """SELECT a.id, a.titulo, a.descripcion, a.estado, a.causa_nc, a.causa_nc_cat,
                  a.otm_id, o.descripcion AS otm_desc, o.area AS otm_area, a.responsable,
                  a.partida_id, a.hito_id,
                  ev.codigo AS partida_codigo, ev.descripcion AS partida_desc,
                  COALESCE(ev.unidad, a.und) AS und,
                  pm.cantidad AS metrado_dia,
                  h.descripcion AS hito_desc,
                  -- Qué frente es (0038): sin el área y la capa, «Capa 1» le
                  -- aparece al supervisor cinco veces y no sabe cuál trabajó.
                  a.desglose_1, a.desglose_2, a.padre_id, a.es_frente,
                  (SELECT count(*) FROM campo_reportes cr
                    WHERE cr.actividad_id = a.id AND cr.fecha = $1) AS reportes_hoy
           FROM prog_actividades a
           LEFT JOIN otms o ON o.id = a.otm_id
           LEFT JOIN ev_partidas ev ON ev.id = a.partida_id
           LEFT JOIN ev_hitos h ON h.id = a.hito_id
           LEFT JOIN prog_metrado_dia pm ON pm.actividad_id = a.id AND pm.fecha = $1
           WHERE $1 BETWEEN a.fecha AND COALESCE(a.fecha_fin, a.fecha)
             AND NOT ($1 = ANY(a.dias_salto))
             AND a.supervisor_id = $2 AND a.estado <> 'CANCELADO'
             -- La fila dividida no es trabajo de nadie: lo son sus frentes.
             AND NOT EXISTS (SELECT 1 FROM prog_actividades h2 WHERE h2.padre_id = a.id)
           ORDER BY a.id""", f, supervisor_id)
    return [dict(r) for r in rows]


@router_campo.get("/frentes")
async def frentes_otm(otm_id: str = "", user: dict = Depends(require_role())):
    """Catálogo de frentes/zonas ya usados en esa OTM — se auto-alimenta con el
    uso: el supervisor escribe uno la primera vez y después solo lo toca.
    Devuelve los más frecuentes primero (los que de verdad se usan)."""
    if not otm_id.strip():
        return []
    pool = await db()
    rows = await pool.fetch(
        """SELECT frente, COUNT(*) AS n FROM campo_reportes
           WHERE otm_id = $1 AND frente IS NOT NULL AND frente <> ''
           GROUP BY frente ORDER BY n DESC, frente LIMIT 20""", otm_id.strip())
    return [r["frente"] for r in rows]


@router_campo.get("/reporte-plantilla")
async def reporte_plantilla(actividad_id: Optional[int] = None,
                            partida_id: Optional[int] = None,
                            user: dict = Depends(require_role())):
    """Último reporte de ESA misma partida/hito, para reusarlo como plantilla:
    el supervisor solo cambia lo que cambió en vez de escribir todo de nuevo.
    Devuelve {} si es la primera vez que se reporta esa partida.

    Se puede preguntar por `actividad_id` (actividad programada) o por
    `partida_id`: una partida que el planner no programó todavía no tiene fila
    en el LookAhead cuando el supervisor abre el parte, y aun así merece la
    misma ayuda — si ya la reportó otro día, no debería reescribirlo todo."""
    if actividad_id is None and partida_id is None:
        raise HTTPException(422, "Indica actividad_id o partida_id")
    pool = await db()
    if actividad_id is None:
        # Sin actividad: cualquier hito de esa partida sirve como base.
        prev = await pool.fetchrow(
            """SELECT r.id, r.fecha::text AS fecha, r.area, r.frente, r.turno,
                      r.anotaciones, r.restricciones
               FROM campo_reportes r
               JOIN prog_actividades a ON a.id = r.actividad_id
              WHERE a.partida_id = $1
                AND (r.anotaciones IS NOT NULL OR r.area IS NOT NULL)
              ORDER BY r.fecha DESC, r.id DESC LIMIT 1""", partida_id)
        if not prev:
            return {}
        return {"fecha": prev["fecha"], "area": prev["area"], "frente": prev["frente"],
                "turno": prev["turno"],
                "anotaciones": json.loads(prev["anotaciones"]) if prev["anotaciones"] else [],
                "restricciones": json.loads(prev["restricciones"]) if prev["restricciones"] else []}
    act = await pool.fetchrow(
        "SELECT partida_id, hito_id, otm_id FROM prog_actividades WHERE id = $1", actividad_id)
    if not act:
        raise HTTPException(404, "Actividad no encontrada")
    if act["partida_id"] is None:
        return {}
    prev = await pool.fetchrow(
        """SELECT r.id, r.fecha::text AS fecha, r.area, r.frente, r.turno,
                  r.anotaciones, r.restricciones
           FROM campo_reportes r
           JOIN prog_actividades a ON a.id = r.actividad_id
          WHERE a.partida_id = $1
            AND (a.hito_id IS NOT DISTINCT FROM $2)
            AND r.actividad_id <> $3
            AND (r.anotaciones IS NOT NULL OR r.area IS NOT NULL)
          ORDER BY r.fecha DESC, r.id DESC LIMIT 1""",
        act["partida_id"], act["hito_id"], actividad_id)
    if not prev:
        return {}
    return {"fecha": prev["fecha"], "area": prev["area"], "frente": prev["frente"],
            "turno": prev["turno"],
            "anotaciones": json.loads(prev["anotaciones"]) if prev["anotaciones"] else [],
            "restricciones": json.loads(prev["restricciones"]) if prev["restricciones"] else []}


@router_campo.post("/actividades/{act_id}/no-cumplida")
async def marcar_no_cumplida(act_id: int, data: dict,
                             user: dict = Depends(require_role())):
    """El supervisor registra la CAUSA DE NO CUMPLIMIENTO de una actividad
    programada que no se ejecutó (Last Planner): categoría del catálogo CNC
    (obligatoria, para el Pareto) + detalle libre (opcional)."""
    supervisor_id = str(data.get("supervisor_id") or "").strip()
    causa = str(data.get("causa") or "").strip()
    causa_cat = _validar_cnc(data.get("causa_cat"))
    exigir_identidad_supervisor(user, supervisor_id)
    if not causa_cat and not causa:
        raise HTTPException(400, "Indica la causa de no cumplimiento")
    pool = await db()
    row = await pool.fetchrow(
        """UPDATE prog_actividades
           SET estado = 'NO_CUMPLIDA', causa_nc = $2, causa_nc_cat = $3,
               actualizado_en = now()
           WHERE id = $1 AND estado = 'PROGRAMADO' RETURNING id""",
        act_id, causa or None, causa_cat or "OTROS")
    if not row:
        raise HTTPException(409, "La actividad no existe o ya no está PROGRAMADO")
    return {"ok": True, "estado": "NO_CUMPLIDA"}


# ── Parte diario del supervisor (texto para el grupo de WhatsApp) ──
# Función PURA: la app de campo genera el mismo texto sin red (offline) y el
# panel lo ofrece para copiar. Formato acordado con Jean (2026-07-19).

CNC_TXT = {
    "MATERIALES": "Falta de materiales", "MANO_OBRA": "Falta de mano de obra",
    "EQUIPOS": "Falta de equipos", "INFORMACION": "Falta de información / planos",
    "CLIMA": "Clima", "INTERFERENCIA": "Interferencia con otra disciplina",
    "PRERREQUISITO": "Prerrequisito no terminado", "CLIENTE": "Cambio de prioridad del cliente",
    "PROGRAMACION": "Mala programación", "OTROS": "Otros",
}


def armar_texto_reporte(fecha: date, turno: str, responsable: str,
                        personal: list, bloques: list, restricciones: list) -> str:
    """Arma el parte diario.

    personal: [{cargo, n}] (cargo exacto del padrón — decisión de Jean)
    bloques:  [{area, frente, items:[str]}] — `area` la fija el proyecto y
              `frente` (0033) es la zona concreta que eligió el supervisor.
    restricciones: [{cat, detalle}]
    """
    L = [f"Fecha: {fecha.strftime('%d/%m')}",
         f"Turno: {(turno or 'DIA').upper()}",
         f"Responsable: {responsable or '—'}",
         "-" * 41]
    total = sum(int(p.get("n") or 0) for p in personal)
    L.append(f"CANTIDAD TOTAL PERSONAL: {total}")
    for p in personal:
        L.append(f"* {str(p.get('cargo') or 'SIN CARGO').title()}: {int(p.get('n') or 0):02d}")
    L.append("-" * 41)
    L.append("")
    L.append("ACTIVIDADES REALIZADAS")
    for b in bloques:
        if b.get("area"):
            L.append(f"AREA: {b['area']}")
        if b.get("frente"):
            L.append(f"FRENTE: {b['frente']}")
        for it in b.get("items", []):
            if str(it).strip():
                L.append(f"* {str(it).strip()}")
        L.append("")
    if restricciones:
        L.append("RESTRICCIONES.")
        for r in restricciones:
            det = str(r.get("detalle") or "").strip()
            cat = CNC_TXT.get(str(r.get("cat") or "").upper(), "")
            L.append(f"* {det or cat}" + (f" ({cat})" if det and cat else ""))
    return "\n".join(L).strip()


@router.get("/reporte-dia")
async def reporte_dia(fecha: str = "", supervisor_id: str = "", proyecto_id: int = 1):
    """Parte diario listo para copiar (lo mismo que ve el supervisor en su app).
    Sin supervisor_id devuelve el de todos, uno por supervisor."""
    f = parse_fecha(fecha) or fecha_lima()
    sup = (supervisor_id or "").strip()
    pool = await db()
    reps = await pool.fetch(
        """SELECT r.supervisor_id, s.nombre AS supervisor_nombre, r.turno, r.area, r.frente,
                  r.anotaciones, r.restricciones, r.descripcion, r.otm_id
           FROM campo_reportes r
           LEFT JOIN supervisores s ON s.id = r.supervisor_id
           WHERE r.fecha = $1 AND r.proyecto_id = $2
             AND ($3 = '' OR r.supervisor_id = $3)
           ORDER BY r.supervisor_id, r.id""", f, proyecto_id, sup)
    # Personal del día por supervisor y cargo (del tareo real)
    hh = await pool.fetch(
        """SELECT tp.supervisor_id, COALESCE(t.cargo,'SIN CARGO') AS cargo,
                  COUNT(DISTINCT tp.trabajador_id) AS n
           FROM tareo_partida tp
           LEFT JOIN trabajadores t ON t.id = tp.trabajador_id
           WHERE tp.fecha = $1 AND ($2 = '' OR tp.supervisor_id = $2)
           GROUP BY 1, 2 ORDER BY n DESC, cargo""", f, sup)

    out = []
    for sup in dict.fromkeys([r["supervisor_id"] for r in reps]):
        mios = [r for r in reps if r["supervisor_id"] == sup]
        bloques, rests = [], []
        for r in mios:
            items = json.loads(r["anotaciones"]) if r["anotaciones"] else (
                [ln.lstrip("•- ").strip() for ln in (r["descripcion"] or "").split("\n") if ln.strip()])
            if items:
                bloques.append({"area": r["area"] or "", "frente": r["frente"], "items": items})
            if r["restricciones"]:
                rests += json.loads(r["restricciones"])
        out.append({
            "supervisor_id": sup,
            "supervisor": mios[0]["supervisor_nombre"] or sup,
            "texto": armar_texto_reporte(
                f, mios[0]["turno"] or "DIA", mios[0]["supervisor_nombre"] or sup,
                [{"cargo": x["cargo"], "n": x["n"]} for x in hh if x["supervisor_id"] == sup],
                bloques, rests),
        })
    return {"fecha": f.isoformat(), "partes": out}


async def _actividad_para_parte(con, partida_id: int, otm_id: str, supervisor_id: str,
                                fecha: date, creado_por) -> Optional[int]:
    """La actividad a la que se cuelga el parte de una partida NO programada.

    Decisión de Jean: el trabajo ocurrió, así que entra al LookAhead. Es lo que
    dice Last Planner — lo que el planner no previó es justo lo que hay que
    medir, y el cierre semanal ya lo cuenta como NO PLANIFICADO (lo deduce de
    que la fila nació después de empezada la semana, § prog_cierre).

    Si ese supervisor ya tiene una actividad viva de esa partida ese día, se
    reusa: mandar dos partes del mismo frente no puede crear dos filas.
    """
    p = await con.fetchrow(
        "SELECT id, codigo, descripcion FROM ev_partidas WHERE id = $1 AND activo",
        partida_id)
    if not p:
        raise HTTPException(400, "La partida no existe o está desactivada")
    ya = await con.fetchval(
        """SELECT id FROM prog_actividades
            WHERE partida_id = $1 AND supervisor_id = $2 AND estado <> 'CANCELADO'
              AND $3 BETWEEN fecha AND COALESCE(fecha_fin, fecha)
              AND NOT EXISTS (SELECT 1 FROM prog_actividades h WHERE h.padre_id = prog_actividades.id)
            ORDER BY id LIMIT 1""", partida_id, supervisor_id, fecha)
    if ya:
        return ya
    titulo = (p["descripcion"] or p["codigo"] or "Trabajo no programado")[:120]
    return await con.fetchval(
        """INSERT INTO prog_actividades
             (proyecto_id, fecha, fecha_fin, otm_id, partida_id, titulo,
              supervisor_id, creado_por, modo_fecha)
           VALUES (1, $1, $1, $2, $3, $4, $5, $6, 'INICIO_FIN') RETURNING id""",
        fecha, otm_id or None, partida_id, titulo, supervisor_id, creado_por)


@router_campo.post("/reportes")
async def crear_reporte(
    proyecto_id: int = Form(1),
    fecha: str = Form(""),
    otm_id: str = Form(...),
    supervisor_id: str = Form(...),
    descripcion: str = Form(""),
    actividad_id: Optional[int] = Form(None),
    # Partida que se trabajó SIN estar programada: el parte no tiene actividad a
    # la que colgarse, así que se crea aquí (ver `_actividad_para_parte`). Va
    # como dato del formulario y no como una llamada aparte a propósito: la app
    # de campo guarda el parte en su outbox y lo manda cuando hay señal, así que
    # no puede depender de una respuesta previa del servidor.
    partida_id: Optional[int] = Form(None),
    id_local: Optional[str] = Form(None),
    area: str = Form(""),             # legado: se IGNORA (el área la fija el proyecto)
    frente: str = Form(""),           # zona concreta donde trabajó la cuadrilla
    turno: str = Form("DIA"),
    anotaciones: str = Form(""),      # JSON: ["viñeta", …]
    restricciones: str = Form(""),    # JSON: [{"cat":"MATERIALES","detalle":"…"}, …]
    fotos: List[UploadFile] = File(default=[]),
    user: dict = Depends(require_role()),
):
    exigir_identidad_supervisor(user, supervisor_id)
    f_rep = parse_fecha(fecha) or fecha_lima()
    # Reporte estructurado (0032): viñetas de lo realizado + restricciones con
    # categoría del catálogo CNC. `descripcion` se conserva por compatibilidad
    # (si no viene, se arma con las viñetas).
    notas = _lista_json(anotaciones)
    rests = [
        {"cat": _validar_cnc(x.get("cat")) or "OTROS", "detalle": str(x.get("detalle") or "").strip()}
        for x in _lista_json(restricciones) if isinstance(x, dict)
    ]
    turno = turno.strip().upper() if turno.strip().upper() in ("DIA", "NOCHE") else "DIA"
    # 0033: el ÁREA ya no la escribe el supervisor — se copia del proyecto para
    # que el parte y la matriz Área×Disciplina del EV nunca digan cosas
    # distintas. El FRENTE sí es suyo, normalizado para no duplicar variantes.
    frente = _norm_frente(frente)
    if not descripcion.strip() and notas:
        descripcion = "\n".join(f"• {n}" for n in notas)
    if not descripcion.strip() and not fotos:
        raise HTTPException(400, "El reporte necesita una descripción o al menos una foto")
    if len(fotos) > MAX_FOTOS_POR_REPORTE:
        raise HTTPException(422, f"Máximo {MAX_FOTOS_POR_REPORTE} fotos por reporte")

    # F4: idempotencia del outbox offline — si este id_local ya entró (reintento
    # tras respuesta perdida), devolver el reporte existente sin duplicar nada.
    id_local = (id_local or "").strip()[:64] or None
    if id_local:
        pool = await db()
        ya = await pool.fetchrow(
            """SELECT r.id, COUNT(f.id) AS fotos FROM campo_reportes r
               LEFT JOIN campo_fotos f ON f.reporte_id = r.id
               WHERE r.id_local = $1 GROUP BY r.id""", id_local)
        if ya:
            return {"ok": True, "id": ya["id"], "fotos": ya["fotos"], "duplicado": True}

    # Procesar/escribir las fotos ANTES de la transacción. Si CUALQUIER paso
    # posterior falla (otra foto inválida, FK en la BD…), los archivos ya
    # escritos se borran del disco — sin huérfanos (C2·F-1, Fase S).
    guardadas: list = []

    def _limpiar_huerfanas() -> None:
        for g in guardadas:
            for ruta in (g["ruta"], g["ruta_thumb"]):
                p = media_dir() / ruta
                if p.is_file():
                    p.unlink()

    try:
        for up in fotos:
            data = await up.read()
            if len(data) > MAX_FOTO_BYTES:
                raise HTTPException(413, f"{up.filename}: supera el máximo de 8 MB")
            try:
                guardadas.append(guardar_foto(f_rep, data))
            except ValueError as e:
                raise HTTPException(415, f"{up.filename}: {e}")

        pool = await db()
        async with pool.acquire() as con:
            async with con.transaction():
                try:
                    # El área se copia del proyecto (foto histórica): así el
                    # parte impreso y el análisis EV siempre coinciden, y si
                    # mañana cambia otms.area los partes viejos no se alteran.
                    area_proy = await con.fetchval(
                        "SELECT area FROM otms WHERE id = $1", otm_id.strip())
                    # Partida trabajada sin programar: su fila nace aquí, para
                    # que el parte tenga de dónde colgarse y el trabajo no
                    # planificado quede contado en el PPC.
                    if actividad_id is None and partida_id:
                        actividad_id = await _actividad_para_parte(
                            con, partida_id, otm_id.strip(), supervisor_id.strip(),
                            f_rep, user.get("sub"))
                    rid = await con.fetchval(
                        """INSERT INTO campo_reportes
                           (proyecto_id, fecha, otm_id, actividad_id, supervisor_id, descripcion,
                            id_local, area, frente, turno, anotaciones, restricciones)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id""",
                        proyecto_id, f_rep, otm_id.strip(), actividad_id,
                        supervisor_id.strip(), descripcion.strip() or None, id_local,
                        (area_proy or "").strip() or None, frente, turno,
                        json.dumps(notas) if notas else None,
                        json.dumps(rests) if rests else None)
                except asyncpg.UniqueViolationError:
                    # Carrera de reintentos simultáneos del outbox: el otro ganó.
                    raise _ReporteYaExiste()
                except _ERRORES_DATO:
                    raise HTTPException(400, "OTM, actividad o supervisor inválido: revisa los datos")
                for g in guardadas:
                    await con.execute(
                        """INSERT INTO campo_fotos
                           (reporte_id, semana_iso, ruta, ruta_thumb, bytes, bytes_thumb, ancho, alto)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                        rid, g["semana_iso"], g["ruta"], g["ruta_thumb"],
                        g["bytes"], g["bytes_thumb"], g["ancho"], g["alto"])
                # Calendario combinado: el reporte "ejecuta" la actividad programada.
                if actividad_id:
                    await con.execute(
                        "UPDATE prog_actividades SET estado='EJECUTADO', actualizado_en=now() "
                        "WHERE id = $1 AND estado = 'PROGRAMADO'", actividad_id)
    except _ReporteYaExiste:
        _limpiar_huerfanas()
        pool = await db()
        ya = await pool.fetchrow(
            """SELECT r.id, COUNT(f.id) AS fotos FROM campo_reportes r
               LEFT JOIN campo_fotos f ON f.reporte_id = r.id
               WHERE r.id_local = $1 GROUP BY r.id""", id_local)
        if ya:
            return {"ok": True, "id": ya["id"], "fotos": ya["fotos"], "duplicado": True}
        raise HTTPException(409, "Conflicto al registrar el reporte — reintenta")
    except BaseException:
        _limpiar_huerfanas()
        raise
    return {"ok": True, "id": rid, "fotos": len(guardadas)}
