# -*- coding: utf-8 -*-
"""Corrección de HH desde oficina, con traza (encargo de Jean 2026-08-02).

El caso: dos supervisores tarean a la misma persona el mismo día y quedan 13.5
HH contra una jornada de 9.5. Hasta ahora eso NO se podía arreglar desde el
panel —no existe ningún endpoint que edite `tareo_partida`—; el único camino
era que el supervisor reenviara desde la app, y su reenvío borra y reescribe su
día entero.

Dos columnas y una tabla:

· `editado_por`/`editado_en`/`motivo_edicion` marcan la línea corregida en
  oficina. Es la marca de la que depende la regla que decidió Jean —«la
  corrección de oficina gana sobre todo»—: el reenvío del supervisor NO borra
  ni pisa una línea marcada. Sin la marca en la propia fila esa regla sería
  imposible de aplicar, porque el reenvío llega como un lote nuevo que no sabe
  qué se tocó antes.

· Anular una línea NO la borra: la deja en 0 CON la marca. Un borrado físico
  perdería la marca y el siguiente reenvío del supervisor la recrearía tal
  cual, deshaciendo la corrección en silencio — justo lo que la regla quiere
  impedir. En 0 no suma al EV ni a la curva, y se sigue viendo que alguien la
  anuló a propósito en vez de parecer un dato que falta.

· `tareo_ediciones` es la traza append-only: quién, cuándo, valor anterior y
  motivo. Editar HH es tocar lo que un supervisor firmó y lo que alimenta el
  ISP y la valorización; sin traza, un número corregido es indistinguible de un
  número mal capturado. No lleva FK a `tareo_partida` a propósito: la traza
  debe sobrevivir aunque la línea desaparezca.
"""
from alembic import op

revision = "0043_tareo_edicion"
down_revision = "0042_actividad_externa"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE tareo_partida
  ADD COLUMN IF NOT EXISTS editado_por    VARCHAR(60),
  ADD COLUMN IF NOT EXISTS editado_en     TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS motivo_edicion TEXT;

-- El reenvío del supervisor consulta «¿qué líneas de este día/OTM están
-- corregidas?» en cada envío: sin el índice parcial es un scan por fecha.
CREATE INDEX IF NOT EXISTS idx_tp_editado
  ON tareo_partida (fecha, otm_id) WHERE editado_por IS NOT NULL;

CREATE TABLE IF NOT EXISTS tareo_ediciones (
    id               SERIAL PRIMARY KEY,
    tareo_id         INTEGER,
    accion           VARCHAR(10)  NOT NULL,
    trabajador_id    VARCHAR(10)  NOT NULL,
    fecha            DATE         NOT NULL,
    otm_id           VARCHAR(50),
    partida_id_antes INTEGER,
    partida_id       INTEGER,
    hh_antes         NUMERIC(6,4),
    hh               NUMERIC(6,4),
    motivo           TEXT,
    usuario          VARCHAR(60)  NOT NULL,
    creado_en        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT tareo_ediciones_accion_check
      CHECK (accion IN ('crear', 'editar', 'anular'))
);

CREATE INDEX IF NOT EXISTS idx_ted_fecha ON tareo_ediciones (fecha);
CREATE INDEX IF NOT EXISTS idx_ted_trab  ON tareo_ediciones (trabajador_id, fecha);
"""

_DOWN = """
DROP TABLE IF EXISTS tareo_ediciones;
DROP INDEX IF EXISTS idx_tp_editado;
ALTER TABLE tareo_partida
  DROP COLUMN IF EXISTS motivo_edicion,
  DROP COLUMN IF EXISTS editado_en,
  DROP COLUMN IF EXISTS editado_por;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
