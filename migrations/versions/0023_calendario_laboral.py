# -*- coding: utf-8 -*-
"""Calendario laboral de la programación (pedido de Jean 2026-07-12):

  · prog_config — días de la semana que se trabajan, por proyecto (ISO 1=Lun..
    7=Dom; default todos, como hasta ahora). Pensado para reusar KAMPFER en
    empresas con otros regímenes.
  · prog_feriados — días no laborables puntuales (feriados, paradas de planta):
    el prorrateo del LookAhead los salta.
  · prog_actividades.dias_salto — saltos INTENCIONALES por actividad (el
    planner decide que tal día no se trabaja esa actividad aunque sea hábil).
"""
from alembic import op

revision = "0023_calendario_laboral"
down_revision = "0022_lookahead_metrado"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE prog_config (
  proyecto_id INT PRIMARY KEY REFERENCES proyectos(id),
  dias_semana INT[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
  actualizado_en TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE prog_feriados (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  fecha DATE NOT NULL,
  motivo TEXT,
  UNIQUE (proyecto_id, fecha)
);

ALTER TABLE prog_actividades ADD COLUMN dias_salto DATE[] NOT NULL DEFAULT '{}';
"""

_DOWN = """
ALTER TABLE prog_actividades DROP COLUMN IF EXISTS dias_salto;
DROP TABLE IF EXISTS prog_feriados;
DROP TABLE IF EXISTS prog_config;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
