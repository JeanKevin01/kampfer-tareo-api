# ============================================================
# routers/ev/historico.py — cuadrillas plantilla + carga de historico (F0.5b)
# Extraído de valor_ganado.py SIN cambios de lógica.
# ============================================================
import json  # noqa: F401
import os  # noqa: F401
from collections import defaultdict  # noqa: F401
from datetime import date, timedelta, datetime, timezone  # noqa: F401
from typing import Optional  # noqa: F401

import asyncpg  # noqa: F401
from fastapi import APIRouter, Depends, HTTPException  # noqa: F401
from pydantic import BaseModel, Field  # noqa: F401

from core.auth import exigir_identidad_supervisor, require_role
from core.db import db  # noqa: F401
# Una sola regla de «quien la arma la usa», compartida con el alta del panel.
from core.cuadrillas import marcar_habitual as _marcar_habitual
from core.ids import ids_unicos as _ids_unicos
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


# ═══════════════════════════════════════════════════════════════
# FASE 1 — Control Maestro: Cuadrillas + Asignación + Histórico
# ═══════════════════════════════════════════════════════════════

# ── Cuadrillas del supervisor ─────────────────────────────────
# Fuente única desde 0046: `cuadrilla_grupos` + `cuadrilla_grupo_miembros`, la
# MISMA que gestiona el panel (/api/cuadrillas/*). Antes esto vivía en
# `cuadrilla_otm`, que nadie más leía: el panel escribía en otra tabla y por eso
# crear una cuadrilla en oficina no se veía nunca en el tareo.
#
# El path y el shape se conservan intactos —{nombre: [miembros]}— porque el
# index.html desplegado en campo los consume tal cual. `otm_id` se sigue
# aceptando por compatibilidad con esa app y con lo que haya en su outbox, pero
# ya no discrimina: la cuadrilla es del supervisor y sirve en cualquier proyecto.

@router_campo.get("/cuadrillas-plantilla")
async def listar_cuadrillas_plantilla(
    supervisor_id: str,
    otm_id: str | None = None,   # aceptado por compat; ya no filtra
):
    """TODAS las cuadrillas, con las HABITUALES de este supervisor primero.

    Desde 0048 son libres: si hoy le toca el frente de otro, quiere su lista sin
    tener que pedirla. El orden es lo que evita que eso sea una lista larga
    donde la suya está enterrada — el dict conserva el orden en JSON. Cuáles son
    habituales se pide aparte (`/ev/cuadrillas-habituales`) para no tocar este
    shape, que lo consume el index.html ya desplegado en los teléfonos."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT g.nombre AS plantilla, m.trab_id AS trabajador_id,
                      t.nombre, t.cargo, COALESCE(t.tipo,'DIRECTO') AS tipo,
                      m.orden
               FROM cuadrilla_grupos g
               LEFT JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id
               LEFT JOIN trabajadores t ON t.id = m.trab_id AND t.activo = TRUE
               WHERE g.activo = TRUE
               ORDER BY (NOT EXISTS (SELECT 1 FROM cuadrilla_habituales h
                                      WHERE h.grupo_id = g.id
                                        AND h.supervisor_id = $1)),
                        lower(g.nombre), m.orden, t.nombre""",
            supervisor_id
        )
        # El LEFT JOIN mantiene visible la cuadrilla que se quedó sin miembros
        # activos: que desaparezca de la pantalla sin explicación es peor que
        # verla vacía. Un trabajador dado de baja sí sale de la lista — no se
        # puede tarear a alguien que ya no está en el padrón.
        plantillas: dict = {}
        for r in rows:
            plantillas.setdefault(r["plantilla"], [])
            if r["trabajador_id"] is None or r["nombre"] is None:
                continue
            plantillas[r["plantilla"]].append({
                "trabajador_id": r["trabajador_id"],
                "nombre":        r["nombre"],
                "cargo":         r["cargo"],
                "tipo":          r["tipo"],
                "orden":         r["orden"],
            })
        return plantillas


@router_campo.get("/cuadrillas-habituales")
async def listar_cuadrillas_habituales(supervisor_id: str):
    """Nombres de las cuadrillas habituales de ese supervisor.

    Endpoint aparte y no un campo dentro de `/cuadrillas-plantilla` porque ese
    shape es `{nombre: [miembros]}` y lo consume el teléfono ya desplegado:
    meterle una clave especial la convertiría en una cuadrilla fantasma. Van
    nombres y no ids porque el teléfono indexa las plantillas por nombre, que
    desde 0048 es único en toda la empresa."""
    pool = await db()
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT g.nombre
                 FROM cuadrilla_habituales h
                 JOIN cuadrilla_grupos g ON g.id = h.grupo_id AND g.activo
                WHERE h.supervisor_id = $1
                ORDER BY lower(g.nombre)""",
            supervisor_id
        )
        return {"habituales": [r["nombre"] for r in rows]}


@router_campo.post("/cuadrillas-plantilla")
async def guardar_cuadrilla_plantilla(
    data: dict,
    user: dict = Depends(require_role()),
):
    """Crea o reemplaza una cuadrilla del supervisor (la guarda el teléfono)."""
    supervisor_id = data.get("supervisor_id")
    nombre        = str(data.get("nombre") or "Principal").strip()[:100]
    trabajadores  = data.get("trabajadores", [])

    if not supervisor_id:
        raise HTTPException(400, "supervisor_id es requerido")
    if not nombre:
        raise HTTPException(422, "La cuadrilla necesita un nombre")
    # F0.6: sin esto cualquier supervisor autenticado podía sobrescribir la
    # cuadrilla de otro mandando su id en el cuerpo.
    exigir_identidad_supervisor(user, supervisor_id)

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            # El nombre es único en toda la empresa (0048): guardar «Encofrado»
            # cuando ya existe REEMPLAZA su lista, que es la misma semántica que
            # tenía por supervisor y la que espera el outbox del teléfono.
            gid = await con.fetchval(
                "UPDATE cuadrilla_grupos SET activo = TRUE, nombre = $1 "
                " WHERE lower(nombre) = lower($1) RETURNING id", nombre)
            if not gid:
                gid = await con.fetchval(
                    "INSERT INTO cuadrilla_grupos (creada_por, nombre) "
                    "VALUES ($1,$2) RETURNING id", supervisor_id, nombre)
            # Reemplazo, no merge: la app manda la lista completa que el
            # supervisor tiene en pantalla (misma semántica que antes).
            await con.execute(
                "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = $1", gid)
            for idx, tid in enumerate(_ids_unicos(trabajadores)):
                await con.execute(
                    """INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden)
                       VALUES ($1,$2,$3) ON CONFLICT DO NOTHING""",
                    gid, tid, idx
                )
            # La guardó desde su teléfono: mañana la quiere arriba, no revuelta
            # con las de todos.
            await _marcar_habitual(con, supervisor_id, gid)
    return {"ok": True, "nombre": nombre, "total": len(_ids_unicos(trabajadores))}


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


