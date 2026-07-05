#!/bin/sh
# Backup diario de la BD de KAMPFER a Cloudflare R2 (F0.2 del PLAN_MAESTRO).
# Corre dentro del contenedor de backup (ver scripts/Dockerfile.backup) o de cualquier
# contenedor con pg_dump + aws-cli. Requiere env: DATABASE_URL, R2_ENDPOINT, y credenciales
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (token S3 de R2).
set -eu

BUCKET="${R2_BUCKET:-kampfer-backups}"
F="kampfer_$(date +%F).dump"

echo "[backup] generando $F"
pg_dump -Fc "$DATABASE_URL" > "/tmp/$F"
SIZE=$(wc -c < "/tmp/$F")
echo "[backup] tamaño: $SIZE bytes"
# Un dump sospechosamente chico = BD vacía o error silencioso: no lo subimos como si nada.
[ "$SIZE" -gt 10000 ] || { echo "[backup] ERROR: dump demasiado chico"; exit 1; }

aws s3 cp "/tmp/$F" "s3://$BUCKET/$F" --endpoint-url "$R2_ENDPOINT"
rm -f "/tmp/$F"

echo "[backup] aplicando retención de 14 días"
aws s3 ls "s3://$BUCKET/" --endpoint-url "$R2_ENDPOINT" | awk '{print $4}' | grep '^kampfer_' | sort | head -n -14 \
  | while read -r OLD; do
      [ -n "$OLD" ] && aws s3 rm "s3://$BUCKET/$OLD" --endpoint-url "$R2_ENDPOINT"
    done

echo "[backup] OK $F"
