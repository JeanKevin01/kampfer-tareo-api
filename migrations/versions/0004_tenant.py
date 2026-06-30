"""tenant-ready: empresas / proyectos / usuario_proyectos + columnas de tenencia

Decisiones de negocio:
  · Padrón (trabajadores, supervisores, usuarios) = a nivel EMPRESA (compartido entre proyectos).
  · Un usuario puede acceder a VARIOS proyectos (tabla usuario_proyectos) — habilita el selector.
  · La OTM es el agrupador de proyecto → otms.proyecto_id.

Seguro/invisible: las columnas nuevas llevan DEFAULT al proyecto/empresa actual (id=1),
así la app sigue insertando/leyendo sin cambios. El scoping por proyecto y el selector
en el panel vienen en el paso siguiente (0.3b).

Revision ID: 0004_tenant
Revises: 0003_drop_stp
Create Date: 2026-06-29
"""
from alembic import op

revision = "0004_tenant"
down_revision = "0003_drop_stp"
branch_labels = None
depends_on = None

_UP = """
-- 1) Empresa (raíz de tenencia) + seed del tenant actual
CREATE TABLE empresas (
    id        SERIAL PRIMARY KEY,
    slug      TEXT UNIQUE NOT NULL,
    nombre    TEXT NOT NULL,
    ruc       TEXT,
    activo    BOOLEAN NOT NULL DEFAULT true,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO empresas (id, slug, nombre) VALUES (1, 'kampfer', 'KAMPFER');
SELECT setval('empresas_id_seq', 1, true);

-- 2) Proyecto (bajo empresa) + seed del proyecto SMCV actual
CREATE TABLE proyectos (
    id         SERIAL PRIMARY KEY,
    empresa_id INTEGER NOT NULL REFERENCES empresas(id),
    codigo     TEXT NOT NULL,
    nombre     TEXT NOT NULL,
    cliente    TEXT,
    ubicacion  TEXT,
    activo     BOOLEAN NOT NULL DEFAULT true,
    creado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (empresa_id, codigo)
);
INSERT INTO proyectos (id, empresa_id, codigo, nombre, cliente, ubicacion)
VALUES (1, 1, 'SMCV-MISC', 'SMCV Misceláneos', 'Sociedad Minera Cerro Verde', 'Arequipa');
SELECT setval('proyectos_id_seq', 1, true);

-- 3) Acceso de usuarios a proyectos (muchos-a-muchos) + dar acceso al proyecto 1 a los existentes
CREATE TABLE usuario_proyectos (
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    proyecto_id INTEGER NOT NULL REFERENCES proyectos(id) ON DELETE CASCADE,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (usuario_id, proyecto_id)
);
INSERT INTO usuario_proyectos (usuario_id, proyecto_id) SELECT id, 1 FROM usuarios;

-- 4) Padrón a nivel empresa (DEFAULT 1 → la app sigue funcionando sin cambios)
ALTER TABLE trabajadores ADD COLUMN empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id);
ALTER TABLE supervisores ADD COLUMN empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id);
ALTER TABLE usuarios     ADD COLUMN empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id);

-- 5) La OTM pertenece a un proyecto (DEFAULT 1)
ALTER TABLE otms ADD COLUMN proyecto_id INTEGER NOT NULL DEFAULT 1 REFERENCES proyectos(id);
CREATE INDEX idx_otms_proyecto ON otms(proyecto_id);
"""

_DOWN = """
DROP INDEX IF EXISTS idx_otms_proyecto;
ALTER TABLE otms         DROP COLUMN IF EXISTS proyecto_id;
ALTER TABLE usuarios     DROP COLUMN IF EXISTS empresa_id;
ALTER TABLE supervisores DROP COLUMN IF EXISTS empresa_id;
ALTER TABLE trabajadores DROP COLUMN IF EXISTS empresa_id;
DROP TABLE IF EXISTS usuario_proyectos;
DROP TABLE IF EXISTS proyectos;
DROP TABLE IF EXISTS empresas;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_UP)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(_DOWN)
