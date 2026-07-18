# -*- coding: utf-8 -*-
"""Proyectos (ex-OTM) — moneda por proyecto (encargo Jean 2026-07-18):

  · otms.moneda — 'PEN' (soles) o 'USD' (dólares), default PEN. Se elige al
    crear/importar el proyecto y etiqueta los montos en el panel.

La entidad sigue siendo la tabla `otms` (compatibilidad de API y FKs); el
rename a "Proyecto" es de UI. El catálogo de estados (POR INICIAR / EJECUCION /
CONCLUIDO / CERRADO / STAND BY) se valida en el API, no con CHECK, para no
romper dumps históricos con estados libres.
"""
from alembic import op

revision = "0026_proyecto_moneda"
down_revision = "0025_hitos_fuente_unica"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE otms
  ADD COLUMN moneda TEXT NOT NULL DEFAULT 'PEN'
    CHECK (moneda IN ('PEN', 'USD'));
"""

_DOWN = """
ALTER TABLE otms DROP COLUMN IF EXISTS moneda;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
