# -*- coding: utf-8 -*-
"""Frente / zona del parte de campo (2026-07-25) — separar ÁREA de FRENTE.

Problema que resuelve (auditoría del área con Jean): había TRES campos
llamados "área" y uno de ellos era texto libre en el celular:

  · `otms.area`            — el área del proyecto (oficina).
  · `ev_partidas.sistema`  — el "Área" del ANÁLISIS (matriz Área×Disciplina
                             del Resumen Ejecutivo); hereda de otms.area.
  · `campo_reportes.area`  — texto LIBRE que el supervisor podía reescribir.

Los dos primeros son dimensiones presupuestales y deben ser estables; el
tercero era operativo pero, al ser libre, (a) podía divergir del análisis
—el parte decía un área y la matriz agrupaba en otra— y (b) generaba
variantes sucias ("GSC", "gsc", "G.S.C").

Decisión: el ÁREA deja de ser editable en campo (se copia del proyecto, como
foto histórica: si mañana cambia otms.area, los partes viejos no se alteran)
y se agrega FRENTE — la zona concreta donde trabajó la cuadrilla, que el
supervisor elige de un catálogo auto-alimentado por OTM (se normaliza en
MAYÚSCULAS al guardar para que no se dupliquen variantes).

Así el parte imprime dos líneas coherentes:
    AREA:   GSC        ← fija, del proyecto = la del análisis EV
    FRENTE: BAHIA 4    ← operativa, seleccionable

Downgrade real: elimina la columna (el dato de frente es nuevo de esta versión).
"""
from alembic import op

revision = "0033_frente"
down_revision = "0032_reporte_estructurado"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE campo_reportes ADD COLUMN IF NOT EXISTS frente TEXT;
-- Catálogo de frentes por OTM: lo alimenta el propio uso (DISTINCT), así que
-- el índice es lo único que hace falta para que la consulta sea barata.
CREATE INDEX IF NOT EXISTS idx_campo_reportes_frente
  ON campo_reportes (otm_id, frente);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_campo_reportes_frente;
ALTER TABLE campo_reportes DROP COLUMN IF EXISTS frente;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
