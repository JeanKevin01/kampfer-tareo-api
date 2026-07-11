# -*- coding: utf-8 -*-
"""Catálogo de fases por proyecto (mejoras UX pre-F4).

`fase` seguirá siendo TEXT libre en las tablas de datos (el RO cruza costo↔meta
por igualdad de string y eso NO cambia); esta tabla aporta los metadatos
(nombre largo, color, orden) para selectores y reportes, y permite crear
fases nuevas en proyectos futuros.

Seed (idempotente, ON CONFLICT DO NOTHING):
  a) fases reales usadas por las partidas de control (ev_partidas.fase es el
     codigo de la partida-padre; el proyecto se deriva vía otms),
  b) fases usadas en documentos de costo que no estén en (a),
  c) las 11 disciplinas de la Guía de Fases del panel (proyecto 1).
"""
from alembic import op

revision = "0018_fases"
down_revision = "0017_valorizaciones"
branch_labels = None
depends_on = None

_UP = r"""
CREATE TABLE fases (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  codigo TEXT NOT NULL,
  nombre TEXT NOT NULL,
  descripcion TEXT,
  color TEXT,
  orden INT NOT NULL DEFAULT 999,
  activo BOOLEAN NOT NULL DEFAULT true,
  creado_en TIMESTAMPTZ DEFAULT now(),
  UNIQUE (proyecto_id, codigo)
);

-- a) fases de las partidas de control, con el nombre del nodo padre si existe
INSERT INTO fases (proyecto_id, codigo, nombre, orden)
SELECT DISTINCT ON (o.proyecto_id, p.fase)
       o.proyecto_id, p.fase,
       COALESCE(pad.descripcion, 'Fase ' || p.fase),
       COALESCE(NULLIF(LEFT(regexp_replace(p.fase, '\D', '', 'g'), 6), '')::int, 999)
FROM ev_partidas p
JOIN otms o ON o.id = p.otm_id
LEFT JOIN ev_partidas pad ON pad.codigo = p.fase
WHERE p.fase IS NOT NULL AND btrim(p.fase) <> ''
ORDER BY o.proyecto_id, p.fase, pad.id
ON CONFLICT (proyecto_id, codigo) DO NOTHING;

-- b) fases usadas en documentos de costo que falten
INSERT INTO fases (proyecto_id, codigo, nombre, orden)
SELECT DISTINCT cd.proyecto_id, cd.fase, 'Fase ' || cd.fase,
       COALESCE(NULLIF(LEFT(regexp_replace(cd.fase, '\D', '', 'g'), 6), '')::int, 999)
FROM costo_documentos cd
WHERE cd.fase IS NOT NULL AND btrim(cd.fase) <> ''
ON CONFLICT (proyecto_id, codigo) DO NOTHING;

-- c) disciplinas de la Guía de Fases (semántica del panel) para el proyecto 1
INSERT INTO fases (proyecto_id, codigo, nombre, descripcion, color, orden)
SELECT 1, v.codigo, v.nombre, v.descripcion, v.color, v.orden
FROM (VALUES
  ('FAB', 'Fabricación en Planta',  'Fabricación de estructuras, componentes y módulos en taller. Incluye arenado y pintura.', '#1D9E75', 101),
  ('EST', 'Montaje de Estructuras', 'Montaje e instalación de estructuras metálicas en campo.', '#3B82F6', 102),
  ('MEC', 'Mecánico / Proceso',     'Instalación y ajuste de equipos mecánicos: poleas, motores, reductores, válvulas, bombas.', '#D85A30', 103),
  ('ELE', 'Eléctrico',              'Tendido de cables, bandejas, tableros y puesta a tierra.', '#BA7517', 104),
  ('TUB', 'Tuberías y Piping',      'Habilitación, montaje y prueba de tuberías de proceso. Tie-In y soportería.', '#7F77DD', 105),
  ('INS', 'Instrumentación',        'Instalación de instrumentos de campo, señales y calibración de lazos.', '#D4537E', 106),
  ('CIV', 'Civil y Geotécnico',     'Excavación, relleno, concreto, anclajes y demoliciones.', '#888780', 107),
  ('AND', 'Andamios y Accesos',     'Instalación, modificación y desinstalación de andamios.', '#0F8C6A', 108),
  ('APY', 'Apoyo Constructivo',     'Recepción de materiales, carguío, transporte interno, ploteo.', '#639922', 109),
  ('ING', 'Ingeniería de Campo',    'Desarrollo de ingenierías menores, planos y topografía.', '#D97706', 110),
  ('COM', 'Pre-comisionado',        'Comisionado, pruebas funcionales y puesta en marcha.', '#7C3ABD', 111)
) AS v(codigo, nombre, descripcion, color, orden)
WHERE EXISTS (SELECT 1 FROM proyectos WHERE id = 1)
ON CONFLICT (proyecto_id, codigo) DO NOTHING;
"""

_DOWN = """
DROP TABLE IF EXISTS fases;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
