# -*- coding: utf-8 -*-
"""F2.6: proyección mixta del RO (AUTO/MANUAL) + snapshot PREV.

- ro_proyeccion: costo proyectado por (fase, recurso, periodo futuro). El motor
  la usa para PROY/SALDO DE OBRA. `origen` distingue la propuesta AUTO del
  override MANUAL (regenerar NO pisa lo manual).
- ro_prev: snapshot inmutable de la proyección al CERRAR un mes → la columna
  PREV del mes siguiente en T OBRA.
"""
from alembic import op

revision = "0016_proyeccion"
down_revision = "0015_venta"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE ro_proyeccion (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  fase TEXT,
  tipo_recurso TEXT NOT NULL,
  periodo_id INT NOT NULL REFERENCES periodos(id),
  monto NUMERIC(14,2) NOT NULL DEFAULT 0,
  origen TEXT NOT NULL DEFAULT 'AUTO' CHECK (origen IN ('AUTO','MANUAL')),
  actualizado_por TEXT,
  actualizado_en TIMESTAMPTZ DEFAULT now(),
  UNIQUE (proyecto_id, fase, tipo_recurso, periodo_id)
);
CREATE TABLE ro_prev (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  periodo_id INT NOT NULL REFERENCES periodos(id),   -- el mes al que aplica el PREV
  fase TEXT,
  tipo_recurso TEXT NOT NULL,
  monto NUMERIC(14,2) NOT NULL DEFAULT 0,
  creado_en TIMESTAMPTZ DEFAULT now(),
  UNIQUE (proyecto_id, periodo_id, fase, tipo_recurso)
);
"""

_DOWN = """
DROP TABLE IF EXISTS ro_prev;
DROP TABLE IF EXISTS ro_proyeccion;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
