# ============================================================
# routers/ev/_datos.py — capa de datos del motor EV (F0.5b)
#
# Todo lo que lee/escribe BD para el cálculo: fecha base, HH del tareo,
# improductivas y el fetch base de partidas/hitos/avances. Sin endpoints.
# ============================================================
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from core.db import db
from core.tiempo import LIMA, semana_de as _semana_de  # noqa: F401


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


def _get(row, key, default=None):
    """Acceso tolerante a dict y a asyncpg.Record (que no tiene .get())."""
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _norm_tipo_costo(v) -> str:
    """Normaliza a 'DIRECTO' | 'INDIRECTO' (default DIRECTO)."""
    t = str(v or "DIRECTO").strip().upper()
    return "INDIRECTO" if t == "INDIRECTO" else "DIRECTO"


def _norm_naturaleza(v) -> str:
    """Normaliza a 'CONTRACTUAL' | 'ADICIONAL' (default CONTRACTUAL)."""
    t = str(v or "CONTRACTUAL").strip().upper()
    return "ADICIONAL" if t in ("ADICIONAL", "ADICIONALES", "ADIC") else "CONTRACTUAL"


# ---------------------- Config (fecha base) ----------------------
async def _fecha_base(con) -> Optional[date]:
    """Lunes que ancla la semana 1 del proyecto.

    F0.3 + decisión de Jean (2026-07-05): si no está configurada, se AUTO-DERIVA
    del primer día con HH del tareo QR (tareo_partida) alineado a lunes, y se
    PERSISTE para que la numeración de semanas quede estable aunque después se
    cargue data anterior. Si está configurada, se alinea SIEMPRE a lunes (fix del
    bug de semana inconsistente entre main.py y este módulo)."""
    v = await con.fetchval("SELECT valor FROM ev_config WHERE clave='fecha_base'")
    if v:
        base = date.fromisoformat(v)
        return base - timedelta(days=base.weekday())
    f = await con.fetchval(
        "SELECT MIN(fecha) FROM tareo_partida WHERE hh IS NOT NULL AND hh > 0"
    )
    if f:
        base = f - timedelta(days=f.weekday())
        await con.execute(
            "INSERT INTO ev_config (clave, valor) VALUES ('fecha_base', $1) "
            "ON CONFLICT (clave) DO NOTHING", base.isoformat()
        )
        return base
    return None


# ---------------------- HH del tareo (fuente única) ----------------------
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


async def _hh_real_split(con) -> dict:
    """{(partida_id, semana): {'dir':hh_directas, 'tot':hh_totales}} desde tareo_partida,
    clasificando por trabajadores.tipo (INDIRECTO vs resto = DIRECTO). Sirve para sacar
    la fracción directa de cada partida y separar el PF directo del total."""
    out: dict = {}
    base = await _fecha_base(con)
    if not base:
        return out
    rows = await con.fetch(
        """SELECT tp.partida_id, tp.fecha,
                  SUM(tp.hh) AS tot,
                  SUM(CASE WHEN COALESCE(t.tipo,'DIRECTO') <> 'INDIRECTO'
                           THEN tp.hh ELSE 0 END) AS dir
           FROM tareo_partida tp
           LEFT JOIN trabajadores t
                  ON t.id = tp.trabajador_id
           WHERE tp.hh IS NOT NULL
           GROUP BY tp.partida_id, tp.fecha"""
    )
    for r in rows:
        k = (r['partida_id'], _semana_de(r['fecha'], base))
        e = out.setdefault(k, {'dir': 0.0, 'tot': 0.0})
        e['dir'] += float(r['dir'] or 0)
        e['tot'] += float(r['tot'] or 0)
    return out


async def _hh_gastadas_unificada(con) -> dict:
    """FUENTE ÚNICA de HH gastadas por (partida, semana) para árbol/ISP/reporte.

    Precedencia (de mayor a menor):
      1. override manual del residente  (ev_hh_gastadas fuente 'manual'/'distribucion')
      2. tareo_partida REAL              (lo que captura la app — fuente principal)
      3. migración histórica             (ev_hh_gastadas fuente 'historico'/'importado')
    (F0.3: el nivel 4 proporcional desde `registros` fue retirado — flujo legacy congelado)

    Las filas 'diario'/'tareo' de ev_hh_gastadas (que producía el botón
    "Volcar al ISP") se IGNORAN: ahora se lee tareo_partida directamente, así que
    el volcado manual ya no es necesario y no hay doble conteo.
    """
    out: dict = {}
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


async def _improductivas(con, semana: int, otm: Optional[str] = None):
    """HH improductivas (captura de oficina, semanal por OTM). Son HH consumidas
    NO asignadas a partidas: entran al total de HH gastadas y bajan el PF del
    proyecto. acum = Σ hh con semana<=; sem = Σ hh de la semana exacta."""
    if otm:
        rows = await con.fetch(
            "SELECT semana, hh, motivo FROM ev_hh_improductivas WHERE semana <= $1 AND otm_id = $2",
            semana, otm,
        )
    else:
        rows = await con.fetch(
            "SELECT semana, hh, motivo FROM ev_hh_improductivas WHERE semana <= $1", semana,
        )
    acum = sum(float(r["hh"]) for r in rows)
    sem = sum(float(r["hh"]) for r in rows if r["semana"] == semana)
    por_motivo: dict = defaultdict(float)
    for r in rows:
        por_motivo[r["motivo"] or "SIN MOTIVO"] += float(r["hh"])
    return {
        "acum": round(acum, 2),
        "sem": round(sem, 2),
        "por_motivo": [{"motivo": k, "hh": round(v, 2)} for k, v in sorted(por_motivo.items())],
    }


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
        split = await _hh_real_split(con)
    # hh_rows se devuelve vacío: las HH gastadas ya vienen unificadas en `tareo`
    # (manual > tareo_partida real > histórico > proporcional). Ver _hh_gastadas_unificada.
    return partidas, hitos, avances, [], tareo, split
