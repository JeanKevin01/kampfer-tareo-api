# ============================================================
# plantillas/definiciones.py — qué columnas tiene cada plantilla
#
# Una sola declaración por plantilla, que sirve para DIBUJARLA y para
# documentarla. La cabecera (`clave`) es el contrato con el importador y no se
# toca: cambiarla rompería los archivos que ya circulan. Todo lo que hay que
# explicar vive en `titulo`, `ayuda` y `si_falla`, que no son contrato.
# ============================================================
from datetime import date

from ._estilo import Col, Plantilla

# Formatos de Excel reutilizados
F_FECHA  = "dd/mm/yyyy"
F_MONTO  = "#,##0.00"
F_ENTERO = "#,##0"
F_DEC2   = "0.00"
F_DEC3   = "0.000"
F_TEXTO  = "@"


# ── 1 · Personal del padrón ──────────────────────────────────
def personal(cargos: list[str]) -> Plantilla:
    return Plantilla(
        clave="personal",
        archivo="plantilla_personal.xlsx",
        titulo="Personal de obra",
        proposito="Da de alta trabajadores en el padrón: los que se tarean en campo "
                  "y los que reportan desde la app.",
        hoja="PERSONAL",
        pasos=[
            "Escribe los APELLIDOS primero: así se ordena el padrón y así se busca "
            "en el escáner de campo.",
            "Si la persona ya existe, se actualiza en vez de duplicarse "
            "(se compara por DNI y por nombre).",
        ],
        avisos=[
            "⚠  ES_SUPERVISOR no depende del cargo: se pone SI solo a quien va a "
            "reportar desde la app de campo.",
        ],
        catalogos={"Cargos ya usados en el padrón":
                   [(c, "") for c in cargos] or [("OPERARIO", ""), ("PEON", "")]},
        cols=[
            Col("NOMBRE", "Nombre completo", "obligatorio", ancho=38,
                ayuda="Apellidos primero. Ej: GARCIA FLORES JUAN PABLO",
                si_falla="La fila se descarta: sin nombre no hay trabajador.",
                ejemplo="GARCIA FLORES JUAN PABLO", ejemplo2="QUISPE MAMANI ROSA"),
            Col("CARGO", "Cargo o especialidad", "obligatorio", ancho=26,
                catalogo="Cargos ya usados en el padrón",
                ayuda="Elige uno de la lista o escribe uno nuevo.",
                si_falla="Entra como SIN CARGO y no suma en el histograma por cargo.",
                ejemplo="OFICIAL MECANICO", ejemplo2="LIDER MECANICO"),
            Col("DNI", "Documento de identidad", "opcional", ancho=14, formato=F_TEXTO,
                ayuda="8 dígitos. Está en formato TEXTO a propósito: si fuera número, "
                      "un DNI que empieza en 0 perdería ese cero.",
                si_falla="Se guarda vacío; el trabajador funciona igual.",
                ejemplo="12345678", ejemplo2="87654321"),
            Col("TIPO", "Mano de obra", "obligatorio", ancho=13,
                lista=["DIRECTO", "INDIRECTO"],
                ayuda="DIRECTO = su HH va a una partida. INDIRECTO = apoyo, "
                      "supervisión, conducción.",
                si_falla="Se asume DIRECTO.",
                ejemplo="DIRECTO", ejemplo2="INDIRECTO"),
            Col("ES_SUPERVISOR", "¿Reporta desde la app?", "obligatorio", ancho=15,
                lista=["SI", "NO"],
                ayuda="SI solo para quien va a usar la app de campo. Se le creará su "
                      "acceso desde Usuarios.",
                si_falla="Se asume NO y esa persona no podrá reportar en campo.",
                ejemplo="NO", ejemplo2="SI"),
        ],
    )


