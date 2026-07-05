# -*- coding: utf-8 -*-
"""F0.6: identidad de supervisor en usuarios.

Columna aditiva `usuarios.supervisor_id` (FK a supervisores). Obligatoria a nivel de
aplicación para usuarios con rol 'supervisor' (el endpoint de creación la exige); a nivel
de esquema queda NULLable porque admin/oficina no la usan.

El token JWT emite `sup_id` y los endpoints de campo verifican que un supervisor solo
opere con su propia identidad (anti-suplantación).
"""
from alembic import op

revision = "0011_usuarios_supervisor"
down_revision = "0010_drop_vista_legacy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE usuarios ADD COLUMN supervisor_id varchar(10)")
    op.execute(
        "ALTER TABLE usuarios ADD CONSTRAINT fk_usuarios_supervisor "
        "FOREIGN KEY (supervisor_id) REFERENCES supervisores(id)"
    )


def downgrade():
    op.execute("ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS fk_usuarios_supervisor")
    op.execute("ALTER TABLE usuarios DROP COLUMN IF EXISTS supervisor_id")
