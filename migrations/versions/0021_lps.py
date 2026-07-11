# -*- coding: utf-8 -*-
"""Last Planner System (pedido de Jean 2026-07-11 tarde):

  · causa_nc_cat — la causa de no cumplimiento pasa a CATEGORÍA de catálogo
    (para el Pareto de CNC) + el detalle libre queda en causa_nc.
  · prog_restricciones — análisis de restricciones del lookahead: qué falta
    liberar (materiales, información, prerrequisitos…), quién y para cuándo.
    Una actividad "sana" para comprometer es la que no tiene pendientes.
"""
from alembic import op

revision = "0021_lps"
down_revision = "0020_actividades_supervisor"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades ADD COLUMN causa_nc_cat TEXT
  CHECK (causa_nc_cat IN ('MATERIALES','MANO_OBRA','EQUIPOS','INFORMACION','CLIMA',
                          'INTERFERENCIA','PRERREQUISITO','CLIENTE','PROGRAMACION','OTROS'));

CREATE TABLE prog_restricciones (
  id SERIAL PRIMARY KEY,
  actividad_id INT NOT NULL REFERENCES prog_actividades(id) ON DELETE CASCADE,
  descripcion TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'OTROS'
    CHECK (tipo IN ('MATERIALES','MANO_OBRA','EQUIPOS','INFORMACION',
                    'PRERREQUISITO','PERMISOS','ESPACIO','OTROS')),
  responsable TEXT,
  fecha_requerida DATE,
  liberada BOOLEAN NOT NULL DEFAULT false,
  liberada_en TIMESTAMPTZ,
  creado_en TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_progrest_act ON prog_restricciones (actividad_id);
"""

_DOWN = """
DROP TABLE IF EXISTS prog_restricciones;
ALTER TABLE prog_actividades DROP COLUMN IF EXISTS causa_nc_cat;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