# ── 2 · Partidas del valor ganado ────────────────────────────
def partidas(fases: list[tuple], unidades: list[str]) -> Plantilla:
    hitos: list[Col] = []
    for n in (1, 2, 3, 4, 5):
        hitos += [
            # Los hitos van SOLO en la fila hoja (la 2ª del ejemplo): un nodo
            # padre no se mide, así que ponerle hitos enseñaría lo contrario de
            # lo que dice el paso 4 de la guía.
            Col(f"HITO{n}_DESC", f"Hito {n} — qué se hace", "opcional", ancho=17,
                grupo="Hitos (rules of credit)", tono="info",
                ayuda="Etapa medible de la partida. Ej: Preparación, Ejecución, Prueba.",
                si_falla="Sin hitos se crea uno solo, «Ejecución», con peso 1.00.",
                ejemplo="",
                ejemplo2="Preparación" if n == 1 else ("Ejecución" if n == 2 else "")),
            Col(f"HITO{n}_PESO", f"Hito {n} — peso", "opcional", ancho=10,
                formato=F_DEC2, grupo="Hitos (rules of credit)", tono="info",
                ayuda="Cuánto avance vale ese hito, en tanto por uno. Los pesos de "
                      "una misma partida deben sumar 1.00.",
                si_falla="Si no suman 1.00 el sistema los normaliza y avisa.",
                ejemplo="",
                ejemplo2=0.10 if n == 1 else (0.90 if n == 2 else "")),
        ]
    ult = 11 + len(hitos)   # columna de control, tras los hitos
    return Plantilla(
        clave="partidas",
        archivo="plantilla_partidas.xlsx",
        titulo="Partidas del valor ganado",
        proposito="Carga el árbol de partidas que se va a medir: metrado, HH "
                  "presupuestadas y los hitos con que se acredita el avance.",
        hoja="PARTIDAS",
        ejemplos=2,
        pasos=[
            "Una fila por partida. El CODIGO manda la jerarquía: 02 es padre de "
            "02.01, que es padre de 02.01.01.",
            "Deja la FASE VACÍA en las filas que solo agrupan (los padres). "
            "Con FASE es una partida real que se mide — mira las dos filas de ejemplo.",
            "Los pesos de los hitos de cada fila deben sumar 1.00: la última columna "
            "te lo comprueba sola (verde = bien, rojo = revísalo).",
        ],
        avisos=[
            "⚠  HH_GASTADAS y HH_GANADAS iniciales solo se llenan si la obra ya "
            "empezó antes de usar KAMPFER. En una obra nueva van vacías.",
        ],
        catalogos={
            "Fases del proyecto": fases or [("CIV", "Obras civiles")],
            "Unidades ya usadas": [(u, "") for u in unidades] or
                                  [("m3", ""), ("m2", ""), ("kg", ""), ("und", "")],
        },
        cols=[
            Col("CODIGO", "Código de la partida", "obligatorio", ancho=16,
                grupo="Identificación", tono="wbs", formato=F_TEXTO,
                ayuda="Jerárquico, con puntos. El árbol se arma solo a partir de él.",
                si_falla="La fila se descarta.",
                ejemplo="02.01", ejemplo2="02.01.01.01"),
            Col("FASE", "Fase / disciplina", "opcional", ancho=9,
                grupo="Identificación", tono="wbs", catalogo="Fases del proyecto",
                ayuda="VACÍA = la fila solo agrupa (nodo padre).\n"
                      "CON VALOR = partida real que se mide y se tarea.",
                si_falla="Sin fase la partida no se puede medir: queda como agrupador.",
                ejemplo="", ejemplo2="AND"),
            Col("DESCRIPCION", "Descripción", "obligatorio", ancho=36,
                grupo="Identificación", tono="wbs",
                ayuda="Lo que se lee en el LookAhead y en la app de campo.",
                si_falla="La fila se descarta.",
                ejemplo="DIVERTER DV-041", ejemplo2="TRANSPORTE INTERNO CAMIÓN GRÚA"),
            Col("UNIDAD", "Unidad", "opcional", ancho=9, grupo="Medición", tono="medida",
                catalogo="Unidades ya usadas",
                ayuda="La del metrado: m3, m2, kg, und, hm, hh…",
                si_falla="El avance se registra sin unidad.",
                ejemplo="", ejemplo2="hm"),
            Col("METRADO_PRESUP", "Metrado presupuestado", "opcional", ancho=15,
                formato=F_DEC3, grupo="Medición", tono="medida",
                ayuda="El del presupuesto contractual. Es la línea base del avance.",
                si_falla="La partida no tiene contra qué medirse: el % de avance sale 0.",
                ejemplo="", ejemplo2=16),
            Col("METRADO_PROYEC", "Metrado proyectado", "opcional", ancho=15,
                formato=F_DEC3, grupo="Medición", tono="medida",
                ayuda="Solo si hoy ya sabes que será distinto al presupuestado.",
                si_falla="Se usa el presupuestado.",
                ejemplo="", ejemplo2=""),
            Col("HH_PRESUP", "HH presupuestadas", "opcional", ancho=13,
                formato=F_DEC2, grupo="Medición", tono="medida",
                ayuda="Horas-hombre del presupuesto. Con esto se calculan las HH "
                      "ganadas y el factor de productividad.",
                si_falla="Sin HH no hay valor ganado para esa partida.",
                ejemplo="", ejemplo2=17.57),
            Col("HH_ACTUALIZADO", "HH actualizadas", "opcional", ancho=14,
                formato=F_DEC2, grupo="Medición", tono="medida",
                ayuda="Reestimación vigente, si la hay. No sustituye al presupuesto.",
                si_falla="Se usa HH_PRESUP.",
                ejemplo="", ejemplo2=18),
            Col("NATURALEZA", "Naturaleza", "opcional", ancho=14,
                grupo="Medición", tono="medida",
                lista=["CONTRACTUAL", "ADICIONAL"],
                ayuda="CONTRACTUAL = estaba en el contrato. ADICIONAL = entró después.",
                si_falla="Se asume CONTRACTUAL.",
                ejemplo="", ejemplo2="CONTRACTUAL"),
            Col("HH_GASTADAS_INICIAL", "HH ya gastadas", "opcional", ancho=17,
                formato=F_DEC2, grupo="Arranque histórico", tono="info",
                ayuda="Solo si la obra venía de antes: HH consumidas hasta hoy.",
                si_falla="Se asume 0 (obra que empieza).",
                ejemplo="", ejemplo2=""),
            Col("HH_GANADAS_INICIAL", "HH ya ganadas", "opcional", ancho=17,
                formato=F_DEC2, grupo="Arranque histórico", tono="info",
                ayuda="Solo si la obra venía de antes: valor ganado acumulado.",
                si_falla="Se asume 0.",
                ejemplo="", ejemplo2=""),
            *hitos,
            Col("SUMA_PESOS", "Control: ¿suman 1.00?", "calculado", ancho=13,
                formato=F_DEC2, grupo="Control", tono="info",
                ayuda="Se calcula sola. Debe dar 1.00 en toda fila con hitos.",
                si_falla="—",
                formula="=IF(COUNTA({HITO1_PESO}{f},{HITO2_PESO}{f},{HITO3_PESO}{f},"
                        "{HITO4_PESO}{f},{HITO5_PESO}{f})=0,\"\","
                        "SUM({HITO1_PESO}{f},{HITO2_PESO}{f},{HITO3_PESO}{f},"
                        "{HITO4_PESO}{f},{HITO5_PESO}{f}))"),
        ],
    )


