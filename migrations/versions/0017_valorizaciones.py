# -*- coding: utf-8 -*-
"""F2.8: valorización mensual (BORRADOR → PRESENTADA → APROBADA).

Una por (proyecto, periodo). Al APROBAR, el motor del RO (F2.5) la prefiere
como venta CONTRACTUAL del periodo (sobre la mensualización de ev_valorizado).
"""
from alembic import op

revision = "0017_valorizaciones"
down_revision = "0016_proyeccion"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE valorizaciones (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  periodo_id INT NOT NULL REFERENCES periodos(id),
  estado TEXT NOT NULL DEFAULT 'BORRADOR' CHECK (estado IN ('BORRADOR','PRESENTADA','APROBADA')),
  nota TEXT,
  creado_en TIMESTAMPTZ DEFAULT now(),
  aprobado_en TIMESTAMPTZ,
  UNIQUE (proyecto_id, periodo_id)
);
CREATE TABLE valorizacion_lineas (
  id SERIAL PRIMARY KEY,
  valorizacion_id INT NOT NULL REFERENCES valorizaciones(id) ON DELETE CASCADE,
  partida_id INT NOT NULL REFERENCES ev_partidas(id),
  cantidad NUMERIC(14,4) NOT NULL DEFAULT 0,
  pu NUMERIC(14,4) NOT NULL DEFAULT 0,
  UNIQUE (valorizacion_id, partida_id)
);
"""

_DOWN = """
DROP TABLE IF EXISTS valorizacion_lineas;
DROP TABLE IF EXISTS valorizaciones;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
