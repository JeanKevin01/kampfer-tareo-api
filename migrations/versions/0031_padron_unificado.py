# -*- coding: utf-8 -*-
"""Padrón unificado (2026-07-19) — todo supervisor es también trabajador.

Regla de Jean: la base de personal es UNA sola (`trabajadores`) e incluye
directos, indirectos y supervisores; ser supervisor es un ROL adicional
(`supervisores.trabajador_id`, migración 0030). Hasta ahora el import bifurcaba:
quien venía con ES_SUPERVISOR=SI se creaba SOLO en `supervisores` y no aparecía
en el padrón de trabajadores.

Este backfill repara a los supervisores ya cargados:
  a) si existe un trabajador con su mismo nombre (comparación tolerante a
     mayúsculas/espacios), los liga — sin duplicar a nadie;
  b) si no existe, le crea su ficha de trabajador (tipo INDIRECTO, cargo
     SUPERVISOR) con el siguiente id correlativo y la liga.

Downgrade real: desliga a todos y borra únicamente las fichas que creó este
backfill —las que no tienen NINGÚN movimiento de tareo ni registro—, para no
destruir datos si alguien ya tareó con ellas.
"""
from alembic import op

revision = "0031_padron_unificado"
down_revision = "0030_usuarios_desde_padron"
branch_labels = None
depends_on = None

_UP = """
-- (a) Ligar supervisores sueltos con el trabajador homónimo ya existente.
UPDATE supervisores s
   SET trabajador_id = t.id
  FROM trabajadores t
 WHERE s.trabajador_id IS NULL
   AND upper(btrim(regexp_replace(t.nombre, '\\s+', ' ', 'g')))
     = upper(btrim(regexp_replace(s.nombre, '\\s+', ' ', 'g')))
   AND NOT EXISTS (SELECT 1 FROM supervisores s2 WHERE s2.trabajador_id = t.id);

-- (b) Crear la ficha de trabajador de los que siguen sin ella.
WITH faltantes AS (
  SELECT s.id,
         s.nombre,
         row_number() OVER (ORDER BY s.id) AS n
    FROM supervisores s
   WHERE s.trabajador_id IS NULL
),
base AS (
  SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) AS maxid
    FROM trabajadores WHERE id ~ '^\\d+$'
),
nuevos AS (
  INSERT INTO trabajadores (id, nombre, cargo, tipo, activo)
  SELECT lpad((base.maxid + f.n)::text, 3, '0'), f.nombre, 'SUPERVISOR', 'INDIRECTO', true
    FROM faltantes f CROSS JOIN base
  RETURNING id, nombre
)
UPDATE supervisores s
   SET trabajador_id = n.id
  FROM nuevos n
 WHERE s.trabajador_id IS NULL AND s.nombre = n.nombre;
"""

_DOWN = """
-- Candidatas: fichas creadas por el backfill y sin ningún movimiento.
CREATE TEMP TABLE _bf_padron ON COMMIT DROP AS
SELECT t.id
  FROM trabajadores t
 WHERE t.cargo = 'SUPERVISOR'
   AND EXISTS (SELECT 1 FROM supervisores s WHERE s.trabajador_id = t.id)
   AND NOT EXISTS (SELECT 1 FROM tareo_partida tp WHERE tp.trabajador_id = t.id)
   AND NOT EXISTS (SELECT 1 FROM registros r WHERE r.trab_id = t.id)
   AND NOT EXISTS (SELECT 1 FROM sesion_trabajadores st WHERE st.trab_id = t.id)
   AND NOT EXISTS (SELECT 1 FROM cuadrilla_otm c WHERE c.trabajador_id = t.id);

-- Soltar el vínculo primero (la FK lo exige) y borrar solo esas fichas;
-- las que ya tienen datos se conservan.
UPDATE supervisores SET trabajador_id = NULL;
DELETE FROM trabajadores WHERE id IN (SELECT id FROM _bf_padron);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
