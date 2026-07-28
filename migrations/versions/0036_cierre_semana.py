# -*- coding: utf-8 -*-
"""Cierre de la semana: el PPC deja de recalcularse hacia atrás
(auditoría del PPC, encargo de Jean 2026-07-27):

El PPC se calculaba SIEMPRE sobre el plan vigente, así que el pasado se movía:

  · reprogramar una actividad que no se hizo BORRABA su compromiso de la semana
    ya cerrada (el DELETE de `_redistribuir` no tiene filtro de fecha) → esa
    actividad salía del denominador y de las no cumplidas, y el PPC de una
    semana cerrada SUBÍA solo. «Si no cumpliste, muévela y se limpia»;
  · el trabajo creado a mitad de semana (el adicional de urgencia) entraba al
    denominador como si se hubiera comprometido el lunes, y al registrarse
    contaba como cumplido: premiaba improvisar;
  · programar hacia atrás reescribía el PPC de semanas pasadas.

En Last Planner el PPC se mide contra el plan COMPROMETIDO, no contra el
vigente. Esta migración trae esa pieza:

  · `prog_semana_cierre` — una semana cerrada: hasta qué día se contó, si el
    corte fue parcial (antes de que la semana termine, como cuando piden el
    reporte el viernes en una obra que trabaja domingo) y quién la cerró.
  · `prog_semana_cierre_det` — el veredicto congelado de cada actividad, con el
    comprometido y el alcanzado del momento del corte, y la causa si no llegó.
  · `prog_config.cierre_dia` / `cierre_semana_siguiente` — CUÁNDO se hace el
    corte, configurable por proyecto: el viernes de la misma semana, el domingo,
    o el lunes de la siguiente. En la experiencia de Jean cada empresa lo pide
    un día distinto, y eso no puede obligar a tocar código.

Lo que esta migración NO hace, a propósito: mover el ancla de la semana. La
semana sigue siendo lunes→domingo en TODOS los módulos (EV, tareo, LookAhead,
curva S). El día de corte dice cuándo se mira, no cuándo empieza la semana —
desalinear eso rompería la comparabilidad entre indicadores.

Sin backfill: las semanas anteriores a esta migración no tienen cierre y el PPC
las sigue calculando como hasta ahora, rotulado como «sobre plan vigente».
"""
from alembic import op

revision = "0036_cierre_semana"
down_revision = "0035_avance_por_actividad"
branch_labels = None
depends_on = None

_UP = """
-- Cuándo se corta el PPC. ISO 1=Lun..7=Dom; por defecto domingo de la misma
-- semana, que es el comportamiento actual (la semana cierra y se evalúa).
ALTER TABLE prog_config
  ADD COLUMN IF NOT EXISTS cierre_dia SMALLINT NOT NULL DEFAULT 7,
  ADD COLUMN IF NOT EXISTS cierre_semana_siguiente BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE prog_config DROP CONSTRAINT IF EXISTS prog_config_cierre_dia_check;
ALTER TABLE prog_config
  ADD CONSTRAINT prog_config_cierre_dia_check CHECK (cierre_dia BETWEEN 1 AND 7);

CREATE TABLE IF NOT EXISTS prog_semana_cierre (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  lunes DATE NOT NULL,
  -- Último día contado. Con corte parcial es anterior al domingo.
  hasta DATE NOT NULL,
  parcial BOOLEAN NOT NULL DEFAULT false,
  comprometidas INT NOT NULL DEFAULT 0,
  cumplidas INT NOT NULL DEFAULT 0,
  no_cumplidas INT NOT NULL DEFAULT 0,
  -- Trabajo que NO estaba comprometido al abrir la semana (se creó después).
  -- No entra al PPC: se informa aparte, y es la medida de cuánto improvisa la
  -- obra — el dato que sustenta el caso del adicional de urgencia.
  no_planificadas INT NOT NULL DEFAULT 0,
  cerrado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  cerrado_por TEXT,
  nota TEXT,
  UNIQUE (proyecto_id, lunes)
);

CREATE TABLE IF NOT EXISTS prog_semana_cierre_det (
  id SERIAL PRIMARY KEY,
  cierre_id INT NOT NULL REFERENCES prog_semana_cierre(id) ON DELETE CASCADE,
  -- ON DELETE CASCADE sería perder historia: si la actividad se borra, el
  -- veredicto de esa semana debe seguir en pie (por eso SET NULL + el título).
  actividad_id INT REFERENCES prog_actividades(id) ON DELETE SET NULL,
  titulo TEXT,
  partida_id INT REFERENCES ev_partidas(id) ON DELETE SET NULL,
  supervisor_id VARCHAR(10) REFERENCES supervisores(id),
  comprometido NUMERIC(14,3) NOT NULL DEFAULT 0,
  alcanzado NUMERIC(14,3) NOT NULL DEFAULT 0,
  cumplida BOOLEAN NOT NULL DEFAULT false,
  -- Trabajo que entró después del compromiso: se guarda para poder medirlo,
  -- marcado, y NO cuenta en el PPC de la semana.
  no_planificada BOOLEAN NOT NULL DEFAULT false,
  causa_cat TEXT,
  causa TEXT
);

CREATE INDEX IF NOT EXISTS idx_cierre_det_cierre
  ON prog_semana_cierre_det (cierre_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cierre_det_act
  ON prog_semana_cierre_det (cierre_id, actividad_id)
  WHERE actividad_id IS NOT NULL;
"""

_DOWN = """
DROP TABLE IF EXISTS prog_semana_cierre_det;
DROP TABLE IF EXISTS prog_semana_cierre;
ALTER TABLE prog_config DROP CONSTRAINT IF EXISTS prog_config_cierre_dia_check;
ALTER TABLE prog_config DROP COLUMN IF EXISTS cierre_semana_siguiente;
ALTER TABLE prog_config DROP COLUMN IF EXISTS cierre_dia;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
