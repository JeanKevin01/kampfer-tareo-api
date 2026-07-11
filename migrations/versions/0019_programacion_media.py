# -*- coding: utf-8 -*-
"""Calendario de programación + reportes de campo con fotos (mejoras UX pre-F4).

  · prog_actividades — lo que el planner programa por día (calendario combinado:
    PROGRAMADO → EJECUTADO cuando llega un reporte de campo, o CANCELADO).
  · campo_reportes   — lo que el supervisor reporta desde el celular
    (descripción + fotos), opcionalmente vinculado a una actividad.
  · campo_fotos      — los archivos en el disco del VPS (MEDIA_DIR), en carpeta
    por semana ISO para que la purga y el indicador de uso mapeen 1:1.
    `purgada=true` = el archivo ya se borró del disco (el texto se conserva).

El downgrade NO toca los archivos del disco (solo el esquema).
"""
from alembic import op

revision = "0019_programacion_media"
down_revision = "0018_fases"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE prog_actividades (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  fecha DATE NOT NULL,
  otm_id VARCHAR(15) REFERENCES otms(id),
  partida_id INT REFERENCES ev_partidas(id),
  titulo TEXT NOT NULL,
  descripcion TEXT,
  estado TEXT NOT NULL DEFAULT 'PROGRAMADO'
    CHECK (estado IN ('PROGRAMADO','EJECUTADO','CANCELADO')),
  responsable TEXT,
  creado_por TEXT,
  creado_en TIMESTAMPTZ DEFAULT now(),
  actualizado_en TIMESTAMPTZ
);
CREATE INDEX idx_progact_proy_fecha ON prog_actividades (proyecto_id, fecha);

CREATE TABLE campo_reportes (
  id SERIAL PRIMARY KEY,
  proyecto_id INT NOT NULL REFERENCES proyectos(id),
  fecha DATE NOT NULL,
  otm_id VARCHAR(15) REFERENCES otms(id),
  actividad_id INT REFERENCES prog_actividades(id) ON DELETE SET NULL,
  supervisor_id VARCHAR(10) REFERENCES supervisores(id),
  descripcion TEXT,
  creado_en TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_camprep_proy_fecha ON campo_reportes (proyecto_id, fecha);

CREATE TABLE campo_fotos (
  id SERIAL PRIMARY KEY,
  reporte_id INT NOT NULL REFERENCES campo_reportes(id) ON DELETE CASCADE,
  semana_iso TEXT NOT NULL,
  ruta TEXT NOT NULL,
  ruta_thumb TEXT NOT NULL,
  bytes INT NOT NULL DEFAULT 0,
  bytes_thumb INT NOT NULL DEFAULT 0,
  ancho INT,
  alto INT,
  purgada BOOLEAN NOT NULL DEFAULT false,
  creado_en TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_campofotos_semana ON campo_fotos (semana_iso);
"""

_DOWN = """
DROP TABLE IF EXISTS campo_fotos;
DROP TABLE IF EXISTS campo_reportes;
DROP TABLE IF EXISTS prog_actividades;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
