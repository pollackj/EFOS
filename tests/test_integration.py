"""Integration tests against a disposable PostgreSQL database."""
from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import psycopg2
import pytest
from psycopg2 import errors as pg_errors

from efos_app.config import AppConfig
from efos_app.db import validate_readonly_sql
from efos_app.errors import (
    ConflictError,
    EFOSError,
    NotFoundError,
    ReadOnlyQueryError,
    SchemaValidationError,
    ValidationError,
)
from efos_app.models import (
    Edge,
    EdgeKind,
    NodeKind,
    NodeRef,
    ProjectState,
    RecurrenceFrequency,
    TaskState,
)
from efos_app.service import EFOSService


def test_schema_bootstrap_and_connection(service):
    counts = service.repo.schema_counts()
    assert set(counts) == {
        "tasks",
        "projects",
        "conditions",
        "task_preconditions",
        "project_preconditions",
        "task_prerequisites",
        "project_prerequisites",
        "events",
        "notes",
        "assistant_threads",
        "custom_queries",
        "calendar_events",
    }
    status = service.connection_status()
    assert status.connected
    assert status.metadata == service.config.db_display


def test_conjunctive_availability_and_bypass_prevention(service):
    first = service.create_condition("First", evaluation_state=True)
    second = service.create_condition("Second", evaluation_state=False)
    task = service.create_task("Gated work")
    service.connect(NodeRef(NodeKind.CONDITION, first), NodeRef(NodeKind.TASK, task))
    service.connect(NodeRef(NodeKind.CONDITION, second), NodeRef(NodeKind.TASK, task))
    view = service.load_view()
    assert view.snapshot.tasks[task].current_state is TaskState.UNAVAILABLE
    assert not view.gate_for_task(task)

    with pytest.raises(ConflictError):
        service.complete_task(task)
    # The database itself refuses an unearned transition into available.
    with pytest.raises(pg_errors.CheckViolation):
        with service.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.tasks SET current_state = 'available'::public.task_state "
                    "WHERE id = %s::uuid",
                    (task,),
                )

    service.set_condition_value(second, True)
    view = service.load_view()
    assert view.snapshot.tasks[task].current_state is TaskState.AVAILABLE
    assert view.gate_for_task(task)
    service.set_condition_value(first, None)
    assert (
        service.load_view().snapshot.tasks[task].current_state
        is TaskState.UNAVAILABLE
    )


def test_prerequisite_source_states(service):
    source = service.create_task("Source")
    target = service.create_task("Target")
    service.connect(NodeRef(NodeKind.TASK, source), NodeRef(NodeKind.TASK, target))
    view = service.load_view()
    assert view.snapshot.tasks[target].current_state is TaskState.UNAVAILABLE
    service.complete_task(source)
    view = service.load_view()
    assert view.snapshot.tasks[source].current_state is TaskState.COMPLETED
    assert view.snapshot.tasks[target].current_state is TaskState.AVAILABLE

    service.reopen_task(source)
    view = service.load_view()
    assert view.snapshot.tasks[source].current_state is TaskState.AVAILABLE
    assert view.snapshot.tasks[target].current_state is TaskState.UNAVAILABLE


def test_project_unknown_flag_and_auto_resolution(service):
    project = service.create_project("Launch")
    view = service.load_view()
    assert view.snapshot.projects[project].resolution_conditions_unknown is True
    assert view.snapshot.projects[project].current_state is ProjectState.ACTIVE

    condition = service.create_condition("Approved", evaluation_state=False)
    service.connect(NodeRef(NodeKind.CONDITION, condition), NodeRef(NodeKind.PROJECT, project))
    view = service.load_view()
    assert view.snapshot.projects[project].resolution_conditions_unknown is False
    assert view.snapshot.projects[project].current_state is ProjectState.ACTIVE

    service.set_condition_value(condition, True)
    view = service.load_view()
    assert view.snapshot.projects[project].current_state is ProjectState.RESOLVED


def test_shelved_and_stalled_projects_do_not_auto_resolve(service):
    condition = service.create_condition("Ready", evaluation_state=True)
    shelved = service.create_project("Shelved")
    service.shelve_project(shelved)
    service.connect(NodeRef(NodeKind.CONDITION, condition), NodeRef(NodeKind.PROJECT, shelved))
    view = service.load_view()
    assert view.snapshot.projects[shelved].current_state is ProjectState.SHELVED

    service.unshelve_project(shelved)
    view = service.load_view()
    assert view.snapshot.projects[shelved].current_state is ProjectState.RESOLVED

    second = service.create_condition("Second", evaluation_state=True)
    stalled = service.create_project("Stalled")
    service.stall_project(stalled)
    service.connect(NodeRef(NodeKind.CONDITION, second), NodeRef(NodeKind.PROJECT, stalled))
    view = service.load_view()
    assert view.snapshot.projects[stalled].current_state is ProjectState.STALLED
    service.reactivate_project(stalled)
    assert (
        service.load_view().snapshot.projects[stalled].current_state
        is ProjectState.RESOLVED
    )


