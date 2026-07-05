# ============================================================
# routers/ro.py — Fase 2: Resultado Operativo (RO) "a la fecha"
#
# Por fase: VENTA − COSTO = MARGEN (%), con el costo abierto en recursos.
#   · Costo MO   = HH del tareo (tareo_partida) × tarifa por cargo (ev_tarifas_cargo).
#   · Costo no-MO = tabla `costos` (MAT/EQP/EQT/SUB directos; DIR/GG indirectos).
#   · Venta      = Σ(cantidad valorizada × PU del presupuesto) + `venta_ajustes`.
#
# Se monta en main.py con require_role("oficina").
# ============================================================
from collections import defaultdict
from datetime import date
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# F0.4: este router usa el pool asyncpg único (core.db); la lib `databases` salió de aquí.
from core.db import db

router = APIRouter(prefix="/ev/ro", tags=["resultado-operativo"])


# ── Modelos de carga ──────────────────────────────────────────
class CostoIn(BaseModel):
    fase: Optional[str] = None
    tipo_recurso: str                 # MAT | EQP | EQT | SUB | DIR | GG
    directo: bool = True
    periodo: str                      # 'YYYY-MM-DD' (día 1 del mes)
    monto: float = 0
    fuente: Optional[str] = None
    nota: Optional[str] = None


class CostosIn(BaseModel):
    proyecto_id: int = 1
    costos: List[CostoIn]


class AjusteIn(BaseModel):
    fase: Optional[str] = None
    tipo: str                         # CONTRACTUAL | ADICIONAL | REAJUSTE | TERCEROS
    periodo: Optional[str] = None
    monto: float = 0
    nota: Optional[str] = None


class AjustesIn(BaseModel):
    proyecto_id: int = 1
    ajustes: List[AjusteIn]


def _d(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s[:10]) if s else None


# ── Lógica pura (testeable sin BD) ────────────────────────────
def _calc_mo_por_fase(hh_rows, tarifas: dict, default) -> dict:
    """Costo de MO por fase = Σ (HH del cargo × tarifa). hh_rows: [{fase,cargo,hh}]."""
    out: dict = defaultdict(float)
    for r in hh_rows:
        rate = tarifas.get(r["cargo"])
        if rate is None:
            rate = default
        if rate is None:
            rate = 0.0
        out[r["fase"]] += float(r["hh"] or 0) * rate
    return dict(out)


def _armar_ro(mo_por_fase: dict, costos, venta_real: dict, ajustes, fases_desc: dict):
    """Arma el cuadro RO. Función pura → testeable.
    costos: [{fase,tipo_recurso,directo,monto}] · ajustes: [{fase,tipo,monto}]."""
    dir_por_fase = defaultdict(lambda: defaultdict(float))   # fase -> tipo -> monto (directos)
    ind_por_tipo = defaultdict(float)                        # tipo -> monto (indirectos)
    for c in costos:
        if c["directo"]:
            dir_por_fase[c["fase"]][c["tipo_recurso"]] += float(c["monto"] or 0)
        else:
            ind_por_tipo[c["tipo_recurso"]] += float(c["monto"] or 0)

    ajuste_por_fase = defaultdict(float)
    ajuste_sin_fase = 0.0
    for a in ajustes:
        if a["fase"]:
            ajuste_por_fase[a["fase"]] += float(a["monto"] or 0)
        else:
            ajuste_sin_fase += float(a["monto"] or 0)

    fases = (set(mo_por_fase) | set(dir_por_fase) | set(venta_real) | set(ajuste_por_fase))
    fases.discard(None)

    filas = []
    tot_costo_dir = tot_venta = 0.0
    for f in sorted(fases):
        d = dir_por_fase.get(f, {})
        mat, eqp, eqt, sub = (d.get("MAT", 0.0), d.get("EQP", 0.0), d.get("EQT", 0.0), d.get("SUB", 0.0))
        mo = float(mo_por_fase.get(f, 0.0))
        costo = round(mat + mo + eqp + eqt + sub, 2)
        venta = round(float(venta_real.get(f, 0.0)) + ajuste_por_fase.get(f, 0.0), 2)
        margen = round(venta - costo, 2)
        filas.append({
            "fase": f, "descripcion": fases_desc.get(f),
            "mat": round(mat, 2), "mo": round(mo, 2), "eqp": round(eqp, 2),
            "eqt": round(eqt, 2), "sub": round(sub, 2),
            "costo": costo, "venta": venta, "margen": margen,
            "pct_margen": round(margen / venta, 4) if venta > 0 else 0,
        })
        tot_costo_dir += costo
        tot_venta += venta

    costo_ind = round(sum(ind_por_tipo.values()), 2)
    venta_total = round(tot_venta + ajuste_sin_fase, 2)
    costo_total = round(tot_costo_dir + costo_ind, 2)
    margen_total = round(venta_total - costo_total, 2)
    return {
        "fases": filas,
        "indirectos": {"DIR": round(ind_por_tipo.get("DIR", 0.0), 2),
                       "GG": round(ind_por_tipo.get("GG", 0.0), 2),
                       "total": costo_ind},
        "totales": {
            "costo_directo": round(tot_costo_dir, 2), "costo_indirecto": costo_ind,
            "costo_total": costo_total, "venta": venta_total, "margen": margen_total,
            "pct_margen": round(margen_total / venta_total, 4) if venta_total > 0 else 0,
        },
    }


