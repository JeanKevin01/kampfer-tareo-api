# -*- coding: utf-8 -*-
"""F0.7 (0008b del PLAN_MAESTRO): unicidad de ev_partidas.codigo POR OTM (no global).

Problema: el índice global ev_partidas_codigo_key impedía que dos OTMs tuvieran la partida
"01.01.01", y peor: /ev/importar resolvía por código a secas y podía ROBARLE la partida a otra
OTM (corrupción silenciosa). Bloqueaba multi-OTM/multi-proyecto.

Este cambio va acompañado (mismo commit) de los fixes de código:
  - /ev/importar resuelve por (codigo, otm_id) con IS NOT DISTINCT FROM.
  - crear_partida reporta el conflicto por OTM.
  - presupuesto.congelar sincroniza solo partidas de las OTMs del proyecto.

Verificación en prod ANTES de aplicar (protocolo ⚠️VENTANA, o directo mientras no haya usuarios):
  SELECT COALESCE(otm_id,'(SIN)'), codigo, count(*) FROM ev_partidas
  GROUP BY 1,2 HAVING count(*)>1;   -- debe devolver 0 filas
(la migración lo re-verifica y aborta si hay duplicados)

Downgrade: restaura el índice global — fallará si ya se crearon códigos repetidos entre OTMs
(en ese caso hay que resolver los duplicados antes de bajar; es intencional).
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_unicidad_otm"
down_revision = "0007_limpieza_muertas"
branch_labels = None
depends_on = None


def upgrade():
    con = op.get_bind()
    dups = con.execute(sa.text(
        "SELECT COALESCE(otm_id,'(SIN)') AS otm, codigo, count(*) AS n "
        "FROM ev_partidas GROUP BY 1,2 HAVING count(*)>1"
    )).fetchall()
    if dups:
        raise RuntimeError(
            f"ABORTADO: hay {len(dups)} (otm,codigo) duplicados en ev_partidas; "
            f"resolver antes de migrar: {dups[:5]}"
        )
    # En PROD ev_partidas_codigo_key es una CONSTRAINT (BD creada por el DDL pre-Alembic);
    # en BDs creadas desde el baseline es un ÍNDICE (el export del baseline lo convirtió).
    # Deriva detectada el 2026-07-05 al aplicar en prod — manejar ambos casos:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conname = 'ev_partidas_codigo_key'
                         AND conrelid = 'ev_partidas'::regclass) THEN
                ALTER TABLE ev_partidas DROP CONSTRAINT ev_partidas_codigo_key;
            ELSIF EXISTS (SELECT 1 FROM pg_indexes
                          WHERE indexname = 'ev_partidas_codigo_key') THEN
                DROP INDEX ev_partidas_codigo_key;
            END IF;
        END $$;
    """)
    op.execute(
        "CREATE UNIQUE INDEX uq_partida_otm_codigo "
        "ON ev_partidas (COALESCE(otm_id,'(SIN)'), codigo)"
    )


def downgrade():
    op.execute("DROP INDEX uq_partida_otm_codigo")
    # Se restaura como CONSTRAINT (la forma original de prod; funcionalmente equivalente)
    op.execute("ALTER TABLE ev_partidas ADD CONSTRAINT ev_partidas_codigo_key UNIQUE (codigo)")
