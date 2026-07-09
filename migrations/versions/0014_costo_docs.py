# -*- coding: utf-8 -*-
"""F2.2: documentos de costo (nivel factura/OC/vale) + migración desde `costos`.

`costo_documentos` reemplaza a la tabla agregada `costos`: cada fila es un
documento con proveedor/número/fecha, atado a un PERIODO (F2.1). El RO agrega
desde aquí. La migración de datos convierte cada fila vieja en un documento
tipo 'OTRO' fuente 'migracion-0006' (creando los periodos que falten) y
elimina `costos`. Downgrade: recrea `costos` y devuelve esas filas migradas.
"""
from alembic import op

revision = "0014_costo_docs"
down_revision = "0013_periodos"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE costo_documentos (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  periodo_id INT NOT NULL REFERENCES periodos(id),
  tipo_doc TEXT NOT NULL DEFAULT 'FACTURA'
    CHECK (tipo_doc IN ('FACTURA','OC','VALE','PLANILLA_AJUSTE','OTRO')),
  proveedor TEXT, numero_doc TEXT, fecha DATE,
  tipo_recurso TEXT NOT NULL CHECK (tipo_recurso IN ('MAT','EQP','EQT','SUB','DIR','GG','MO')),
  directo BOOLEAN NOT NULL DEFAULT true,
  fase TEXT,
  partida_id INT REFERENCES ev_partidas(id),
  moneda TEXT NOT NULL DEFAULT 'PEN',
  monto NUMERIC(14,2) NOT NULL,
  glosa TEXT,
  fuente TEXT NOT NULL DEFAULT 'manual',   -- manual | import:<hash> | migracion-0006 | ajuste-mo
  creado_por TEXT,
  creado_en TIMESTAMPTZ DEFAULT now(),
  CONSTRAINT mo_solo_ajuste CHECK (tipo_recurso <> 'MO' OR tipo_doc = 'PLANILLA_AJUSTE')
);
CREATE INDEX idx_cdoc_proy_per ON costo_documentos (proyecto_id, periodo_id);

-- Migración de datos: periodos que falten + cada costo → documento 'OTRO'
INSERT INTO periodos (proyecto_id, anio, mes)
SELECT DISTINCT c.proyecto_id, EXTRACT(YEAR FROM c.periodo)::int, EXTRACT(MONTH FROM c.periodo)::int
FROM costos c
ON CONFLICT (proyecto_id, anio, mes) DO NOTHING;

INSERT INTO costo_documentos
  (proyecto_id, periodo_id, tipo_doc, fecha, tipo_recurso, directo, fase,
   moneda, monto, glosa, fuente, creado_en)
SELECT c.proyecto_id, p.id, 'OTRO', c.periodo, c.tipo_recurso, c.directo, c.fase,
       c.moneda, c.monto, c.nota, 'migracion-0006', c.creado_en
FROM costos c
JOIN periodos p ON p.proyecto_id = c.proyecto_id
  AND p.anio = EXTRACT(YEAR FROM c.periodo)::int
  AND p.mes  = EXTRACT(MONTH FROM c.periodo)::int;

DROP TABLE costos;
"""

_DOWN = """
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

INSERT INTO costos (proyecto_id, fase, tipo_recurso, directo, periodo, monto, moneda, fuente, nota, creado_en)
SELECT cd.proyecto_id, cd.fase, cd.tipo_recurso, cd.directo,
       COALESCE(cd.fecha, make_date(p.anio, p.mes, 1)), cd.monto, cd.moneda,
       NULL, cd.glosa, COALESCE(cd.creado_en, now())
FROM costo_documentos cd
JOIN periodos p ON p.id = cd.periodo_id
WHERE cd.fuente = 'migracion-0006';

DROP TABLE costo_documentos;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
