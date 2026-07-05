from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from database import database
from datetime import date, datetime, timezone, timedelta
from typing import Optional
import os
import re
import hmac
import hashlib
import base64
import json
import time
import secrets
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse
from core.log import setup_logging, get_logger
from core.db import db as core_db, close_pool
from routers.valor_ganado import router as ev_router
from routers.presupuesto import router as presupuesto_router
from routers.ro import router as ro_router

setup_logging()
log = get_logger("api")

APP_VERSION = "1.5.0"   # única fuente de la versión (app y /health)

# ── Seguridad Fase 1+3: compuerta global (retrocompatible) ──
# Mientras API_KEY no esté seteada, NO se exige nada (despliegue sin cortar la operación).
# Cuando se setea API_KEY, se cierra TODA la API: cada petición debe traer UNA de dos cosas:
#   · X-API-Key correcta  → para integraciones (n8n), o
#   · Authorization: Bearer <token JWT válido>  → para el panel y la app de campo.
# Rollout seguro: 1) desplegar esta API, 2) configurar n8n con la key, 3) recién setear API_KEY.
ENV = os.getenv("ENV", "dev").strip().lower()   # 'dev' | 'prod' — controla el modo fail-closed
API_KEY = os.getenv("API_KEY", "").strip()
# Rutas que NUNCA exigen credenciales (login incluido: sin él no se podría obtener el token).
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/api/auth/login"}

def _api_key_ok(x_api_key: str) -> bool:
    """True si la X-API-Key coincide con la configurada (comparación en tiempo constante)."""
    return bool(API_KEY) and bool(x_api_key) and hmac.compare_digest(x_api_key, API_KEY)

async def require_key(
    request: Request,
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
):
    # Compat (EPIC 0.4.3): en dev sin API_KEY no se exige nada → operación sin cortes.
    # En prod, validar_secretos_arranque() garantiza API_KEY, así que la API queda cerrada.
    if not API_KEY and ENV != "prod":
        return
    if request.url.path in _PUBLIC_PATHS or request.method == "OPTIONS":
        return
    # 1) Integraciones (n8n): API key compartida en X-API-Key
    if _api_key_ok(x_api_key):
        return
    # 2) Usuarios (panel / app de campo): token JWT válido
    if authorization[:7].lower() == "bearer " and verify_token(authorization[7:]):
        return
    raise HTTPException(401, "No autenticado: se requiere token de sesión o API key")

# Orígenes permitidos por entorno (CSV). Default '*' = comportamiento actual hasta configurarlo.
_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = ["*"] if _origins_env in ("", "*") else [o.strip() for o in _origins_env.split(",") if o.strip()]

# ── Seguridad Fase 2: autenticación por usuario (JWT propio, sin dependencias) ──
# Roles: 'admin' (todo) | 'oficina' (panel) | 'supervisor' (app). Token firmado con HMAC-SHA256.
_DEFAULT_JWT_SECRET = "kampfer-cambia-este-secreto-en-produccion"
_DEFAULT_ADMIN_PW   = "admin123"
JWT_SECRET = (os.getenv("JWT_SECRET", "").strip() or _DEFAULT_JWT_SECRET)
TOKEN_TTL  = int(os.getenv("TOKEN_TTL_SEG", str(60 * 60 * 12)))   # 12 h por defecto
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", _DEFAULT_ADMIN_PW)   # solo para el seed inicial

def validar_secretos_arranque():
    """Fail-closed (EPIC 0.4.1): en producción NO arrancar con secretos por defecto
    ni con la API abierta. En dev no hace nada (operación sin cortes)."""
    if ENV != "prod":
        return
    faltas = []
    if JWT_SECRET == _DEFAULT_JWT_SECRET:
        faltas.append("JWT_SECRET sin configurar")
    if not API_KEY:
        faltas.append("API_KEY obligatoria")
    if ADMIN_PASSWORD == _DEFAULT_ADMIN_PW:
        faltas.append("ADMIN_PASSWORD por defecto")
    if faltas:
        raise RuntimeError(
            "Arranque abortado por seguridad (ENV=prod): " + "; ".join(faltas) +
            ". Configura estas variables de entorno en Coolify."
        )

