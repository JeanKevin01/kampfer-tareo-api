# -*- coding: utf-8 -*-
"""Trabajo no planificado: por qué entró, a quién desplazó, y cuánto costó en HH
(análisis pedido por Jean 2026-07-30).

Desde 0036 el cierre semanal ya detecta el trabajo que entró después del
compromiso y lo deja FUERA del PPC. Esa regla es la correcta —si contara como
cumplida premiaría improvisar, y si contara como no cumplida el supervisor
dejaría de reportarla, que es peor porque se pierde el dato—. El problema es que
la señal se tiraba: «3 no planificadas (fuera del PPC)» no alimentaba ninguna
decisión.

Esta migración trae las cuatro piezas que faltaban:

1. POR QUÉ entró (`no_plan_motivo`). Cuatro motivos, y la distinción importa
   porque disparan acciones opuestas: la omisión del planner es un defecto de
   planificación, el imprevisto se ataca con holgura, el pedido del cliente es
   sustento comercial (no un error), y el ADELANTO no es improvisación en
   absoluto — es resecuenciamiento, y por eso queda fuera del indicador.
   Mezclarlos vuelve el número políticamente inservible: nadie acepta un
   indicador que lo culpa del cambio de prioridad de un cliente.

2. A QUIÉN DESPLAZÓ (`no_plan_desplaza_a`). Cuando entra trabajo no planificado
   la cuadrilla sale de otra parte, y alguna actividad comprometida se cae por
   eso. Hoy la que se cae queda con causa MANO_OBRA y la intrusa en otra lista:
   nadie las une, así que el Pareto dice «nos falta gente» cuando la verdad es
   «nos metieron trabajo que no estaba». Con el vínculo, el Pareto puede afirmar
   «el 40% de nuestros incumplimientos los causó trabajo no planificado, y de
   ese, el 65% era previsible» — que es la frase que cambia comportamiento.

3. EL COMPROMISO CONGELADO (`prog_semana_plan`). Hasta ahora el compromiso se
   DEDUCÍA de `creado_en > referencia`. Es un proxy con dos fallas opuestas: un
   plan acordado el lunes y tecleado el martes se marcaba como no planificado
   (falso positivo), y al cerrar el planner podía DESMARCAR la bandera y
   convertir su propia omisión en compromiso cumplido, inflando el PPC sin
   dejar rastro. Con un acto explícito de «comprometer la semana» el conjunto
   queda congelado y lo que entra después es no planificado por construcción,
   sin inferencia y sin discusión. La deducción por fecha se conserva como
   respaldo para las semanas sin compromiso congelado.

4. LA AUDITORÍA Y LAS HH. `no_plan_auto` guarda lo que el sistema propuso, así
   que un desmarque queda visible junto a `cerrado_por`/`cerrado_en`. Y las HH
   —por actividad y por semana— habilitan el indicador que de verdad habla:
   «el 18% de nuestras horas de esta semana se fueron en trabajo que nadie
   planificó». El conteo de actividades hace pesar igual una de 4 HH y una de
   200; las HH, no.

Sin backfill de motivos: las no planificadas que ya existen quedan con motivo
NULL y el panel las pide clasificar. Poner un motivo por defecto sería inventar
la causa de hechos pasados.
"""
from alembic import op

revision = "0040_no_planificadas"
down_revision = "0039_etiqueta_generica"
branch_labels = None
depends_on = None

