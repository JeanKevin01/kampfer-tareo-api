from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import database
from datetime import date, datetime, timezone, timedelta
from typing import Optional
from routers.valor_ganado import router as ev_router

# Zona horaria de Peru (UTC-5)
LIMA = timezone(timedelta(hours=-5))
def ahora_lima(): return datetime.now(LIMA)
def fecha_lima(): return ahora_lima().date()
def hora_lima(): return ahora_lima().strftime("%H:%M:%S")

app = FastAPI(title="Kampfer Tareo API", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ev_router)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


# ── SESIONES (Session-First model) ───────────────────────────

@app.post("/api/sesion")
async def crear_sesion(data: dict):
    supervisor_id = str(data.get("supervisor_id", "")).strip()
    otm_id        = str(data.get("otm_id", "")).strip()
    fecha_str     = str(data.get("fecha", fecha_lima().isoformat()))
    hh_turno      = float(data.get("hh_turno", 9.5))
    if not supervisor_id or not otm_id:
        raise HTTPException(400, "supervisor_id y otm_id son requeridos")
    row = await database.fetch_one(
        f"INSERT INTO sesiones (supervisor_id, otm_id, fecha, hh_turno) "
        f"VALUES (:sup, :otm, '{fecha_str}'::date, :hh) RETURNING id",
        {"sup": supervisor_id, "otm": otm_id, "hh": hh_turno}
    )
    return {"id": row["id"], "ok": True}


@app.get("/api/sesion/hoy/{supervisor_id}")
async def sesiones_hoy(supervisor_id: str):
    fecha_str = fecha_lima().isoformat()
    sesiones = await database.fetch_all(
        f"SELECT s.id, s.supervisor_id, s.otm_id, s.estado, "
        f"       s.hh_turno, s.created_at, "
        f"       COUNT(st.id) AS total, "
        f"       COUNT(st.id) FILTER (WHERE st.presente) AS presentes "
        f"FROM sesiones s "
        f"LEFT JOIN sesion_trabajadores st ON st.sesion_id = s.id "
        f"WHERE s.supervisor_id = :sup AND s.fecha = '{fecha_str}'::date "
        f"GROUP BY s.id ORDER BY s.created_at DESC",
        {"sup": supervisor_id}
    )
    result = []
    for s in sesiones:
        d = dict(s)
        trabs = await database.fetch_all(
            "SELECT st.trab_id, st.presente, st.hh_override, st.agregado_via, "
            "       t.nombre, t.cargo "
            "FROM sesion_trabajadores st "
            "JOIN trabajadores t ON t.id = st.trab_id "
            "WHERE st.sesion_id = :sid ORDER BY t.nombre",
            {"sid": d["id"]}
        )
        d["trabajadores"] = [dict(t) for t in trabs]
        result.append(d)
    return result


