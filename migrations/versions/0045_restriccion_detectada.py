# -*- coding: utf-8 -*-
"""Fecha de DETECCIÓN de la restricción (encargo de Jean 2026-08-02, 2ª vuelta).

La 0044 arregló el lado de la salida: `liberada_en` (cuándo se marcó) frente a
`liberada_el` (cuándo se liberó de verdad). El lado de la ENTRADA tenía el mismo
defecto y quedó sin tocar: `creado_en` es cuándo alguien tecleó la restricción en
el sistema, no cuándo apareció el problema. Si el planner registra el viernes
cinco cosas que se detectaron el lunes, esa columna mide otra vez su hábito de
captura.

`detectada_el` (DATE, editable) cierra la simetría y desbloquea dos preguntas que
hoy no se pueden responder:

  · ANTIGÜEDAD de lo pendiente: «esta lleva 23 días abierta». Distinto de estar
    vencida — una restricción puede llevar un mes viva y no estar vencida todavía
    porque la fecha que pidió el planner aún no llega.
  · DURACIÓN real de las liberadas: `liberada_el − detectada_el`. Distinto de la
    latencia (`liberada_el − fecha_requerida`), que mide si el responsable
    cumplió el plazo PEDIDO. Una cosa es «llegó 2 días tarde» y otra «esto nos
    estuvo bloqueando 40 días»; las dos importan y no son la misma.

SIN BACKFILL, por la tercera vez y por la misma razón que 0041 y 0044: copiar
`creado_en` a `detectada_el` convertiría un sello de captura en una declaración
que nadie hizo. Las filas viejas se quedan en NULL y la lectura cae al sello
MARCÁNDOLO como derivado, igual que ya hace con la liberación.

La traza append-only también guarda las dos columnas nuevas: si no, una
restricción borrada perdería su duración, que es justo lo que la traza existe
para conservar.
"""
from alembic import op

revision = "0045_restriccion_detectada"
down_revision = "0044_fiabilidad_restricciones"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_restricciones
  ADD COLUMN IF NOT EXISTS detectada_el DATE;

ALTER TABLE prog_restriccion_eventos
  ADD COLUMN IF NOT EXISTS detectada_el  DATE,
  ADD COLUMN IF NOT EXISTS duracion_dias INT;

-- La bandeja de pendientes ordena por antigüedad; sin esto es un seq scan de la
-- tabla entera cada vez que el planner abre el módulo.
CREATE INDEX IF NOT EXISTS idx_progrest_detectada
    ON prog_restricciones (detectada_el)
 WHERE NOT liberada;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_progrest_detectada;
ALTER TABLE prog_restriccion_eventos
  DROP COLUMN IF EXISTS duracion_dias,
  DROP COLUMN IF EXISTS detectada_el;
ALTER TABLE prog_restricciones
  DROP COLUMN IF EXISTS detectada_el;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
