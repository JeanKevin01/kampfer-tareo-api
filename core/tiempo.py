"""Fechas y hora de Lima — implementación ÚNICA (F0.4).

Antes estos helpers vivían duplicados en main.py y valor_ganado.py (con el riesgo real de
que divergieran: el bug de la semana inconsistente nació así). Todo el API importa de aquí.
"""
import re
from datetime import date, datetime, timedelta, timezone

LIMA = timezone(timedelta(hours=-5))   # Perú no tiene horario de verano


def ahora_lima() -> datetime:
    return datetime.now(LIMA)


def fecha_lima() -> date:
    return ahora_lima().date()


def hora_lima() -> str:
    return ahora_lima().strftime("%H:%M:%S")


def hora_lima_t():
    """Hora como objeto time (para columnas SQL `time`)."""
    return ahora_lima().time().replace(microsecond=0)


def fecha_de(dt):
    """Día de LIMA de un timestamptz. `dt.date()` a secas da el día en UTC.

    Postgres devuelve los `TIMESTAMPTZ` en UTC, así que a partir de las 19:00 de
    Lima `.date()` ya contesta el día siguiente. Sobre un sello de captura eso
    corre la medición un día entero, y hacia el lado que no se nota: parece que
    se registró más tarde de lo que fue."""
    return dt.astimezone(LIMA).date() if dt else None


def parse_fecha(v):
    """Convierte v a un objeto date (o None). Acepta date/datetime, 'YYYY-MM-DD'
    y 'DD/MM/YYYY'. Necesario porque asyncpg exige date, no str, en columnas date."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        try:
            return date(int(y), int(mo), int(d))
        except ValueError:
            return None
    return None


def semana_de(fecha: date, base: date) -> int:
    """Número de semana del proyecto (base = lunes de la semana 1)."""
    return (fecha - base).days // 7 + 1