@app.post("/api/sesion/{sesion_id}/enviar")
async def enviar_sesion(sesion_id: int, data: dict):
    sesion = await database.fetch_one(
        "SELECT * FROM sesiones WHERE id = :id AND estado = 'borrador'",
        {"id": sesion_id}
    )
    if not sesion:
        raise HTTPException(404, "Sesión no encontrada o ya enviada")
    sesion      = dict(sesion)
    fecha_obj   = sesion["fecha"]
    fecha_str   = fecha_obj.isoformat() if hasattr(fecha_obj, "isoformat") else str(fecha_obj)
    hora        = hora_lima()
    trabajadores = data.get("trabajadores", [])

    # Limpiar y volver a insertar trabajadores
    await database.execute(
        "DELETE FROM sesion_trabajadores WHERE sesion_id = :sid",
        {"sid": sesion_id}
    )
    for t in trabajadores:
        hh_ov = t.get("hh_override")
        await database.execute(
            "INSERT INTO sesion_trabajadores "
            "(sesion_id, trab_id, presente, hh_override, agregado_via) "
            "VALUES (:sid, :tid, :pres, :hh, :via) "
            "ON CONFLICT (sesion_id, trab_id) DO UPDATE "
            "SET presente = :pres, hh_override = :hh",
            {"sid": sesion_id, "tid": t["trab_id"],
             "pres": t.get("presente", True),
             "hh": hh_ov, "via": t.get("agregado_via", "busqueda")}
        )

    # Crear registros de tareo para los presentes
    enviados = 0
    for t in trabajadores:
        if not t.get("presente", True):
            continue
        hh = t.get("hh_override") or float(sesion["hh_turno"])
        try:
            await database.execute(
                f"INSERT INTO registros "
                f"(trab_id, otm_id, supervisor_id, fecha, hora, hh) "
                f"VALUES (:tid, :otm, :sup, '{fecha_str}'::date, '{hora}'::time, :hh) "
                f"ON CONFLICT (trab_id, otm_id, fecha) "
                f"DO UPDATE SET hh = :hh, hora = '{hora}'::time",
                {"tid": t["trab_id"], "otm": sesion["otm_id"],
                 "sup": sesion["supervisor_id"], "hh": hh}
            )
            enviados += 1
        except Exception as e:
            print(f"[SESION] Error registro trab={t['trab_id']}: {e}")

    await database.execute(
        "UPDATE sesiones SET estado = 'enviada', enviada_at = now() WHERE id = :id",
        {"id": sesion_id}
    )
    return {"ok": True, "enviados": enviados}


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


# ── ADMIN: SUPERVISORES (CRUD) ────────────────────────────────
@app.get("/admin/supervisores")
async def listar_supervisores_admin():
    rows = await database.fetch_all(
        "SELECT id, nombre, email, activo FROM supervisores ORDER BY CAST(id AS INTEGER)"
    )
    return [dict(r) for r in rows]


@app.post("/admin/supervisor")
async def crear_supervisor(data: dict):
    nombre = data.get("nombre", "").strip().upper()
    email  = data.get("email",  "").strip()

    if not nombre:
        raise HTTPException(400, "Nombre es requerido")

    row = await database.fetch_one(
        "SELECT MAX(CAST(id AS INTEGER)) as max_id FROM supervisores"
    )
    next_id = str((row["max_id"] or 0) + 1).zfill(2)

    await database.execute(
        "INSERT INTO supervisores (id, nombre, email, activo) "
        "VALUES (:id, :nombre, :email, true)",
        {"id": next_id, "nombre": nombre, "email": email}
    )
    return {"status": "ok", "id": next_id, "nombre": nombre}


@app.put("/admin/supervisor/{sup_id}")
async def editar_supervisor(sup_id: str, data: dict):
    row = await database.fetch_one(
        "SELECT id FROM supervisores WHERE id = :id", {"id": sup_id}
    )
    if not row:
        raise HTTPException(404, "Supervisor no encontrado")
    await database.execute(
        "UPDATE supervisores SET nombre = :nombre, email = :email WHERE id = :id",
        {
            "id": sup_id,
            "nombre": data.get("nombre", "").upper().strip(),
            "email":  data.get("email", "").strip(),
        }
    )
    updated = await database.fetch_one(
        "SELECT id, nombre, email FROM supervisores WHERE id = :id", {"id": sup_id}
    )
    return dict(updated)


@app.put("/admin/supervisor/{sup_id}/baja")
async def dar_baja_supervisor(sup_id: str):
    await database.execute(
        "UPDATE supervisores SET activo = false WHERE id = :id", {"id": sup_id}
    )
    return {"status": "ok"}

