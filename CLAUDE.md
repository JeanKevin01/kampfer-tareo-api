# CLAUDE.md — kampfer-tareo-api

Responder en español. Este es el backend del sistema KAMPFER (tareo QR → ISP → presupuesto → RO).

## Comandos

- Tests: `.venv/Scripts/python -m pytest tests/ -q` (49 puros, sin BD)
- Migraciones: `DATABASE_URL=... .venv/Scripts/python -m alembic upgrade head`
- Postgres local: contenedor `postgres:16` (ver README). App local: uvicorn puerto 8001.

## Reglas duras

1. **Nunca** editar migraciones ya aplicadas; todo cambio de esquema = nueva migración con
   `upgrade` Y `downgrade` reales, probada contra BD limpia (el CI lo verifica).
2. **Nunca** tocar la BD de producción a mano.
3. Compatibilidad de API: no cambiar paths/shapes de endpoints salvo que la tarea lo pida
   (verificar con diff de `openapi.json`).
4. El motor EV (`routers/valor_ganado.py`: `_calcular/_totales/_agrupar/_matriz_area_disciplina`)
   es el activo más valioso — funciones puras con tests; no tocar sin tests verdes.
   Precedencia anti-doble-conteo de HH: manual > tareo real > histórico > proporcional.
5. Logging: `from core.log import get_logger` (JSON a stdout). Prohibido `print(`.
   Errores: dejar que suba al exception handler global o `log.exception(...)` + HTTP 500 genérico.
   NUNCA devolver `str(e)` al cliente.
6. Commits convencionales `tipo(scope): descripción` en español.

## Contexto de deuda (plan vigente: PLAN_MAESTRO v3.1, en la carpeta `Analisis Claude/` del workspace)

- Doble capa de BD (lib `databases` + pool asyncpg privado en valor_ganado) — se unifica en F0.4.
- Endpoints legacy del flujo viejo de tareo (n8n apagado) — se retiran en F0.3.
- `/ev` montado sin `require_role` — se corrige en F0.6.
- `ev_partidas.codigo` UNIQUE global (bloquea multi-OTM) — migración 0008b en F0.7.
- Sistema aún sin usuarios activos (pre-despliegue a supervisores): ventanas de migración flexibles.
