from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import database
from datetime import date, datetime, time
from typing import Optional
import math

app = FastAPI(title="Kampfer Tareo API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# ── HEALTH ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.1.0"}

# ── SUPERVISORES ─────────────────────────────────────────────
@app.get("/api/supervisores")
async def get_supervisores():
    rows = await database.fetch_all(
        "SELECT id, nombre, email FROM supervisores WHERE activo = true ORDER BY nombre"
    )
    return [dict(r) for r in rows]

# ── OTMs ─────────────────────────────────────────────────────
@app.get("/api/otms")
async def get_otms():
    rows = await database.fetch_all(
        """SELECT id, descripcion, area, estado, centro_costo
           FROM otms
           WHERE estado IN ('EJECUCION', 'POR INICIAR')
           ORDER BY id"""
    )
    return [dict(r) for r in rows]

# ── TRABAJADORES ─────────────────────────────────────────────
@app.get("/api/trabajadores")
async def get_trabajadores():
    rows = await database.fetch_all(
        "SELECT id, nombre, cargo FROM trabajadores WHERE activo = true ORDER BY nombre"
    )
    return [dict(r) for r in rows]

# ── BUSCAR TRABAJADOR ─────────────────────────────────────────
@app.get("/api/buscar")
async def buscar(q: str):
    if len(q) < 2:
        return []
    rows = await database.fetch_all(
        """SELECT id, nombre, cargo FROM trabajadores
           WHERE activo = true AND (
             nombre ILIKE :q OR cargo ILIKE :q OR id = :id
           ) ORDER BY nombre LIMIT 8""",
        {"q": f"%{q}%", "id": q.zfill(3)}
    )
    return [dict(r) for r in rows]

# ── REGISTRO BATCH ────────────────────────────────────────────
@app.post("/api/registro")
async def registrar(data: dict):
    supervisor_id     = data.get("supervisor_id", "").strip()
    otm_id            = data.get("otm_id", "").strip()
    fecha_raw         = data.get("fecha", date.today().isoformat())
    fecha             = date.fromisoformat(fecha_raw) if isinstance(fecha_raw, str) else fecha_raw
    trabajadores_list = data.get("trabajadores", [])

    if not supervisor_id or not otm_id or not trabajadores_list:
        raise HTTPException(400, "Faltan campos: supervisor_id, otm_id, trabajadores[]")

    # Verificar que el supervisor existe
    sup = await database.fetch_one(
        "SELECT id FROM supervisores WHERE id = :id", {"id": supervisor_id}
    )
    if not sup:
        raise HTTPException(400, f"Supervisor '{supervisor_id}' no encontrado en la base de datos")

    # Verificar que la OTM existe
    otm = await database.fetch_one(
        "SELECT id FROM otms WHERE id = :id", {"id": otm_id}
    )
    if not otm:
        raise HTTPException(400, f"OTM '{otm_id}' no encontrada en la base de datos")

    resultados = []

    for t in trabajadores_list:
        trab_id  = str(t.get("trab_id", "")).zfill(3)
        hora_raw = t.get("hora", datetime.now().strftime("%H:%M:%S"))
        # Asegurar formato HH:MM:SS
        hora = hora_raw[:8] if len(hora_raw) >= 8 else hora_raw + ":00"

        nombre = t.get("nombre", "")
        cargo  = t.get("cargo",  "")

        # Verificar que el trabajador existe en la BD
        trab = await database.fetch_one(
            "SELECT id, nombre, cargo FROM trabajadores WHERE id = :id AND activo = true",
            {"id": trab_id}
        )

        if not trab:
            resultados.append({
                "trab_id": trab_id, "nombre": nombre, "cargo": cargo,
                "status": "error", "mensaje": f"Trabajador ID {trab_id} no encontrado o inactivo"
            })
            continue

        # Verificar duplicado: mismo trabajador + misma OTM + mismo día
        existente = await database.fetch_one(
            """SELECT id, hora::text as hora FROM registros
               WHERE trab_id = :trab_id AND otm_id = :otm_id AND fecha = :fecha""",
            {"trab_id": trab_id, "otm_id": otm_id, "fecha": fecha}
        )

        if existente:
            resultados.append({
                "trab_id": trab_id,
                "nombre":  dict(trab)["nombre"],
                "cargo":   dict(trab)["cargo"],
                "status":  "duplicate",
                "mensaje": f"Ya registrado en {otm_id} hoy a las {dict(existente)['hora'][:5]}"
            })
            continue

        # Insertar registro — HH vacío, se calcula al final del día
        try:
            await database.execute(
                """INSERT INTO registros (trab_id, otm_id, supervisor_id, fecha, hora)
                   VALUES (:trab_id, :otm_id, :supervisor_id, :fecha, :hora)""",
                {
                    "trab_id":       trab_id,
                    "otm_id":        otm_id,
                    "supervisor_id": supervisor_id,
                    "fecha":         fecha,
                    "hora":          hora,
                }
            )
            resultados.append({
                "trab_id": trab_id,
                "nombre":  dict(trab)["nombre"],
                "cargo":   dict(trab)["cargo"],
                "status":  "ok"
            })
        except Exception as e:
            print(f"[ERROR] INSERT trab_id={trab_id} otm={otm_id} sup={supervisor_id}: {str(e)}")
            resultados.append({
                "trab_id": trab_id, "nombre": nombre, "cargo": cargo,
                "status": "error", "mensaje": str(e)
            })

    ok  = len([r for r in resultados if r["status"] == "ok"])
    dup = len([r for r in resultados if r["status"] == "duplicate"])
    err = len([r for r in resultados if r["status"] == "error"])

    return {
        "status":     "ok",
        "nuevos":     ok,
        "duplicados": dup,
        "errores":    err,
        "resultados": resultados
    }

