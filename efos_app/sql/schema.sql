--
-- EFOS canonical schema — explicit dependency model
--
-- This file is the authoritative initialization script. The application executes
-- it verbatim (byte-for-byte) against a brand-new database, then verifies every
-- table, index, trigger, and enum. A partial or mismatched schema is never
-- modified automatically.
--
-- Model summary:
--   * Preconditions are external three-valued facts (public.conditions).
--   * Prerequisites are internal entity states (task completed / project resolved).
--   * Status and resolution are conjunctive (AND), derived by services.
--   * Temporal availability is a separate optional local-time model; it never
--     changes Task state, resolution, scheduling, or recurrence.
--   * public.events is an append-only audit ledger (no foreign keys).
--
SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET client_min_messages = warning;
SET row_security = off;

--
-- Enumerated state machines
--
CREATE TYPE public.project_state AS ENUM (
    'active',
    'stalled',
    'shelved',
    'resolved'
);

CREATE TYPE public.recurrence_frequency AS ENUM (
    'daily',
    'weekly',
    'monthly',
    'yearly'
);

CREATE TYPE public.task_state AS ENUM (
    'ready',
    'blocked',
    'completed',
    'deferred'
);

--
-- Functions and triggers
--
CREATE FUNCTION public.maintain_previous_state_history() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Keep the old state for the native transition CHECK constraints.
    NEW.previous_state := OLD.current_state;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.update_note_timestamp() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.reject_event_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'public.events is an append-only audit ledger'
        USING ERRCODE = '23514';
END;
$$;

CREATE FUNCTION public.sync_project_requirements_flag() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    target uuid;
    remaining integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target := OLD.project_id;
    ELSE
        target := NEW.project_id;
    END IF;

    SELECT
        (SELECT count(*) FROM public.project_preconditions WHERE project_id = target)
      + (SELECT count(*) FROM public.project_prerequisites WHERE project_id = target)
      INTO remaining;

    UPDATE public.projects
       SET resolution_conditions_unknown = (remaining = 0)
     WHERE id = target;

    RETURN NULL;
END;
$$;

CREATE FUNCTION public.validate_task_status() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN

    -- Structural Dependency Guard (Preconditions & Prerequisites)
    IF NEW.current_state = 'ready' AND OLD.current_state IS DISTINCT FROM 'ready' THEN
        IF EXISTS (
            SELECT 1 FROM public.task_preconditions tp
            JOIN public.conditions c ON c.id = tp.condition_id
            WHERE tp.task_id = NEW.id AND c.evaluation_state IS NOT TRUE
        ) THEN
            RAISE EXCEPTION 'Task % has unresolved preconditions', NEW.id USING ERRCODE = '23514';
        END IF;
        
        IF EXISTS (
            SELECT 1 FROM public.task_prerequisites tp
            LEFT JOIN public.tasks st ON st.id = tp.source_task_id
            LEFT JOIN public.projects sp ON sp.id = tp.source_project_id
            WHERE tp.task_id = NEW.id AND NOT (
                (tp.source_task_id IS NOT NULL AND st.current_state = 'completed') OR 
                (tp.source_project_id IS NOT NULL AND sp.current_state = 'resolved')
            )
        ) THEN
            RAISE EXCEPTION 'Task % has unresolved prerequisites', NEW.id USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE FUNCTION public.validate_project_resolution() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.current_state = 'resolved'
       AND OLD.current_state IS DISTINCT FROM 'resolved' THEN
        IF NEW.resolution_conditions_unknown THEN
            RAISE EXCEPTION 'Project % has undefined requirements', NEW.id
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM public.project_preconditions pp
              JOIN public.conditions c ON c.id = pp.condition_id
             WHERE pp.project_id = NEW.id
               AND c.evaluation_state IS NOT TRUE
        ) THEN
            RAISE EXCEPTION 'Project % has unresolved preconditions', NEW.id
                USING ERRCODE = '23514';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM public.project_prerequisites pp
              LEFT JOIN public.tasks st ON st.id = pp.source_task_id
              LEFT JOIN public.projects sp ON sp.id = pp.source_project_id
             WHERE pp.project_id = NEW.id
               AND NOT (
                    (pp.source_task_id IS NOT NULL AND st.current_state = 'completed')
                 OR (pp.source_project_id IS NOT NULL AND sp.current_state = 'resolved')
               )
        ) THEN
            RAISE EXCEPTION 'Project % has unresolved prerequisites', NEW.id
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

