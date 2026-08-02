# ============================================================
# routers/fiabilidad.py — libro mayor de fiabilidad de compromisos
#
# GET/POST/PUT /ev/responsables          catálogo de áreas responsables
# GET  /ev/fiabilidad/restricciones      latencia y reincidencia por tipo/responsable
# GET  /ev/fiabilidad/pendientes         bandeja de lo que sigue restringido
# POST /ev/fiabilidad/liberar            liberación en lote con fecha real
#
# QUÉ MIDE Y QUÉ NO
# `liberada_el - fecha_requerida` es la latencia de liberación: días de retraso
# (o de adelanto, si es negativa) del responsable frente a la fecha que el
# planner pidió. Se calcula SOLO sobre restricciones liberadas que tengan las
# dos fechas; el resto se cuenta aparte en vez de desaparecer del denominador.
#
# POR QUÉ NO SE DEVUELVE UNA PROBABILIDAD
# Un «22 % de cumplir» calculado sobre tres observaciones es falsa precisión: en
# cuanto falla dos veces, el planner deja de mirar el indicador y se pierde algo
# que sí servía. Aquí se devuelve la evidencia cruda —mediana, p75, n— y una
# banda cualitativa. El `n` viaja SIEMPRE al lado del número: es lo que permite
# a quien lee saber si puede apoyarse en él.
#
# `_resumen` es pura (testeable sin BD).
# ============================================================
import unicodedata
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db
from core.log import get_logger
from core.tiempo import fecha_lima, parse_fecha
# La traza append-only vive con el resto de restricciones; se importa en vez de
# duplicarla para que exista UN solo sitio que decida qué se apunta al liberar.
# No hay ciclo: `programacion` no conoce este módulo.
from routers.programacion import _evento_restriccion

log = get_logger("api")

router = APIRouter(prefix="/ev", tags=["fiabilidad"])

TIPOS_RESP = ("INTERNA", "CLIENTE", "PROVEEDOR", "SUBCONTRATA")

# Tope del lote de liberación. No es una defensa contra el planner —es contra un
# cliente que mande la lista entera sin querer—: una revisión semanal real no
# pasa de unas decenas de restricciones.
MAX_LOTE = 200

# Mínimo de observaciones para que una mediana se presente como referencia y no
# como anécdota. Por debajo se devuelve igual, pero marcada `suficiente=false`:
# ocultarla dejaría al planner sin nada, y darla sin avisar sería peor.
N_MINIMO = 5


def _norm(s: Optional[str]) -> str:
    """Nombre canónico del área: MAYÚSCULAS, sin tildes, sin espacios de sobra.

    Las tildes se quitan porque si no «LOGÍSTICA» y «LOGISTICA» son dos áreas
    distintas para el UNIQUE de la base — el mismo duplicado que este catálogo
    existe para impedir. El nombre es un identificador de área, no prosa, así
    que se guarda ya normalizado y no hay dos verdades que mantener en sincronía
    (mismo criterio que `core.personal.norm_nombre` para el padrón)."""
    limpio = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(limpio.upper().split())


def _percentil(valores: list, p: float) -> Optional[float]:
    """Percentil por interpolación lineal. `valores` debe venir ordenado."""
    if not valores:
        return None
    if len(valores) == 1:
        return float(valores[0])
    k = (len(valores) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(valores) - 1)
    return round(float(valores[lo]) + (float(valores[hi]) - float(valores[lo])) * (k - lo), 1)


def _celda(filas: list, hoy: date) -> dict:
    """Métricas de un grupo de restricciones (un tipo, un responsable o el cruce)."""
    lat = sorted(f["latencia"] for f in filas if f["latencia"] is not None)
    derivadas = sum(1 for f in filas if f["latencia"] is not None and f["derivada"])
    liberadas = [f for f in filas if f["liberada"]]
    vencidas = [f for f in filas
                if not f["liberada"] and f["fecha_requerida"] and f["fecha_requerida"] < hoy]
    a_tiempo = sum(1 for v in lat if v <= 0)
    return {
        "n": len(filas),
        "n_liberadas": len(liberadas),
        "n_pendientes": len(filas) - len(liberadas),
        "n_vencidas": len(vencidas),
        "n_medidas": len(lat),
        "n_derivadas": derivadas,
        "mediana_dias": _percentil(lat, 0.5),
        "p75_dias": _percentil(lat, 0.75),
        "peor_dias": lat[-1] if lat else None,
        "pct_a_tiempo": round(100 * a_tiempo / len(lat), 1) if lat else None,
        "suficiente": len(lat) >= N_MINIMO,
    }