# ── 3 · Proyectos / OTMs ─────────────────────────────────────
def proyectos(areas: list[str]) -> Plantilla:
    return Plantilla(
        clave="proyectos",
        archivo="plantilla_proyectos.xlsx",
        titulo="Proyectos (OTM)",
        proposito="Da de alta las órdenes de trabajo donde se tarea, se programa y "
                  "se mide el avance.",
        hoja="PROYECTOS",
        pasos=[
            "Deja el ID VACÍO para un proyecto nuevo: el código PROY-#### se genera solo.",
            "La fecha de fin NO se pide: se calcula como FECHA DE INICIO + PLAZO.",
        ],
        avisos=[
            "⚠  Si el nombre se parece a uno ya cargado, o el monto está a menos de "
            "100 de otro, el panel pedirá confirmación antes de crearlo.",
        ],
        catalogos={
            "Áreas ya usadas": [(a, "") for a in areas] or [("PLANTA", ""), ("MINA", "")],
            "Estados": [("POR INICIAR", "Aún no arranca"),
                        ("EJECUCION", "En ejecución"),
                        ("PARALIZADO", "Detenido temporalmente"),
                        ("TERMINADO", "Trabajos concluidos"),
                        ("LIQUIDADO", "Cerrado administrativamente")],
        },
        cols=[
            Col("NOMBRE", "Nombre del proyecto", "obligatorio", ancho=40,
                ayuda="Como se conoce en obra. Se guarda en MAYÚSCULAS.",
                si_falla="La fila se descarta.",
                ejemplo="MONTAJE ESTRUCTURA M-12", ejemplo2="REUBICACION NIDO DE CICLONES"),
            Col("AREA", "Área", "opcional", ancho=16, catalogo="Áreas ya usadas",
                ayuda="Zona de la obra. Es la que fija el ÁREA de los partes de campo.",
                si_falla="El proyecto queda sin área y los partes salen sin ella.",
                ejemplo="PLANTA", ejemplo2="MINA"),
            Col("ESTADO", "Estado", "opcional", ancho=15,
                lista=["POR INICIAR", "EJECUCION", "PARALIZADO", "TERMINADO", "LIQUIDADO"],
                ayuda="En qué punto está el proyecto.",
                si_falla="Se asume POR INICIAR.",
                ejemplo="EJECUCION", ejemplo2="POR INICIAR"),
            Col("CENTRO_COSTO", "Centro de costo", "opcional", ancho=16,
                ayuda="El de contabilidad, si lo usas.",
                si_falla="Se guarda vacío.", ejemplo="", ejemplo2=""),
            Col("MONEDA", "Moneda", "opcional", ancho=10, lista=["PEN", "USD"],
                ayuda="PEN = soles · USD = dólares.",
                si_falla="Se asume PEN.", ejemplo="PEN", ejemplo2="USD"),
            Col("PLAZO", "Plazo (días)", "opcional", ancho=11, formato=F_ENTERO,
                ayuda="Días calendario. Con la fecha de inicio se calcula la de fin.",
                si_falla="El proyecto queda sin fecha de fin.",
                ejemplo=30, ejemplo2=45),
            Col("FECHA DE INICIO", "Fecha de inicio", "opcional", ancho=15,
                formato=F_FECHA,
                ayuda="Escríbela como fecha (01/06/2026). La celda ya tiene ese formato.",
                si_falla="El proyecto queda sin fechas y no entra en la Curva S.",
                # Fecha de verdad, no texto: si el ejemplo fuera un string, Excel
                # lo alinearía a la izquierda y la columna acabaría con dos tipos
                # mezclados — el defecto que esta tanda viene a cerrar.
                ejemplo=date(2026, 1, 6), ejemplo2=date(2026, 1, 13)),
            Col("MONTO CONTRACTUAL", "Monto contractual", "opcional", ancho=18,
                formato=F_MONTO,
                ayuda="Venta contratada, sin IGV.",
                si_falla="El Resultado Operativo arranca sin venta contractual.",
                ejemplo=125000, ejemplo2=280000),
            Col("MONTO VALORIZADO", "Monto valorizado", "opcional", ancho=18,
                formato=F_MONTO,
                ayuda="Lo ya valorizado a la fecha, si viene de antes.",
                si_falla="Se asume 0.", ejemplo=0, ejemplo2=0),
            Col("ID", "Código del proyecto", "opcional", ancho=13, formato=F_TEXTO,
                ayuda="VACÍO para uno nuevo. Pon un ID existente (PROY-0003) SOLO si "
                      "quieres actualizar ese proyecto.",
                si_falla="Con un ID que no existe, la fila se rechaza.",
                ejemplo="", ejemplo2=""),
        ],
    )


