"""Unit tests for the explicit dependency model."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from efos_app.errors import ConflictError, ValidationError
from efos_app.logic import (
    availability_is_open,
    availability_state_text,
    availability_summary,
    evaluate_workspace,
    find_cycle_with_edge,
    latest_occurrence,
    normalize_availability_windows,
    priority_narrative,
    recommend,
    reconcile_plan,
    recurrence_due,
    validate_connection,
    validate_project_transition,
    validate_task_transition,
)
from efos_app.models import (
    AvailabilityWindow,
    Condition,
    NodeKind,
    NodeRef,
    Project,
    ProjectState,
    RecurrenceFrequency,
    RecurrenceRule,
    Task,
    TaskState,
    WorkspaceSnapshot,
)


def test_conjunctive_preconditions_and_prerequisites():
    snapshot = WorkspaceSnapshot(
        tasks={
            "t": Task("t", "Target"),
            "src": Task("src", "Source", current_state=TaskState.COMPLETED),
        },
        conditions={
            "a": Condition("a", "A", evaluation_state=True),
            "b": Condition("b", "B", evaluation_state=False),
        },
    )
    snapshot.task_preconditions = {"t": {"a", "b"}}
    snapshot.task_prerequisites = {"t": {NodeRef(NodeKind.TASK, "src")}}
    evaluation = evaluate_workspace(snapshot)
    assert evaluation.task_status("t").ready is False
    assert len(evaluation.task_status("t").blockers) == 1

    snapshot.conditions["b"].evaluation_state = None
    assert evaluate_workspace(snapshot).task_status("t").ready is False
    snapshot.conditions["b"].evaluation_state = True
    assert evaluate_workspace(snapshot).task_status("t").ready is True
    snapshot.tasks["src"].current_state = TaskState.READY
    evaluation = evaluate_workspace(snapshot)
    assert evaluation.task_status("t").ready is False
    assert any(
        "Prerequisite" in blocker and "ready" in blocker
        for blocker in evaluation.task_status("t").blockers
    )


def test_no_dependencies_satisfied_by_default():
    snapshot = WorkspaceSnapshot(tasks={"t": Task("t", "Free")})
    evaluation = evaluate_workspace(snapshot)
    assert evaluation.task_status("t").ready is True
    plan = reconcile_plan(snapshot, evaluation)
    assert plan.empty


def test_project_prerequisite_requires_resolved_source():
    snapshot = WorkspaceSnapshot(
        projects={
            "p": Project("p", "Target"),
            "source": Project("source", "Source", current_state=ProjectState.ACTIVE),
        }
    )
    snapshot.project_prerequisites = {"p": {NodeRef(NodeKind.PROJECT, "source")}}
    assert evaluate_workspace(snapshot).project_status("p").ready is False
    snapshot.projects["source"].current_state = ProjectState.RESOLVED
    assert evaluate_workspace(snapshot).project_status("p").ready is True


def test_unknown_project_blocks_automated_resolution():
    condition = Condition("c", "Ready", evaluation_state=True)
    project = Project("p", "Project", resolution_conditions_unknown=True)
    snapshot = WorkspaceSnapshot(
        conditions={"c": condition}, projects={"p": project}
    )
    snapshot.project_preconditions = {"p": {"c"}}
    evaluation = evaluate_workspace(snapshot)
    assert evaluation.project_status("p").ready is True
    plan = reconcile_plan(snapshot, evaluation)
    assert plan.empty  # undefined requirements block automation
    project.resolution_conditions_unknown = False
    plan = reconcile_plan(snapshot, evaluate_workspace(snapshot))
    assert plan.project_targets["p"] is ProjectState.RESOLVED


def test_cycle_prevention_and_flat_connections():
    snapshot = WorkspaceSnapshot(
        tasks={"a": Task("a", "A"), "b": Task("b", "B")},
        projects={"p": Project("p", "P")},
        conditions={"c": Condition("c", "C", evaluation_state=True)},
    )
    assert (
        validate_connection(
            snapshot,
            NodeRef(NodeKind.CONDITION, "c"),
            NodeRef(NodeKind.TASK, "a"),
        ).value
        == "precondition"
    )
    assert (
        validate_connection(
            snapshot,
            NodeRef(NodeKind.TASK, "a"),
            NodeRef(NodeKind.TASK, "b"),
        ).value
        == "prerequisite"
    )
    assert (
        validate_connection(
            snapshot,
            NodeRef(NodeKind.PROJECT, "p"),
            NodeRef(NodeKind.TASK, "b"),
        ).value
        == "prerequisite"
    )
    snapshot.task_prerequisites = {"b": {NodeRef(NodeKind.TASK, "a")}}
    with pytest.raises(ConflictError):
        validate_connection(
            snapshot,
            NodeRef(NodeKind.TASK, "b"),
            NodeRef(NodeKind.TASK, "a"),
        )
    with pytest.raises(ValidationError):
        validate_connection(
            snapshot,
            NodeRef(NodeKind.CONDITION, "c"),
            NodeRef(NodeKind.CONDITION, "c"),
        )
    with pytest.raises(ConflictError):
        validate_connection(
            snapshot,
            NodeRef(NodeKind.TASK, "a"),
            NodeRef(NodeKind.TASK, "a"),
        )


def test_fsm_matrices():
    validate_task_transition(TaskState.READY, TaskState.COMPLETED)
    validate_task_transition(TaskState.READY, TaskState.DEFERRED)
    validate_task_transition(TaskState.DEFERRED, TaskState.READY)
    with pytest.raises(ValidationError):
        validate_task_transition(TaskState.COMPLETED, TaskState.DEFERRED)
    validate_project_transition(ProjectState.ACTIVE, ProjectState.SHELVED)
    validate_project_transition(ProjectState.SHELVED, ProjectState.ACTIVE)
    with pytest.raises(ValidationError):
        validate_project_transition(ProjectState.RESOLVED, ProjectState.RESOLVED)


def test_recommendation_tiers():
    now = datetime(2026, 11, 9, 12, 0)
    snapshot = WorkspaceSnapshot(
        tasks={
            "t1": Task("t1", "Ready one", scheduled_start=now + timedelta(hours=2)),
            "t2": Task("t2", "Ready two"),
        },
    )
    snapshot.task_prerequisites = {"t1": {NodeRef(NodeKind.TASK, "t2")}}
    snapshot.tasks["t2"].current_state = TaskState.READY
    evaluation = evaluate_workspace(snapshot)
    recommendation = recommend(snapshot, evaluation, now)
    assert recommendation is not None and recommendation.tier == 1
    assert recommendation.ref.id in ("t1", "t2")

    stalled = Project("p", "Stalled", current_state=ProjectState.STALLED)
    milestone = Project(
        "m", "Milestone", resolution_conditions_unknown=False
    )
    snapshot = WorkspaceSnapshot(
        conditions={
            "a": Condition("a", "A", evaluation_state=True),
            "b": Condition("b", "B", evaluation_state=True),
            "c": Condition("c", "C", evaluation_state=False),
        },
        projects={"p": stalled, "m": milestone},
    )
    snapshot.project_preconditions = {"m": {"a", "b", "c"}}
    evaluation = evaluate_workspace(snapshot)
    recommendation = recommend(snapshot, evaluation, now)
    assert recommendation is not None and recommendation.tier == 2
    assert "2 of 3" in recommendation.rationale

    snapshot.project_preconditions = {"m": {"c"}}
    evaluation = evaluate_workspace(snapshot)
    recommendation = recommend(snapshot, evaluation, now)
    assert recommendation is not None and recommendation.tier == 3

    snapshot.projects["p"] = Project(
        "p", "Shelved", current_state=ProjectState.SHELVED
    )
    recommendation = recommend(snapshot, evaluate_workspace(snapshot), now)
    assert recommendation is not None and recommendation.tier == 4


def test_priority_analyzer_only_selects_ready_tasks():
    snapshot = WorkspaceSnapshot(
        tasks={
            "ready": Task("ready", "Ready"),
            "locked": Task("locked", "Locked"),
            "done": Task("done", "Done", current_state=TaskState.COMPLETED),
        },
        conditions={"block": Condition("block", "Block", evaluation_state=False)},
    )
    snapshot.task_preconditions = {"locked": {"block"}}
    evaluation = evaluate_workspace(snapshot)
    items, narrative = priority_narrative(snapshot, evaluation)
    assert [item.ref.id for item in items] == ["ready"]
    assert "Locked and excluded" in narrative
    assert "locked" in narrative


def test_recurrence_window_idempotency():
    now = datetime(2026, 11, 9, 17, 0)
    rule = RecurrenceRule(
        "t",
        frequency=RecurrenceFrequency.DAILY,
        interval=1,
        reset_hour=18,
        reset_minute=0,
        created_at=datetime(2026, 11, 6, 9, 0),
    )
    occurrence = latest_occurrence(rule, now)
    assert occurrence == datetime(2026, 11, 8, 18, 0)
    assert recurrence_due(rule, now) == occurrence
    rule.last_reset_window = occurrence
    assert recurrence_due(rule, now) is None
    assert recurrence_due(rule, datetime(2026, 11, 9, 18, 1)) == datetime(
        2026, 11, 9, 18, 0
    )

    weekly = RecurrenceRule(
        "t",
        frequency=RecurrenceFrequency.WEEKLY,
        interval=1,
        by_day="SU",
        reset_hour=18,
        created_at=datetime(2026, 11, 1, 9, 0),  # Sunday
    )
    assert latest_occurrence(weekly, datetime(2026, 11, 10, 12, 0)) == datetime(
        2026, 11, 8, 18, 0
    )
    weekly.until_date = datetime(2026, 11, 1, 23, 59)
    assert latest_occurrence(weekly, datetime(2026, 11, 10, 12, 0)) is None


def test_default_availability_is_24_7_and_weekday_windows():
    assert availability_is_open([], datetime(2026, 11, 14, 3, 0)) is True
    assert availability_summary([]) == "24/7 — always available"

    windows = normalize_availability_windows(
        [AvailabilityWindow(day, 9 * 60, 17 * 60) for day in range(5)]
    )
    assert availability_is_open(windows, datetime(2026, 11, 16, 10, 0)) is True
    assert availability_is_open(windows, datetime(2026, 11, 16, 18, 0)) is False
    assert availability_is_open(windows, datetime(2026, 11, 14, 10, 0)) is False  # Saturday
    assert availability_summary(windows) == "Mon–Fri · 09:00–17:00"


def test_multiple_daily_windows_and_selected_weekdays():
    windows = normalize_availability_windows(
        [
            AvailabilityWindow(0, 8 * 60, 12 * 60),
            AvailabilityWindow(0, 13 * 60, 17 * 60),
            AvailabilityWindow(2, 8 * 60, 12 * 60),
            AvailabilityWindow(2, 13 * 60, 17 * 60),
            AvailabilityWindow(4, 8 * 60, 12 * 60),
            AvailabilityWindow(4, 13 * 60, 17 * 60),
        ]
    )
    assert availability_is_open(windows, datetime(2026, 11, 16, 12, 30)) is False
    assert availability_is_open(windows, datetime(2026, 11, 16, 13, 30)) is True
    assert availability_is_open(windows, datetime(2026, 11, 17, 10, 0)) is False  # Tuesday
    assert availability_summary(windows) == (
        "Mon, Wed, Fri · 08:00–12:00, 13:00–17:00"
    )


def test_availability_windows_crossing_midnight():
    windows = normalize_availability_windows(
        [AvailabilityWindow(4, 22 * 60, 2 * 60)]  # Friday 22:00 → Saturday 02:00
    )
    assert AvailabilityWindow(4, 22 * 60, 24 * 60) in windows
    assert AvailabilityWindow(5, 0, 2 * 60) in windows
    assert availability_is_open(windows, datetime(2026, 11, 20, 23, 0)) is True
    assert availability_is_open(windows, datetime(2026, 11, 21, 1, 0)) is True
    assert availability_is_open(windows, datetime(2026, 11, 21, 3, 0)) is False
    assert "Fri · 22:00–24:00" in availability_summary(windows)
    assert "Sat · 00:00–02:00" in availability_summary(windows)


def test_availability_validation_and_state_text():
    with pytest.raises(ValidationError):
        normalize_availability_windows([AvailabilityWindow(7, 0, 60)])
    with pytest.raises(ValidationError):
        normalize_availability_windows([AvailabilityWindow(0, 600, 600)])
    assert availability_state_text([], datetime(2026, 11, 16, 10, 0)) == "open 24/7"
    windows = [AvailabilityWindow(0, 9 * 60, 17 * 60)]
    assert "outside availability" in availability_state_text(
        windows, datetime(2026, 11, 16, 18, 0)
    )
    assert availability_state_text(
        windows, datetime(2026, 11, 16, 10, 0)
    ) == "available now"


def test_availability_never_affects_status_or_reconciliation():
    closed = [AvailabilityWindow(0, 9 * 60, 10 * 60)]
    ready = Task("ready", "Ready", availability=closed)
    blocked = Task("blocked", "Blocked", current_state=TaskState.BLOCKED, availability=[])
    snapshot = WorkspaceSnapshot(
        tasks={"ready": ready, "blocked": blocked},
        conditions={"gate": Condition("gate", "Gate", evaluation_state=False)},
    )
    snapshot.task_preconditions = {"blocked": {"gate"}}
    evaluation = evaluate_workspace(snapshot)
    # The available/closed distinction is unrelated to dependency evaluation.
    assert evaluation.task_status("ready").ready is True
    assert evaluation.task_status("blocked").ready is False
    assert reconcile_plan(snapshot, evaluation).empty
