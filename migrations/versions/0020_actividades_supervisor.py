# -*- coding: utf-8 -*-
"""Flujo de campo sobre las actividades programadas (pedido de Jean 2026-07-11):

  · prog_actividades.supervisor_id — el planner ASIGNA la actividad a un
    supervisor; la app de campo le muestra "sus" actividades del día.
  · estado nuevo NO_CUMPLIDA + causa_nc — si la actividad no se ejecutó, el
    supervisor (u oficina) registra la causa de no cumplimiento (opcional,
    estilo Last Planner).
"""
from alembic import op

revision = "0020_actividades_supervisor"
down_revision = "0019_programacion_media"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades ADD COLUMN supervisor_id VARCHAR(10) REFERENCES supervisores(id);
ALTER TABLE prog_actividades ADD COLUMN causa_nc TEXT;
ALTER TABLE prog_actividades DROP CONSTRAINT prog_actividades_estado_check;
ALTER TABLE prog_actividades ADD CONSTRAINT prog_actividades_estado_check
  CHECK (estado IN ('PROGRAMADO','EJECUTADO','CANCELADO','NO_CUMPLIDA'));
CREATE INDEX idx_progact_sup_fecha ON prog_actividades (supervisor_id, fecha);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_progact_sup_fecha;
UPDATE prog_actividades SET estado = 'CANCELADO' WHERE estado = 'NO_CUMPLIDA';
ALTER TABLE prog_actividades DROP CONSTRAINT prog_actividades_estado_check;
ALTER TABLE prog_actividades ADD CONSTRAINT prog_actividades_estado_check
  CHECK (estado IN ('PROGRAMADO','EJECUTADO','CANCELADO'));
ALTER TABLE prog_actividades DROP COLUMN IF EXISTS causa_nc;
ALTER TABLE prog_actividades DROP COLUMN IF EXISTS supervisor_id;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
