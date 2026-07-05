# -*- coding: utf-8 -*-
"""F0.7 (0008a del PLAN_MAESTRO): eliminar tablas muertas.

- tokens_edicion: flujo de edición por token que nunca se usó (0 lectores en el código).
- ev_config_jornada: reemplazada por ev_jornada_reglas (ver resolver_jornada en main.py).

Pre-check 2026-07-05: grep en los 3 repos (api/panel/web) = 0 referencias fuera del baseline.
Decisión de negocio: son tablas sin datos útiles; el downgrade las recrea VACÍAS con su DDL
original del baseline (no hay datos que restaurar — estaban muertas).
"""
from alembic import op

revision = "0007_limpieza_muertas"
down_revision = "0006_ro"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TABLE tokens_edicion")
    op.execute("DROP TABLE ev_config_jornada")


def downgrade():
    op.execute("""
        CREATE TABLE ev_config_jornada (
            dia_semana integer NOT NULL,
            hh_dia numeric(4,2) DEFAULT '9.5' NOT NULL,
            activo boolean DEFAULT true,
            CONSTRAINT ev_config_jornada_pkey PRIMARY KEY (dia_semana),
            CONSTRAINT ev_config_jornada_dia_semana_check
                CHECK (dia_semana >= 0 AND dia_semana <= 6)
        )
    """)
    op.execute("""
        CREATE TABLE tokens_edicion (
            token character varying(50) NOT NULL,
            supervisor_id character varying(10),
            otm_id character varying(15),
            fecha date,
            expira_at timestamp NOT NULL,
            usado boolean DEFAULT false,
            created_at timestamp DEFAULT now(),
            CONSTRAINT tokens_edicion_pkey PRIMARY KEY (token)
        )
    """)
