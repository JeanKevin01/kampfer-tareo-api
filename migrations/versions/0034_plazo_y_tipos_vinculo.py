# -*- coding: utf-8 -*-
"""Plazo como dato de primera clase + tipos de vínculo SS/FF
(observaciones del planner, encargo de Jean 2026-07-26):

  · prog_actividades.plazo_dias — la DURACIÓN de la actividad medida en DÍAS
    HÁBILES ponderados: día completo 1, medio día (dias_medio) 0.5, salto
    (dias_salto) 0. Hasta ahora la duración no existía como dato: se deducía
    contando los días del rango, así que no se podía programar "arranca el
    lunes y dura 1.5 días" — que es como razona el planner.
  · prog_actividades.modo_fecha — cuál de los tres datos (Inicio, Fin, Plazo)
    es el DERIVADO, igual que el scheduling mode de P6/MS Project:
      INICIO_PLAZO  fijas inicio y plazo  → se calcula el fin  (por defecto)
      FIN_PLAZO     fijas fin y plazo     → se calcula el inicio
      INICIO_FIN    fijas ambas fechas    → el plazo es de solo lectura
  · prog_dependencias.tipo — hasta ahora era TEXT libre con default 'FS' y el
    motor de cascada solo entendía FS. Se acota con CHECK a FS/SS/FF, que son
    los tres que la cascada implementa desde esta tanda.

Backfill: el plazo de cada actividad existente se calcula con su calendario
real (prog_config.dias_semana + prog_feriados + sus dias_salto/dias_medio), de
modo que al terminar la migración `plazo` cuadra exactamente con el rango que
ya tenía y NINGUNA actividad cambia de fechas.
"""
from alembic import op

revision = "0034_plazo_y_tipos_vinculo"
down_revision = "0033_frente"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE prog_actividades
  ADD COLUMN IF NOT EXISTS plazo_dias NUMERIC(6,1),
  ADD COLUMN IF NOT EXISTS modo_fecha TEXT NOT NULL DEFAULT 'INICIO_PLAZO';

ALTER TABLE prog_actividades
  DROP CONSTRAINT IF EXISTS prog_actividades_modo_fecha_check;
ALTER TABLE prog_actividades
  ADD CONSTRAINT prog_actividades_modo_fecha_check
  CHECK (modo_fecha IN ('INICIO_PLAZO','FIN_PLAZO','INICIO_FIN'));

-- Plazo de lo ya programado = Σ pesos de los días hábiles de su rango.
-- generate_series recorre el rango día a día; se descartan los días no
-- laborables del proyecto, los feriados y los saltos de la actividad, y los
-- medios días cuentan 0.5. Sin filas afectadas si la tabla está vacía.
UPDATE prog_actividades a
   SET plazo_dias = sub.plazo
  FROM (
    SELECT a2.id,
           SUM(CASE WHEN s.ts::date = ANY(a2.dias_medio) THEN 0.5 ELSE 1 END) AS plazo
      FROM prog_actividades a2
      LEFT JOIN prog_config c ON c.proyecto_id = a2.proyecto_id
      CROSS JOIN LATERAL generate_series(
             a2.fecha, COALESCE(a2.fecha_fin, a2.fecha), interval '1 day') AS s(ts)
     WHERE EXTRACT(ISODOW FROM s.ts)::int = ANY(COALESCE(c.dias_semana, '{1,2,3,4,5,6,7}'::int[]))
       AND NOT (s.ts::date = ANY(a2.dias_salto))
       AND NOT EXISTS (SELECT 1 FROM prog_feriados f
                        WHERE f.proyecto_id = a2.proyecto_id AND f.fecha = s.ts::date)
     GROUP BY a2.id
  ) sub
 WHERE sub.id = a.id;

-- Rango sin ningún día hábil (todo feriado/salto): plazo 0, no NULL.
UPDATE prog_actividades SET plazo_dias = 0 WHERE plazo_dias IS NULL;

ALTER TABLE prog_dependencias
  DROP CONSTRAINT IF EXISTS prog_dependencias_tipo_check;
UPDATE prog_dependencias SET tipo = 'FS' WHERE tipo NOT IN ('FS','SS','FF');
ALTER TABLE prog_dependencias
  ADD CONSTRAINT prog_dependencias_tipo_check CHECK (tipo IN ('FS','SS','FF'));
"""

_DOWN = """
ALTER TABLE prog_dependencias
  DROP CONSTRAINT IF EXISTS prog_dependencias_tipo_check;
-- Los vínculos SS/FF vuelven a FS: sin el motor nuevo no se saben interpretar.
UPDATE prog_dependencias SET tipo = 'FS' WHERE tipo <> 'FS';
ALTER TABLE prog_actividades
  DROP CONSTRAINT IF EXISTS prog_actividades_modo_fecha_check;
ALTER TABLE prog_actividades
  DROP COLUMN IF EXISTS plazo_dias,
  DROP COLUMN IF EXISTS modo_fecha;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
