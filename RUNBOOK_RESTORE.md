# RUNBOOK — Backup y restauración de la BD KAMPFER (F0.2)

## 0. Qué hay HOY en R2 (verificado 2026-07-05)

En el bucket `kampfer` existe `data/coolify/backups/coolify/coolify-db-hostdockerinternal/` con
dumps diarios `pg-dump-coolify-*.dmp` (desde mayo 2026). **Eso es el backup de la BD interna de
COOLIFY** (configuración del servidor), NO de la BD de KAMPFER. El MIME "tcpdump.pcap" que muestra
Cloudflare es una etiqueta errónea inofensiva.

**Conclusión:** la conexión Coolify→R2 ya funciona (no hay que crear tokens ni buckets), pero
**la BD de KAMPFER aún no se respalda** → activar la Opción A.

## 1. Opción A (RECOMENDADA) — Backup nativo de Coolify para la BD de KAMPFER

En el dashboard de Coolify (10 minutos, una sola vez):

1. Abrir el **recurso de la base de datos** de KAMPFER (el PostgreSQL del proyecto, el mismo cuyo
   `DATABASE_URL` usa el API).
2. Entrar a la pestaña **Backups** → **Add / Scheduled Backup**.
3. Configurar: **Frequency** = `0 2 * * *` (02:00 todos los días) · **Save to S3** = ON →
   seleccionar el storage S3/R2 ya existente (el mismo que usa el backup de Coolify) ·
   retención/número de backups a conservar = 14 (si el campo existe).
4. Botón **Backup Now** para no esperar a las 02:00.
5. Verificar en Cloudflare R2: debe aparecer una carpeta NUEVA bajo `data/coolify/backups/…`
   (con el nombre del recurso Postgres de KAMPFER) con un `pg-dump-…dmp` de HOY.
6. De paso: revisar si el backup de la BD interna de Coolify sigue corriendo (último archivo
   ¿de mayo o de hoy?). Si se detuvo: Coolify → Settings → Backup → re-habilitar.

> Si el Postgres de KAMPFER NO aparece como recurso propio en Coolify (p. ej. corre embebido en el
> compose del API), usar la Opción B.

## 2. Opción B (alternativa) — Contenedor de backup propio

Servicio en Coolify desde este repo con `scripts/Dockerfile.backup` (cron 02:00 América/Lima ejecuta
`scripts/backup_diario.sh`: `pg_dump -Fc` + subida a R2 + retención 14 días). Variables requeridas:
`DATABASE_URL`, `R2_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `R2_BUCKET` (usar
`kampfer` o crear `kampfer-backups`). Las credenciales se crean en R2 → *Manage API Tokens* →
token **Object Read & Write** limitado al bucket.

## 3. Restauración (RTO objetivo: < 1 hora)

1. **Descargar el dump** más reciente de KAMPFER: dashboard R2 → bucket → carpeta del backup →
   click en el archivo → **Descargar**. (Con backup nativo de Coolify también se puede restaurar
   desde la misma pestaña Backups con un click — preferir esa vía si el servidor está vivo.)
2. Anotar el conteo de control de la BD destino (si está viva):
   `psql "$DATABASE_URL" -c "SELECT count(*) FROM tareo_partida;"`
3. Restaurar: `pg_restore -d "$DATABASE_URL" --clean --if-exists --no-owner <archivo>.dmp`
4. Verificar:
   - `SELECT count(*) FROM tareo_partida;` ≈ valor esperado
   - `SELECT version_num FROM alembic_version;` = última migración aplicada
5. Reiniciar el contenedor del API en Coolify y smoke test: login en el panel + `GET /health` +
   abrir Valor Ganado.

Si la restauración falla a medias: repetir el paso 3 (es `--clean --if-exists`), o restaurar a una
BD nueva y apuntar el `DATABASE_URL` del API a ella.

## 4. Prueba del runbook (obligatoria — un backup no probado no existe)

Con un dump descargado y Docker local (esta prueba la ejecuta Claude en la máquina de Jean):

```bash
docker run -d --name pg-restore-test -e POSTGRES_PASSWORD=test -p 55433:5432 postgres:16
pg_restore -d "postgresql://postgres:test@localhost:55433/postgres" --clean --if-exists --no-owner <dump>
psql "postgresql://postgres:test@localhost:55433/postgres" -c "SELECT count(*) FROM tareo_partida;"
docker rm -f pg-restore-test
```

| Fecha | Dump probado | Resultado |
|---|---|---|
| _pendiente_ | | |