def _resumen(filas: list, hoy: date) -> dict:
    """filas [{tipo, responsable_id, responsable, liberada, fecha_requerida,
    latencia, derivada}] → libro mayor por tipo, por responsable y el cruce."""
    def agrupar(clave):
        g: dict = {}
        for f in filas:
            g.setdefault(clave(f), []).append(f)
        return g

    por_tipo = [{"tipo": k, **_celda(v, hoy)}
                for k, v in sorted(agrupar(lambda f: f["tipo"]).items())]

    por_resp = [{"responsable_id": k[0], "responsable": k[1], **_celda(v, hoy)}
                for k, v in sorted(agrupar(lambda f: (f["responsable_id"], f["responsable"]))
                                   .items(), key=lambda x: str(x[0][1]))]

    # El cruce solo se ofrece donde hay materia: con 8 tipos por N responsables,
    # la mayoría de las celdas tendrían n=1 y una mediana de una observación no
    # es una mediana. Se devuelven ordenadas por reincidencia, que es lo que
    # sirve desde la primera semana aunque no haya distribución.
    cruce = [{"tipo": k[0], "responsable_id": k[1], "responsable": k[2], **_celda(v, hoy)}
             for k, v in agrupar(lambda f: (f["tipo"], f["responsable_id"], f["responsable"])).items()]
    cruce.sort(key=lambda c: (-c["n"], c["tipo"], str(c["responsable"])))

    return {
        "total": _celda(filas, hoy),
        "por_tipo": por_tipo,
        "por_responsable": por_resp,
        "reincidencia": [c for c in cruce if c["n"] > 1],
        "n_minimo": N_MINIMO,
    }


# ── Catálogo de responsables ──────────────────────────────────
@router.get("/responsables")
async def listar_responsables(proyecto_id: int = 1, incluir_inactivos: bool = False):
    pool = await db()
    rows = await pool.fetch(
        f"""SELECT r.*, s.nombre AS supervisor_nombre,
                   (SELECT count(*) FROM prog_restricciones x WHERE x.responsable_id = r.id) AS n_restricciones
              FROM prog_responsables r
              LEFT JOIN supervisores s ON s.id = r.supervisor_id
             WHERE r.proyecto_id = $1 {'' if incluir_inactivos else 'AND r.activo'}
             ORDER BY r.nombre""",
        proyecto_id)
    return [dict(r) for r in rows]


@router.post("/responsables")
async def crear_responsable(data: dict, _u: dict = Depends(require_role("oficina"))):
    nombre = _norm(data.get("nombre"))
    if not nombre:
        raise HTTPException(400, "nombre requerido")
    tipo = _norm(data.get("tipo")) or "INTERNA"
    if tipo not in TIPOS_RESP:
        raise HTTPException(422, f"tipo inválido (usa {'/'.join(TIPOS_RESP)})")
    pool = await db()
    ya = await pool.fetchrow(
        "SELECT * FROM prog_responsables WHERE proyecto_id = $1 AND nombre = $2",
        int(data.get("proyecto_id") or 1), nombre)
    if ya:
        # Reactivar en vez de rechazar: el planner que vuelve a escribir un área
        # dada de baja quiere usarla, no un 409 que no sabe resolver.
        row = await pool.fetchrow(
            "UPDATE prog_responsables SET activo = true WHERE id = $1 RETURNING *", ya["id"])
        return dict(row)
    row = await pool.fetchrow(
        """INSERT INTO prog_responsables (proyecto_id, nombre, tipo, supervisor_id)
           VALUES ($1,$2,$3,$4) RETURNING *""",
        int(data.get("proyecto_id") or 1), nombre, tipo, data.get("supervisor_id") or None)
    return dict(row)


