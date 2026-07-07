# ============================================================
# parsers/plantilla_pu.py — parser PURO de la plantilla
# "PU - Mano de Obra, Materiales y Equipos - Rev01 Meta Metrado.xls" (F1.2)
#
# bytes .xls (BIFF, lib xlrd) → dataclasses. SIN BD, SIN FastAPI.
#
# Formato (validado contra el archivo real, 2026-07-06):
#   Hoja PtoMeta  (222×14, header idx 8): jerarquía del presupuesto.
#     D=Item con A(Fase) vacía → nodo padre (nivel = nº de segmentos de D).
#     Fila con A → partida hoja: A=fase B=sub_fase D=codigo E=desc F=und
#     G=metrado H=PU meta I=parcial meta L=PU oferta M=parcial oferta.
#   Hoja PU-Meta (4,453×21, header idx 7): APU por bloques.
#     'HH - Dia' en fila idx 5 col I.
#     H=='Partida'   → nuevo bloque (A='P' hoja: I=codigo J=desc Q=metrado
#                      T=costo partida · A='SP' subpartida: I=descripcion).
#     H=='Rendimiento' → J=rend MO, L=rend EQ, P=Costo Unitario Directo (CUD).
#     I∈{Mano de Obra, Materiales, Equipos, Subpartidas} sin D → cambia sección.
#     Fila con D (MO|MAT|EQ) → recurso: H=codigo I=desc L=und M=cuadrilla
#                      N=cantidad O=precio P=parcial (MO: R=HH tot, S=costo tot).
#     En 'Subpartidas': fila con H=código 909* → referencia SUB (N=cantidad de
#                      uso, O=precio, P=parcial); su APU lo define el bloque 'SP'
#                      con la MISMA descripción.
# ============================================================
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

import xlrd

# Índices de columna (0-based): A=0 B=1 C=2 D=3 E=4 F=5 G=6 H=7 I=8 J=9 K=10
# L=11 M=12 N=13 O=14 P=15 Q=16 R=17 S=18 T=19 U=20
_A, _B, _C, _D, _E, _F, _G, _H, _I, _J, _K, _L, _M, _N, _O, _P, _Q, _R, _S, _T = range(20)

_SECCIONES = {"Mano de Obra": "MO", "Materiales": "MAT", "Equipos": "EQ", "Subpartidas": "SUB"}
_TIPOS_D = {"MO": "MO", "MAT": "MAT", "EQ": "EQ"}


@dataclass
class RecursoPU:
    tipo: str                       # MO | MAT | EQ | SUB
    codigo: str
    descripcion: str
    unidad: Optional[str]
    cuadrilla: Optional[float]
    cantidad: float
    precio: float
    parcial: float
    hh_totales: Optional[float] = None   # solo MO (col R)
    costo_total: Optional[float] = None  # solo MO (col S)
    fila: int = 0                        # nº de fila del Excel (1-based, para errores)
    sub: Optional["PartidaPU"] = None    # solo tipo SUB: la subpartida (canónica) que define su APU


@dataclass
class PartidaPU:
    codigo: str                     # Item de PtoMeta (hojas/padres) o código 909* (subpartidas)
    descripcion: str
    unidad: Optional[str] = None
    fase: Optional[str] = None
    sub_fase: Optional[str] = None
    area: Optional[str] = None
    metrado: float = 0.0
    pu_meta: float = 0.0            # CUD del APU (hojas) / PtoMeta col H
    parcial_meta: float = 0.0       # PtoMeta col I (hojas) o col J (padres: Total)
    pu_oferta: float = 0.0          # PtoMeta col L
    nivel: int = 1                  # padres: nº segmentos; hojas: segmentos; SUBpartidas: 0
    parent: Optional[str] = None
    rend_mo: Optional[float] = None
    rend_eq: Optional[float] = None
    es_hoja: bool = False
    es_sub: bool = False
    recursos: list = field(default_factory=list)   # [RecursoPU]
    cud: Optional[float] = None     # Costo Unitario Directo declarado en PU-Meta
    sps: dict = field(default_factory=dict)        # SP definidas DENTRO de este bloque {desc: PartidaPU}


