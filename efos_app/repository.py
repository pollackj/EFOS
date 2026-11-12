"""Typed repository over the canonical EFOS PostgreSQL schema.

Every mutation that changes lifecycle state, dependencies, notes, or the audit
ledger runs inside one explicit transaction. ``public.events`` is only ever
appended to; this module deliberately exposes no update or delete method for it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Iterable
from uuid import UUID

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import Json

from .db import Database
from .errors import NotFoundError, ValidationError
from .models import (
    CalendarItem,
    ChatMessage,
    ChatThread,
    Condition,
    ConnectionStatus,
    CustomQuery,
    NodeKind,
    NodeRef,
    Project,
    ProjectState,
    RecurrenceFrequency,
    RecurrenceRule,
    SyncState,
    Task,
    TaskState,
    WorkspaceSnapshot,
)

log = logging.getLogger(__name__)

MAX_ASSISTANT_THREADS = 256

_JUNCTION = {
    NodeKind.TASK: ("task_notes", "task_id", "public.tasks"),
    NodeKind.PROJECT: ("project_notes", "project_id", "public.projects"),
    NodeKind.CONDITION: ("condition_notes", "condition_id", "public.conditions"),
}

def as_uuid(value: str, label: str = "identifier") -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError(f"Invalid {label}: expected a UUID.") from exc

def clean_name(value: str, *, label: str = "Name") -> str:
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{label} cannot be empty.")
    if len(text) > 255:
        raise ValidationError(f"{label} must be 255 characters or fewer.")
    return text

def _naive_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)

def _schedule(
    start: datetime | None, end: datetime | None
) -> tuple[datetime | None, datetime | None]:
    if start is None and end is None:
        return None, None
    if start is None or end is None:
        raise ValidationError("A schedule requires both a start and an end time.")
    start = _naive_local(start)
    end = _naive_local(end)
    assert start is not None and end is not None
    if start >= end:
        raise ValidationError("The schedule end must be after its start.")
    return start, end

def _rule_from_row(row: dict[str, Any]) -> RecurrenceRule:
    return RecurrenceRule(
        task_id=str(row["task_id"]),
        frequency=RecurrenceFrequency(row["frequency"]),
        interval=int(row["interval"]),
        by_day=row["by_day"],
        by_month_day=row["by_month_day"],
        occurrence_count=row["occurrence_count"],
        until_date=row["until_date"],
        reset_hour=int(row["reset_hour"]),
        reset_minute=int(row["reset_minute"]),
        end_hour=int(row["end_hour"]),
        end_minute=int(row["end_minute"]),
        last_reset_window=row["last_reset_window"],
        created_at=row["created_at"],
    )

def _task_from_row(row: dict[str, Any], rule: RecurrenceRule | None = None) -> Task:
    return Task(
        id=str(row["id"]),
        name=row["name"],
        is_recurring=bool(row["is_recurring"]),
        current_state=TaskState(row["current_state"]),
        previous_state=TaskState(row["previous_state"]) if row["previous_state"] else None,
        created_at=row["created_at"],
        scheduled_start=row["scheduled_start"],
        scheduled_end=row["scheduled_end"],
        recurrence=rule,
    )

def _project_from_row(row: dict[str, Any]) -> Project:
    return Project(
        id=str(row["id"]),
        name=row["name"],
        current_state=ProjectState(row["current_state"]),
        previous_state=ProjectState(row["previous_state"]) if row["previous_state"] else None,
        resolution_conditions_unknown=bool(row["resolution_conditions_unknown"]),
        created_at=row["created_at"],
    )

def _condition_from_row(row: dict[str, Any]) -> Condition:
    return Condition(
        id=str(row["id"]),
        display_name=row["display_name"],
        evaluation_state=row["evaluation_state"],
        created_at=row["created_at"],
    )

def _insert_event(
    cur: Any,
    event_type: str,
    entity_type: str | None,
    entity_id: str | None,
    summary: str,
    details: dict[str, Any] | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO public.events
            (event_type, entity_type, entity_id, summary, details)
        VALUES (%s, %s, %s::uuid, %s, %s)
        """,
        (
            event_type,
            entity_type,
            entity_id,
            summary,
            Json(details or {}),
        ),
    )