@router.put("/responsables/{resp_id}")
async def editar_responsable(resp_id: int, data: dict,
                             _u: dict = Depends(require_role("oficina"))):
    campos, valores = [], []
    if "nombre" in data:
        n = _norm(data["nombre"])
        if not n:
            raise HTTPException(400, "nombre requerido")
        campos.append(f"nombre = ${len(valores) + 2}"); valores.append(n)
    if "tipo" in data:
        t = _norm(data["tipo"])
        if t not in TIPOS_RESP:
            raise HTTPException(422, f"tipo inválido (usa {'/'.join(TIPOS_RESP)})")
        campos.append(f"tipo = ${len(valores) + 2}"); valores.append(t)
    if "supervisor_id" in data:
        campos.append(f"supervisor_id = ${len(valores) + 2}")
        valores.append(data["supervisor_id"] or None)
    if "activo" in data:
        campos.append(f"activo = ${len(valores) + 2}"); valores.append(bool(data["activo"]))
    if not campos:
        raise HTTPException(400, "Nada que actualizar")
    pool = await db()
    try:
        row = await pool.fetchrow(
            f"UPDATE prog_responsables SET {', '.join(campos)} WHERE id = $1 RETURNING *",
            resp_id, *valores)
    except Exception:
        log.exception("responsable duplicado o dato inválido", extra={"resp_id": resp_id})
        raise HTTPException(409, "Ya existe un responsable con ese nombre en el proyecto")
    if not row:
        raise HTTPException(404, "Responsable no encontrado")
    return dict(row)


# NO hay DELETE: un responsable con restricciones históricas es el eje de la
# medición. Se desactiva con PUT {activo:false} y deja de ofrecerse al elegir.


# ── Libro mayor ───────────────────────────────────────────────
@router.get("/fiabilidad/restricciones")
async def fiabilidad_restricciones(desde: str = "", hasta: str = "", proyecto_id: int = 1):
    """Latencia de liberación y reincidencia, por tipo y por responsable."""
    hoy = fecha_lima()
    d, h = parse_fecha(desde), parse_fecha(hasta)
    conds = ["a.proyecto_id = $1"]
    args: list = [proyecto_id]
    if d:
        args.append(d); conds.append(f"r.creado_en >= ${len(args)}")
    if h:
        args.append(h); conds.append(f"r.creado_en < ${len(args)}::date + 1")

    pool = await db()
    rows = await pool.fetch(
        f"""SELECT r.id, r.tipo, r.liberada, r.fecha_requerida, r.liberada_el,
                   r.liberada_en, r.responsable_id, r.descripcion, r.actividad_id,
                   COALESCE(p.nombre, '(sin responsable)') AS responsable,
                   act.titulo AS actividad
              FROM prog_restricciones r
              JOIN prog_actividades a  ON a.id = r.actividad_id
              LEFT JOIN prog_actividades act ON act.id = r.actividad_id
              LEFT JOIN prog_responsables p  ON p.id = r.responsable_id
             WHERE {' AND '.join(conds)}""",
        *args)

    filas = []
    for r in rows:
        # La fecha real manda; si no la hay se cae al sello de captura, y se
        # marca `derivada` para que la métrica pueda declarar cuánto de lo que
        # muestra viene de un dato de segunda.
        real = r["liberada_el"]
        derivada = False
        if r["liberada"] and not real and r["liberada_en"]:
            real, derivada = r["liberada_en"].date(), True
        lat = (real - r["fecha_requerida"]).days if (real and r["fecha_requerida"]) else None
        filas.append({
            "id": r["id"], "tipo": r["tipo"], "liberada": r["liberada"],
            "fecha_requerida": r["fecha_requerida"], "latencia": lat, "derivada": derivada,
            "responsable_id": r["responsable_id"], "responsable": r["responsable"],
            "descripcion": r["descripcion"], "actividad": r["actividad"],
            "actividad_id": r["actividad_id"],
        })

    out = _resumen(filas, hoy)
    # Las que están pendientes y ya pasadas de fecha: es lo accionable de hoy,
    # no una estadística del pasado.
    out["vencidas"] = sorted(
        [{"id": f["id"], "tipo": f["tipo"], "responsable": f["responsable"],
          "descripcion": f["descripcion"], "actividad": f["actividad"],
          "actividad_id": f["actividad_id"],
          "fecha_requerida": str(f["fecha_requerida"]),
          "dias": (hoy - f["fecha_requerida"]).days}
         for f in filas
         if not f["liberada"] and f["fecha_requerida"] and f["fecha_requerida"] < hoy],
        key=lambda x: -x["dias"])
    out["hoy"] = str(hoy)
    return out


