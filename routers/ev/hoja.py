# ============================================================
# routers/ev/hoja.py — hoja semanal de HH: OTM → partida → personal
#
# GET    /ev/hoja-semanal?lunes&otm      la semana completa, editable
# GET    /ev/hoja-semanal/persona        el día de una persona CRUZANDO proyectos
# POST   /ev/tareo-linea                 agregar HH que faltan
# PATCH  /ev/tareo-linea/{id}            corregir HH o cambiar de partida
# DELETE /ev/tareo-linea/{id}            anular (deja la línea en 0 CON la marca)
#
# Por qué esta vista y no editar la matriz histórica: la celda de la matriz es
# una SUMA (`SUM(hh) GROUP BY trabajador, fecha`), así que escribir encima es
# ambiguo — no hay forma de saber a qué partida quitarle las horas. Aquí la fila
# es la línea real del tareo, y por eso se puede editar sin adivinar.
#
# `_armar_hoja` es pura (testeable sin BD).
# ============================================================
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db
from core.log import get_logger
from core.tiempo import parse_fecha, semana_de
from routers.jornada import resolver_jornada

log = get_logger("ev")

router = APIRouter()
router_campo = APIRouter()

# La misma tolerancia que usa la app de campo para avisar de HH desbalanceadas:
# si allá 9.4 contra 9.5 no es un problema, aquí tampoco.
TOL = 0.15
HH_MAX = 24.0

_SQL_SEMANA = """
    SELECT tp.id, tp.trabajador_id, tp.partida_id, tp.otm_id, tp.fecha, tp.hh,
           tp.supervisor_id, tp.editado_por, tp.editado_en, tp.motivo_edicion,
           COALESCE(t.nombre, 'Trab. ' || tp.trabajador_id) AS trab_nombre,
           COALESCE(t.cargo, '')                           AS cargo,
           ev.codigo, COALESCE(ev.descripcion, '')         AS partida_desc,
           COALESCE(o.descripcion, '')                     AS otm_desc,
           s.nombre                                        AS sup_nombre
      FROM tareo_partida tp
      LEFT JOIN trabajadores t  ON t.id  = tp.trabajador_id
      LEFT JOIN ev_partidas  ev ON ev.id = tp.partida_id
      LEFT JOIN otms         o  ON o.id  = tp.otm_id
      LEFT JOIN supervisores s  ON s.id  = tp.supervisor_id
     WHERE tp.fecha BETWEEN $1 AND $2
     ORDER BY tp.otm_id, ev.codigo, trab_nombre, tp.fecha, tp.id
"""


def _armar_hoja(rows, fechas, jornadas, otm_filtro: str = ""):
    """rows (semana COMPLETA) → (proyectos, totales_persona_dia).

    Los totales por persona/día se calculan con TODAS las filas, incluso cuando
    se pide un solo proyecto: el exceso de una persona casi siempre nace de
    estar en dos proyectos a la vez, así que calcularlo sobre la vista filtrada
    daría justo el falso negativo que esta hoja existe para cazar.
    """
    # ── totales por persona y día (sin filtrar por OTM) ──
    tot_pd: dict = {}
    for r in rows:
        k = (r["trabajador_id"], str(r["fecha"]))
        d = tot_pd.setdefault(k, {
            "trab_id": r["trabajador_id"], "nombre": r["trab_nombre"],
            "fecha": str(r["fecha"]), "hh": 0.0, "n_otms": set(), "n_lineas": 0,
        })
        d["hh"] += float(r["hh"] or 0)
        d["n_otms"].add(r["otm_id"])
        d["n_lineas"] += 1

    for d in tot_pd.values():
        jor = jornadas.get(d["fecha"], 0.0)
        diff = d["hh"] - jor
        d["jornada"] = jor
        d["diff"]    = round(diff, 2)
        d["hh"]      = round(d["hh"], 2)
        d["estado"]  = "ok" if abs(diff) <= TOL else ("bajo" if diff < 0 else "extra")
        d["n_otms"]  = len(d["n_otms"])

    # ── árbol OTM → partida → persona ──
    proyectos: dict = {}
    for r in rows:
        if otm_filtro and r["otm_id"] != otm_filtro:
            continue
        f  = str(r["fecha"])
        hh = float(r["hh"] or 0)

        p = proyectos.setdefault(r["otm_id"], {
            "otm_id": r["otm_id"], "descripcion": r["otm_desc"],
            "partidas": {}, "celdas": {}, "total": 0.0, "personal": {},
        })
        pa = p["partidas"].setdefault(r["partida_id"], {
            "partida_id": r["partida_id"], "codigo": r["codigo"] or "—",
            "descripcion": r["partida_desc"], "personas": {}, "celdas": {}, "total": 0.0,
        })
        pe = pa["personas"].setdefault(r["trabajador_id"], {
            "trab_id": r["trabajador_id"], "nombre": r["trab_nombre"],
            "cargo": r["cargo"], "celdas": {}, "total": 0.0,
        })

        celda = pe["celdas"].setdefault(f, {"hh": 0.0, "editado": False, "lineas": []})
        celda["hh"] += hh
        celda["lineas"].append({
            "id": r["id"], "hh": hh,
            "supervisor_id": r["supervisor_id"],
            "supervisor": r["sup_nombre"] or r["supervisor_id"],
            "editado_por": r["editado_por"],
            "motivo": r["motivo_edicion"],
        })
        if r["editado_por"]:
            celda["editado"] = True

        # Los subtotales de proyecto y partida son números por día; la celda de
        # la persona es el objeto de arriba (lleva las líneas para editarlas).
        for nivel in (p, pa):
            nivel["celdas"][f] = round(nivel["celdas"].get(f, 0.0) + hh, 2)
        for nivel in (p, pa, pe):
            nivel["total"] = round(nivel["total"] + hh, 2)

        per = p["personal"].setdefault(r["trabajador_id"], {
            "trab_id": r["trabajador_id"], "nombre": r["trab_nombre"],
            "cargo": r["cargo"], "total": 0.0,
        })
        per["total"] = round(per["total"] + hh, 2)

    # dict → listas ordenadas, y celdas de persona redondeadas
    salida = []
    for p in sorted(proyectos.values(), key=lambda x: x["otm_id"]):
        partidas = []
        for pa in sorted(p["partidas"].values(), key=lambda x: x["codigo"]):
            personas = []
            for pe in sorted(pa["personas"].values(), key=lambda x: x["nombre"]):
                for c in pe["celdas"].values():
                    c["hh"] = round(c["hh"], 2)
                    c["n"]  = len(c["lineas"])
                personas.append(pe)
            pa["personas"] = personas
            partidas.append(pa)
        p["partidas"] = partidas
        p["personal"] = sorted(p["personal"].values(), key=lambda x: x["nombre"])
        salida.append(p)

    return salida, tot_pd