# ── OTMs ─────────────────────────────────────────────────────
@app.get("/api/otms")
async def get_otms(activas: bool = False):
    # activas=true (app móvil) -> solo OTMs en EJECUCION.
    # Caso general (panel) -> TODAS las OTMs, sin filtrar por estado, porque el
    # vocabulario real de estados es abierto (ej. 'CULMINADO', 'GENERAR NUEVO SDP')
    # y no queremos ocultar OTMs por un estado no previsto.
    where = "WHERE estado = 'EJECUCION'" if activas else ""
    rows = await database.fetch_all(
        f"SELECT id, descripcion, area, estado, centro_costo, sdp, plazo, "
        f"       fecha_inicio, fecha_fin, monto_contractual, monto_valorizado "
        f"FROM otms {where} ORDER BY id"
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
    fecha_raw         = data.get("fecha", fecha_lima().isoformat())
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
        hora_raw = t.get("hora", hora_lima())
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



@app.post("/api/sesion/enviar-directo")
async def enviar_directo(data: dict):
    """Crea la sesión y crea los registros en un solo paso (sin borrador)."""
    supervisor_id = str(data.get("supervisor_id", "")).strip()
    otm_id        = str(data.get("otm_id", "")).strip()
    fecha_str     = str(data.get("fecha", fecha_lima().isoformat()))
    hh_turno      = float(data.get("hh_turno", 9.5))
    trabajadores  = data.get("trabajadores", [])
    if not supervisor_id or not otm_id:
        raise HTTPException(400, "supervisor_id y otm_id son requeridos")
    if not trabajadores:
        raise HTTPException(400, "La lista de trabajadores está vacía")
    hora = hora_lima()
    row = await database.fetch_one(
        f"INSERT INTO sesiones "
        f"(supervisor_id, otm_id, fecha, hh_turno, estado, enviada_at) "
        f"VALUES (:sup, :otm, '{fecha_str}'::date, :hh, 'enviada', now()) RETURNING id",
        {"sup": supervisor_id, "otm": otm_id, "hh": hh_turno}
    )
    sesion_id = row["id"]
    enviados = 0
    for t in trabajadores:
        trab_id = str(t.get("trab_id", "")).zfill(3)
        hh_ov   = t.get("hh_override")
        via     = t.get("agregado_via", "busqueda")
        await database.execute(
            "INSERT INTO sesion_trabajadores "
            "(sesion_id, trab_id, presente, hh_override, agregado_via) "
            "VALUES (:sid, :tid, true, :hh, :via)",
            {"sid": sesion_id, "tid": trab_id, "hh": hh_ov, "via": via}
        )
        hh_real = float(hh_ov) if hh_ov is not None else hh_turno
        await database.execute(
            f"INSERT INTO registros "
            f"(trab_id, otm_id, supervisor_id, fecha, hora, hh) "
            f"VALUES (:tid, :otm, :sup, '{fecha_str}'::date, '{hora}'::time, :hh) "
            f"ON CONFLICT (trab_id, otm_id, fecha) DO UPDATE SET hh=:hh, hora='{hora}'::time",
            {"tid": trab_id, "otm": otm_id, "sup": supervisor_id, "hh": hh_real}
        )
        enviados += 1
    return {"ok": True, "enviados": enviados, "sesion_id": sesion_id}

# ── CUADRILLAS PRE-ESTABLECIDAS ───────────────────────────────

@app.get("/api/cuadrilla/{supervisor_id}")
async def get_cuadrilla(supervisor_id: str):
    rows = await database.fetch_all(
        "SELECT c.trab_id, t.nombre, t.cargo "
        "FROM cuadrillas c "
        "JOIN trabajadores t ON t.id = c.trab_id "
        "WHERE c.supervisor_id = :sup AND t.activo = true "
        "ORDER BY t.nombre",
        {"sup": supervisor_id}
    )
    return [dict(r) for r in rows]


@app.post("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def agregar_cuadrilla(supervisor_id: str, trab_id: str):
    await database.execute(
        "INSERT INTO cuadrillas (supervisor_id, trab_id) "
        "VALUES (:sup, :tid) ON CONFLICT DO NOTHING",
        {"sup": supervisor_id, "tid": trab_id.zfill(3)}
    )
    return {"ok": True}


@app.delete("/api/cuadrilla/{supervisor_id}/{trab_id}")
async def quitar_cuadrilla(supervisor_id: str, trab_id: str):
    await database.execute(
        "DELETE FROM cuadrillas WHERE supervisor_id = :sup AND trab_id = :tid",
        {"sup": supervisor_id, "tid": trab_id.zfill(3)}
    )
    return {"ok": True}


# ── REGISTROS DEL DÍA ─────────────────────────────────────────
@app.get("/api/registros/hoy")
async def registros_hoy():
    hoy = fecha_lima().isoformat()
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
    fecha_str = data.get("fecha", fecha_lima().isoformat())

    # HH totales según día de semana
    fecha_obj = date.fromisoformat(fecha_str)
    HH_DIA    = {0: 9.5, 1: 9.5, 2: 10.0, 3: 9.5, 4: 9.5}
    total_hh  = HH_DIA.get(fecha_obj.weekday(), 9.5)

    # Registros del día ordenados por trabajador y hora
    rows = await database.fetch_all(
        f"""SELECT trab_id, otm_id, hora::text as hora
            FROM registros
            WHERE fecha = '{fecha_str}'::date
              AND hh IS NULL
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
    tipo   = data.get("tipo",   "").strip().upper()

    if not nombre or not cargo:
        raise HTTPException(400, "Nombre y cargo son requeridos")

    if tipo not in ("DIRECTO", "INDIRECTO"):
        tipo = "DIRECTO"

    row = await database.fetch_one(
        "SELECT MAX(CAST(id AS INTEGER)) as max_id FROM trabajadores"
    )
    next_id = str((row["max_id"] or 0) + 1).zfill(3)

    await database.execute(
        "INSERT INTO trabajadores (id, nombre, cargo, dni, tipo) "
        "VALUES (:id, :nombre, :cargo, :dni, :tipo)",
        {"id": next_id, "nombre": nombre, "cargo": cargo, "dni": dni, "tipo": tipo}
    )
    return {"status": "ok", "id": next_id, "nombre": nombre, "cargo": cargo, "tipo": tipo}

# ── ADMIN: LISTAR TRABAJADORES ────────────────────────────────
@app.get("/admin/trabajadores")
async def listar_trabajadores():
    rows = await database.fetch_all(
        "SELECT id, nombre, cargo, dni, COALESCE(tipo,'DIRECTO') AS tipo, activo "
        "FROM trabajadores ORDER BY CAST(id AS INTEGER)"
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
    plazo       = data.get("plazo") or None
    f_inicio    = data.get("fecha_inicio") or None
    f_fin       = data.get("fecha_fin") or None
    monto_c     = data.get("monto_contractual") or None
    monto_v     = data.get("monto_valorizado") or 0

    if not otm_id or not descripcion:
        raise HTTPException(400, "ID y descripción son requeridos")

    await database.execute(
        """INSERT INTO otms (id, sdp, descripcion, centro_costo, area, estado,
                              plazo, fecha_inicio, fecha_fin, monto_contractual, monto_valorizado)
           VALUES (:id, :sdp, :desc, :cc, :area, :estado,
                   :plazo, :f_inicio, :f_fin, :monto_c, :monto_v)
           ON CONFLICT (id) DO UPDATE SET
             estado = EXCLUDED.estado,
             descripcion = EXCLUDED.descripcion,
             area = EXCLUDED.area,
             sdp = EXCLUDED.sdp,
             centro_costo = EXCLUDED.centro_costo,
             plazo = COALESCE(EXCLUDED.plazo, otms.plazo),
             fecha_inicio = COALESCE(EXCLUDED.fecha_inicio, otms.fecha_inicio),
             fecha_fin = COALESCE(EXCLUDED.fecha_fin, otms.fecha_fin),
             monto_contractual = COALESCE(EXCLUDED.monto_contractual, otms.monto_contractual),
             monto_valorizado = EXCLUDED.monto_valorizado""",
        {"id": otm_id, "sdp": sdp, "desc": descripcion, "cc": cc,
         "area": area, "estado": estado, "plazo": plazo,
         "f_inicio": f_inicio, "f_fin": f_fin,
         "monto_c": monto_c, "monto_v": monto_v}
    )
    return {"status": "ok", "id": otm_id}


@app.post("/admin/otms/bulk")
async def crear_otms_bulk(data: dict):
    """Importación masiva de OTMs — recibe {otms: [{id,descripcion,area,estado,sdp,centro_costo,
    plazo,fecha_inicio,fecha_fin,monto_contractual,monto_valorizado}]}"""
    otms = data.get("otms", [])
    if not otms:
        raise HTTPException(400, "Lista de OTMs vacía")

    creadas, errores = [], []

    for o in otms:
        otm_id      = str(o.get("id", "")).strip().upper()
        descripcion = str(o.get("descripcion", "")).strip().upper()
        area        = str(o.get("area", "")).strip()
        # Se conserva el estado tal como viene (en mayúsculas). NO se fuerza a
        # 'POR INICIAR' si es desconocido: el vocabulario real es abierto
        # (ej. 'CULMINADO', 'GENERAR NUEVO SDP') y forzarlo falsearía el dato.
        estado      = str(o.get("estado", "")).strip().upper() or "POR INICIAR"
        sdp         = str(o.get("sdp", "")).strip()
        cc          = str(o.get("centro_costo", "")).strip()
        plazo       = o.get("plazo") or None
        f_inicio    = o.get("fecha_inicio") or None
        f_fin       = o.get("fecha_fin") or None
        monto_c     = o.get("monto_contractual") or None
        monto_v     = o.get("monto_valorizado") or 0

        if not otm_id or not descripcion:
            errores.append({"id": otm_id or "—", "error": "ID o descripción vacíos"})
            continue

        try:
            await database.execute(
                """INSERT INTO otms (id, sdp, descripcion, centro_costo, area, estado,
                                      plazo, fecha_inicio, fecha_fin, monto_contractual, monto_valorizado)
                   VALUES (:id, :sdp, :desc, :cc, :area, :estado,
                           :plazo, :f_inicio, :f_fin, :monto_c, :monto_v)
                   ON CONFLICT (id) DO UPDATE SET
                     descripcion = EXCLUDED.descripcion,
                     area = EXCLUDED.area,
                     estado = EXCLUDED.estado,
                     sdp = EXCLUDED.sdp,
                     centro_costo = EXCLUDED.centro_costo,
                     plazo = COALESCE(EXCLUDED.plazo, otms.plazo),
                     fecha_inicio = COALESCE(EXCLUDED.fecha_inicio, otms.fecha_inicio),
                     fecha_fin = COALESCE(EXCLUDED.fecha_fin, otms.fecha_fin),
                     monto_contractual = COALESCE(EXCLUDED.monto_contractual, otms.monto_contractual),
                     monto_valorizado = EXCLUDED.monto_valorizado""",
                {"id": otm_id, "sdp": sdp, "desc": descripcion, "cc": cc,
                 "area": area, "estado": estado, "plazo": plazo,
                 "f_inicio": f_inicio, "f_fin": f_fin,
                 "monto_c": monto_c, "monto_v": monto_v}
            )
            creadas.append(otm_id)
        except Exception as e:
            errores.append({"id": otm_id, "error": str(e)})

    return {"status": "ok", "creadas": len(creadas), "errores": errores}

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

# ── EDITAR TRABAJADOR ─────────────────────────────────────────
@app.put("/admin/trabajador/{trab_id}")
async def editar_trabajador(trab_id: str, data: dict):
    row = await database.fetch_one(
        "SELECT id FROM trabajadores WHERE id = :id",
        {"id": trab_id}
    )
    if not row:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    tipo = data.get("tipo", "").strip().upper()
    if tipo not in ("DIRECTO", "INDIRECTO"):
        tipo = None
    await database.execute(
        "UPDATE trabajadores SET nombre = :nombre, cargo = :cargo, dni = :dni"
        + (", tipo = :tipo" if tipo else "") +
        " WHERE id = :id",
        {
            "id":     trab_id,
            "nombre": data.get("nombre", "").upper().strip(),
            "cargo":  data.get("cargo",  "").upper().strip(),
            "dni":    data.get("dni",    ""),
            **({"tipo": tipo} if tipo else {}),
        }
    )
    updated = await database.fetch_one(
        "SELECT id, nombre, cargo, dni, COALESCE(tipo,'DIRECTO') AS tipo FROM trabajadores WHERE id = :id",
        {"id": trab_id}
    )
    return dict(updated)


# ═══════════════════════════════════════════════════════════════
# SPRINT 2: Control diario por partida
# ═══════════════════════════════════════════════════════════════

# ── PARTIDAS POR OTM (para la app móvil) ─────────────────────

@app.get("/api/partidas-otm/{otm_id}")
async def get_partidas_otm(otm_id: str):
    """Devuelve el árbol completo (nodos padre + hojas) de una OTM, para que
    la app muestre la jerarquía con color, pero solo las hojas (fase != null)
    son seleccionables para registrar tareo."""
    rows = await database.fetch_all(
        """SELECT p.id, p.codigo, p.descripcion, p.fase, p.sub_fase,
                  p.unidad, p.hh_presup, p.metrado_presup,
                  p.nivel, p.parent_codigo,
                  (p.fase IS NOT NULL) AS es_hoja
           FROM ev_partidas p
           WHERE p.otm_id = :otm AND p.activo = true
           ORDER BY p.codigo""",
        {"otm": otm_id}
    )
    return [dict(r) for r in rows]


@app.get("/api/partidas-otm/{otm_id}/hojas")
async def get_partidas_otm_hojas(otm_id: str):
    """Solo las partidas hoja — compat con clientes que no necesitan el árbol."""
    rows = await database.fetch_all(
        """SELECT p.id, p.codigo, p.descripcion, p.fase, p.sub_fase,
                  p.unidad, p.hh_presup, p.metrado_presup
           FROM ev_partidas p
           WHERE p.otm_id = :otm AND p.activo = true AND p.fase IS NOT NULL
           ORDER BY p.codigo""",
        {"otm": otm_id}
    )
    return [dict(r) for r in rows]


# ── CUADRILLA GRUPOS (múltiples por supervisor) ───────────────

@app.get("/api/cuadrillas/{supervisor_id}")
async def listar_cuadrilla_grupos(supervisor_id: str):
    """Lista todos los grupos de cuadrilla del supervisor con sus miembros."""
    grupos = await database.fetch_all(
        """SELECT g.id, g.nombre, g.activo, g.creado_en,
                  COUNT(m.trab_id) AS total
           FROM cuadrilla_grupos g
           LEFT JOIN cuadrilla_grupo_miembros m ON m.grupo_id = g.id
           WHERE g.supervisor_id = :sup AND g.activo = true
           GROUP BY g.id ORDER BY g.creado_en""",
        {"sup": supervisor_id}
    )
    result = []
    for g in grupos:
        gd = dict(g)
        miembros = await database.fetch_all(
            """SELECT m.trab_id, t.nombre, t.cargo
               FROM cuadrilla_grupo_miembros m
               JOIN trabajadores t ON t.id = m.trab_id AND t.activo = true
               WHERE m.grupo_id = :gid ORDER BY t.nombre""",
            {"gid": gd["id"]}
        )
        gd["miembros"] = [dict(m) for m in miembros]
        gd["total"]    = int(gd["total"])
        result.append(gd)
    return result


@app.post("/api/cuadrillas/{supervisor_id}")
async def crear_cuadrilla_grupo(supervisor_id: str, data: dict):
    """Crea un nuevo grupo de cuadrilla con su lista de miembros."""
    nombre   = data.get("nombre", "").strip()
    trab_ids = data.get("trab_ids", [])
    if not nombre:
        raise HTTPException(400, "El nombre es requerido")
    row = await database.fetch_one(
        "INSERT INTO cuadrilla_grupos (supervisor_id, nombre) "
        "VALUES (:sup, :nombre) ON CONFLICT (supervisor_id, nombre) "
        "DO UPDATE SET activo = true RETURNING id",
        {"sup": supervisor_id, "nombre": nombre}
    )
    grupo_id = row["id"]
    for tid in trab_ids:
        await database.execute(
            "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id) "
            "VALUES (:gid, :tid) ON CONFLICT DO NOTHING",
            {"gid": grupo_id, "tid": str(tid).zfill(3)}
        )
    return {"ok": True, "id": grupo_id, "nombre": nombre}


@app.put("/api/cuadrilla-grupo/{grupo_id}/miembros")
async def reemplazar_miembros_grupo(grupo_id: int, data: dict):
    """Reemplaza la lista completa de miembros del grupo."""
    trab_ids = data.get("trab_ids", [])
    await database.execute(
        "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = :gid",
        {"gid": grupo_id}
    )
    for tid in trab_ids:
        await database.execute(
            "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id) "
            "VALUES (:gid, :tid) ON CONFLICT DO NOTHING",
            {"gid": grupo_id, "tid": str(tid).zfill(3)}
        )
    return {"ok": True, "total": len(trab_ids)}


@app.post("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def agregar_miembro_grupo(grupo_id: int, trab_id: str):
    await database.execute(
        "INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id) "
        "VALUES (:gid, :tid) ON CONFLICT DO NOTHING",
        {"gid": grupo_id, "tid": trab_id.zfill(3)}
    )
    return {"ok": True}


@app.delete("/api/cuadrilla-grupo/{grupo_id}/miembro/{trab_id}")
async def quitar_miembro_grupo(grupo_id: int, trab_id: str):
    await database.execute(
        "DELETE FROM cuadrilla_grupo_miembros WHERE grupo_id = :gid AND trab_id = :tid",
        {"gid": grupo_id, "tid": trab_id.zfill(3)}
    )
    return {"ok": True}


@app.delete("/api/cuadrilla-grupo/{grupo_id}")
async def eliminar_cuadrilla_grupo(grupo_id: int):
    await database.execute(
        "UPDATE cuadrilla_grupos SET activo = false WHERE id = :gid",
        {"gid": grupo_id}
    )
    return {"ok": True}


# ── ENVIAR CON PARTIDAS (nuevo flujo) ─────────────────────────

@app.post("/api/sesion/enviar-con-partidas")
async def enviar_con_partidas(data: dict):
    """
    Flujo nuevo: cada trabajador tiene 1+ asignaciones a partidas con HH.
    Soporta ambos formatos:
      - Nuevo: {trab_id, asignaciones:[{partida_id, hh}], via}
      - Viejo: {trab_id, partida_id, via}  (compat)
    """
    supervisor_id = str(data.get("supervisor_id", "")).strip()
    otm_id        = str(data.get("otm_id", "")).strip()
    fecha_str     = str(data.get("fecha", fecha_lima().isoformat()))
    trabajadores  = data.get("trabajadores", [])

    if not supervisor_id or not otm_id:
        raise HTTPException(400, "supervisor_id y otm_id son requeridos")
    if not trabajadores:
        raise HTTPException(400, "Lista de trabajadores vacía")

    fecha_obj = date.fromisoformat(fecha_str)
    jornada = await database.fetch_one(
        "SELECT hh_dia FROM ev_config_jornada WHERE dia_semana = :dow AND activo = true",
        {"dow": fecha_obj.weekday()}
    )
    hh_dia = float(jornada["hh_dia"]) if jornada else 9.5

    row_cfg = await database.fetch_one("SELECT valor FROM ev_config WHERE clave = :k", {"k": "fecha_base"})
    semana = 1
    if row_cfg and row_cfg["valor"]:
        base = date.fromisoformat(str(row_cfg["valor"]))
        base = base - timedelta(days=base.weekday())   # alinear a lunes (consistente con _semana_de)
        semana = max(1, (fecha_obj - base).days // 7 + 1)

    hora = hora_lima()

    row = await database.fetch_one(
        f"INSERT INTO sesiones "
        f"(supervisor_id, otm_id, fecha, hh_turno, estado, enviada_at) "
        f"VALUES (:sup, :otm, '{fecha_str}'::date, :hh, 'enviada', now()) RETURNING id",
        {"sup": supervisor_id, "otm": otm_id, "hh": hh_dia}
    )
    sesion_id = row["id"]
    enviados  = 0

    for t in trabajadores:
        trab_id = str(t.get("trab_id", "")).zfill(3)
        via     = t.get("via", "app")

        # Normalizar asignaciones — soportar ambos formatos
        asignaciones = t.get("asignaciones")
        if asignaciones is None:
            pid_old = t.get("partida_id")
            asignaciones = [{"partida_id": pid_old, "hh": hh_dia}] if pid_old else []

        # HH total del trabajador = suma de asignaciones (o hh_dia si no hay partidas)
        hh_total = sum(float(a.get("hh", 0)) for a in asignaciones) if asignaciones else hh_dia
        if hh_total <= 0:
            hh_total = hh_dia

        # sesion_trabajadores
        await database.execute(
            "INSERT INTO sesion_trabajadores "
            "(sesion_id, trab_id, presente, hh_override, agregado_via) "
            "VALUES (:sid, :tid, true, null, :via)",
            {"sid": sesion_id, "tid": trab_id, "via": via}
        )

        # registros (backward compat)
        await database.execute(
            f"INSERT INTO registros "
            f"(trab_id, otm_id, supervisor_id, fecha, hora, hh) "
            f"VALUES (:tid, :otm, :sup, '{fecha_str}'::date, '{hora}'::time, :hh) "
            f"ON CONFLICT (trab_id, otm_id, fecha) "
            f"DO UPDATE SET hh=:hh, hora='{hora}'::time",
            {"tid": trab_id, "otm": otm_id, "sup": supervisor_id, "hh": hh_total}
        )

        # tareo_partida — una fila por cada asignación a partida
        for asig in asignaciones:
            pid = asig.get("partida_id")
            hh  = float(asig.get("hh", hh_dia))
            if not pid or hh <= 0:
                continue
            try:
                await database.execute(
                    f"INSERT INTO tareo_partida "
                    f"(trabajador_id, partida_id, otm_id, fecha, semana, "
                    f" hora_registro, hh, supervisor_id, sesion_id, fuente) "
                    f"VALUES (:tid, :pid, :otm, '{fecha_str}'::date, :sem, "
                    f"        NOW(), :hh, :sup, :sid, 'tareo')",
                    {"tid": trab_id, "pid": pid, "otm": otm_id,
                     "sem": semana, "hh": hh,
                     "sup": supervisor_id, "sid": sesion_id}
                )
            except Exception as e:
                print(f"[tareo_partida] trab={trab_id} pid={pid}: {e}")

        enviados += 1

    return {"ok": True, "enviados": enviados, "sesion_id": sesion_id, "hh_dia": hh_dia}


@app.post("/api/tareo-partida/cambio")
async def cambio_partida_dia(data: dict):
    """
    Registra un cambio de partida a mitad del día.
    La hora queda como el timestamp del request.
    El cron recalculará las HH al cierre del día.
    """
    trabajador_ids = data.get("trabajador_ids", [])
    partida_id     = data.get("partida_id")
    otm_id         = str(data.get("otm_id", "")).strip()
    supervisor_id  = str(data.get("supervisor_id", "")).strip()
    fecha_str      = str(data.get("fecha", fecha_lima().isoformat()))

    if not trabajador_ids or not partida_id:
        raise HTTPException(400, "trabajador_ids y partida_id son requeridos")

    row_cfg2 = await database.fetch_one("SELECT valor FROM ev_config WHERE clave = :k", {"k": "fecha_base"})
    semana = 1
    if row_cfg2 and row_cfg2["valor"]:
        base = date.fromisoformat(str(row_cfg2["valor"]))
        base = base - timedelta(days=base.weekday())   # alinear a lunes (consistente con _semana_de)
        semana = max(1, (date.fromisoformat(fecha_str) - base).days // 7 + 1)

    creados = 0
    for tid in trabajador_ids:
        trab_id = str(tid).zfill(3)
        await database.execute(
            f"INSERT INTO tareo_partida "
            f"(trabajador_id, partida_id, otm_id, fecha, semana, "
            f" hora_registro, supervisor_id, fuente) "
            f"VALUES (:tid, :pid, :otm, '{fecha_str}'::date, :sem, "
            f"        NOW(), :sup, 'cambio')",
            {"tid": trab_id, "pid": partida_id, "otm": otm_id,
             "sem": semana, "sup": supervisor_id}
        )
        creados += 1

    return {"ok": True, "creados": creados}
