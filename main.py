from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import database
from datetime import date, datetime
from typing import Optional

app = FastAPI(title="Kampfer Tareo API", version="1.2.0")

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
    return {"status": "ok", "version": "1.2.0"}

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
    fecha_str         = fecha_raw if isinstance(fecha_raw, str) else fecha_raw.isoformat()
    trabajadores_list = data.get("trabajadores", [])

    if not supervisor_id or not otm_id or not trabajadores_list:
        raise HTTPException(400, "Faltan campos: supervisor_id, otm_id, trabajadores[]")

    # Verificar supervisor
    sup = await database.fetch_one(
        "SELECT id FROM supervisores WHERE id = :id",
        {"id": supervisor_id}
    )
    if not sup:
        raise HTTPException(400, f"Supervisor '{supervisor_id}' no encontrado")

    # Verificar OTM
    otm = await database.fetch_one(
        "SELECT id FROM otms WHERE id = :id",
        {"id": otm_id}
    )
    if not otm:
        raise HTTPException(400, f"OTM '{otm_id}' no encontrada")

    resultados = []

    for t in trabajadores_list:
        trab_id  = str(t.get("trab_id", "")).zfill(3)
        hora_raw = t.get("hora", datetime.now().strftime("%H:%M:%S"))
        hora     = hora_raw[:8] if len(hora_raw) >= 8 else hora_raw + ":00"
        nombre   = t.get("nombre", "")
        cargo    = t.get("cargo",  "")

        # Verificar trabajador
        trab = await database.fetch_one(
            "SELECT id, nombre, cargo FROM trabajadores WHERE id = :id AND activo = true",
            {"id": trab_id}
        )
        if not trab:
            resultados.append({
                "trab_id": trab_id, "nombre": nombre, "cargo": cargo,
                "status": "error", "mensaje": f"ID {trab_id} no encontrado o inactivo"
            })
            continue

        trab_dict = dict(trab)

        # Verificar duplicado usando SQL puro sin pasar fecha como parámetro tipado
        # Concatenamos la fecha directamente en el SQL para evitar el problema de tipo
        check_sql = f"""
            SELECT id FROM registros
            WHERE trab_id = :trab_id
              AND otm_id  = :otm_id
              AND fecha   = '{fecha_str}'::date
        """
        existente = await database.fetch_one(
            check_sql,
            {"trab_id": trab_id, "otm_id": otm_id}
        )

        if existente:
            resultados.append({
                "trab_id": trab_id,
                "nombre":  trab_dict["nombre"],
                "cargo":   trab_dict["cargo"],
                "status":  "duplicate",
                "mensaje": f"Ya registrado en {otm_id} hoy"
            })
            continue

        # Insertar — fecha y hora como literales SQL para evitar problema de tipo asyncpg
        try:
            insert_sql = f"""
                INSERT INTO registros (trab_id, otm_id, supervisor_id, fecha, hora)
                VALUES (:trab_id, :otm_id, :supervisor_id, '{fecha_str}'::date, '{hora}'::time)
            """
            await database.execute(
                insert_sql,
                {
                    "trab_id":       trab_id,
                    "otm_id":        otm_id,
                    "supervisor_id": supervisor_id,
                }
            )
            resultados.append({
                "trab_id": trab_id,
                "nombre":  trab_dict["nombre"],
                "cargo":   trab_dict["cargo"],
                "status":  "ok"
            })
        except Exception as e:
            print(f"[ERROR] INSERT trab={trab_id} otm={otm_id}: {str(e)}")
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

# ── REGISTROS DEL DÍA ─────────────────────────────────────────
@app.get("/api/registros/hoy")
async def registros_hoy():
    hoy = date.today().isoformat()
    rows = await database.fetch_all(
        f"""SELECT r.trab_id, t.nombre, t.cargo,
                   r.otm_id, r.hora::text, r.supervisor_id, r.hh
            FROM registros r
            JOIN trabajadores t ON r.trab_id = t.id
            WHERE r.fecha = '{hoy}'::date
            ORDER BY r.trab_id, r.hora"""
    )
    return [dict(r) for r in rows]

# ── REGISTROS POR FECHA ───────────────────────────────────────
@app.get("/api/registros/{fecha}")
async def registros_por_fecha(fecha: str):
    rows = await database.fetch_all(
        f"""SELECT r.trab_id, t.nombre, t.cargo,
                   r.otm_id, r.hora::text, r.supervisor_id, r.hh
            FROM registros r
            JOIN trabajadores t ON r.trab_id = t.id
            WHERE r.fecha = '{fecha}'::date
            ORDER BY r.trab_id, r.hora"""
    )
    return [dict(r) for r in rows]

