# ============================================================
# core/personal.py — padrón unificado de personas
#
# REGLA (Jean, 2026-07-19): TODA persona del proyecto vive en `trabajadores`
# —directos, indirectos y supervisores—. Ser supervisor es un ROL adicional:
# una ficha en `supervisores` ligada a su ficha de trabajador
# (`supervisores.trabajador_id`, migración 0030) más su acceso a la app.
#
# Estas funciones son idempotentes y REUTILIZAN el perfil existente (por DNI
# si lo hay, si no por nombre normalizado): reimportar la misma planilla no
# duplica personas ni accesos.
# ============================================================
import re
import unicodedata
from typing import Optional

from core.auth import _hash_pw

CLAVE_INICIAL = "1234"


def norm_nombre(s: Optional[str]) -> str:
    """Nombre comparable: sin tildes, en mayúsculas y sin espacios de sobra."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


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


async def asegurar_trabajador(con, nombre: str, cargo: str = "", dni: str = "",
                              tipo: str = "DIRECTO") -> dict:
    """Devuelve la ficha de trabajador de esa persona, creándola si no existe.

    Reutiliza el perfil por DNI (si viene) o por nombre normalizado; en ese
    caso completa los datos que estuvieran vacíos y lo reactiva si estaba dado
    de baja. Devuelve {id, nombre, nuevo}.
    """
    nombre = (nombre or "").strip().upper()
    if not nombre:
        raise ValueError("nombre requerido")
    cargo = (cargo or "").strip().upper()
    dni = (dni or "").strip()
    tipo = tipo if tipo in ("DIRECTO", "INDIRECTO") else "DIRECTO"

    fila = None
    if dni:
        fila = await con.fetchrow(
            "SELECT id, nombre FROM trabajadores WHERE dni = $1 LIMIT 1", dni)
    if not fila:
        # La comparación sin tildes se hace aquí (Postgres necesitaría la
        # extensión unaccent); el padrón son decenas de filas.
        objetivo = norm_nombre(nombre)
        for r in await con.fetch("SELECT id, nombre FROM trabajadores"):
            if norm_nombre(r["nombre"]) == objetivo:
                fila = r
                break
    if fila:
        await con.execute(
            "UPDATE trabajadores SET activo = true, "
            "  cargo = CASE WHEN $2 <> '' THEN $2 ELSE cargo END, "
            "  dni   = CASE WHEN $3 <> '' THEN $3 ELSE dni END "
            "WHERE id = $1", fila["id"], cargo, dni)
        return {"id": fila["id"], "nombre": fila["nombre"], "nuevo": False}

    max_id = await con.fetchval(
        r"SELECT MAX(CAST(id AS INTEGER)) FROM trabajadores WHERE id ~ '^\d+$'")
    nuevo_id = str((max_id or 0) + 1).zfill(3)
    await con.execute(
        "INSERT INTO trabajadores (id, nombre, cargo, dni, tipo) VALUES ($1,$2,$3,$4,$5)",
        nuevo_id, nombre, cargo or "SIN CARGO", dni, tipo)
    return {"id": nuevo_id, "nombre": nombre, "nuevo": True}


async def asegurar_supervisor(con, trabajador_id: str, nombre: str, email: str = "") -> dict:
    """Da (o recupera) el rol de supervisor de una persona del padrón.

    Reutiliza la ficha ligada a ese trabajador; si no la hay, adopta una ficha
    de supervisor suelta con el mismo nombre (los que existían antes de que el
    padrón se unificara) y la liga. Devuelve {id, nuevo}.
    """
    nombre = (nombre or "").strip().upper()
    sup_id = await con.fetchval(
        "SELECT id FROM supervisores WHERE trabajador_id = $1", trabajador_id)
    if sup_id:
        await con.execute("UPDATE supervisores SET activo = true WHERE id = $1", sup_id)
        return {"id": sup_id, "nuevo": False}

    objetivo = norm_nombre(nombre)
    suelto = next(
        (r["id"] for r in await con.fetch(
            "SELECT id, nombre FROM supervisores WHERE trabajador_id IS NULL")
         if norm_nombre(r["nombre"]) == objetivo), None)
    if suelto:
        await con.execute(
            "UPDATE supervisores SET trabajador_id = $1, activo = true WHERE id = $2",
            trabajador_id, suelto)
        return {"id": suelto, "nuevo": False}

    max_id = await con.fetchval(
        r"SELECT MAX(CAST(id AS INTEGER)) FROM supervisores WHERE id ~ '^\d+$'")
    nuevo_id = str((max_id or 0) + 1).zfill(2)
    await con.execute(
        "INSERT INTO supervisores (id, nombre, email, activo, trabajador_id) "
        "VALUES ($1,$2,$3,true,$4)", nuevo_id, nombre, (email or "").strip(), trabajador_id)
    return {"id": nuevo_id, "nuevo": True}


async def crear_usuario_supervisor(con, supervisor_id: str, nombre: str,
                                   username: str = "", password: str = "") -> Optional[dict]:
    """Crea el acceso a la app de campo de un supervisor del padrón.

    Devuelve None si ese supervisor YA tiene usuario activo — el perfil se
    reutiliza con su contraseña actual (reimportar no lo pisa ni lo duplica).
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


async def alta_persona(con, nombre: str, cargo: str = "", dni: str = "", tipo: str = "DIRECTO",
                       es_supervisor: bool = False, email: str = "") -> dict:
    """Alta unificada: ficha de trabajador SIEMPRE y, si reporta, rol de
    supervisor + acceso a la app. Idempotente de punta a punta."""
    trab = await asegurar_trabajador(con, nombre, cargo, dni, tipo)
    out = {"id": trab["id"], "nombre": trab["nombre"], "nuevo": trab["nuevo"],
           "supervisor_id": None, "usuario": None, "password": None}
    if es_supervisor:
        sup = await asegurar_supervisor(con, trab["id"], trab["nombre"], email)
        acceso = await crear_usuario_supervisor(con, sup["id"], trab["nombre"])
        out["supervisor_id"] = sup["id"]
        if acceso:
            out["usuario"] = acceso["username"]
            out["password"] = acceso["password"]
    return out
