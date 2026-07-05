# CLAUDE.md — kampfer-tareo-api

Responder en español. Este es el backend del sistema KAMPFER (tareo QR → ISP → presupuesto → RO).

## Comandos

- Tests: `.venv/Scripts/python -m pytest tests/ -q` (61 puros; los 403 de roles usan TestClient sin BD)
- E2E local (11 checks, mismo arnés del CI): ver cabecera de `scripts/validacion_e2e.py`
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

## Estado (plan vigente: PLAN_MAESTRO v3.1 en `Analisis Claude/` del workspace)

- Fase 0 completa salvo F0.5b (partir `valor_ganado.py` en `routers/ev/` — 2,2k líneas;
  los tests importan de `routers.valor_ganado`, usar shim de re-exports al partirlo).
- Migración aplicada más alta en prod: verificar con `alembic current` en el contenedor (Coolify).
- Sistema aún sin usuarios activos (pre-despliegue a supervisores): ventanas de migración flexibles.
- Siguiente en el orden ajustado: F4 (app de campo offline v2 + piloto = experimento de la tesis)
  → F1 (APU) → F2 (RO mensual).