class Repository:
    def __init__(self, database: Database):
        self.db = database

    # ------------------------------------------------------------------ status
    def connection_status(self) -> ConnectionStatus:
        """Parse live connectivity without keeping any state in the repository."""
        return self.db.probe()

    # ------------------------------------------------------------------- reads
    def snapshot(self) -> WorkspaceSnapshot:
        snapshot = WorkspaceSnapshot(loaded_at=datetime.now())
        rules: dict[str, RecurrenceRule] = {}
        for row in self.db.fetch_all("SELECT * FROM public.task_recurrence_rules"):
            rule = _rule_from_row(row)
            rules[rule.task_id] = rule
        for row in self.db.fetch_all(
            "SELECT * FROM public.tasks ORDER BY created_at NULLS LAST, name"
        ):
            task = _task_from_row(row, rules.get(str(row["id"])))
            snapshot.tasks[task.id] = task
        for row in self.db.fetch_all(
            "SELECT * FROM public.projects ORDER BY created_at NULLS LAST, name"
        ):
            project = _project_from_row(row)
            snapshot.projects[project.id] = project
        for row in self.db.fetch_all(
            "SELECT * FROM public.conditions ORDER BY created_at NULLS LAST, display_name"
        ):
            condition = _condition_from_row(row)
            snapshot.conditions[condition.id] = condition
        for row in self.db.fetch_all(
            "SELECT task_id, condition_id FROM public.task_preconditions"
        ):
            snapshot.task_preconditions.setdefault(str(row["task_id"]), set()).add(
                str(row["condition_id"])
            )
        for row in self.db.fetch_all(
            "SELECT project_id, condition_id FROM public.project_preconditions"
        ):
            snapshot.project_preconditions.setdefault(str(row["project_id"]), set()).add(
                str(row["condition_id"])
            )
        for row in self.db.fetch_all(
            "SELECT task_id, source_task_id, source_project_id FROM public.task_prerequisites"
        ):
            source = (
                NodeRef(NodeKind.TASK, str(row["source_task_id"]))
                if row["source_task_id"]
                else NodeRef(NodeKind.PROJECT, str(row["source_project_id"]))
            )
            snapshot.task_prerequisites.setdefault(str(row["task_id"]), set()).add(source)
        for row in self.db.fetch_all(
            "SELECT project_id, source_task_id, source_project_id FROM public.project_prerequisites"
        ):
            source = (
                NodeRef(NodeKind.TASK, str(row["source_task_id"]))
                if row["source_task_id"]
                else NodeRef(NodeKind.PROJECT, str(row["source_project_id"]))
            )
            snapshot.project_prerequisites.setdefault(str(row["project_id"]), set()).add(
                source
            )
        return snapshot

    def get_task(self, task_id: str) -> Task:
        task_id = as_uuid(task_id, "task id")
        row = self.db.fetch_one("SELECT * FROM public.tasks WHERE id = %s::uuid", (task_id,))
        if not row:
            raise NotFoundError("The task no longer exists.")
        rule_row = self.db.fetch_one(
            "SELECT * FROM public.task_recurrence_rules WHERE task_id = %s::uuid",
            (task_id,),
        )
        return _task_from_row(row, _rule_from_row(rule_row) if rule_row else None)

    def get_project(self, project_id: str) -> Project:
        project_id = as_uuid(project_id, "project id")
        row = self.db.fetch_one(
            "SELECT * FROM public.projects WHERE id = %s::uuid", (project_id,)
        )
        if not row:
            raise NotFoundError("The project no longer exists.")
        return _project_from_row(row)

    def get_condition(self, condition_id: str) -> Condition:
        condition_id = as_uuid(condition_id, "condition id")
        row = self.db.fetch_one(
            "SELECT * FROM public.conditions WHERE id = %s::uuid", (condition_id,)
        )
        if not row:
            raise NotFoundError("The condition no longer exists.")
        return _condition_from_row(row)

    def get_rule(self, task_id: str) -> RecurrenceRule | None:
        task_id = as_uuid(task_id, "task id")
        row = self.db.fetch_one(
            "SELECT * FROM public.task_recurrence_rules WHERE task_id = %s::uuid",
            (task_id,),
        )
        return _rule_from_row(row) if row else None

    # ------------------------------------------------------------------- notes
    def get_note(self, kind: NodeKind, entity_id: str) -> str:
        table, column, _ = _JUNCTION[kind]
        entity_id = as_uuid(entity_id, f"{kind.value} id")
        row = self.db.fetch_one(
            f"""
            SELECT n.content
              FROM public.notes n
              JOIN public.{table} j ON j.note_id = n.id
             WHERE j.{column} = %s::uuid
            """,
            (entity_id,),
        )
        return row["content"] if row else ""

    def save_note(self, kind: NodeKind, entity_id: str, content: str) -> None:
        content = content or ""
        if not content.strip():
            # Empty edits never overwrite or delete historical content.
            return
        table, column, _ = _JUNCTION[kind]
        entity_id = as_uuid(entity_id, f"{kind.value} id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT note_id FROM public.{table} WHERE {column} = %s::uuid",
                    (entity_id,),
                )
                row = cur.fetchone()
                if row:
                    note_id = str(row[0])
                    cur.execute(
                        "UPDATE public.notes SET content = %s WHERE id = %s::uuid",
                        (content, note_id),
                    )
                else:
                    cur.execute(
                        "INSERT INTO public.notes (content) VALUES (%s) RETURNING id",
                        (content,),
                    )
                    note_id = str(cur.fetchone()[0])
                    cur.execute(
                        f"INSERT INTO public.{table} ({column}, note_id) "
                        "VALUES (%s::uuid, %s::uuid)",
                        (entity_id, note_id),
                    )
                _insert_event(
                    cur,
                    "note.saved",
                    kind.value,
                    entity_id,
                    f"Note saved for {kind.value}",
                    {"length": len(content)},
                )

    def delete_note(self, kind: NodeKind, entity_id: str) -> None:
        table, column, _ = _JUNCTION[kind]
        entity_id = as_uuid(entity_id, f"{kind.value} id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT note_id FROM public.{table} WHERE {column} = %s::uuid",
                    (entity_id,),
                )
                row = cur.fetchone()
                if not row:
                    return
                note_id = str(row[0])
                cur.execute(
                    f"DELETE FROM public.{table} WHERE {column} = %s::uuid",
                    (entity_id,),
                )
                cur.execute("DELETE FROM public.notes WHERE id = %s::uuid", (note_id,))
                _insert_event(
                    cur,
                    "note.deleted",
                    kind.value,
                    entity_id,
                    f"Note deleted for {kind.value}",
                    {"note_id": note_id},
                )

    def note_summary(self) -> dict[str, int]:
        rows = self.db.fetch_all("SELECT count(*) AS n FROM public.notes")
        return {"notes": int(rows[0]["n"]) if rows else 0}

    # ------------------------------------------------------------------ events
    def append_event(
        self,
        event_type: str,
        entity_type: str | None,
        entity_id: str | None,
        summary: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                _insert_event(cur, event_type, entity_type, entity_id, summary, details)

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            "SELECT * FROM public.events ORDER BY occurred_at DESC, id DESC LIMIT %s",
            (max(1, min(limit, 1000)),),
        )

    # ------------------------------------------------------------------- tasks
    def create_task(
        self,
        name: str,
        *,
        is_recurring: bool = False,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> str:
        name = clean_name(name, label="Task title")
        start, end = _schedule(start, end)
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.tasks
                        (name, is_recurring, current_state, previous_state,
                         scheduled_start, scheduled_end)
                    VALUES (%s, %s, 'available'::public.task_state, NULL, %s, %s)
                    RETURNING id
                    """,
                    (name, bool(is_recurring), start, end),
                )
                task_id = str(cur.fetchone()[0])
                _insert_event(
                    cur,
                    "task.created",
                    "task",
                    task_id,
                    f"Task created: {name}",
                    {"name": name, "is_recurring": bool(is_recurring)},
                )
        return task_id

    def update_task(self, task_id: str, *, name: str, is_recurring: bool) -> None:
        task_id = as_uuid(task_id, "task id")
        name = clean_name(name, label="Task title")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.tasks SET name = %s, is_recurring = %s "
                    "WHERE id = %s::uuid RETURNING name",
                    (name, bool(is_recurring), task_id),
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The task no longer exists.")
                _insert_event(
                    cur,
                    "task.updated",
                    "task",
                    task_id,
                    f"Task updated: {name}",
                    {"name": name, "is_recurring": bool(is_recurring)},
                )

    def update_task_schedule(
        self, task_id: str, start: datetime | None, end: datetime | None
    ) -> None:
        task_id = as_uuid(task_id, "task id")
        start, end = _schedule(start, end)
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.tasks SET scheduled_start = %s, scheduled_end = %s "
                    "WHERE id = %s::uuid RETURNING name",
                    (start, end, task_id),
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The task no longer exists.")
                if start is not None and end is not None:
                    cur.execute(
                        """
                        UPDATE public.task_recurrence_rules
                           SET reset_hour = %s,
                               reset_minute = %s,
                               end_hour = %s,
                               end_minute = %s,
                               last_reset_window = %s
                         WHERE task_id = %s::uuid
                        """,
                        (start.hour, start.minute, end.hour, end.minute, start, task_id),
                    )
                _insert_event(
                    cur,
                    "task.schedule_changed",
                    "task",
                    task_id,
                    f"Schedule updated: {row[0]}",
                    {
                        "scheduled_start": start.isoformat() if start else None,
                        "scheduled_end": end.isoformat() if end else None,
                    },
                )

    def transition_task(
        self,
        task_id: str,
        target: TaskState,
        *,
        expected: TaskState | None = None,
        reason: str = "manual",
    ) -> None:
        task_id = as_uuid(task_id, "task id")
        params: list[Any] = [target.value, task_id]
        where = "id = %s::uuid"
        if expected is not None:
            where += " AND current_state = %s::public.task_state"
            params.append(expected.value)
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        f"UPDATE public.tasks SET current_state = %s::public.task_state "
                        f"WHERE {where} RETURNING name, previous_state",
                        tuple(params),
                    )
                except pg_errors.CheckViolation as exc:
                    raise ValidationError(
                        "The database state machine rejected this task transition."
                    ) from exc
                row = cur.fetchone()
                if not row:
                    raise NotFoundError(
                        "The task changed elsewhere before this action completed. "
                        "Refresh and retry."
                    )
                name = row[0]
                previous = row[1] or "—"
                _insert_event(
                    cur,
                    "task.state_changed",
                    "task",
                    task_id,
                    f"{name}: {previous} → {target.value}",
                    {
                        "previous": str(previous),
                        "current": target.value,
                        "reason": reason,
                    },
                )

    def delete_task(self, task_id: str) -> None:
        task_id = as_uuid(task_id, "task id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM public.tasks WHERE id = %s::uuid", (task_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The task no longer exists.")
                name = row[0]
                cur.execute(
                    "SELECT note_id FROM public.task_notes WHERE task_id = %s::uuid",
                    (task_id,),
                )
                note_ids = [str(item[0]) for item in cur.fetchall()]
                cur.execute(
                    "DELETE FROM public.task_notes WHERE task_id = %s::uuid", (task_id,)
                )
                if note_ids:
                    cur.execute(
                        "DELETE FROM public.notes WHERE id = ANY(%s::uuid[])", (note_ids,)
                    )
                cur.execute("DELETE FROM public.tasks WHERE id = %s::uuid", (task_id,))
                _insert_event(
                    cur,
                    "task.deleted",
                    "task",
                    task_id,
                    f"Task deleted: {name}",
                    {"name": name, "note_ids": note_ids},
                )

    # ---------------------------------------------------------------- projects
    def create_project(self, name: str) -> str:
        name = clean_name(name, label="Project name")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.projects
                        (name, current_state, previous_state, resolution_conditions_unknown)
                    VALUES (%s, 'active'::public.project_state, NULL, true)
                    RETURNING id
                    """,
                    (name,),
                )
                project_id = str(cur.fetchone()[0])
                _insert_event(
                    cur,
                    "project.created",
                    "project",
                    project_id,
                    f"Project created: {name}",
                    {"name": name, "requirements_undefined": True},
                )
        return project_id

    def update_project(self, project_id: str, *, name: str) -> None:
        project_id = as_uuid(project_id, "project id")
        name = clean_name(name, label="Project name")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.projects SET name = %s WHERE id = %s::uuid "
                    "RETURNING name",
                    (name, project_id),
                )
                if not cur.fetchone():
                    raise NotFoundError("The project no longer exists.")
                _insert_event(
                    cur,
                    "project.updated",
                    "project",
                    project_id,
                    f"Project renamed: {name}",
                    {"name": name},
                )

    def transition_project(
        self,
        project_id: str,
        target: ProjectState,
        *,
        expected: ProjectState | None = None,
        reason: str = "manual",
    ) -> None:
        project_id = as_uuid(project_id, "project id")
        params: list[Any] = [target.value, project_id]
        where = "id = %s::uuid"
        if expected is not None:
            where += " AND current_state = %s::public.project_state"
            params.append(expected.value)
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        f"UPDATE public.projects "
                        f"SET current_state = %s::public.project_state "
                        f"WHERE {where} RETURNING name, previous_state",
                        tuple(params),
                    )
                except pg_errors.CheckViolation as exc:
                    raise ValidationError(
                        "The database rejected this project transition. Requirements "
                        "may be undefined or dependency barriers may be unresolved."
                    ) from exc
                row = cur.fetchone()
                if not row:
                    raise NotFoundError(
                        "The project changed elsewhere before this action completed. "
                        "Refresh and retry."
                    )
                name, previous = row[0], row[1] or "—"
                _insert_event(
                    cur,
                    "project.state_changed",
                    "project",
                    project_id,
                    f"{name}: {previous} → {target.value}",
                    {
                        "previous": str(previous),
                        "current": target.value,
                        "reason": reason,
                    },
                )

    def delete_project(self, project_id: str) -> None:
        project_id = as_uuid(project_id, "project id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM public.projects WHERE id = %s::uuid", (project_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The project no longer exists.")
                name = row[0]
                cur.execute(
                    "SELECT note_id FROM public.project_notes WHERE project_id = %s::uuid",
                    (project_id,),
                )
                note_ids = [str(item[0]) for item in cur.fetchall()]
                cur.execute(
                    "DELETE FROM public.project_notes WHERE project_id = %s::uuid",
                    (project_id,),
                )
                if note_ids:
                    cur.execute(
                        "DELETE FROM public.notes WHERE id = ANY(%s::uuid[])", (note_ids,)
                    )
                cur.execute(
                    "DELETE FROM public.projects WHERE id = %s::uuid", (project_id,)
                )
                _insert_event(
                    cur,
                    "project.deleted",
                    "project",
                    project_id,
                    f"Project deleted: {name}",
                    {"name": name, "note_ids": note_ids},
                )

    # -------------------------------------------------------------- conditions
    def create_condition(
        self, display_name: str, *, evaluation_state: bool | None = None
    ) -> str:
        display_name = clean_name(display_name, label="Condition name")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.conditions (display_name, evaluation_state) "
                    "VALUES (%s, %s) RETURNING id",
                    (display_name, evaluation_state),
                )
                condition_id = str(cur.fetchone()[0])
                _insert_event(
                    cur,
                    "condition.created",
                    "condition",
                    condition_id,
                    f"Condition created: {display_name}",
                    {"name": display_name, "evaluation_state": evaluation_state},
                )
        return condition_id

    def update_condition(
        self, condition_id: str, *, display_name: str, evaluation_state: bool | None
    ) -> None:
        condition_id = as_uuid(condition_id, "condition id")
        display_name = clean_name(display_name, label="Condition name")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.conditions SET display_name = %s, evaluation_state = %s "
                    "WHERE id = %s::uuid RETURNING display_name",
                    (display_name, evaluation_state, condition_id),
                )
                if not cur.fetchone():
                    raise NotFoundError("The condition no longer exists.")
                _insert_event(
                    cur,
                    "condition.updated",
                    "condition",
                    condition_id,
                    f"Condition updated: {display_name}",
                    {
                        "name": display_name,
                        "evaluation_state": evaluation_state,
                    },
                )

    def set_condition_value(self, condition_id: str, value: bool | None) -> None:
        condition_id = as_uuid(condition_id, "condition id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.conditions SET evaluation_state = %s "
                    "WHERE id = %s::uuid RETURNING display_name",
                    (value, condition_id),
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The condition no longer exists.")
                _insert_event(
                    cur,
                    "condition.value_changed",
                    "condition",
                    condition_id,
                    f"{row[0]}: {_truth(value)}",
                    {"value": value},
                )

    def delete_condition(self, condition_id: str) -> None:
        condition_id = as_uuid(condition_id, "condition id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT display_name FROM public.conditions WHERE id = %s::uuid",
                    (condition_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The condition no longer exists.")
                name = row[0]
                cur.execute(
                    "SELECT note_id FROM public.condition_notes WHERE condition_id = %s::uuid",
                    (condition_id,),
                )
                note_ids = [str(item[0]) for item in cur.fetchall()]
                cur.execute(
                    "DELETE FROM public.condition_notes WHERE condition_id = %s::uuid",
                    (condition_id,),
                )
                if note_ids:
                    cur.execute(
                        "DELETE FROM public.notes WHERE id = ANY(%s::uuid[])", (note_ids,)
                    )
                cur.execute(
                    "DELETE FROM public.conditions WHERE id = %s::uuid", (condition_id,)
                )
                _insert_event(
                    cur,
                    "condition.deleted",
                    "condition",
                    condition_id,
                    f"Condition deleted: {name}",
                    {"name": name, "note_ids": note_ids},
                )

    # -------------------------------------------------------- dependency CRUD
    def add_precondition(
        self, target_kind: NodeKind, target_id: str, condition_id: str
    ) -> bool:
        if target_kind is NodeKind.CONDITION:
            raise ValidationError("Conditions cannot be dependency targets.")
        target_id = as_uuid(target_id, f"{target_kind.value} id")
        condition_id = as_uuid(condition_id, "condition id")
        table = (
            "task_preconditions"
            if target_kind is NodeKind.TASK
            else "project_preconditions"
        )
        key = "task_id" if target_kind is NodeKind.TASK else "project_id"
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO public.{table} ({key}, condition_id)
                    SELECT %s::uuid, %s::uuid
                     WHERE NOT EXISTS (
                        SELECT 1 FROM public.{table}
                         WHERE {key} = %s::uuid AND condition_id = %s::uuid
                     )
                    """,
                    (target_id, condition_id, target_id, condition_id),
                )
                inserted = cur.rowcount > 0
                if inserted:
                    cur.execute(
                        f"SELECT display_name FROM public.conditions WHERE id = %s::uuid",
                        (condition_id,),
                    )
                    condition_name = (cur.fetchone() or ["condition"])[0]
                    cur.execute(
                        f"SELECT name FROM public.{'tasks' if target_kind is NodeKind.TASK else 'projects'} "
                        "WHERE id = %s::uuid",
                        (target_id,),
                    )
                    target_name = (cur.fetchone() or ["entity"])[0]
                    _insert_event(
                        cur,
                        "dependency.added",
                        target_kind.value,
                        target_id,
                        f"Precondition attached: {condition_name} → {target_name}",
                        {
                            "dependency_type": "precondition",
                            "source": NodeRef(NodeKind.CONDITION, condition_id).key,
                            "target": NodeRef(target_kind, target_id).key,
                        },
                    )
        return inserted

    def add_prerequisite(
        self, target_kind: NodeKind, target_id: str, source: NodeRef
    ) -> bool:
        if target_kind is NodeKind.CONDITION or source.kind is NodeKind.CONDITION:
            raise ValidationError("Prerequisites connect Tasks and Projects only.")
        target_id = as_uuid(target_id, f"{target_kind.value} id")
        source_id = as_uuid(source.id, f"source {source.kind.value} id")
        table = (
            "task_prerequisites"
            if target_kind is NodeKind.TASK
            else "project_prerequisites"
        )
        key = "task_id" if target_kind is NodeKind.TASK else "project_id"
        source_task = source_id if source.kind is NodeKind.TASK else None
        source_project = source_id if source.kind is NodeKind.PROJECT else None
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO public.{table}
                        ({key}, source_task_id, source_project_id)
                    SELECT %s::uuid, %s::uuid, %s::uuid
                     WHERE NOT EXISTS (
                        SELECT 1 FROM public.{table}
                         WHERE {key} = %s::uuid
                           AND source_task_id IS NOT DISTINCT FROM %s::uuid
                           AND source_project_id IS NOT DISTINCT FROM %s::uuid
                     )
                    """,
                    (
                        target_id,
                        source_task,
                        source_project,
                        target_id,
                        source_task,
                        source_project,
                    ),
                )
                inserted = cur.rowcount > 0
                if inserted:
                    cur.execute(
                        f"SELECT name FROM public.{'tasks' if source.kind is NodeKind.TASK else 'projects'} "
                        "WHERE id = %s::uuid",
                        (source_id,),
                    )
                    source_name = (cur.fetchone() or ["source"])[0]
                    cur.execute(
                        f"SELECT name FROM public.{'tasks' if target_kind is NodeKind.TASK else 'projects'} "
                        "WHERE id = %s::uuid",
                        (target_id,),
                    )
                    target_name = (cur.fetchone() or ["entity"])[0]
                    _insert_event(
                        cur,
                        "dependency.added",
                        target_kind.value,
                        target_id,
                        f"Prerequisite attached: {source_name} → {target_name}",
                        {
                            "dependency_type": "prerequisite",
                            "source": source.key,
                            "target": NodeRef(target_kind, target_id).key,
                        },
                    )
        return inserted

    def remove_precondition(
        self, target_kind: NodeKind, target_id: str, condition_id: str
    ) -> bool:
        target_id = as_uuid(target_id, f"{target_kind.value} id")
        condition_id = as_uuid(condition_id, "condition id")
        table = (
            "task_preconditions"
            if target_kind is NodeKind.TASK
            else "project_preconditions"
        )
        key = "task_id" if target_kind is NodeKind.TASK else "project_id"
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT display_name FROM public.conditions WHERE id = %s::uuid",
                    (condition_id,),
                )
                condition_name = (cur.fetchone() or ["condition"])[0]
                cur.execute(
                    f"SELECT name FROM public.{'tasks' if target_kind is NodeKind.TASK else 'projects'} "
                    "WHERE id = %s::uuid",
                    (target_id,),
                )
                target_name = (cur.fetchone() or ["entity"])[0]
                cur.execute(
                    f"DELETE FROM public.{table} WHERE {key} = %s::uuid "
                    "AND condition_id = %s::uuid",
                    (target_id, condition_id),
                )
                removed = cur.rowcount > 0
                if removed:
                    _insert_event(
                        cur,
                        "dependency.removed",
                        target_kind.value,
                        target_id,
                        f"Precondition detached: {condition_name} → {target_name}",
                        {
                            "dependency_type": "precondition",
                            "source": NodeRef(NodeKind.CONDITION, condition_id).key,
                            "target": NodeRef(target_kind, target_id).key,
                        },
                    )
        return removed

    def remove_prerequisite(
        self, target_kind: NodeKind, target_id: str, source: NodeRef
    ) -> bool:
        target_id = as_uuid(target_id, f"{target_kind.value} id")
        source_id = as_uuid(source.id, f"source {source.kind.value} id")
        table = (
            "task_prerequisites"
            if target_kind is NodeKind.TASK
            else "project_prerequisites"
        )
        key = "task_id" if target_kind is NodeKind.TASK else "project_id"
        source_task = source_id if source.kind is NodeKind.TASK else None
        source_project = source_id if source.kind is NodeKind.PROJECT else None
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT name FROM public.{'tasks' if source.kind is NodeKind.TASK else 'projects'} "
                    "WHERE id = %s::uuid",
                    (source_id,),
                )
                source_name = (cur.fetchone() or ["source"])[0]
                cur.execute(
                    f"SELECT name FROM public.{'tasks' if target_kind is NodeKind.TASK else 'projects'} "
                    "WHERE id = %s::uuid",
                    (target_id,),
                )
                target_name = (cur.fetchone() or ["entity"])[0]
                cur.execute(
                    f"""
                    DELETE FROM public.{table}
                     WHERE {key} = %s::uuid
                       AND source_task_id IS NOT DISTINCT FROM %s::uuid
                       AND source_project_id IS NOT DISTINCT FROM %s::uuid
                    """,
                    (target_id, source_task, source_project),
                )
                removed = cur.rowcount > 0
                if removed:
                    _insert_event(
                        cur,
                        "dependency.removed",
                        target_kind.value,
                        target_id,
                        f"Prerequisite detached: {source_name} → {target_name}",
                        {
                            "dependency_type": "prerequisite",
                            "source": source.key,
                            "target": NodeRef(target_kind, target_id).key,
                        },
                    )
        return removed

    # -------------------------------------------------------------- recurrence
    def save_rule(
        self,
        task_id: str,
        *,
        frequency: RecurrenceFrequency,
        interval: int,
        by_day: str | None,
        by_month_day: int | None,
        occurrence_count: int | None,
        until_date: datetime | None,
        reset_hour: int,
        reset_minute: int,
        end_hour: int = 19,
        end_minute: int = 0,
    ) -> None:
        task_id = as_uuid(task_id, "task id")
        if interval < 1:
            raise ValidationError("Recurrence interval must be at least 1.")
        if by_month_day is not None and (by_month_day == 0 or not -31 <= by_month_day <= 31):
            raise ValidationError("Month day must be between -31 and 31 and cannot be 0.")
        if occurrence_count is not None and occurrence_count < 1:
            raise ValidationError("Occurrence count must be at least 1.")
        if occurrence_count is not None and until_date is not None:
            raise ValidationError(
                "A recurrence rule cannot define both an occurrence count and an "
                "until date."
            )
        if (end_hour, end_minute) == (reset_hour, reset_minute):
            raise ValidationError("The recurrence window end time must differ from its start time.")
        until = _naive_local(until_date)
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM public.tasks WHERE id = %s::uuid", (task_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The task no longer exists.")
                cur.execute(
                    """
                    INSERT INTO public.task_recurrence_rules
                        (task_id, frequency, "interval", by_day, by_month_day,
                         occurrence_count, until_date, reset_hour, reset_minute,
                         end_hour, end_minute)
                    VALUES (%s::uuid, %s::public.recurrence_frequency, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (task_id) DO UPDATE SET
                        frequency = EXCLUDED.frequency,
                        "interval" = EXCLUDED."interval",
                        by_day = EXCLUDED.by_day,
                        by_month_day = EXCLUDED.by_month_day,
                        occurrence_count = EXCLUDED.occurrence_count,
                        until_date = EXCLUDED.until_date,
                        reset_hour = EXCLUDED.reset_hour,
                        reset_minute = EXCLUDED.reset_minute,
                        end_hour = EXCLUDED.end_hour,
                        end_minute = EXCLUDED.end_minute,
                        last_reset_window = NULL
                    """,
                    (
                        task_id,
                        frequency.value,
                        int(interval),
                        by_day or None,
                        by_month_day,
                        occurrence_count,
                        until,
                        int(reset_hour),
                        int(reset_minute),
                        int(end_hour),
                        int(end_minute),
                    ),
                )

                from .logic import latest_occurrence, next_occurrence

                now = datetime.now()
                temp_rule = RecurrenceRule(
                    task_id=task_id,
                    frequency=frequency,
                    interval=int(interval),
                    by_day=by_day or None,
                    by_month_day=by_month_day,
                    occurrence_count=occurrence_count,
                    until_date=until,
                    reset_hour=int(reset_hour),
                    reset_minute=int(reset_minute),
                    end_hour=int(end_hour),
                    end_minute=int(end_minute),
                    created_at=now.replace(hour=0, minute=0, second=0, microsecond=0),
                )
                occ = latest_occurrence(temp_rule, now)
                if occ is not None:
                    occ_end = occ.replace(
                        hour=int(end_hour),
                        minute=int(end_minute),
                        second=0,
                        microsecond=0,
                    )
                    if (int(end_hour), int(end_minute)) < (int(reset_hour), int(reset_minute)):
                        occ_end += timedelta(days=1)
                    if occ_end < now:
                        occ = next_occurrence(temp_rule, now)
                else:
                    occ = next_occurrence(temp_rule, now)

                if occ is not None:
                    win_start = occ
                    win_end = occ.replace(
                        hour=int(end_hour),
                        minute=int(end_minute),
                        second=0,
                        microsecond=0,
                    )
                    if (int(end_hour), int(end_minute)) < (int(reset_hour), int(reset_minute)):
                        win_end += timedelta(days=1)
                    cur.execute(
                        "UPDATE public.tasks "
                        "SET is_recurring = true, scheduled_start = %s, scheduled_end = %s "
                        "WHERE id = %s::uuid",
                        (win_start, win_end, task_id),
                    )
                else:
                    cur.execute(
                        "UPDATE public.tasks SET is_recurring = true WHERE id = %s::uuid",
                        (task_id,),
                    )
                _insert_event(
                    cur,
                    "task.recurrence_saved",
                    "task",
                    task_id,
                    f"Recurrence rule saved: {row[0]}",
                    {
                        "frequency": frequency.value,
                        "interval": int(interval),
                        "by_day": by_day,
                        "window": f"{reset_hour:02d}:{reset_minute:02d}-{end_hour:02d}:{end_minute:02d}",
                    },
                )

    def delete_rule(self, task_id: str) -> None:
        task_id = as_uuid(task_id, "task id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name FROM public.tasks WHERE id = %s::uuid", (task_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The task no longer exists.")
                cur.execute(
                    "DELETE FROM public.task_recurrence_rules WHERE task_id = %s::uuid", (task_id,)
                )
                cur.execute(
                    "UPDATE public.tasks "
                    "SET is_recurring = false, scheduled_start = NULL, scheduled_end = NULL "
                    "WHERE id = %s::uuid",
                    (task_id,)
                )

                _insert_event(
                    cur,
                    "task.recurrence_removed",
                    "task",
                    task_id,
                    f"Recurrence rule removed: {row[0]}",
                )

    def recurring_tasks(self) -> list[tuple[Task, RecurrenceRule]]:
        rows = self.db.fetch_all(
            """
            SELECT t.*, r.frequency, r."interval" AS rule_interval, r.by_day,
                   r.by_month_day, r.occurrence_count, r.until_date,
                   r.reset_hour, r.reset_minute, r.end_hour, r.end_minute,
                   r.last_reset_window, r.created_at AS rule_created_at
              FROM public.tasks t
              JOIN public.task_recurrence_rules r ON r.task_id = t.id
             WHERE t.is_recurring = true
            """
        )
        result: list[tuple[Task, RecurrenceRule]] = []
        for row in rows:
            task = _task_from_row(row)
            rule = RecurrenceRule(
                task_id=task.id,
                frequency=RecurrenceFrequency(row["frequency"]),
                interval=int(row["rule_interval"]),
                by_day=row["by_day"],
                by_month_day=row["by_month_day"],
                occurrence_count=row["occurrence_count"],
                until_date=row["until_date"],
                reset_hour=int(row["reset_hour"]),
                reset_minute=int(row["reset_minute"]),
                end_hour=int(row["end_hour"]),
                end_minute=int(row["end_minute"]),
                last_reset_window=row["last_reset_window"],
                created_at=row["rule_created_at"],
            )
            result.append((task, rule))
        return result

    def mark_reset_window(self, task_id: str, occurrence: datetime) -> None:
        task_id = as_uuid(task_id, "task id")
        self.db.execute(
            "UPDATE public.task_recurrence_rules SET last_reset_window = %s "
            "WHERE task_id = %s::uuid",
            (occurrence, task_id),
        )

    def fire_recurrence(self, task_id: str, occurrence: datetime) -> tuple[bool, str]:
        """Reset a recurring task into its active window, honouring database barriers."""
        task_id = as_uuid(task_id, "task id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.name, t.current_state, r.end_hour, r.end_minute
                      FROM public.tasks t
                      JOIN public.task_recurrence_rules r ON r.task_id = t.id
                     WHERE t.id = %s::uuid
                    """,
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise NotFoundError("The task no longer exists.")
                name, state, end_hour, end_minute = row[0], row[1], int(row[2]), int(row[3])
                window_start = occurrence
                window_end = occurrence.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
                if (end_hour, end_minute) < (occurrence.hour, occurrence.minute):
                    window_end += timedelta(days=1)

                cur.execute(
                    "UPDATE public.task_recurrence_rules SET last_reset_window = %s "
                    "WHERE task_id = %s::uuid",
                    (occurrence, task_id),
                )
                cur.execute(
                    "UPDATE public.tasks SET scheduled_start = %s, scheduled_end = %s "
                    "WHERE id = %s::uuid",
                    (window_start, window_end, task_id),
                )
                if state not in ("completed", "unavailable"):
                    return False, "already active"
                cur.execute("SAVEPOINT recurrence_reset")
                try:
                    cur.execute(
                        "UPDATE public.tasks SET current_state = 'available'::public.task_state "
                        "WHERE id = %s::uuid AND current_state IN ('completed'::public.task_state, 'unavailable'::public.task_state)",
                        (task_id,),
                    )
                except pg_errors.CheckViolation as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT recurrence_reset")
                    _insert_event(
                        cur,
                        "recurrence.blocked",
                        "task",
                        task_id,
                        f"Recurrence reset blocked for {name}: dependencies unresolved",
                        {
                            "window": occurrence.isoformat(),
                            "error": str(exc).splitlines()[0][:200],
                        },
                    )
                    return False, "blocked by dependencies"
                _insert_event(
                    cur,
                    "recurrence.reset",
                    "task",
                    task_id,
                    f"Recurring task reset to available: {name}",
                    {"window": occurrence.isoformat(), "window_end": window_end.isoformat()},
                )
                return True, "reset"

    # --------------------------------------------------------------- assistant
    def count_threads(self) -> int:
        row = self.db.fetch_one("SELECT count(*) AS n FROM public.assistant_threads")
        return int(row["n"]) if row else 0

    def list_threads(self) -> list[ChatThread]:
        rows = self.db.fetch_all(
            """
            SELECT t.*, (SELECT count(*) FROM public.assistant_messages m
                          WHERE m.thread_id = t.id) AS message_count
              FROM public.assistant_threads t
             ORDER BY t.updated_at DESC, t.created_at DESC
             LIMIT 256
            """
        )
        return [
            ChatThread(
                id=str(row["id"]),
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=int(row["message_count"]),
            )
            for row in rows
        ]

    def create_thread(self, title: str = "New conversation") -> str:
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.assistant_threads")
                count = int(cur.fetchone()[0])
                dropped: list[str] = []
                while count >= MAX_ASSISTANT_THREADS:
                    cur.execute(
                        "DELETE FROM public.assistant_threads WHERE id = ("
                        "SELECT id FROM public.assistant_threads "
                        "ORDER BY created_at ASC, id ASC LIMIT 1) RETURNING id"
                    )
                    row = cur.fetchone()
                    if not row:
                        break
                    dropped.append(str(row[0]))
                    count -= 1
                cur.execute(
                    "INSERT INTO public.assistant_threads (title) VALUES (%s) RETURNING id",
                    (title[:160],),
                )
                thread_id = str(cur.fetchone()[0])
                _insert_event(
                    cur,
                    "assistant.thread_created",
                    "assistant_thread",
                    thread_id,
                    "Assistant conversation created",
                    {"dropped": dropped},
                )
        return thread_id

    def load_thread(self, thread_id: str) -> tuple[ChatThread, list[ChatMessage]]:
        thread_id = as_uuid(thread_id, "thread id")
        row = self.db.fetch_one(
            "SELECT * FROM public.assistant_threads WHERE id = %s::uuid", (thread_id,)
        )
        if not row:
            raise NotFoundError("The conversation no longer exists.")
        messages = self.db.fetch_all(
            "SELECT * FROM public.assistant_messages WHERE thread_id = %s::uuid "
            "ORDER BY id ASC",
            (thread_id,),
        )
        thread = ChatThread(
            id=str(row["id"]),
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=len(messages),
        )
        return thread, [
            ChatMessage(
                id=str(item["id"]),
                thread_id=str(item["thread_id"]),
                role=item["role"],
                content=item["content"],
                metadata=item["metadata"] or {},
                created_at=item["created_at"],
            )
            for item in messages
        ]

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        thread_id = as_uuid(thread_id, "thread id")
        if role not in ("user", "assistant", "system"):
            raise ValidationError("Invalid assistant message role.")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.assistant_messages "
                    "(thread_id, role, content, metadata) "
                    "VALUES (%s::uuid, %s, %s, %s) RETURNING id",
                    (thread_id, role, content, Json(metadata or {})),
                )
                message_id = str(cur.fetchone()[0])
                cur.execute(
                    "UPDATE public.assistant_threads SET updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = %s::uuid",
                    (thread_id,),
                )
                if role == "user":
                    cur.execute(
                        "SELECT title, (SELECT count(*) FROM public.assistant_messages "
                        "WHERE thread_id = %s::uuid AND role = 'user') "
                        "FROM public.assistant_threads WHERE id = %s::uuid",
                        (thread_id, thread_id),
                    )
                    title_row = cur.fetchone()
                    if title_row and int(title_row[1]) == 1 and title_row[0] in (
                        "New conversation",
                        "",
                    ):
                        title = " ".join(content.split())[:60] or "New conversation"
                        cur.execute(
                            "UPDATE public.assistant_threads SET title = %s "
                            "WHERE id = %s::uuid",
                            (title, thread_id),
                        )
        return message_id

    def delete_thread(self, thread_id: str) -> None:
        thread_id = as_uuid(thread_id, "thread id")
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.assistant_threads WHERE id = %s::uuid",
                    (thread_id,),
                )
                if cur.rowcount == 0:
                    raise NotFoundError("The conversation no longer exists.")
                _insert_event(
                    cur,
                    "assistant.thread_deleted",
                    "assistant_thread",
                    thread_id,
                    "Assistant conversation deleted",
                )

    def rename_thread(self, thread_id: str, title: str) -> None:
        thread_id = as_uuid(thread_id, "thread id")
        title = clean_name(title, label="Conversation title")[:160]
        if self.db.execute(
            "UPDATE public.assistant_threads SET title = %s WHERE id = %s::uuid",
            (title, thread_id),
        ) == 0:
            raise NotFoundError("The conversation no longer exists.")

    # ---------------------------------------------------------- custom queries
    def list_custom_queries(self) -> list[CustomQuery]:
        rows = self.db.fetch_all(
            "SELECT * FROM public.custom_queries "
            "ORDER BY favorite DESC, sort_index ASC, created_at ASC"
        )
        return [
            CustomQuery(
                id=str(row["id"]),
                label=row["label"],
                prompt=row["prompt"],
                favorite=bool(row["favorite"]),
                sort_index=int(row["sort_index"]),
            )
            for row in rows
        ]

    def create_custom_query(self, label: str, prompt: str, *, favorite: bool = False) -> str:
        label = clean_name(label, label="Query label")[:80]
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValidationError("The backend instruction payload cannot be empty.")
        row = self.db.fetch_one(
            "SELECT COALESCE(max(sort_index), -1) + 1 AS next FROM public.custom_queries"
        )
        next_index = int(row["next"]) if row else 0
        result = self.db.fetch_one(
            "INSERT INTO public.custom_queries (label, prompt, favorite, sort_index) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (label, prompt, bool(favorite), next_index),
        )
        assert result is not None
        return str(result["id"])

    def update_custom_query(
        self,
        query_id: str,
        *,
        label: str,
        prompt: str,
        favorite: bool | None = None,
    ) -> None:
        query_id = as_uuid(query_id, "query id")
        label = clean_name(label, label="Query label")[:80]
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValidationError("The backend instruction payload cannot be empty.")
        if favorite is None:
            count = self.db.execute(
                "UPDATE public.custom_queries SET label = %s, prompt = %s "
                "WHERE id = %s::uuid",
                (label, prompt, query_id),
            )
        else:
            count = self.db.execute(
                "UPDATE public.custom_queries SET label = %s, prompt = %s, favorite = %s "
                "WHERE id = %s::uuid",
                (label, prompt, bool(favorite), query_id),
            )
        if count == 0:
            raise NotFoundError("The custom query no longer exists.")

    def delete_custom_query(self, query_id: str) -> None:
        query_id = as_uuid(query_id, "query id")
        if self.db.execute(
            "DELETE FROM public.custom_queries WHERE id = %s::uuid", (query_id,)
        ) == 0:
            raise NotFoundError("The custom query no longer exists.")

    def set_custom_query_favorite(self, query_id: str, favorite: bool) -> None:
        query_id = as_uuid(query_id, "query id")
        if self.db.execute(
            "UPDATE public.custom_queries SET favorite = %s WHERE id = %s::uuid",
            (bool(favorite), query_id),
        ) == 0:
            raise NotFoundError("The custom query no longer exists.")

    def reorder_custom_queries(self, ordered_ids: list[str]) -> None:
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                for index, query_id in enumerate(ordered_ids):
                    cur.execute(
                        "UPDATE public.custom_queries SET sort_index = %s "
                        "WHERE id = %s::uuid",
                        (index, as_uuid(query_id, "query id")),
                    )

    # --------------------------------------------------------------- calendar
    def calendar_account(self) -> dict[str, Any] | None:
        return self.db.fetch_one(
            "SELECT * FROM public.calendar_accounts ORDER BY connected_at DESC LIMIT 1"
        )

    def save_calendar_account(
        self,
        account_id: str,
        email: str,
        calendar_id: str,
        *,
        sync_token: str | None = None,
        status: str = "never",
        error: str | None = None,
        last_sync_at: datetime | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO public.calendar_accounts
                (id, email, calendar_id, sync_token, sync_status, last_error, last_sync_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                email = EXCLUDED.email,
                calendar_id = EXCLUDED.calendar_id,
                sync_token = COALESCE(EXCLUDED.sync_token, public.calendar_accounts.sync_token),
                sync_status = EXCLUDED.sync_status,
                last_error = EXCLUDED.last_error,
                last_sync_at = COALESCE(EXCLUDED.last_sync_at, public.calendar_accounts.last_sync_at)
            """,
            (
                account_id,
                email,
                calendar_id,
                sync_token,
                status,
                error,
                _naive_local(last_sync_at),
            ),
        )

    def calendar_sync_state(self) -> SyncState:
        row = self.calendar_account()
        if not row:
            return SyncState(status="never", connected=False)
        status = row["sync_status"] or "never"
        return SyncState(
            status=status,
            last_sync_at=row["last_sync_at"],
            message=row["last_error"] or "",
            account_email=row["email"] or "",
            connected=True,
        )

    def upsert_calendar_events(
        self, account_id: str, calendar_id: str, events: Iterable[dict[str, Any]]
    ) -> int:
        count = 0
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                for event in events:
                    event_id = str(event["id"])
                    if event.get("status") == "cancelled":
                        cur.execute(
                            "DELETE FROM public.calendar_events WHERE id = %s", (event_id,)
                        )
                        continue
                    cur.execute(
                        """
                        INSERT INTO public.calendar_events
                            (id, account_id, calendar_id, summary, description, location,
                             start_at, end_at, all_day, status, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (id) DO UPDATE SET
                            account_id = EXCLUDED.account_id,
                            calendar_id = EXCLUDED.calendar_id,
                            summary = EXCLUDED.summary,
                            description = EXCLUDED.description,
                            location = EXCLUDED.location,
                            start_at = EXCLUDED.start_at,
                            end_at = EXCLUDED.end_at,
                            all_day = EXCLUDED.all_day,
                            status = EXCLUDED.status,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            event_id,
                            account_id,
                            calendar_id,
                            event.get("summary") or "(untitled calendar event)",
                            event.get("description") or "",
                            event.get("location") or "",
                            event["start_at"],
                            event["end_at"],
                            bool(event.get("all_day")),
                            event.get("status") or "confirmed",
                        ),
                    )
                    count += 1
        return count

    def replace_calendar_events(
        self, account_id: str, calendar_id: str, events: Iterable[dict[str, Any]]
    ) -> int:
        """Atomically replace the local snapshot so a failed sync cannot wipe it."""
        count = 0
        with self.db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.calendar_events WHERE account_id = %s",
                    (account_id,),
                )
                for event in events:
                    event_id = str(event["id"])
                    if event.get("status") == "cancelled":
                        continue
                    cur.execute(
                        """
                        INSERT INTO public.calendar_events
                            (id, account_id, calendar_id, summary, description, location,
                             start_at, end_at, all_day, status, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        """,
                        (
                            event_id,
                            account_id,
                            calendar_id,
                            event.get("summary") or "(untitled calendar event)",
                            event.get("description") or "",
                            event.get("location") or "",
                            event["start_at"],
                            event["end_at"],
                            bool(event.get("all_day")),
                            event.get("status") or "confirmed",
                        ),
                    )
                    count += 1
        return count

    def calendar_events_between(
        self, start: datetime, end: datetime
    ) -> list[CalendarItem]:
        rows = self.db.fetch_all(
            """
            SELECT * FROM public.calendar_events
             WHERE start_at < %s AND end_at > %s
             ORDER BY start_at
            """,
            (_naive_local(end), _naive_local(start)),
        )
        return [
            CalendarItem(
                kind="external",
                id=row["id"],
                title=row["summary"] or "(untitled calendar event)",
                start=row["start_at"],
                end=row["end_at"],
                all_day=bool(row["all_day"]),
                state=row["status"] or "confirmed",
                read_only=True,
                detail=row["location"] or row["description"] or "",
            )
            for row in rows
        ]

    def scheduled_tasks_between(self, start: datetime, end: datetime) -> list[Task]:
        rows = self.db.fetch_all(
            """
            SELECT * FROM public.tasks
             WHERE scheduled_start IS NOT NULL AND scheduled_end IS NOT NULL
               AND scheduled_start < %s AND scheduled_end > %s
               AND current_state NOT IN ('completed'::public.task_state,
                                         'deferred'::public.task_state)
             ORDER BY scheduled_start
            """,
            (_naive_local(end), _naive_local(start)),
        )
        return [_task_from_row(row) for row in rows]

    def schedule_conflicts(
        self,
        start: datetime,
        end: datetime,
        *,
        exclude_task_id: str | None = None,
    ) -> list[Task]:
        start, end = _schedule(start, end)
        assert start is not None and end is not None
        exclude = as_uuid(exclude_task_id, "task id") if exclude_task_id else None
        rows = self.db.fetch_all(
            """
            SELECT * FROM public.tasks
             WHERE scheduled_start IS NOT NULL AND scheduled_end IS NOT NULL
               AND scheduled_start < %s AND scheduled_end > %s
               AND (%s::uuid IS NULL OR id <> %s::uuid)
               AND current_state NOT IN ('completed'::public.task_state,
                                         'deferred'::public.task_state)
             ORDER BY scheduled_start
            """,
            (end, start, exclude, exclude),
        )
        return [_task_from_row(row) for row in rows]

    # ------------------------------------------------------------------- RAG
    def schema_architecture(self) -> str:
        rows = self.db.fetch_all(
            """
            SELECT c.table_name, c.column_name, c.data_type, c.udt_name,
                   c.is_nullable, c.column_default
              FROM information_schema.columns c
             WHERE c.table_schema = 'public'
               AND c.table_name = ANY(%s)
             ORDER BY c.table_name, c.ordinal_position
            """,
            (list(_public_tables()),),
        )
        enum_rows = self.db.fetch_all(
            """
            SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
              FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
             WHERE t.typname IN ('task_state','project_state','recurrence_frequency')
             GROUP BY t.typname ORDER BY t.typname
            """
        )
        fks = self.db.fetch_all(
            """
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS foreign_table, ccu.column_name AS foreign_column,
                   rc.delete_rule
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
              JOIN information_schema.referential_constraints rc
                ON rc.constraint_name = tc.constraint_name AND rc.constraint_schema = tc.table_schema
             WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
             ORDER BY tc.table_name, kcu.column_name
            """
        )
        lines: list[str] = ["SCHEMA public"]
        current = None
        for row in rows:
            if row["table_name"] != current:
                current = row["table_name"]
                lines.append(f"\nTABLE public.{current}")
            type_name = row["udt_name"] if row["data_type"] == "USER-DEFINED" else row["data_type"]
            null_text = "NULL" if row["is_nullable"] == "YES" else "NOT NULL"
            default = f" DEFAULT {row['column_default']}" if row["column_default"] else ""
            lines.append(f"  {row['column_name']} {type_name} {null_text}{default}")
        lines.append("\nENUMS")
        for row in enum_rows:
            lines.append(f"  {row['typname']} = {list(row['labels'])}")
        lines.append("\nFOREIGN KEYS")
        for row in fks:
            lines.append(
                f"  {row['table_name']}.{row['column_name']} -> "
                f"{row['foreign_table']}.{row['foreign_column']} ON DELETE {row['delete_rule']}"
            )
        lines.append(
            "\nDEPENDENCY MODEL\n"
            "  Preconditions: condition.evaluation_state must be TRUE (NULL/UNKNOWN blocks).\n"
            "  Prerequisites: source task must be completed; source project must be resolved.\n"
            "  Tasks: current_state ∈ {available, unavailable, completed, deferred}.\n"
            "  Projects: current_state ∈ {active, stalled, shelved, resolved}.\n"
            "  Availability and resolution are conjunctive (AND) and derived automatically.\n"
            "  projects.resolution_conditions_unknown=true blocks automated resolution.\n"
            "  events is append-only; no UPDATE or DELETE is permitted."
        )
        return "\n".join(lines)

    def live_state_summary(self, snapshot: WorkspaceSnapshot) -> str:
        task_states = {state.value: 0 for state in TaskState}
        project_states = {state.value: 0 for state in ProjectState}
        for task in snapshot.tasks.values():
            task_states[task.current_state.value] += 1
        for project in snapshot.projects.values():
            project_states[project.current_state.value] += 1
        values = {"true": 0, "false": 0, "unknown": 0}
        for condition in snapshot.conditions.values():
            if condition.evaluation_state is True:
                values["true"] += 1
            elif condition.evaluation_state is False:
                values["false"] += 1
            else:
                values["unknown"] += 1
        return (
            f"LIVE DATABASE STATE @ {datetime.now().isoformat(timespec='seconds')}\n"
            f"tasks by state: {task_states}\n"
            f"projects by state: {project_states}\n"
            f"condition values: {values}\n"
            f"scheduled tasks: {sum(1 for task in snapshot.tasks.values() if task.has_schedule)}\n"
            f"task preconditions: {sum(len(v) for v in snapshot.task_preconditions.values())}\n"
            f"task prerequisites: {sum(len(v) for v in snapshot.task_prerequisites.values())}\n"
            f"project preconditions: {sum(len(v) for v in snapshot.project_preconditions.values())}\n"
            f"project prerequisites: {sum(len(v) for v in snapshot.project_prerequisites.values())}\n"
            f"undefined-requirement projects: "
            f"{sum(1 for p in snapshot.projects.values() if p.resolution_conditions_unknown)}"
        )

    def sample_registry(self, snapshot: WorkspaceSnapshot, limit: int = 14) -> str:
        lines = ["SAMPLE REGISTRY ROWS (UUIDs are queryable; names are untrusted text)"]
        lines.append("\nTASKS")
        for task in list(snapshot.tasks.values())[:limit]:
            schedule = ""
            if task.scheduled_start and task.scheduled_end:
                schedule = (
                    f" | {task.scheduled_start.isoformat(sep=' ')}"
                    f" → {task.scheduled_end.isoformat(sep=' ')}"
                )
            lines.append(
                f"  {task.id} | {task.name} | {task.current_state.value} | "
                f"recurring={task.is_recurring}{schedule}"
            )
        lines.append("\nPROJECTS")
        for project in list(snapshot.projects.values())[:limit]:
            lines.append(
                f"  {project.id} | {project.name} | {project.current_state.value} | "
                f"requirements_undefined={project.resolution_conditions_unknown}"
            )
        lines.append("\nCONDITIONS")
        for condition in list(snapshot.conditions.values())[:limit]:
            lines.append(
                f"  {condition.id} | {condition.display_name} | "
                f"value={condition.evaluation_state}"
            )
        lines.append("\nLINK TABLES")
        for task_id, condition_ids in list(snapshot.task_preconditions.items())[:limit]:
            for condition_id in condition_ids:
                lines.append(f"  task_preconditions: task={task_id} condition={condition_id}")
        for task_id, sources in list(snapshot.task_prerequisites.items())[:limit]:
            for source in sources:
                lines.append(f"  task_prerequisites: task={task_id} source={source.key}")
        for project_id, condition_ids in list(snapshot.project_preconditions.items())[:limit]:
            for condition_id in condition_ids:
                lines.append(
                    f"  project_preconditions: project={project_id} condition={condition_id}"
                )
        for project_id, sources in list(snapshot.project_prerequisites.items())[:limit]:
            for source in sources:
                lines.append(
                    f"  project_prerequisites: project={project_id} source={source.key}"
                )
        return "\n".join(lines)

    # ----------------------------------------------------------------- counts
    def registry_counts(self) -> dict[str, int]:
        row = self.db.fetch_one(
            """
            SELECT
              (SELECT count(*) FROM public.tasks) AS tasks,
              (SELECT count(*) FROM public.projects) AS projects,
              (SELECT count(*) FROM public.conditions) AS conditions,
              (SELECT count(*) FROM public.task_preconditions)
                + (SELECT count(*) FROM public.project_preconditions) AS preconditions,
              (SELECT count(*) FROM public.task_prerequisites)
                + (SELECT count(*) FROM public.project_prerequisites) AS prerequisites,
              (SELECT count(*) FROM public.notes) AS notes,
              (SELECT count(*) FROM public.events) AS events,
              (SELECT count(*) FROM public.assistant_threads) AS threads
            """
        )
        return {key: int(value) for key, value in (row or {}).items()}

    def schema_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in (
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
        ):
            row = self.db.fetch_one(f"SELECT count(*) AS n FROM public.{table}")
            counts[table] = int(row["n"]) if row else 0
        return counts

def _truth(value: bool | None) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return "UNKNOWN"

def _public_tables() -> tuple[str, ...]:
    from .models import PUBLIC_TABLES

    return PUBLIC_TABLES
