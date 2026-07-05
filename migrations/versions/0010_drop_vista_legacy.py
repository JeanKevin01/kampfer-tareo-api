# -*- coding: utf-8 -*-
"""F0.3: retiro del flujo legacy de tareo — parte de esquema.

La vista ev_hh_tareo (agregado sobre `registros`) pierde su último lector con este corte
(semanas() ya lee tareo_partida). La tabla `registros` se CONSERVA como histórico congelado
(sin escritores ni lectores en el código; verificación 2026-07-05 sobre datos reales:
0 semanas dependían de la distribución proporcional → la migración de datos que preveía el
plan resultó innecesaria).

Downgrade: recrea la vista con su definición original del baseline.
"""
from alembic import op

revision = "0010_drop_vista_legacy"
down_revision = "0009_ids_trabajador_fks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP VIEW IF EXISTS ev_hh_tareo")


def downgrade():
    op.execute("""
        CREATE VIEW ev_hh_tareo AS
        SELECT partida_id, fecha, sum(hh) AS hh
        FROM registros
        WHERE partida_id IS NOT NULL AND hh IS NOT NULL AND hh > 0
        GROUP BY partida_id, fecha
    """)
