# -*- coding: utf-8 -*-
"""F2.1: periodos contables (mes a mes) con tipo de cambio y cierre.

El RO mensual (F2.5) agrupa TODO por periodo; los documentos de costo, ajustes de
venta y valorizaciones solo se mueven con el periodo ABIERTO (la regla la aplican
los endpoints → 409 si está CERRADO). El tipo_cambio mensual alimenta la vista USD.
"""
from alembic import op

revision = "0013_periodos"
down_revision = "0012_apu"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE periodos (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  anio INT NOT NULL,
  mes INT NOT NULL CHECK (mes BETWEEN 1 AND 12),
  tipo_cambio NUMERIC(8,4) NOT NULL DEFAULT 1,
  estado TEXT NOT NULL DEFAULT 'ABIERTO' CHECK (estado IN ('ABIERTO','CERRADO')),
  cerrado_en TIMESTAMPTZ,
  cerrado_por TEXT,
  UNIQUE (proyecto_id, anio, mes)
);
"""

_DOWN = "DROP TABLE IF EXISTS periodos;"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
