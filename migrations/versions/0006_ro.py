"""Fase 2: RO (Resultado Operativo) — costos por recurso/fase + ajustes de venta

Decisiones de negocio aprobadas:
  · Costos NO-MO (Material, Equipo Propio/Terceros, Subcontratos, Dirección, GG) → tabla `costos`,
    cargados por FASE y mes (espejo del Excel RO). La Mano de Obra del costo sale del tareo
    (HH × tarifa), no de esta tabla.
  · Venta = (cantidad valorizada × PU del presupuesto) + ajustes (`venta_ajustes`:
    contractual / adicional / reajuste / terceros).
  · Alcance inicial: RO "a la fecha" (Venta − Costo = Margen por fase). La proyección a fin de
    obra y el APU por recurso vienen en un sub-paso posterior.

Revision ID: 0006_ro
Revises: 0005_presupuesto
Create Date: 2026-06-29
"""
from alembic import op

revision = "0006_ro"
down_revision = "0005_presupuesto"
branch_labels = None
depends_on = None

_UP = """
-- Costos NO-MO (la MO sale del tareo). fase NULL = costo indirecto a nivel proyecto (DIR/GG).
CREATE TABLE costos (
    id           SERIAL PRIMARY KEY,
    proyecto_id  INTEGER NOT NULL REFERENCES proyectos(id),
    fase         TEXT,
    tipo_recurso TEXT NOT NULL,
    directo      BOOLEAN NOT NULL DEFAULT true,
    periodo      DATE NOT NULL,
    monto        NUMERIC(14,2) NOT NULL DEFAULT 0,
    moneda       TEXT NOT NULL DEFAULT 'PEN',
    fuente       TEXT,
    nota         TEXT,
    creado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT costos_tipo_check CHECK (tipo_recurso IN ('MAT','EQP','EQT','SUB','DIR','GG'))
);
CREATE INDEX idx_costos_proy_fase ON costos (proyecto_id, fase);
CREATE INDEX idx_costos_periodo   ON costos (periodo);

-- Ajustes de venta que no salen del valorizado×PU.
CREATE TABLE venta_ajustes (
    id          SERIAL PRIMARY KEY,
    proyecto_id INTEGER NOT NULL REFERENCES proyectos(id),
    fase        TEXT,
    tipo        TEXT NOT NULL,
    periodo     DATE,
    monto       NUMERIC(14,2) NOT NULL DEFAULT 0,
    nota        TEXT,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT venta_ajustes_tipo_check CHECK (tipo IN ('CONTRACTUAL','ADICIONAL','REAJUSTE','TERCEROS'))
);
CREATE INDEX idx_venta_ajustes_proy ON venta_ajustes (proyecto_id);
"""

_DOWN = """
DROP TABLE IF EXISTS venta_ajustes;
DROP TABLE IF EXISTS costos;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
