# ============================================================
# routers/ro_proyeccion.py — F2.6: proyección mixta AUTO/MANUAL del RO
#
# POST /ev/ro/proyectar   → recalcula la propuesta AUTO: costo restante por
#   (fase, recurso) = costo_meta × (1 − avance_fase del EV), repartido en los
#   meses restantes estimados por el ritmo real (HH ganadas de las últimas 4
#   semanas). SOLO pisa celdas AUTO — las MANUAL sobreviven.
# PUT  /ev/ro/proyeccion  → edita UNA celda (origen MANUAL, con quién/cuándo).
# GET  /ev/ro/proyeccion  → celdas por periodo futuro.
# snapshot_prev(con, ...) → al CERRAR un mes (periodos.py) congela la proyección
#   del mes siguiente en ro_prev (la columna PREV del motor).
# ============================================================
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from core.auth import require_role
from core.db import db
from routers.periodos import periodo_de

router = APIRouter(prefix="/ev/ro", tags=["resultado-operativo"])


def repartir_restante(restante_por_celda: dict, meses: list) -> list:
    """PURA: reparte cada (fase, recurso) → monto restante en partes iguales entre
    los periodos futuros dados (el último absorbe el redondeo)."""
    filas = []
    n = len(meses)
    if n == 0:
        return filas
    for (fase, rec), monto in restante_por_celda.items():
        if abs(monto) < 0.005:
            continue
        cuota = round(monto / n, 2)
        acum = 0.0
        for i, per in enumerate(meses):
            m = round(monto - acum, 2) if i == n - 1 else cuota
            acum += m
            filas.append({"fase": fase, "tipo_recurso": rec, "periodo_id": per, "monto": m})
    return filas


def meses_restantes_por_ritmo(hh_restantes: float, hh_ganadas_4sem: float,
                              minimo: int = 1, maximo: int = 24) -> int:
    """PURA: nº de meses para terminar al ritmo actual (4.33 semanas/mes)."""
    if hh_restantes <= 0:
        return minimo
    if hh_ganadas_4sem <= 0:
        return maximo
    ritmo_mensual = hh_ganadas_4sem / 4 * 4.33
    n = int(hh_restantes / ritmo_mensual + 0.999)
    return max(minimo, min(maximo, n))


async def _avance_por_fase(con, proyecto_id: int):
    """(avance_fase {fase: 0..1}, hh_restantes, hh_ganadas_ultimas_4_semanas) desde el motor EV."""
    from routers.ev._datos import _datos_base, _fecha_base
    from routers.ev._engine import _calcular

    sem_max = await con.fetchval(
        "SELECT COALESCE(MAX(semana), 1) FROM tareo_partida") or 1
    partidas, hitos, avances, hh, tareo, split = await _datos_base(int(sem_max))
    filas = _calcular(partidas, hitos, avances, hh, tareo, int(sem_max), split)
    ganadas_f, meta_f = {}, {}
    for f in filas:
        fase = f.get("fase") or "(SIN)"
        ganadas_f[fase] = ganadas_f.get(fase, 0.0) + f["hh_ganadas_acum"]
        meta_f[fase] = meta_f.get(fase, 0.0) + (f["hh_presup"] or 0.0)
    avance = {f: (ganadas_f.get(f, 0.0) / m if m > 0 else 0.0) for f, m in meta_f.items()}
    hh_rest = sum(max(m - ganadas_f.get(f, 0.0), 0.0) for f, m in meta_f.items())
    # ritmo: HH ganadas en las últimas 4 semanas (aprox: diferencia de acumulados)
    sem_ini = max(1, int(sem_max) - 4)
    filas_prev = _calcular(partidas, hitos, avances, hh, tareo, sem_ini, split)
    gan_prev = sum(f["hh_ganadas_acum"] for f in filas_prev)
    gan_hoy = sum(ganadas_f.values())
    return avance, hh_rest, max(gan_hoy - gan_prev, 0.0)


