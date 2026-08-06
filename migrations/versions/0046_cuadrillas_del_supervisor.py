# -*- coding: utf-8 -*-
"""Una sola cuadrilla: la del supervisor, con varias listas nombradas.

(encargo de Jean 2026-08-06, tras el diagnóstico de por qué crear una cuadrilla
desde el panel «no hacía nada» en el tareo)

En el esquema convivían TRES modelos de cuadrilla y cada mitad del sistema
escribía en uno distinto:

  · `cuadrillas`      (supervisor, trabajador) — una lista plana. La escribía el
                      panel y `admin.html`. No la leía NADIE.
  · `cuadrilla_otm`   (supervisor, OTM, nombre, trabajador) — la escribía y leía
                      solo la app de campo.
  · `cuadrilla_grupos`+`_miembros` (supervisor, nombre) — el modelo correcto,
                      construido entero y abandonado: ni un solo lector.

De ahí el síntoma: la cuadrilla del panel SÍ se guardaba, en una tabla que el
tareo no consulta. Dos circuitos cerrados que no se cruzan en ningún punto.

Esta migración elige `cuadrilla_grupos` como fuente única —es el único de los
tres que ya modela lo que Jean pidió, varias cuadrillas por supervisor— y le
pone lo que le faltaba para servir de verdad:

  · `orden` en los miembros: la app de campo ya guardaba la posición y el modelo
    de grupos la perdía. Sin ella la lista se reordena sola en cada lectura.
  · las dos FK que nunca tuvo. `cuadrilla_otm` y `cuadrillas` sí referencian a
    supervisores y trabajadores con CASCADE; los grupos no, así que dar de baja
    a alguien dejaba miembros fantasma. Se pueden añadir sin riesgo justamente
    porque la tabla está vacía y el backfill de abajo filtra lo que no existe.

BACKFILL, y aquí sí (a diferencia de 0041/0044/0045): lo que se copia no es un
sello de captura convertido en declaración, sino la MISMA lista de personas
cambiando de tabla. No copiarla significaría que los supervisores pierden las
cuadrillas que ya tenían guardadas en el teléfono.

Consolidar (supervisor, OTM, nombre) en (supervisor, nombre) puede chocar: un
supervisor con «Principal» en dos proyectos y gente distinta en cada uno. Unir
las dos listas metería en una cuadrilla a personas que nunca estuvieron; el
backfill desambigua con el proyecto («Principal · OTM-123») solo cuando el
nombre se repite entre OTMs, y deja el nombre limpio cuando no.

Las tablas viejas NO se borran aquí, a propósito. Entre `alembic upgrade` y el
redeploy del API hay una ventana en la que el contenedor todavía en marcha lee
`cuadrilla_otm`; borrarla la convierte en caída. La limpieza va en una migración
posterior, cuando el redeploy esté verificado (expand/contract).

DOWNGRADE: revierte el esquema entero (columna, FKs, índice). No borra los
grupos backfilleados y es deliberado — las tablas de origen siguen intactas, así
que volver atrás no pierde nada, mientras que un DELETE se llevaría por delante
las cuadrillas creadas después del upgrade.
"""
from alembic import op

revision = "0046_cuadrillas_del_supervisor"
down_revision = "0045_restriccion_detectada"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE cuadrilla_grupo_miembros
  ADD COLUMN IF NOT EXISTS orden INT NOT NULL DEFAULT 0;

-- ── Backfill 1: las cuadrillas que los supervisores ya tienen en el teléfono ──
-- Un grupo por (supervisor, nombre). El nombre lleva el proyecto pegado SOLO si
-- el mismo nombre se usó en más de una OTM (listas distintas que no deben
-- fundirse). left(...,100) porque cuadrilla_otm.nombre no tiene tope y
-- cuadrilla_grupos.nombre es varchar(100).
WITH origen AS (
    SELECT DISTINCT supervisor_id, otm_id, nombre
      FROM cuadrilla_otm
     WHERE activo IS NOT FALSE
), etiquetado AS (
    SELECT supervisor_id, otm_id, nombre,
           left(CASE WHEN COUNT(*) OVER (PARTITION BY supervisor_id, nombre) > 1
                     THEN nombre || ' · ' || otm_id
                     ELSE nombre END, 100) AS nombre_final
      FROM origen
)
INSERT INTO cuadrilla_grupos (supervisor_id, nombre)
SELECT DISTINCT e.supervisor_id, e.nombre_final
  FROM etiquetado e
 WHERE EXISTS (SELECT 1 FROM supervisores s WHERE s.id = e.supervisor_id)
   AND e.nombre_final <> ''
