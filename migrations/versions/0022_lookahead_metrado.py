# -*- coding: utf-8 -*-
"""Lookahead con metrado diario (pedido de Jean 2026-07-11 noche, plantillas
"Anexo 01 - LookAhead" y "F030b - Planeamiento" del ex-gerente):

  · prog_actividades gana fecha_fin (la actividad puede abarcar un rango,
    como F.Inic/F.Fin del Excel), metrado_prog (metrado comprometido del
    rango) y und (unidad libre cuando no hay partida de control).
  · prog_metrado_dia — la distribución DIARIA del metrado programado
    (las celdas verdes del LookAhead: metrado/(días del rango), editables).

El metrado REAL no vive aquí: se registra en ev_avances_diarios (la misma
tabla del módulo de Valor Ganado), de modo que el avance puede ingresarse
por cualquiera de las 2 vías y siempre alimenta el EV.
"""
from alembic import op

revision = "0022_lookahead_metrado"
down_revision = "0021_lps"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades
  ADD COLUMN fecha_fin DATE,
  ADD COLUMN metrado_prog NUMERIC(14,3),
  ADD COLUMN und VARCHAR(10);
ALTER TABLE prog_actividades
  ADD CONSTRAINT prog_actividades_rango_check
  CHECK (fecha_fin IS NULL OR fecha_fin >= fecha);

CREATE TABLE prog_metrado_dia (
  id SERIAL PRIMARY KEY,
  actividad_id INT NOT NULL REFERENCES prog_actividades(id) ON DELETE CASCADE,
  fecha DATE NOT NULL,
  cantidad NUMERIC(14,3) NOT NULL,
  UNIQUE (actividad_id, fecha)
);
CREATE INDEX idx_progmet_fecha ON prog_metrado_dia (fecha);
"""

_DOWN = """
DROP TABLE IF EXISTS prog_metrado_dia;
ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_actividades_rango_check;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS fecha_fin,
  DROP COLUMN IF EXISTS metrado_prog,
  DROP COLUMN IF EXISTS und;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
