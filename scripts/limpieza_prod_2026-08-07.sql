-- ============================================================================
-- LIMPIEZA DE PRODUCCIÓN — 2026-08-07
--
-- Encargo de Jean: «todo lo registrado hasta la fecha eran partidas inventadas
-- con HH que inventé; dejar limpio el proyecto y comenzar a reportar bien».
-- Alcance elegido: OPERATIVO + PROYECTOS (otms).
--
--   SE BORRA   : partidas, hitos, avances, tareo/HH, sesiones, reportes de
--                campo y sus fotos, programación (actividades, plan, cierres,
--                restricciones), presupuestos/APU, costos, valorizaciones,
--                RO y sus periodos, y los proyectos (tabla `otms`).
--   SE CONSERVA: trabajadores, supervisores, usuarios, cuadrillas y habituales,
--                catálogo de fases, tarifas de MO, jornada global, feriados,
--                configuración de programación, `proyectos` y `empresas`.
--
-- ⚠️ EL SCRIPT TERMINA EN **ROLLBACK**. Corre así la primera vez: no cambia
--    nada y te imprime el antes/después. Solo cuando los números te cuadren,
--    cambia la ÚLTIMA línea a COMMIT y vuelve a correrlo.
--
-- ⚠️ ANTES DE NADA: confirma que hay un backup FRESCO en R2 (Coolify →
--    servicio de backup → último dump de hoy). Esto no tiene deshacer.
--
-- El orden de borrado NO está escrito de memoria: se derivó del grafo de
-- claves foráneas del esquema en 0049. Tres trampas que ese grafo destapó y
-- que un script «de cabeza» se habría comido:
--
--   1. `ev_partidas.otm_id` NO tiene FK contra `otms`. Borrar proyectos no
--      arrastra sus partidas ni da error: las deja huérfanas y mudas.
--      Lo mismo con `cuadrilla_otm.otm_id`, `tareo_ediciones.otm_id`,
--      `ev_historico_carga.otm_id` y `ev_jornada_reglas.otm_id`.
--   2. `ev_config.fecha_base` = '2026-06-01' SOBREVIVE al borrado de datos.
--      Si se queda, el primer tareo nuevo no cae en la semana 1 sino en la
--      semana que le toque contando desde junio. Hay que borrar esa fila para
--      que el motor la vuelva a derivar del primer día tareado de verdad.
--   3. `proyectos` (id=1) NO es `otms`. Es el contenedor del que cuelgan fases,
--      periodos y configuración. Se conserva: borrarlo se llevaría por delante
--      el catálogo de fases y la config de programación.
-- ============================================================================

BEGIN;

-- ── ANTES ───────────────────────────────────────────────────────────────────
\echo ''
\echo '=================== ANTES ==================='
SELECT 'otms (proyectos)'    AS tabla, count(*) FROM otms
UNION ALL SELECT 'ev_partidas',        count(*) FROM ev_partidas
UNION ALL SELECT 'ev_hitos',           count(*) FROM ev_hitos
UNION ALL SELECT 'ev_avances',         count(*) FROM ev_avances
UNION ALL SELECT 'ev_avances_diarios', count(*) FROM ev_avances_diarios
UNION ALL SELECT 'tareo_partida',      count(*) FROM tareo_partida
UNION ALL SELECT 'sesiones',           count(*) FROM sesiones
UNION ALL SELECT 'campo_reportes',     count(*) FROM campo_reportes
UNION ALL SELECT 'campo_fotos',        count(*) FROM campo_fotos
UNION ALL SELECT 'prog_actividades',   count(*) FROM prog_actividades
UNION ALL SELECT '--- se conserva ---', NULL
UNION ALL SELECT 'trabajadores',       count(*) FROM trabajadores
UNION ALL SELECT 'supervisores',       count(*) FROM supervisores
UNION ALL SELECT 'usuarios',           count(*) FROM usuarios
UNION ALL SELECT 'fases',              count(*) FROM fases;

-- ── 1 · CAMPO (fotos primero; las filas van con CASCADE, los ARCHIVOS no) ────
-- OJO: esto borra los REGISTROS de las 35 fotos, no los archivos del disco del
-- VPS. Los archivos se borran aparte — ver el paso 2 del instructivo.
DELETE FROM campo_fotos;
DELETE FROM campo_reportes;

-- ── 2 · PROGRAMACIÓN ────────────────────────────────────────────────────────
DELETE FROM prog_semana_plan_det;
DELETE FROM prog_semana_plan;
DELETE FROM prog_semana_cierre_det;
DELETE FROM prog_semana_cierre;
DELETE FROM prog_semana_eventos;
DELETE FROM prog_restriccion_eventos;
DELETE FROM prog_restricciones;
DELETE FROM prog_dependencias;
DELETE FROM prog_metrado_dia;
DELETE FROM prog_actividades;
DELETE FROM prog_responsables;

