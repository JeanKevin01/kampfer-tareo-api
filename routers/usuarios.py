# ============================================================
# routers/usuarios.py — login JWT + gestión de usuarios (Fase 2 seguridad)
# ============================================================
import re
import unicodedata
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from core.auth import _check_pw, _hash_pw, current_user, make_token, require_role
from core.db import db as core_db

router = APIRouter(tags=["usuarios"])


@router.post("/api/auth/login")
async def auth_login(data: dict):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        raise HTTPException(400, "Usuario y contraseña requeridos")
    pool = await core_db()
    row = await pool.fetchrow(
        "SELECT username, password_hash, rol, nombre, supervisor_id FROM usuarios "
        "WHERE lower(username) = lower($1) AND activo = true",
        username,
    )
    if not row or not _check_pw(password, row["password_hash"]):
        raise HTTPException(401, "Usuario o contraseña inválidos")
    # F0.6: el token de un supervisor lleva su identidad (claim sup_id) para
    # que los endpoints de campo puedan impedir la suplantación.
    extra = {"sup_id": row["supervisor_id"]} if row["supervisor_id"] else None
    token = make_token(row["username"], row["rol"], row["nombre"] or "", extra=extra)
    return {"token": token, "username": row["username"], "rol": row["rol"],
            "nombre": row["nombre"], "supervisor_id": row["supervisor_id"]}


@router.get("/api/auth/me")
async def auth_me(user: Optional[dict] = Depends(current_user)):
    if not user:
        raise HTTPException(401, "No autenticado")
    return {"username": user.get("sub"), "rol": user.get("rol"),
            "nombre": user.get("nombre"), "supervisor_id": user.get("sup_id")}


@router.get("/api/admin/usuarios")
async def usuarios_listar(_u: dict = Depends(require_role("admin"))):
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT id, username, rol, nombre, activo, supervisor_id, clave_inicial, "
        "       creado_en::text AS creado_en "
        "FROM usuarios ORDER BY username"
    )
    return [dict(r) for r in rows]


@router.post("/api/admin/usuarios")
async def usuarios_crear(data: dict, _u: dict = Depends(require_role("admin"))):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    rol = str(data.get("rol", "oficina")).strip()
    if not username or not password:
        raise HTTPException(400, "username y password requeridos")
    if rol not in ("admin", "oficina", "supervisor"):
        rol = "oficina"
    pool = await core_db()
    # F0.6: un usuario supervisor DEBE quedar ligado a un supervisor del padrón
    # (esa identidad viaja en el token y evita la suplantación en campo).
    supervisor_id = str(data.get("supervisor_id") or "").strip() or None
    if rol == "supervisor":
        if not supervisor_id:
            raise HTTPException(400, "supervisor_id es requerido para usuarios con rol supervisor")
        existe = await pool.fetchval(
            "SELECT 1 FROM supervisores WHERE id = $1 AND activo = true", supervisor_id)
        if not existe:
            raise HTTPException(400, f"El supervisor {supervisor_id} no existe o está inactivo")
    else:
        supervisor_id = None
    try:
        await pool.execute(
            "INSERT INTO usuarios (username, password_hash, rol, nombre, supervisor_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            username, _hash_pw(password), rol, data.get("nombre"), supervisor_id,
        )
    except Exception:
        raise HTTPException(409, "El usuario ya existe")
    return {"ok": True}


@router.put("/api/admin/usuarios/{uid}/password")
async def usuarios_password(uid: int, data: dict, _u: dict = Depends(require_role("admin"))):
    password = str(data.get("password", ""))
    if len(password) < 4:
        raise HTTPException(400, "La contraseña debe tener al menos 4 caracteres")
    pool = await core_db()
    # clave_inicial deja de ser cierto salvo que se reponga la de fábrica
    await pool.execute(
        "UPDATE usuarios SET password_hash = $1, clave_inicial = $2 WHERE id = $3",
        _hash_pw(password), password == CLAVE_INICIAL, uid)
    return {"ok": True}