# ── REGISTROS DEL DÍA ────────────────────────────────────────
@app.get("/api/registros/hoy")
async def registros_hoy():
    rows = await database.fetch_all(
        """SELECT r.trab_id, t.nombre, t.cargo,
                  r.otm_id, r.hora::text, r.supervisor_id, r.hh
           FROM registros r
           JOIN trabajadores t ON r.trab_id = t.id
           WHERE r.fecha = CURRENT_DATE
           ORDER BY r.trab_id, r.hora"""
    )
    return [dict(r) for r in rows]

# ── CALCULAR Y GUARDAR HH DEL DÍA ────────────────────────────
# Este endpoint es llamado por n8n a las 5:30pm
@app.post("/api/calcular-hh")
async def calcular_hh(data: dict = {}):
    fecha_raw = data.get("fecha", date.today().isoformat())
    fecha_str = fecha_raw if isinstance(fecha_raw, str) else fecha_raw.isoformat()
    fecha_obj = date.fromisoformat(fecha_str)

    # HH totales según día de semana (0=Lunes, 2=Miércoles)
    HH_DIA = {0: 9.5, 1: 9.5, 2: 10.0, 3: 9.5, 4: 9.5}
    total_hh = HH_DIA.get(fecha_obj.weekday(), 9.5)

    # Obtener todos los registros del día ordenados por trabajador y hora
    rows = await database.fetch_all(
        """SELECT trab_id, otm_id, hora::text as hora, supervisor_id
           FROM registros
           WHERE fecha = :fecha
           ORDER BY trab_id, hora""",
        {"fecha": fecha_str}
    )

    # Agrupar por trabajador
    por_trabajador = {}
    for r in rows:
        d = dict(r)
        tid = d["trab_id"]
        if tid not in por_trabajador:
            por_trabajador[tid] = []
        por_trabajador[tid].append(d)

    INICIO_TURNO = "06:30:00"
    actualizados = 0

    for trab_id, registros in por_trabajador.items():
        n = len(registros)

        if n == 1:
            # Un solo OTM → recibe todas las HH del día
            await database.execute(
                """UPDATE registros SET hh = :hh
                   WHERE trab_id = :tid AND fecha = :fecha AND otm_id = :otm""",
                {"hh": total_hh, "tid": trab_id, "fecha": fecha_str, "otm": registros[0]["otm_id"]}
            )
            actualizados += 1
        else:
            # Múltiples OTMs → calcular por tramos
            hh_acumulado = 0.0

            for i, reg in enumerate(registros):
                if i == 0:
                    # Primer tramo: desde inicio del turno hasta primer cambio de OTM
                    t_inicio = datetime.strptime(f"{fecha_str} {INICIO_TURNO}", "%Y-%m-%d %H:%M:%S")
                    t_fin    = datetime.strptime(f"{fecha_str} {reg['hora']}", "%Y-%m-%d %H:%M:%S")
                    # El primer registro ES cuando llegó, así que su tramo va hasta el segundo registro
                    continue

                # Tramo i: desde el registro anterior hasta este
                t_anterior = datetime.strptime(f"{fecha_str} {registros[i-1]['hora']}", "%Y-%m-%d %H:%M:%S")
                t_actual   = datetime.strptime(f"{fecha_str} {reg['hora']}", "%Y-%m-%d %H:%M:%S")

                if i == 1:
                    # Primer tramo real: desde inicio del turno hasta el segundo registro
                    t_inicio_turno = datetime.strptime(f"{fecha_str} {INICIO_TURNO}", "%Y-%m-%d %H:%M:%S")
                    diff_horas = (t_anterior - t_inicio_turno).total_seconds() / 3600
                else:
                    diff_horas = (t_anterior - datetime.strptime(
                        f"{fecha_str} {registros[i-2]['hora']}", "%Y-%m-%d %H:%M:%S"
                    )).total_seconds() / 3600

                # Redondear a 0.5H (no es el último tramo)
                hh_tramo = round(diff_horas * 2) / 2
                hh_acumulado += hh_tramo

                otm_anterior = registros[i-1]["otm_id"]
                await database.execute(
                    """UPDATE registros SET hh = :hh
                       WHERE trab_id = :tid AND fecha = :fecha AND otm_id = :otm""",
                    {"hh": hh_tramo, "tid": trab_id, "fecha": fecha_str, "otm": otm_anterior}
                )
                actualizados += 1

            # Último tramo: absorbe el restante exacto
            hh_restante = round(total_hh - hh_acumulado, 1)
            ultimo_otm  = registros[-1]["otm_id"]
            await database.execute(
                """UPDATE registros SET hh = :hh
                   WHERE trab_id = :tid AND fecha = :fecha AND otm_id = :otm""",
                {"hh": hh_restante, "tid": trab_id, "fecha": fecha_str, "otm": ultimo_otm}
            )
            actualizados += 1

    return {
        "status": "ok",
        "fecha": fecha_str,
        "total_hh_dia": total_hh,
        "trabajadores_procesados": len(por_trabajador),
        "registros_actualizados": actualizados
    }