def test_database_rejects_forced_resolution(service):
    project = service.create_project("Blocked")
    with pytest.raises(pg_errors.CheckViolation):
        with service.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.projects SET current_state = 'resolved'::public.project_state "
                    "WHERE id = %s::uuid",
                    (project,),
                )


def test_dependency_removal_cascade_and_event(service):
    source = service.create_task("Source")
    target = service.create_task("Target")
    edge = Edge(
        NodeRef(NodeKind.TASK, source),
        NodeRef(NodeKind.TASK, target),
        EdgeKind.PREREQUISITE,
    )
    service.connect(edge.source, edge.target)
    assert service.load_view().snapshot.tasks[target].current_state is TaskState.UNAVAILABLE
    service.disconnect(edge)
    view = service.load_view()
    assert view.snapshot.tasks[target].current_state is TaskState.AVAILABLE
    events = service.repo.list_events()
    assert any(event["event_type"] == "dependency.removed" for event in events)
    # The source record is untouched.
    assert view.snapshot.tasks[source].current_state is TaskState.AVAILABLE


def test_notes_blur_semantics_and_atomic_cascade(service):
    task = service.create_task("Note target")
    ref = NodeRef(NodeKind.TASK, task)
    assert service.save_note(ref, "First content") is True
    assert service.get_note(ref) == "First content"
    assert service.save_note(ref, "   ") is False
    assert service.get_note(ref) == "First content"
    service.save_note(ref, "Updated content")
    assert service.get_note(ref) == "Updated content"
    note_ids = service.db.fetch_all(
        "SELECT note_id FROM public.task_notes WHERE task_id = %s::uuid", (task,)
    )
    assert len(note_ids) == 1
    service.delete_task(task)
    assert service.db.fetch_one(
        "SELECT 1 FROM public.notes WHERE id = %s::uuid", (note_ids[0]["note_id"],)
    ) is None
    assert service.db.fetch_one(
        "SELECT 1 FROM public.task_notes WHERE task_id = %s::uuid", (task,)
    ) is None


def test_events_are_append_only_and_survive_deletion(service):
    task = service.create_task("Audited")
    events = service.repo.list_events()
    event_id = events[0]["id"]
    with pytest.raises(pg_errors.CheckViolation):
        with service.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.events SET summary = 'tampered' WHERE id = %s",
                    (event_id,),
                )
    with pytest.raises(pg_errors.CheckViolation):
        with service.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.events WHERE id = %s", (event_id,))
    service.delete_task(task)
    remaining = service.repo.list_events(limit=1000)
    assert any(event["event_type"] == "task.deleted" for event in remaining)
    assert not hasattr(service.repo, "update_event")
    assert not hasattr(service.repo, "delete_event")


def test_recurrence_reset_is_idempotent(service):
    task = service.create_task("Recurring")
    service.complete_task(task)
    now = datetime.now().replace(second=0, microsecond=0)
    service.save_rule(
        task,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        reset_hour=(now - timedelta(minutes=2)).hour,
        reset_minute=(now - timedelta(minutes=2)).minute,
    )
    results = service.recurrence_pulse(now)
    assert any(item[2] == "reset" for item in results)
    assert service.repo.get_task(task).current_state is TaskState.AVAILABLE
    resets = [
        event
        for event in service.repo.list_events(limit=1000)
        if event["event_type"] == "recurrence.reset"
    ]
    assert len(resets) == 1
    # Completing again inside the same window must not reset twice.
    service.complete_task(task)
    second = service.recurrence_pulse(now)
    assert not any(item[2] == "reset" for item in second)
    assert service.repo.get_task(task).current_state is TaskState.COMPLETED


def test_recurrence_reset_blocked_by_dependencies(service):
    source = service.create_task("Recurring source")
    task = service.create_task("Recurring gated")
    service.connect(NodeRef(NodeKind.TASK, source), NodeRef(NodeKind.TASK, task))
    service.complete_task(source)  # clears the prerequisite barrier
    service.complete_task(task)
    service.reopen_task(source)  # dependency regresses while target is historical
    now = datetime.now().replace(second=0, microsecond=0)
    service.save_rule(
        task,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        reset_hour=(now - timedelta(minutes=2)).hour,
        reset_minute=(now - timedelta(minutes=2)).minute,
    )
    results = service.recurrence_pulse(now)
    assert any(item[2] == "blocked by dependencies" for item in results)
    assert service.repo.get_task(task).current_state is TaskState.COMPLETED
    assert any(
        event["event_type"] == "recurrence.blocked"
        for event in service.repo.list_events(limit=1000)
    )


def test_scheduler_pulse_runs_recurrence_and_reconciliation(service):
    task = service.create_task("Pulse task")
    service.complete_task(task)
    now = datetime.now().replace(second=0, microsecond=0)
    service.save_rule(
        task,
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        reset_hour=(now - timedelta(minutes=2)).hour,
        reset_minute=(now - timedelta(minutes=2)).minute,
    )
    results, changes = service.pulse(now)
    assert any(item[2] == "reset" for item in results)
    assert service.repo.get_task(task).current_state is TaskState.AVAILABLE


