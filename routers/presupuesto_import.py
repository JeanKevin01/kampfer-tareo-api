# ============================================================
# routers/presupuesto_import.py — F1.2: importar la plantilla PU (.xls)
#
# POST /ev/presupuesto/importar-pu (multipart)
#   ?confirmar=false → SOLO preview: {resumen, muestra} (no toca la BD)
#   ?confirmar=true  → si 0 errores: transacción única que crea
#       presupuestos(tipo='META', version=siguiente, moneda='USD') +
#       presupuesto_partidas (padres, hojas y subpartidas) + apu_recursos.
# Toda fila ilegible se reporta con su número; con errores NO se permite
# confirmar (no hay import parcial). Se monta en main.py con rol oficina.
# ============================================================
from fastapi import APIRouter, File, HTTPException, UploadFile

from core.db import db
from core.log import get_logger
from parsers.plantilla_pu import parsear_plantilla_pu
from routers.presupuesto_derivados import hh_meta

log = get_logger("presupuesto")

router = APIRouter(prefix="/ev/presupuesto", tags=["presupuesto"])


def _resumen(res) -> dict:
    hojas = [p for p in res.partidas if p.es_hoja]
    total = sum((p.cud or p.pu_meta or 0) * p.metrado for p in hojas)
    hh_total = sum(hh_meta([{"tipo": r.tipo, "cantidad": r.cantidad} for r in p.recursos],
                           p.metrado) for p in hojas)
    return {
        "partidas": len(res.partidas),
        "hojas": len(hojas),
        "subpartidas": len(res.subpartidas),
        "recursos": sum(len(p.recursos) for p in hojas + res.subpartidas),
        "hh_dia": res.hh_dia,
        "hh_meta_total": round(hh_total, 1),
        "costo_meta_total": round(total, 2),
        "errores": res.errores,
        "avisos": res.avisos,
    }


@router.post("/importar-pu")
async def importar_pu(proyecto_id: int = 1, confirmar: bool = False,
                      nota: str = "", file: UploadFile = File(...)):
    contenido = await file.read()
    try:
        res = parsear_plantilla_pu(contenido)
    except Exception:
        log.exception("importar-pu: archivo ilegible")
        raise HTTPException(400, "El archivo no es un .xls legible (formato BIFF esperado)")

    resumen = _resumen(res)
    hojas = [p for p in res.partidas if p.es_hoja]

    if not confirmar:
        muestra = [{
            "codigo": p.codigo, "descripcion": p.descripcion, "unidad": p.unidad,
            "fase": p.fase, "metrado": p.metrado, "pu_meta": round(p.cud or p.pu_meta or 0, 4),
            "hh_meta": hh_meta([{"tipo": r.tipo, "cantidad": r.cantidad} for r in p.recursos],
                               p.metrado),
            "n_recursos": len(p.recursos),
        } for p in hojas[:10]]
        return {"resumen": resumen, "muestra": muestra}

    if res.errores:
        raise HTTPException(409, {"detail": "El archivo tiene errores; corrige y reintenta",
                                  "errores": res.errores})

    pool = await db()
    async with pool.acquire() as con:
        async with con.transaction():
            version = await con.fetchval(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM presupuestos "
                "WHERE proyecto_id = $1 AND tipo = 'META'", proyecto_id)
            pid = await con.fetchval(
                """INSERT INTO presupuestos (proyecto_id, version, estado, vigente, nota,
                                             tipo, moneda)
                   VALUES ($1, $2, 'BORRADOR', false, $3, 'META', 'USD') RETURNING id""",
                proyecto_id, version,
                nota or f"Importado de {file.filename or 'plantilla PU'}")

            ids: dict = {}   # codigo -> presupuesto_partida_id

            async def _insertar_partida(p, es_sub: bool) -> int:
                hh = hh_meta([{"tipo": r.tipo, "cantidad": r.cantidad} for r in p.recursos],
                             p.metrado) if p.es_hoja else 0.0
                return await con.fetchval(
                    """INSERT INTO presupuesto_partidas
                       (presupuesto_id, codigo, descripcion, unidad, fase, sub_fase,
                        metrado, precio_unitario, hh_meta, rendimiento_mo, rendimiento_eq,
                        hh_dia, area, nivel, parent_codigo)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                       RETURNING id""",
                    pid, p.codigo, p.descripcion, p.unidad, p.fase, p.sub_fase,
                    p.metrado, (p.cud if p.cud is not None else p.pu_meta) or 0.0, hh,
                    p.rend_mo, p.rend_eq, res.hh_dia, p.area,
                    0 if es_sub else p.nivel, p.parent)

            # 1) padres + hojas (orden del PtoMeta) y subpartidas (nivel 0)
            for p in res.partidas:
                ids[p.codigo] = await _insertar_partida(p, es_sub=False)
            for sp in res.subpartidas:
                ids[sp.codigo] = await _insertar_partida(sp, es_sub=True)

            # 2) recursos del APU (hojas + subpartidas)
            n_rec = 0
            for p in list(hojas) + list(res.subpartidas):
                for orden, r in enumerate(p.recursos):
                    sub_id = ids.get(r.sub.codigo) if (r.tipo == "SUB" and r.sub) else None
                    await con.execute(
                        """INSERT INTO apu_recursos
                           (presupuesto_partida_id, tipo, codigo, descripcion, unidad,
                            cuadrilla, cantidad, precio, parcial, sub_partida_id, orden)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                        ids[p.codigo], r.tipo, r.codigo, r.descripcion, r.unidad,
                        r.cuadrilla, r.cantidad, r.precio, r.parcial, sub_id, orden)
                    n_rec += 1

    log.info("importar-pu confirmado", extra={
        "presupuesto_id": pid, "version": version, "partidas": len(ids), "recursos": n_rec})
    return {"ok": True, "id": pid, "version": version, "tipo": "META",
            "partidas": len(ids), "recursos": n_rec, "resumen": resumen}