# SQL: HH del tareo por fase y cargo (join directo: 0009 garantiza ids con pad + FK).
_HH_FASE_CARGO_SQL = """
    SELECT ev.fase AS fase, COALESCE(t.cargo, '(Sin cargo)') AS cargo, SUM(tp.hh) AS hh
    FROM tareo_partida tp
    JOIN ev_partidas ev ON ev.id = tp.partida_id
    LEFT JOIN trabajadores t ON t.id = tp.trabajador_id
    WHERE tp.hh IS NOT NULL AND ev.fase IS NOT NULL
      AND ev.otm_id IN (SELECT id FROM otms WHERE proyecto_id = $1)
    GROUP BY ev.fase, t.cargo
"""
# SQL: venta real por fase = Σ(cantidad valorizada × PU).
_VENTA_FASE_SQL = """
    SELECT ev.fase AS fase, SUM(v.cantidad_valorizada * ev.precio_unitario) AS venta
    FROM ev_valorizado v
    JOIN ev_partidas ev ON ev.id = v.partida_id
    WHERE ev.fase IS NOT NULL
      AND ev.otm_id IN (SELECT id FROM otms WHERE proyecto_id = $1)
    GROUP BY ev.fase
"""


# ── Endpoints ─────────────────────────────────────────────────
@router.get("")
async def resultado_operativo(proyecto_id: int = 1):
    pool = await db()
    async with pool.acquire() as con:
        hh_rows = await con.fetch(_HH_FASE_CARGO_SQL, proyecto_id)
        tar_rows = await con.fetch("SELECT cargo, costo_hh FROM ev_tarifas_cargo")
        costos = [dict(r) for r in await con.fetch(
            "SELECT fase, tipo_recurso, directo, SUM(monto) AS monto FROM costos "
            "WHERE proyecto_id = $1 GROUP BY fase, tipo_recurso, directo", proyecto_id)]
        venta_rows = await con.fetch(_VENTA_FASE_SQL, proyecto_id)
        ajustes = [dict(r) for r in await con.fetch(
            "SELECT fase, tipo, SUM(monto) AS monto FROM venta_ajustes "
            "WHERE proyecto_id = $1 GROUP BY fase, tipo", proyecto_id)]
        fases_rows = await con.fetch(
            "SELECT codigo AS fase, descripcion FROM ev_partidas WHERE codigo IN "
            "(SELECT DISTINCT fase FROM ev_partidas WHERE fase IS NOT NULL)")

    tarifas = {r["cargo"]: float(r["costo_hh"]) for r in tar_rows}
    default = tarifas.get("(Default)")
    mo = _calc_mo_por_fase([dict(r) for r in hh_rows], tarifas, default)
    venta_real = {r["fase"]: float(r["venta"] or 0) for r in venta_rows}
    fases_desc = {r["fase"]: r["descripcion"] for r in fases_rows}

    return _armar_ro(mo, costos, venta_real, ajustes, fases_desc)


@router.get("/costos")
async def listar_costos(proyecto_id: int = 1):
    pool = await db()
    rows = await pool.fetch(
        "SELECT * FROM costos WHERE proyecto_id = $1 ORDER BY periodo DESC, fase", proyecto_id)
    return [dict(r) for r in rows]


@router.post("/costos")
async def cargar_costos(body: CostosIn):
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for c in body.costos:
                await con.execute(
                    """INSERT INTO costos (proyecto_id, fase, tipo_recurso, directo, periodo, monto, fuente, nota)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    body.proyecto_id, c.fase, c.tipo_recurso, c.directo,
                    _d(c.periodo), c.monto, c.fuente, c.nota,
                )
    return {"ok": True, "cargados": len(body.costos)}


@router.delete("/costos/{cid}")
async def borrar_costo(cid: int):
    pool = await db()
    await pool.execute("DELETE FROM costos WHERE id = $1", cid)
    return {"ok": True}


@router.get("/venta-ajustes")
async def listar_ajustes(proyecto_id: int = 1):
    pool = await db()
    rows = await pool.fetch(
        "SELECT * FROM venta_ajustes WHERE proyecto_id = $1 ORDER BY id DESC", proyecto_id)
    return [dict(r) for r in rows]


@router.post("/venta-ajustes")
async def cargar_ajustes(body: AjustesIn):
    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            for a in body.ajustes:
                await con.execute(
                    """INSERT INTO venta_ajustes (proyecto_id, fase, tipo, periodo, monto, nota)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    body.proyecto_id, a.fase, a.tipo, _d(a.periodo), a.monto, a.nota,
                )
    return {"ok": True, "cargados": len(body.ajustes)}


@router.delete("/venta-ajustes/{aid}")
async def borrar_ajuste(aid: int):
    pool = await db()
    await pool.execute("DELETE FROM venta_ajustes WHERE id = $1", aid)
    return {"ok": True}