@router.put("/api/admin/usuarios/{uid}/baja")
async def usuarios_baja(uid: int, _u: dict = Depends(require_role("admin"))):
    pool = await core_db()
    await pool.execute("UPDATE usuarios SET activo = false WHERE id = $1", uid)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# USUARIOS DESDE EL PADRÓN (2026-07-19, pre-piloto)
#
# El acceso de un supervisor NO se escribe a mano: se elige a la persona ya
# registrada (supervisor del padrón o trabajador, que se promueve solo) y el
# usuario nace con su identidad ligada — así al entrar a la app de campo se
# salta la pantalla "¿Quién eres?" (el token lleva sup_id, F0.6).
# ══════════════════════════════════════════════════════════════

CLAVE_INICIAL = "1234"


def slug_username(nombre: str) -> str:
    """Genera el usuario a partir del nombre del padrón.

    Formato peruano «APELLIDO APELLIDO NOMBRES» → inicial del nombre + primer
    apellido, en minúsculas y sin tildes: MAMANI CCOPA DAVID → dmamani
    (decisión de Jean: corto, se teclea con guantes en el celular).
    """
    limpio = unicodedata.normalize("NFKD", str(nombre or "")).encode("ascii", "ignore").decode()
    # Los signos se quitan SIN partir la palabra (O'CONNOR → OCONNOR)
    partes = [p for p in re.sub(r"[^A-Za-z ]", "", limpio).upper().split() if p]
    if not partes:
        return ""
    if len(partes) == 1:
        base = partes[0]
    else:
        # ≥3 palabras: los 2 primeros son apellidos y el 3º el nombre de pila.
        pila = partes[2] if len(partes) >= 3 else partes[1]
        base = pila[0] + partes[0]
    return base.lower()[:20]


async def _username_libre(con, base: str) -> str:
    """Primer username disponible a partir de la base (dmamani, dmamani2…)."""
    base = base or "usuario"
    cand, n = base, 1
    while await con.fetchval("SELECT 1 FROM usuarios WHERE lower(username) = lower($1)", cand):
        n += 1
        cand = f"{base}{n}"
    return cand


async def crear_usuario_supervisor(con, supervisor_id: str, nombre: str,
                                   username: str = "", password: str = "") -> Optional[dict]:
    """Crea el acceso de un supervisor del padrón. Devuelve None si ese
    supervisor YA tiene usuario activo (idempotente: el import puede repetirse).
    """
    ya = await con.fetchval(
        "SELECT username FROM usuarios WHERE supervisor_id = $1 AND activo = true", supervisor_id)
    if ya:
        return None
    clave = password or CLAVE_INICIAL
    user = (username or "").strip().lower() or await _username_libre(con, slug_username(nombre))
    await con.execute(
        "INSERT INTO usuarios (username, password_hash, rol, nombre, supervisor_id, clave_inicial) "
        "VALUES ($1, $2, 'supervisor', $3, $4, $5)",
        user, _hash_pw(clave), nombre, supervisor_id, clave == CLAVE_INICIAL,
    )
    return {"supervisor_id": supervisor_id, "nombre": nombre,
            "username": user, "password": clave}


@router.get("/api/admin/personal-elegible")
async def personal_elegible(_u: dict = Depends(require_role("admin"))):
    """Personal del padrón para el selector de Usuarios: supervisores y
    trabajadores activos, con su acceso actual (si lo tienen) y el username
    sugerido. Elegir un TRABAJADOR lo promueve a supervisor al crear el usuario.
    """
    pool = await core_db()
    sups = await pool.fetch(
        "SELECT s.id, s.nombre, s.trabajador_id, "
        "       (SELECT username FROM usuarios u "
        "         WHERE u.supervisor_id = s.id AND u.activo = true LIMIT 1) AS username "
        "FROM supervisores s WHERE s.activo = true ORDER BY s.nombre")
    trabs = await pool.fetch(
        "SELECT t.id, t.nombre, t.cargo FROM trabajadores t "
        "WHERE t.activo = true "
        "  AND NOT EXISTS (SELECT 1 FROM supervisores s WHERE s.trabajador_id = t.id) "
        "ORDER BY t.nombre")
    out = [{"origen": "SUPERVISOR", "id": r["id"], "nombre": r["nombre"],
            "cargo": "Supervisor", "supervisor_id": r["id"],
            "username": r["username"],
            "username_sugerido": slug_username(r["nombre"])} for r in sups]
    out += [{"origen": "TRABAJADOR", "id": r["id"], "nombre": r["nombre"],
             "cargo": r["cargo"], "supervisor_id": None, "username": None,
             "username_sugerido": slug_username(r["nombre"])} for r in trabs]
    return out


