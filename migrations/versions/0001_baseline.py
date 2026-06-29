"""baseline: esquema inicial completo (tablas, secuencias, índices, FKs, trigger, vistas y funciones)

Captura el esquema REAL como punto de partida versionado. A partir de aquí, todo
cambio de esquema es una migración nueva (nunca más cambios a mano en Adminer).

Uso:
  · BD nueva/vacía:  alembic upgrade head           → reconstruye TODO desde cero.
  · BD ya existente: alembic stamp 0001_baseline     → la marca como migrada sin re-ejecutar.

El SQL vive en 0001_baseline.sql (al lado de este archivo); se ejecuta tal cual.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-29
"""
from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# SQL del baseline (funciones → tablas/secuencias/índices → trigger → FKs → vistas)
_SQL = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")

# Para downgrade (revertir el baseline por completo).
_VIEWS = ["v_hh_sesion_partida_semana", "ev_hh_tareo"]
_TABLES = [
    "cuadrilla_grupo_miembros", "cuadrilla_grupos", "cuadrilla_otm", "cuadrillas",
    "ev_avances", "ev_avances_diarios", "ev_config", "ev_config_jornada",
    "ev_hh_gastadas", "ev_hh_improductivas", "ev_historico_carga", "ev_hitos",
    "ev_jornada_reglas", "ev_partidas", "ev_plantillas_hitos", "ev_tarifas_cargo",
    "ev_valorizado", "hh_conflictos", "otms", "registros",
    "sesion_trabajador_partidas", "sesion_trabajadores", "sesiones", "supervisores",
    "tareo_partida", "tokens_edicion", "trabajadores", "usuarios",
]
_FUNCS = ["fn_sync_hh_gastadas()", "ev_semana_de(date)"]


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    for v in _VIEWS:
        bind.exec_driver_sql(f'DROP VIEW IF EXISTS "{v}" CASCADE')
    for t in _TABLES:
        bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    for f in _FUNCS:
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {f} CASCADE")
