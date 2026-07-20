# -*- coding: utf-8 -*-
"""Reporte de campo estructurado (2026-07-19) — el parte diario del supervisor.

El reporte deja de ser un texto suelto: se guarda por partes para poder
(a) armar el mensaje que el supervisor pega en el grupo de WhatsApp,
(b) alimentar el Pareto de causas con las restricciones que le bajaron la
    productividad (mismo catálogo CNC de «no se hizo»), y
(c) ofrecerle el último reporte de esa misma partida/hito como plantilla.

- `area`: dónde se ejecutó (se precarga con el área del proyecto y se edita).
- `turno`: DIA | NOCHE.
- `anotaciones`: JSONB, lista de viñetas ["Se realizó el corte…", …].
- `restricciones`: JSONB, lista de {cat, detalle} con `cat` del catálogo CNC.

`descripcion` se conserva (compatibilidad del panel y de los reportes ya
cargados): se sigue guardando con las viñetas unidas en texto.
"""
from alembic import op

revision = "0032_reporte_estructurado"
down_revision = "0031_padron_unificado"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE campo_reportes ADD COLUMN IF NOT EXISTS area TEXT;
ALTER TABLE campo_reportes ADD COLUMN IF NOT EXISTS turno TEXT NOT NULL DEFAULT 'DIA';
ALTER TABLE campo_reportes ADD COLUMN IF NOT EXISTS anotaciones JSONB;
ALTER TABLE campo_reportes ADD COLUMN IF NOT EXISTS restricciones JSONB;
ALTER TABLE campo_reportes DROP CONSTRAINT IF EXISTS campo_reportes_turno_chk;
ALTER TABLE campo_reportes ADD CONSTRAINT campo_reportes_turno_chk
  CHECK (turno IN ('DIA','NOCHE'));
"""

_DOWN = """
ALTER TABLE campo_reportes DROP CONSTRAINT IF EXISTS campo_reportes_turno_chk;
ALTER TABLE campo_reportes DROP COLUMN IF EXISTS restricciones;
ALTER TABLE campo_reportes DROP COLUMN IF EXISTS anotaciones;
ALTER TABLE campo_reportes DROP COLUMN IF EXISTS turno;
ALTER TABLE campo_reportes DROP COLUMN IF EXISTS area;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
