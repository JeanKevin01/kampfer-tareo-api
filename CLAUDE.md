# CLAUDE.md — kampfer-tareo-api

Responder en español. Este es el backend del sistema KAMPFER (tareo QR → ISP → presupuesto → RO).

## Comandos

- Tests: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest tests/ -q` (**574** puros; los 403 de
  roles usan TestClient sin BD)
- E2E local (**153** checks, mismo arnés del CI): ver cabecera de `scripts/validacion_e2e.py`.
  Correrlo **dos veces seguidas**: la segunda pasada destapa que un humo no limpió lo suyo.
  Reiniciar el uvicorn local antes de cada corrida — no lleva `--reload` y serviría el código viejo.
- Migraciones: `DATABASE_URL=... .venv/Scripts/python -m alembic upgrade head`
- Postgres local: contenedor `postgres:16` (ver README). App local: uvicorn puerto 8001.

## Arquitectura (post F0.5)

- `main.py` = solo ensamblado (lifespan con semillas, CORS, observabilidad, includes). NO agregar endpoints aquí.
- `core/`: `config` (env + fail-closed prod) · `auth` (API key, JWT, require_role, identidad supervisor)
  · `db` (pool asyncpg ÚNICO — no crear pools ni usar otra capa de BD) · `tiempo` (fechas Lima, semana_de)
  · `log` (JSON a stdout).
- `routers/`: `tareo` (flujo de campo) · `padron` · `otms` · `jornada` · `monitor` · `usuarios`
  · `valor_ganado` (/ev; `router` = oficina, `router_campo` = endpoints del supervisor)
  · `presupuesto` · `ro`.

## Reglas duras

1. **Nunca** editar migraciones ya aplicadas; todo cambio de esquema = nueva migración con
   `upgrade` Y `downgrade` reales, probada contra BD limpia (el CI lo verifica) **y contra el
   dump real de prod** (lección 0008: el baseline puede diferir en constraints vs índices).
2. **Nunca** tocar la BD de producción a mano.
3. Compatibilidad de API: no cambiar paths/shapes de endpoints salvo que la tarea lo pida
   (verificar con diff de `openapi.json`).
4. El motor EV (`routers/valor_ganado.py`: `_calcular/_totales/_agrupar/_matriz_area_disciplina`)
   es el activo más valioso — funciones puras con tests; no tocar sin tests verdes.
   Precedencia anti-doble-conteo de HH: manual > tareo real > histórico (proporcional retirada en F0.3).
5. Logging: `from core.log import get_logger` (JSON a stdout). Prohibido `print(`.
   Errores: dejar que suba al exception handler global o `log.exception(...)` + HTTP 500 genérico.
   NUNCA devolver `str(e)` al cliente.
6. Seguridad F0.6: `/ev` es de oficina (los endpoints de campo van en `router_campo`);
   los endpoints de campo con `supervisor_id` llaman `exigir_identidad_supervisor`.
   Los usuarios rol supervisor llevan `supervisor_id` (claim `sup_id` en el token).
7. Commits convencionales `tipo(scope): descripción` en español.

## Estado (plan vigente: `Analisis Claude/VIGENTE/PLAN_MAESTRO_V9.md` del workspace)

- Fases 0, 1, 2, S y 4 completas. `valor_ganado.py` ya está partido en `routers/ev/` (los tests
  importan de `routers.valor_ganado`: hay shim de re-exports, no romperlo).
- **El archivo grande de hoy es `routers/programacion.py` (4,3k líneas)** — es el próximo a partir,
  pero **no durante el piloto**.
- Migración aplicada más alta en prod: verificar con `alembic current` en el contenedor (Coolify).
- Sistema aún sin usuarios activos (pre-despliegue a supervisores): ventanas de migración flexibles.
- Siguiente: congelar el resultado semanal (T2) → métricas de la tesis (T5) → validación de una
  semana real → piloto. Ver §12 del plan maestro.
