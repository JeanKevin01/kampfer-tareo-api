# ============================================================
# routers/ev/partidas.py — plantillas de hitos + CRUD de partidas + OTMs + importador masivo (F0.5b)
# Extraído de valor_ganado.py SIN cambios de lógica.
# ============================================================
import json  # noqa: F401
import os  # noqa: F401
from collections import defaultdict  # noqa: F401
from datetime import date, timedelta, datetime, timezone  # noqa: F401
from typing import Optional  # noqa: F401

import asyncpg  # noqa: F401
from fastapi import APIRouter, HTTPException  # noqa: F401
from pydantic import BaseModel, Field  # noqa: F401

from core.db import db  # noqa: F401
from core.log import get_logger
from core.tiempo import LIMA, semana_de as _semana_de  # noqa: F401

from routers.ev._datos import (  # noqa: F401
    _hoy_lima, _as_date, _get, _norm_tipo_costo, _norm_naturaleza, _fecha_base,
    _hh_real_por_semana, _hh_real_split, _hh_gastadas_unificada, _improductivas,
    _datos_base,
)
from routers.ev._engine import (  # noqa: F401
    _validar_pesos, _acum_a_semana, _calcular, _agrupar, _matriz_area_disciplina,
    _calc_costo_mo, _totales,
)
from routers.ev._modelos import *  # noqa: F401,F403

log = get_logger("ev")

# Sin prefijo: /ev lo aporta el router agregador (valor_ganado.py).
router = APIRouter()
router_campo = APIRouter()


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
                        metrado_presup, metrado_proyec, hh_presup, hh_actualizado)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id""",
                    body.codigo, body.otm_id, body.fase, body.sub_fase, body.descripcion,
                    body.unidad, body.sistema, body.metrado_presup,
                    body.metrado_proyec, body.hh_presup, body.hh_actualizado,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    409, f"Ya existe una partida con código {body.codigo} "
                         f"en la OTM {body.otm_id or '(sin OTM)'}")
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
                   metrado_proyec=$10, hh_presup=$11, hh_actualizado=$12 WHERE id=$1""",
                partida_id, body.codigo, body.otm_id, body.fase, body.sub_fase,
                body.descripcion, body.unidad, body.sistema,
                body.metrado_presup, body.metrado_proyec, body.hh_presup,
                body.hh_actualizado,
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
                tipo_costo = _norm_tipo_costo(p.tipo_costo)
                naturaleza = _norm_naturaleza(p.naturaleza)

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

                # 0008: el código es único POR OTM — resolver siempre con la pareja (codigo, otm)
                existente = await con.fetchval(
                    "SELECT id FROM ev_partidas WHERE codigo=$1 AND otm_id IS NOT DISTINCT FROM $2",
                    p.codigo, p.otm_id,
                )
                if existente:
                    await con.execute(
                        """UPDATE ev_partidas SET otm_id=$2, fase=$3, sub_fase=$4, descripcion=$5,
                           unidad=$6, sistema=$7, metrado_presup=$8, metrado_proyec=$9,
                           hh_presup=$10, nivel=$11, parent_codigo=$12, tipo_costo=$13,
                           naturaleza=$14, hh_actualizado=$15, activo=TRUE WHERE id=$1""",
                        existente, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo, tipo_costo, naturaleza, p.hh_actualizado,
                    )
                    await con.execute("DELETE FROM ev_hitos WHERE partida_id=$1", existente)
                    pid = existente; actualizadas += 1
                else:
                    pid = await con.fetchval(
                        """INSERT INTO ev_partidas
                           (codigo, otm_id, fase, sub_fase, descripcion, unidad, sistema,
                            metrado_presup, metrado_proyec, hh_presup, nivel, parent_codigo,
                            tipo_costo, naturaleza, hh_actualizado)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id""",
                        p.codigo, p.otm_id, p.fase, p.sub_fase, p.descripcion, p.unidad,
                        p.sistema, p.metrado_presup, p.metrado_proyec, p.hh_presup,
                        nivel, parent_codigo, tipo_costo, naturaleza, p.hh_actualizado,
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

            async def _pid_por_codigo(codigo: str):
                """Resuelve un código NO incluido en este import. Con unicidad por OTM (0008)
                el código a secas puede ser ambiguo → error explícito en vez de adivinar."""
                filas = await con.fetch("SELECT id FROM ev_partidas WHERE codigo=$1", codigo)
                if len(filas) > 1:
                    return "AMBIGUO"
                return filas[0]["id"] if filas else None

            av_ins, hist_err = 0, []
            for a in body.avances:
                pid = codigo_a_id.get(a.codigo) or await _pid_por_codigo(a.codigo)
                if pid == "AMBIGUO":
                    hist_err.append(f"Avance: código {a.codigo} existe en varias OTMs — "
                                    "incluye la partida en el import para desambiguar")
                    continue
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
                pid = codigo_a_id.get(r.codigo) or await _pid_por_codigo(r.codigo)
                if pid == "AMBIGUO":
                    hist_err.append(f"HH: código {r.codigo} existe en varias OTMs — "
                                    "incluye la partida en el import para desambiguar")
                    continue
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