# ── 4 · Presupuesto de control ───────────────────────────────
def presupuesto(fases: list[tuple]) -> Plantilla:
    return Plantilla(
        clave="presupuesto",
        archivo="plantilla_presupuesto_control.xlsx",
        titulo="Presupuesto de control",
        proposito="El presupuesto con el que se controla la obra: metrado, precio "
                  "unitario y HH meta por partida.",
        hoja="PRESUPUESTO",
        pasos=[
            "Una fila por partida de control, con su precio unitario de venta.",
            "FASE y SUB_FASE agrupan el presupuesto igual que en el Resultado Operativo.",
        ],
        avisos=[
            "⚠  Esto NO es el presupuesto META. La META nace del import de la "
            "plantilla PU y no se edita línea a línea.",
        ],
        catalogos={"Fases del proyecto": fases or [("10", "Obras preliminares")]},
        cols=[
            Col("CODIGO", "Código", "obligatorio", ancho=15, formato=F_TEXTO,
                grupo="Identificación", tono="wbs",
                ayuda="Jerárquico, con puntos.",
                si_falla="La fila se descarta.",
                ejemplo="40.01.01", ejemplo2="10.01"),
            Col("DESCRIPCION", "Descripción", "obligatorio", ancho=38,
                grupo="Identificación", tono="wbs",
                ayuda="Nombre de la partida.",
                si_falla="La fila se descarta.",
                ejemplo="Acero en zapatas", ejemplo2="Movilización"),
            Col("UNIDAD", "Unidad", "opcional", ancho=10,
                grupo="Identificación", tono="wbs",
                ayuda="KG, M3, GLB, UND…",
                si_falla="Se guarda sin unidad.", ejemplo="KG", ejemplo2="GLB"),
            Col("FASE", "Fase", "opcional", ancho=10, catalogo="Fases del proyecto",
                grupo="Identificación", tono="wbs",
                ayuda="La del catálogo del proyecto. Es la que cruza con el RO.",
                si_falla="La partida no suma en su fase del Resultado Operativo.",
                ejemplo="40", ejemplo2="10"),
            Col("SUB_FASE", "Sub-fase", "opcional", ancho=11, formato=F_TEXTO,
                grupo="Identificación", tono="wbs",
                ayuda="Segundo nivel de agrupación, si lo usas.",
                si_falla="Se agrupa solo por fase.",
                ejemplo="40.01", ejemplo2="10.01"),
            Col("METRADO", "Metrado", "obligatorio", ancho=14, formato=F_DEC3,
                grupo="Medición y precio", tono="dinero",
                ayuda="Cantidad presupuestada.",
                si_falla="Sin metrado no hay parcial ni avance.",
                ejemplo=168375, ejemplo2=1),
            Col("PRECIO_UNITARIO", "Precio unitario", "obligatorio", ancho=16,
                formato=F_MONTO, grupo="Medición y precio", tono="dinero",
                ayuda="Precio de VENTA por unidad, sin IGV.",
                si_falla="La partida vale 0 en la valorización.",
                ejemplo=2.5, ejemplo2=2500),
            Col("HH_META", "HH meta", "opcional", ancho=12, formato=F_DEC2,
                grupo="Medición y precio", tono="dinero",
                ayuda="Horas-hombre objetivo de la partida.",
                si_falla="No se puede medir la productividad de esa partida.",
                ejemplo=12576, ejemplo2=200),
            Col("PARCIAL", "Control: metrado × PU", "calculado", ancho=16,
                formato=F_MONTO, grupo="Control", tono="info",
                ayuda="Se calcula solo. Suma esta columna para cuadrar el total "
                      "antes de subir el archivo.",
                si_falla="—",
                formula="=IF(COUNTA({METRADO}{f},{PRECIO_UNITARIO}{f})<2,\"\","
                        "{METRADO}{f}*{PRECIO_UNITARIO}{f})"),
        ],
    )