--
-- Core entity tables
--
CREATE TABLE public.schema_meta (
    key text NOT NULL,
    value text NOT NULL
);

INSERT INTO public.schema_meta (key, value) VALUES ('schema_version', '2.1.0');

CREATE TABLE public.conditions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    display_name character varying(255) NOT NULL,
    evaluation_state boolean,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.notes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    content text NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE public.tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(255) CONSTRAINT tasks_title_not_null NOT NULL,
    is_recurring boolean DEFAULT false NOT NULL,
    current_state public.task_state DEFAULT 'ready'::public.task_state NOT NULL,
    previous_state public.task_state,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    scheduled_start timestamp without time zone,
    scheduled_end timestamp without time zone,
    CONSTRAINT valid_task_transition CHECK ((((previous_state IS NULL) AND (current_state = ANY (ARRAY['ready'::public.task_state, 'blocked'::public.task_state]))) OR ((previous_state = 'ready'::public.task_state) AND (current_state = 'blocked'::public.task_state)) OR ((previous_state = 'ready'::public.task_state) AND (current_state = 'completed'::public.task_state)) OR ((previous_state = 'ready'::public.task_state) AND (current_state = 'deferred'::public.task_state)) OR ((previous_state = 'blocked'::public.task_state) AND (current_state = 'ready'::public.task_state)) OR ((previous_state = 'blocked'::public.task_state) AND (current_state = 'deferred'::public.task_state)) OR ((previous_state = 'completed'::public.task_state) AND (current_state = 'ready'::public.task_state)) OR ((previous_state = 'completed'::public.task_state) AND (current_state = 'blocked'::public.task_state)) OR ((previous_state = 'deferred'::public.task_state) AND (current_state = 'ready'::public.task_state)) OR ((previous_state = 'deferred'::public.task_state) AND (current_state = 'blocked'::public.task_state)))),
    CONSTRAINT valid_task_schedule CHECK (((scheduled_start IS NULL AND scheduled_end IS NULL) OR (scheduled_start IS NOT NULL AND scheduled_end IS NOT NULL AND scheduled_end > scheduled_start)))
);

CREATE TABLE public.projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(255) NOT NULL,
    current_state public.project_state DEFAULT 'active'::public.project_state NOT NULL,
    previous_state public.project_state,
    resolution_conditions_unknown boolean DEFAULT true NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT valid_project_transition CHECK ((((previous_state IS NULL) AND (current_state = 'active'::public.project_state)) OR ((previous_state = 'active'::public.project_state) AND (current_state = 'stalled'::public.project_state)) OR ((previous_state = 'active'::public.project_state) AND (current_state = 'shelved'::public.project_state)) OR ((previous_state = 'active'::public.project_state) AND (current_state = 'resolved'::public.project_state)) OR ((previous_state = 'stalled'::public.project_state) AND (current_state = 'active'::public.project_state)) OR ((previous_state = 'stalled'::public.project_state) AND (current_state = 'resolved'::public.project_state)) OR ((previous_state = 'shelved'::public.project_state) AND (current_state = 'active'::public.project_state)) OR ((previous_state = 'shelved'::public.project_state) AND (current_state = 'resolved'::public.project_state)) OR ((previous_state = 'resolved'::public.project_state) AND (current_state = 'active'::public.project_state)) OR ((previous_state = 'resolved'::public.project_state) AND (current_state = 'stalled'::public.project_state)) OR ((previous_state = 'resolved'::public.project_state) AND (current_state = 'shelved'::public.project_state))))
);

