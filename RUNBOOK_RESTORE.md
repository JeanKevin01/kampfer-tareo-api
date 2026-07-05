# RUNBOOK — Backup y restauración de la BD KAMPFER (F0.2)

## 0. Estado actual (2026-07-05)

- **Configurado por Jean en Coolify (v4.1.2):** recurso `postgres` del proyecto → Copias de
  seguridad programadas, diario `0 2 * * *` (zona UTC = 21:00 hora Perú), S3 habilitado →
  storage **"Kampfer"** → bucket R2 **`kampfer-backups`** (endpoint de la cuenta, validado).
- Retención local (disco del VPS): 1 copia / 2 días. Retención S3: **14 copias** (acordado;
  0 = ilimitado en Coolify — no dejar en 0 por costo, ni en 2 por seguridad).
- Los dumps `pg-dump-coolify-*` de mayo 2026 en el bucket viejo `kampfer` eran del **VPS gratuito
  anterior** (BD interna de aquel Coolify). Sin valor actual; borrables.
- ⚠️ El token R2 quedó expuesto en una captura durante el setup → **rotarlo** (R2 → API Tokens →
  Roll) y actualizar las claves del Storage en Coolify una vez validado el flujo.

## 1. Verificaciones del primer backup — COMPLETADAS 2026-07-05 ✅

1. ✅ BD verificada: `DATABASE_URL` del API apunta a la misma BD (`postgres`) que respalda Coolify.
2. ✅ "Copia de seguridad ahora" ejecutada con éxito.
3. ✅ `pg-dump-postgres-1783236464.dmp` (110 KB) en el bucket `kampfer-backups` (02:27 PET).
4. ✅ Prueba de restauración local OK (ver tabla §4): 58 tareo_partida / 551 HH / 100 trabajadores.
5. ☐ **PENDIENTE: rotar el token R2** (quedó expuesto en captura durante el setup) — R2 → API
   Tokens → Roll → actualizar claves en el Storage de Coolify → Validate Connection.
6. ✅ Secretos del API rotados y redeploy hecho (2026-07-05). Recordatorio: la clave REAL del
   usuario admin vive en la BD (cambiarla desde el panel → Usuarios). Contraseña de Postgres sin
   rotar (aceptado: el contenedor no expone puerto público).

## 2. Alternativa (solo si el backup nativo fallara) — Contenedor propio

Servicio en Coolify desde este repo con `scripts/Dockerfile.backup` (cron 02:00 América/Lima:
`scripts/backup_diario.sh` = `pg_dump -Fc` + subida a R2 + retención 14 días). Variables:
`DATABASE_URL`, `R2_ENDPOINT`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `R2_BUCKET=kampfer-backups`.

## 3. Restauración (RTO objetivo: < 1 hora)

1. **Descargar el dump** más reciente: dashboard R2 → `kampfer-backups` → archivo → Descargar.
   (Con el servidor vivo, Coolify también permite restaurar desde la pestaña Backups directamente.)
2. Anotar conteo de control de la BD destino (si está viva):
   `psql "$DATABASE_URL" -c "SELECT count(*) FROM tareo_partida;"`
3. Restaurar: `pg_restore -d "$DATABASE_URL" --clean --if-exists --no-owner <archivo>.dmp`
4. Verificar: conteo de `tareo_partida` ≈ esperado · `SELECT version_num FROM alembic_version;`
   = última migración.
5. Reiniciar el contenedor del API en Coolify y smoke test: login panel + `GET /health` + Valor Ganado.

Si falla a medias: repetir paso 3 (es `--clean --if-exists`) o restaurar a una BD nueva y apuntar
el `DATABASE_URL` del API a ella.

## 4. Prueba del runbook (obligatoria — un backup no probado no existe)

Con un dump descargado y Docker local (la ejecuta Claude en la máquina de Jean):

```bash
docker run -d --name pg-restore-test -e POSTGRES_PASSWORD=test -p 55433:5432 postgres:16
pg_restore -d "postgresql://postgres:test@localhost:55433/postgres" --clean --if-exists --no-owner <dump>
psql "postgresql://postgres:test@localhost:55433/postgres" -c "SELECT count(*) FROM tareo_partida;"
docker rm -f pg-restore-test
```

| Fecha | Dump probado | Resultado |
|---|---|---|
| 2026-07-05 | `pg-dump-postgres-1783236464.dmp` (110,566 B, backup nativo Coolify → R2) | ✅ OK — restaurado en postgres:16 limpio: alembic 0006_ro · tareo_partida 58 filas/551 HH · registros 52 · trabajadores 100 · ev_partidas 70 · usuarios 2 |
