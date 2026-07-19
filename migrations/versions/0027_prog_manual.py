# -*- coding: utf-8 -*-
"""Celdas del PROGRAMADO protegidas (encargo Jean 2026-07-19):

  · prog_metrado_dia.manual — true cuando el planner escribió la celda a
    mano ("ese día programo menos"). Los re-prorrateos (_redistribuir) le
    reparten el saldo SOLO a los días no manuales y sin avance real; la
    celda manual sobrevive avances, cambios de calendario y cascada FS.
    Vaciar la celda la libera (DELETE) y vuelve al prorrateo automático.

Aditiva y reversible: el down solo elimina la columna.
"""
from alembic import op

revision = "0027_prog_manual"
down_revision = "0026_proyecto_moneda"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_metrado_dia
  ADD COLUMN manual BOOLEAN NOT NULL DEFAULT false;
"""

_DOWN = """
ALTER TABLE prog_metrado_dia DROP COLUMN IF EXISTS manual;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
