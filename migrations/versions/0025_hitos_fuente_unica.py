# -*- coding: utf-8 -*-
"""Hitos en el LookAhead + fuente única de avance (encargo Jean 2026-07-18):

  · ev_avances_diarios.hito_id — a qué HITO (etapa) de la partida pertenece el
    registro diario. NULL = hito principal (convención: las vistas por partida
    — semana-grid, matriz — leen solo NULL = cantidad instalada). El unique
    pasa de (partida, fecha) a (partida, fecha, COALESCE(hito_id, 0)) para
    permitir una fila por etapa el mismo día.
  · prog_actividades.hito_id — sub-actividad "desplegada por hitos" del
    LookAhead: la actividad programa UNA etapa de la partida; su registro
    diario alimenta ese hito en ev_avances (rollup automático en el API).

Downgrade real: elimina las filas diarias por hito (dato nuevo de esta
versión) para poder restaurar el unique original.
"""
from alembic import op

revision = "0025_hitos_fuente_unica"
down_revision = "0024_lookahead_v2"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE ev_avances_diarios
  ADD COLUMN hito_id INT REFERENCES ev_hitos(id) ON DELETE CASCADE;

DROP INDEX IF EXISTS ev_avances_diarios_partida_id_fecha_key;
CREATE UNIQUE INDEX ev_avances_diarios_pfh_key
  ON ev_avances_diarios (partida_id, fecha, COALESCE(hito_id, 0));

ALTER TABLE prog_actividades
  ADD COLUMN hito_id INT REFERENCES ev_hitos(id) ON DELETE SET NULL;
"""

_DOWN = """
ALTER TABLE prog_actividades DROP COLUMN IF EXISTS hito_id;

DELETE FROM ev_avances_diarios WHERE hito_id IS NOT NULL;
DROP INDEX IF EXISTS ev_avances_diarios_pfh_key;
ALTER TABLE ev_avances_diarios DROP COLUMN IF EXISTS hito_id;
CREATE UNIQUE INDEX ev_avances_diarios_partida_id_fecha_key
  ON ev_avances_diarios (partida_id, fecha);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
