# -*- coding: utf-8 -*-
"""Guardia contra la lección 0008: constraint disfrazada de índice.

El `0001_baseline.sql` salió de un dump que imprime las UNIQUE CONSTRAINT de
producción como `CREATE UNIQUE INDEX`. La distinción se perdió ahí: los objetos
que vienen del baseline son índices sueltos en cualquier BD local o de CI, y
constraints de verdad en el VPS.

La consecuencia es que `DROP INDEX IF EXISTS x` NO es inofensivo contra esos
nombres. Si `x` respalda una constraint, Postgres aborta con
DependentObjectsStillExist en vez de ignorarlo — y como Alembic corre toda la
tanda en una transacción, se cae el deploy entero. En local pasa siempre; en
prod falla siempre. **El CI no puede cazarlo**: su BD se construye desde el
baseline, o sea desde la forma equivocada.

Por eso el chequeo es textual. La regla es soltar primero la constraint (que se
lleva su propio índice) y después intentar el índice suelto, que es lo que ya
hacían la 0025 y la 0038 y lo que a la 0048 se le olvidó.
"""
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"
BASELINE = VERSIONS / "0001_baseline.sql"

_DROP_INDEX = re.compile(r"DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
_DROP_CONSTRAINT = re.compile(r"DROP\s+CONSTRAINT\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)


def _nombres_del_baseline() -> set:
    """Objetos nacidos en el dump: de estos no sabemos si son índice o constraint."""
    texto = BASELINE.read_text(encoding="utf-8", errors="ignore")
    return set(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+([A-Za-z_][\w]*)", texto, re.I))


def _migraciones():
    return sorted(p for p in VERSIONS.glob("*.py") if p.name != "__init__.py")


def _riesgosos(texto: str, del_baseline: set):
    """(nombre, posición) de cada DROP INDEX sobre un objeto del baseline."""
    return [(m.group(1), m.start()) for m in _DROP_INDEX.finditer(texto)
            if m.group(1) in del_baseline]


def test_hay_migraciones_y_baseline():
    """Si los paths se rompen, el chequeo pasaría en vacío sin proteger nada."""
    assert BASELINE.exists()
    assert len(_migraciones()) > 40
    assert len(_nombres_del_baseline()) > 20


@pytest.mark.parametrize("ruta", _migraciones(), ids=lambda p: p.stem)
def test_drop_index_del_baseline_suelta_antes_la_constraint(ruta):
    del_baseline = _nombres_del_baseline()
    texto = ruta.read_text(encoding="utf-8", errors="ignore")

    for nombre, pos in _riesgosos(texto, del_baseline):
        previos = [m.start() for m in _DROP_CONSTRAINT.finditer(texto)
                   if m.group(1) == nombre and m.start() < pos]
        assert previos, (
            f"{ruta.name}: `DROP INDEX ... {nombre}` sin soltar antes su posible "
            f"constraint. Ese objeto viene del baseline, así que en producción "
            f"puede ser una UNIQUE CONSTRAINT y el DROP INDEX tumba la migración. "
            f"Poner delante: ALTER TABLE ... DROP CONSTRAINT IF EXISTS {nombre};"
        )


def test_el_chequeo_detecta_el_orden_invertido():
    """Control: sin esto, un chequeo que nunca encuentra nada también pasaría."""
    malo = ("DROP INDEX IF EXISTS cuadrilla_grupos_supervisor_id_nombre_key;\n"
            "ALTER TABLE t DROP CONSTRAINT IF EXISTS cuadrilla_grupos_supervisor_id_nombre_key;")
    del_baseline = _nombres_del_baseline()
    assert "cuadrilla_grupos_supervisor_id_nombre_key" in del_baseline
    [(nombre, pos)] = _riesgosos(malo, del_baseline)
    assert not [m.start() for m in _DROP_CONSTRAINT.finditer(malo)
                if m.group(1) == nombre and m.start() < pos]