# ── RESUMEN DEL DÍA (para Google Sheets / n8n) ───────────────
@app.get("/api/resumen/{fecha}")
async def resumen_dia(fecha: str):
    rows = await database.fetch_all(
        """SELECT r.trab_id, t.nombre, t.cargo,
                  r.otm_id, o.centro_costo, r.hora::text, r.hh,
                  r.supervisor_id, s.nombre as supervisor_nombre
           FROM registros r
           JOIN trabajadores t ON r.trab_id = t.id
           JOIN otms o         ON r.otm_id  = o.id
           JOIN supervisores s ON r.supervisor_id = s.id
           WHERE r.fecha = :fecha
           ORDER BY r.otm_id, t.nombre""",
        {"fecha": fecha}
    )
    return [dict(r) for r in rows]

# ── CONTEO PARA ALMUERZOS (para email de las 8am) ────────────
@app.get("/api/almuerzos/{fecha}")
async def almuerzos(fecha: str):
    rows = await database.fetch_all(
        """SELECT r.otm_id, t.nombre, t.cargo, r.supervisor_id
           FROM registros r
           JOIN trabajadores t ON r.trab_id = t.id
           WHERE r.fecha = :fecha
           ORDER BY r.otm_id, t.nombre""",
        {"fecha": fecha}
    )
    data = [dict(r) for r in rows]

    # Deduplicar por trabajador (puede estar en 2 OTMs pero solo 1 almuerzo)
    vistos = set()
    total_unicos = 0
    por_otm = {}

    for r in data:
        if r["trab_id"] not in vistos:
            vistos.add(r["trab_id"] if "trab_id" in r else r["nombre"])
            total_unicos += 1
        otm = r["otm_id"]
        if otm not in por_otm:
            por_otm[otm] = []
        por_otm[otm].append(r)

    return {
        "fecha": fecha,
        "total_almuerzos": total_unicos,
        "por_otm": por_otm
    }

