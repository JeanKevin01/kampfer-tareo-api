# ============================================================
# routers/ev/ — módulo de Valor Ganado (F0.5b, en progreso)
#
#   _engine.py  SOLO funciones puras del motor (testeables sin BD):
#               _calcular, _totales, _agrupar, _matriz_area_disciplina,
#               _calc_costo_mo, _acum_a_semana, _validar_pesos
#   _datos.py   Acceso a datos del EV: _fecha_base, _hh_*_unificada/real,
#               _improductivas, _datos_base y helpers (_get, _as_date, _norm_*)
#
# Los endpoints siguen en routers/valor_ganado.py (que re-exporta estos nombres
# para no romper los tests). El siguiente paso del plan (PR-3..6) moverá los
# sub-routers aquí grupo por grupo.
# ============================================================
