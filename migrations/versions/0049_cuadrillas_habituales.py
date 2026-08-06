# -*- coding: utf-8 -*-
"""Cuadrillas habituales: cuáles usa normalmente cada supervisor.

(encargo de Jean 2026-08-06, 4ª vuelta: «me gustaría poder asignarle a cada
supervisor cuadrillas habituales, que cuando seleccionen sus cuadrillas les
aparezcan al inicio y como por separado, y ya luego las demás»)

La 0048 dejó las cuadrillas libres y ordenaba el teléfono por `creada_por`
—«las que armé yo primero»—, que es un apaño: quien la armó no es
necesariamente quien la usa. Un supervisor que entra hoy no ha armado ninguna,
así que las ve todas revueltas; y una lista que armó oficina no es habitual de
nadie aunque la usen tres personas todos los días.

Esto lo hace explícito y N:M, que es lo que pide la obra de verdad: la misma
cuadrilla puede ser habitual de dos supervisores (turnos, un frente que se
cubre entre dos) y un supervisor tiene varias habituales. Sigue sin haber
propiedad: `habitual` es un ATAJO DE PANTALLA, no un permiso. Todos ven todas.

BACKFILL: quien armó una cuadrilla la recibe como habitual. Es exactamente el
orden que ese supervisor ya veía en su teléfono, así que el día del deploy nadie
nota un cambio de sitio; a partir de ahí oficina lo ajusta.

DOWNGRADE: DROP TABLE. Se pierde la asignación (no el dato de tareo ni las
cuadrillas), y el orden vuelve a salir de `creada_por`, que sigue existiendo.
"""
from alembic import op

revision = "0049_cuadrillas_habituales"
down_revision = "0048_cuadrillas_libres"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS cuadrilla_habituales (
    supervisor_id VARCHAR(10) NOT NULL
        REFERENCES supervisores(id) ON DELETE CASCADE,
    grupo_id      INTEGER NOT NULL
        REFERENCES cuadrilla_grupos(id) ON DELETE CASCADE,
    creado_en     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (supervisor_id, grupo_id)
);

-- La PK ya sirve para «las habituales de este supervisor»; este índice es para
-- el sentido contrario, «de quién es habitual esta cuadrilla», que es lo que
-- pinta el panel y lo que hay que borrar en cascada al eliminar una cuadrilla.
CREATE INDEX IF NOT EXISTS idx_chab_grupo
    ON cuadrilla_habituales (grupo_id);

-- Quien la armó la recibe como habitual: es el orden que ya tenía en pantalla.
-- El JOIN con supervisores no es decorativo — `creada_por` admite NULL y puede
-- apuntar a un supervisor dado de baja; sin él la FK tumbaría la migración.
INSERT INTO cuadrilla_habituales (supervisor_id, grupo_id, creado_en)
SELECT g.creada_por, g.id, g.creado_en
  FROM cuadrilla_grupos g
  JOIN supervisores s ON s.id = g.creada_por
 WHERE g.creada_por IS NOT NULL
ON CONFLICT DO NOTHING;
"""

_DOWN = """
DROP TABLE IF EXISTS cuadrilla_habituales;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
