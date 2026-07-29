# -*- coding: utf-8 -*-
"""La segunda etiqueta del desglose deja de llamarse «Capa» (2026-07-29).

Encargo de Jean, y tiene razón: «Capa» solo existe en movimiento de tierras. En
una obra de estructuras, de tuberías o de acabados esa palabra no significa
nada, y KAMPFER tiene que servir para cualquiera de las tres.

El defecto pasa a «Frente / Tramo / Sector» —las tres palabras juntas, como en
el resto del módulo— para que ninguna especialidad tenga que traducirlo: es un
frente en tierras, un tramo en carreteras, un sector en edificación. Sigue
siendo configurable por proyecto (`PUT /config/desglose`): quien de verdad
trabaje por capas la escribe así y listo.

Se actualizan solo las filas que aún tienen el defecto viejo intacto: si alguien
ya la renombró a mano, esa decisión es suya y no se toca.

Y las dos columnas pasan de 24 a 40 caracteres: «Frente / Tramo / Sector» ocupa
23 y no dejaba sitio para nada más largo.

Downgrade real: devuelve el defecto y el ancho anteriores (los nombres más
largos de 24 se recortan, que es lo que cabía antes).
"""
from alembic import op

revision = "0039_etiqueta_generica"
down_revision = "0038_tramos_lookahead"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_config
  ALTER COLUMN etiqueta_desglose_1 TYPE VARCHAR(40),
  ALTER COLUMN etiqueta_desglose_2 TYPE VARCHAR(40);

ALTER TABLE prog_config
  ALTER COLUMN etiqueta_desglose_2 SET DEFAULT 'Frente / Tramo / Sector';

UPDATE prog_config SET etiqueta_desglose_2 = 'Frente / Tramo / Sector'
 WHERE etiqueta_desglose_2 = 'Capa';
"""

_DOWN = """
UPDATE prog_config SET etiqueta_desglose_2 = 'Capa'
 WHERE etiqueta_desglose_2 = 'Frente / Tramo / Sector';

ALTER TABLE prog_config
  ALTER COLUMN etiqueta_desglose_2 SET DEFAULT 'Capa';

ALTER TABLE prog_config
  ALTER COLUMN etiqueta_desglose_1 TYPE VARCHAR(24) USING LEFT(etiqueta_desglose_1, 24),
  ALTER COLUMN etiqueta_desglose_2 TYPE VARCHAR(24) USING LEFT(etiqueta_desglose_2, 24);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
