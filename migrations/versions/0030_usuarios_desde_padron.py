# -*- coding: utf-8 -*-
"""Usuarios desde el padrón (2026-07-19) — pre-piloto.

Dos cambios aditivos para que los accesos nazcan del personal registrado y
no de texto libre:

1. `usuarios.clave_inicial`: marca al usuario creado con la clave inicial
   (1234) y que aún no la cambió. El panel lo muestra con ⚠ para saber quién
   sigue con la clave de fábrica (decisión de Jean: no se obliga a cambiarla
   —cero fricción en campo— pero se avisa).
2. `supervisores.trabajador_id`: vínculo opcional al padrón de trabajadores.
   Cuando se promueve a un trabajador a supervisor desde el panel Usuarios,
   queda registrado de quién se trata (y el índice único parcial impide
   promover dos veces a la misma persona).
"""
from alembic import op

revision = "0030_usuarios_desde_padron"
down_revision = "0029_reporte_id_local"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS clave_inicial BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE supervisores ADD COLUMN IF NOT EXISTS trabajador_id VARCHAR(5);
ALTER TABLE supervisores DROP CONSTRAINT IF EXISTS fk_supervisores_trabajador;
ALTER TABLE supervisores ADD CONSTRAINT fk_supervisores_trabajador
  FOREIGN KEY (trabajador_id) REFERENCES trabajadores(id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_supervisores_trabajador
  ON supervisores (trabajador_id) WHERE trabajador_id IS NOT NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS uq_supervisores_trabajador;
ALTER TABLE supervisores DROP CONSTRAINT IF EXISTS fk_supervisores_trabajador;
ALTER TABLE supervisores DROP COLUMN IF EXISTS trabajador_id;
ALTER TABLE usuarios DROP COLUMN IF EXISTS clave_inicial;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
