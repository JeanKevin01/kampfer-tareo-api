# -*- coding: utf-8 -*-
"""F4 (app de campo offline, 2026-07-19) — idempotencia de /campo/reportes:
la app de campo genera un UUID (id_local) por reporte y lo reenvía en cada
reintento del outbox; el índice único parcial garantiza que un reintento
tras un éxito "silencioso" (respuesta perdida por mala señal) NO duplique
el reporte ni sus fotos. Nullable: los reportes históricos y los clientes
viejos siguen funcionando sin enviarlo.
"""
from alembic import op

revision = "0029_reporte_id_local"
down_revision = "0028_drop_plantillas_hitos"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE campo_reportes ADD COLUMN IF NOT EXISTS id_local TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_campo_reportes_id_local
  ON campo_reportes (id_local) WHERE id_local IS NOT NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS uq_campo_reportes_id_local;
ALTER TABLE campo_reportes DROP COLUMN IF EXISTS id_local;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
