# -*- coding: utf-8 -*-
"""F2.4: venta completa estilo T OBRA (+ F2.5 contingencia del proyecto).

- venta_ajustes gana periodo_id (FK a periodos) y margen_previsto; el CHECK se
  amplía a los 6 conceptos del T OBRA. Los 'ADICIONAL' existentes migran a
  'NUEVAS_PARTIDAS' (el POST acepta el alias viejo una versión).
- Backfill: los ajustes con fecha en `periodo` quedan atados a su periodo mensual
  (se crean los periodos que falten).
- proyectos.contingencia (F2.5): monto que castiga el margen del RO.
"""
from alembic import op

revision = "0015_venta"
down_revision = "0014_costo_docs"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE venta_ajustes ADD COLUMN periodo_id INT REFERENCES periodos(id);
ALTER TABLE venta_ajustes ADD COLUMN margen_previsto NUMERIC(14,2);

ALTER TABLE venta_ajustes DROP CONSTRAINT venta_ajustes_tipo_check;
UPDATE venta_ajustes SET tipo = 'NUEVAS_PARTIDAS' WHERE tipo = 'ADICIONAL';
ALTER TABLE venta_ajustes ADD CONSTRAINT venta_ajustes_tipo_check CHECK (tipo IN
  ('CONTRACTUAL','DIF_METRADO','NUEVAS_PARTIDAS','POR_APROBAR','REAJUSTE','TERCEROS'));

-- Backfill de periodo_id desde la fecha `periodo` (creando meses que falten)
INSERT INTO periodos (proyecto_id, anio, mes)
SELECT DISTINCT v.proyecto_id, EXTRACT(YEAR FROM v.periodo)::int, EXTRACT(MONTH FROM v.periodo)::int
FROM venta_ajustes v WHERE v.periodo IS NOT NULL
ON CONFLICT (proyecto_id, anio, mes) DO NOTHING;

UPDATE venta_ajustes v SET periodo_id = p.id
FROM periodos p
WHERE v.periodo IS NOT NULL AND p.proyecto_id = v.proyecto_id
  AND p.anio = EXTRACT(YEAR FROM v.periodo)::int
  AND p.mes  = EXTRACT(MONTH FROM v.periodo)::int;

ALTER TABLE proyectos ADD COLUMN contingencia NUMERIC(14,2) NOT NULL DEFAULT 0;
"""

_DOWN = """
ALTER TABLE proyectos DROP COLUMN IF EXISTS contingencia;
ALTER TABLE venta_ajustes DROP CONSTRAINT IF EXISTS venta_ajustes_tipo_check;
UPDATE venta_ajustes SET tipo = 'ADICIONAL'
 WHERE tipo IN ('NUEVAS_PARTIDAS','DIF_METRADO','POR_APROBAR');
ALTER TABLE venta_ajustes ADD CONSTRAINT venta_ajustes_tipo_check CHECK (tipo IN
  ('CONTRACTUAL','ADICIONAL','REAJUSTE','TERCEROS'));
ALTER TABLE venta_ajustes DROP COLUMN IF EXISTS margen_previsto;
ALTER TABLE venta_ajustes DROP COLUMN IF EXISTS periodo_id;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