def test_assistant_thread_fifo(service, monkeypatch):
    import efos_app.repository as repository_module

    monkeypatch.setattr(repository_module, "MAX_ASSISTANT_THREADS", 3)
    threads = [service.create_thread(f"Thread {index}") for index in range(4)]
    assert service.repo.count_threads() == 3
    with pytest.raises(NotFoundError):
        service.load_thread(threads[0])
    service.append_message(threads[-1], "user", "hello")
    service.append_message(threads[-1], "assistant", "world")
    thread, messages = service.load_thread(threads[-1])
    assert thread.message_count == 2
    assert messages[0].content == "hello"


def test_custom_query_metadata_disconnect(service):
    query_id = service.create_custom_query("Overdue Audit", "A much longer backend prompt")
    queries = service.custom_queries()
    assert queries[0].label == "Overdue Audit"
    assert queries[0].prompt == "A much longer backend prompt"
    service.update_custom_query(
        query_id, label="Audit", prompt="Prompt two", favorite=True
    )
    queries = service.custom_queries()
    assert queries[0].label == "Audit"
    assert queries[0].favorite is True
    service.set_custom_query_favorite(query_id, False)
    assert service.custom_queries()[0].favorite is False
    service.delete_custom_query(query_id)
    assert service.custom_queries() == []


def test_calendar_cache_collision_and_stale_state(service):
    start = datetime.now().replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=1)
    task = service.create_task("Local block", start=start, end=end)
    service.repo.save_calendar_account(
        "google", "user@example.com", "primary", status="ok", last_sync_at=start
    )
    service.repo.upsert_calendar_events(
        "google",
        "primary",
        [
            {
                "id": "evt-1",
                "summary": "External meeting",
                "description": "",
                "location": "",
                "status": "confirmed",
                "start_at": start + timedelta(minutes=30),
                "end_at": end + timedelta(minutes=30),
                "all_day": False,
            }
        ],
    )
    collisions = service.schedule_collisions(
        start, end, exclude_task_id=None
    )
    kinds = {item.kind for item in collisions}
    assert "external" in kinds
    items = service.calendar_items(start - timedelta(hours=1), end + timedelta(hours=1))
    assert {item.kind for item in items} == {"task", "external"}
    service.repo.save_calendar_account(
        "google", "user@example.com", "primary", status="stale", error="offline"
    )
    state = service.calendar_sync_state()
    assert state.status == "stale"
    assert state.warning.startswith("Sync Unavailable — Data Stale as of")


def test_google_datetime_local_mapping():
    from efos_app.calendar_service import parse_google_event

    parsed = parse_google_event(
        {
            "id": "x",
            "summary": "Timed",
            "start": {"dateTime": "2026-11-09T15:00:00-06:00"},
            "end": {"dateTime": "2026-11-09T16:00:00-06:00"},
        }
    )
    assert parsed is not None
    assert parsed["start_at"].tzinfo is None
    expected = datetime.fromisoformat("2026-11-09T15:00:00-06:00").astimezone().replace(
        tzinfo=None
    )
    assert parsed["start_at"] == expected
    all_day = parse_google_event(
        {
            "id": "y",
            "summary": "Holiday",
            "start": {"date": "2026-11-09"},
            "end": {"date": "2026-11-10"},
        }
    )
    assert all_day is not None and all_day["all_day"] is True


def test_readonly_sql_guard(service):
    assert (
        validate_readonly_sql("select count(*) from public.tasks")
        == "select count(*) from public.tasks"
    )
    for sql in (
        "delete from public.tasks",
        "select 1; drop table public.tasks",
        "select * from pg_catalog.pg_tables",
        "update public.tasks set name='x'",
        "with x as (delete from public.tasks returning *) select * from x",
    ):
        with pytest.raises(ReadOnlyQueryError):
            validate_readonly_sql(sql)
    service.create_task("Query me")
    columns, rows = service.run_readonly_query(
        "select name, current_state from public.tasks order by name"
    )
    assert columns == ["name", "current_state"]
    assert rows[0]["name"] == "Query me"


def test_partial_schema_aborts_without_modification(test_database):
    base = AppConfig.load()
    name = "efos_bad_" + uuid.uuid4().hex[:8]
    password = base.db_password
    admin = psycopg2.connect(
        host=base.db_host,
        port=base.db_port,
        user=base.admin_user,
        password=password,
        dbname="postgres",
    )
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        conn = psycopg2.connect(
            host=base.db_host,
            port=base.db_port,
            user=base.admin_user,
            password=password,
            dbname=name,
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE public.tasks (id uuid PRIMARY KEY)")
        conn.close()
        config = replace(base, db_name=name)
        service = EFOSService(config)
        with pytest.raises(SchemaValidationError) as info:
            service.start()
        assert any("Missing table" in item for item in info.value.discrepancies)
        # The partial table was not modified or replaced.
        conn = psycopg2.connect(
            host=base.db_host,
            port=base.db_port,
            user=base.admin_user,
            password=password,
            dbname=name,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='tasks'"
            )
            assert cur.fetchone()[0] == 1
        conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