# ── ADMIN: CREAR TRABAJADOR ───────────────────────────────────
@app.post("/admin/trabajador")
async def crear_trabajador(data: dict):
    nombre = data.get("nombre", "").strip().upper()
    cargo  = data.get("cargo",  "").strip().upper()
    dni    = data.get("dni",    "").strip()

    if not nombre or not cargo:
        raise HTTPException(400, "Nombre y cargo son requeridos")

    # Siguiente ID disponible
    row = await database.fetch_one(
        "SELECT MAX(CAST(id AS INTEGER)) as max_id FROM trabajadores"
    )
    next_id = str((row["max_id"] or 0) + 1).zfill(3)

    await database.execute(
        "INSERT INTO trabajadores (id, nombre, cargo, dni) VALUES (:id, :nombre, :cargo, :dni)",
        {"id": next_id, "nombre": nombre, "cargo": cargo, "dni": dni}
    )

    return {"status": "ok", "id": next_id, "nombre": nombre, "cargo": cargo}

# ── ADMIN: LISTAR TRABAJADORES ────────────────────────────────
@app.get("/admin/trabajadores")
async def listar_trabajadores():
    rows = await database.fetch_all(
        "SELECT id, nombre, cargo, activo FROM trabajadores ORDER BY CAST(id AS INTEGER)"
    )
    return [dict(r) for r in rows]

# ── ADMIN: DAR DE BAJA ────────────────────────────────────────
@app.put("/admin/trabajador/{trab_id}/baja")
async def dar_baja(trab_id: str):
    await database.execute(
        "UPDATE trabajadores SET activo = false WHERE id = :id",
        {"id": trab_id.zfill(3)}
    )
    return {"status": "ok"}

# ── ADMIN: AGREGAR OTM ────────────────────────────────────────
@app.post("/admin/otm")
async def crear_otm(data: dict):
    otm_id      = data.get("id", "").strip().upper()
    descripcion = data.get("descripcion", "").strip().upper()
    area        = data.get("area", "").strip()
    estado      = data.get("estado", "POR INICIAR").strip()
    sdp         = data.get("sdp", "").strip()
    cc          = data.get("centro_costo", "").strip()

    if not otm_id or not descripcion:
        raise HTTPException(400, "ID y descripción son requeridos")

    await database.execute(
        """INSERT INTO otms (id, sdp, descripcion, centro_costo, area, estado)
           VALUES (:id, :sdp, :descripcion, :cc, :area, :estado)
           ON CONFLICT (id) DO UPDATE SET estado = EXCLUDED.estado""",
        {"id": otm_id, "sdp": sdp, "descripcion": descripcion, "cc": cc, "area": area, "estado": estado}
    )
    return {"status": "ok", "id": otm_id}

# ── ADMIN: ACTUALIZAR ESTADO OTM ─────────────────────────────
@app.put("/admin/otm/{otm_id}/estado")
async def actualizar_estado_otm(otm_id: str, data: dict):
    estado = data.get("estado", "").strip()
    if estado not in ["EJECUCION", "POR INICIAR", "CERRADO", "CONCLUIDO", "STAND BY"]:
        raise HTTPException(400, f"Estado inválido: {estado}")
    await database.execute(
        "UPDATE otms SET estado = :estado WHERE id = :id",
        {"estado": estado, "id": otm_id}
    )
    return {"status": "ok"}
