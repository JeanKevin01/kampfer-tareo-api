# ============================================================
# main.py — ensamblado de la app (F0.5: el monolito quedó partido)
#
#   core/     config (env + fail-closed) · auth (API key, JWT, roles)
#             db (pool asyncpg único) · tiempo (fechas Lima) · log (JSON)
#   routers/  tareo · padron · otms · jornada · monitor · usuarios
#             valor_ganado (/ev) · presupuesto · ro
#
# Aquí solo viven: lifespan (semillas idempotentes), CORS, observabilidad
# (exception handler + timing) y el include de los routers.
# ============================================================
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core import config
from core.auth import require_key, require_role, _hash_pw
from core.db import db as core_db, close_pool
from core.log import setup_logging, get_logger
from routers.jornada import router as jornada_router
from routers.monitor import router as monitor_router
from routers.otms import router as otms_router
from routers.padron import router as padron_router
from routers.presupuesto import router as presupuesto_router
from routers.ro import router as ro_router
from routers.tareo import router as tareo_router
from routers.usuarios import router as usuarios_router
from routers.valor_ganado import router as ev_router

setup_logging()
log = get_logger("api")

APP_VERSION = "1.5.0"   # única fuente de la versión (app y /health)

# EPIC 0.4.1: aborta el arranque en prod si los secretos están por defecto o la API quedaría abierta.
config.validar_secretos_arranque()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # EPIC 0.2.4: el esquema lo gobiernan las migraciones (Alembic). Aquí NO hay DDL,
    # solo conexión y semillas idempotentes (datos) para una BD recién creada.
    pool = await core_db()   # F0.4: pool asyncpg ÚNICO (falla rápido si no hay BD)
    # Semilla de jornada base (si no hay reglas semanales): Miércoles 10 HH, resto 9.5.
    n = await pool.fetchval("SELECT COUNT(*) FROM ev_jornada_reglas WHERE tipo = 'semanal'")
    if not n:
        for dow in range(7):
            await pool.execute(
                "INSERT INTO ev_jornada_reglas (tipo, desde, dia_semana, hh, nota) "
                "VALUES ('semanal', '2000-01-01', $1, $2, 'base inicial')",
                dow, 10.0 if dow == 2 else 9.5,
            )
    # Semilla de usuario admin (si la tabla está vacía).
    nu = await pool.fetchval("SELECT COUNT(*) FROM usuarios")
    if not nu:
        await pool.execute(
            "INSERT INTO usuarios (username, password_hash, rol, nombre) "
            "VALUES ('admin', $1, 'admin', 'Administrador')",
            _hash_pw(config.ADMIN_PASSWORD),
        )
    yield
    await close_pool()   # F0.4: antes el pool de valor_ganado nunca se cerraba


app = FastAPI(title="Kampfer Tareo API", version=APP_VERSION, lifespan=lifespan,
              dependencies=[Depends(require_key)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Observabilidad (F0.8) ────────────────────────────────────
@app.exception_handler(Exception)
async def _error_no_controlado(request: Request, exc: Exception):
    """Cualquier excepción no manejada: traceback al log, mensaje genérico al cliente
    (antes varios endpoints devolvían str(e) y filtraban internals)."""
    log.error("error no controlado", exc_info=exc, extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "Error interno"})


@app.middleware("http")
async def _medir_requests(request: Request, call_next):
    """Loguea requests lentos (>1s) o con 5xx, con path y duración en ms."""
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - t0) * 1000)
    if ms > 1000 or response.status_code >= 500:
        log.info("request", extra={"path": request.url.path, "ms": ms,
                                   "status": response.status_code})
    return response


# ── HEALTH ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


# ── Routers ──────────────────────────────────────────────────
app.include_router(ev_router)
# Fase 1: módulo de presupuesto (todos los endpoints son de oficina).
app.include_router(presupuesto_router, dependencies=[Depends(require_role("oficina"))])
# Fase 2: Resultado Operativo (oficina).
app.include_router(ro_router, dependencies=[Depends(require_role("oficina"))])
# F0.5: dominios que antes vivían en este archivo.
app.include_router(tareo_router)
app.include_router(padron_router)
app.include_router(otms_router)
app.include_router(jornada_router)
app.include_router(monitor_router)
app.include_router(usuarios_router)
