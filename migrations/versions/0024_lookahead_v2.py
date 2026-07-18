# -*- coding: utf-8 -*-
"""LookAhead v2 (encargo Opus 2026-07-18, una sola migración aditiva):

  · causa_nc_planner_cat / causa_nc_planner — la causa de no cumplimiento
    según el PLANNER (oficina), separada de la reportada desde campo (F3).
  · dias_medio — días del rango que pesan 0.5 en el prorrateo (medio día,
    espejo de dias_salto que pesa 0) (F4).
  · prog_dependencias — antecesoras Fin→Inicio (FS) con lag en días, para
    la auto-cascada del LookAhead (F5). Sin ciclos (lo valida el API).
"""
from alembic import op

revision = "0024_lookahead_v2"
down_revision = "0023_calendario_laboral"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades
  ADD COLUMN causa_nc_planner_cat TEXT
    CHECK (causa_nc_planner_cat IN ('MATERIALES','MANO_OBRA','EQUIPOS','INFORMACION','CLIMA',
                                    'INTERFERENCIA','PRERREQUISITO','CLIENTE','PROGRAMACION','OTROS')),
  ADD COLUMN causa_nc_planner TEXT,
  ADD COLUMN dias_medio DATE[] NOT NULL DEFAULT '{}';

CREATE TABLE prog_dependencias (
  id SERIAL PRIMARY KEY,
  actividad_id INT NOT NULL REFERENCES prog_actividades(id) ON DELETE CASCADE,
  predecesora_id INT NOT NULL REFERENCES prog_actividades(id) ON DELETE CASCADE,
  tipo TEXT NOT NULL DEFAULT 'FS',
  lag_dias INT NOT NULL DEFAULT 0,
  UNIQUE (actividad_id, predecesora_id),
  CHECK (actividad_id <> predecesora_id)
);
CREATE INDEX idx_progdep_pred ON prog_dependencias (predecesora_id);
"""

_DOWN = """
DROP TABLE IF EXISTS prog_dependencias;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS causa_nc_planner_cat,
  DROP COLUMN IF EXISTS causa_nc_planner,
  DROP COLUMN IF EXISTS dias_medio;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
