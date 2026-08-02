# -*- coding: utf-8 -*-
"""Base del libro mayor de fiabilidad de compromisos (encargo de Jean 2026-08-02).

`prog_restricciones` ya guarda tipo, responsable y fecha_requerida, y el sistema
ya casa el compromiso congelado con el cumplimiento. Eso permite medir, por
responsable y por tipo, cuánto tarda de verdad en liberarse una restricción — y
nadie más puede hacerlo porque nadie más tiene esas marcas de tiempo casadas.
Faltaban tres cosas para que el dato sirva:

1. `responsable` es TEXT LIBRE. Es el mismo patrón que la auditoría del 25-jul
   destapó con «área»: en cuanto un dato se convierte en EJE DE ANÁLISIS, el
   texto libre lo rompe — «Logística», «LOGISTICA» y «logistica » son tres
   responsables distintos para un GROUP BY. `prog_responsables` lo normaliza a
   entidad. Es un ÁREA y no una persona a propósito: quien responde por los
   materiales es Logística, aunque el que conteste el teléfono cambie.

2. `liberada_en` NO es cuándo se liberó, es cuándo alguien lo MARCÓ. Si el
   planner limpia cinco restricciones el viernes por la tarde, esa columna mide
   su hábito de captura, no la latencia del responsable. `liberada_el` (DATE,
   editable) es la fecha REAL; `liberada_en` se queda como sello de captura. La
   métrica usa la real y, cuando no la hay, cae al sello CONTÁNDOLO aparte: un
   promedio que mezcla ambas sin decirlo miente.

3. Al desmarcar `liberada`, el código pone `liberada_en = NULL`: el historial se
   perdía entero. `prog_restriccion_eventos` es append-only y sin FK a la
   restricción, para que la traza sobreviva incluso al borrado de la fila.

SIN BACKFILL de `liberada_el`, a propósito y por la misma razón que la 0041 no
reconstruyó el metrado congelado: copiar el sello de captura a la fecha real
sería inventar una medición que nadie hizo. Las restricciones ya liberadas
entran a la métrica por el camino «derivada», que se reporta aparte.

El catálogo SÍ se siembra con los responsables ya escritos —eso no inventa nada,
solo normaliza lo que el planner tecleó— y las restricciones quedan ligadas.
"""
from alembic import op

revision = "0044_fiabilidad_restricciones"
down_revision = "0043_tareo_edicion"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS prog_responsables (
    id            SERIAL PRIMARY KEY,
    proyecto_id   INT          NOT NULL DEFAULT 1,
    nombre        TEXT         NOT NULL,
    tipo          TEXT         NOT NULL DEFAULT 'INTERNA',
    supervisor_id VARCHAR(10)  REFERENCES supervisores(id),
    activo        BOOLEAN      NOT NULL DEFAULT true,
    creado_en     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT prog_responsables_tipo_check
      CHECK (tipo IN ('INTERNA', 'CLIENTE', 'PROVEEDOR', 'SUBCONTRATA')),
    CONSTRAINT prog_responsables_nombre_uq UNIQUE (proyecto_id, nombre)
);

ALTER TABLE prog_restricciones
  ADD COLUMN IF NOT EXISTS responsable_id INT REFERENCES prog_responsables(id),
  ADD COLUMN IF NOT EXISTS liberada_el    DATE;

CREATE INDEX IF NOT EXISTS idx_progrest_resp ON prog_restricciones (responsable_id);
CREATE INDEX IF NOT EXISTS idx_progrest_tipo ON prog_restricciones (tipo, liberada);

-- Semilla del catálogo con lo que el planner ya escribió, normalizado. No
-- inventa: agrupa. Las tildes se quitan con translate y no con unaccent (que
-- es una EXTENSIÓN y no está garantizada en el Postgres de producción): sin
-- eso «Logística» y «LOGISTICA» entrarían como dos áreas distintas, que es
-- exactamente el duplicado que este catálogo viene a cerrar.
INSERT INTO prog_responsables (proyecto_id, nombre)
SELECT DISTINCT 1,
       translate(upper(btrim(regexp_replace(responsable, '\\s+', ' ', 'g'))),
                 'ÁÀÄÂÉÈËÊÍÌÏÎÓÒÖÔÚÙÜÛÑÇ', 'AAAAEEEEIIIIOOOOUUUUNC')
  FROM prog_restricciones
 WHERE responsable IS NOT NULL AND btrim(responsable) <> ''
ON CONFLICT (proyecto_id, nombre) DO NOTHING;

UPDATE prog_restricciones r
   SET responsable_id = a.id
  FROM prog_responsables a
 WHERE r.responsable_id IS NULL
   AND r.responsable IS NOT NULL
   AND a.nombre = translate(upper(btrim(regexp_replace(r.responsable, '\\s+', ' ', 'g'))),
                            'ÁÀÄÂÉÈËÊÍÌÏÎÓÒÖÔÚÙÜÛÑÇ', 'AAAAEEEEIIIIOOOOUUUUNC');

CREATE TABLE IF NOT EXISTS prog_restriccion_eventos (
    id              SERIAL PRIMARY KEY,
    restriccion_id  INT          NOT NULL,
    actividad_id    INT,
    accion          TEXT         NOT NULL,
    tipo            TEXT,
    responsable_id  INT,
    fecha_requerida DATE,
    liberada_el     DATE,
    latencia_dias   INT,
    actor           TEXT,
    notas           TEXT,
    creado_en       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT prog_rest_eventos_accion_check
      CHECK (accion IN ('crear', 'liberar', 'reabrir', 'editar', 'eliminar'))
);

CREATE INDEX IF NOT EXISTS idx_progresteve_rest ON prog_restriccion_eventos (restriccion_id);
CREATE INDEX IF NOT EXISTS idx_progresteve_fecha ON prog_restriccion_eventos (creado_en);
"""

_DOWN = """
DROP TABLE IF EXISTS prog_restriccion_eventos;
DROP INDEX IF EXISTS idx_progrest_tipo;
DROP INDEX IF EXISTS idx_progrest_resp;
ALTER TABLE prog_restricciones
  DROP COLUMN IF EXISTS liberada_el,
  DROP COLUMN IF EXISTS responsable_id;
DROP TABLE IF EXISTS prog_responsables;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
