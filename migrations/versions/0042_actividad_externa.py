# -*- coding: utf-8 -*-
"""Trabajo de terceros en el LookAhead: lo que no depende de nosotros pero
condiciona nuestras fechas (encargo de Jean 2026-08-01).

El caso: otra empresa da su plazo —«el montaje nos toma 10 días»— y de eso
dependen actividades nuestras. Hasta ahora había dos formas de anotarlo y
ninguna servía:

  · como RESTRICCIÓN (`prog_restricciones`, tipo PRERREQUISITO): tiene
    responsable y fecha requerida, pero es UNA FECHA, no una duración, y no
    arrastra el cronograma. Sigue siendo lo correcto para lo que no dura (un
    permiso, un material que llega);
  · como actividad libre: arrastra bien —vínculos FS/SS/FF, lag, cascada, días
    medios, saltos— pero **entra al PPC**. Una actividad sin metrado se juzga
    por estado en la semana de su F.Inicio, así que el atraso del contratista
    bajaba NUESTRO indicador de confiabilidad, y desde 0040/0041 además entraba
    al compromiso semanal mezclada con lo propio.

`externa` es esa distinción, y es una COLUMNA y no una convención de nombre a
propósito. Jean escribe la empresa en el título («ELECTRO SAC — Montaje de
bandejas»), que es lo cómodo para leerlo; pero de esta marca depende que la
fila quede fuera del PPC, y deducir eso del texto del título sería frágil —el
día que el nombre se escriba distinto, un compromiso ajeno se cuela en el
indicador propio sin que nadie lo note.

`empresa` es opcional y va aparte del título por una sola razón: poder
responder «¿cuántos días nos ha corrido esta empresa?» agrupando. Se ofrece con
autocompletado de lo ya escrito (mismo patrón que el catálogo de frentes, que
se autoalimenta) en vez de un CRUD: no hace falta administrar empresas para
anotar una fecha.

El CHECK es la parte que no se puede saltar: una fila externa NO puede tener
partida ni metrado. Si pudiera, su avance entraría al valor ganado y su metrado
a la curva S — trabajo que no está en nuestro presupuesto inflando nuestro
BAC. Que sea un CHECK y no solo una validación del router significa que ninguna
ruta futura puede crear esa combinación por descuido.
"""
from alembic import op

revision = "0042_actividad_externa"
down_revision = "0041_metrado_comprometido"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades
  ADD COLUMN IF NOT EXISTS externa BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS empresa TEXT;

-- Una fila de terceros no tiene metrado NUESTRO ni cuelga de una partida de
-- control: su avance no es valor ganado nuestro y su metrado no va a la curva S.
ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_act_externa_sin_metrado;
ALTER TABLE prog_actividades
  ADD CONSTRAINT prog_act_externa_sin_metrado
  CHECK (NOT externa OR (partida_id IS NULL AND COALESCE(metrado_prog, 0) = 0));

-- El LookAhead filtra por proyecto y fechas; la marca acompaña esa consulta.
CREATE INDEX IF NOT EXISTS idx_prog_act_externa
  ON prog_actividades (proyecto_id, externa) WHERE externa;

-- Autocompletado de empresas ya usadas (no hay catálogo que administrar).
CREATE INDEX IF NOT EXISTS idx_prog_act_empresa
  ON prog_actividades (proyecto_id, empresa) WHERE empresa IS NOT NULL;
"""

_DOWN = """
DROP INDEX IF EXISTS idx_prog_act_empresa;
DROP INDEX IF EXISTS idx_prog_act_externa;
ALTER TABLE prog_actividades DROP CONSTRAINT IF EXISTS prog_act_externa_sin_metrado;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS empresa,
  DROP COLUMN IF EXISTS externa;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
