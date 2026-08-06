# -*- coding: utf-8 -*-
"""La cuadrilla no es de nadie: es una lista de gente que cualquiera puede usar.

(corrección de Jean 2026-08-06, 3ª vuelta: «lo de que una cuadrilla solo puede
tener un supervisor no siempre se da, quisiera que fueran libres; las cuadrillas
solo son personas preestablecidas para que se les haga más fácil el registro
diario a los supervisores»)

Yo la modelé como una unidad de MANDO —alguien la dirige, rota de supervisor— y
es un atajo de CAPTURA: la lista de siempre para no teclear quince nombres cada
mañana. Con esa lectura, la propiedad exclusiva sobra entera: si hoy me toca el
frente de fulano, quiero su lista, y no tener que pedir que me la reasignen o
duplicarla para usarla una vez.

Qué cambia:

  · `supervisor_id` → `creada_por`. La columna se queda pero cambia de oficio:
    ya no dice de quién ES, sino quién la armó. Se usa SOLO para ordenar la
    pantalla del teléfono —las tuyas primero, el resto debajo— porque una lista
    de treinta cuadrillas sin orden es peor que no tenerlas. Renombrarla es
    deliberado: dejar un `supervisor_id` que ya no significa propiedad es
    exactamente la trampa que provocó el lío de las tres tablas de cuadrilla.
  · `asignado_en` se va: no hay asignación que fechar.
  · El nombre pasa a ser único en TODA la empresa, ahora sí. Antes podían
    coexistir «Encofrado» de dos supervisores porque cada uno veía solo la suya;
    ahora las ve todo el mundo en la misma lista y dos «Encofrado» son
    indistinguibles justo en el momento de elegir.

DESAMBIGUACIÓN del backfill: si hay nombres repetidos, al segundo y siguientes
se les pega su id —«Encofrado (57)»— en vez de un contador. Es más feo y es a
propósito: el id es único por definición, así que el índice no puede fallar
después. Un contador puede chocar contra un «Encofrado (2)» que ya exista y
tumbar la migración entera.

DOWNGRADE: devuelve la columna, la unicidad por supervisor y `asignado_en`. No
des-desambigua los nombres (no hay forma de saber cuáles se tocaron sin guardar
otra tabla para eso), así que un nombre renombrado se queda renombrado. Es la
única pérdida y no afecta a ningún dato de tareo.

CONSTRAINT vs ÍNDICE (lección 0008, que aquí volví a pagar): la unicidad
`(supervisor_id, nombre)` es un CONSTRAINT en la BD real y un índice suelto en
el baseline —el dump que lo generó imprime las unique constraints como
`CREATE UNIQUE INDEX`, y esa distinción se perdió—. `DROP INDEX IF EXISTS` NO
es inofensivo contra un índice que respalda un constraint: Postgres aborta con
DependentObjectsStillExist en vez de ignorarlo. Por eso se suelta primero el
constraint (que se lleva su índice) y solo después se intenta el índice suelto:
así funciona en los dos mundos. El downgrade lo devuelve como CONSTRAINT, que
es la forma de producción.
"""
from alembic import op

revision = "0048_cuadrillas_libres"
down_revision = "0047_cuadrillas_asignables"
branch_labels = None
depends_on = None

_UP = """
DROP INDEX IF EXISTS idx_cgrupos_pool_nombre;
DROP INDEX IF EXISTS idx_cgrupos_sup;

-- El constraint PRIMERO: se lleva por delante su propio índice. Al revés,
-- `DROP INDEX` sobre un índice que respalda un constraint aborta la migración
-- entera (DependentObjectsStillExist), no lo ignora. El `DROP INDEX` de después
-- cubre el caso del baseline, donde la unicidad es un índice suelto.
ALTER TABLE cuadrilla_grupos
  DROP CONSTRAINT IF EXISTS cuadrilla_grupos_supervisor_id_nombre_key;
DROP INDEX IF EXISTS cuadrilla_grupos_supervisor_id_nombre_key;

ALTER TABLE cuadrilla_grupos RENAME COLUMN supervisor_id TO creada_por;
ALTER TABLE cuadrilla_grupos DROP COLUMN IF EXISTS asignado_en;

-- Nombres repetidos (ignorando mayúsculas): al segundo y siguientes se les pega
-- su id, que es único por definición y no puede volver a chocar.
UPDATE cuadrilla_grupos g
   SET nombre = left(g.nombre, 90) || ' (' || g.id || ')'
 WHERE EXISTS (
       SELECT 1 FROM cuadrilla_grupos o
        WHERE lower(o.nombre) = lower(g.nombre)
          AND (o.creado_en, o.id) < (g.creado_en, g.id));

CREATE UNIQUE INDEX IF NOT EXISTS idx_cgrupos_nombre
    ON cuadrilla_grupos (lower(nombre));

-- La pantalla del teléfono ordena por «las que armé yo» primero.
CREATE INDEX IF NOT EXISTS idx_cgrupos_creada_por
    ON cuadrilla_grupos (creada_por) WHERE activo;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_cgrupos_creada_por;
DROP INDEX IF EXISTS idx_cgrupos_nombre;
ALTER TABLE cuadrilla_grupos ADD COLUMN IF NOT EXISTS asignado_en TIMESTAMPTZ;
ALTER TABLE cuadrilla_grupos RENAME COLUMN creada_por TO supervisor_id;

-- Se devuelve como CONSTRAINT —la forma que tiene en producción—, no como
-- índice suelto. Limpiar los dos nombres antes deja el terreno libre venga de
-- donde venga la BD.
ALTER TABLE cuadrilla_grupos
  DROP CONSTRAINT IF EXISTS cuadrilla_grupos_supervisor_id_nombre_key;
DROP INDEX IF EXISTS cuadrilla_grupos_supervisor_id_nombre_key;
ALTER TABLE cuadrilla_grupos
  ADD CONSTRAINT cuadrilla_grupos_supervisor_id_nombre_key
  UNIQUE (supervisor_id, nombre);
UPDATE cuadrilla_grupos SET asignado_en = creado_en WHERE supervisor_id IS NOT NULL;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
