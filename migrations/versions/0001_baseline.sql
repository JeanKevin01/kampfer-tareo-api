-- Funciones del esquema (Adminer no las exporta). Deben crearse ANTES del trigger y las vistas.
CREATE OR REPLACE FUNCTION public.ev_semana_de(p_fecha date)
 RETURNS integer
 LANGUAGE plpgsql
 STABLE
AS $function$
DECLARE
    v_base DATE;
BEGIN
    SELECT fecha_base INTO v_base FROM ev_config ORDER BY id LIMIT 1;
    IF v_base IS NULL THEN
        SELECT MIN(fecha) INTO v_base FROM registros;
    END IF;
    IF v_base IS NULL THEN RETURN 1; END IF;
    RETURN GREATEST(1, (p_fecha - v_base) / 7 + 1);
END;
$function$
;
CREATE OR REPLACE FUNCTION public.fn_sync_hh_gastadas()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_semana  INTEGER;
    v_hh_acum NUMERIC;
BEGIN
    IF NEW.partida_id IS NULL THEN RETURN NEW; END IF;
    v_semana := ev_semana_de(NEW.fecha);

    IF NOT EXISTS (
        SELECT 1 FROM ev_hh_gastadas
        WHERE partida_id = NEW.partida_id
          AND semana     = v_semana
          AND fuente     = 'manual'
    ) THEN
        -- Recalcular suma total (no solo la fila nueva)
        SELECT COALESCE(SUM(hh), 0) INTO v_hh_acum
        FROM sesion_trabajador_partidas
        WHERE partida_id = NEW.partida_id
          AND ev_semana_de(fecha) = v_semana;

        INSERT INTO ev_hh_gastadas (partida_id, semana, hh, fuente)
        VALUES (NEW.partida_id, v_semana, v_hh_acum, 'tareo_partida')
        ON CONFLICT (partida_id, semana)
        DO UPDATE SET hh = EXCLUDED.hh, fuente = 'tareo_partida';
    END IF;

    RETURN NEW;
END;
$function$
;


-- ===== ESQUEMA (tablas, secuencias, índices, trigger, FKs, vistas) =====

CREATE TABLE "public"."cuadrilla_grupo_miembros" (
    "grupo_id" integer NOT NULL,
    "trab_id" character varying(10) NOT NULL,
    CONSTRAINT "cuadrilla_grupo_miembros_pkey" PRIMARY KEY ("grupo_id", "trab_id")
)
WITH (oids = false);


