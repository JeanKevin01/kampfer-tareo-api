# -*- coding: utf-8 -*-
"""Sub-filas del LookAhead: una partida grande se ejecuta por tramos (2026-07-28).

Encargo de Jean, del caso de movimiento de tierras: RELLENO ZONA 5 son 15 000 m3
presupuestados que NO se ejecutan de una — se avanzan de 200 en 200 por áreas y
capas. Lo que pidió es un ÁRBOL dentro del LookAhead: la fila #46 se despliega en
#46.1, #46.2… cada una con su metrado, que se descuenta del presupuesto de la
MISMA partida (nunca se crean partidas nuevas: el WBS tiene que seguir siendo
comparable contra el contractual).

Tres columnas:

  · `prog_actividades.padre_id` — el árbol. Un solo nivel: el padre pasa a ser un
    contenedor (su metrado y sus celdas se mudan al primer hijo) y lo que se ve
    en la fila del padre es la SUMA de sus hijos por día. Borrar el padre se
    lleva a los hijos (CASCADE): sin él la fila hija no significa nada.

  · `prog_actividades.es_frente` — de qué tipo es el hijo. Una partida se parte
    de dos maneras y NO se mezclan (decisión de Jean):
      es_frente = true  → «Frente / Tramo / Sector»: una porción del metrado
                          (área + capa viven en desglose_1/2, migración 0037).
                          En el panel lleva el «#» azul.
      es_frente = false → sub-etapa: un hito de la partida, lo que ya existía
                          desde 0025. En el panel lleva el «#» violeta.
    Hace falta la columna y no basta con `hito_id IS NULL`: un frente HEREDA el
    hito del padre (lo necesita para el rollup de EV) y sin la marca se
    dibujaría como sub-etapa.

  · `ev_avances_diarios.tramo_id` — el avance real POR TRAMO, que es lo que hace
    verdadero todo lo anterior. Hasta hoy el diario era único por
    (partida, fecha, etapa) y, si la partida-etapa tenía varios tramos vivos, el
    total del día se REPARTÍA entre ellos (§ _dueno_del_real). Con dos áreas
    trabajando el mismo lunes —150 m3 en la A y 50 en la B— el historial por
    área decía 100 y 100: inventado. El unique pasa a incluir el tramo, así cada
    hijo guarda su propio real y el total de la partida no cambia (el rollup de
    EV suma por hito y semana, no por tramo).

OJO con el nombre: `campo_reportes.frente` (0033) es otra cosa — la zona que el
supervisor elige en el parte del día. Aquí «frente» es solo la ETIQUETA visible
que pidió Jean («Frente / Tramo / Sector», para que lo entienda cualquier
especialidad); en la BD el concepto se llama TRAMO, que es la palabra que este
módulo ya usaba para una porción de partida programada aparte.

Downgrade real: borra los avances por tramo y las actividades hijas (datos
nuevos de esta versión, igual que hizo 0025 con las filas por hito) para poder
restaurar el unique anterior. Lo que se pierde al bajar es el desglose, no el
avance de la partida: los avances del padre nunca llevan tramo_id.
"""
from alembic import op

revision = "0038_tramos_lookahead"
down_revision = "0037_desglose_actividad"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades
  ADD COLUMN IF NOT EXISTS padre_id INT REFERENCES prog_actividades(id) ON DELETE CASCADE,
  ADD COLUMN IF NOT EXISTS es_frente BOOLEAN NOT NULL DEFAULT false;

-- Pintar el árbol es leer los hijos de cada fila visible del rango.
CREATE INDEX IF NOT EXISTS ix_prog_act_padre
  ON prog_actividades (padre_id) WHERE padre_id IS NOT NULL;

ALTER TABLE ev_avances_diarios
  ADD COLUMN IF NOT EXISTS tramo_id INT
    REFERENCES prog_actividades(id) ON DELETE CASCADE;

-- Lección 0008/0025: soltar la forma que exista (índice o constraint).
ALTER TABLE ev_avances_diarios
  DROP CONSTRAINT IF EXISTS ev_avances_diarios_pfh_key;
DROP INDEX IF EXISTS ev_avances_diarios_pfh_key;
CREATE UNIQUE INDEX ev_avances_diarios_pfht_key
  ON ev_avances_diarios (partida_id, fecha, COALESCE(hito_id, 0), COALESCE(tramo_id, 0));

-- El re-prorrateo de un tramo lee sus propios reales y solo los suyos.
CREATE INDEX IF NOT EXISTS ix_ead_tramo
  ON ev_avances_diarios (tramo_id) WHERE tramo_id IS NOT NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS ix_ead_tramo;
DELETE FROM ev_avances_diarios WHERE tramo_id IS NOT NULL;
DROP INDEX IF EXISTS ev_avances_diarios_pfht_key;
ALTER TABLE ev_avances_diarios DROP COLUMN IF EXISTS tramo_id;
CREATE UNIQUE INDEX ev_avances_diarios_pfh_key
  ON ev_avances_diarios (partida_id, fecha, COALESCE(hito_id, 0));

DELETE FROM prog_actividades WHERE padre_id IS NOT NULL;
DROP INDEX IF EXISTS ix_prog_act_padre;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS es_frente,
  DROP COLUMN IF EXISTS padre_id;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