@router.post("/proyectar")
async def proyectar(data: dict, user: dict = Depends(require_role("oficina"))):
    """Regenera la propuesta AUTO {proyecto_id, meses?: n}. Respeta las celdas MANUAL."""
    proyecto_id = int(data.get("proyecto_id") or 1)
    pool = await db()
    async with pool.acquire() as con:
        meta_rows = await con.fetch(
            """SELECT pcm.fase, pcm.tipo_recurso, pcm.monto::float AS monto
               FROM presupuesto_costo_meta pcm
               JOIN presupuestos p ON p.id = pcm.presupuesto_id
               WHERE p.proyecto_id = $1 AND p.tipo = 'META' AND p.vigente""", proyecto_id)
        if not meta_rows:
            raise HTTPException(409, "No hay presupuesto META vigente (congela uno en Presupuesto → Meta)")
        avance, hh_rest, gan4 = await _avance_por_fase(con, proyecto_id)
        n_meses = int(data.get("meses") or 0) or meses_restantes_por_ritmo(hh_rest, gan4)

        # celdas restantes = meta × (1 − avance de su fase)
        restante = {}
        for r in meta_rows:
            av = min(avance.get(r["fase"], 0.0), 1.0)
            resto = r["monto"] * (1 - av)
            if resto > 0.005:
                restante[(r["fase"], r["tipo_recurso"])] = round(resto, 2)

        # meses futuros (se crean a partir del mes siguiente al actual)
        hoy = date.today()
        meses_ids = []
        anio, mes = hoy.year, hoy.month
        for _ in range(n_meses):
            mes += 1
            if mes > 12:
                mes, anio = 1, anio + 1
            meses_ids.append(await periodo_de(con, proyecto_id, date(anio, mes, 1)))

        filas = repartir_restante(restante, meses_ids)
        async with con.transaction():
            await con.execute(
                "DELETE FROM ro_proyeccion WHERE proyecto_id=$1 AND origen='AUTO'", proyecto_id)
            escritas = 0
            for f in filas:
                r = await con.execute(
                    """INSERT INTO ro_proyeccion
                       (proyecto_id, fase, tipo_recurso, periodo_id, monto, origen, actualizado_por)
                       VALUES ($1,$2,$3,$4,$5,'AUTO',$6)
                       ON CONFLICT (proyecto_id, fase, tipo_recurso, periodo_id) DO NOTHING""",
                    proyecto_id, f["fase"], f["tipo_recurso"], f["periodo_id"],
                    f["monto"], user.get("sub"))
                if r.endswith("1"):
                    escritas += 1
    return {"ok": True, "meses": n_meses, "celdas_propuestas": len(filas),
            "escritas": escritas, "respetadas_manual": len(filas) - escritas,
            "hh_restantes": round(hh_rest, 1), "ritmo_hh_4sem": round(gan4, 1)}


@router.put("/proyeccion")
async def editar_celda(data: dict, user: dict = Depends(require_role("oficina"))):
    """Edita una celda {proyecto_id, fase, tipo_recurso, periodo_id, monto} → MANUAL."""
    try:
        proyecto_id = int(data.get("proyecto_id") or 1)
        periodo_id = int(data["periodo_id"])
        monto = float(data["monto"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "periodo_id y monto son requeridos")
    pool = await db()
    await pool.execute(
        """INSERT INTO ro_proyeccion
           (proyecto_id, fase, tipo_recurso, periodo_id, monto, origen, actualizado_por, actualizado_en)
           VALUES ($1,$2,$3,$4,$5,'MANUAL',$6, now())
           ON CONFLICT (proyecto_id, fase, tipo_recurso, periodo_id)
           DO UPDATE SET monto=$5, origen='MANUAL', actualizado_por=$6, actualizado_en=now()""",
        proyecto_id, data.get("fase"), str(data.get("tipo_recurso") or "MAT").upper(),
        periodo_id, monto, user.get("sub"))
    return {"ok": True, "origen": "MANUAL"}


@router.get("/proyeccion")
async def ver_proyeccion(proyecto_id: int = 1):
    pool = await db()
    rows = await pool.fetch(
        """SELECT rp.*, rp.monto::float AS monto, p.anio, p.mes
           FROM ro_proyeccion rp JOIN periodos p ON p.id = rp.periodo_id
           WHERE rp.proyecto_id = $1 ORDER BY p.anio, p.mes, rp.fase, rp.tipo_recurso""",
        proyecto_id)
    return [dict(r) for r in rows]


async def snapshot_prev(con, proyecto_id: int, periodo_cerrado_id: int) -> int:
    """Al cerrar un mes: congela la proyección del MES SIGUIENTE en ro_prev
    (será el PREV cuando ese mes se analice). Lo llama periodos.cerrar."""
    p1 = await con.fetchrow("SELECT anio, mes FROM periodos WHERE id=$1", periodo_cerrado_id)
    if not p1:
        return 0
    anio, mes = (p1["anio"] + 1, 1) if p1["mes"] == 12 else (p1["anio"], p1["mes"] + 1)
    sig_id = await periodo_de(con, proyecto_id, date(anio, mes, 1))
    await con.execute(
        "DELETE FROM ro_prev WHERE proyecto_id=$1 AND periodo_id=$2", proyecto_id, sig_id)
    res = await con.execute(
        """INSERT INTO ro_prev (proyecto_id, periodo_id, fase, tipo_recurso, monto)
           SELECT proyecto_id, periodo_id, fase, tipo_recurso, monto
           FROM ro_proyeccion WHERE proyecto_id=$1 AND periodo_id=$2""",
        proyecto_id, sig_id)
    return int(res.split()[-1] or 0)