ON CONFLICT (supervisor_id, nombre) DO NOTHING;

WITH origen AS (
    SELECT DISTINCT supervisor_id, otm_id, nombre
      FROM cuadrilla_otm
     WHERE activo IS NOT FALSE
), etiquetado AS (
    SELECT supervisor_id, otm_id, nombre,
           left(CASE WHEN COUNT(*) OVER (PARTITION BY supervisor_id, nombre) > 1
                     THEN nombre || ' · ' || otm_id
                     ELSE nombre END, 100) AS nombre_final
      FROM origen
)
INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden)
SELECT g.id, c.trabajador_id, COALESCE(c.orden, 0)
  FROM cuadrilla_otm c
  JOIN etiquetado e
    ON e.supervisor_id = c.supervisor_id
   AND e.otm_id        = c.otm_id
   AND e.nombre        = c.nombre
  JOIN cuadrilla_grupos g
    ON g.supervisor_id = c.supervisor_id
   AND g.nombre        = e.nombre_final
 WHERE c.activo IS NOT FALSE
   AND EXISTS (SELECT 1 FROM trabajadores t WHERE t.id = c.trabajador_id)
ON CONFLICT (grupo_id, trab_id) DO NOTHING;

-- ── Backfill 2: la lista plana que se editaba desde el panel ─────────────────
-- Es lo único que había en `cuadrillas`; se rescata como una cuadrilla más.
INSERT INTO cuadrilla_grupos (supervisor_id, nombre)
SELECT DISTINCT c.supervisor_id, 'Cuadrilla habitual'
  FROM cuadrillas c
 WHERE EXISTS (SELECT 1 FROM supervisores s WHERE s.id = c.supervisor_id)
ON CONFLICT (supervisor_id, nombre) DO NOTHING;

INSERT INTO cuadrilla_grupo_miembros (grupo_id, trab_id, orden)
SELECT g.id, c.trab_id, 0
  FROM cuadrillas c
  JOIN cuadrilla_grupos g
    ON g.supervisor_id = c.supervisor_id
   AND g.nombre        = 'Cuadrilla habitual'
 WHERE EXISTS (SELECT 1 FROM trabajadores t WHERE t.id = c.trab_id)
ON CONFLICT (grupo_id, trab_id) DO NOTHING;

-- ── Integridad que a estas dos tablas les faltaba ────────────────────────────
CREATE INDEX IF NOT EXISTS idx_cgm_trab
    ON cuadrilla_grupo_miembros (trab_id);

ALTER TABLE cuadrilla_grupos
  DROP CONSTRAINT IF EXISTS cuadrilla_grupos_supervisor_id_fkey;
ALTER TABLE cuadrilla_grupos
  ADD CONSTRAINT cuadrilla_grupos_supervisor_id_fkey
  FOREIGN KEY (supervisor_id) REFERENCES supervisores(id) ON DELETE CASCADE;

ALTER TABLE cuadrilla_grupo_miembros
  DROP CONSTRAINT IF EXISTS cuadrilla_grupo_miembros_trab_id_fkey;
ALTER TABLE cuadrilla_grupo_miembros
  ADD CONSTRAINT cuadrilla_grupo_miembros_trab_id_fkey
  FOREIGN KEY (trab_id) REFERENCES trabajadores(id) ON DELETE CASCADE;
"""

_DOWN = """
ALTER TABLE cuadrilla_grupo_miembros
  DROP CONSTRAINT IF EXISTS cuadrilla_grupo_miembros_trab_id_fkey;
ALTER TABLE cuadrilla_grupos
  DROP CONSTRAINT IF EXISTS cuadrilla_grupos_supervisor_id_fkey;
DROP INDEX IF EXISTS idx_cgm_trab;
ALTER TABLE cuadrilla_grupo_miembros
  DROP COLUMN IF EXISTS orden;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