# ── CALCULAR HH DEL DÍA (llamado por n8n a las 5:30pm) ───────
@app.post("/api/calcular-hh")
async def calcular_hh(data: dict = {}):
    fecha_str = data.get("fecha", date.today().isoformat())

    # HH totales según día de semana
    fecha_obj = date.fromisoformat(fecha_str)
    HH_DIA    = {0: 9.5, 1: 9.5, 2: 10.0, 3: 9.5, 4: 9.5}
    total_hh  = HH_DIA.get(fecha_obj.weekday(), 9.5)

    # Registros del día ordenados por trabajador y hora
    rows = await database.fetch_all(
        f"""SELECT trab_id, otm_id, hora::text as hora
            FROM registros
            WHERE fecha = '{fecha_str}'::date
            ORDER BY trab_id, hora"""
    )

    # Agrupar por trabajador
    por_trabajador = {}
    for r in rows:
        d   = dict(r)
        tid = d["trab_id"]
        if tid not in por_trabajador:
            por_trabajador[tid] = []
        por_trabajador[tid].append(d)

    INICIO_TURNO = "06:30:00"
    actualizados = 0

    for trab_id, registros in por_trabajador.items():
        n = len(registros)

        if n == 1:
            # Una sola OTM → todas las HH del día
            await database.execute(
                f"""UPDATE registros SET hh = {total_hh}
                    WHERE trab_id = :tid
                      AND fecha   = '{fecha_str}'::date
                      AND otm_id  = :otm""",
                {"tid": trab_id, "otm": registros[0]["otm_id"]}
            )
            actualizados += 1
        else:
            # Múltiples OTMs → calcular por tramos
            hh_acumulado = 0.0
            t_inicio_turno = datetime.strptime(f"{fecha_str} {INICIO_TURNO}", "%Y-%m-%d %H:%M:%S")

            for i in range(len(registros)):
                if i < len(registros) - 1:
                    # Tramos intermedios: desde hora de registro hasta el siguiente
                    if i == 0:
                        t_desde = t_inicio_turno
                    else:
                        t_desde = datetime.strptime(
                            f"{fecha_str} {registros[i-1]['hora']}", "%Y-%m-%d %H:%M:%S"
                        )
                    t_hasta = datetime.strptime(
                        f"{fecha_str} {registros[i]['hora']}", "%Y-%m-%d %H:%M:%S"
                    )
                    diff_h  = (t_hasta - t_desde).total_seconds() / 3600
                    # Redondear a 0.5H
                    hh_tramo = round(diff_h * 2) / 2
                    hh_acumulado += hh_tramo

                    await database.execute(
                        f"""UPDATE registros SET hh = {hh_tramo}
                            WHERE trab_id = :tid
                              AND fecha   = '{fecha_str}'::date
                              AND otm_id  = :otm""",
                        {"tid": trab_id, "otm": registros[i]["otm_id"]}
                    )
                    actualizados += 1
                else:
                    # Último tramo: absorbe el restante exacto
                    hh_restante = round(total_hh - hh_acumulado, 1)
                    await database.execute(
                        f"""UPDATE registros SET hh = {hh_restante}
                            WHERE trab_id = :tid
                              AND fecha   = '{fecha_str}'::date
                              AND otm_id  = :otm""",
                        {"tid": trab_id, "otm": registros[i]["otm_id"]}
                    )
                    actualizados += 1

    return {
        "status":                 "ok",
        "fecha":                  fecha_str,
        "total_hh_dia":           total_hh,
        "trabajadores_procesados": len(por_trabajador),
        "registros_actualizados":  actualizados
    }

# ── RESUMEN DÍA para Google Sheets via n8n ───────────────────
@app.get("/api/resumen/{fecha}")
async def resumen_dia(fecha: str):
    rows = await database.fetch_all(
        f"""SELECT r.trab_id, t.nombre, t.cargo,
                   r.otm_id, o.centro_costo,
                   r.hora::text, r.hh,
                   r.supervisor_id, s.nombre as supervisor_nombre
            FROM registros r
            JOIN trabajadores t ON r.trab_id = t.id
            JOIN otms o         ON r.otm_id  = o.id
            JOIN supervisores s ON r.supervisor_id = s.id
            WHERE r.fecha = '{fecha}'::date
            ORDER BY r.otm_id, t.nombre"""
    )
    return [dict(r) for r in rows]

# ── CONTEO ALMUERZOS para email 8am ──────────────────────────
@app.get("/api/almuerzos/{fecha}")
async def almuerzos(fecha: str):
    rows = await database.fetch_all(
        f"""SELECT DISTINCT ON (r.trab_id)
                   r.trab_id, t.nombre, t.cargo,
                   r.otm_id, r.supervisor_id
            FROM registros r
            JOIN trabajadores t ON r.trab_id = t.id
            WHERE r.fecha = '{fecha}'::date
            ORDER BY r.trab_id, r.hora"""
    )
    data = [dict(r) for r in rows]

    # Agrupar por OTM para el email
    por_otm = {}
    for r in data:
        otm = r["otm_id"]
        if otm not in por_otm:
            por_otm[otm] = []
        por_otm[otm].append(r)

    return {
        "fecha":           fecha,
        "total_almuerzos": len(data),
        "por_otm":         por_otm
    }

# ── ADMIN: CREAR TRABAJADOR ───────────────────────────────────
@app.post("/admin/trabajador")
async def crear_trabajador(data: dict):
    nombre = data.get("nombre", "").strip().upper()
    cargo  = data.get("cargo",  "").strip().upper()
    dni    = data.get("dni",    "").strip()

    if not nombre or not cargo:
        raise HTTPException(400, "Nombre y cargo son requeridos")

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

# ── ADMIN: AGREGAR / ACTUALIZAR OTM ──────────────────────────
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
           VALUES (:id, :sdp, :desc, :cc, :area, :estado)
           ON CONFLICT (id) DO UPDATE SET
             estado = EXCLUDED.estado,
             descripcion = EXCLUDED.descripcion""",
        {"id": otm_id, "sdp": sdp, "desc": descripcion, "cc": cc,
         "area": area, "estado": estado}
    )
    return {"status": "ok", "id": otm_id}

# ── ADMIN: CAMBIAR ESTADO OTM ─────────────────────────────────
@app.put("/admin/otm/{otm_id}/estado")
async def actualizar_estado_otm(otm_id: str, data: dict):
    estado = data.get("estado", "").strip()
    validos = ["EJECUCION", "POR INICIAR", "CERRADO", "CONCLUIDO", "STAND BY"]
    if estado not in validos:
        raise HTTPException(400, f"Estado inválido. Válidos: {validos}")
    await database.execute(
        "UPDATE otms SET estado = :estado WHERE id = :id",
        {"estado": estado, "id": otm_id}
    )
    return {"status": "ok"}
