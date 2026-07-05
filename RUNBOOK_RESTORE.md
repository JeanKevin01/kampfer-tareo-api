# RUNBOOK — Backup y restauración de la BD KAMPFER (F0.2)

## 1. Cómo funciona el backup

- `scripts/backup_diario.sh` corre **todos los días a las 02:00 (America/Lima)** dentro del
  contenedor definido en `scripts/Dockerfile.backup` (servicio en Coolify).
- Genera `kampfer_YYYY-MM-DD.dump` (`pg_dump -Fc`) y lo sube al bucket R2 `kampfer-backups`.
- Retención: **14 días** (borra los dumps más viejos automáticamente).

## 2. Setup pendiente (una sola vez — Jean) ☐

1. **Cloudflare R2** (dash.cloudflare.com → R2): crear bucket privado `kampfer-backups`.
2. R2 → *Manage API Tokens* → crear token **Object Read & Write** limitado al bucket. Anotar
   `Access Key ID`, `Secret Access Key` y el endpoint `https://<account_id>.r2.cloudflarestorage.com`.
3. **Coolify**: nuevo recurso → *Dockerfile* → este repo, ruta `scripts/Dockerfile.backup`, con
   variables de entorno:
   - `DATABASE_URL` = la misma del API (interna del VPS)
   - `R2_ENDPOINT` = endpoint del paso 2
   - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` = credenciales del token
   - (opcional) `R2_BUCKET` = kampfer-backups
4. Probar sin esperar a las 02:00: en la terminal del contenedor → `backup_diario.sh`.
5. Verificar: `aws s3 ls s3://kampfer-backups/ --endpoint-url $R2_ENDPOINT` muestra el dump de hoy
   (o verlo en el dashboard de R2).

## 3. Restauración (RTO objetivo: < 1 hora)

```bash
# 1) Descargar el dump más reciente (dashboard R2 o aws cli):
aws s3 cp s3://kampfer-backups/kampfer_YYYY-MM-DD.dump . --endpoint-url $R2_ENDPOINT

# 2) ANTES de restaurar, anotar el conteo de control de la BD destino (si está viva):
psql "$DATABASE_URL" -c "SELECT count(*) FROM tareo_partida;"

# 3) Restaurar (idempotente: limpia y recrea objetos):
pg_restore -d "$DATABASE_URL" --clean --if-exists --no-owner kampfer_YYYY-MM-DD.dump

# 4) Verificar:
psql "$DATABASE_URL" -c "SELECT count(*) FROM tareo_partida;"          # ≈ valor esperado
psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"     # última migración
# 5) Levantar/reiniciar el contenedor del API en Coolify y smoke test:
#    login en el panel + GET /health + GET /ev/reporte
```

Si la restauración falla a medias: repetir el paso 3 (es `--clean --if-exists`) o restaurar sobre
una BD nueva y apuntar `DATABASE_URL` del API a ella.

## 4. Prueba del runbook (obligatoria — el backup no probado no existe)

Probar contra un Postgres local (Docker):
```bash
docker run -d --name pg-restore-test -e POSTGRES_PASSWORD=test -p 55433:5432 postgres:16
pg_restore -d "postgresql://postgres:test@localhost:55433/postgres" --clean --if-exists --no-owner kampfer_YYYY-MM-DD.dump
psql "postgresql://postgres:test@localhost:55433/postgres" -c "SELECT count(*) FROM tareo_partida;"
docker rm -f pg-restore-test
```

| Fecha | Dump probado | Resultado |
|---|---|---|
| _pendiente_ | | |