# ── 5 · Costos, documento por documento ──────────────────────
def costos(fases: list[tuple]) -> Plantilla:
    return Plantilla(
        clave="costos",
        archivo="plantilla_costos_documentos.xlsx",
        titulo="Costos — documento por documento",
        proposito="Carga facturas, órdenes de compra y vales de almacén para el "
                  "Resultado Operativo.",
        hoja="DOCUMENTOS",
        ejemplos=2,
        pasos=[
            "Una fila por documento. La FECHA decide en qué mes contable entra.",
            "El periodo de esa fecha tiene que estar ABIERTO en el panel; si está "
            "cerrado, la fila se rechaza.",
        ],
        avisos=[
            "⚠  La mano de obra NO se importa aquí: sale sola del tareo. Solo entra "
            "como ajuste de planilla desde el panel.",
        ],
        catalogos={
            "Fases del proyecto": fases or [("11", "Obras civiles")],
            "Tipos de documento": [("FACTURA", "Factura"), ("OC", "Orden de compra"),
                                   ("VALE", "Vale de almacén"), ("OTRO", "Otro documento")],
            "Tipos de recurso": [("MAT", "Materiales"), ("EQP", "Equipos propios"),
                                 ("EQT", "Equipos de terceros"), ("SUB", "Subcontratos"),
                                 ("DIR", "Dirección de obra"), ("GG", "Gastos generales")],
        },
        cols=[
            Col("PROVEEDOR", "Proveedor", "obligatorio", ancho=30,
                grupo="Documento", tono="neutro",
                ayuda="Razón social de quien emite.",
                si_falla="La fila se descarta.",
                ejemplo="FERRETERIA EL SOL SAC", ejemplo2="GRUAS ANDINAS EIRL"),
            Col("NUMERO_DOC", "Número", "obligatorio", ancho=16, formato=F_TEXTO,
                grupo="Documento", tono="neutro",
                ayuda="Serie y correlativo. Ej: F001-000123",
                si_falla="La fila se descarta: sin número no se puede rastrear.",
                ejemplo="F001-000123", ejemplo2="F002-000456"),
            Col("FECHA", "Fecha del documento", "obligatorio", ancho=15, formato=F_FECHA,
                grupo="Documento", tono="neutro",
                ayuda="Determina el mes contable. La celda ya tiene formato de fecha.",
                si_falla="La fila se rechaza o cae en el mes equivocado.",
                ejemplo=date(2026, 7, 5), ejemplo2=date(2026, 7, 8)),
            Col("TIPO_DOC", "Tipo de documento", "obligatorio", ancho=15,
                grupo="Documento", tono="neutro",
                lista=["FACTURA", "OC", "VALE", "OTRO"],
                ayuda="FACTURA · OC (orden de compra) · VALE (de almacén) · OTRO.",
                si_falla="La fila se rechaza.",
                ejemplo="FACTURA", ejemplo2="OC"),
            Col("TIPO_RECURSO", "Tipo de recurso", "obligatorio", ancho=15,
                grupo="Clasificación", tono="dinero",
                lista=["MAT", "EQP", "EQT", "SUB", "DIR", "GG"],
                ayuda="Qué se compró. Míralos en la hoja CATÁLOGOS.",
                si_falla="La fila se rechaza.",
                ejemplo="MAT", ejemplo2="EQT"),
            Col("DIRECTO", "¿Costo directo?", "obligatorio", ancho=12,
                grupo="Clasificación", tono="dinero", lista=["SI", "NO"],
                ayuda="SI = costo directo de obra. NO = indirecto. "
                      "DIR y GG son siempre NO.",
                si_falla="Se asume SI y el costo entra donde no debe.",
                ejemplo="SI", ejemplo2="SI"),
            Col("FASE", "Fase", "opcional", ancho=10, catalogo="Fases del proyecto",
                grupo="Clasificación", tono="dinero",
                ayuda="Contra qué fase se imputa. Los indirectos pueden ir sin fase.",
                si_falla="El costo no suma en ninguna fase del RO.",
                ejemplo="11", ejemplo2="11"),
            Col("MONEDA", "Moneda", "opcional", ancho=10, lista=["PEN", "USD"],
                grupo="Importe", tono="dinero",
                ayuda="PEN o USD.", si_falla="Se asume PEN.",
                ejemplo="PEN", ejemplo2="PEN"),
            Col("MONTO", "Monto sin IGV", "obligatorio", ancho=14, formato=F_MONTO,
                grupo="Importe", tono="dinero",
                ayuda="Importe SIN IGV. El IGV no es costo de obra.",
                si_falla="La fila se descarta.",
                ejemplo=1250.5, ejemplo2=3800),
            Col("GLOSA", "Detalle", "opcional", ancho=32,
                grupo="Importe", tono="dinero",
                ayuda="Qué se compró, en corto. Ayuda a sustentar el RO.",
                si_falla="Se guarda vacío.",
                ejemplo="Pernos y soldadura", ejemplo2="Alquiler grúa 25t"),
        ],
    )


