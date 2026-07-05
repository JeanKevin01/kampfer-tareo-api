# -*- coding: utf-8 -*-
"""F0.7 (0008c del PLAN_MAESTRO): integridad de IDs de trabajador + FKs reales.

Qué hace:
1. Pre-check: aborta si existe algún id de trabajador SIN pad de 3 dígitos en cualquiera de las
   7 columnas involucradas (verificado contra el dump de prod 2026-07-05: 0 filas — los zfill(3)
   de la capa de entrada mantuvieron los datos limpios). Decisión: pre-check-y-abortar en vez de
   normalizar dentro de la migración — actualizar PKs con FKs vivas exige malabares (drop/re-add)
   que no se justifican para un caso que hoy no existe.
2. Armoniza tipos: varchar(5) → varchar(10) en trabajadores.id y columnas hijas.
3. Crea las FKs que faltaban (NOT VALID → VALIDATE, patrón sin bloqueo largo):
   tareo_partida.{trabajador_id→trabajadores, supervisor_id→supervisores, otm_id→otms}
   ev_hh_improductivas.{partida_id→ev_partidas, otm_id→otms}
   (verificado contra prod: 0 huérfanos en las 5)
4. El MISMO commit elimina los JOIN con LPAD (ro.py, valor_ganado.py) que anulaban índices:
   con los datos garantizados por FK, el join directo es correcto y usa índice.

Downgrade: quita las 5 FKs y revierte los tipos a varchar(5) (los datos de 3 chars caben).
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_ids_trabajador_fks"
down_revision = "0008_unicidad_otm"
branch_labels = None
depends_on = None

# (tabla, columna) que referencian trabajador con texto
_COLS_TRAB = [
    ("trabajadores", "id"),
    ("cuadrillas", "trab_id"),
    ("cuadrilla_grupo_miembros", "trab_id"),
    ("sesion_trabajadores", "trab_id"),
    ("registros", "trab_id"),
    ("tareo_partida", "trabajador_id"),
    ("cuadrilla_otm", "trabajador_id"),
    ("hh_conflictos", "trabajador_id"),
]

_FKS = [
    ("tareo_partida", "trabajador_id", "trabajadores", "id", "fk_tp_trabajador"),
    ("tareo_partida", "supervisor_id", "supervisores", "id", "fk_tp_supervisor"),
    ("tareo_partida", "otm_id", "otms", "id", "fk_tp_otm"),
    ("ev_hh_improductivas", "partida_id", "ev_partidas", "id", "fk_improd_partida"),
    ("ev_hh_improductivas", "otm_id", "otms", "id", "fk_improd_otm"),
]


def upgrade():
    con = op.get_bind()

    # 1) pre-check de ids sin pad (debe ser 0 en todos lados)
    sucios = []
    for tabla, col in _COLS_TRAB:
        n = con.execute(sa.text(
            f"SELECT count(*) FROM {tabla} WHERE {col} ~ '^[0-9]{{1,2}}$'"
        )).scalar()
        if n:
            sucios.append(f"{tabla}.{col}={n}")
    if sucios:
        raise RuntimeError(
            "ABORTADO: hay ids de trabajador sin pad de 3 dígitos: " + ", ".join(sucios) +
            ". Normalizar con /api/trabajadores/merge (o SQL guiado) antes de migrar."
        )

    # 2) pre-check de huérfanos para cada FK nueva (debe ser 0)
    huerfanos = []
    for tabla, col, ref_t, ref_c, nombre in _FKS:
        n = con.execute(sa.text(
            f"SELECT count(*) FROM {tabla} x WHERE x.{col} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {ref_t} r WHERE r.{ref_c}::text = x.{col}::text)"
        )).scalar()
        if n:
            huerfanos.append(f"{nombre}: {n} filas huérfanas en {tabla}.{col}")
    if huerfanos:
        raise RuntimeError("ABORTADO: FKs con huérfanos: " + "; ".join(huerfanos))

    # 3) armonizar tipos a varchar(10)
    op.execute("ALTER TABLE trabajadores ALTER COLUMN id TYPE varchar(10)")
    op.execute("ALTER TABLE cuadrillas ALTER COLUMN trab_id TYPE varchar(10)")
    op.execute("ALTER TABLE sesion_trabajadores ALTER COLUMN trab_id TYPE varchar(10)")
    op.execute("ALTER TABLE registros ALTER COLUMN trab_id TYPE varchar(10)")

    # 4) FKs nuevas: NOT VALID (no bloquea) + VALIDATE (escaneo con lock suave)
    for tabla, col, ref_t, ref_c, nombre in _FKS:
        op.execute(
            f"ALTER TABLE {tabla} ADD CONSTRAINT {nombre} "
            f"FOREIGN KEY ({col}) REFERENCES {ref_t} ({ref_c}) NOT VALID"
        )
        op.execute(f"ALTER TABLE {tabla} VALIDATE CONSTRAINT {nombre}")


def downgrade():
    for tabla, col, ref_t, ref_c, nombre in reversed(_FKS):
        op.execute(f"ALTER TABLE {tabla} DROP CONSTRAINT {nombre}")
    op.execute("ALTER TABLE registros ALTER COLUMN trab_id TYPE varchar(5)")
    op.execute("ALTER TABLE sesion_trabajadores ALTER COLUMN trab_id TYPE varchar(5)")
    op.execute("ALTER TABLE cuadrillas ALTER COLUMN trab_id TYPE varchar(5)")
    op.execute("ALTER TABLE trabajadores ALTER COLUMN id TYPE varchar(5)")
