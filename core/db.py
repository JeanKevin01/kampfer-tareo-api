"""Pool asyncpg ÚNICO del API (F0.4).

Antes había DOS capas de acceso a BD: la lib `databases` (main/presupuesto/ro) y un pool
asyncpg privado en valor_ganado que nunca se cerraba. Este módulo es el único dueño del
pool asyncpg; `lifespan` (main.py) lo calienta al arrancar y lo cierra al apagar.
La lib `databases` sigue en main.py/presupuesto.py durante la transición (F0.4 continúa).

Uso:
    from core.db import db
    pool = await db()
    async with pool.acquire() as con: ...
"""
import os
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def db() -> asyncpg.Pool:
    """Devuelve el pool compartido (lazy: lo crea en el primer uso)."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"], min_size=2, max_size=10
        )
    return _pool


async def close_pool() -> None:
    """Cierra el pool (lo llama lifespan al apagar; antes se filtraban conexiones)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
