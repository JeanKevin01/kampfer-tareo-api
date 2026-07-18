# -*- coding: utf-8 -*-
"""Validación end-to-end LOCAL del flujo tareo → ISP (hito de la Fase 0).

Simula el circuito completo contra una BD local migrada:
  supervisor envía tareo (enviar-con-partidas) → tareo_partida →
  captura de avance → /ev/reporte y /ev/isp muestran HH gastadas y ganadas.
También verifica la idempotencia del reenvío (mismo supervisor/OTM/día REEMPLAZA).

Uso (Windows Git Bash / Linux):
  1) Postgres local:  docker run -d --name kampfer-e2e -e POSTGRES_PASSWORD=test \
       -e POSTGRES_DB=kampfer_test -p 55432:5432 postgres:16
  2) Migrar:          DATABASE_URL=postgresql://postgres:test@localhost:55432/kampfer_test \
       python -m alembic upgrade head
  3) API local:       (mismo DATABASE_URL) ENV=dev python -m uvicorn main:app --port 8765
  4) Este script:     (mismo DATABASE_URL) API_URL=http://localhost:8765 \
       python scripts/validacion_e2e.py

Idempotente: limpia sus propios datos (OTM-E2E / E2E-%) al inicio.
Sale con código 0 si TODO pasa; 1 si algo falla.
"""
import os
import sys

import httpx
import psycopg2

API = os.environ.get("API_URL", "http://localhost:8765")
DB = os.environ["DATABASE_URL"]

FECHA_BASE = "2026-06-01"   # lunes → semana 1 = 2026-06-01..07
FECHA_TAREO = "2026-06-02"  # martes de la semana 1

_fallas = []


def check(nombre, cond, detalle=""):
    estado = "OK  " if cond else "FALLA"
    print(f"[{estado}] {nombre}" + (f" — {detalle}" if detalle and not cond else ""))
    if not cond:
        _fallas.append(nombre)


def limpiar_y_sembrar(cur):
    cur.execute("DELETE FROM campo_fotos WHERE reporte_id IN "
                "(SELECT id FROM campo_reportes WHERE otm_id='OTM-E2E')")
    cur.execute("DELETE FROM campo_reportes WHERE otm_id='OTM-E2E'")
    cur.execute("DELETE FROM prog_actividades WHERE titulo LIKE 'E2E %' OR otm_id='OTM-E2E'")
    cur.execute("DELETE FROM prog_feriados WHERE proyecto_id=1")
    cur.execute("DELETE FROM prog_config WHERE proyecto_id=1")
    cur.execute("DELETE FROM fases WHERE codigo LIKE 'E2E-%'")
    cur.execute("DELETE FROM tareo_partida WHERE otm_id='OTM-E2E'")
    cur.execute("DELETE FROM registros WHERE otm_id='OTM-E2E'")
    cur.execute("DELETE FROM sesion_trabajadores WHERE sesion_id IN "
                "(SELECT id FROM sesiones WHERE otm_id='OTM-E2E')")
    cur.execute("DELETE FROM sesiones WHERE otm_id='OTM-E2E'")
    cur.execute("DELETE FROM ev_avances WHERE hito_id IN (SELECT id FROM ev_hitos WHERE "
                "partida_id IN (SELECT id FROM ev_partidas WHERE codigo LIKE 'E2E-%'))")
    cur.execute("DELETE FROM ev_hh_gastadas WHERE partida_id IN "
                "(SELECT id FROM ev_partidas WHERE codigo LIKE 'E2E-%')")
    cur.execute("DELETE FROM ev_hitos WHERE partida_id IN "
                "(SELECT id FROM ev_partidas WHERE codigo LIKE 'E2E-%')")
    # Residuos de smokes de F2 en BDs locales reutilizadas:
    cur.execute("DELETE FROM ev_avances_diarios WHERE partida_id IN "
                "(SELECT id FROM ev_partidas WHERE codigo LIKE 'E2E-%')")
    cur.execute("DELETE FROM valorizacion_lineas WHERE partida_id IN "
                "(SELECT id FROM ev_partidas WHERE codigo LIKE 'E2E-%')")
    cur.execute("DELETE FROM ev_valorizado WHERE partida_id IN "
                "(SELECT id FROM ev_partidas WHERE codigo LIKE 'E2E-%')")
    cur.execute("DELETE FROM ev_partidas WHERE codigo LIKE 'E2E-%'")

    cur.execute("INSERT INTO supervisores (id, nombre) VALUES ('SUPE2E','Supervisor E2E') "
                "ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO trabajadores (id, nombre, cargo) VALUES "
                "('901','Trabajador E2E Uno','OFICIAL'),('902','Trabajador E2E Dos','OPERARIO') "
                "ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO otms (id, descripcion, proyecto_id) VALUES "
                "('OTM-E2E','OTM de validación E2E',1) ON CONFLICT (id) DO NOTHING")
    cur.execute("INSERT INTO ev_config (clave, valor) VALUES ('fecha_base', %s) "
                "ON CONFLICT (clave) DO UPDATE SET valor=%s", (FECHA_BASE, FECHA_BASE))

    partidas = {}
    for codigo, hh_presup, metrado in (("E2E-001", 100, 10), ("E2E-002", 50, 5)):
        cur.execute(
            "INSERT INTO ev_partidas (codigo, fase, descripcion, unidad, metrado_presup, "
            "hh_presup, otm_id) VALUES (%s,'F-E2E',%s,'und',%s,%s,'OTM-E2E') RETURNING id",
            (codigo, f"Partida {codigo}", metrado, hh_presup))
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal) "
            "VALUES (%s,1,'Ejecución',1.0,true) RETURNING id", (pid,))
        partidas[codigo] = {"id": pid, "hito_id": cur.fetchone()[0]}
    return partidas


