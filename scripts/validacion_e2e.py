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
    cur.execute("DELETE FROM prog_actividades WHERE titulo LIKE 'E2E %'")
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
               json={"supervisor_id": "SUPE2E", "causa": "Falta de materiales"})
    r2 = c.get(f"{API}/ev/programacion/semana", params={"proyecto_id": 1, "lunes": FECHA_TAREO})
    a2 = next((a for a in r2.json().get("actividades", []) if a["id"] == act2.get("id")), {})
    check("P10 no-cumplida registra estado y causa",
          r.status_code == 200 and a2.get("estado") == "NO_CUMPLIDA"
          and a2.get("causa_nc") == "Falta de materiales",
          f"status={r.status_code} act={a2.get('estado')}/{a2.get('causa_nc')}")

    r = c.post(f"{API}/ev/programacion/actividades", json={
        "proyecto_id": 1, "fecha": FECHA_TAREO, "otm_id": "OTM-NO-EXISTE-99",
        "titulo": "E2E OTM inválida"})
    check("P11 OTM inexistente -> 400 claro (no 500 sin CORS)",
          r.status_code == 400, f"status={r.status_code}")

    print()
    if _fallas:
        print(f"RESULTADO: {len(_fallas)} verificaciones FALLARON: {_fallas}")
        sys.exit(1)
    print("RESULTADO: TODAS las verificaciones pasaron -- flujo tareo->ISP OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
