# -*- coding: utf-8 -*-
"""La cuadrilla existe por sí misma; el supervisor es una asignación.

(encargo de Jean 2026-08-06, 2ª vuelta: «en mi anterior proyecto teníamos
supervisores y cuadrillas que rotaban... estaría bueno tener una base de datos
de las cuadrillas y poder editar y asignar libremente en lugar de crear cada vez
que entra un nuevo supervisor»)

La 0046 hizo la cuadrilla del SUPERVISOR, que era el arreglo correcto entonces:
antes colgaba del proyecto y no se podía reutilizar. Pero deja fuera lo que pasa
de verdad en obra: **lo que rota es el mando, no la gente**. La cuadrilla de
encofrado sigue siendo la misma cuando cambia quien la dirige, y con
`supervisor_id NOT NULL` la única salida era volver a teclearla entera con cada
supervisor nuevo.

Con esto la cuadrilla puede estar SIN ASIGNAR —existe en el catálogo, esperando
a quien la lleve— y cambiar de supervisor sin perder su gente.

Lo que NO cambia, deliberadamente:

  · El nombre sigue siendo único POR SUPERVISOR, no en toda la empresa.
    «Encofrado» del frente A y «Encofrado» del frente B son dos cuadrillas
    legítimas, y exigir unicidad global obligaría a renombrar cuadrillas que ya
    existen y que nadie pidió tocar. La única regla nueva es que entre las que
    están sin asignar no haya nombres repetidos: ahí no hay un supervisor que
    las distinga y dos «Encofrado» en el catálogo son indistinguibles.
    Consecuencia aceptada: asignar a alguien que YA tiene ese nombre da 409, y
    ese error es accionable (renombrar una de las dos).
  · Una cuadrilla tiene UN supervisor a la vez. Para la misma gente en dos
    frentes está duplicar, que es lo que Jean pidió primero. Un N:M sin
    necesidad real solo complica quién ve qué en el teléfono.

`asignado_en` responde «¿desde cuándo la lleva?». No es un historial —eso sería
otra tabla— pero es el dato que se pide en cuanto hay rotación, y cuesta una
columna.

DOWNGRADE: devuelve `supervisor_id` a NOT NULL, y para poder hacerlo BORRA las
cuadrillas sin asignar. Es destructivo y es a propósito: esas filas solo pueden
existir gracias a este upgrade (antes la columna no admitía NULL), así que se
borra exactamente lo que esta migración hizo posible. Las asignadas y sus
miembros no se tocan.
"""
from alembic import op

revision = "0047_cuadrillas_asignables"
down_revision = "0046_cuadrillas_del_supervisor"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE cuadrilla_grupos
  ALTER COLUMN supervisor_id DROP NOT NULL,
  ADD COLUMN IF NOT EXISTS asignado_en TIMESTAMPTZ;

-- El unique de siempre (supervisor_id, nombre) NO cubre las del pool: en un
-- índice único los NULL no colisionan entre sí, así que sin esto se podrían
-- crear diez «Encofrado» sin asignar y ninguna distinguible de la otra.
CREATE UNIQUE INDEX IF NOT EXISTS idx_cgrupos_pool_nombre
    ON cuadrilla_grupos (nombre)
 WHERE supervisor_id IS NULL;

-- El catálogo se lista por supervisor y se filtra por «sin asignar».
CREATE INDEX IF NOT EXISTS idx_cgrupos_sup
    ON cuadrilla_grupos (supervisor_id)
 WHERE activo;

-- Las que ya existen llevan con su supervisor desde que se crearon; es lo más
-- cierto que se puede decir sin inventar una fecha.
UPDATE cuadrilla_grupos
   SET asignado_en = creado_en
 WHERE supervisor_id IS NOT NULL AND asignado_en IS NULL;
"""

_DOWN = """
DELETE FROM cuadrilla_grupos WHERE supervisor_id IS NULL;
DROP INDEX IF EXISTS idx_cgrupos_sup;
DROP INDEX IF EXISTS idx_cgrupos_pool_nombre;
ALTER TABLE cuadrilla_grupos
  DROP COLUMN IF EXISTS asignado_en,
  ALTER COLUMN supervisor_id SET NOT NULL;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
