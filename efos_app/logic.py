"""Pure domain logic for the explicit conjunctive dependency model.

No Qt or database imports live here. The rules are deterministic and directly
testable. Availability and resolution are derived, never manually asserted:

* Preconditions are three-valued external facts; only TRUE clears the barrier.
* Prerequisites are two-valued internal states; a source Task must be
  ``completed`` and a source Project must be ``resolved``.
* An entity with no dependencies is satisfied by default.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    Edge,
    EdgeKind,
    EntityStatus,
    Evaluation,
    NodeKind,
    NodeRef,
    PriorityItem,
    ProjectState,
    RecurrenceFrequency,
    RecurrenceRule,
    Recommendation,
    TaskState,
    WorkspaceSnapshot,
)

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.AVAILABLE: frozenset(
        {TaskState.UNAVAILABLE, TaskState.COMPLETED, TaskState.DEFERRED}
    ),
    TaskState.UNAVAILABLE: frozenset({TaskState.AVAILABLE, TaskState.DEFERRED}),
    TaskState.COMPLETED: frozenset({TaskState.AVAILABLE, TaskState.UNAVAILABLE}),
    TaskState.DEFERRED: frozenset({TaskState.AVAILABLE, TaskState.UNAVAILABLE}),
}

PROJECT_TRANSITIONS: dict[ProjectState, frozenset[ProjectState]] = {
    ProjectState.ACTIVE: frozenset(
        {ProjectState.STALLED, ProjectState.SHELVED, ProjectState.RESOLVED}
    ),
    ProjectState.STALLED: frozenset({ProjectState.ACTIVE, ProjectState.RESOLVED}),
    ProjectState.SHELVED: frozenset({ProjectState.ACTIVE, ProjectState.RESOLVED}),
    ProjectState.RESOLVED: frozenset(
        {ProjectState.ACTIVE, ProjectState.STALLED, ProjectState.SHELVED}
    ),
}

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_NAMES = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}


def task_targets(current: TaskState) -> frozenset[TaskState]:
    return TASK_TRANSITIONS.get(current, frozenset())


def project_targets(current: ProjectState) -> frozenset[ProjectState]:
    return PROJECT_TRANSITIONS.get(current, frozenset())


def validate_task_transition(current: TaskState, target: TaskState) -> None:
    if target not in task_targets(current):
        raise ValidationError(
            f"The task state machine does not allow {current.label} → {target.label}."
        )


def validate_project_transition(current: ProjectState, target: ProjectState) -> None:
    if target not in project_targets(current):
        raise ValidationError(
            f"The project state machine does not allow {current.label} → {target.label}."
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def source_satisfied(snapshot: WorkspaceSnapshot, source: NodeRef) -> bool:
    if source.kind is NodeKind.TASK:
        task = snapshot.tasks.get(source.id)
        return bool(task and task.current_state is TaskState.COMPLETED)
    if source.kind is NodeKind.PROJECT:
        project = snapshot.projects.get(source.id)
        return bool(project and project.current_state is ProjectState.RESOLVED)
    return False


def _status_for(
    snapshot: WorkspaceSnapshot,
    precondition_ids: Iterable[str],
    prerequisites: Iterable[NodeRef],
) -> EntityStatus:
    status = EntityStatus()
    for condition_id in sorted(precondition_ids):
        condition = snapshot.conditions.get(condition_id)
        value = condition.evaluation_state if condition else None
        status.precondition_values[condition_id] = value
        if value is not True:
            label = condition.display_name if condition else "Missing condition"
            state = "UNKNOWN" if value is None else "FALSE"
            status.blockers.append(f"Precondition “{label}” is {state}")
    for source in sorted(prerequisites, key=lambda ref: ref.key):
        satisfied = source_satisfied(snapshot, source)
        status.prerequisite_values[source] = satisfied
        if not satisfied:
            name = snapshot.get_name(source)
            current = snapshot.get_state(source)
            status.blockers.append(f"Prerequisite “{name}” is {current}")
    status.available = all(
        value is True for value in status.precondition_values.values()
    ) and all(status.prerequisite_values.values())
    return status


def evaluate_workspace(snapshot: WorkspaceSnapshot) -> Evaluation:
    evaluation = Evaluation()
    for condition in snapshot.conditions.values():
        evaluation.condition_values[condition.id] = condition.evaluation_state
    for task in snapshot.tasks.values():
        evaluation.tasks[task.id] = _status_for(
            snapshot,
            snapshot.task_condition_ids(task.id),
            snapshot.prerequisites_for(NodeRef(NodeKind.TASK, task.id)),
        )
    for project in snapshot.projects.values():
        evaluation.projects[project.id] = _status_for(
            snapshot,
            snapshot.project_condition_ids(project.id),
            snapshot.prerequisites_for(NodeRef(NodeKind.PROJECT, project.id)),
        )
    return evaluation


# ---------------------------------------------------------------------------
# Cycle prevention (prerequisite graph: source → dependent)
# ---------------------------------------------------------------------------
def prerequisite_adjacency(snapshot: WorkspaceSnapshot) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for ref in snapshot.all_refs():
        if ref.kind is not NodeKind.CONDITION:
            adjacency.setdefault(ref.key, set())
    for task_id, sources in snapshot.task_prerequisites.items():
        target = NodeRef(NodeKind.TASK, task_id).key
        adjacency.setdefault(target, set())
        for source in sources:
            adjacency.setdefault(source.key, set()).add(target)
    for project_id, sources in snapshot.project_prerequisites.items():
        target = NodeRef(NodeKind.PROJECT, project_id).key
        adjacency.setdefault(target, set())
        for source in sources:
            adjacency.setdefault(source.key, set()).add(target)
    for task_id, condition_ids in snapshot.task_preconditions.items():
        target = NodeRef(NodeKind.TASK, task_id).key
        for condition_id in condition_ids:
            adjacency.setdefault(NodeRef(NodeKind.CONDITION, condition_id).key, set()).add(
                target
            )
    for project_id, condition_ids in snapshot.project_preconditions.items():
        target = NodeRef(NodeKind.PROJECT, project_id).key
        for condition_id in condition_ids:
            adjacency.setdefault(NodeRef(NodeKind.CONDITION, condition_id).key, set()).add(
                target
            )
    return adjacency


def find_cycle_with_edge(
    snapshot: WorkspaceSnapshot, source_key: str, target_key: str
) -> list[str]:
    adjacency = {key: set(values) for key, values in prerequisite_adjacency(snapshot).items()}
    adjacency.setdefault(source_key, set()).add(target_key)
    adjacency.setdefault(target_key, set())

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {key: WHITE for key in adjacency}
    parent: dict[str, str | None] = {key: None for key in adjacency}

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        for neighbor in sorted(adjacency.get(node, ())):
            if color.get(neighbor, WHITE) == WHITE:
                parent[neighbor] = node
                found = visit(neighbor)
                if found:
                    return found
            elif color.get(neighbor) == GRAY:
                cycle = [neighbor]
                cursor = node
                while cursor is not None and cursor != neighbor:
                    cycle.append(cursor)
                    cursor = parent.get(cursor)
                cycle.append(neighbor)
                cycle.reverse()
                return cycle
        color[node] = BLACK
        return None

    for node in sorted(adjacency):
        if color[node] == WHITE:
            cycle = visit(node)
            if cycle:
                return cycle
    return []


def describe_cycle(snapshot: WorkspaceSnapshot, cycles: Iterable[str]) -> str:
    labels: list[str] = []
    for key in cycles:
        try:
            labels.append(snapshot.get_name(NodeRef.parse(key)))
        except (ValueError, KeyError):
            labels.append(key)
    return " → ".join(labels)


def validate_connection(
    snapshot: WorkspaceSnapshot, source: NodeRef, target: NodeRef
) -> EdgeKind:
    if not snapshot.exists(source):
        raise NotFoundError(f"The source {source.kind.value} no longer exists.")
    if not snapshot.exists(target):
        raise NotFoundError(f"The target {target.kind.value} no longer exists.")
    if source == target:
        raise ConflictError("A node cannot depend on itself.")
    if target.kind is NodeKind.PROJECT:
        project = snapshot.projects.get(target.id)
        if project and project.current_state is ProjectState.RESOLVED:
            raise ConflictError(
                "This project is resolved. Its dependency set is closed; historical "
                "requirements cannot be extended."
            )
    if target.kind is NodeKind.TASK:
        task = snapshot.tasks.get(target.id)
        if task and task.current_state.is_terminal:
            raise ConflictError(
                "Completed and deferred tasks are historical. Reopen the task before "
                "changing its dependencies."
            )
    if target.kind is NodeKind.CONDITION:
        raise ValidationError(
            "Conditions are external facts. Draw arrows from a Condition to a Task "
            "or Project, or from a Task/Project to a downstream Task/Project."
        )
    if source.kind is NodeKind.CONDITION:
        if target.kind not in (NodeKind.TASK, NodeKind.PROJECT):
            raise ValidationError("A Condition can only target a Task or Project.")
        current = snapshot.preconditions_for(target)
        if source.id in current:
            raise ConflictError("That precondition is already attached.")
        cycle = find_cycle_with_edge(snapshot, source.key, target.key)
        if cycle:
            raise ConflictError(
                "Circular dependency rejected: " + describe_cycle(snapshot, cycle)
            )
        return EdgeKind.PRECONDITION
    # Task or Project source → Task or Project target.
    if source in snapshot.prerequisites_for(target):
        raise ConflictError("That prerequisite is already attached.")
    cycle = find_cycle_with_edge(snapshot, source.key, target.key)
    if cycle:
        raise ConflictError(
            "Circular prerequisite dependency rejected: "
            + describe_cycle(snapshot, cycle)
        )
    return EdgeKind.PREREQUISITE


def validate_new_entity_connection(
    snapshot: WorkspaceSnapshot, kind: NodeKind, source: NodeRef
) -> None:
    if kind is NodeKind.CONDITION:
        raise ValidationError("Conditions cannot target other Conditions.")
    if source.kind is NodeKind.CONDITION:
        return
    raise ValidationError("Unsupported dependency direction.")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReconcilePlan:
    task_targets: dict[str, TaskState] = field(default_factory=dict)
    project_targets: dict[str, ProjectState] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.task_targets and not self.project_targets


def reconcile_plan(snapshot: WorkspaceSnapshot, evaluation: Evaluation) -> ReconcilePlan:
    plan = ReconcilePlan()
    for task in snapshot.tasks.values():
        if not task.current_state.is_active:
            continue
        status = evaluation.task_status(task.id)
        desired = TaskState.AVAILABLE if status.available else TaskState.UNAVAILABLE
        if desired is not task.current_state:
            plan.task_targets[task.id] = desired
    for project in snapshot.projects.values():
        # Automated resolution applies only to active projects. Stalled and
        # shelved projects require an explicit administrative state change.
        if project.current_state is not ProjectState.ACTIVE:
            continue
        if project.resolution_conditions_unknown:
            continue
        status = evaluation.project_status(project.id)
        if status.available:
            plan.project_targets[project.id] = ProjectState.RESOLVED
    return plan


# ---------------------------------------------------------------------------
# Recommendation cascade
# ---------------------------------------------------------------------------
def _upcoming_window(task, now: datetime) -> bool:
    if not task.has_schedule:
        return False
    assert task.scheduled_start is not None and task.scheduled_end is not None
    return task.scheduled_start <= now + timedelta(days=7) and task.scheduled_end >= now


def _active_score(snapshot: WorkspaceSnapshot, task, now: datetime) -> tuple:
    downstream = snapshot.downstream_count(NodeRef(NodeKind.TASK, task.id))
    upcoming = _upcoming_window(task, now)
    soon = bool(task.scheduled_start and task.scheduled_start.date() == now.date())
    score = downstream * 10 + (5 if upcoming else 0) + (2 if soon else 0)
    start = task.scheduled_start or datetime.max
    return (score, start, task.name.casefold())


def _active_sort_key(snapshot: WorkspaceSnapshot, task, now: datetime) -> tuple:
    score, start, name = _active_score(snapshot, task, now)
    return (-score, start, name)


def active_task_ranking(
    snapshot: WorkspaceSnapshot, evaluation: Evaluation, now: datetime | None = None
) -> list[PriorityItem]:
    now = now or datetime.now()
    candidates = []
    for task in snapshot.tasks.values():
        if task.current_state is not TaskState.AVAILABLE:
            continue
        if not evaluation.task_status(task.id).available:
            continue
        candidates.append(task)
    candidates.sort(key=lambda task: _active_sort_key(snapshot, task, now))
    ranked: list[PriorityItem] = []
    for index, task in enumerate(candidates, start=1):
        parts = ["Available now"]
        downstream = snapshot.downstream_count(NodeRef(NodeKind.TASK, task.id))
        if downstream:
            parts.append(
                f"Unblocks {downstream} downstream "
                f"{'Task' if downstream == 1 else 'Tasks'}"
            )
        if task.has_schedule and task.scheduled_start:
            parts.append(f"Scheduled {task.scheduled_start:%a %b %d, %H:%M}")
        ranked.append(
            PriorityItem(
                index,
                NodeRef(NodeKind.TASK, task.id),
                task.name,
                ". ".join(parts) + ".",
            )
        )
    return ranked


def recommend(
    snapshot: WorkspaceSnapshot,
    evaluation: Evaluation,
    now: datetime | None = None,
) -> Recommendation | None:
    """Deterministic four-tier suggestion cascade."""
    now = now or datetime.now()

    # Tier 1 — active priority: an available Task.
    for item in active_task_ranking(snapshot, evaluation, now):
        task = snapshot.tasks[item.ref.id]
        parts = [item.rationale]
        if not snapshot.downstream_count(item.ref) and not task.has_schedule:
            parts = ["Available now; all preconditions and prerequisites are satisfied."]
        return Recommendation(
            item.ref, item.name, task.current_state.value, " ".join(parts), 1
        )

    # Tier 2 — critical milestone: > 50% of direct requirements TRUE.
    milestones = []
    for project in snapshot.projects.values():
        if project.current_state is not ProjectState.ACTIVE:
            continue
        if project.resolution_conditions_unknown:
            continue
        status = evaluation.project_status(project.id)
        if status.total and status.ratio > 0.5:
            milestones.append((status.ratio, status.satisfied, project))
    milestones.sort(key=lambda row: (-row[0], -row[1], row[2].name.casefold()))
    if milestones:
        ratio, satisfied, project = milestones[0]
        status = evaluation.project_status(project.id)
        return Recommendation(
            NodeRef(NodeKind.PROJECT, project.id),
            project.name,
            project.current_state.value,
            f"{satisfied} of {status.total} requirements satisfied ({ratio:.0%}).",
            2,
        )

    # Tier 3 — stalled snag alert.
    stalled = sorted(
        (p for p in snapshot.projects.values() if p.current_state is ProjectState.STALLED),
        key=lambda p: p.name.casefold(),
    )
    if stalled:
        project = stalled[0]
        status = evaluation.project_status(project.id)
        blockers = len(status.blockers)
        sentence = (
            f"Stalled project with {blockers} unresolved "
            f"{'blocker' if blockers == 1 else 'blockers'}; requires intervention."
            if blockers
            else "Stalled project; requires an explicit reactivation decision."
        )
        return Recommendation(
            NodeRef(NodeKind.PROJECT, project.id),
            project.name,
            project.current_state.value,
            sentence,
            3,
        )

    # Tier 4 — revisit candidate: shelved with zero unresolved precondition blocks.
    revisits = []
    for project in snapshot.projects.values():
        if project.current_state is not ProjectState.SHELVED:
            continue
        status = evaluation.project_status(project.id)
        if all(value is True for value in status.precondition_values.values()):
            revisits.append(project)
    revisits.sort(key=lambda p: p.name.casefold())
    if revisits:
        project = revisits[0]
        count = len(evaluation.project_status(project.id).precondition_values)
        sentence = (
            f"Safe to resume: {count} external "
            f"{'precondition is' if count == 1 else 'preconditions are'} resolved."
            if count
            else "Safe to resume: no external preconditions block this project."
        )
        return Recommendation(
            NodeRef(NodeKind.PROJECT, project.id),
            project.name,
            project.current_state.value,
            sentence,
            4,
        )
    return None


def priority_narrative(
    snapshot: WorkspaceSnapshot,
    evaluation: Evaluation,
    now: datetime | None = None,
) -> tuple[list[PriorityItem], str]:
    """Ground the top-3 preset exclusively in authoritatively available Tasks."""
    items = active_task_ranking(snapshot, evaluation, now)[:3]
    if not items:
        return [], (
            "No Tasks are authoritatively available right now. Every active Task is "
            "locked by at least one unresolved precondition or prerequisite. "
            "Availability is read-only; this analysis will not change any record."
        )
    lines = [
        "Top priorities are drawn only from Tasks whose conjunctive dependencies "
        "are fully satisfied. No locked Task is recommended."
    ]
    for item in items:
        lines.append(f"{item.rank}. {item.name} — {item.rationale}")
    remaining = [
        task.name
        for task in snapshot.tasks.values()
        if task.current_state.is_active
        and not evaluation.task_status(task.id).available
    ]
    if remaining:
        lines.append(
            "Locked and excluded from consideration: "
            + ", ".join(sorted(remaining, key=str.casefold)[:6])
            + ("…" if len(remaining) > 6 else "")
            + "."
        )
    return items, "\n".join(lines)


# ---------------------------------------------------------------------------
# Recurrence windows
# ---------------------------------------------------------------------------
def _month_day(year: int, month: int, day: int) -> date | None:
    last = calendar.monthrange(year, month)[1]
    if day < 0:
        day = last + day + 1
    if 1 <= day <= last:
        return date(year, month, day)
    return None


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + offset
    return index // 12, index % 12 + 1


def latest_occurrence(rule: RecurrenceRule, now: datetime) -> datetime | None:
    """Most recent configured window at or before ``now``.

    The reset hour/minute selects the moment inside the window at which the task
    returns to availability. The idempotency marker is ``last_reset_window``.
    """
    anchor = (rule.created_at or now).date()
    today = now.date()
    clock = time(rule.reset_hour, rule.reset_minute)
    interval = max(1, int(rule.interval or 1))
    candidates: list[date] = []

    if rule.frequency is RecurrenceFrequency.DAILY:
        diff = (today - anchor).days
        if diff >= 0:
            base = (diff // interval) * interval
            candidates.append(anchor + timedelta(days=base))
            candidates.append(anchor + timedelta(days=base - interval))

    elif rule.frequency is RecurrenceFrequency.WEEKLY:
        codes = rule.by_day_list or [WEEKDAY_CODES[anchor.weekday()]]
        weekdays = sorted({WEEKDAY_CODES.index(code) for code in codes if code in WEEKDAY_CODES})
        anchor_week = anchor - timedelta(days=anchor.weekday())
        current_week = today - timedelta(days=today.weekday())
        weeks = (current_week - anchor_week).days // 7
        if weeks >= 0:
            base = anchor_week + timedelta(weeks=(weeks // interval) * interval)
            for weekday in weekdays:
                candidate = base + timedelta(days=weekday)
                candidates.append(candidate)
            previous = base - timedelta(weeks=interval)
            for weekday in weekdays:
                candidates.append(previous + timedelta(days=weekday))

    elif rule.frequency is RecurrenceFrequency.MONTHLY:
        months = (today.year - anchor.year) * 12 + (today.month - anchor.month)
        if months >= 0:
            day = rule.by_month_day if rule.by_month_day is not None else anchor.day
            for offset in ((months // interval) * interval, ((months // interval) * interval) - interval):
                year, month = _add_months(anchor.year, anchor.month, offset)
                candidate = _month_day(year, month, day)
                if candidate:
                    candidates.append(candidate)

    else:  # YEARLY
        years = today.year - anchor.year
        if years >= 0:
            month = anchor.month
            day = rule.by_month_day if rule.by_month_day is not None else anchor.day
            for offset in ((years // interval) * interval, ((years // interval) * interval) - interval):
                candidate = _month_day(anchor.year + offset, month, day)
                if candidate:
                    candidates.append(candidate)

    best: datetime | None = None
    for candidate in candidates:
        moment = datetime.combine(candidate, clock)
        if moment > now:
            continue
        if rule.until_date is not None and moment > rule.until_date:
            continue
        if best is None or moment > best:
            best = moment
    return best


def recurrence_due(rule: RecurrenceRule, now: datetime) -> datetime | None:
    occurrence = latest_occurrence(rule, now)
    if occurrence is None:
        return None
    if rule.last_reset_window is not None and rule.last_reset_window >= occurrence:
        return None
    return occurrence


def next_occurrence(rule: RecurrenceRule, after: datetime) -> datetime | None:
    probe = after
    for _ in range(400):
        probe = probe + timedelta(days=1)
        occurrence = latest_occurrence(rule, probe)
        if occurrence is not None and occurrence > after:
            return occurrence
    return None


def gate_summary(
    snapshot: WorkspaceSnapshot, evaluation: Evaluation, ref: NodeRef
) -> str:
    status = evaluation.for_ref(ref)
    if status is None:
        return ""
    if status.total == 0:
        return "No dependencies · satisfied by default"
    return f"{status.satisfied}/{status.total} requirements satisfied"


def condition_truth_label(value: bool | None) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return "UNKNOWN"
