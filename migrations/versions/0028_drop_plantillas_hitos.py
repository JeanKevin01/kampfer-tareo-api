# -*- coding: utf-8 -*-
"""Fase S (saneamiento pre-F4, 2026-07-19) — retiro de la tabla huérfana
ev_plantillas_hitos: el catálogo de "rules of credit por tipo de actividad"
nunca se conectó a la UI y lo reemplazaron los hitos por partida (HITO1..5
del import + VG·Configuración) y el hito principal silencioso (0025).
Los endpoints GET/POST /ev/plantillas se retiran en el mismo commit.

Downgrade real: recrea la tabla con su esquema original (baseline 0001).
"""
from alembic import op

revision = "0028_drop_plantillas_hitos"
down_revision = "0027_prog_manual"
branch_labels = None
depends_on = None

_UP = """
DROP TABLE IF EXISTS ev_plantillas_hitos;
"""

_DOWN = """
CREATE TABLE ev_plantillas_hitos (
  tipo_actividad TEXT PRIMARY KEY,
  hitos JSONB NOT NULL
);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