@router.get("/hoja-semanal")
async def hoja_semanal(lunes: str = "", otm: str = ""):
    """La semana de trabajo agrupada OTM → partida → personal, lista para editar."""
    ini = parse_fecha(lunes)
    if not ini:
        raise HTTPException(400, "lunes inválido (YYYY-MM-DD)")
    ini = ini - timedelta(days=ini.weekday())      # alinear a lunes siempre
    fin = ini + timedelta(days=6)
    fechas = [str(ini + timedelta(days=i)) for i in range(7)]

    # Jornada GLOBAL del día (sin OTM): el turno de una persona es uno solo. Si
    # se tomara la jornada de cada proyecto, quien reparte su día entre dos OTMs
    # con jornadas distintas saldría en rojo sin que nadie se haya equivocado.
    jornadas = {f: await resolver_jornada(date.fromisoformat(f)) for f in fechas}

    pool = await db()
    async with pool.acquire() as con:
        rows = [dict(r) for r in await con.fetch(_SQL_SEMANA, ini, fin)]
        otms = [dict(r) for r in await con.fetch(
            """SELECT DISTINCT tp.otm_id AS id, COALESCE(o.descripcion,'') AS descripcion
                 FROM tareo_partida tp LEFT JOIN otms o ON o.id = tp.otm_id
                WHERE tp.fecha BETWEEN $1 AND $2 ORDER BY 1""", ini, fin)]

    proyectos, tot_pd = _armar_hoja(rows, fechas, jornadas, otm)
    avisos = [d for d in tot_pd.values() if d["estado"] == "extra"]
    avisos.sort(key=lambda d: (-d["diff"], d["nombre"]))

    return {
        "lunes": str(ini), "domingo": str(fin), "fechas": fechas,
        "jornadas": jornadas, "otms": otms, "otm": otm,
        "proyectos": proyectos,
        "totales_persona_dia": {f"{k[0]}|{k[1]}": v for k, v in tot_pd.items()},
        "avisos": avisos,
        "total_hh": round(sum(p["total"] for p in proyectos), 2),
    }