-- ── 3 · TAREO Y HH ──────────────────────────────────────────────────────────
DELETE FROM hh_conflictos;
DELETE FROM tareo_ediciones;
DELETE FROM tareo_partida;
DELETE FROM registros;
DELETE FROM sesion_trabajadores;
DELETE FROM sesiones;

-- ── 4 · RO, COSTOS Y VALORIZACIONES ─────────────────────────────────────────
DELETE FROM valorizacion_lineas;
DELETE FROM valorizaciones;
DELETE FROM venta_ajustes;
DELETE FROM ro_prev;
DELETE FROM ro_proyeccion;
DELETE FROM costo_documentos;
DELETE FROM periodos;

-- ── 5 · PRESUPUESTO / APU ───────────────────────────────────────────────────
DELETE FROM apu_recursos;
DELETE FROM presupuesto_costo_meta;
DELETE FROM presupuesto_partidas;
DELETE FROM presupuestos;

-- ── 6 · MOTOR EV (avances → hitos → partidas) ───────────────────────────────
DELETE FROM ev_avances_diarios;
DELETE FROM ev_avances;
DELETE FROM ev_hitos;
DELETE FROM ev_hh_gastadas;
DELETE FROM ev_hh_improductivas;
DELETE FROM ev_historico_carga;
DELETE FROM ev_valorizado;
DELETE FROM ev_partidas;

-- ── 7 · PROYECTOS (otms) y lo que cuelga de ellos SIN FK ────────────────────
-- `cuadrilla_otm` y las reglas de jornada POR OTM no tienen FK: si no se
-- borran aquí, quedan apuntando a proyectos que ya no existen.
DELETE FROM cuadrilla_otm;
DELETE FROM ev_jornada_reglas WHERE otm_id IS NOT NULL;   -- la global (NULL) se conserva
DELETE FROM otms;

-- ── 8 · LA TRAMPA DE LA FECHA BASE ──────────────────────────────────────────
-- Sin esto, el primer tareo nuevo NO cae en la semana 1: el motor sigue
-- contando desde 2026-06-01. Al borrarla, `_fecha_base` la vuelve a derivar
-- (y a persistir) del primer día con HH del tareo nuevo.
DELETE FROM ev_config WHERE clave = 'fecha_base';

-- ── DESPUÉS ─────────────────────────────────────────────────────────────────
\echo ''
\echo '=================== DESPUÉS (todo lo de arriba debe ser 0) ==================='
SELECT 'otms (proyectos)'    AS tabla, count(*) FROM otms
UNION ALL SELECT 'ev_partidas',        count(*) FROM ev_partidas
UNION ALL SELECT 'ev_hitos',           count(*) FROM ev_hitos
UNION ALL SELECT 'ev_avances',         count(*) FROM ev_avances
UNION ALL SELECT 'ev_avances_diarios', count(*) FROM ev_avances_diarios
UNION ALL SELECT 'tareo_partida',      count(*) FROM tareo_partida
UNION ALL SELECT 'sesiones',           count(*) FROM sesiones
UNION ALL SELECT 'campo_reportes',     count(*) FROM campo_reportes
UNION ALL SELECT 'campo_fotos',        count(*) FROM campo_fotos
UNION ALL SELECT 'prog_actividades',   count(*) FROM prog_actividades
UNION ALL SELECT '--- debe seguir ---', NULL
UNION ALL SELECT 'trabajadores',       count(*) FROM trabajadores
UNION ALL SELECT 'supervisores',       count(*) FROM supervisores
UNION ALL SELECT 'usuarios',           count(*) FROM usuarios
UNION ALL SELECT 'fases',              count(*) FROM fases
UNION ALL SELECT 'proyectos',          count(*) FROM proyectos
UNION ALL SELECT 'cuadrilla_grupos',   count(*) FROM cuadrilla_grupos;

\echo ''
\echo '=================== HUÉRFANOS (las 4 filas deben decir 0) ==================='
SELECT 'partidas sin proyecto'   AS control, count(*) FROM ev_partidas p
        WHERE NOT EXISTS (SELECT 1 FROM otms o WHERE o.id = p.otm_id)
UNION ALL
SELECT 'cuadrilla_otm huérfana',        count(*) FROM cuadrilla_otm c
        WHERE NOT EXISTS (SELECT 1 FROM otms o WHERE o.id = c.otm_id)
UNION ALL
SELECT 'jornada por OTM huérfana',      count(*) FROM ev_jornada_reglas j
        WHERE j.otm_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM otms o WHERE o.id = j.otm_id)
UNION ALL
SELECT 'fecha_base que sobrevive',      count(*) FROM ev_config
        WHERE clave = 'fecha_base';

-- ============================================================================
-- ⬇️  CAMBIA ESTA LÍNEA A  COMMIT;  CUANDO LOS NÚMEROS TE CUADREN  ⬇️
-- ============================================================================
ROLLBACK;
