"""limpieza: retirar el flujo redundante sesion_trabajador_partidas

Decisión de negocio: la ÚNICA fuente de verdad de HH es el tareo web (`tareo_partida`).
El segundo camino —tabla `sesion_trabajador_partidas` + pantalla "Sesiones sin asignar"
(endpoints /ev/sesiones-sin-asignar y /ev/asignar-sesion-partidas) + /ev/hh-trabajador-dia—
queda retirado. Su trigger y su vista ya se eliminaron en 0002.

Esta migración elimina la tabla. El código de los endpoints se retira en el mismo PR.

Revision ID: 0003_drop_stp
Revises: 0002_limpieza_hh
Create Date: 2026-06-29
"""
from alembic import op

revision = "0003_drop_stp"
down_revision = "0002_limpieza_hh"
branch_labels = None
depends_on = None

_UP = "DROP TABLE IF EXISTS sesion_trabajador_partidas CASCADE;"

# downgrade = recrear la tabla idéntica al baseline (secuencia, índices, OWNED BY, FKs).
# El trigger NO se recrea aquí: lo restaura 0002.downgrade cuando se baje de 0002 a 0001.
_DOWN = """
CREATE SEQUENCE "public".sesion_trabajador_partidas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;
CREATE TABLE "public"."sesion_trabajador_partidas" (
    "id" integer DEFAULT nextval('sesion_trabajador_partidas_id_seq') NOT NULL,
    "sesion_id" integer NOT NULL,
    "trabajador_id" character varying NOT NULL,
    "partida_id" integer,
    "hh" numeric(10,4) DEFAULT '0' NOT NULL,
    "fecha" date NOT NULL,
    "supervisor_id" character varying,
    "otm_id" character varying NOT NULL,
    "created_at" timestamp DEFAULT now(),
    CONSTRAINT "sesion_trabajador_partidas_pkey" PRIMARY KEY ("id")
) WITH (oids = false);
ALTER SEQUENCE "public".sesion_trabajador_partidas_id_seq OWNED BY "public".sesion_trabajador_partidas.id;
CREATE UNIQUE INDEX sesion_trabajador_partidas_sesion_id_trabajador_id_partida__key ON public.sesion_trabajador_partidas USING btree (sesion_id, trabajador_id, partida_id);
CREATE INDEX idx_stp_sesion ON public.sesion_trabajador_partidas USING btree (sesion_id);
CREATE INDEX idx_stp_fecha ON public.sesion_trabajador_partidas USING btree (fecha);
CREATE INDEX idx_stp_partida ON public.sesion_trabajador_partidas USING btree (partida_id);
CREATE INDEX idx_stp_trab_fecha ON public.sesion_trabajador_partidas USING btree (trabajador_id, fecha);
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id) ON DELETE SET NULL;
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_sesion_id_fkey" FOREIGN KEY (sesion_id) REFERENCES sesiones(id) ON DELETE CASCADE;
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_supervisor_id_fkey" FOREIGN KEY (supervisor_id) REFERENCES supervisores(id);
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_trabajador_id_fkey" FOREIGN KEY (trabajador_id) REFERENCES trabajadores(id) ON DELETE CASCADE;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
