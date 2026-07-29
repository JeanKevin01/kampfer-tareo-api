# -*- coding: utf-8 -*-
"""Desglose de la actividad en dos dimensiones (encargo de Jean 2026-07-28):

El caso: en movimiento de tierras, RELLENO ZONA 5 tiene 15 000 m³
presupuestados, pero no se ejecuta de una — se avanza por ÁREAS y por CAPAS, de
200 m³ en 200 m³, y en la reunión el lookahead se presenta agrupado así. Lo
mismo pasa en cualquier obra grande con otro nombre: eje/nivel en estructuras,
tramo/prueba en tuberías, sector/mano en pintura.

La estructura para hacerlo YA existía: una partida puede tener varias
actividades encima (tramos) y `_dueno_del_real` reparte el avance entre
hermanas sin contarlo dos veces. Lo que faltaba era poder ETIQUETAR cada tramo
para poder agruparlo. Eso son estas dos columnas.

Decisión explícita de Jean: NO se crean partidas hijas por área-capa. Serían
decenas de partidas por cada una del presupuesto, el WBS se volvería ilegible y
se perdería la comparación contra el contractual. La partida sigue siendo una;
lo que se subdivide es su programación.

Los nombres de las dos dimensiones son configurables por proyecto porque cada
obra las llama distinto; por eso las columnas son `desglose_1`/`desglose_2` y no
`area`/`capa` (además, `area` ya significa otra cosa en `campo_reportes`).

Texto libre a propósito: se escribe al programar, con autocompletado de lo ya
usado en esa partida. Obligar a declarar antes el mapa de áreas y capas
retrasaría el primer uso, y en obra el mapa cambia.
"""
from alembic import op

revision = "0037_desglose_actividad"
down_revision = "0036_cierre_semana"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades
  ADD COLUMN IF NOT EXISTS desglose_1 VARCHAR(40),
  ADD COLUMN IF NOT EXISTS desglose_2 VARCHAR(40);

-- Cómo se llaman las dos dimensiones EN ESTE proyecto.
ALTER TABLE prog_config
  ADD COLUMN IF NOT EXISTS etiqueta_desglose_1 VARCHAR(24) NOT NULL DEFAULT 'Área',
  ADD COLUMN IF NOT EXISTS etiqueta_desglose_2 VARCHAR(24) NOT NULL DEFAULT 'Capa';

-- Para el autocompletado (valores ya usados en esa partida) y para agrupar el
-- lookahead sin escanear la tabla entera.
CREATE INDEX IF NOT EXISTS ix_prog_act_desglose
  ON prog_actividades (proyecto_id, partida_id, desglose_1, desglose_2);
"""

_DOWN = """
DROP INDEX IF EXISTS ix_prog_act_desglose;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS desglose_1,
  DROP COLUMN IF EXISTS desglose_2;
ALTER TABLE prog_config
  DROP COLUMN IF EXISTS etiqueta_desglose_1,
  DROP COLUMN IF EXISTS etiqueta_desglose_2;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
