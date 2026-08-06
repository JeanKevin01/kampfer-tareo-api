# ============================================================
# core/ids.py — normalización de identificadores del padrón.
#
# El `zfill(3)` estaba repetido en cada endpoint de cuadrilla de tareo.py y
# AUSENTE en el de campo (historico.py), que guardaba `str(tid)` a secas. Con
# ids cortos eso son dos filas distintas para la misma persona: el panel graba
# '007' y el teléfono '7'. Aquí hay una sola versión.
# ============================================================
from typing import Iterable, List


def norm_trab_id(valor) -> str:
    """Id de trabajador tal como vive en la BD.

    Los correlativos cortos se guardan con ceros a la izquierda ('7' → '007').
    `zfill` no toca los ids que ya son más largos, así que es seguro para el
    varchar(10) que dejó la 0009. El vacío se devuelve vacío: `"".zfill(3)` da
    '000', que es un id perfectamente válido de otra persona.
    """
    s = str(valor or "").strip()
    return s.zfill(3) if s else ""


def ids_unicos(valores: Iterable) -> List[str]:
    """Normaliza y quita repetidos CONSERVANDO el orden.

    El orden importa: es el que se guarda como `orden` de la cuadrilla y el que
    el supervisor ve en su teléfono.
    """
    vistos: set = set()
    salida: List[str] = []
    for v in valores or []:
        t = norm_trab_id(v)
        if t and t not in vistos:
            vistos.add(t)
            salida.append(t)
    return salida