CREATE SEQUENCE "public".cuadrilla_grupos_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."cuadrilla_grupos" (
    "id" integer DEFAULT nextval('cuadrilla_grupos_id_seq') NOT NULL,
    "supervisor_id" character varying(10) NOT NULL,
    "nombre" character varying(100) DEFAULT 'Mi cuadrilla' NOT NULL,
    "activo" boolean DEFAULT true,
    "creado_en" timestamptz DEFAULT now(),
    CONSTRAINT "cuadrilla_grupos_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX cuadrilla_grupos_supervisor_id_nombre_key ON public.cuadrilla_grupos USING btree (supervisor_id, nombre);


CREATE SEQUENCE "public".cuadrilla_otm_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."cuadrilla_otm" (
    "id" integer DEFAULT nextval('cuadrilla_otm_id_seq') NOT NULL,
    "supervisor_id" character varying NOT NULL,
    "otm_id" character varying NOT NULL,
    "nombre" character varying DEFAULT 'Principal' NOT NULL,
    "trabajador_id" character varying NOT NULL,
    "orden" integer DEFAULT '0',
    "activo" boolean DEFAULT true,
    "created_at" timestamp DEFAULT now(),
    CONSTRAINT "cuadrilla_otm_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX cuadrilla_otm_supervisor_id_otm_id_nombre_trabajador_id_key ON public.cuadrilla_otm USING btree (supervisor_id, otm_id, nombre, trabajador_id);

CREATE INDEX idx_cotm_sup_otm ON public.cuadrilla_otm USING btree (supervisor_id, otm_id, nombre);

CREATE INDEX idx_cotm_trab ON public.cuadrilla_otm USING btree (trabajador_id);


CREATE SEQUENCE "public".cuadrillas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."cuadrillas" (
    "id" integer DEFAULT nextval('cuadrillas_id_seq') NOT NULL,
    "supervisor_id" character varying(10) NOT NULL,
    "trab_id" character varying(5) NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "cuadrillas_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX cuadrillas_supervisor_id_trab_id_key ON public.cuadrillas USING btree (supervisor_id, trab_id);

CREATE INDEX idx_cuadrillas_sup ON public.cuadrillas USING btree (supervisor_id);


CREATE SEQUENCE "public".ev_avances_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_avances" (
    "id" integer DEFAULT nextval('ev_avances_id_seq') NOT NULL,
    "hito_id" integer NOT NULL,
    "semana" integer NOT NULL,
    "cantidad_acum" numeric(14,4) DEFAULT '0' NOT NULL,
    "registrado_en" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "ev_avances_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_avances_hito_id_semana_key ON public.ev_avances USING btree (hito_id, semana);

CREATE INDEX idx_ev_avances_semana ON public.ev_avances USING btree (semana);


CREATE SEQUENCE "public".ev_avances_diarios_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_avances_diarios" (
    "id" integer DEFAULT nextval('ev_avances_diarios_id_seq') NOT NULL,
    "partida_id" integer NOT NULL,
    "fecha" date NOT NULL,
    "semana" integer NOT NULL,
    "cantidad_dia" numeric(10,4) DEFAULT '0',
    "registrado_por" character varying(10),
    "registrado_en" timestamptz DEFAULT now(),
    "notas" text,
    CONSTRAINT "ev_avances_diarios_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_avances_diarios_partida_id_fecha_key ON public.ev_avances_diarios USING btree (partida_id, fecha);

CREATE INDEX idx_ead_semana ON public.ev_avances_diarios USING btree (semana, partida_id);


CREATE TABLE "public"."ev_config" (
    "clave" text NOT NULL,
    "valor" text NOT NULL,
    CONSTRAINT "ev_config_pkey" PRIMARY KEY ("clave")
)
WITH (oids = false);


CREATE TABLE "public"."ev_config_jornada" (
    "dia_semana" integer NOT NULL,
    "hh_dia" numeric(4,2) DEFAULT '9.5' NOT NULL,
    "activo" boolean DEFAULT true,
    CONSTRAINT "ev_config_jornada_pkey" PRIMARY KEY ("dia_semana"),
    CONSTRAINT "ev_config_jornada_dia_semana_check" CHECK ((((dia_semana >= 0) AND (dia_semana <= 6))))
)
WITH (oids = false);


CREATE SEQUENCE "public".ev_hh_gastadas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_hh_gastadas" (
    "id" integer DEFAULT nextval('ev_hh_gastadas_id_seq') NOT NULL,
    "partida_id" integer NOT NULL,
    "semana" integer NOT NULL,
    "hh" numeric(12,2) DEFAULT '0' NOT NULL,
    "fuente" text DEFAULT 'manual' NOT NULL,
    CONSTRAINT "ev_hh_gastadas_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_hh_gastadas_partida_id_semana_key ON public.ev_hh_gastadas USING btree (partida_id, semana);

CREATE INDEX idx_ev_hh_semana ON public.ev_hh_gastadas USING btree (semana);


CREATE SEQUENCE "public".ev_hh_improductivas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_hh_improductivas" (
    "id" integer DEFAULT nextval('ev_hh_improductivas_id_seq') NOT NULL,
    "otm_id" text,
    "semana" integer NOT NULL,
    "hh" numeric NOT NULL,
    "motivo" text,
    "nota" text,
    "registrado_en" timestamptz DEFAULT now() NOT NULL,
    "partida_id" integer,
    CONSTRAINT "ev_hh_improductivas_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);




CREATE SEQUENCE "public".ev_historico_carga_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_historico_carga" (
    "id" integer DEFAULT nextval('ev_historico_carga_id_seq') NOT NULL,
    "otm_id" character varying NOT NULL,
    "partida_id" integer NOT NULL,
    "semana" integer NOT NULL,
    "hh_gastadas_acum" numeric(12,4) DEFAULT '0',
    "cantidad_ejecutada_acum" numeric(12,4) DEFAULT '0',
    "notas" text,
    "cargado_por" character varying,
    "fecha_carga" timestamp DEFAULT now(),
    CONSTRAINT "ev_historico_carga_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_historico_carga_otm_id_partida_id_semana_key ON public.ev_historico_carga USING btree (otm_id, partida_id, semana);

CREATE INDEX idx_hist_otm_sem ON public.ev_historico_carga USING btree (otm_id, semana);

CREATE INDEX idx_hist_partida ON public.ev_historico_carga USING btree (partida_id);


CREATE SEQUENCE "public".ev_hitos_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_hitos" (
    "id" integer DEFAULT nextval('ev_hitos_id_seq') NOT NULL,
    "partida_id" integer NOT NULL,
    "numero" smallint NOT NULL,
    "descripcion" text DEFAULT '' NOT NULL,
    "peso" numeric(6,4) NOT NULL,
    "es_principal" boolean DEFAULT false NOT NULL,
    CONSTRAINT "ev_hitos_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "ev_hitos_numero_check" CHECK ((((numero >= 1) AND (numero <= 10)))),
    CONSTRAINT "ev_hitos_peso_check" CHECK ((((peso > (0)::numeric) AND (peso <= (1)::numeric))))
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_hitos_partida_id_numero_key ON public.ev_hitos USING btree (partida_id, numero);

CREATE INDEX idx_ev_hitos_partida ON public.ev_hitos USING btree (partida_id);


CREATE SEQUENCE "public".ev_jornada_reglas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_jornada_reglas" (
    "id" integer DEFAULT nextval('ev_jornada_reglas_id_seq') NOT NULL,
    "tipo" text DEFAULT 'semanal' NOT NULL,
    "desde" date NOT NULL,
    "dia_semana" integer,
    "hh" numeric(5,2) NOT NULL,
    "nota" text,
    "creado_en" timestamptz DEFAULT now() NOT NULL,
    "otm_id" text,
    CONSTRAINT "ev_jornada_reglas_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);


CREATE SEQUENCE "public".ev_partidas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_partidas" (
    "id" integer DEFAULT nextval('ev_partidas_id_seq') NOT NULL,
    "codigo" text NOT NULL,
    "fase" text,
    "sub_fase" text,
    "descripcion" text NOT NULL,
    "unidad" text,
    "sistema" text,
    "metrado_presup" numeric(14,4) DEFAULT '0' NOT NULL,
    "metrado_proyec" numeric(14,4),
    "hh_presup" numeric(12,2) DEFAULT '0' NOT NULL,
    "activo" boolean DEFAULT true NOT NULL,
    "creado_en" timestamptz DEFAULT now() NOT NULL,
    "otm_id" text,
    "nivel" integer DEFAULT '1',
    "parent_codigo" character varying(200),
    "tipo_costo" text DEFAULT 'DIRECTO' NOT NULL,
    "naturaleza" text DEFAULT 'CONTRACTUAL' NOT NULL,
    "hh_actualizado" numeric,
    CONSTRAINT "ev_partidas_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_partidas_codigo_key ON public.ev_partidas USING btree (codigo);

CREATE INDEX idx_ev_partidas_otm ON public.ev_partidas USING btree (otm_id);

CREATE INDEX idx_ev_partidas_parent ON public.ev_partidas USING btree (parent_codigo, otm_id) WHERE (parent_codigo IS NOT NULL);

CREATE INDEX idx_ev_partidas_otm_activo ON public.ev_partidas USING btree (otm_id, activo);


CREATE TABLE "public"."ev_plantillas_hitos" (
    "tipo_actividad" text NOT NULL,
    "hitos" jsonb NOT NULL,
    CONSTRAINT "ev_plantillas_hitos_pkey" PRIMARY KEY ("tipo_actividad")
)
WITH (oids = false);


CREATE TABLE "public"."ev_tarifas_cargo" (
    "cargo" text NOT NULL,
    "costo_hh" numeric DEFAULT '0' NOT NULL,
    CONSTRAINT "ev_tarifas_cargo_pkey" PRIMARY KEY ("cargo")
)
WITH (oids = false);


CREATE SEQUENCE "public".ev_valorizado_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."ev_valorizado" (
    "id" integer DEFAULT nextval('ev_valorizado_id_seq') NOT NULL,
    "partida_id" integer NOT NULL,
    "semana" integer NOT NULL,
    "cantidad_valorizada" numeric NOT NULL,
    "registrado_en" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "ev_valorizado_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX ev_valorizado_partida_id_semana_key ON public.ev_valorizado USING btree (partida_id, semana);


CREATE SEQUENCE "public".hh_conflictos_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."hh_conflictos" (
    "id" integer DEFAULT nextval('hh_conflictos_id_seq') NOT NULL,
    "trabajador_id" character varying NOT NULL,
    "fecha" date NOT NULL,
    "supervisor_id_1" character varying,
    "supervisor_id_2" character varying,
    "hh_1" numeric(10,4),
    "hh_2" numeric(10,4),
    "estado" character varying DEFAULT 'PENDIENTE',
    "resolucion" character varying,
    "notas" text,
    "created_at" timestamp DEFAULT now(),
    CONSTRAINT "hh_conflictos_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX hh_conflictos_trabajador_id_fecha_supervisor_id_1_superviso_key ON public.hh_conflictos USING btree (trabajador_id, fecha, supervisor_id_1, supervisor_id_2);

CREATE INDEX idx_hh_conf_fecha ON public.hh_conflictos USING btree (fecha);

CREATE INDEX idx_hh_conf_estado ON public.hh_conflictos USING btree (estado);


CREATE TABLE "public"."otms" (
    "id" character varying(15) NOT NULL,
    "sdp" character varying(20),
    "descripcion" text NOT NULL,
    "centro_costo" character varying(50),
    "solicitado_por" character varying(100),
    "gerencia" character varying(100),
    "area" character varying(50),
    "estado" character varying(20) DEFAULT 'POR INICIAR',
    "fecha_aprobacion" date,
    "created_at" timestamp DEFAULT now(),
    "plazo" integer,
    "fecha_inicio" date,
    "fecha_fin" date,
    "monto_contractual" numeric(14,2),
    "monto_valorizado" numeric(14,2) DEFAULT '0',
    CONSTRAINT "otms_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE INDEX idx_otm_estado ON public.otms USING btree (estado);


CREATE SEQUENCE "public".registros_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."registros" (
    "id" integer DEFAULT nextval('registros_id_seq') NOT NULL,
    "trab_id" character varying(5),
    "otm_id" character varying(15),
    "supervisor_id" character varying(10),
    "fecha" date NOT NULL,
    "hora" time without time zone NOT NULL,
    "hh" numeric(4,1),
    "created_at" timestamp DEFAULT now(),
    "partida_id" integer,
    CONSTRAINT "registros_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX registros_trab_id_otm_id_fecha_key ON public.registros USING btree (trab_id, otm_id, fecha);

CREATE INDEX idx_registros_fecha ON public.registros USING btree (fecha);

CREATE INDEX idx_registros_otm ON public.registros USING btree (otm_id);

CREATE INDEX idx_registros_partida ON public.registros USING btree (partida_id, fecha);

CREATE INDEX idx_registros_trab ON public.registros USING btree (trab_id);


CREATE SEQUENCE "public".sesion_trabajador_partidas_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."sesion_trabajador_partidas" (
    "id" integer DEFAULT nextval('sesion_trabajador_partidas_id_seq') NOT NULL,
    "sesion_id" integer NOT NULL,
    "trabajador_id" character varying NOT NULL,
    "partida_id" integer,
    "hh" numeric(10,4) DEFAULT '0' NOT NULL,
    "fecha" date NOT NULL,
    "supervisor_id" character varying,
    "otm_id" character varying NOT NULL,
    "created_at" timestamp DEFAULT now(),
    CONSTRAINT "sesion_trabajador_partidas_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX sesion_trabajador_partidas_sesion_id_trabajador_id_partida__key ON public.sesion_trabajador_partidas USING btree (sesion_id, trabajador_id, partida_id);

CREATE INDEX idx_stp_sesion ON public.sesion_trabajador_partidas USING btree (sesion_id);

CREATE INDEX idx_stp_fecha ON public.sesion_trabajador_partidas USING btree (fecha);

CREATE INDEX idx_stp_partida ON public.sesion_trabajador_partidas USING btree (partida_id);

CREATE INDEX idx_stp_trab_fecha ON public.sesion_trabajador_partidas USING btree (trabajador_id, fecha);



CREATE TRIGGER "trg_sync_hh_gastadas" AFTER INSERT OR UPDATE ON "public"."sesion_trabajador_partidas" FOR EACH ROW EXECUTE FUNCTION fn_sync_hh_gastadas();


CREATE SEQUENCE "public".sesion_trabajadores_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."sesion_trabajadores" (
    "id" integer DEFAULT nextval('sesion_trabajadores_id_seq') NOT NULL,
    "sesion_id" integer NOT NULL,
    "trab_id" character varying(5) NOT NULL,
    "presente" boolean DEFAULT true NOT NULL,
    "hh_override" numeric(4,1),
    "agregado_via" text DEFAULT 'busqueda' NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "sesion_trabajadores_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX sesion_trabajadores_sesion_id_trab_id_key ON public.sesion_trabajadores USING btree (sesion_id, trab_id);

CREATE INDEX idx_ses_trabs ON public.sesion_trabajadores USING btree (sesion_id);


CREATE SEQUENCE "public".sesiones_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."sesiones" (
    "id" integer DEFAULT nextval('sesiones_id_seq') NOT NULL,
    "supervisor_id" character varying(10) NOT NULL,
    "otm_id" character varying(15) NOT NULL,
    "fecha" date DEFAULT CURRENT_DATE NOT NULL,
    "hh_turno" numeric(4,1) DEFAULT '10' NOT NULL,
    "estado" text DEFAULT 'borrador' NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "enviada_at" timestamptz,
    CONSTRAINT "sesiones_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "sesiones_estado_check" CHECK (((estado = ANY (ARRAY['borrador'::text, 'enviada'::text]))))
)
WITH (oids = false);

CREATE INDEX idx_sesiones_sup_fecha ON public.sesiones USING btree (supervisor_id, fecha);

CREATE INDEX idx_sesiones_fecha ON public.sesiones USING btree (fecha);


CREATE TABLE "public"."supervisores" (
    "id" character varying(10) NOT NULL,
    "nombre" character varying(100) NOT NULL,
    "email" character varying(100),
    "activo" boolean DEFAULT true,
    "created_at" timestamp DEFAULT now(),
    CONSTRAINT "supervisores_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);


CREATE SEQUENCE "public".tareo_partida_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."tareo_partida" (
    "id" integer DEFAULT nextval('tareo_partida_id_seq') NOT NULL,
    "trabajador_id" character varying(10) NOT NULL,
    "partida_id" integer NOT NULL,
    "otm_id" character varying(50) NOT NULL,
    "fecha" date NOT NULL,
    "semana" integer,
    "hora_registro" timestamptz DEFAULT now() NOT NULL,
    "hh" numeric(6,4),
    "fuente" character varying(20) DEFAULT 'tareo',
    "supervisor_id" character varying(10),
    "sesion_id" integer,
    "notas" text,
    CONSTRAINT "tareo_partida_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE INDEX idx_tp_fecha_otm ON public.tareo_partida USING btree (fecha, otm_id);

CREATE INDEX idx_tp_trab_fecha ON public.tareo_partida USING btree (trabajador_id, fecha);

CREATE INDEX idx_tp_partida_fecha ON public.tareo_partida USING btree (partida_id, fecha);

CREATE INDEX idx_tp_semana ON public.tareo_partida USING btree (semana, otm_id);


CREATE TABLE "public"."tokens_edicion" (
    "token" character varying(50) NOT NULL,
    "supervisor_id" character varying(10),
    "otm_id" character varying(15),
    "fecha" date,
    "expira_at" timestamp NOT NULL,
    "usado" boolean DEFAULT false,
    "created_at" timestamp DEFAULT now(),
    CONSTRAINT "tokens_edicion_pkey" PRIMARY KEY ("token")
)
WITH (oids = false);


CREATE TABLE "public"."trabajadores" (
    "id" character varying(5) NOT NULL,
    "nombre" character varying(150) NOT NULL,
    "cargo" character varying(100) NOT NULL,
    "dni" character varying(20),
    "activo" boolean DEFAULT true,
    "created_at" timestamp DEFAULT now(),
    "tipo" character varying DEFAULT 'DIRECTO',
    CONSTRAINT "trabajadores_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE INDEX idx_trab_nombre ON public.trabajadores USING btree (nombre);


CREATE SEQUENCE "public".usuarios_id_seq INCREMENT 1 MINVALUE 1 MAXVALUE 2147483647 CACHE 1;

CREATE TABLE "public"."usuarios" (
    "id" integer DEFAULT nextval('usuarios_id_seq') NOT NULL,
    "username" text NOT NULL,
    "password_hash" text NOT NULL,
    "rol" text DEFAULT 'oficina' NOT NULL,
    "nombre" text,
    "activo" boolean DEFAULT true NOT NULL,
    "creado_en" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "usuarios_pkey" PRIMARY KEY ("id")
)
WITH (oids = false);

CREATE UNIQUE INDEX usuarios_username_key ON public.usuarios USING btree (username);




ALTER TABLE ONLY "public"."cuadrilla_grupo_miembros" ADD CONSTRAINT "cuadrilla_grupo_miembros_grupo_id_fkey" FOREIGN KEY (grupo_id) REFERENCES cuadrilla_grupos(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."cuadrilla_otm" ADD CONSTRAINT "cuadrilla_otm_supervisor_id_fkey" FOREIGN KEY (supervisor_id) REFERENCES supervisores(id) ON DELETE CASCADE;
ALTER TABLE ONLY "public"."cuadrilla_otm" ADD CONSTRAINT "cuadrilla_otm_trabajador_id_fkey" FOREIGN KEY (trabajador_id) REFERENCES trabajadores(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."cuadrillas" ADD CONSTRAINT "cuadrillas_supervisor_id_fkey" FOREIGN KEY (supervisor_id) REFERENCES supervisores(id) ON DELETE CASCADE;
ALTER TABLE ONLY "public"."cuadrillas" ADD CONSTRAINT "cuadrillas_trab_id_fkey" FOREIGN KEY (trab_id) REFERENCES trabajadores(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."ev_avances" ADD CONSTRAINT "ev_avances_hito_id_fkey" FOREIGN KEY (hito_id) REFERENCES ev_hitos(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."ev_avances_diarios" ADD CONSTRAINT "ev_avances_diarios_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id);

ALTER TABLE ONLY "public"."ev_hh_gastadas" ADD CONSTRAINT "ev_hh_gastadas_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."ev_historico_carga" ADD CONSTRAINT "ev_historico_carga_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."ev_hitos" ADD CONSTRAINT "ev_hitos_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."hh_conflictos" ADD CONSTRAINT "hh_conflictos_supervisor_id_1_fkey" FOREIGN KEY (supervisor_id_1) REFERENCES supervisores(id);
ALTER TABLE ONLY "public"."hh_conflictos" ADD CONSTRAINT "hh_conflictos_supervisor_id_2_fkey" FOREIGN KEY (supervisor_id_2) REFERENCES supervisores(id);
ALTER TABLE ONLY "public"."hh_conflictos" ADD CONSTRAINT "hh_conflictos_trabajador_id_fkey" FOREIGN KEY (trabajador_id) REFERENCES trabajadores(id);

ALTER TABLE ONLY "public"."registros" ADD CONSTRAINT "registros_otm_id_fkey" FOREIGN KEY (otm_id) REFERENCES otms(id);
ALTER TABLE ONLY "public"."registros" ADD CONSTRAINT "registros_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id);
ALTER TABLE ONLY "public"."registros" ADD CONSTRAINT "registros_supervisor_id_fkey" FOREIGN KEY (supervisor_id) REFERENCES supervisores(id);
ALTER TABLE ONLY "public"."registros" ADD CONSTRAINT "registros_trab_id_fkey" FOREIGN KEY (trab_id) REFERENCES trabajadores(id);

ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id) ON DELETE SET NULL;
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_sesion_id_fkey" FOREIGN KEY (sesion_id) REFERENCES sesiones(id) ON DELETE CASCADE;
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_supervisor_id_fkey" FOREIGN KEY (supervisor_id) REFERENCES supervisores(id);
ALTER TABLE ONLY "public"."sesion_trabajador_partidas" ADD CONSTRAINT "sesion_trabajador_partidas_trabajador_id_fkey" FOREIGN KEY (trabajador_id) REFERENCES trabajadores(id) ON DELETE CASCADE;

ALTER TABLE ONLY "public"."sesion_trabajadores" ADD CONSTRAINT "sesion_trabajadores_sesion_id_fkey" FOREIGN KEY (sesion_id) REFERENCES sesiones(id) ON DELETE CASCADE;
ALTER TABLE ONLY "public"."sesion_trabajadores" ADD CONSTRAINT "sesion_trabajadores_trab_id_fkey" FOREIGN KEY (trab_id) REFERENCES trabajadores(id);

ALTER TABLE ONLY "public"."sesiones" ADD CONSTRAINT "sesiones_otm_id_fkey" FOREIGN KEY (otm_id) REFERENCES otms(id);
ALTER TABLE ONLY "public"."sesiones" ADD CONSTRAINT "sesiones_supervisor_id_fkey" FOREIGN KEY (supervisor_id) REFERENCES supervisores(id);

ALTER TABLE ONLY "public"."tareo_partida" ADD CONSTRAINT "tareo_partida_partida_id_fkey" FOREIGN KEY (partida_id) REFERENCES ev_partidas(id);
ALTER TABLE ONLY "public"."tareo_partida" ADD CONSTRAINT "tareo_partida_sesion_id_fkey" FOREIGN KEY (sesion_id) REFERENCES sesiones(id);

CREATE VIEW "public"."ev_hh_tareo" AS SELECT partida_id,
    fecha,
    sum(hh) AS hh
   FROM registros
  WHERE ((partida_id IS NOT NULL) AND (hh IS NOT NULL) AND (hh > (0)::numeric))
  GROUP BY partida_id, fecha;

CREATE VIEW "public"."v_hh_sesion_partida_semana" AS SELECT partida_id,
    otm_id,
    ev_semana_de(fecha) AS semana,
    sum(hh) AS hh_total,
    count(DISTINCT fecha) AS dias
   FROM sesion_trabajador_partidas
  WHERE (partida_id IS NOT NULL)
  GROUP BY partida_id, otm_id, (ev_semana_de(fecha));


-- ===== OWNED BY: la secuencia se borra junto con su tabla (faithful a prod) =====
ALTER SEQUENCE "public".cuadrilla_grupos_id_seq OWNED BY "public".cuadrilla_grupos.id;
ALTER SEQUENCE "public".cuadrilla_otm_id_seq OWNED BY "public".cuadrilla_otm.id;
ALTER SEQUENCE "public".cuadrillas_id_seq OWNED BY "public".cuadrillas.id;
ALTER SEQUENCE "public".ev_avances_id_seq OWNED BY "public".ev_avances.id;
ALTER SEQUENCE "public".ev_avances_diarios_id_seq OWNED BY "public".ev_avances_diarios.id;
ALTER SEQUENCE "public".ev_hh_gastadas_id_seq OWNED BY "public".ev_hh_gastadas.id;
ALTER SEQUENCE "public".ev_hh_improductivas_id_seq OWNED BY "public".ev_hh_improductivas.id;
ALTER SEQUENCE "public".ev_historico_carga_id_seq OWNED BY "public".ev_historico_carga.id;
ALTER SEQUENCE "public".ev_hitos_id_seq OWNED BY "public".ev_hitos.id;
ALTER SEQUENCE "public".ev_jornada_reglas_id_seq OWNED BY "public".ev_jornada_reglas.id;
ALTER SEQUENCE "public".ev_partidas_id_seq OWNED BY "public".ev_partidas.id;
ALTER SEQUENCE "public".ev_valorizado_id_seq OWNED BY "public".ev_valorizado.id;
ALTER SEQUENCE "public".hh_conflictos_id_seq OWNED BY "public".hh_conflictos.id;
ALTER SEQUENCE "public".registros_id_seq OWNED BY "public".registros.id;
ALTER SEQUENCE "public".sesion_trabajador_partidas_id_seq OWNED BY "public".sesion_trabajador_partidas.id;
ALTER SEQUENCE "public".sesion_trabajadores_id_seq OWNED BY "public".sesion_trabajadores.id;
ALTER SEQUENCE "public".sesiones_id_seq OWNED BY "public".sesiones.id;
ALTER SEQUENCE "public".tareo_partida_id_seq OWNED BY "public".tareo_partida.id;
ALTER SEQUENCE "public".usuarios_id_seq OWNED BY "public".usuarios.id;