# ── 6 · Costos agregados del RO ──────────────────────────────
def costos_ro(fases: list[tuple]) -> Plantilla:
    return Plantilla(
        clave="costos_ro",
        archivo="plantilla_costos_agregados_ro.xlsx",
        titulo="Costos agregados del Resultado Operativo",
        proposito="Carga el costo YA TOTALIZADO por fase y por mes — no documento a "
                  "documento.",
        hoja="COSTOS",
        ejemplos=2,
        pasos=[
            "Una fila por combinación de fase, tipo de recurso y mes.",
            "PERIODO es el PRIMER DÍA del mes: 01/06/2026. La celda ya tiene formato "
            "de fecha.",
        ],
        avisos=[
            "⚠  Si lo que tienes es factura por factura, usa la plantilla «Costos — "
            "documento por documento»: da mejor sustento y esta lo duplicaría.",
        ],
        catalogos={
            "Fases del proyecto": fases or [("10", "Obras preliminares")],
            "Tipos de recurso": [("MAT", "Materiales"), ("EQP", "Equipos propios"),
                                 ("EQT", "Equipos de terceros"), ("SUB", "Subcontratos"),
                                 ("DIR", "Dirección de obra"), ("GG", "Gastos generales")],
        },
        cols=[
            Col("FASE", "Fase", "opcional", ancho=10, catalogo="Fases del proyecto",
                ayuda="Vacía solo para indirectos que no cuelgan de ninguna fase.",
                si_falla="El costo queda sin fase en el RO.",
                ejemplo="10", ejemplo2=""),
            Col("TIPO_RECURSO", "Tipo de recurso", "obligatorio", ancho=15,
                lista=["MAT", "EQP", "EQT", "SUB", "DIR", "GG"],
                ayuda="Míralos en la hoja CATÁLOGOS.",
                si_falla="La fila se ignora en silencio: sin un tipo válido no entra.",
                ejemplo="MAT", ejemplo2="GG"),
            Col("DIRECTO", "¿Costo directo?", "opcional", ancho=12, lista=["SI", "NO"],
                ayuda="SI = directo de obra · NO = indirecto.",
                si_falla="Se asume SI.", ejemplo="SI", ejemplo2="NO"),
            Col("PERIODO", "Mes contable", "obligatorio", ancho=14, formato=F_FECHA,
                ayuda="PRIMER día del mes. Ej: 01/06/2026.",
                si_falla="La fila se ignora: sin periodo no hay mes al que imputar.",
                ejemplo=date(2026, 6, 1), ejemplo2=date(2026, 6, 1)),
            Col("MONTO", "Monto sin IGV", "obligatorio", ancho=14, formato=F_MONTO,
                ayuda="Total del mes para esa fase y recurso, sin IGV.",
                si_falla="Entra como 0.",
                ejemplo=101073, ejemplo2=60781),
            Col("FUENTE", "De dónde sale", "opcional", ancho=20,
                ayuda="Ej: Factura, Subcontrato, GG mes. Sirve de rastro.",
                si_falla="Se guarda vacío.",
                ejemplo="Factura", ejemplo2="GG mes"),
            Col("NOTA", "Nota", "opcional", ancho=28,
                ayuda="Cualquier aclaración.",
                si_falla="Se guarda vacío.", ejemplo="", ejemplo2="indirecto"),
        ],
    )
