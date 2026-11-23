"""Pure domain logic for the explicit conjunctive dependency model.

No Qt or database imports live here. The rules are deterministic and directly
testable. Status and resolution are derived, never manually asserted:

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
    AvailabilityWindow,
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
    TaskState.READY: frozenset({TaskState.BLOCKED, TaskState.COMPLETED, TaskState.DEFERRED}),
    TaskState.BLOCKED: frozenset({TaskState.READY, TaskState.DEFERRED}),
    TaskState.COMPLETED: frozenset({TaskState.READY, TaskState.BLOCKED}),
    TaskState.DEFERRED: frozenset({TaskState.READY, TaskState.BLOCKED}),
}

PROJECT_TRANSITIONS: dict[ProjectState, frozenset[ProjectState]] = {
    ProjectState.ACTIVE: frozenset({ProjectState.STALLED, ProjectState.SHELVED, ProjectState.RESOLVED}),
    ProjectState.STALLED: frozenset({ProjectState.ACTIVE, ProjectState.RESOLVED}),
    ProjectState.SHELVED: frozenset({ProjectState.ACTIVE, ProjectState.RESOLVED}),
    ProjectState.RESOLVED: frozenset({ProjectState.ACTIVE, ProjectState.STALLED, ProjectState.SHELVED}),
}

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_NAMES = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday", "SA": "Saturday", "SU": "Sunday"}

def task_targets(current: TaskState) -> frozenset[TaskState]:
    return TASK_TRANSITIONS.get(current, frozenset())

def project_targets(current: ProjectState) -> frozenset[ProjectState]:
    return PROJECT_TRANSITIONS.get(current, frozenset())

def validate_task_transition(current: TaskState, target: TaskState) -> None:
    if target not in task_targets(current):
        raise ValidationError(f"The task state machine does not allow {current.label} → {target.label}.")

def validate_project_transition(current: ProjectState, target: ProjectState) -> None:
    if target not in project_targets(current):
        raise ValidationError(f"The project state machine does not allow {current.label} → {target.label}.")

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

def _status_for(snapshot: WorkspaceSnapshot, precondition_ids: Iterable[str], prerequisites: Iterable[NodeRef]) -> EntityStatus:
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
    status.ready = all(
        value is True for value in status.precondition_values.values()
    ) and all(status.prerequisite_values.values())
    return status

def evaluate_workspace(snapshot: WorkspaceSnapshot) -> Evaluation:
    evaluation = Evaluation()
    for condition in snapshot.conditions.values(): evaluation.condition_values[condition.id] = condition.evaluation_state
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
        if ref.kind is not NodeKind.CONDITION: adjacency.setdefault(ref.key, set())
    for task_id, sources in snapshot.task_prerequisites.items():
        target = NodeRef(NodeKind.TASK, task_id).key
        adjacency.setdefault(target, set())
        for source in sources: adjacency.setdefault(source.key, set()).add(target)
    for project_id, sources in snapshot.project_prerequisites.items():
        target = NodeRef(NodeKind.PROJECT, project_id).key
        adjacency.setdefault(target, set())
        for source in sources: adjacency.setdefault(source.key, set()).add(target)
    for task_id, condition_ids in snapshot.task_preconditions.items():
        target = NodeRef(NodeKind.TASK, task_id).key
        for condition_id in condition_ids:
            adjacency.setdefault(NodeRef(NodeKind.CONDITION, condition_id).key, set()).add(target)
    for project_id, condition_ids in snapshot.project_preconditions.items():
        target = NodeRef(NodeKind.PROJECT, project_id).key
        for condition_id in condition_ids:
            adjacency.setdefault(NodeRef(NodeKind.CONDITION, condition_id).key, set()).add(target)
    return adjacency

def find_cycle_with_edge(snapshot: WorkspaceSnapshot, source_key: str, target_key: str) -> list[str]:
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
                if found: return found
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
            if cycle: return cycle
    return []

def describe_cycle(snapshot: WorkspaceSnapshot, cycles: Iterable[str]) -> str:
    labels: list[str] = []
    for key in cycles:
        try: labels.append(snapshot.get_name(NodeRef.parse(key)))
        except (ValueError, KeyError): labels.append(key)
    return " → ".join(labels)

def validate_connection(snapshot: WorkspaceSnapshot, source: NodeRef, target: NodeRef) -> EdgeKind:
    if not snapshot.exists(source): raise NotFoundError(f"The source {source.kind.value} no longer exists.")
    if not snapshot.exists(target): raise NotFoundError(f"The target {target.kind.value} no longer exists.")
    if source == target: raise ConflictError("A node cannot depend on itself.")
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
        if source.id in current: raise ConflictError("That precondition is already attached.")
        cycle = find_cycle_with_edge(snapshot, source.key, target.key)
        if cycle: raise ConflictError("Circular dependency rejected: " + describe_cycle(snapshot, cycle))
        return EdgeKind.PRECONDITION
    # Task or Project source → Task or Project target.
    if source in snapshot.prerequisites_for(target): raise ConflictError("That prerequisite is already attached.")
    cycle = find_cycle_with_edge(snapshot, source.key, target.key)
    if cycle:
        raise ConflictError(
            "Circular prerequisite dependency rejected: "
            + describe_cycle(snapshot, cycle)
        )
    return EdgeKind.PREREQUISITE

def validate_new_entity_connection(snapshot: WorkspaceSnapshot, kind: NodeKind, source: NodeRef) -> None:
    if kind is NodeKind.CONDITION: raise ValidationError("Conditions cannot target other Conditions.")
    if source.kind is NodeKind.CONDITION: return
    raise ValidationError("Unsupported dependency direction.")

# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReconcilePlan:
    task_targets: dict[str, TaskState] = field(default_factory=dict)
    project_targets: dict[str, ProjectState] = field(default_factory=dict)

    @property
    def empty(self) -> bool: return not self.task_targets and not self.project_targets

def reconcile_plan(snapshot: WorkspaceSnapshot, evaluation: Evaluation) -> ReconcilePlan:
    plan = ReconcilePlan()
    for task in snapshot.tasks.values():
        if not task.current_state.is_active: continue
        status = evaluation.task_status(task.id)
        desired = TaskState.READY if status.ready else TaskState.BLOCKED
        if desired is not task.current_state: plan.task_targets[task.id] = desired
    for project in snapshot.projects.values():
        # Automated resolution applies only to active projects. Stalled and
        # shelved projects require an explicit administrative state change.
        if project.current_state is not ProjectState.ACTIVE: continue
        if project.resolution_conditions_unknown: continue
        status = evaluation.project_status(project.id)
        if status.ready: plan.project_targets[project.id] = ProjectState.RESOLVED
    return plan

# ---------------------------------------------------------------------------
# Recommendation cascade
# ---------------------------------------------------------------------------
def _upcoming_window(task, now: datetime) -> bool:
    if not task.has_schedule: return False
    assert task.scheduled_start is not None and task.scheduled_end is not None
    return task.scheduled_start <= now + timedelta(days=7) and task.scheduled_end >= now

def _schedule_urgency_score(task, now: datetime) -> int:
    if not task.has_schedule or task.scheduled_start is None: return 0
    end = task.scheduled_end or task.scheduled_start
    if task.scheduled_start <= now <= end:
        return 25  # Active right now
    delta = task.scheduled_start - now
    if timedelta(0) < delta <= timedelta(hours=24):
        return 20  # Starting within the next 24 hours (e.g. tomorrow morning)
    if timedelta(hours=24) < delta <= timedelta(hours=48):
        return 12  # Starting within 48 hours
    if timedelta(hours=48) < delta <= timedelta(days=7):
        return 6   # Coming up this week
    return 1       # Scheduled further out (e.g. Dec 6)

def _active_score(snapshot: WorkspaceSnapshot, task, now: datetime) -> tuple:
    downstream = snapshot.downstream_count(NodeRef(NodeKind.TASK, task.id))
    urgency = _schedule_urgency_score(task, now)
    score = urgency + downstream * 10
    start = task.scheduled_start or datetime.max
    return (score, start, task.name.casefold())

def _active_sort_key(snapshot: WorkspaceSnapshot, task, now: datetime) -> tuple:
    score, start, name = _active_score(snapshot, task, now)
    return (-score, start, name)

def active_task_ranking(snapshot: WorkspaceSnapshot, evaluation: Evaluation, now: datetime | None = None) -> list[PriorityItem]:
    now = now or datetime.now()
    candidates = []
    for task in snapshot.tasks.values():
        if task.current_state is not TaskState.READY: continue
        if not evaluation.task_status(task.id).ready: continue
        candidates.append(task)
    candidates.sort(key=lambda task: _active_sort_key(snapshot, task, now))
    ranked: list[PriorityItem] = []
    for index, task in enumerate(candidates, start=1):
        parts = ["Ready now"]
        downstream = snapshot.downstream_count(NodeRef(NodeKind.TASK, task.id))
        if downstream:
            parts.append(
                f"Unblocks {downstream} downstream "
                f"{'Task' if downstream == 1 else 'Tasks'}"
            )
        if task.has_schedule and task.scheduled_start: parts.append(f"Scheduled {task.scheduled_start:%a %b %d, %H:%M}")
        ranked.append(PriorityItem(index, NodeRef(NodeKind.TASK, task.id), task.name, ". ".join(parts) + "."))
    return ranked

def recommend(snapshot: WorkspaceSnapshot, evaluation: Evaluation, now: datetime | None = None) -> Recommendation | None:
    """Deterministic four-tier suggestion cascade."""
    now = now or datetime.now()

    # Tier 1 — active priority: a ready Task.
    for item in active_task_ranking(snapshot, evaluation, now):
        task = snapshot.tasks[item.ref.id]
        parts = [item.rationale]
        if not snapshot.downstream_count(item.ref) and not task.has_schedule:
            parts = ["Ready now; all preconditions and prerequisites are satisfied."]
        return Recommendation(item.ref, item.name, task.current_state.value, " ".join(parts), 1)

    # Tier 2 — critical milestone: > 50% of direct requirements TRUE.
    milestones = []
    for project in snapshot.projects.values():
        if project.current_state is not ProjectState.ACTIVE: continue
        if project.resolution_conditions_unknown: continue
        status = evaluation.project_status(project.id)
        if status.total and status.ratio > 0.5: milestones.append((status.ratio, status.satisfied, project))
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
    stalled = sorted((p for p in snapshot.projects.values() if p.current_state is ProjectState.STALLED), key=lambda p: p.name.casefold())
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
        return Recommendation(NodeRef(NodeKind.PROJECT, project.id), project.name, project.current_state.value, sentence, 3)

    # Tier 4 — revisit candidate: shelved with zero unresolved precondition blocks.
    revisits = []
    for project in snapshot.projects.values():
        if project.current_state is not ProjectState.SHELVED: continue
        status = evaluation.project_status(project.id)
        if all(value is True for value in status.precondition_values.values()): revisits.append(project)
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
        return Recommendation(NodeRef(NodeKind.PROJECT, project.id), project.name, project.current_state.value, sentence, 4)
    return None

def priority_narrative(snapshot: WorkspaceSnapshot, evaluation: Evaluation, now: datetime | None = None) -> tuple[list[PriorityItem], str]:
    """Ground the top-3 preset exclusively in authoritatively ready Tasks."""
    items = active_task_ranking(snapshot, evaluation, now)[:3]
    if not items:
        return [], (
            "No Tasks are authoritatively ready right now. Every active Task is "
            "locked by at least one unresolved precondition or prerequisite.\n\n"
            "Status is read-only; this analysis will not change any record."
        )
    lines = ["Top 3 Ready Priorities - No locked Task is recommended."]
    for item in items:
        lines.append("")
        lines.append(f"{item.rank}. {item.name}")
        lines.append(f"    {item.rationale}")
    remaining = [
        task.name
        for task in snapshot.tasks.values()
        if task.current_state.is_active
        and not evaluation.task_status(task.id).ready
    ]
    if remaining:
        lines.append("")
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
    if day < 0: day = last + day + 1
    if 1 <= day <= last: return date(year, month, day)
    return None

def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + offset
    return index // 12, index % 12 + 1

def latest_occurrence(rule: RecurrenceRule, now: datetime) -> datetime | None:
    """Most recent configured window at or before ``now``.

    The reset hour/minute selects the moment inside the window at which the task
    returns to Ready status. The idempotency marker is ``last_reset_window``.
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
            for weekday in weekdays: candidates.append(previous + timedelta(days=weekday))

    elif rule.frequency is RecurrenceFrequency.MONTHLY:
        months = (today.year - anchor.year) * 12 + (today.month - anchor.month)
        if months >= 0:
            day = rule.by_month_day if rule.by_month_day is not None else anchor.day
            for offset in ((months // interval) * interval, ((months // interval) * interval) - interval):
                year, month = _add_months(anchor.year, anchor.month, offset)
                candidate = _month_day(year, month, day)
                if candidate: candidates.append(candidate)

    else:  # YEARLY
        years = today.year - anchor.year
        if years >= 0:
            month = anchor.month
            day = rule.by_month_day if rule.by_month_day is not None else anchor.day
            for offset in ((years // interval) * interval, ((years // interval) * interval) - interval):
                candidate = _month_day(anchor.year + offset, month, day)
                if candidate: candidates.append(candidate)

    best: datetime | None = None
    for candidate in candidates:
        moment = datetime.combine(candidate, clock)
        if moment > now: continue
        if rule.until_date is not None and moment > rule.until_date: continue
        if best is None or moment > best: best = moment
    return best

def recurrence_due(rule: RecurrenceRule, now: datetime) -> datetime | None:
    occurrence = latest_occurrence(rule, now)
    if occurrence is None: return None
    if rule.last_reset_window is not None and rule.last_reset_window >= occurrence: return None
    return occurrence

def next_occurrence(rule: RecurrenceRule, after: datetime) -> datetime | None:
    probe = after
    for _ in range(400):
        probe = probe + timedelta(days=1)
        occurrence = latest_occurrence(rule, probe)
        if occurrence is not None and occurrence > after: return occurrence
    return None

def gate_summary(snapshot: WorkspaceSnapshot, evaluation: Evaluation, ref: NodeRef) -> str:
    status = evaluation.for_ref(ref)
    if status is None: return ""
    if status.total == 0: return "No dependencies · satisfied by default"
    return f"{status.satisfied}/{status.total} requirements satisfied"

def condition_truth_label(value: bool | None) -> str:
    if value is True: return "TRUE"
    if value is False: return "FALSE"
    return "UNKNOWN"

# ---------------------------------------------------------------------------
# Temporal availability (independent of Task Status, scheduling, recurrence)
# ---------------------------------------------------------------------------
AVAILABILITY_DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
AVAILABILITY_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MINUTES_PER_DAY = 24 * 60

def _local_naive(moment: datetime) -> datetime:
    if moment.tzinfo is not None: moment = moment.astimezone().replace(tzinfo=None)
    return moment.replace(second=0, microsecond=0)

def _clock_text(minutes: int) -> str:
    if minutes >= MINUTES_PER_DAY: return "24:00"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

def normalize_availability_windows(windows: Iterable[AvailabilityWindow]) -> list[AvailabilityWindow]:
    """Validate, de-duplicate, and split windows that cross local midnight.

    A window whose end is at or before its start is interpreted as crossing
    midnight (for example Mon 22:00–02:00). It is stored as two non-crossing
    rows: Mon 22:00–24:00 and Tue 00:00–02:00. This keeps evaluation and SQL
    constraints simple while preserving the intended real-world window.
    """
    result: list[AvailabilityWindow] = []
    for window in windows:
        try:
            weekday = int(window.weekday)
            start = int(window.start_minute)
            end = int(window.end_minute)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Availability windows require integer day and time values.") from exc
        if not 0 <= weekday <= 6:
            raise ValidationError("Availability weekday must be between 0 (Monday) and 6 (Sunday).")
        if not 0 <= start < MINUTES_PER_DAY:
            raise ValidationError("Availability start time must be within the selected day.")
        if not 0 < end <= MINUTES_PER_DAY:
            raise ValidationError("Availability end time must be within the selected day.")
        if start == end: raise ValidationError("An availability window cannot start and end at the same time.")
        if end < start:
            result.append(AvailabilityWindow(weekday, start, MINUTES_PER_DAY))
            result.append(AvailabilityWindow((weekday + 1) % 7, 0, end))
        else: result.append(AvailabilityWindow(weekday, start, end))
    unique = sorted({window.key for window in result})
    return [AvailabilityWindow(*key) for key in unique]

def availability_is_open(windows: Iterable[AvailabilityWindow], moment: datetime | None = None) -> bool:
    """Whether a Task may be performed at ``moment`` (local wall-clock time).

    No configured windows means the 24/7 default, which is always open. This
    function never participates in dependency evaluation or Task state.
    """
    window_list = list(windows)
    if not window_list: return True
    moment = _local_naive(moment or datetime.now())
    minutes = moment.hour * 60 + moment.minute
    return any(
        window.weekday == moment.weekday()
        and window.start_minute <= minutes < window.end_minute
        for window in window_list
    )

def next_availability_start(windows: Iterable[AvailabilityWindow], moment: datetime | None = None) -> datetime | None:
    window_list = list(windows)
    if not window_list: return None
    moment = _local_naive(moment or datetime.now())
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    best: datetime | None = None
    for window in window_list:
        candidate = midnight + timedelta(days=(window.weekday - moment.weekday()) % 7, minutes=window.start_minute)
        if candidate <= moment: candidate += timedelta(days=7)
        if best is None or candidate < best: best = candidate
    return best

def availability_state_text(windows: Iterable[AvailabilityWindow], moment: datetime | None = None) -> str:
    window_list = list(windows)
    if not window_list: return "open 24/7"
    if availability_is_open(window_list, moment): return "available now"
    upcoming = next_availability_start(window_list, moment)
    if upcoming is None: return "outside availability"
    return f"outside availability · opens {upcoming:%a %H:%M}"

def _format_availability_days(days: list[int]) -> str:
    ordered = sorted(days)
    if ordered == list(range(7)): return "Every day"
    runs: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for day in ordered[1:]:
        if day == previous + 1:
            previous = day
            continue
        runs.append((start, previous))
        start = previous = day
    runs.append((start, previous))
    parts: list[str] = []
    for first, last in runs:
        if first == last: parts.append(AVAILABILITY_DAY_LABELS[first])
        else: parts.append(f"{AVAILABILITY_DAY_LABELS[first]}–{AVAILABILITY_DAY_LABELS[last]}")
    return ", ".join(parts)

def availability_summary(windows: Iterable[AvailabilityWindow]) -> str:
    """Human-readable availability, distinct from Status, schedule, and recurrence."""
    window_list = list(windows)
    if not window_list: return "24/7 — always available"
    by_day: dict[int, list[tuple[int, int]]] = {day: [] for day in range(7)}
    for window in window_list: by_day[window.weekday].append((window.start_minute, window.end_minute))
    groups: dict[tuple[tuple[int, int], ...], list[int]] = {}
    for day in range(7):
        spans = tuple(sorted(by_day[day]))
        if spans: groups.setdefault(spans, []).append(day)
    parts: list[str] = []
    for spans, days in sorted(groups.items(), key=lambda item: (item[1][0], item[0])):
        span_text = ", ".join(f"{_clock_text(start)}–{_clock_text(end)}" for start, end in spans)
        parts.append(f"{_format_availability_days(days)} · {span_text}")
    return "; ".join(parts)