def fila(reporte, codigo):
    return next((p for p in reporte["partidas"] if p.get("codigo") == codigo), None)


def main():
    con = psycopg2.connect(DB)
    con.autocommit = True
    cur = con.cursor()
    c = httpx.Client(timeout=30)

    print(f"== Validación E2E tareo->ISP contra {API} ==")
    partidas = limpiar_y_sembrar(cur)
    p1, p2 = partidas["E2E-001"], partidas["E2E-002"]

    # T1 — salud
    r = c.get(f"{API}/health")
    check("T1 /health responde", r.status_code == 200 and r.json().get("status") == "ok")

    # T2 — enviar tareo del día (2 trabajadores, 3 asignaciones)
    payload = {
        "supervisor_id": "SUPE2E", "otm_id": "OTM-E2E", "fecha": FECHA_TAREO,
        "trabajadores": [
            {"trab_id": "901", "via": "e2e", "asignaciones": [
                {"partida_id": p1["id"], "hh": 5.0}, {"partida_id": p2["id"], "hh": 4.5}]},
            {"trab_id": "902", "via": "e2e", "asignaciones": [
                {"partida_id": p1["id"], "hh": 9.5}]},
        ],
    }
    r = c.post(f"{API}/api/sesion/enviar-con-partidas", json=payload)
    ok = r.status_code == 200 and r.json().get("ok") is True
    check("T2 enviar-con-partidas acepta el tareo", ok, f"status={r.status_code} body={r.text[:200]}")

    # T3 — el tareo quedó en BD con la semana correcta
    cur.execute("SELECT count(*), COALESCE(sum(hh),0), COALESCE(min(semana),0), "
                "COALESCE(max(semana),0) FROM tareo_partida WHERE otm_id='OTM-E2E'")
    n, suma, smin, smax = cur.fetchone()
    check("T3 tareo_partida: 3 filas, 19.0 HH, semana 1",
          n == 3 and float(suma) == 19.0 and smin == 1 and smax == 1,
          f"filas={n} hh={suma} semanas={smin}..{smax}")

    # T4 — captura de avance: 50% de E2E-001 (5 de 10 und)
    r = c.post(f"{API}/ev/captura", json={
        "semana": 1, "avances": [{"hito_id": p1["hito_id"], "cantidad_acum": 5}], "hh_gastadas": []})
    check("T4 captura de avance 50% E2E-001", r.status_code == 200)

    # T5 — /ev/reporte semana 1: HH gastadas por partida + ganadas por avance
    r = c.get(f"{API}/ev/reporte", params={"semana": 1, "otm": "OTM-E2E"})
    check("T5a /ev/reporte responde", r.status_code == 200, r.text[:200])
    rep = r.json()
    f1, f2 = fila(rep, "E2E-001"), fila(rep, "E2E-002")
    check("T5b E2E-001 gastadas=14.5",
          f1 is not None and abs(f1.get("hh_gastadas_acum", -1) - 14.5) < 0.01, f"fila={f1}")
    check("T5c E2E-002 gastadas=4.5",
          f2 is not None and abs(f2.get("hh_gastadas_acum", -1) - 4.5) < 0.01, f"fila={f2}")
    check("T5d E2E-001 ganadas=50.0 (50% de 100 HH presup)",
          f1 is not None and abs(f1.get("hh_ganadas_acum", -1) - 50.0) < 0.01,
          f"claves={list(f1.keys()) if f1 else None}")
    tot = rep.get("totales", {})
    check("T5e totales.gastadas=19.0", abs(tot.get("hh_gastadas_acum", -1) - 19.0) < 0.01,
          f"totales={tot}")

    # T6 — idempotencia: reenviar el MISMO día reemplaza (no acumula)
    payload["trabajadores"] = [
        {"trab_id": "901", "via": "e2e", "asignaciones": [
            {"partida_id": p1["id"], "hh": 2.0}, {"partida_id": p2["id"], "hh": 2.0}]},
        {"trab_id": "902", "via": "e2e", "asignaciones": [
            {"partida_id": p1["id"], "hh": 9.5}]},
    ]
    r = c.post(f"{API}/api/sesion/enviar-con-partidas", json=payload)
    cur.execute("SELECT count(*), COALESCE(sum(hh),0) FROM tareo_partida WHERE otm_id='OTM-E2E'")
    n, suma = cur.fetchone()
    check("T6 reenvío del día REEMPLAZA (3 filas, 13.5 HH -- no 32.5)",
          r.status_code == 200 and n == 3 and float(suma) == 13.5, f"filas={n} hh={suma}")

    # T7 — /ev/isp incluye la semana con nuestras partidas
    r = c.get(f"{API}/ev/isp", params={"otm": "OTM-E2E"})
    ok = r.status_code == 200
    cods = [p.get("codigo") for p in r.json().get("partidas", [])] if ok else []
    check("T7 /ev/isp incluye E2E-001 y E2E-002",
          ok and "E2E-001" in cods and "E2E-002" in cods, f"status={r.status_code} codigos={cods[:5]}")

    # F-FASES — catálogo de fases (migración 0018 + CRUD)
    r = c.get(f"{API}/ev/fases", params={"proyecto_id": 1})
    fases = r.json() if r.status_code == 200 else []
    check("F1 /ev/fases con seed de disciplinas (>=11, incluye FAB)",
          r.status_code == 200 and len(fases) >= 11
          and any(f["codigo"] == "FAB" for f in fases),
          f"status={r.status_code} n={len(fases)}")

    r = c.post(f"{API}/ev/fases", json={"codigo": " e2e-99 ", "nombre": "Fase E2E"})
    fase_nueva = r.json() if r.status_code == 200 else {}
    check("F2 POST /ev/fases crea y normaliza el codigo a E2E-99",
          r.status_code == 200 and fase_nueva.get("codigo") == "E2E-99",
          f"status={r.status_code} body={r.text[:120]}")

    r = c.post(f"{API}/ev/fases", json={"codigo": "E2E-99", "nombre": "Duplicada"})
    check("F3 POST fase duplicada -> 409", r.status_code == 409, f"status={r.status_code}")

    r = c.put(f"{API}/ev/fases/{fase_nueva.get('id', 0)}", json={"activo": False})
    r2 = c.get(f"{API}/ev/fases", params={"proyecto_id": 1})
    activas = [f["codigo"] for f in r2.json()] if r2.status_code == 200 else []
    check("F4 PUT desactiva y el GET por defecto la oculta",
          r.status_code == 200 and "E2E-99" not in activas, f"activas={len(activas)}")

    r = c.get(f"{API}/ev/presupuesto/plantilla-pu")
    check("F5 plantilla PU descarga .xls (magic bytes BIFF)",
          r.status_code == 200 and r.content[:4] == b"\xd0\xcf\x11\xe0",
          f"status={r.status_code}")

    # M — matriz histórica (fechas × partidas/trabajadores)
    r = c.get(f"{API}/ev/matriz", params={"desde": FECHA_BASE, "hasta": "2026-06-07",
                                          "modo": "partidas", "otm": "OTM-E2E"})
    mz = r.json() if r.status_code == 200 else {}
    fila_p1 = next((f for f in mz.get("filas", []) if f["etiqueta"].startswith("E2E-001")), None)
    check("M1 matriz partidas: E2E-001 con 11.5 HH el martes y total de columna 13.5",
          r.status_code == 200 and fila_p1 is not None
          and abs(fila_p1["celdas"].get(FECHA_TAREO, 0) - 11.5) < 0.01
          and abs(mz.get("tot_col", {}).get(FECHA_TAREO, 0) - 13.5) < 0.01,
          f"status={r.status_code} fila={fila_p1} tot={mz.get('tot_col')}")

    r = c.get(f"{API}/ev/matriz", params={"desde": FECHA_BASE, "hasta": "2026-06-07",
                                          "modo": "trabajadores", "otm": "OTM-E2E"})
    mz = r.json() if r.status_code == 200 else {}
    check("M2 matriz trabajadores: 2 filas y semanas etiquetadas",
          r.status_code == 200 and len(mz.get("filas", [])) == 2
          and len(mz.get("semanas", [])) >= 1,
          f"filas={len(mz.get('filas', []))}")

    # F-PROG — calendario de programación + reportes de campo con fotos (0019)
    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "otm_id": "OTM-E2E",
        "titulo": "E2E Hormigonado losa", "responsable": "Cuadrilla 1"})
    act = r.json() if r.status_code == 200 else {}
    check("P1 crear actividad programada", r.status_code == 200 and act.get("estado") == "PROGRAMADO",
          f"status={r.status_code} body={r.text[:150]}")

    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (900, 600), (180, 90, 30)).save(buf, "JPEG")
    r = c.post(f"{API}/campo/reportes",
               data={"proyecto_id": 1, "fecha": FECHA_TAREO, "otm_id": "OTM-E2E",
                     "supervisor_id": "SUPE2E", "descripcion": "Losa vaciada al 100%",
                     "actividad_id": act.get("id")},
               files=[("fotos", ("losa.jpg", buf.getvalue(), "image/jpeg"))])
    check("P2 reporte de campo con foto", r.status_code == 200 and r.json().get("fotos") == 1,
          f"status={r.status_code} body={r.text[:150]}")

    r = c.get(f"{API}/ev/programacion/semana", params={"proyecto_id": 1, "lunes": FECHA_TAREO})
    sem = r.json() if r.status_code == 200 else {}
    act_sem = next((a for a in sem.get("actividades", []) if a["id"] == act.get("id")), None)
    rep_sem = next((x for x in sem.get("reportes", []) if x["otm_id"] == "OTM-E2E"), None)
    check("P3 semana: actividad EJECUTADO (reporte la ejecuta) + reporte con foto",
          act_sem is not None and act_sem["estado"] == "EJECUTADO"
          and rep_sem is not None and len(rep_sem["fotos"]) == 1
          and rep_sem["fotos"][0]["url"], f"act={act_sem and act_sem.get('estado')}")

    url_foto = rep_sem["fotos"][0]["url"] if rep_sem and rep_sem["fotos"] else ""
    r = c.get(f"{API}{url_foto}")
    check("P4 URL firmada sirve el JPEG", r.status_code == 200 and r.content[:2] == b"\xff\xd8",
          f"status={r.status_code}")
    r = c.get(f"{API}{url_foto[:-4]}0000")
    check("P5 firma corrupta -> 403", r.status_code == 403, f"status={r.status_code}")

    r = c.get(f"{API}/ev/programacion/media-uso", params={"proyecto_id": 1})
    uso = r.json() if r.status_code == 200 else []
    sem_iso = next((u for u in uso if u["n_fotos"] >= 1 and u["bytes_en_disco"] > 0), None)
    check("P6 media-uso reporta bytes por semana", sem_iso is not None, f"uso={uso}")

    r = c.delete(f"{API}/ev/programacion/actividades/{act.get('id')}")
    check("P7 DELETE actividad con reporte -> 409", r.status_code == 409, f"status={r.status_code}")

    r = c.post(f"{API}/ev/programacion/purgar",
               json={"proyecto_id": 1, "semana_iso": sem_iso["semana_iso"] if sem_iso else ""})
    purga = r.json() if r.status_code == 200 else {}
    r2 = c.get(f"{API}{url_foto}")
    check("P8 purga libera bytes y la foto ya no se sirve",
          r.status_code == 200 and purga.get("fotos_purgadas", 0) >= 1 and r2.status_code == 404,
          f"purga={purga} get={r2.status_code}")

    # Flujo del supervisor: actividad asignada + causa de no cumplimiento (0020)
    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "otm_id": "OTM-E2E",
        "titulo": "E2E Montaje faja", "supervisor_id": "SUPE2E"})
    act2 = r.json() if r.status_code == 200 else {}
    r2 = c.get(f"{API}/campo/mis-actividades",
               params={"fecha": FECHA_TAREO, "supervisor_id": "SUPE2E"})
    mias = r2.json() if r2.status_code == 200 else []
    check("P9 actividad asignada aparece en mis-actividades del supervisor",
          r.status_code == 200 and any(a["id"] == act2.get("id") for a in mias),
          f"status={r.status_code}/{r2.status_code} n={len(mias)}")

    r = c.post(f"{API}/campo/actividades/{act2.get('id')}/no-cumplida",
               json={"supervisor_id": "SUPE2E", "causa_cat": "MATERIALES",
                     "causa": "No llegó el acero"})
    r2 = c.get(f"{API}/ev/programacion/semana", params={"proyecto_id": 1, "lunes": FECHA_TAREO})
    a2 = next((a for a in r2.json().get("actividades", []) if a["id"] == act2.get("id")), {})
    check("P10 no-cumplida registra estado, categoría CNC y detalle",
          r.status_code == 200 and a2.get("estado") == "NO_CUMPLIDA"
          and a2.get("causa_nc_cat") == "MATERIALES"
          and a2.get("causa_nc") == "No llegó el acero",
          f"status={r.status_code} act={a2.get('estado')}/{a2.get('causa_nc_cat')}")

    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "otm_id": "OTM-NO-EXISTE-99",
        "titulo": "E2E OTM inválida"})
    check("P11 OTM inexistente -> 400 claro (no 500 sin CORS)",
          r.status_code == 400, f"status={r.status_code}")

    # Last Planner: partida + restricciones (lookahead) + PPC/CNC (0021)
    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "otm_id": "OTM-E2E",
        "titulo": "E2E Con partida y restricción", "partida_id": p1["id"],
        "supervisor_id": "SUPE2E"})
    act3 = r.json() if r.status_code == 200 else {}
    r2 = c.post(f"{API}/ev/programacion/actividades/{act3.get('id')}/restricciones",
                json={"descripcion": "Llega el acero", "tipo": "MATERIALES",
                      "responsable": "Logística"})
    rest = r2.json() if r2.status_code == 200 else {}
    r3 = c.get(f"{API}/ev/programacion/lookahead",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 3})
    la = r3.json() if r3.status_code == 200 else {}
    a3 = next((a for s in la.get("semanas", []) for a in s["actividades"]
               if a["id"] == act3.get("id")), {})
    check("P12 lookahead: actividad con partida y 1 restricción pendiente",
          a3.get("rest_pend") == 1 and a3.get("partida_codigo") == "E2E-001"
          and len(la.get("semanas", [])) == 3,
          f"act={a3.get('rest_pend')}/{a3.get('partida_codigo')} sem={len(la.get('semanas', []))}")

    r = c.put(f"{API}/ev/programacion/restricciones/{rest.get('id')}", json={"liberada": True})
    r2 = c.get(f"{API}/ev/programacion/lookahead",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    a3 = next((a for s in r2.json().get("semanas", []) for a in s["actividades"]
               if a["id"] == act3.get("id")), {})
    check("P13 liberar la restricción deja la actividad lista (0 pendientes)",
          r.status_code == 200 and r.json().get("liberada") is True
          and a3.get("rest_pend") == 0, f"rest={a3.get('rest_pend')}")

    r = c.get(f"{API}/ev/programacion/ppc", params={"proyecto_id": 1, "semanas": 26})
    d = r.json() if r.status_code == 200 else {}
    cnc_mat = next((x for x in d.get("cnc", []) if x["causa"] == "MATERIALES"), None)
    sem_ppc = next((x for x in d.get("semanal", []) if x["comprometidas"] >= 3), None)
    check("P14 PPC: Pareto registra MATERIALES y la semana calcula su PPC",
          r.status_code == 200 and cnc_mat is not None and cnc_mat["n"] >= 1
          and sem_ppc is not None and sem_ppc["ppc"] is not None,
          f"cnc={d.get('cnc')} sem={sem_ppc}")

    # Lookahead-grid con metrado diario (0022 — plantillas Anexo 01 / F030b)
    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "fecha_fin": "2026-06-04",
        "otm_id": "OTM-E2E", "titulo": "E2E Relleno con metrado",
        "partida_id": p2["id"], "metrado_prog": 90})
    act4 = r.json() if r.status_code == 200 else {}
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 2})
    grid = r2.json() if r2.status_code == 200 else {}
    ga = next((a for g in grid.get("grupos", []) for a in g["actividades"]
               if a["id"] == act4.get("id")), {})
    check("P15 lookahead-grid distribuye 90 en 3 días (30/30/30) agrupado por OTM",
          r.status_code == 200 and ga.get("metrado_prog") == 90
          and ga.get("prog", {}).get(FECHA_TAREO) == 30
          and ga.get("prog", {}).get("2026-06-04") == 30
          and len(grid.get("fechas", [])) == 14,
          f"status={r.status_code} prog={ga.get('prog')}")

    r = c.put(f"{API}/ev/programacion/actividades/{act4.get('id')}/metrado-dias",
              json={"dias": {"2026-06-03": 50}})
    check("P16 editar una celda recalcula el total (30+50+30=110)",
          r.status_code == 200 and r.json().get("metrado_prog") == 110,
          f"status={r.status_code} total={r.json().get('metrado_prog') if r.status_code == 200 else '-'}")

    r = c.post(f"{API}/ev/programacion/avance-dia",
               json={"partida_id": p2["id"], "fecha": FECHA_TAREO, "cantidad": 2.5})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    ga = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act4.get("id")), {})
    r3 = c.get(f"{API}/ev/semana-grid", params={"semana": 1, "otm": "OTM-E2E"})
    fila_ev = next((p for p in r3.json().get("partidas", []) if p["id"] == p2["id"]), {})
    cant_ev = (fila_ev.get("dias", {}).get(FECHA_TAREO) or {}).get("cant_ejecutada")
    check("P17 avance real: aparece en el grid Y en /ev/semana-grid (las 2 vías, un dato)",
          ga.get("real", {}).get(FECHA_TAREO) == 2.5 and cant_ev == 2.5
          and ga.get("saldo") == 2.5,   # metrado_presup 5 - 2.5
          f"real={ga.get('real')} ev={cant_ev} saldo={ga.get('saldo')}")

    # Programación por lote: N partidas → N actividades, metrado default = presupuesto
    r = c.post(f"{API}/ev/programacion/actividades-lote", json={
        "proyecto_id": 1, "otm_id": "OTM-E2E", "responsable": "Cuadrilla E2E",
        "items": [
            {"partida_id": p1["id"], "fecha": FECHA_TAREO, "fecha_fin": "2026-06-03"},
            {"partida_id": p2["id"], "fecha": FECHA_TAREO, "metrado_prog": 4},
        ]})
    lote = r.json() if r.status_code == 200 else {}
    a_l1 = next((a for a in lote.get("actividades", []) if a["partida_id"] == p1["id"]), {})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    gl = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == a_l1.get("id")), {})
    check("P18 lote: 2 partidas -> 2 actividades; sin metrado usa el del presupuesto (10) y prorratea 5/5",
          lote.get("creadas") == 2 and a_l1.get("metrado_prog") == 10
          and gl.get("prog", {}).get(FECHA_TAREO) == 5
          and gl.get("prog", {}).get("2026-06-03") == 5
          and a_l1.get("titulo") == "Partida E2E-001",
          f"status={r.status_code} lote={lote.get('creadas')} prog={gl.get('prog')}")

    # Calendario laboral + saltos intencionales + re-prorrateo con avance (0023)
    r = c.put(f"{API}/ev/programacion/config", json={"proyecto_id": 1, "dias_semana": [1, 2, 3, 4, 5, 6]})
    r2 = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": "2026-06-05", "fecha_fin": "2026-06-08",
        "otm_id": "OTM-E2E", "titulo": "E2E Con calendario y salto",
        "metrado_prog": 90, "dias_salto": ["2026-06-06"]})
    act5 = r2.json() if r2.status_code == 200 else {}
    r3 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": "2026-06-05", "semanas": 2})
    g5 = next((a for g in r3.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act5.get("id")), {})
    check("P19 calendario L-S + salto del sábado: 90 cae solo en vie y lun (45/45)",
          r.status_code == 200 and g5.get("prog") == {"2026-06-05": 45, "2026-06-08": 45}
          and r3.json().get("dias_semana") == [1, 2, 3, 4, 5, 6],
          f"cfg={r.status_code} prog={g5.get('prog')}")

    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "fecha_fin": "2026-06-04",
        "otm_id": "OTM-E2E", "titulo": "E2E Linea base", "partida_id": p1["id"],
        "metrado_prog": 90})
    act6 = r.json() if r.status_code == 200 else {}
    r2 = c.post(f"{API}/ev/programacion/actividades/{act6.get('id')}/avance-dia",
                json={"fecha": FECHA_TAREO, "cantidad": 12})
    r3 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    g6 = next((a for g in r3.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act6.get("id")), {})
    check("P20 avance 12 vs prog 30: el día queda congelado y el saldo 78 se re-prorratea 39/39",
          r2.status_code == 200 and g6.get("prog", {}).get(FECHA_TAREO) == 30
          and g6.get("real", {}).get(FECHA_TAREO) == 12
          and g6.get("prog", {}).get("2026-06-03") == 39
          and g6.get("prog", {}).get("2026-06-04") == 39,
          f"status={r2.status_code} prog={g6.get('prog')} real={g6.get('real')}")

    r = c.put(f"{API}/ev/programacion/actividades/{act6.get('id')}",
              json={"fecha_fin": "2026-06-05"})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    g6 = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act6.get("id")), {})
    check("P21 ampliar F.Fin re-prorratea el saldo en 3 días (26/26/26) sin tocar el congelado",
          r.status_code == 200 and g6.get("prog", {}).get(FECHA_TAREO) == 30
          and g6.get("prog", {}).get("2026-06-03") == 26
          and g6.get("prog", {}).get("2026-06-05") == 26,
          f"prog={g6.get('prog')}")

    # P22: registrar avance en un día intermedio NO toca los días anteriores
    # (act6: rango 02-05, prog {02:30 congelado, 03:26, 04:26, 05:26}, real 02=12)
    r = c.post(f"{API}/ev/programacion/actividades/{act6.get('id')}/avance-dia",
               json={"fecha": "2026-06-04", "cantidad": 26})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    g6 = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act6.get("id")), {})
    check("P22 avance en día intermedio: los anteriores quedan intactos y el saldo cae después (05=52)",
          r.status_code == 200
          and g6.get("prog", {}).get("2026-06-03") == 26      # anterior: intacto
          and g6.get("prog", {}).get("2026-06-04") == 26      # el registrado: congelado
          and g6.get("prog", {}).get("2026-06-05") == 52      # 90-12-26 en el único día posterior
          and g6.get("real", {}).get("2026-06-04") == 26,
          f"prog={g6.get('prog')} real={g6.get('real')}")

    # restaurar el calendario para no contaminar corridas parciales
    c.put(f"{API}/ev/programacion/config", json={"proyecto_id": 1, "dias_semana": [1, 2, 3, 4, 5, 6, 7]})
    for fx in (FECHA_TAREO, "2026-06-04"):
        c.post(f"{API}/ev/programacion/actividades/{act6.get('id')}/avance-dia",
               json={"fecha": fx, "cantidad": None})

    # P23 — F1 LookAhead v2: el avance por la VÍA DEL EV (/ev/avance-diario)
    # re-prorratea la actividad vinculada igual que la vía de programación,
    # y la semana se calcula con core.tiempo.semana_de (base = lunes 06-01).
    r = c.post(f"{API}/ev/avance-diario",
               json={"partida_id": p1["id"], "fecha": "2026-06-03", "cantidad_dia": 5})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    g6 = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act6.get("id")), {})
    cur.execute("SELECT semana FROM ev_avances_diarios WHERE partida_id=%s AND fecha='2026-06-03'",
                (p1["id"],))
    fila_sem = cur.fetchone()
    r3 = c.post(f"{API}/ev/avance-diario",
                json={"partida_id": p1["id"], "fecha": "2026-06-03", "cantidad_dia": None})
    r4 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    g6b = next((a for g in r4.json().get("grupos", []) for a in g["actividades"]
                if a["id"] == act6.get("id")), {})
    check("P23 avance vía EV re-prorratea el LookAhead (42.5/42.5), semana canónica=1 y el null borra",
          r.status_code == 200 and g6.get("real", {}).get("2026-06-03") == 5
          and g6.get("prog", {}).get("2026-06-04") == 42.5
          and g6.get("prog", {}).get("2026-06-05") == 42.5
          and fila_sem is not None and fila_sem[0] == 1
          and r3.status_code == 200 and "2026-06-03" not in g6b.get("real", {}),
          f"real={g6.get('real')} prog={g6.get('prog')} sem={fila_sem}")

    # P24 — F3 v2: causa de no cumplimiento del PLANNER, separada de la de campo;
    # en el Pareto manda la del planner (act2 pasa de MATERIALES a PROGRAMACION).
    r = c.put(f"{API}/ev/programacion/actividades/{act2.get('id')}",
              json={"causa_nc_planner_cat": "PROGRAMACION",
                    "causa_nc_planner": "Secuencia mal estimada"})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": FECHA_TAREO, "semanas": 1})
    a2g = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
                if a["id"] == act2.get("id")), {})
    r3 = c.get(f"{API}/ev/programacion/ppc", params={"proyecto_id": 1, "semanas": 26})
    pareto = {x["causa"]: x["n"] for x in r3.json().get("cnc", [])}
    check("P24 causa del planner: se guarda, sale en el grid y manda en el Pareto",
          r.status_code == 200
          and a2g.get("causa_nc_planner_cat") == "PROGRAMACION"
          and a2g.get("causa_nc_planner") == "Secuencia mal estimada"
          and a2g.get("causa_nc_cat") == "MATERIALES"
          and pareto.get("PROGRAMACION", 0) >= 1,
          f"act={a2g.get('causa_nc_planner_cat')}/{a2g.get('causa_nc_cat')} pareto={pareto}")

    # P25 — F4 v2: medio día pesa 0.5 en el prorrateo (90 en L/M◐/X → 36/18/36)
    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": "2026-06-08", "fecha_fin": "2026-06-10",
        "otm_id": "OTM-E2E", "titulo": "E2E Medio dia", "metrado_prog": 90,
        "dias_medio": ["2026-06-09"]})
    act7 = r.json() if r.status_code == 200 else {}
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": "2026-06-08", "semanas": 1})
    g7 = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act7.get("id")), {})
    check("P25 medio dia pesa 0.5: 90 en 3 días con M◐ -> 36/18/36 y el grid devuelve dias_medio",
          r.status_code == 200
          and g7.get("prog", {}).get("2026-06-08") == 36
          and g7.get("prog", {}).get("2026-06-09") == 18
          and g7.get("prog", {}).get("2026-06-10") == 36
          and g7.get("dias_medio") == ["2026-06-09"],
          f"status={r.status_code} prog={g7.get('prog')} medios={g7.get('dias_medio')}")

    # P26 — F5a v2: dependencia FS con anti-ciclo y retorno en el grid
    # act7 (08-10) será antecesora de act8 (11-12); el ciclo inverso da 409.
    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": "2026-06-11", "fecha_fin": "2026-06-12",
        "otm_id": "OTM-E2E", "titulo": "E2E Sucesora", "metrado_prog": 40})
    act8 = r.json() if r.status_code == 200 else {}
    r2 = c.post(f"{API}/ev/programacion/actividades/{act8.get('id')}/dependencias",
                json={"predecesora_id": act7.get("id"), "lag_dias": 0})
    r3 = c.post(f"{API}/ev/programacion/actividades/{act7.get('id')}/dependencias",
                json={"predecesora_id": act8.get("id")})       # ciclo → 409
    r4 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": "2026-06-08", "semanas": 1})
    g8 = next((a for g in r4.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act8.get("id")), {})
    g7b = next((a for g in r4.json().get("grupos", []) for a in g["actividades"]
                if a["id"] == act7.get("id")), {})
    check("P26 dependencia FS: se crea, el ciclo inverso da 409 y el grid trae PRED./sucesoras",
          r2.status_code == 200 and r3.status_code == 409
          and [p["id"] for p in g8.get("predecesoras", [])] == [act7.get("id")]
          and g8.get("dep_total") == 1
          and g7b.get("sucesoras") == [act8.get("id")],
          f"dep={r2.status_code} ciclo={r3.status_code} preds={g8.get('predecesoras')}")

    # P27 — F5b v2: mover la F.Fin de la antecesora EMPUJA a la sucesora
    # act7 termina ahora el 11 (era 10) → act8 (11-12, dur 2 hábiles) debe
    # arrancar el 12 y terminar el 13, con su metrado re-prorrateado (20/20).
    r = c.put(f"{API}/ev/programacion/actividades/{act7.get('id')}",
              json={"fecha_fin": "2026-06-11"})
    movidas = r.json().get("movidas", []) if r.status_code == 200 else []
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": "2026-06-08", "semanas": 1})
    g8 = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act8.get("id")), {})
    check("P27 cascada: ampliar la antecesora empuja la sucesora al 12-13 y re-prorratea 20/20",
          r.status_code == 200 and movidas == [act8.get("id")]
          and g8.get("fecha") == "2026-06-12" and g8.get("fecha_fin") == "2026-06-13"
          and g8.get("prog", {}).get("2026-06-12") == 20
          and g8.get("prog", {}).get("2026-06-13") == 20,
          f"movidas={movidas} rango={g8.get('fecha')}..{g8.get('fecha_fin')} prog={g8.get('prog')}")

    # P28 — la cascada nunca ADELANTA: acortar la antecesora no mueve a nadie
    r = c.put(f"{API}/ev/programacion/actividades/{act7.get('id')}",
              json={"fecha_fin": "2026-06-09"})
    r2 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": "2026-06-08", "semanas": 1})
    g8b = next((a for g in r2.json().get("grupos", []) for a in g["actividades"]
                if a["id"] == act8.get("id")), {})
    check("P28 la cascada nunca adelanta: acortar la antecesora deja a la sucesora en 12-13",
          r.status_code == 200 and r.json().get("movidas") == []
          and g8b.get("fecha") == "2026-06-12" and g8b.get("fecha_fin") == "2026-06-13",
          f"movidas={r.json().get('movidas')} rango={g8b.get('fecha')}..{g8b.get('fecha_fin')}")

    # ── Hitos + fuente única (migración 0025, encargo 2026-07-18) ──

    # P29 — ROLLUP: cada registro diario deriva ev_avances del hito principal
    # (la ENTRADA del motor EV) = Σ diario de la semana canónica.
    r = c.post(f"{API}/ev/avance-diario",
               json={"partida_id": p1["id"], "fecha": "2026-06-04", "cantidad_dia": 2})
    cur.execute("SELECT COALESCE(SUM(cantidad_dia),0) FROM ev_avances_diarios "
                "WHERE partida_id=%s AND hito_id IS NULL AND semana=1", (p1["id"],))
    suma_diaria = float(cur.fetchone()[0])
    cur.execute("SELECT cantidad_acum FROM ev_avances WHERE hito_id=%s AND semana=1",
                (p1["hito_id"],))
    fila_acum = cur.fetchone()
    check("P29 rollup fuente única: ev_avances(sem 1) del hito principal = suma del diario",
          r.status_code == 200 and fila_acum is not None
          and abs(float(fila_acum[0]) - suma_diaria) < 0.001,
          f"acum={fila_acum} sum_diario={suma_diaria}")

    # P30 — ETAPAS: partida con 2 hitos desplegada por hitos en el lote, con
    # FS encadenado; el diario de la etapa NO principal alimenta SU hito y no
    # aparece en semana-grid (que es solo cantidad instalada = principal).
    cur.execute("INSERT INTO ev_partidas (codigo, fase, descripcion, unidad, "
                "metrado_presup, hh_presup, otm_id) VALUES "
                "('E2E-003','F-E2E','Partida etapas','und',20,80,'OTM-E2E') RETURNING id")
    p3 = cur.fetchone()[0]
    cur.execute("INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal) "
                "VALUES (%s,1,'Habilitación',0.4,false) RETURNING id", (p3,))
    h1 = cur.fetchone()[0]
    cur.execute("INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal) "
                "VALUES (%s,2,'Montaje',0.6,true) RETURNING id", (p3,))
    h2 = cur.fetchone()[0]
    r = c.post(f"{API}/ev/programacion/actividades-lote", json={
        "proyecto_id": 1, "otm_id": "OTM-E2E", "encadenar_hitos": True,
        "items": [
            {"partida_id": p3, "hito_id": h1, "fecha": "2026-06-15", "fecha_fin": "2026-06-16"},
            {"partida_id": p3, "hito_id": h2, "fecha": "2026-06-17", "fecha_fin": "2026-06-18"},
        ]})
    lote = r.json() if r.status_code == 200 else {}
    acts_etapa = lote.get("actividades", [])
    act_h1 = next((a for a in acts_etapa if a.get("hito_id") == h1), {})
    act_h2 = next((a for a in acts_etapa if a.get("hito_id") == h2), {})
    cur.execute("SELECT count(*) FROM prog_dependencias WHERE actividad_id=%s "
                "AND predecesora_id=%s", (act_h2.get("id"), act_h1.get("id")))
    fs_creado = cur.fetchone()[0]
    r2 = c.post(f"{API}/ev/programacion/actividades/{act_h1.get('id')}/avance-dia",
                json={"fecha": "2026-06-15", "cantidad": 4})
    cur.execute("SELECT cantidad_acum FROM ev_avances WHERE hito_id=%s", (h1,))
    acum_h1 = cur.fetchone()
    r3 = c.get(f"{API}/ev/semana-grid", params={"semana": 3, "otm": "OTM-E2E",
                                                "lunes": "2026-06-15"})
    fila3 = next((p for p in r3.json().get("partidas", []) if p["id"] == p3), {})
    cant_grid = (fila3.get("dias", {}).get("2026-06-15") or {}).get("cant_ejecutada")
    r4 = c.get(f"{API}/ev/programacion/lookahead-grid",
               params={"proyecto_id": 1, "desde": "2026-06-15", "semanas": 1})
    ge = next((a for g in r4.json().get("grupos", []) for a in g["actividades"]
               if a["id"] == act_h1.get("id")), {})
    check("P30 etapas: lote por hitos con FS auto, el diario alimenta SU hito y "
          "semana-grid solo muestra el principal",
          r.status_code == 200 and len(acts_etapa) == 2 and fs_creado == 1
          and r2.status_code == 200 and acum_h1 is not None
          and abs(float(acum_h1[0]) - 4) < 0.001
          and cant_grid is None
          and ge.get("hito_desc") == "Habilitación"
          and ge.get("real", {}).get("2026-06-15") == 4,
          f"lote={len(acts_etapa)} fs={fs_creado} acum_h1={acum_h1} "
          f"grid_cant={cant_grid} hito_desc={ge.get('hito_desc')}")

    # P31 — CHECKPOINT: etapa manual sin diario acepta pct; la etapa con
    # diario lo rechaza (409, la gobierna el rollup).
    cur.execute("INSERT INTO ev_partidas (codigo, fase, descripcion, unidad, "
                "metrado_presup, hh_presup, otm_id) VALUES "
                "('E2E-004','F-E2E','Partida checkpoint','und',10,40,'OTM-E2E') RETURNING id")
    p4 = cur.fetchone()[0]
    cur.execute("INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal) "
                "VALUES (%s,1,'QC',0.3,false) RETURNING id", (p4,))
    h4 = cur.fetchone()[0]
    cur.execute("INSERT INTO ev_hitos (partida_id, numero, descripcion, peso, es_principal) "
                "VALUES (%s,2,'Ejecución',0.7,true)", (p4,))
    r = c.post(f"{API}/ev/programacion/hitos/{h4}/checkpoint",
               json={"fecha": FECHA_TAREO, "pct": 1})
    cur.execute("SELECT cantidad_acum FROM ev_avances WHERE hito_id=%s", (h4,))
    acum_h4 = cur.fetchone()
    r2 = c.post(f"{API}/ev/programacion/hitos/{h1}/checkpoint", json={"pct": 1})
    check("P31 checkpoint: etapa manual guarda pct*metrado y la etapa con diario da 409",
          r.status_code == 200 and acum_h4 is not None
          and abs(float(acum_h4[0]) - 10) < 0.001 and r2.status_code == 409,
          f"chk={r.status_code} acum={acum_h4} con_diario={r2.status_code}")

    # P32 — HISTORIAL-GRID: arranca en el primer registro, trae las etapas y
    # marca sin_registros la partida que aún no tiene diario ni tareo.
    r = c.get(f"{API}/ev/programacion/historial-grid", params={"otm": "OTM-E2E"})
    hg = r.json() if r.status_code == 200 else {}
    hg3 = next((p for p in hg.get("partidas", []) if p["id"] == p3), {})
    hg4 = next((p for p in hg.get("partidas", []) if p["id"] == p4), {})
    etapas3 = {e.get("hito_id") for e in hg3.get("etapas", [])}
    check("P32 historial-grid: rango desde el primer registro, etapas por hito y sin_registros",
          r.status_code == 200 and hg.get("desde") == "2026-06-01"
          and h1 in etapas3
          and hg3.get("sin_registros") is False
          and hg4.get("sin_registros") is True,
          f"desde={hg.get('desde')} etapas3={etapas3} sr4={hg4.get('sin_registros')}")

    # P33 — PERFORMANCE: la serie semanal sale del motor con los datos ya
    # registrados (sin llenar nada) y refleja HH ganadas > 0.
    r = c.get(f"{API}/ev/performance", params={"hasta": 3, "otm": "OTM-E2E"})
    pf = r.json() if r.status_code == 200 else {}
    serie = pf.get("serie", [])
    check("P33 performance: serie semanal auto-alimentada con HH ganadas y presupuesto",
          r.status_code == 200 and len(serie) >= 1
          and pf.get("hh_presup_total", 0) > 0
          and any(s.get("hh_ganadas_acum", 0) > 0 for s in serie),
          f"status={r.status_code} n={len(serie)} presup={pf.get('hh_presup_total')}")

    print()
    if _fallas:
        print(f"RESULTADO: {len(_fallas)} verificaciones FALLARON: {_fallas}")
        sys.exit(1)
    print("RESULTADO: TODAS las verificaciones pasaron -- flujo tareo->ISP OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