@dataclass
class ResultadoPU:
    partidas: list = field(default_factory=list)       # padres + hojas (orden PtoMeta)
    subpartidas: list = field(default_factory=list)    # PartidaPU es_sub=True (deduplicadas)
    hh_dia: float = 10.0
    errores: list = field(default_factory=list)        # ["fila N: detalle", ...]
    avisos: list = field(default_factory=list)         # no bloquean el import


def _s(sh, r, c) -> str:
    """Celda como string limpio ('' si no existe)."""
    try:
        v = sh.cell_value(r, c)
    except IndexError:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def _f(sh, r, c) -> Optional[float]:
    """Celda como float (None si vacía o no numérica)."""
    try:
        v = sh.cell_value(r, c)
    except IndexError:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _es_codigo_item(s: str) -> bool:
    """'01', '01.01', '01.01.01', ... (solo dígitos y puntos)."""
    return bool(s) and all(seg.isdigit() for seg in s.split("."))


def _norm_desc(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _desc_match(a: str, b: str) -> bool:
    """La plantilla real tiene referencias con el nombre RECORTADO de la subpartida
    ('... de Radier' vs '... de Radier o losas') → match exacto o por prefijo largo."""
    na, nb = _norm_desc(a), _norm_desc(b)
    if na == nb:
        return True
    corto, largo = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(corto) >= 12 and largo.startswith(corto)


def _buscar_desc(d: dict, desc: str):
    """Busca en un dict {descripcion: X} con _desc_match (exacto primero)."""
    if desc in d:
        return d[desc]
    for k, v in d.items():
        if _desc_match(k, desc):
            return v
    return None


def _parse_ptometa(sh, out: ResultadoPU) -> dict:
    """Jerarquía del presupuesto. Devuelve {codigo_hoja: PartidaPU}."""
    hojas: dict = {}
    for r in range(9, sh.nrows):
        item = _s(sh, r, _D)
        if not _es_codigo_item(item):
            continue
        fase = _s(sh, r, _A)
        nivel = len(item.split("."))
        parent = item.rsplit(".", 1)[0] if "." in item else None
        if not fase:
            # nodo padre
            out.partidas.append(PartidaPU(
                codigo=item, descripcion=_s(sh, r, _E), nivel=nivel, parent=parent,
                parcial_meta=_f(sh, r, _J) or 0.0,
            ))
            continue
        p = PartidaPU(
            codigo=item, descripcion=_s(sh, r, _E), unidad=_s(sh, r, _F) or None,
            fase=fase, sub_fase=_s(sh, r, _B) or None,
            metrado=_f(sh, r, _G) or 0.0,
            pu_meta=_f(sh, r, _H) or 0.0,
            parcial_meta=_f(sh, r, _I) or 0.0,
            pu_oferta=_f(sh, r, _L) or 0.0,
            nivel=nivel, parent=parent, es_hoja=True,
        )
        if item in hojas:
            out.errores.append(f"PtoMeta fila {r+1}: item repetido {item}")
            continue
        hojas[item] = p
        out.partidas.append(p)
    if not hojas:
        out.errores.append("PtoMeta: no se encontró ninguna partida hoja (¿formato distinto?)")
    return hojas


def _parse_pumeta(sh, out: ResultadoPU) -> tuple[dict, dict]:
    """Bloques de APU. Devuelve ({codigo_item: bloque_P}, {descripcion: bloque_SP}).
    Cada bloque es un PartidaPU parcial (rend, cud, recursos)."""
    v = _f(sh, 5, _I)
    if v and v > 0:
        out.hh_dia = v

    # Estructura real (validada contra el archivo): es un ÁRBOL de bloques.
    # En la sección 'Subpartidas' de un bloque, cada referencia 909* va seguida
    # del bloque 'SP' que la define; ese SP puede a su vez abrir SU sección
    # 'Subpartidas' (anidamiento). Al terminar un SP, el control vuelve al bloque
    # ancestro cuya sección 'Subpartidas' sigue abierta. La MISMA descripción de
    # SP puede tener recetas DISTINTAS bajo padres distintos → scope por bloque.
    bloques_p: dict = {}
    pila: list = []          # [PartidaPU] — bloque activo = pila[-1]
    # estado por bloque (id(bloque) → dict): sección actual, si abrió Subpartidas,
    # y qué descripciones de SP espera (refs vistas sin definición todavía).
    estado: dict = {}

    def _st(b):
        return estado.setdefault(id(b), {"seccion": None, "abrio_subs": False, "esperando": []})

    for r in range(8, sh.nrows):
        h = _s(sh, r, _H)
        i = _s(sh, r, _I)
        d = _s(sh, r, _D)

        if h == "Partida":
            if _s(sh, r, _A) == "SP":
                sp = PartidaPU(codigo="", descripcion=i, es_sub=True, nivel=0,
                               area=_s(sh, r, _B) or None,
                               fase=_s(sh, r, _E) or None,
                               sub_fase=_s(sh, r, _F) or None)
                # dueño = el bloque más profundo que está esperando esta definición
                def _esperada(b):
                    return any(_desc_match(e, i) for e in _st(b)["esperando"])
                while len(pila) > 1 and not _esperada(pila[-1]):
                    pila.pop()
                if pila:
                    dueno = pila[-1]
                    for e in _st(dueno)["esperando"]:
                        if _desc_match(e, i):
                            _st(dueno)["esperando"].remove(e)
                            break
                    if i in dueno.sps:
                        out.avisos.append(
                            f"PU-Meta fila {r+1}: subpartida '{i}' definida dos veces "
                            f"bajo el mismo bloque (gana la última)")
                    dueno.sps[i] = sp
                    pila.append(sp)
                else:
                    out.avisos.append(f"PU-Meta fila {r+1}: bloque SP '{i}' sin partida padre (se ignora)")
            else:
                p = PartidaPU(codigo=i, descripcion=_s(sh, r, _J),
                              metrado=_f(sh, r, _Q) or 0.0,
                              area=_s(sh, r, _B) or None, es_hoja=True)
                if i in bloques_p:
                    out.errores.append(f"PU-Meta fila {r+1}: bloque de partida repetido {i}")
                    pila = []
                else:
                    bloques_p[i] = p
                    pila = [p]
            continue

        if not pila:
            continue
        activo = pila[-1]

        if h == "Rendimiento":
            activo.rend_mo = _f(sh, r, _J)
            activo.rend_eq = _f(sh, r, _L)
            if _s(sh, r, _O).lower().startswith("costo unitario"):
                activo.cud = _f(sh, r, _P)
            continue

        if not d and not h and i in _SECCIONES:
            st = _st(activo)
            st["seccion"] = _SECCIONES[i]
            if st["seccion"] == "SUB":
                st["abrio_subs"] = True
            continue

        if d in _TIPOS_D and h:
            rec = RecursoPU(
                tipo=_TIPOS_D[d], codigo=h, descripcion=i,
                unidad=_s(sh, r, _L) or None, cuadrilla=_f(sh, r, _M),
                cantidad=_f(sh, r, _N) or 0.0, precio=_f(sh, r, _O) or 0.0,
                parcial=_f(sh, r, _P) or 0.0, fila=r + 1,
            )
            if rec.tipo == "MO":
                rec.hh_totales = _f(sh, r, _R)
                rec.costo_total = _f(sh, r, _S)
            activo.recursos.append(rec)
            continue

        # Fila de cierre de bloque: sin D/H/I, con P == CUD de ALGÚN bloque de la
        # pila (cierra ese bloque y todos los anidados dentro de él; los bloques
        # internos no siempre tienen su propia fila de cierre).
        if not d and not h and not i and len(pila) > 1:
            p_val = _f(sh, r, _P)
            if p_val is not None:
                for j in range(len(pila) - 1, 0, -1):
                    cud_j = pila[j].cud
                    if cud_j and abs(p_val - cud_j) <= 0.005:
                        del pila[j:]
                        break
            continue

        # Referencia a subpartida: sin D, con código NUMÉRICO en H y descripción en I.
        if h and i and not d and h.isdigit():
            # pertenece al bloque más profundo con sección Subpartidas abierta
            while len(pila) > 1 and not _st(pila[-1])["abrio_subs"]:
                pila.pop()
            activo = pila[-1]
            activo.recursos.append(RecursoPU(
                tipo="SUB", codigo=h, descripcion=i,
                unidad=_s(sh, r, _L) or None, cuadrilla=None,
                cantidad=_f(sh, r, _N) or 0.0, precio=_f(sh, r, _O) or 0.0,
                parcial=_f(sh, r, _P) or 0.0, fila=r + 1,
            ))
            _st(activo)["esperando"].append(i)
            continue

    return bloques_p


def _verificar(p: PartidaPU, out: ResultadoPU, tol_cud: float = 0.02, tol_hh: float = 0.1):
    """Invariantes del APU declarados por la plantilla."""
    if p.cud is not None and p.recursos:
        suma = sum(r.parcial for r in p.recursos)
        if p.cud == 0:
            # Realidad de la plantilla: partidas "no consideradas" en la meta llevan
            # CUD 0 aunque el APU liste recursos. No es error: el dinero meta es 0.
            out.avisos.append(
                f"{'SP ' + p.descripcion if p.es_sub else p.codigo}: CUD declarado en 0 "
                f"(partida no considerada en la meta; recursos suman {suma:.4f})")
        elif abs(suma - p.cud) > max(tol_cud, p.cud * 0.001):
            if p.es_sub:
                # El costo del presupuesto usa el PARCIAL de la referencia (validado a
                # nivel de partida y contra PtoMeta), así que una receta interna
                # inconsistente de la plantilla no bloquea el import — pero se avisa.
                out.avisos.append(
                    f"SP {p.descripcion}: la receta suma {suma:.4f} pero la plantilla "
                    f"declara CUD {p.cud:.4f} (inconsistencia del Excel de origen)")
            else:
                out.errores.append(
                    f"{p.codigo}: Σ parciales de recursos ({suma:.4f}) no cuadra "
                    f"con el CUD declarado ({p.cud:.4f})")
    if p.es_hoja and p.metrado > 0:
        hh_calc = sum(r.cantidad for r in p.recursos if r.tipo == "MO") * p.metrado
        hh_decl = sum(r.hh_totales or 0 for r in p.recursos if r.tipo == "MO")
        if hh_decl and abs(hh_calc - hh_decl) > max(tol_hh, hh_decl * 0.001):
            out.errores.append(
                f"{p.codigo}: HH calculadas ({hh_calc:.2f}) no cuadran con la col R ({hh_decl:.2f})")


def parsear_plantilla_pu(contenido: bytes) -> ResultadoPU:
    """Punto de entrada: bytes del .xls → ResultadoPU. Nunca lanza por datos
    (los problemas van a .errores/.avisos); sí lanza si el archivo no es un
    .xls legible o faltan las hojas."""
    out = ResultadoPU()
    wb = xlrd.open_workbook(file_contents=contenido)
    faltan = {"PtoMeta", "PU-Meta"} - set(wb.sheet_names())
    if faltan:
        out.errores.append(f"Faltan hojas requeridas: {sorted(faltan)}")
        return out

    hojas = _parse_ptometa(wb.sheet_by_name("PtoMeta"), out)
    bloques_p = _parse_pumeta(wb.sheet_by_name("PU-Meta"), out)

    # ── Enlace SUB → subpartida (por descripción, DENTRO del bloque padre) ──
    # La misma descripción puede tener recetas distintas bajo padres distintos, así
    # que la canonicidad se decide por la RECETA: recetas idénticas se deduplican
    # en una sola subpartida; distintas generan códigos con sufijo -2, -3, ...
    canonicas: dict = {}      # firma_receta -> PartidaPU canónica
    por_codigo: dict = {}     # codigo asignado -> firma (para sufijos)

    def _firma(sp: PartidaPU):
        return (sp.descripcion, round(sp.cud or 0, 4),
                tuple(sorted((rc.tipo, rc.codigo, round(rc.cantidad, 6), round(rc.precio, 6))
                             for rc in sp.recursos)))

    def _canonizar(sp: PartidaPU, codigo_ref: str) -> PartidaPU:
        f = _firma(sp)
        if f in canonicas:
            return canonicas[f]
        codigo = codigo_ref
        n = 1
        while codigo in por_codigo and por_codigo[codigo] != f:
            n += 1
            codigo = f"{codigo_ref}-{n}"
        sp.codigo = codigo
        por_codigo[codigo] = f
        canonicas[f] = sp
        out.subpartidas.append(sp)
        _verificar(sp, out)
        return sp

    # Índice global de definiciones SP (fallback: el template a veces referencia
    # una SP cuya definición vive bajo OTRO bloque — p.ej. variantes de nombre).
    global_sps: dict = {}
    def _indexar(sps: dict):
        for desc, sp in sps.items():
            global_sps.setdefault(desc, sp)
            _indexar(sp.sps)
    for b in bloques_p.values():
        _indexar(b.sps)

    def _resolver_subs(p: PartidaPU, ambito: dict, pila=()):
        for rec in p.recursos:
            if rec.tipo != "SUB":
                continue
            sp = _buscar_desc(ambito, rec.descripcion)
            if sp is None:
                sp = _buscar_desc(global_sps, rec.descripcion)
                if sp is not None:
                    out.avisos.append(
                        f"fila {rec.fila}: subpartida '{rec.descripcion}' resuelta con la "
                        f"definición de otro bloque (no estaba en el suyo)")
            if sp is None:
                out.errores.append(
                    f"fila {rec.fila}: subpartida '{rec.descripcion}' referenciada y no definida en su bloque")
                continue
            if id(sp) in pila:
                out.errores.append(f"fila {rec.fila}: referencia circular de subpartida '{rec.descripcion}'")
                continue
            # resolver anidadas ANTES de canonizar (la firma no depende de eso, pero
            # los recursos SUB de la sp deben quedar enlazados una sola vez)
            if not any(getattr(r2, "sub", None) for r2 in sp.recursos if r2.tipo == "SUB"):
                _resolver_subs(sp, {**ambito, **sp.sps}, pila + (id(sp),))
            rec.sub = _canonizar(sp, rec.codigo)

    # ── Merge PtoMeta ↔ PU-Meta ──
    for codigo, hoja in hojas.items():
        b = bloques_p.get(codigo)
        if b is None:
            if hoja.pu_meta > 0:
                out.errores.append(f"partida {codigo}: tiene PU meta en PtoMeta pero no hay bloque APU en PU-Meta")
            else:
                out.avisos.append(f"partida {codigo}: sin APU (PU meta = 0)")
            continue
        hoja.recursos = b.recursos
        hoja.rend_mo, hoja.rend_eq, hoja.cud = b.rend_mo, b.rend_eq, b.cud
        hoja.area = b.area
        hoja.sps = b.sps
        if b.metrado and hoja.metrado and abs(b.metrado - hoja.metrado) > 0.01:
            out.avisos.append(
                f"partida {codigo}: metrado difiere entre PtoMeta ({hoja.metrado}) y PU-Meta ({b.metrado})")
        if hoja.cud and hoja.pu_meta and abs(hoja.cud - hoja.pu_meta) > max(0.02, hoja.pu_meta * 0.001):
            out.errores.append(
                f"partida {codigo}: PU meta de PtoMeta ({hoja.pu_meta:.4f}) no cuadra con el CUD ({hoja.cud:.4f})")
        _resolver_subs(hoja, hoja.sps)
        _verificar(hoja, out)

    huerfanos = set(bloques_p) - set(hojas)
    for c in sorted(huerfanos):
        out.avisos.append(f"PU-Meta: bloque {c} sin fila en PtoMeta (se ignora)")
    return out


def parsear_archivo(ruta: str) -> ResultadoPU:
    with open(ruta, "rb") as f:
        return parsear_plantilla_pu(f.read())
