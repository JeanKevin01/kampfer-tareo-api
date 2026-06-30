"""Fase 1: presupuesto gobernado (versionable + congelable) + precio_unitario en ev_partidas

Modelo (decisión de negocio aprobada):
  · presupuestos          → el documento, por proyecto, con version y estado (BORRADOR|CONGELADO).
                            Solo UNA versión 'vigente' por proyecto (índice parcial único).
  · presupuesto_partidas  → las líneas de esa versión (metrado, precio_unitario, hh_meta).
  · ev_partidas.precio_unitario (NUEVA) → para la venta del RO (cantidad × PU). DEFAULT 0.

Al "congelar+activar" una versión, la lógica de la API copiará metrado/hh_meta/PU a ev_partidas
(match por código). El motor de Valor Ganado NO cambia: sigue leyendo ev_partidas.

Revision ID: 0005_presupuesto
Revises: 0004_tenant
Create Date: 2026-06-29
"""
from alembic import op

revision = "0005_presupuesto"
down_revision = "0004_tenant"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE presupuestos (
    id            SERIAL PRIMARY KEY,
    proyecto_id   INTEGER NOT NULL REFERENCES proyectos(id),
    version       INTEGER NOT NULL,
    estado        TEXT NOT NULL DEFAULT 'BORRADOR',
    vigente       BOOLEAN NOT NULL DEFAULT false,
    nota          TEXT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    congelado_en  TIMESTAMPTZ,
    congelado_por TEXT,
    CONSTRAINT presupuestos_estado_check CHECK (estado IN ('BORRADOR', 'CONGELADO')),
    UNIQUE (proyecto_id, version)
);
-- Solo puede haber UN presupuesto vigente por proyecto.
CREATE UNIQUE INDEX uq_presupuesto_vigente ON presupuestos (proyecto_id) WHERE vigente;

CREATE TABLE presupuesto_partidas (
    id              SERIAL PRIMARY KEY,
    presupuesto_id  INTEGER NOT NULL REFERENCES presupuestos(id) ON DELETE CASCADE,
    codigo          TEXT NOT NULL,
    descripcion     TEXT,
    unidad          TEXT,
    fase            TEXT,
    sub_fase        TEXT,
    metrado         NUMERIC(14,4) NOT NULL DEFAULT 0,
    precio_unitario NUMERIC(14,4) NOT NULL DEFAULT 0,
    hh_meta         NUMERIC(12,2) NOT NULL DEFAULT 0,
    UNIQUE (presupuesto_id, codigo)
);
CREATE INDEX idx_pp_presupuesto ON presupuesto_partidas (presupuesto_id);

-- Precio unitario en la partida de ejecución (lo llena el congelado; venta = cantidad × PU).
ALTER TABLE ev_partidas ADD COLUMN precio_unitario NUMERIC(14,4) NOT NULL DEFAULT 0;
"""

_DOWN = """
ALTER TABLE ev_partidas DROP COLUMN IF EXISTS precio_unitario;
DROP TABLE IF EXISTS presupuesto_partidas;
DROP TABLE IF EXISTS presupuestos;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
