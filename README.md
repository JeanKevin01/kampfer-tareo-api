# kampfer-tareo-api

API del sistema KAMPFER (*del tareo al Resultado Operativo sin Excel*): tareo QR desde campo →
ISP/Valor Ganado → Presupuesto → Resultado Operativo. FastAPI + PostgreSQL + Alembic.

## Arranque local (Windows/Linux)

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt pytest httpx

# Postgres local con Docker:
docker run -d --name kampfer-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=kampfer_test -p 55432:5432 postgres:16

# Migraciones (BD limpia → esquema completo):
DATABASE_URL="postgresql://postgres:test@localhost:55432/kampfer_test" .venv/Scripts/python -m alembic upgrade head

# Correr el API:
DATABASE_URL="postgresql://postgres:test@localhost:55432/kampfer_test" ENV=dev .venv/Scripts/python -m uvicorn main:app --port 8001
```

Tests (49, funciones puras — no necesitan BD): `.venv/Scripts/python -m pytest tests/ -v`

## Variables de entorno

| Var | Uso |
|---|---|
| `DATABASE_URL` | Postgres (obligatoria) |
| `ENV` | `dev` (abierto sin API_KEY) / `prod` (fail-closed: exige secretos) |
| `API_KEY`, `JWT_SECRET`, `ADMIN_PASSWORD`, `ALLOWED_ORIGINS` | Secretos de producción |
| `LOG_LEVEL` | Nivel de logging JSON (default INFO) |

## Estructura

- `main.py` — app, seguridad (gate global + JWT + roles), tareo/sesiones/cuadrillas, padrón, salud.
- `routers/valor_ganado.py` — motor EV/ISP (funciones puras testeadas + endpoints `/ev/*`).
- `routers/presupuesto.py` / `routers/ro.py` — presupuesto gobernado y Resultado Operativo.
- `core/` — utilidades transversales (`log.py`: logging JSON).
- `migrations/` — Alembic (`0001_baseline` → …). **Nunca editar migraciones aplicadas.**
- `scripts/` — operación (backup diario a R2; ver `RUNBOOK_RESTORE.md`).

## Reglas del repo

1. Todo cambio de esquema = migración Alembic con `upgrade` y `downgrade` reales.
2. CI (GitHub Actions) corre migraciones en Postgres limpio + pytest; debe estar verde antes de mergear.
3. Deploy: Coolify (VPS). Backups: `RUNBOOK_RESTORE.md`.
4. Hoja de ruta: `PLAN_MAESTRO` (carpeta de trabajo `Analisis Claude/`, fuera del repo).
