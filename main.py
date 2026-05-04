from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import database
from datetime import date, datetime
from typing import Optional

app = FastAPI(title="Kampfer Tareo API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# ── HEALTH CHECK ─────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}

# ── TRABAJADORES ─────────────────────────────────────────
@app.get("/api/trabajadores")
async def get_trabajadores():
    rows = await database.fetch_all(
        "SELECT id, nombre, cargo FROM trabajadores "
        "WHERE activo = true ORDER BY nombre"
    )
    return [dict(r) for r in rows]

# ── BUSCAR TRABAJADOR ─────────────────────────────────────
@app.get("/api/buscar")
async def buscar(q: str):
    if len(q) < 2:
        return []
    rows = await database.fetch_all(
        "SELECT id, nombre, cargo FROM trabajadores "
        "WHERE activo = true AND ("
        "  nombre ILIKE :q OR cargo ILIKE :q OR id = :id"
        ") ORDER BY nombre LIMIT 8",
        {"q": f"%{q}%", "id": q.zfill(3)}
    )
    return [dict(r) for r in rows]

# ── OTMs ─────────────────────────────────────────────────
@app.get("/api/otms")
async def get_otms():
    rows = await database.fetch_all(
        "SELECT id, descripcion, area, estado, centro_costo "
        "FROM otms "
        "WHERE estado IN ('EJECUCION', 'POR INICIAR') "
        "ORDER BY id"
    )
    return [dict(r) for r in rows]

# ── SUPERVISORES ──────────────────────────────────────────
@app.get("/api/supervisores")
async def get_supervisores():
    rows = await database.fetch_all(
        "SELECT id, nombre, email FROM supervisores "
        "WHERE activo = true ORDER BY nombre"
    )
    return [dict(r) for r in rows]

# ── REGISTRO BATCH ────────────────────────────────────────
@app.post("/api/registro")
async def registrar(data: dict):
    supervisor_id     = data.get("supervisor_id")
    otm_id            = data.get("otm_id")
    fecha             = data.get("fecha", date.today().isoformat())
    trabajadores_list = data.get("trabajadores", [])

    if not supervisor_id or not otm_id or not trabajadores_list:
        raise HTTPException(400, "Faltan campos: supervisor_id, otm_id, trabajadores[]")

    resultados = []

    for t in trabajadores_list:
        trab_id = str(t.get("trab_id", "")).zfill(3)
        hora    = t.get("hora", datetime.now().strftime("%H:%M:%S"))

        try:
            await database.execute(
                """
                INSERT INTO registros (trab_id, otm_id, supervisor_id, fecha, hora)
                VALUES (:trab_id, :otm_id, :supervisor_id, :fecha, :hora)
                """,
                {
                    "trab_id":     trab_id,
                    "otm_id":      otm_id,
                    "supervisor_id": supervisor_id,
                    "fecha":       fecha,
                    "hora":        hora,
                }
            )
            resultados.append({
                "trab_id": trab_id,
                "nombre":  t.get("nombre", ""),
                "cargo":   t.get("cargo", ""),
                "status":  "ok"
            })

        except Exception as e:
            if "unique" in str(e).lower():
                resultados.append({
                    "trab_id": trab_id,
                    "nombre":  t.get("nombre", ""),
                    "cargo":   t.get("cargo", ""),
                    "status":  "duplicate"
                })
            else:
                resultados.append({
                    "trab_id": trab_id,
                    "nombre":  t.get("nombre", ""),
                    "cargo":   t.get("cargo", ""),
                    "status":  "error",
                    "error":   str(e)
                })

    ok  = len([r for r in resultados if r["status"] == "ok"])
    dup = len([r for r in resultados if r["status"] == "duplicate"])

    return {
        "status":     "ok",
        "nuevos":     ok,
        "duplicados": dup,
        "resultados": resultados
    }

# ── REGISTROS DEL DÍA ─────────────────────────────────────
@app.get("/api/registros/hoy")
async def registros_hoy():
    rows = await database.fetch_all(
        """
        SELECT r.trab_id, t.nombre, t.cargo,
               r.otm_id, r.hora::text, r.supervisor_id, r.hh
        FROM registros r
        JOIN trabajadores t ON r.trab_id = t.id
        WHERE r.fecha = CURRENT_DATE
        ORDER BY r.hora
        """
    )
    return [dict(r) for r in rows]

# ── REGISTROS POR FECHA ───────────────────────────────────
@app.get("/api/registros/{fecha}")
async def registros_por_fecha(fecha: str):
    rows = await database.fetch_all(
        """
        SELECT r.trab_id, t.nombre, t.cargo,
               r.otm_id, r.hora::text, r.supervisor_id, r.hh
        FROM registros r
        JOIN trabajadores t ON r.trab_id = t.id
        WHERE r.fecha = :fecha
        ORDER BY r.hora
        """,
        {"fecha": fecha}
    )
    return [dict(r) for r in rows]