_UP = """
-- ── 1 y 2: motivo y desplazamiento, en la actividad ──────────────────────
-- Viven en la actividad y no solo en el cierre porque son verdad permanente de
-- ese trabajo: se pueden clasificar durante la semana, antes de cerrar.
ALTER TABLE prog_actividades
  ADD COLUMN IF NOT EXISTS no_plan_motivo TEXT,
  ADD COLUMN IF NOT EXISTS no_plan_desplaza_a INT
      REFERENCES prog_actividades(id) ON DELETE SET NULL;

ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_act_no_plan_motivo_check;
ALTER TABLE prog_actividades
  ADD CONSTRAINT prog_act_no_plan_motivo_check
  CHECK (no_plan_motivo IS NULL OR no_plan_motivo IN
         ('OMISION_PLANNER', 'EMERGENCIA', 'CLIENTE', 'ADELANTO'));

-- Anti-self: una actividad no puede desplazarse a sí misma.
ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_act_desplaza_no_self;
ALTER TABLE prog_actividades
  ADD CONSTRAINT prog_act_desplaza_no_self
  CHECK (no_plan_desplaza_a IS NULL OR no_plan_desplaza_a <> id);

CREATE INDEX IF NOT EXISTS idx_prog_act_desplaza
  ON prog_actividades (no_plan_desplaza_a) WHERE no_plan_desplaza_a IS NOT NULL;

-- ── 3: el compromiso de la semana, congelado ─────────────────────────────
CREATE TABLE IF NOT EXISTS prog_semana_plan (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  lunes DATE NOT NULL,
  comprometido_en TIMESTAMPTZ NOT NULL DEFAULT now(),
  comprometido_por TEXT,
  nota TEXT,
  UNIQUE (proyecto_id, lunes)
);

-- CASCADE en actividad_id: si la actividad se borra, deja de ser parte del
-- compromiso. No hay historia que perder aquí — la historia del veredicto vive
-- en prog_semana_cierre_det, que sí conserva el título.
CREATE TABLE IF NOT EXISTS prog_semana_plan_det (
  plan_id INT NOT NULL REFERENCES prog_semana_plan(id) ON DELETE CASCADE,
  actividad_id INT NOT NULL REFERENCES prog_actividades(id) ON DELETE CASCADE,
  PRIMARY KEY (plan_id, actividad_id)
);

-- ── 4: auditoría del desmarque y HH, en el cierre congelado ──────────────
ALTER TABLE prog_semana_cierre_det
  ADD COLUMN IF NOT EXISTS no_plan_motivo TEXT,
  -- Sin FK a propósito: es un valor congelado. Si la actividad desplazada se
  -- borra, el cierre debe seguir diciendo a quién desplazó.
  ADD COLUMN IF NOT EXISTS no_plan_desplaza_a INT,
  ADD COLUMN IF NOT EXISTS no_plan_desplaza_tit TEXT,
  -- Lo que el sistema PROPUSO. Si difiere de `no_planificada`, alguien lo
  -- cambió al cerrar: con cerrado_por/cerrado_en queda quién y cuándo.
  ADD COLUMN IF NOT EXISTS no_plan_auto BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS hh NUMERIC(14,2) NOT NULL DEFAULT 0;

ALTER TABLE prog_semana_cierre
  ADD COLUMN IF NOT EXISTS hh_total NUMERIC(14,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS hh_no_planificadas NUMERIC(14,2) NOT NULL DEFAULT 0,
  -- HH de la semana en partidas SIN ninguna actividad: no son planificadas ni
  -- no planificadas, son horas sin plan al que atribuirlas. Se informan aparte
  -- porque si se callaran el indicador quedaría corto sin que nadie lo note.
  ADD COLUMN IF NOT EXISTS hh_sin_actividad NUMERIC(14,2) NOT NULL DEFAULT 0;
"""

_DOWN = """
ALTER TABLE prog_semana_cierre
  DROP COLUMN IF EXISTS hh_sin_actividad,
  DROP COLUMN IF EXISTS hh_no_planificadas,
  DROP COLUMN IF EXISTS hh_total;

ALTER TABLE prog_semana_cierre_det
  DROP COLUMN IF EXISTS hh,
  DROP COLUMN IF EXISTS no_plan_auto,
  DROP COLUMN IF EXISTS no_plan_desplaza_tit,
  DROP COLUMN IF EXISTS no_plan_desplaza_a,
  DROP COLUMN IF EXISTS no_plan_motivo;

DROP TABLE IF EXISTS prog_semana_plan_det;
DROP TABLE IF EXISTS prog_semana_plan;

DROP INDEX IF EXISTS idx_prog_act_desplaza;
ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_act_desplaza_no_self;
ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_act_no_plan_motivo_check;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS no_plan_desplaza_a,
  DROP COLUMN IF EXISTS no_plan_motivo;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