--
-- Dependency relationship tables
--
CREATE TABLE public.task_preconditions (
    task_id uuid NOT NULL,
    condition_id uuid NOT NULL
);

CREATE TABLE public.project_preconditions (
    project_id uuid NOT NULL,
    condition_id uuid NOT NULL
);

CREATE TABLE public.task_prerequisites (
    task_id uuid NOT NULL,
    source_task_id uuid,
    source_project_id uuid,
    CONSTRAINT task_prerequisites_exactly_one_source CHECK ((((source_task_id IS NOT NULL) AND (source_project_id IS NULL)) OR ((source_task_id IS NULL) AND (source_project_id IS NOT NULL)))),
    CONSTRAINT task_prerequisites_no_self_reference CHECK (((source_task_id IS NULL) OR (source_task_id <> task_id)))
);

CREATE TABLE public.project_prerequisites (
    project_id uuid NOT NULL,
    source_task_id uuid,
    source_project_id uuid,
    CONSTRAINT project_prerequisites_exactly_one_source CHECK ((((source_task_id IS NOT NULL) AND (source_project_id IS NULL)) OR ((source_task_id IS NULL) AND (source_project_id IS NOT NULL)))),
    CONSTRAINT project_prerequisites_no_self_reference CHECK (((source_project_id IS NULL) OR (source_project_id <> project_id)))
);

--
-- One-to-one notes junctions
--
CREATE TABLE public.task_notes (
    task_id uuid NOT NULL,
    note_id uuid NOT NULL
);

CREATE TABLE public.project_notes (
    project_id uuid NOT NULL,
    note_id uuid NOT NULL
);

CREATE TABLE public.condition_notes (
    condition_id uuid NOT NULL,
    note_id uuid NOT NULL
);

--
-- Google Calendar style recurrence rules
--
CREATE TABLE public.task_recurrence_rules (
    task_id uuid NOT NULL,
    frequency public.recurrence_frequency NOT NULL,
    "interval" integer DEFAULT 1 NOT NULL,
    by_day character varying(50),
    by_month_day integer,
    occurrence_count integer,
    until_date timestamp without time zone,
    reset_hour smallint DEFAULT 18 NOT NULL,
    reset_minute smallint DEFAULT 0 NOT NULL,
    last_reset_window timestamp without time zone,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    end_hour smallint DEFAULT 19 NOT NULL,
    end_minute smallint DEFAULT 0 NOT NULL,
    CONSTRAINT exclusive_end_boundary CHECK (((occurrence_count IS NULL) OR (until_date IS NULL))),
    CONSTRAINT valid_end_hour CHECK (((end_hour >= 0) AND (end_hour <= 23))),
    CONSTRAINT valid_end_minute CHECK (((end_minute >= 0) AND (end_minute <= 59))),
    CONSTRAINT valid_interval CHECK (("interval" > 0)),
    CONSTRAINT valid_month_day CHECK (((by_month_day IS NULL) OR ((by_month_day >= '-31'::integer) AND (by_month_day <= 31) AND (by_month_day <> 0)))),
    CONSTRAINT valid_occurrence_count CHECK (((occurrence_count IS NULL) OR (occurrence_count > 0))),
    CONSTRAINT valid_reset_hour CHECK (((reset_hour >= 0) AND (reset_hour <= 23))),
    CONSTRAINT valid_reset_minute CHECK (((reset_minute >= 0) AND (reset_minute <= 59)))
);

--
-- Optional temporal Task availability (independent of state, schedule, recurrence)
--
-- No rows for a task means the 24/7 default. A row is one local-time window:
-- weekday 0 = Monday … 6 = Sunday; minutes are measured from local midnight and
-- the end is exclusive. Windows never cross midnight in storage; a real-world
-- window such as Mon 22:00-02:00 is normalized into two rows on save.
--
CREATE TABLE public.task_availability_windows (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id uuid NOT NULL,
    weekday smallint NOT NULL,
    start_minute smallint NOT NULL,
    end_minute smallint NOT NULL,
    CONSTRAINT task_availability_windows_pkey PRIMARY KEY (id),
    CONSTRAINT task_availability_windows_unique UNIQUE (task_id, weekday, start_minute, end_minute),
    CONSTRAINT task_availability_valid_weekday CHECK (((weekday >= 0) AND (weekday <= 6))),
    CONSTRAINT task_availability_valid_start CHECK (((start_minute >= 0) AND (start_minute <= 1439))),
    CONSTRAINT task_availability_valid_end CHECK (((end_minute > 0) AND (end_minute <= 1440))),
    CONSTRAINT task_availability_valid_window CHECK ((end_minute > start_minute))
);