# ── Bandeja de liberación ─────────────────────────────────────
# El libro mayor de arriba mide el pasado. Esto es la otra mitad del encargo de
# Jean (2026-08-02): el viernes revisa lo que sigue restringido y declara, ahí
# mismo, qué día se liberó de verdad cada cosa. Sin esto la única vía era abrir
# actividad por actividad en el Lookahead, y la fecha real acababa siendo
# siempre «el día en que el planner tuvo tiempo de limpiar» — justo el sesgo que
# `liberada_el` existe para evitar.
@router.get("/fiabilidad/pendientes")
async def fiabilidad_pendientes(proyecto_id: int = 1):
    """Restricciones sin liberar, la más urgente primero."""
    hoy = fecha_lima()
    pool = await db()
    rows = await pool.fetch(
        """SELECT r.id, r.descripcion, r.tipo, r.fecha_requerida, r.responsable_id,
                  COALESCE(p.nombre, '(sin responsable)') AS responsable,
                  a.id AS actividad_id, a.titulo AS actividad, a.fecha AS actividad_fecha,
                  a.estado, a.otm_id
             FROM prog_restricciones r
             JOIN prog_actividades a       ON a.id = r.actividad_id
             LEFT JOIN prog_responsables p ON p.id = r.responsable_id
            WHERE a.proyecto_id = $1 AND NOT r.liberada
            ORDER BY r.fecha_requerida NULLS LAST, r.id""",
        proyecto_id)
    return {
        "hoy": str(hoy),
        # `dias` positivo = vencida hace tantos días; negativo = aún queda plazo.
        # Se manda calculado para que el panel no repita la aritmética de fechas
        # (que en JS, con zonas horarias de por medio, se equivoca sola).
        "pendientes": [
            {**dict(r),
             "fecha_requerida": str(r["fecha_requerida"]) if r["fecha_requerida"] else None,
             "actividad_fecha": str(r["actividad_fecha"]) if r["actividad_fecha"] else None,
             "dias": (hoy - r["fecha_requerida"]).days if r["fecha_requerida"] else None}
            for r in rows],
    }


def _validar_lote(items, hoy: date) -> list:
    """[{id, liberada_el}] → [(id, fecha)] validado. Pura, testeable sin BD.

    Rechaza fechas futuras: nadie puede haber liberado algo mañana, y un 2027
    tecleado por error no se nota en la lista pero envenena la mediana del
    responsable para siempre. Mejor un 422 que un dato que nadie audita."""
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Nada que liberar")
    if len(items) > MAX_LOTE:
        raise HTTPException(422, f"Máximo {MAX_LOTE} restricciones por lote")
    porid: dict = {}
    for it in items:
        if not isinstance(it, dict):
            raise HTTPException(422, "Cada elemento debe ser {id, liberada_el}")
        try:
            rid = int(it.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(422, "id de restricción inválido")
        f = parse_fecha(it.get("liberada_el")) or hoy
        if f > hoy:
            raise HTTPException(422, f"La fecha de liberación no puede ser futura ({f})")
        porid[rid] = f
    return sorted(porid.items())


@router.post("/fiabilidad/liberar")
async def liberar_lote(data: dict, user: dict = Depends(require_role("oficina"))):
    """Marca liberadas varias restricciones, cada una con SU fecha real.

    Una sola transacción: la revisión semanal es un acto, no N actos sueltos que
    puedan quedar a medias si se cae la red a mitad de la lista."""
    hoy = fecha_lima()
    pares = _validar_lote(data.get("items"), hoy)
    notas = str(data.get("notas") or "").strip()
    actor = user.get("sub", "?")

    liberadas, omitidas = [], []
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for rid, fecha in pares:
                # `AND NOT liberada` hace la operación idempotente: si otro ya la
                # liberó, no se pisa su fecha ni se apunta un evento duplicado.
                row = await con.fetchrow(
                    """UPDATE prog_restricciones
                          SET liberada = true, liberada_el = $2, liberada_en = now()
                        WHERE id = $1 AND NOT liberada RETURNING *""",
                    rid, fecha)
                if not row:
                    omitidas.append(rid)
                    continue
                await _evento_restriccion(con, "liberar", row, actor, notas)
                liberadas.append(rid)
    return {"liberadas": liberadas, "omitidas": omitidas, "hoy": str(hoy)}