def _hash_pw(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}${dk.hex()}"

def _check_pw(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def make_token(sub: str, rol: str, nombre: str = "") -> str:
    payload = {"sub": sub, "rol": rol, "nombre": nombre, "exp": int(time.time()) + TOKEN_TTL}
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"

def verify_token(token: str) -> Optional[dict]:
    try:
        body, sig = token.split(".")
        esperado = _b64u(hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, esperado):
            return None
        p = json.loads(_b64u_dec(body))
        if int(p.get("exp", 0)) < time.time():
            return None
        return p
    except Exception:
        return None

async def current_user(authorization: str = Header(default="")) -> Optional[dict]:
    """No-enforcing: devuelve el usuario del token si es válido, o None. No rompe nada."""
    if authorization[:7].lower() == "bearer ":
        return verify_token(authorization[7:])
    return None

def require_role(*roles: str):
    """Dependencia que EXIGE estar autenticado y (opcionalmente) un rol. 'admin' siempre pasa.
    · X-API-Key válida → principal de servicio (n8n): acceso pleno (no rompe integraciones).
    · En dev sin API_KEY → no bloquea (compat con la operación actual).
    · roles vacío = basta con estar autenticado."""
    async def dep(
        x_api_key: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> dict:
        if _api_key_ok(x_api_key):
            return {"sub": "service", "rol": "admin"}
        user = verify_token(authorization[7:]) if authorization[:7].lower() == "bearer " else None
        if not user:
            if not API_KEY and ENV != "prod":      # seguridad global inactiva (compat)
                return {"sub": "compat", "rol": "admin"}
            raise HTTPException(401, "No autenticado")
        if roles and user.get("rol") != "admin" and user.get("rol") not in roles:
            raise HTTPException(403, "No tienes permiso para esta acción")
        return user
    return dep

# F0.4: los helpers de tiempo/fecha viven en core/tiempo.py (implementación única).
from core.tiempo import LIMA, ahora_lima, fecha_lima, hora_lima, hora_lima_t, parse_fecha  # noqa: F401

# EPIC 0.4.1: aborta el arranque en prod si los secretos están por defecto o la API quedaría abierta.
validar_secretos_arranque()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # EPIC 0.2.4: el esquema lo gobiernan las migraciones (Alembic). Aquí NO hay DDL,
    # solo conexión y semillas idempotentes (datos) para una BD recién creada.
    await database.connect()
    await core_db()   # F0.4: calienta el pool asyncpg compartido (falla rápido si no hay BD)
    # Semilla de jornada base (si no hay reglas semanales): Miércoles 10 HH, resto 9.5.
    row = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM ev_jornada_reglas WHERE tipo = 'semanal'"
    )
    if not row or (row["n"] or 0) == 0:
        for dow in range(7):
            await database.execute(
                "INSERT INTO ev_jornada_reglas (tipo, desde, dia_semana, hh, nota) "
                "VALUES ('semanal', '2000-01-01', :d, :h, 'base inicial')",
                {"d": dow, "h": 10.0 if dow == 2 else 9.5},
            )
    # Semilla de usuario admin (si la tabla está vacía).
    urow = await database.fetch_one("SELECT COUNT(*) AS n FROM usuarios")
    if not urow or (urow["n"] or 0) == 0:
        await database.execute(
            "INSERT INTO usuarios (username, password_hash, rol, nombre) "
            "VALUES ('admin', :p, 'admin', 'Administrador')",
            {"p": _hash_pw(ADMIN_PASSWORD)},
        )
    yield
    await close_pool()   # F0.4: antes el pool de valor_ganado nunca se cerraba
    await database.disconnect()


app = FastAPI(title="Kampfer Tareo API", version=APP_VERSION, lifespan=lifespan, dependencies=[Depends(require_key)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
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

app.include_router(ev_router)
# Fase 1: módulo de presupuesto (todos los endpoints son de oficina).
app.include_router(presupuesto_router, dependencies=[Depends(require_role("oficina"))])
# Fase 2: Resultado Operativo (oficina).
app.include_router(ro_router, dependencies=[Depends(require_role("oficina"))])

async def resolver_jornada(fecha: date, otm_id: Optional[str] = None) -> float:
    """HH de jornada vigentes para una fecha (y opcionalmente una OTM):
    En cada nivel, una regla de la OTM específica gana sobre la global (otm_id NULL).
    1) excepción puntual exacta de ese día,
    2) regla semanal del día-de-semana con la mayor 'desde' <= fecha,
    3) fallback (Miércoles 10, resto 9.5)."""
    dow = fecha.weekday()
    # 1) Puntual: OTM específica → global
    if otm_id:
        r = await database.fetch_one(
            "SELECT hh FROM ev_jornada_reglas WHERE tipo='puntual' AND desde=:f AND otm_id=:o "
            "ORDER BY id DESC LIMIT 1", {"f": fecha, "o": otm_id})
        if r:
            return float(r["hh"])
    r = await database.fetch_one(
        "SELECT hh FROM ev_jornada_reglas WHERE tipo='puntual' AND desde=:f AND otm_id IS NULL "
        "ORDER BY id DESC LIMIT 1", {"f": fecha})
    if r:
        return float(r["hh"])
    # 2) Semanal: OTM específica → global
    if otm_id:
        r = await database.fetch_one(
            "SELECT hh FROM ev_jornada_reglas WHERE tipo='semanal' AND dia_semana=:d AND desde<=:f "
            "AND otm_id=:o ORDER BY desde DESC, id DESC LIMIT 1", {"d": dow, "f": fecha, "o": otm_id})
        if r:
            return float(r["hh"])
    r = await database.fetch_one(
        "SELECT hh FROM ev_jornada_reglas WHERE tipo='semanal' AND dia_semana=:d AND desde<=:f "
        "AND otm_id IS NULL ORDER BY desde DESC, id DESC LIMIT 1", {"d": dow, "f": fecha})
    if r:
        return float(r["hh"])
    return 10.0 if dow == 2 else 9.5



# ── JORNADA: reglas de HH por día (configurables, con vigencia) ──
DIAS_SEM = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


@app.get("/api/jornada")
async def jornada_listar(otm: Optional[str] = None):
    """Reglas + HH resueltas para los 7 días de hoy. 'otm' (opcional) calcula
    los vigentes para esa OTM (regla de la OTM gana sobre la global)."""
    reglas = await database.fetch_all(
        "SELECT id, tipo, desde::text AS desde, dia_semana, hh, nota, otm_id, "
        "       creado_en::text AS creado_en "
        "FROM ev_jornada_reglas ORDER BY tipo, otm_id NULLS FIRST, desde DESC, dia_semana"
    )
    otm_id = (otm or "").strip() or None
    hoy = fecha_lima()
    lunes = hoy - timedelta(days=hoy.weekday())
    vigentes = []
    for dow in range(7):
        f = lunes + timedelta(days=dow)
        vigentes.append({
            "dia_semana": dow,
            "dia": DIAS_SEM[dow],
            "hh": await resolver_jornada(f, otm_id),
        })
    return {
        "otm": otm_id,
        "vigentes": vigentes,
        "puntuales": [dict(r) for r in reglas if r["tipo"] == "puntual"],
        "semanal":   [dict(r) for r in reglas if r["tipo"] == "semanal"],
    }


@app.get("/api/jornada/resolver")
async def jornada_resolver(fecha: Optional[str] = None, otm: Optional[str] = None):
    """HH referenciales para una fecha y OTM (la usa el panel del supervisor)."""
    f = parse_fecha(fecha) or fecha_lima()
    otm_id = (otm or "").strip() or None
    return {"fecha": f.isoformat(), "dia_semana": f.weekday(), "otm": otm_id,
            "hh": await resolver_jornada(f, otm_id)}


@app.post("/api/jornada")
async def jornada_guardar(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Crea reglas de jornada. 'otm_id' opcional (null/ausente = todas las OTMs).
    Semanal: {tipo:'semanal', desde:'YYYY-MM-DD', dias:{0:9.5,...,2:10}, otm_id?}
    Puntual: {tipo:'puntual', fecha:'YYYY-MM-DD', hh:12, nota?, otm_id?}"""
    tipo = str(data.get("tipo", "semanal"))
    otm_id = (str(data.get("otm_id") or "").strip()) or None
    if tipo == "puntual":
        f = parse_fecha(data.get("fecha"))
        if not f:
            raise HTTPException(400, "fecha requerida para excepción puntual")
        hh = float(data.get("hh", 0))
        if hh <= 0:
            raise HTTPException(400, "hh debe ser > 0")
        await database.execute(
            "INSERT INTO ev_jornada_reglas (tipo, desde, dia_semana, hh, nota, otm_id) "
            "VALUES ('puntual', :f, NULL, :h, :n, :o)",
            {"f": f, "h": hh, "n": data.get("nota"), "o": otm_id},
        )
        return {"ok": True}

    # semanal
    desde = parse_fecha(data.get("desde")) or fecha_lima()
    dias  = data.get("dias", {})
    if not dias:
        raise HTTPException(400, "dias requerido (ej. {\"0\":9.5,\"2\":10})")
    nota = data.get("nota")
    n = 0
    for dow, hh in dias.items():
        try:
            dow_i = int(dow); hh_f = float(hh)
        except (TypeError, ValueError):
            continue
        if dow_i < 0 or dow_i > 6 or hh_f <= 0:
            continue
        await database.execute(
            "INSERT INTO ev_jornada_reglas (tipo, desde, dia_semana, hh, nota, otm_id) "
            "VALUES ('semanal', :d, :dw, :h, :n, :o)",
            {"d": desde, "dw": dow_i, "h": hh_f, "n": nota, "o": otm_id},
        )
        n += 1
    return {"ok": True, "reglas_creadas": n}


@app.delete("/api/jornada/{regla_id}")
async def jornada_eliminar(regla_id: int, _u: dict = Depends(require_role("oficina"))):
    await database.execute("DELETE FROM ev_jornada_reglas WHERE id = :id", {"id": regla_id})
    return {"ok": True}


# ── MONITOR: seguimiento diario de HH por trabajador / OTM ──
@app.get("/api/monitor/hh-diario")
async def monitor_hh_diario(fecha: Optional[str] = None):
    """Por cada trabajador en la fecha: total de HH registradas sumando TODAS sus
    OTMs/partidas, comparado con la jornada vigente. Semáforo de alertas para
    detectar errores en el tareo (sub-registro o horas extra)."""
    f = parse_fecha(fecha) or fecha_lima()
    jornada = await resolver_jornada(f)
    rows = await database.fetch_all(
        "SELECT tp.trabajador_id, t.nombre, tp.otm_id, "
        "       SUM(tp.hh) AS hh, COUNT(*) AS n "
        "FROM tareo_partida tp "
        "LEFT JOIN trabajadores t ON t.id = tp.trabajador_id "
        "WHERE tp.fecha = :f "
        "GROUP BY tp.trabajador_id, t.nombre, tp.otm_id "
        "ORDER BY t.nombre, tp.otm_id",
        {"f": f},
    )
    por_trab: dict = {}
    for r in rows:
        tid = r["trabajador_id"]
        d = por_trab.setdefault(tid, {
            "trab_id": tid, "nombre": r["nombre"] or tid,
            "total_hh": 0.0, "n_partidas": 0, "otms": [],
        })
        hh = float(r["hh"] or 0)
        d["total_hh"]   += hh
        d["n_partidas"] += int(r["n"] or 0)
        d["otms"].append({"otm_id": r["otm_id"], "hh": round(hh, 2), "n_partidas": int(r["n"] or 0)})

    filas = []
    for d in por_trab.values():
        d["total_hh"] = round(d["total_hh"], 2)
        d["jornada"]  = jornada
        diff = d["total_hh"] - jornada
        d["estado"]    = "ok" if abs(diff) < 0.15 else ("bajo" if diff < 0 else "extra")
        d["diff"]      = round(diff, 2)
        d["multi_otm"] = len(d["otms"]) > 1
        filas.append(d)
    # Alertas primero, luego por nombre
    filas.sort(key=lambda x: (x["estado"] == "ok", x["nombre"]))

    resumen = {
        "fecha": f.isoformat(), "jornada": jornada, "trabajadores": len(filas),
        "ok":    sum(1 for d in filas if d["estado"] == "ok"),
        "bajo":  sum(1 for d in filas if d["estado"] == "bajo"),
        "extra": sum(1 for d in filas if d["estado"] == "extra"),
    }
    return {"resumen": resumen, "filas": filas}


# ── INTEGRIDAD: doble registro de HH y trabajadores duplicados ──
@app.get("/api/monitor/duplicados-hh")
async def monitor_duplicados_hh(fecha: Optional[str] = None):
    """Trabajadores con HH registradas en más de una sesión el mismo día
    (posible doble envío entre supervisores)."""
    f = parse_fecha(fecha) or fecha_lima()
    rows = await database.fetch_all(
        "SELECT tp.trabajador_id, t.nombre, "
        "       COUNT(DISTINCT tp.sesion_id)    AS n_sesiones, "
        "       COUNT(DISTINCT tp.supervisor_id) AS n_supervisores, "
        "       COUNT(DISTINCT tp.otm_id)        AS n_otms, "
        "       SUM(tp.hh)                       AS total_hh "
        "FROM tareo_partida tp "
        "LEFT JOIN trabajadores t ON t.id = tp.trabajador_id "
        "WHERE tp.fecha = :f "
        "GROUP BY tp.trabajador_id, t.nombre "
        "HAVING COUNT(DISTINCT tp.sesion_id) > 1 "
        "ORDER BY SUM(tp.hh) DESC",
        {"f": f},
    )
    filas = [{
        "trab_id": r["trabajador_id"], "nombre": r["nombre"] or r["trabajador_id"],
        "n_sesiones": int(r["n_sesiones"]), "n_supervisores": int(r["n_supervisores"]),
        "n_otms": int(r["n_otms"]), "total_hh": round(float(r["total_hh"] or 0), 2),
    } for r in rows]
    return {"fecha": f.isoformat(), "total": len(filas), "filas": filas}


def _norm_nombre(s: Optional[str]) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


@app.get("/api/trabajadores/duplicados")
async def trabajadores_duplicados():
    """Agrupa trabajadores con el mismo nombre normalizado pero distinto id,
    con su actividad (para decidir cuál conservar)."""
    rows = await database.fetch_all(
        "SELECT t.id, t.nombre, t.cargo, t.activo, "
        "  (SELECT COUNT(*) FROM tareo_partida tp WHERE tp.trabajador_id = t.id) AS n_tareo, "
        "  (SELECT COUNT(*) FROM registros r WHERE r.trab_id = t.id)             AS n_reg "
        "FROM trabajadores t ORDER BY t.nombre, t.id"
    )
    grupos: dict = {}
    for r in rows:
        grupos.setdefault(_norm_nombre(r["nombre"]), []).append({
            "id": r["id"], "nombre": r["nombre"], "cargo": r["cargo"],
            "activo": r["activo"], "n_tareo": int(r["n_tareo"] or 0), "n_reg": int(r["n_reg"] or 0),
        })
    dup = [{"nombre": g[0]["nombre"], "miembros": g} for g in grupos.values() if len(g) > 1]
    dup.sort(key=lambda x: x["nombre"])
    return {"total_grupos": len(dup), "grupos": dup}


@app.post("/api/trabajadores/merge")
async def trabajadores_merge(data: dict, _u: dict = Depends(require_role("oficina"))):
    """Fusiona un trabajador duplicado en otro: reasigna TODAS las referencias
    (vía information_schema) y desactiva el origen. Transaccional: si hay colisión
    de claves únicas, revierte todo y reporta la tabla, sin pérdida de datos."""
    origen  = str(data.get("from_id", "")).strip()
    destino = str(data.get("to_id", "")).strip()
    if not origen or not destino or origen == destino:
        raise HTTPException(400, "from_id y to_id deben ser válidos y distintos")

    cols = await database.fetch_all(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name <> 'trabajadores' "
        "AND column_name IN ('trab_id','trabajador_id','miembro_id')"
    )
    async with database.transaction():
        for c in cols:
            tn, cn = c["table_name"], c["column_name"]
            try:
                await database.execute(
                    f'UPDATE "{tn}" SET "{cn}" = :d WHERE "{cn}" = :o',
                    {"d": destino, "o": origen},
                )
            except Exception as e:
                raise HTTPException(
                    409, f"Colisión al reasignar {tn}.{cn} ({e}). "
                         f"El trabajador {origen} ya tiene datos que chocan con {destino} "
                         f"en esa tabla. Revisa/elimina ese registro y reintenta.")
        await database.execute(
            "UPDATE trabajadores SET activo = false WHERE id = :o", {"o": origen})
    return {"ok": True, "fusionado": origen, "en": destino, "tablas": len(cols)}


# ── AUTH (Fase 2): login JWT + gestión de usuarios ───────────
@app.post("/api/auth/login")
async def auth_login(data: dict):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        raise HTTPException(400, "Usuario y contraseña requeridos")
    row = await database.fetch_one(
        "SELECT username, password_hash, rol, nombre FROM usuarios "
        "WHERE lower(username) = lower(:u) AND activo = true",
        {"u": username},
    )
    if not row or not _check_pw(password, row["password_hash"]):
        raise HTTPException(401, "Usuario o contraseña inválidos")
    token = make_token(row["username"], row["rol"], row["nombre"] or "")
    return {"token": token, "username": row["username"], "rol": row["rol"], "nombre": row["nombre"]}


@app.get("/api/auth/me")
async def auth_me(user: Optional[dict] = Depends(current_user)):
    if not user:
        raise HTTPException(401, "No autenticado")
    return {"username": user.get("sub"), "rol": user.get("rol"), "nombre": user.get("nombre")}


@app.get("/api/admin/usuarios")
async def usuarios_listar(_u: dict = Depends(require_role("admin"))):
    rows = await database.fetch_all(
        "SELECT id, username, rol, nombre, activo, creado_en::text AS creado_en "
        "FROM usuarios ORDER BY username"
    )
    return [dict(r) for r in rows]


@app.post("/api/admin/usuarios")
async def usuarios_crear(data: dict, _u: dict = Depends(require_role("admin"))):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    rol = str(data.get("rol", "oficina")).strip()
    if not username or not password:
        raise HTTPException(400, "username y password requeridos")
    if rol not in ("admin", "oficina", "supervisor"):
        rol = "oficina"
    try:
        await database.execute(
            "INSERT INTO usuarios (username, password_hash, rol, nombre) "
            "VALUES (:u, :p, :r, :n)",
            {"u": username, "p": _hash_pw(password), "r": rol, "n": data.get("nombre")},
        )
    except Exception:
        raise HTTPException(409, "El usuario ya existe")
    return {"ok": True}


@app.put("/api/admin/usuarios/{uid}/password")
async def usuarios_password(uid: int, data: dict, _u: dict = Depends(require_role("admin"))):
    password = str(data.get("password", ""))
    if len(password) < 4:
        raise HTTPException(400, "La contraseña debe tener al menos 4 caracteres")
    await database.execute(
        "UPDATE usuarios SET password_hash = :p WHERE id = :id",
        {"p": _hash_pw(password), "id": uid},
    )
    return {"ok": True}


@app.put("/api/admin/usuarios/{uid}/baja")
async def usuarios_baja(uid: int, _u: dict = Depends(require_role("admin"))):
    await database.execute("UPDATE usuarios SET activo = false WHERE id = :id", {"id": uid})
    return {"ok": True}


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
        "INSERT INTO sesiones (supervisor_id, otm_id, fecha, hh_turno) "
        "VALUES (:sup, :otm, :fch, :hh) RETURNING id",
        {"sup": supervisor_id, "otm": otm_id, "fch": parse_fecha(fecha_str), "hh": hh_turno}
    )
    return {"id": row["id"], "ok": True}


@app.get("/api/sesion/hoy/{supervisor_id}")
async def sesiones_hoy(supervisor_id: str):
    sesiones = await database.fetch_all(
        "SELECT s.id, s.supervisor_id, s.otm_id, s.estado, "
        "       s.hh_turno, s.created_at, "
        "       COUNT(st.id) AS total, "
        "       COUNT(st.id) FILTER (WHERE st.presente) AS presentes "
        "FROM sesiones s "
        "LEFT JOIN sesion_trabajadores st ON st.sesion_id = s.id "
        "WHERE s.supervisor_id = :sup AND s.fecha = :fch "
        "GROUP BY s.id ORDER BY s.created_at DESC",
        {"sup": supervisor_id, "fch": fecha_lima()}
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


# ── HEALTH ───────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}

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
async def crear_supervisor(data: dict, _u: dict = Depends(require_role("oficina"))):
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
async def editar_supervisor(sup_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
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
async def dar_baja_supervisor(sup_id: str, _u: dict = Depends(require_role("oficina"))):
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
# F0.3: estos dos endpoints CONSERVAN su path y shape (los usan 7 páginas del panel)
# pero ahora leen de tareo_partida (fuente única) en vez de la tabla legacy `registros`.
# Una fila por (trabajador, OTM, supervisor) con la suma de HH del día — mismo shape
# que producía la tabla legacy.
_REGISTROS_DIA_SQL = """
    SELECT tp.trabajador_id AS trab_id, t.nombre, t.cargo, tp.otm_id,
           to_char(MIN(tp.hora_registro) AT TIME ZONE 'America/Lima', 'HH24:MI:SS') AS hora,
           tp.supervisor_id, SUM(tp.hh) AS hh
    FROM tareo_partida tp
    JOIN trabajadores t ON t.id = tp.trabajador_id
    WHERE tp.fecha = :fch AND tp.hh IS NOT NULL
    GROUP BY tp.trabajador_id, t.nombre, t.cargo, tp.otm_id, tp.supervisor_id
    ORDER BY trab_id, hora
"""

@app.get("/api/registros/hoy")
async def registros_hoy():
    rows = await database.fetch_all(_REGISTROS_DIA_SQL, {"fch": fecha_lima()})
    return [dict(r) for r in rows]

# ── REGISTROS POR FECHA ───────────────────────────────────────
@app.get("/api/registros/{fecha}")
async def registros_por_fecha(fecha: str):
    f = parse_fecha(fecha)
    if not f:
        raise HTTPException(400, "fecha inválida")
    rows = await database.fetch_all(_REGISTROS_DIA_SQL, {"fch": f})
    return [dict(r) for r in rows]

# ── ADMIN: CREAR TRABAJADOR ───────────────────────────────────
@app.post("/admin/trabajador")
async def crear_trabajador(data: dict, _u: dict = Depends(require_role("oficina"))):
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
async def dar_baja(trab_id: str, _u: dict = Depends(require_role("oficina"))):
    await database.execute(
        "UPDATE trabajadores SET activo = false WHERE id = :id",
        {"id": trab_id.zfill(3)}
    )
    return {"status": "ok"}

# ── ADMIN: AGREGAR / ACTUALIZAR OTM ──────────────────────────
@app.post("/admin/otm")
async def crear_otm(data: dict, _u: dict = Depends(require_role("oficina"))):
    otm_id      = data.get("id", "").strip().upper()
    descripcion = data.get("descripcion", "").strip().upper()
    area        = data.get("area", "").strip()
    estado      = data.get("estado", "POR INICIAR").strip()
    sdp         = data.get("sdp", "").strip()
    cc          = data.get("centro_costo", "").strip()
    plazo       = data.get("plazo") or None
    f_inicio    = parse_fecha(data.get("fecha_inicio"))
    f_fin       = parse_fecha(data.get("fecha_fin"))
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
             monto_valorizado = COALESCE(EXCLUDED.monto_valorizado, otms.monto_valorizado)""",
        {"id": otm_id, "sdp": sdp, "desc": descripcion, "cc": cc,
         "area": area, "estado": estado, "plazo": plazo,
         "f_inicio": f_inicio, "f_fin": f_fin,
         "monto_c": monto_c, "monto_v": monto_v}
    )
    return {"status": "ok", "id": otm_id}


@app.post("/admin/otms/bulk")
async def crear_otms_bulk(data: dict, _u: dict = Depends(require_role("oficina"))):
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
        f_inicio    = parse_fecha(o.get("fecha_inicio"))
        f_fin       = parse_fecha(o.get("fecha_fin"))
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
                     monto_valorizado = COALESCE(EXCLUDED.monto_valorizado, otms.monto_valorizado)""",
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
async def actualizar_estado_otm(otm_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
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
async def editar_trabajador(trab_id: str, data: dict, _u: dict = Depends(require_role("oficina"))):
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

    fecha_obj = parse_fecha(fecha_str)
    if not fecha_obj:
        raise HTTPException(400, "fecha inválida")
    hh_dia = await resolver_jornada(fecha_obj, otm_id)

    row_cfg = await database.fetch_one("SELECT valor FROM ev_config WHERE clave = :k", {"k": "fecha_base"})
    semana = 1
    if row_cfg and row_cfg["valor"]:
        base = date.fromisoformat(str(row_cfg["valor"]))
        base = base - timedelta(days=base.weekday())   # alinear a lunes (consistente con _semana_de)
        semana = max(1, (fecha_obj - base).days // 7 + 1)

    enviados = 0
    fallidos = 0

    try:
        # Todo el registro va en UNA transacción: o entra completo o no entra nada
        # (evita estados a medias y condiciones de carrera en el reenvío).
        async with database.transaction():
            # Idempotencia: un reenvío del mismo supervisor/OTM/día REEMPLAZA el anterior.
            await database.execute(
                "DELETE FROM tareo_partida WHERE supervisor_id=:sup AND otm_id=:otm AND fecha=:fch",
                {"sup": supervisor_id, "otm": otm_id, "fch": fecha_obj}
            )

            row = await database.fetch_one(
                "INSERT INTO sesiones "
                "(supervisor_id, otm_id, fecha, hh_turno, estado, enviada_at) "
                "VALUES (:sup, :otm, :fch, :hh, 'enviada', now()) RETURNING id",
                {"sup": supervisor_id, "otm": otm_id, "hh": hh_dia, "fch": fecha_obj}
            )
            sesion_id = row["id"]

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

                await database.execute(
                    "INSERT INTO sesion_trabajadores "
                    "(sesion_id, trab_id, presente, hh_override, agregado_via) "
                    "VALUES (:sid, :tid, true, null, :via)",
                    {"sid": sesion_id, "tid": trab_id, "via": via}
                )

                # F0.3: se retiró la doble escritura a `registros` (tabla congelada como
                # histórico). tareo_partida es la única fuente de HH del tareo.

                # tareo_partida — una fila por cada asignación a partida
                for asig in asignaciones:
                    pid = asig.get("partida_id")
                    hh  = float(asig.get("hh", hh_dia))
                    if not pid or hh <= 0:
                        continue
                    try:
                        await database.execute(
                            "INSERT INTO tareo_partida "
                            "(trabajador_id, partida_id, otm_id, fecha, semana, "
                            " hora_registro, hh, supervisor_id, sesion_id, fuente) "
                            "VALUES (:tid, :pid, :otm, :fch, :sem, "
                            "        NOW(), :hh, :sup, :sid, 'tareo')",
                            {"tid": trab_id, "pid": pid, "otm": otm_id, "fch": fecha_obj,
                             "sem": semana, "hh": hh,
                             "sup": supervisor_id, "sid": sesion_id}
                        )
                    except Exception as e:
                        fallidos += 1
                        log.warning(f"[tareo_partida] trab={trab_id} pid={pid}: {e}")

                enviados += 1
    except HTTPException:
        raise
    except Exception as e:
        # Cualquier error de BD se devuelve como 500 CON cabeceras CORS y mensaje legible
        # (así el front muestra el error real en vez de un opaco "Failed to fetch").
        raise HTTPException(500, f"Error al registrar tareo: {e}")

    return {"ok": True, "enviados": enviados, "tareo_fallidos": fallidos,
            "sesion_id": sesion_id, "hh_dia": hh_dia}


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

    fecha_obj = parse_fecha(fecha_str)
    creados = 0
    for tid in trabajador_ids:
        trab_id = str(tid).zfill(3)
        await database.execute(
            "INSERT INTO tareo_partida "
            "(trabajador_id, partida_id, otm_id, fecha, semana, "
            " hora_registro, supervisor_id, fuente) "
            "VALUES (:tid, :pid, :otm, :fch, :sem, "
            "        NOW(), :sup, 'cambio')",
            {"tid": trab_id, "pid": partida_id, "otm": otm_id, "fch": fecha_obj,
             "sem": semana, "sup": supervisor_id}
        )
        creados += 1

    return {"ok": True, "creados": creados}