@router.post("/api/admin/usuarios/desde-personal")
async def usuario_desde_personal(data: dict, _u: dict = Depends(require_role("admin"))):
    """Crea el acceso de una persona del padrón. Si es un TRABAJADOR que aún no
    reporta, lo registra primero como supervisor (promoción) y liga ambos.
    """
    origen = str(data.get("origen", "SUPERVISOR")).strip().upper()
    pid = str(data.get("id", "")).strip()
    if not pid:
        raise HTTPException(400, "Elige a la persona del padrón")
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", "")) or CLAVE_INICIAL
    if len(password) < 4:
        raise HTTPException(400, "La contraseña debe tener al menos 4 caracteres")

    pool = await core_db()
    async with pool.acquire() as con:
        async with con.transaction():
            if origen == "TRABAJADOR":
                trab = await con.fetchrow(
                    "SELECT id, nombre FROM trabajadores WHERE id = $1 AND activo = true", pid)
                if not trab:
                    raise HTTPException(400, "Ese trabajador no existe o está inactivo")
                sup_id = await con.fetchval(
                    "SELECT id FROM supervisores WHERE trabajador_id = $1", pid)
                if not sup_id:
                    # Promoción: alta en el padrón de supervisores ligada al trabajador
                    max_id = await con.fetchval(
                        r"SELECT MAX(CAST(id AS INTEGER)) FROM supervisores WHERE id ~ '^\d+$'")
                    sup_id = str((max_id or 0) + 1).zfill(2)
                    await con.execute(
                        "INSERT INTO supervisores (id, nombre, activo, trabajador_id) "
                        "VALUES ($1, $2, true, $3)", sup_id, trab["nombre"], pid)
                nombre = trab["nombre"]
                promovido = True
            else:
                sup = await con.fetchrow(
                    "SELECT id, nombre FROM supervisores WHERE id = $1 AND activo = true", pid)
                if not sup:
                    raise HTTPException(400, "Ese supervisor no existe o está inactivo")
                sup_id, nombre, promovido = sup["id"], sup["nombre"], False

            if username and await con.fetchval(
                    "SELECT 1 FROM usuarios WHERE lower(username) = lower($1)", username):
                raise HTTPException(409, f"El usuario «{username}» ya existe")
            creado = await crear_usuario_supervisor(con, sup_id, nombre, username, password)
            if not creado:
                raise HTTPException(409, f"{nombre} ya tiene un acceso activo")
    creado["promovido"] = promovido
    return creado


@router.post("/api/admin/usuarios/sincronizar-supervisores")
async def sincronizar_supervisores(_u: dict = Depends(require_role("admin"))):
    """Crea de golpe el acceso de todos los supervisores activos que aún no lo
    tienen, con la clave inicial. Idempotente: los que ya tienen no se tocan.
    """
    pool = await core_db()
    creados: list = []
    ya_tenian = 0
    async with pool.acquire() as con:
        async with con.transaction():
            sups = await con.fetch(
                "SELECT id, nombre FROM supervisores WHERE activo = true ORDER BY nombre")
            for s in sups:
                nuevo = await crear_usuario_supervisor(con, s["id"], s["nombre"])
                if nuevo:
                    creados.append(nuevo)
                else:
                    ya_tenian += 1
    return {"creados": creados, "ya_tenian": ya_tenian, "clave_inicial": CLAVE_INICIAL}