@router.get("/hoja-semanal/persona")
async def hoja_persona(trab_id: str = "", fecha: str = ""):
    """El día de una persona con TODAS sus líneas, de todos los proyectos.

    Es el desglose que se abre al pinchar una celda con aviso: el exceso casi
    nunca está dentro del proyecto que se está mirando."""
    f = parse_fecha(fecha)
    if not f or not trab_id:
        raise HTTPException(400, "trab_id y fecha son requeridos")

    jornada = await resolver_jornada(f)
    pool = await db()
    async with pool.acquire() as con:
        rows = [dict(r) for r in await con.fetch(
            _SQL_SEMANA.replace("tp.fecha BETWEEN $1 AND $2",
                                "tp.fecha = $1 AND tp.trabajador_id = $2"),
            f, str(trab_id).zfill(3))]

    total = round(sum(float(r["hh"] or 0) for r in rows), 2)
    return {
        "trab_id": str(trab_id).zfill(3),
        "nombre": rows[0]["trab_nombre"] if rows else str(trab_id).zfill(3),
        "fecha": str(f), "jornada": jornada, "registrado": total,
        "diff": round(total - jornada, 2),
        "estado": "ok" if abs(total - jornada) <= TOL else ("bajo" if total < jornada else "extra"),
        "lineas": [{
            "id": r["id"], "otm_id": r["otm_id"], "otm_desc": r["otm_desc"],
            "partida_id": r["partida_id"], "codigo": r["codigo"],
            "descripcion": r["partida_desc"], "hh": float(r["hh"] or 0),
            "supervisor_id": r["supervisor_id"],
            "supervisor": r["sup_nombre"] or r["supervisor_id"],
            "editado_por": r["editado_por"],
            "editado_en": r["editado_en"], "motivo": r["motivo_edicion"],
        } for r in rows],
    }


# ── Edición (oficina) ─────────────────────────────────────────
def _valida_hh(v) -> float:
    try:
        hh = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "hh debe ser un número")
    if hh < 0 or hh > HH_MAX:
        raise HTTPException(400, f"hh fuera de rango (0 a {HH_MAX})")
    return round(hh, 4)


async def _partida(con, partida_id) -> dict:
    p = await con.fetchrow(
        "SELECT id, otm_id, codigo FROM ev_partidas WHERE id = $1 AND activo = true",
        partida_id)
    if not p:
        raise HTTPException(404, "La partida no existe o está inactiva")
    return dict(p)


