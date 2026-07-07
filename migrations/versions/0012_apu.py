# -*- coding: utf-8 -*-
"""F1.1: esquema APU + dos líneas base (META y CONTRACTUAL).

- presupuestos.tipo: 'META' (costo interno, del Excel PU Rev01) | 'CONTRACTUAL' (venta).
  Los presupuestos existentes quedan CONTRACTUAL (correcto: hoy gobiernan PU/venta).
- El índice de vigencia pasa a ser por (proyecto, tipo): puede haber UN vigente de cada tipo.
- presupuesto_partidas gana columnas del APU (rendimientos, hh/día, jerarquía).
- apu_recursos: el detalle MO/MAT/EQ/SUB de cada partida del presupuesto.
- presupuesto_costo_meta: materialización del costo meta por (fase, tipo de recurso)
  que consumirá el RO (F2) como columna "Meta".

Downgrade = drops espejo (revierte TODO lo de arriba).
"""
from alembic import op

revision = "0012_apu"
down_revision = "0011_usuarios_supervisor"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE presupuestos ADD COLUMN tipo TEXT NOT NULL DEFAULT 'CONTRACTUAL'
  CHECK (tipo IN ('META','CONTRACTUAL'));
ALTER TABLE presupuestos ADD COLUMN moneda TEXT NOT NULL DEFAULT 'PEN';
ALTER TABLE presupuestos ADD COLUMN tipo_cambio NUMERIC(8,4);

-- La versión también es única por tipo ahora (META y CONTRACTUAL numeran aparte).
ALTER TABLE presupuestos DROP CONSTRAINT presupuestos_proyecto_id_version_key;
ALTER TABLE presupuestos ADD CONSTRAINT uq_presupuesto_tipo_version
  UNIQUE (proyecto_id, tipo, version);

DROP INDEX uq_presupuesto_vigente;
CREATE UNIQUE INDEX uq_presupuesto_vigente ON presupuestos (proyecto_id, tipo) WHERE vigente;

ALTER TABLE presupuesto_partidas
  ADD COLUMN rendimiento_mo NUMERIC(12,4),
  ADD COLUMN rendimiento_eq NUMERIC(12,4),
  ADD COLUMN hh_dia NUMERIC(4,1) DEFAULT 10,
  ADD COLUMN area TEXT,
  ADD COLUMN nivel INT DEFAULT 1,
  ADD COLUMN parent_codigo TEXT;

CREATE TABLE apu_recursos (
  id SERIAL PRIMARY KEY,
  presupuesto_partida_id INT NOT NULL REFERENCES presupuesto_partidas(id) ON DELETE CASCADE,
  tipo TEXT NOT NULL CHECK (tipo IN ('MO','MAT','EQ','SUB')),   -- SUB = subpartida anidada
  codigo TEXT, descripcion TEXT NOT NULL, unidad TEXT,
  cuadrilla NUMERIC(8,4), cantidad NUMERIC(14,6) NOT NULL DEFAULT 0,
  precio NUMERIC(14,6) NOT NULL DEFAULT 0, parcial NUMERIC(14,4) NOT NULL DEFAULT 0,
  sub_partida_id INT REFERENCES presupuesto_partidas(id),        -- solo tipo='SUB'
  orden INT NOT NULL DEFAULT 0
);
CREATE INDEX idx_apu_pp ON apu_recursos (presupuesto_partida_id);

CREATE TABLE presupuesto_costo_meta (
  id SERIAL PRIMARY KEY,
  presupuesto_id INT NOT NULL REFERENCES presupuestos(id) ON DELETE CASCADE,
  fase TEXT, tipo_recurso TEXT NOT NULL CHECK (tipo_recurso IN ('MO','MAT','EQ','SUB')),
  monto NUMERIC(14,2) NOT NULL DEFAULT 0,
  UNIQUE (presupuesto_id, fase, tipo_recurso)
);
"""

_DOWN = """
DROP TABLE IF EXISTS presupuesto_costo_meta;
DROP TABLE IF EXISTS apu_recursos;
ALTER TABLE presupuesto_partidas
  DROP COLUMN IF EXISTS rendimiento_mo,
  DROP COLUMN IF EXISTS rendimiento_eq,
  DROP COLUMN IF EXISTS hh_dia,
  DROP COLUMN IF EXISTS area,
  DROP COLUMN IF EXISTS nivel,
  DROP COLUMN IF EXISTS parent_codigo;
DROP INDEX IF EXISTS uq_presupuesto_vigente;
CREATE UNIQUE INDEX uq_presupuesto_vigente ON presupuestos (proyecto_id) WHERE vigente;
ALTER TABLE presupuestos DROP CONSTRAINT IF EXISTS uq_presupuesto_tipo_version;
ALTER TABLE presupuestos ADD CONSTRAINT presupuestos_proyecto_id_version_key
  UNIQUE (proyecto_id, version);
ALTER TABLE presupuestos DROP COLUMN IF EXISTS tipo_cambio;
ALTER TABLE presupuestos DROP COLUMN IF EXISTS moneda;
ALTER TABLE presupuestos DROP COLUMN IF EXISTS tipo;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
