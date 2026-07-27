# ============================================================
# routers/ev/_modelos.py — modelos Pydantic del módulo EV (F0.5b)
# ============================================================
from typing import Optional  # noqa: F401

from pydantic import BaseModel, Field


# ---------------------- Modelos ----------------------
class HitoIn(BaseModel):
    numero: int = Field(ge=1, le=10)
    descripcion: str = ""
    peso: float = Field(gt=0, le=1)
    es_principal: bool = False


class PartidaIn(BaseModel):
    codigo: str
    otm_id: Optional[str] = None
    fase: str
    sub_fase: Optional[str] = None
    descripcion: str
    unidad: str
    sistema: Optional[str] = None
    metrado_presup: float = 0
    metrado_proyec: Optional[float] = None
    hh_presup: float = 0
    hh_actualizado: Optional[float] = None   # #6: presupuesto actualizado (default = hh_presup)
    tipo_costo: Optional[str] = None         # 'DIRECTO' (def) | 'INDIRECTO'
    naturaleza: Optional[str] = None         # 'CONTRACTUAL' (def) | 'ADICIONAL'
    hitos: list[HitoIn]


class AvanceIn(BaseModel):
    hito_id: int
    cantidad_acum: float = Field(ge=0)


class HHIn(BaseModel):
    partida_id: int
    hh: float = Field(ge=0)


class CapturaIn(BaseModel):
    semana: int
    avances: list[AvanceIn] = []
    hh_gastadas: list[HHIn] = []


class ImpPartida(BaseModel):
    codigo: str
    otm_id: Optional[str] = None
    fase: Optional[str] = None           # None para nodos padre del WBS
    sub_fase: Optional[str] = None
    descripcion: str
    unidad: Optional[str] = None         # None para nodos padre
    sistema: Optional[str] = None
    metrado_presup: float = 0
    metrado_proyec: Optional[float] = None
    hh_presup: float = 0
    hh_actualizado: Optional[float] = None  # #6: presupuesto actualizado (default = hh_presup)
    tipo_costo: Optional[str] = None      # 'DIRECTO' (def) | 'INDIRECTO'
    naturaleza: Optional[str] = None      # 'CONTRACTUAL' (def) | 'ADICIONAL'
    tipo_actividad: Optional[str] = None
    hitos: Optional[list[HitoIn]] = None
    nivel: Optional[int] = None          # profundidad en el WBS (calculado si None)
    parent_codigo: Optional[str] = None  # código del nodo padre (calculado si None)


class ImpAvance(BaseModel):
    codigo: str
    semana: int
    hito: int = Field(ge=1, le=10)
    cantidad_acum: float = Field(ge=0)


class ImpHH(BaseModel):
    codigo: str
    semana: int
    hh: float = Field(ge=0)


class ImportarIn(BaseModel):
    partidas: list[ImpPartida]
    avances: list[ImpAvance] = []
    hh: list[ImpHH] = []


class ImproductivaIn(BaseModel):
    """#5: HH improductivas capturadas en oficina, por OTM y semana.
    #4: partida_id opcional para atribuirlas a una partida (columna W del gerente)."""
    otm_id: Optional[str] = None
    semana: int = Field(ge=1)
    hh: float = Field(ge=0)
    motivo: Optional[str] = None
    nota: Optional[str] = None
    partida_id: Optional[int] = None


class ValorizadoIn(BaseModel):
    """#2: cantidad valorizada (reconocida por el cliente) de una partida en una semana."""
    partida_id: int
    semana: int = Field(ge=1)
    cantidad_valorizada: float = Field(ge=0)


class TarifaIn(BaseModel):
    """Rentabilidad: tarifa de Mano de Obra (S/./HH) por cargo. cargo='(Default)' = respaldo."""
    cargo: str
    costo_hh: float = Field(ge=0)


