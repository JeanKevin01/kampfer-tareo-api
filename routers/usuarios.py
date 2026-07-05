# ============================================================
# routers/usuarios.py — login JWT + gestión de usuarios (Fase 2 seguridad)
# ============================================================
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
        "SELECT username, password_hash, rol, nombre FROM usuarios "
        "WHERE lower(username) = lower($1) AND activo = true",
        username,
    )
    if not row or not _check_pw(password, row["password_hash"]):
        raise HTTPException(401, "Usuario o contraseña inválidos")
    token = make_token(row["username"], row["rol"], row["nombre"] or "")
    return {"token": token, "username": row["username"], "rol": row["rol"], "nombre": row["nombre"]}


@router.get("/api/auth/me")
async def auth_me(user: Optional[dict] = Depends(current_user)):
    if not user:
        raise HTTPException(401, "No autenticado")
    return {"username": user.get("sub"), "rol": user.get("rol"), "nombre": user.get("nombre")}


@router.get("/api/admin/usuarios")
async def usuarios_listar(_u: dict = Depends(require_role("admin"))):
    pool = await core_db()
    rows = await pool.fetch(
        "SELECT id, username, rol, nombre, activo, creado_en::text AS creado_en "
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
    try:
        await pool.execute(
            "INSERT INTO usuarios (username, password_hash, rol, nombre) "
            "VALUES ($1, $2, $3, $4)",
            username, _hash_pw(password), rol, data.get("nombre"),
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
    await pool.execute(
        "UPDATE usuarios SET password_hash = $1 WHERE id = $2", _hash_pw(password), uid)
    return {"ok": True}


@router.put("/api/admin/usuarios/{uid}/baja")
async def usuarios_baja(uid: int, _u: dict = Depends(require_role("admin"))):
    pool = await core_db()
    await pool.execute("UPDATE usuarios SET activo = false WHERE id = $1", uid)
    return {"ok": True}
