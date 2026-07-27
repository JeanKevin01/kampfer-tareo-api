# -*- coding: utf-8 -*-
"""El avance real diario recuerda DE QUÉ ACTIVIDAD vino
(auditoría del módulo de programación, encargo de Jean 2026-07-26):

`ev_avances_diarios` guarda el avance por (partida, fecha, etapa) y no sabía de
qué actividad del LookAhead salió. Con una sola actividad por partida-etapa da
igual, pero programar la misma partida en DOS TRAMOS —lo normal en un lookahead
rodante y en obras de misceláneos— hacía que las dos se repartieran mal el mismo
real, en silencio:

  · la cuadrícula mostraba el avance de un tramo también en la fila del otro;
  · al re-prorratear, el real de un tramo le comía el saldo al otro y el
    segundo se quedaba SIN plan (metrado_prog 50, programado vacío);
  · en el PPC las dos se daban por cumplidas con el trabajo de una sola —
    reproducido: 100 m² comprometidos, 50 ejecutados, PPC 100%.

La columna `actividad_id` conserva el dato que ya existía en el gesto del
planner (registra el avance DESDE una actividad) y que se perdía al escribir.
Cuando es NULL (avance cargado por el módulo de Valor Ganado, o histórico) el
API lo atribuye por rango de fechas.

ON DELETE SET NULL a propósito: borrar una actividad del LookAhead no puede
borrar avance real ejecutado — solo deja el registro sin dueño explícito.

Backfill conservador: solo se rellena cuando hay UNA candidata (actividad viva
de la misma partida-etapa cuyo rango cubre la fecha). Con varias se deja NULL y
decide el API; así la migración nunca inventa una atribución.
"""
from alembic import op

revision = "0035_avance_por_actividad"
down_revision = "0034_plazo_y_tipos_vinculo"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE ev_avances_diarios
  ADD COLUMN IF NOT EXISTS actividad_id INT
  REFERENCES prog_actividades(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_avdia_actividad
  ON ev_avances_diarios (actividad_id);

-- Backfill: el hito principal se guarda como NULL en el diario, así que la
-- comparación de etapa se hace sobre COALESCE(hito_id, principal).
WITH principal AS (
  SELECT DISTINCT ON (partida_id) partida_id, id
    FROM ev_hitos ORDER BY partida_id, es_principal DESC, peso DESC, id
), cand AS (
  SELECT d.id AS did, a.id AS aid
    FROM ev_avances_diarios d
    JOIN prog_actividades a
      ON a.partida_id = d.partida_id
     AND a.estado <> 'CANCELADO'
     AND d.fecha BETWEEN a.fecha AND COALESCE(a.fecha_fin, a.fecha)
    LEFT JOIN principal p ON p.partida_id = d.partida_id
   WHERE COALESCE(d.hito_id, p.id) IS NOT DISTINCT FROM COALESCE(a.hito_id, p.id)
), unica AS (
  SELECT did, MIN(aid) AS aid FROM cand GROUP BY did HAVING count(*) = 1
)
UPDATE ev_avances_diarios d SET actividad_id = u.aid
  FROM unica u WHERE u.did = d.id;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_avdia_actividad;
ALTER TABLE ev_avances_diarios DROP COLUMN IF EXISTS actividad_id;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