async def _traza(con, accion: str, usuario: str, fila: dict, motivo: str = "",
                 hh_antes=None, partida_antes=None) -> None:
    await con.execute(
        """INSERT INTO tareo_ediciones
             (tareo_id, accion, trabajador_id, fecha, otm_id,
              partida_id_antes, partida_id, hh_antes, hh, motivo, usuario)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
        fila.get("id"), accion, fila["trabajador_id"], fila["fecha"], fila.get("otm_id"),
        partida_antes, fila.get("partida_id"), hh_antes, fila.get("hh"),
        (motivo or "").strip() or None, usuario,
    )


@router.post("/tareo-linea")
async def crear_linea(data: dict, user: dict = Depends(require_role("oficina"))):
    """Agrega HH que el tareo no registró (el sub-registro no tenía arreglo)."""
    crudo = str(data.get("trabajador_id", "")).strip()
    if not crudo:
        raise HTTPException(400, "trabajador_id requerido")
    trab_id = crudo.zfill(3)
    f       = parse_fecha(str(data.get("fecha", "")))
    hh      = _valida_hh(data.get("hh"))
    if not f:
        raise HTTPException(400, "fecha inválida")
    if hh <= 0:
        raise HTTPException(400, "hh debe ser mayor que 0")

    pool = await db()
    async with pool.acquire() as con:
        if not await con.fetchval("SELECT 1 FROM trabajadores WHERE id = $1", trab_id):
            raise HTTPException(404, "El trabajador no existe en el padrón")
        par = await _partida(con, data.get("partida_id"))

        from routers.ev._datos import _fecha_base
        base = await _fecha_base(con)
        semana = semana_de(f, base) if base else 1

        async with con.transaction():
            fila_id = await con.fetchval(
                """INSERT INTO tareo_partida
                     (trabajador_id, partida_id, otm_id, fecha, semana, hora_registro,
                      hh, supervisor_id, fuente, editado_por, editado_en, motivo_edicion)
                   VALUES ($1,$2,$3,$4,$5,NOW(),$6,$7,'oficina',$8,NOW(),$9)
                RETURNING id""",
                trab_id, par["id"], par["otm_id"], f, semana, hh,
                (data.get("supervisor_id") or None), user.get("sub", "?"),
                (data.get("motivo") or "").strip() or None)
            await _traza(con, "crear", user.get("sub", "?"), {
                "id": fila_id, "trabajador_id": trab_id, "fecha": f,
                "otm_id": par["otm_id"], "partida_id": par["id"], "hh": hh,
            }, data.get("motivo", ""))
    return {"ok": True, "id": fila_id}


@router.patch("/tareo-linea/{linea_id}")
async def editar_linea(linea_id: int, data: dict,
                       user: dict = Depends(require_role("oficina"))):
    """Corrige las HH de una línea y/o la mueve a otra partida."""
    pool = await db()
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT id, trabajador_id, partida_id, otm_id, fecha, hh FROM tareo_partida WHERE id = $1",
            linea_id)
        if not fila:
            raise HTTPException(404, "La línea de tareo no existe")

        hh = _valida_hh(data["hh"]) if "hh" in data else float(fila["hh"] or 0)
        partida_id, otm_id = fila["partida_id"], fila["otm_id"]
        if data.get("partida_id") and int(data["partida_id"]) != fila["partida_id"]:
            par = await _partida(con, int(data["partida_id"]))
            partida_id, otm_id = par["id"], par["otm_id"]

        async with con.transaction():
            await con.execute(
                """UPDATE tareo_partida
                      SET hh = $2, partida_id = $3, otm_id = $4,
                          editado_por = $5, editado_en = NOW(), motivo_edicion = $6
                    WHERE id = $1""",
                linea_id, hh, partida_id, otm_id, user.get("sub", "?"),
                (data.get("motivo") or "").strip() or None)
            await _traza(con, "editar", user.get("sub", "?"), {
                "id": linea_id, "trabajador_id": fila["trabajador_id"],
                "fecha": fila["fecha"], "otm_id": otm_id,
                "partida_id": partida_id, "hh": hh,
            }, data.get("motivo", ""), hh_antes=fila["hh"], partida_antes=fila["partida_id"])
    return {"ok": True, "id": linea_id, "hh": hh, "partida_id": partida_id}


@router.delete("/tareo-linea/{linea_id}")
async def anular_linea(linea_id: int, motivo: str = "",
                       user: dict = Depends(require_role("oficina"))):
    """Anula una línea: la deja en 0 CON la marca de oficina.

    No se borra físicamente a propósito — ver la migración 0043: sin la marca,
    el siguiente reenvío del supervisor la recrearía tal cual y desharía la
    corrección sin que nadie se entere."""
    pool = await db()
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT id, trabajador_id, partida_id, otm_id, fecha, hh FROM tareo_partida WHERE id = $1",
            linea_id)
        if not fila:
            raise HTTPException(404, "La línea de tareo no existe")
        async with con.transaction():
            await con.execute(
                """UPDATE tareo_partida
                      SET hh = 0, editado_por = $2, editado_en = NOW(), motivo_edicion = $3
                    WHERE id = $1""",
                linea_id, user.get("sub", "?"), (motivo or "").strip() or None)
            await _traza(con, "anular", user.get("sub", "?"), {
                "id": linea_id, "trabajador_id": fila["trabajador_id"],
                "fecha": fila["fecha"], "otm_id": fila["otm_id"],
                "partida_id": fila["partida_id"], "hh": 0,
            }, motivo, hh_antes=fila["hh"], partida_antes=fila["partida_id"])
    return {"ok": True, "id": linea_id}


@router.get("/tareo-ediciones")
async def listar_ediciones(desde: str = "", hasta: str = "", trab_id: str = "",
                           limite: int = 200):
    """Traza de correcciones: quién, cuándo, qué valor había antes."""
    conds, args = ["1=1"], []
    if desde:
        args.append(parse_fecha(desde)); conds.append(f"e.fecha >= ${len(args)}")
    if hasta:
        args.append(parse_fecha(hasta)); conds.append(f"e.fecha <= ${len(args)}")
    if trab_id:
        args.append(str(trab_id).zfill(3)); conds.append(f"e.trabajador_id = ${len(args)}")
    args.append(max(1, min(int(limite), 1000)))

    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            f"""SELECT e.*, COALESCE(t.nombre, e.trabajador_id) AS trab_nombre,
                       ev.codigo AS partida_codigo
                  FROM tareo_ediciones e
                  LEFT JOIN trabajadores t  ON t.id  = e.trabajador_id
                  LEFT JOIN ev_partidas  ev ON ev.id = e.partida_id
                 WHERE {' AND '.join(conds)}
                 ORDER BY e.creado_en DESC
                 LIMIT ${len(args)}""", *args)
    return [dict(r) for r in rows]


async def lineas_protegidas(con, supervisor_id: str, otm_id: str, fecha) -> set:
    """Pares (trabajador_id, partida_id) corregidos en oficina para ese día/OTM.

    Lo usa el reenvío del supervisor: la corrección de oficina gana sobre todo
    (decisión de Jean, 2026-08-02), así que esas líneas ni se borran ni se
    reescriben con lo que traiga la app."""
    rows = await con.fetch(
        """SELECT trabajador_id, partida_id FROM tareo_partida
            WHERE otm_id = $1 AND fecha = $2 AND editado_por IS NOT NULL""",
        otm_id, fecha)
    # `supervisor_id` no entra en la consulta a propósito: una corrección de
    # oficina vale frente a CUALQUIER supervisor que reenvíe ese día, no solo
    # frente al que capturó la línea original.
    return {(r["trabajador_id"], r["partida_id"]) for r in rows}
