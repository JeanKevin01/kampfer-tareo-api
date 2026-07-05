-- Verificación post-migración 0008 (unicidad por OTM). Correr tras `alembic upgrade head`.
-- Todas las consultas deben cumplir lo que dice su comentario.

-- 1) El índice nuevo existe y el viejo NO (debe devolver exactamente 1 fila: uq_partida_otm_codigo)
SELECT indexname FROM pg_indexes
WHERE tablename='ev_partidas' AND indexname IN ('ev_partidas_codigo_key','uq_partida_otm_codigo');

-- 2) Sin duplicados por (otm, codigo) (debe devolver 0 filas)
SELECT COALESCE(otm_id,'(SIN)') AS otm, codigo, count(*)
FROM ev_partidas GROUP BY 1,2 HAVING count(*)>1;

-- 3) Conteo de partidas — anotar ANTES y DESPUÉS de migrar: debe ser idéntico
SELECT count(*) AS total_partidas, count(*) FILTER (WHERE activo) AS activas FROM ev_partidas;
