# -*- coding: utf-8 -*-
"""El metrado comprometido se congela, y el congelar/cerrar deja bitácora
(defectos D1 y D2 del plan maestro v8, encargo de Jean 2026-08-01):

D1 — Reprogramar borraba el compromiso de una semana pasada. `_redistribuir()`
borra las celdas de `prog_metrado_dia` que va a recalcular y ese DELETE no tiene
filtro de fecha: al correr la F.Inicio de una actividad que no se hizo, los días
de la semana anterior salen del rango vigente, no están en `intactos`, y sus
celdas desaparecen. 0036 salvó las semanas CERRADAS (el veredicto vive en
`prog_semana_cierre_det`) y 0040 congeló QUÉ actividades se comprometieron
(`prog_semana_plan_det`), pero no CUÁNTO: el denominador del PPC seguía saliendo
de `prog_metrado_dia`, así que mover la fecha dejaba la actividad con
comprometido 0 y la sacaba del indicador. Es decir: *si no cumpliste, muévela y
el PPC se limpia solo* — silencioso, y premia justo lo contrario de lo que el
Last Planner quiere medir.

    prog_semana_plan_det.metrado  ← lo que faltaba

Con el metrado congelado el denominador de una semana comprometida deja de
depender del plan de hoy, y con eso cae también D2 (programar hacia atrás ya no
reescribe el PPC de una semana abierta: lo que entra después no está en el
compromiso).

`metrado` NUMERIC(14,3) igual que `prog_semana_cierre_det.comprometido` y que
`prog_metrado_dia.cantidad`: si el congelado se redondeara distinto, el PPC
congelado no cuadraría con el que el planner acababa de ver.

SIN BACKFILL DEL METRADO, a propósito. Los compromisos que ya existen quedan en
0 y el cálculo cae al plan vigente — reconstruirlos desde `prog_metrado_dia`
sería inventar el pasado: hoy esa tabla contiene el plan de HOY, que es
precisamente lo que puede haber cambiado. `0` significa «no se congeló el
metrado», no «se comprometió cero», y así lo lee `denominador_comprometido()`.

── Bitácora (`prog_semana_eventos`) ────────────────────────────────────────────
Hoy no queda rastro de cuándo se congeló una semana: `prog_semana_plan` tiene
UNIQUE(proyecto_id, lunes) y el ON CONFLICT DO UPDATE pisa `comprometido_en`, y
descomprometer BORRA la fila. Cerrar y reabrir dejan un `log.info` que vive en
el contenedor y se pierde en el próximo redeploy. Para un indicador que sostiene
una tesis eso no alcanza: hay que poder responder «¿cuándo se comprometió esta
semana, quién la reabrió y qué cambió entre un congelado y el otro?».

La tabla es APPEND-ONLY: nunca se actualiza ni se borra una fila. Re-comprometer
deja dos eventos y la comparación entre ambos es la historia. `detalle` guarda la
foto del momento (actividad, título y metrado) para que el evento siga siendo
legible aunque la actividad se borre después.

Backfill SÍ para los eventos existentes: `comprometido_en` y `cerrado_en` son
fechas reales guardadas en BD, copiarlas no inventa nada. Van marcadas con
`detalle->>'backfill' = true` para que nadie las confunda con eventos capturados
en vivo (su `detalle` no tiene la foto por actividad: esa no existía).
"""
from alembic import op

revision = "0041_metrado_comprometido"
down_revision = "0040_no_planificadas"
branch_labels = None
depends_on = None

_UP = """
-- ── D1: el CUÁNTO del compromiso, junto al QUÉ que ya congelaba 0040 ─────
ALTER TABLE prog_semana_plan_det
  ADD COLUMN IF NOT EXISTS metrado NUMERIC(14,3) NOT NULL DEFAULT 0;

-- ── Bitácora de congelamiento (append-only) ──────────────────────────────
CREATE TABLE IF NOT EXISTS prog_semana_eventos (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  lunes DATE NOT NULL,
  evento TEXT NOT NULL,
  -- Quién lo hizo, tal como lo mandó el panel. TEXT y no FK a supervisores
  -- porque quien compromete y cierra es oficina, que no está en ese padrón.
  actor TEXT,
  n_actividades INT NOT NULL DEFAULT 0,
  metrado NUMERIC(14,3) NOT NULL DEFAULT 0,
  -- Solo en CERRADA: el PPC que quedó congelado. Permite leer la bitácora
  -- como serie sin volver a cruzar con prog_semana_cierre.
  ppc NUMERIC(6,4),
  nota TEXT,
  -- Foto del momento: [{id, titulo, metrado}]. Sin FK y sin CASCADE — es
  -- historia, y tiene que sobrevivir al borrado de la actividad.
  detalle JSONB,
  creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE prog_semana_eventos DROP CONSTRAINT IF EXISTS prog_semana_eventos_evento_check;
ALTER TABLE prog_semana_eventos
  ADD CONSTRAINT prog_semana_eventos_evento_check
  CHECK (evento IN ('COMPROMETIDA', 'DESCOMPROMETIDA', 'CERRADA', 'REABIERTA'));

-- (proyecto, lunes, id): la consulta natural es «la historia de esta semana en
-- orden»; el id desempata dos eventos del mismo instante mejor que creado_en.
CREATE INDEX IF NOT EXISTS idx_prog_sem_ev_semana
  ON prog_semana_eventos (proyecto_id, lunes, id);
CREATE INDEX IF NOT EXISTS idx_prog_sem_ev_reciente
  ON prog_semana_eventos (proyecto_id, creado_en DESC);

-- ── Backfill de lo que YA pasó (fechas reales, no inventadas) ────────────
INSERT INTO prog_semana_eventos
  (proyecto_id, lunes, evento, actor, n_actividades, nota, detalle, creado_en)
SELECT p.proyecto_id, p.lunes, 'COMPROMETIDA', p.comprometido_por,
       (SELECT count(*) FROM prog_semana_plan_det d WHERE d.plan_id = p.id),
       p.nota, '{"backfill": true}'::jsonb, p.comprometido_en
  FROM prog_semana_plan p
 WHERE NOT EXISTS (SELECT 1 FROM prog_semana_eventos e
                    WHERE e.proyecto_id = p.proyecto_id AND e.lunes = p.lunes
                      AND e.evento = 'COMPROMETIDA');

INSERT INTO prog_semana_eventos
  (proyecto_id, lunes, evento, actor, n_actividades, ppc, nota, detalle, creado_en)
SELECT c.proyecto_id, c.lunes, 'CERRADA', c.cerrado_por, c.comprometidas,
       CASE WHEN c.comprometidas > 0
            THEN round(c.cumplidas::numeric / c.comprometidas, 4) END,
       c.nota, '{"backfill": true}'::jsonb, c.cerrado_en
  FROM prog_semana_cierre c
 WHERE NOT EXISTS (SELECT 1 FROM prog_semana_eventos e
                    WHERE e.proyecto_id = c.proyecto_id AND e.lunes = c.lunes
                      AND e.evento = 'CERRADA');
"""

_DOWN = """
DROP INDEX IF EXISTS idx_prog_sem_ev_reciente;
DROP INDEX IF EXISTS idx_prog_sem_ev_semana;
DROP TABLE IF EXISTS prog_semana_eventos;
ALTER TABLE prog_semana_plan_det DROP COLUMN IF EXISTS metrado;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