--
-- Append-only audit ledger (deliberately no foreign keys)
--
CREATE TABLE public.events (
    id bigserial NOT NULL,
    occurred_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    event_type character varying(64) NOT NULL,
    entity_type character varying(32),
    entity_id uuid,
    summary text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL
);

--
-- Assistant conversation persistence (256-thread rolling FIFO enforced by service)
--
CREATE TABLE public.assistant_threads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    title character varying(160) DEFAULT 'New conversation'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.assistant_messages (
    id bigserial NOT NULL,
    thread_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    content text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT valid_assistant_role CHECK ((role = ANY (ARRAY['user'::character varying, 'assistant'::character varying, 'system'::character varying])))
);

--
-- User custom assistant query presets
--
CREATE TABLE public.custom_queries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    label character varying(80) NOT NULL,
    prompt text NOT NULL,
    favorite boolean DEFAULT false NOT NULL,
    sort_index integer DEFAULT 0 NOT NULL,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

--
-- Google Calendar OAuth account metadata (tokens live in the OS vault only)
--
CREATE TABLE public.calendar_accounts (
    id text NOT NULL,
    email text,
    calendar_id text NOT NULL,
    connected_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_sync_at timestamp without time zone,
    sync_token text,
    sync_status character varying(24) DEFAULT 'never'::character varying NOT NULL,
    last_error text
);

