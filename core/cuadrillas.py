# -*- coding: utf-8 -*-
"""Reglas compartidas de cuadrillas habituales (0049).

Vive en `core/` y no en `routers/tareo.py` porque la usan los dos lados —el alta
del panel y la del tareo (`/ev/cuadrillas-plantilla`)— y `routers.tareo` importa
`routers.valor_ganado`, que a su vez carga `routers.ev.historico`: importarlo
desde ahí cierra el círculo.
"""


async def marcar_habitual(con, supervisor_id, grupo_id: int) -> None:
    """Quien acaba de armar una cuadrilla la usa: entra como habitual suya.

    Sin esto, un supervisor que guarda su lista desde el teléfono la vería caer
    al fondo del catálogo, revuelta con las de todos, justo la mañana siguiente.
    Es la única forma AUTOMÁTICA de marcar habitual; el resto lo decide oficina.

    El `WHERE EXISTS` no es paranoia: por `/ev/cuadrillas-plantilla` entra el
    `supervisor_id` que manda el teléfono, y uno dado de baja tumbaría la FK y
    con ella el guardado entero de la cuadrilla. Que el atajo de pantalla no se
    lleve por delante el dato es lo importante.
    """
    if not supervisor_id:
        return
    # El SELECT sale de `supervisores` en vez de ser `SELECT $1 WHERE EXISTS(…)`:
    # así el id insertado ES la columna de la tabla y Postgres no tiene que
    # deducir el tipo de $1 desde dos sitios a la vez (AmbiguousParameterError).
    await con.execute(
        "INSERT INTO cuadrilla_habituales (supervisor_id, grupo_id) "
        "SELECT s.id, $2::int FROM supervisores s WHERE s.id = $1 "
        "ON CONFLICT DO NOTHING",
        supervisor_id, grupo_id)