CREATE TABLE public.calendar_events (
    id text NOT NULL,
    account_id text NOT NULL,
    calendar_id text NOT NULL,
    summary text,
    description text,
    location text,
    start_at timestamp without time zone NOT NULL,
    end_at timestamp without time zone NOT NULL,
    all_day boolean DEFAULT false NOT NULL,
    status character varying(24) DEFAULT 'confirmed'::character varying NOT NULL,
    updated_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

--
-- Primary keys, uniqueness, and indexes
--
ALTER TABLE ONLY public.conditions ADD CONSTRAINT conditions_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.schema_meta ADD CONSTRAINT schema_meta_pkey PRIMARY KEY (key);
ALTER TABLE ONLY public.notes ADD CONSTRAINT notes_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.tasks ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.projects ADD CONSTRAINT projects_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.task_preconditions ADD CONSTRAINT task_preconditions_pkey PRIMARY KEY (task_id, condition_id);
ALTER TABLE ONLY public.project_preconditions ADD CONSTRAINT project_preconditions_pkey PRIMARY KEY (project_id, condition_id);
ALTER TABLE ONLY public.task_notes ADD CONSTRAINT task_notes_pkey PRIMARY KEY (task_id);
ALTER TABLE ONLY public.task_notes ADD CONSTRAINT task_notes_note_id_key UNIQUE (note_id);
ALTER TABLE ONLY public.project_notes ADD CONSTRAINT project_notes_pkey PRIMARY KEY (project_id);
ALTER TABLE ONLY public.project_notes ADD CONSTRAINT project_notes_note_id_key UNIQUE (note_id);
ALTER TABLE ONLY public.condition_notes ADD CONSTRAINT condition_notes_pkey PRIMARY KEY (condition_id);
ALTER TABLE ONLY public.condition_notes ADD CONSTRAINT condition_notes_note_id_key UNIQUE (note_id);
ALTER TABLE ONLY public.task_recurrence_rules ADD CONSTRAINT task_recurrence_rules_pkey PRIMARY KEY (task_id);
ALTER TABLE ONLY public.events ADD CONSTRAINT events_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.assistant_threads ADD CONSTRAINT assistant_threads_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.assistant_messages ADD CONSTRAINT assistant_messages_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.custom_queries ADD CONSTRAINT custom_queries_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.calendar_accounts ADD CONSTRAINT calendar_accounts_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.calendar_events ADD CONSTRAINT calendar_events_pkey PRIMARY KEY (id);

CREATE INDEX idx_tasks_schedule ON public.tasks USING btree (scheduled_start, scheduled_end) WHERE ((scheduled_start IS NOT NULL) AND (scheduled_end IS NOT NULL));
CREATE INDEX idx_tasks_state ON public.tasks USING btree (current_state);
CREATE INDEX idx_task_availability_task ON public.task_availability_windows USING btree (task_id, weekday, start_minute);
CREATE INDEX idx_projects_state ON public.projects USING btree (current_state);
CREATE INDEX idx_events_occurred ON public.events USING btree (occurred_at DESC);
CREATE INDEX idx_events_entity ON public.events USING btree (entity_type, entity_id);
CREATE INDEX idx_assistant_messages_thread ON public.assistant_messages USING btree (thread_id, id);
CREATE INDEX idx_assistant_threads_updated ON public.assistant_threads USING btree (updated_at DESC);
CREATE INDEX idx_custom_queries_sort ON public.custom_queries USING btree (favorite DESC, sort_index, created_at);
CREATE INDEX idx_calendar_events_window ON public.calendar_events USING btree (start_at, end_at);
CREATE INDEX idx_calendar_events_account ON public.calendar_events USING btree (account_id);

CREATE UNIQUE INDEX project_prerequisites_project_source_uq ON public.project_prerequisites USING btree (project_id, source_project_id) WHERE (source_project_id IS NOT NULL);
CREATE UNIQUE INDEX project_prerequisites_task_source_uq ON public.project_prerequisites USING btree (project_id, source_task_id) WHERE (source_task_id IS NOT NULL);
CREATE UNIQUE INDEX task_prerequisites_project_source_uq ON public.task_prerequisites USING btree (task_id, source_project_id) WHERE (source_project_id IS NOT NULL);
CREATE UNIQUE INDEX task_prerequisites_task_source_uq ON public.task_prerequisites USING btree (task_id, source_task_id) WHERE (source_task_id IS NOT NULL);

--
-- Foreign keys
--
ALTER TABLE ONLY public.task_preconditions ADD CONSTRAINT task_preconditions_task_fk FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_preconditions ADD CONSTRAINT task_preconditions_condition_fk FOREIGN KEY (condition_id) REFERENCES public.conditions(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_preconditions ADD CONSTRAINT project_preconditions_project_fk FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_preconditions ADD CONSTRAINT project_preconditions_condition_fk FOREIGN KEY (condition_id) REFERENCES public.conditions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.task_prerequisites ADD CONSTRAINT task_prerequisites_task_fk FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_prerequisites ADD CONSTRAINT task_prerequisites_source_task_fk FOREIGN KEY (source_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_prerequisites ADD CONSTRAINT task_prerequisites_source_project_fk FOREIGN KEY (source_project_id) REFERENCES public.projects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.project_prerequisites ADD CONSTRAINT project_prerequisites_project_fk FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_prerequisites ADD CONSTRAINT project_prerequisites_source_task_fk FOREIGN KEY (source_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_prerequisites ADD CONSTRAINT project_prerequisites_source_project_fk FOREIGN KEY (source_project_id) REFERENCES public.projects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.task_notes ADD CONSTRAINT task_notes_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_notes ADD CONSTRAINT task_notes_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_notes ADD CONSTRAINT project_notes_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_notes ADD CONSTRAINT project_notes_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.condition_notes ADD CONSTRAINT condition_notes_condition_id_fkey FOREIGN KEY (condition_id) REFERENCES public.conditions(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.condition_notes ADD CONSTRAINT condition_notes_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.task_recurrence_rules ADD CONSTRAINT fk_recurrence_rule_task FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.task_availability_windows ADD CONSTRAINT task_availability_windows_task_fk FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.assistant_messages ADD CONSTRAINT assistant_messages_thread_fk FOREIGN KEY (thread_id) REFERENCES public.assistant_threads(id) ON DELETE CASCADE;

--
-- Triggers
--
CREATE TRIGGER auto_project_fsm_history BEFORE UPDATE ON public.projects FOR EACH ROW WHEN ((old.current_state IS DISTINCT FROM new.current_state)) EXECUTE FUNCTION public.maintain_previous_state_history();
CREATE TRIGGER auto_task_fsm_history BEFORE UPDATE ON public.tasks FOR EACH ROW WHEN ((old.current_state IS DISTINCT FROM new.current_state)) EXECUTE FUNCTION public.maintain_previous_state_history();
CREATE TRIGGER refresh_note_timestamp BEFORE UPDATE ON public.notes FOR EACH ROW EXECUTE FUNCTION public.update_note_timestamp();
CREATE TRIGGER touch_custom_query_timestamp BEFORE UPDATE ON public.custom_queries FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();
CREATE TRIGGER touch_assistant_thread_timestamp BEFORE UPDATE ON public.assistant_threads FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();
CREATE TRIGGER events_immutable_update_delete BEFORE UPDATE OR DELETE ON public.events FOR EACH ROW EXECUTE FUNCTION public.reject_event_mutation();

CREATE TRIGGER validate_task_status_before_update BEFORE UPDATE ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.validate_task_status();
CREATE TRIGGER validate_project_resolved_before_update BEFORE UPDATE ON public.projects FOR EACH ROW EXECUTE FUNCTION public.validate_project_resolution();

CREATE TRIGGER project_preconditions_sync_flag AFTER INSERT OR DELETE ON public.project_preconditions FOR EACH ROW EXECUTE FUNCTION public.sync_project_requirements_flag();
CREATE TRIGGER project_prerequisites_sync_flag AFTER INSERT OR DELETE ON public.project_prerequisites FOR EACH ROW EXECUTE FUNCTION public.sync_project_requirements_flag();

--
-- Documentation comments
--
COMMENT ON TABLE public.project_preconditions IS 'Precondition object for a dependent Project (3VL external facts).';
COMMENT ON TABLE public.task_preconditions IS 'Precondition object for a dependent Task (3VL external facts).';
COMMENT ON TABLE public.task_prerequisites IS 'Prerequisite whose dependent object is a Task (2VL internal entity state mapping).';
COMMENT ON TABLE public.project_prerequisites IS 'Prerequisite whose dependent object is a Project (2VL internal entity state mapping).';
COMMENT ON TABLE public.task_recurrence_rules IS 'Stores Google Calendar-style recurrence pattern rule structures mapping 1-to-1 with recurring tasks.';
COMMENT ON TABLE public.task_availability_windows IS 'Optional local-time windows when a Task can be performed. No rows = 24/7 default. Independent of Status, scheduled_start/scheduled_end, and recurrence.';
COMMENT ON COLUMN public.task_availability_windows.weekday IS '0 = Monday … 6 = Sunday, matching datetime.weekday().';
COMMENT ON COLUMN public.task_availability_windows.start_minute IS 'Minutes after local midnight; inclusive.';
COMMENT ON COLUMN public.task_availability_windows.end_minute IS 'Minutes after local midnight; exclusive, up to 1440 (midnight).';
COMMENT ON TABLE public.events IS 'Append-only chronological system audit ledger. No foreign keys; UPDATE and DELETE are rejected by trigger.';
COMMENT ON TABLE public.notes IS 'Central one-to-one note content mapped to entities through junction tables.';
COMMENT ON COLUMN public.projects.resolution_conditions_unknown IS 'TRUE blocks automated resolution until valid dependencies are attached.';
COMMENT ON COLUMN public.task_recurrence_rules.last_reset_window IS 'Window occurrence for which the scheduler last fired (idempotency marker).';
