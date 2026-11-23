"""Typed domain records for the EFOS explicit dependency model."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value

class TaskState(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    DEFERRED = "deferred"

    @property
    def label(self) -> str:
        return {"ready": "Ready", "blocked": "Blocked", "completed": "Completed", "deferred": "Deferred"}[self.value]

    @property
    def is_active(self) -> bool: return self in (TaskState.READY, TaskState.BLOCKED)

    @property
    def is_terminal(self) -> bool:
        """Completed and deferred tasks are hidden from operational planning."""
        return self in (TaskState.COMPLETED, TaskState.DEFERRED)

class ProjectState(StrEnum):
    ACTIVE = "active"
    STALLED = "stalled"
    SHELVED = "shelved"
    RESOLVED = "resolved"

    @property
    def label(self) -> str:
        return {"active": "Active", "stalled": "Stalled", "shelved": "Shelved", "resolved": "Resolved"}[self.value]

    @property
    def is_open(self) -> bool: return self in (ProjectState.ACTIVE, ProjectState.STALLED, ProjectState.SHELVED)

    @property
    def is_resolved(self) -> bool: return self is ProjectState.RESOLVED

class NodeKind(StrEnum):
    TASK = "task"
    PROJECT = "project"
    CONDITION = "condition"

    @property
    def label(self) -> str: return self.value.title()

class EdgeKind(StrEnum):
    PRECONDITION = "precondition"
    PREREQUISITE = "prerequisite"

    @property
    def label(self) -> str: return "Precondition" if self is EdgeKind.PRECONDITION else "Prerequisite"

class RecurrenceFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"

    @property
    def label(self) -> str: return self.value.title()

@dataclass(frozen=True, slots=True)
class NodeRef:
    kind: NodeKind
    id: str

    @property
    def key(self) -> str: return f"{self.kind.value}:{self.id}"

    @classmethod
    def parse(cls, key: str) -> "NodeRef":
        kind_text, object_id = key.split(":", 1)
        return cls(NodeKind(kind_text), object_id)

@dataclass(frozen=True, slots=True)
class AvailabilityWindow:
    """One local-time window during which a Task can actually be performed.

    ``weekday`` follows ``datetime.weekday()`` (0 = Monday, 6 = Sunday).
    ``start_minute``/``end_minute`` are minutes after local midnight; the end is
    exclusive. A Task with no windows is available 24/7.
    """

    weekday: int
    start_minute: int
    end_minute: int

    @property
    def key(self) -> tuple[int, int, int]: return (self.weekday, self.start_minute, self.end_minute)

@dataclass(frozen=True, slots=True)
class Edge:
    source: NodeRef
    target: NodeRef
    kind: EdgeKind

    @property
    def key(self) -> str: return f"{self.kind.value}:{self.source.key}->{self.target.key}"

    @property
    def label(self) -> str: return self.kind.label

@dataclass(slots=True)
class RecurrenceRule:
    task_id: str
    frequency: RecurrenceFrequency = RecurrenceFrequency.WEEKLY
    interval: int = 1
    by_day: str | None = None
    by_month_day: int | None = None
    occurrence_count: int | None = None
    until_date: datetime | None = None
    reset_hour: int = 18
    reset_minute: int = 0
    end_hour: int = 19
    end_minute: int = 0
    last_reset_window: datetime | None = None
    created_at: datetime | None = None

    @property
    def by_day_list(self) -> list[str]: return [value for value in (self.by_day or "").split(",") if value]

    def summary(self) -> str:
        days = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
        interval = ""
        if self.interval > 1: interval = f"Every {self.interval} "
        if self.frequency is RecurrenceFrequency.DAILY: text = f"{interval or 'Every '}day"
        elif self.frequency is RecurrenceFrequency.WEEKLY:
            names = ", ".join(days.get(day, day) for day in self.by_day_list)
            text = f"{interval or 'Every '}week" + (f" on {names}" if names else "")
        elif self.frequency is RecurrenceFrequency.MONTHLY:
            if self.by_month_day:
                day = self.by_month_day
                text = f"{interval or 'Every '}month on day {day}"
            else: text = f"{interval or 'Every '}month"
        else: text = f"{interval or 'Every '}year"
        end = ""
        if self.occurrence_count: end = f", {self.occurrence_count} occurrences"
        elif self.until_date: end = f", until {self.until_date:%b %d, %Y}"
        return f"{text}{end}"

@dataclass(slots=True)
class Task:
    id: str
    name: str
    is_recurring: bool = False
    current_state: TaskState = TaskState.READY
    previous_state: TaskState | None = None
    created_at: datetime | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    recurrence: RecurrenceRule | None = None
    availability: list[AvailabilityWindow] = field(default_factory=list)

    @property
    def has_schedule(self) -> bool:
        return self.scheduled_start is not None and self.scheduled_end is not None

    @property
    def has_custom_availability(self) -> bool:
        """True when the Task does not use the 24/7 default."""
        return bool(self.availability)

    @property
    def operational(self) -> bool: return self.current_state.is_active

@dataclass(slots=True)
class Project:
    id: str
    name: str
    current_state: ProjectState = ProjectState.ACTIVE
    previous_state: ProjectState | None = None
    resolution_conditions_unknown: bool = True
    created_at: datetime | None = None

@dataclass(slots=True)
class Condition:
    id: str
    display_name: str
    evaluation_state: bool | None = None
    created_at: datetime | None = None

@dataclass(slots=True)
class WorkspaceSnapshot:
    tasks: dict[str, Task] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)
    conditions: dict[str, Condition] = field(default_factory=dict)
    task_preconditions: dict[str, set[str]] = field(default_factory=dict)
    project_preconditions: dict[str, set[str]] = field(default_factory=dict)
    task_prerequisites: dict[str, set[NodeRef]] = field(default_factory=dict)
    project_prerequisites: dict[str, set[NodeRef]] = field(default_factory=dict)
    loaded_at: datetime | None = None

    # ------------------------------------------------------------- lookups
    def get_name(self, ref: NodeRef) -> str:
        if ref.kind is NodeKind.TASK:
            item = self.tasks.get(ref.id)
            return item.name if item else "Deleted task"
        if ref.kind is NodeKind.PROJECT:
            item = self.projects.get(ref.id)
            return item.name if item else "Deleted project"
        item = self.conditions.get(ref.id)
        return item.display_name if item else "Deleted condition"

    def get_state(self, ref: NodeRef) -> str:
        if ref.kind is NodeKind.TASK:
            item = self.tasks.get(ref.id)
            return item.current_state.value if item else "missing"
        if ref.kind is NodeKind.PROJECT:
            item = self.projects.get(ref.id)
            return item.current_state.value if item else "missing"
        item = self.conditions.get(ref.id)
        if not item: return "missing"
        if item.evaluation_state is True: return "true"
        if item.evaluation_state is False: return "false"
        return "unknown"

    def exists(self, ref: NodeRef) -> bool:
        if ref.kind is NodeKind.TASK: return ref.id in self.tasks
        if ref.kind is NodeKind.PROJECT: return ref.id in self.projects
        return ref.id in self.conditions

    def task_condition_ids(self, task_id: str) -> set[str]:
        return set(self.task_preconditions.get(task_id, set()))

    def project_condition_ids(self, project_id: str) -> set[str]:
        return set(self.project_preconditions.get(project_id, set()))

    def preconditions_for(self, ref: NodeRef) -> set[str]:
        if ref.kind is NodeKind.TASK: return self.task_condition_ids(ref.id)
        if ref.kind is NodeKind.PROJECT: return self.project_condition_ids(ref.id)
        return set()

    def prerequisites_for(self, ref: NodeRef) -> set[NodeRef]:
        if ref.kind is NodeKind.TASK: return set(self.task_prerequisites.get(ref.id, set()))
        if ref.kind is NodeKind.PROJECT: return set(self.project_prerequisites.get(ref.id, set()))
        return set()

    def attached_preconditions(self, condition_id: str) -> list[NodeRef]:
        result: list[NodeRef] = []
        for task_id, condition_ids in self.task_preconditions.items():
            if condition_id in condition_ids: result.append(NodeRef(NodeKind.TASK, task_id))
        for project_id, condition_ids in self.project_preconditions.items():
            if condition_id in condition_ids: result.append(NodeRef(NodeKind.PROJECT, project_id))
        return result

    def dependency_count(self, ref: NodeRef) -> int:
        return len(self.preconditions_for(ref)) + len(self.prerequisites_for(ref))

    def downstream_targets(self, ref: NodeRef) -> set[NodeRef]:
        """Transitive entities that list ``ref`` as a prerequisite."""
        seen: set[NodeRef] = set()
        frontier = [ref]
        while frontier:
            current = frontier.pop()
            for task_id, sources in self.task_prerequisites.items():
                if current in sources:
                    target = NodeRef(NodeKind.TASK, task_id)
                    if target not in seen:
                        seen.add(target)
                        frontier.append(target)
            for project_id, sources in self.project_prerequisites.items():
                if current in sources:
                    target = NodeRef(NodeKind.PROJECT, project_id)
                    if target not in seen:
                        seen.add(target)
                        frontier.append(target)
        return seen

    def downstream_count(self, ref: NodeRef) -> int: return len(self.downstream_targets(ref))

    # --------------------------------------------------------------- edges
    def edges(self) -> list[Edge]:
        result: list[Edge] = []
        for task_id, condition_ids in self.task_preconditions.items():
            if task_id not in self.tasks: continue
            for condition_id in condition_ids:
                if condition_id in self.conditions:
                    result.append(
                        Edge(
                            NodeRef(NodeKind.CONDITION, condition_id),
                            NodeRef(NodeKind.TASK, task_id),
                            EdgeKind.PRECONDITION,
                        )
                    )
        for project_id, condition_ids in self.project_preconditions.items():
            if project_id not in self.projects: continue
            for condition_id in condition_ids:
                if condition_id in self.conditions:
                    result.append(
                        Edge(
                            NodeRef(NodeKind.CONDITION, condition_id),
                            NodeRef(NodeKind.PROJECT, project_id),
                            EdgeKind.PRECONDITION,
                        )
                    )
        for task_id, sources in self.task_prerequisites.items():
            if task_id not in self.tasks: continue
            for source in sources:
                if self.exists(source):
                    result.append(Edge(source, NodeRef(NodeKind.TASK, task_id), EdgeKind.PREREQUISITE))
        for project_id, sources in self.project_prerequisites.items():
            if project_id not in self.projects: continue
            for source in sources:
                if self.exists(source):
                    result.append(Edge(source, NodeRef(NodeKind.PROJECT, project_id), EdgeKind.PREREQUISITE))
        return result

    def count_edges(self) -> int: return len(self.edges())

    def operational_refs(self) -> list[NodeRef]:
        refs: list[NodeRef] = []
        for task in self.tasks.values():
            if task.current_state.is_active: refs.append(NodeRef(NodeKind.TASK, task.id))
        for project in self.projects.values():
            if project.current_state.is_open: refs.append(NodeRef(NodeKind.PROJECT, project.id))
        for condition in self.conditions.values(): refs.append(NodeRef(NodeKind.CONDITION, condition.id))
        return refs

    def all_refs(self) -> list[NodeRef]:
        refs = [NodeRef(NodeKind.TASK, task_id) for task_id in self.tasks]
        refs += [NodeRef(NodeKind.PROJECT, project_id) for project_id in self.projects]
        refs += [NodeRef(NodeKind.CONDITION, condition_id) for condition_id in self.conditions]
        return refs

    # ---------------------------------------------------------- dataclasses
    def downstream_labels(self, ref: NodeRef) -> list[str]:
        return sorted((self.get_name(target) for target in self.downstream_targets(ref)), key=str.casefold)

@dataclass(slots=True)
class EntityStatus:
    ready: bool = True
    precondition_values: dict[str, bool | None] = field(default_factory=dict)
    prerequisite_values: dict[NodeRef, bool] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    @property
    def total(self) -> int: return len(self.precondition_values) + len(self.prerequisite_values)

    @property
    def satisfied(self) -> int:
        return sum(1 for value in self.precondition_values.values() if value is True) + sum(
            1 for value in self.prerequisite_values.values() if value is True
        )

    @property
    def ratio(self) -> float: return self.satisfied / self.total if self.total else 0.0

    def unresolved_preconditions(self) -> list[str]:
        return [key for key, value in self.precondition_values.items() if value is not True]

@dataclass(slots=True)
class Evaluation:
    tasks: dict[str, EntityStatus] = field(default_factory=dict)
    projects: dict[str, EntityStatus] = field(default_factory=dict)
    condition_values: dict[str, bool | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def for_ref(self, ref: NodeRef) -> EntityStatus | None:
        if ref.kind is NodeKind.TASK: return self.tasks.get(ref.id)
        if ref.kind is NodeKind.PROJECT: return self.projects.get(ref.id)
        return None

    def task_status(self, task_id: str) -> EntityStatus: return self.tasks.get(task_id, EntityStatus())

    def project_status(self, project_id: str) -> EntityStatus: return self.projects.get(project_id, EntityStatus())

    def condition_value(self, condition_id: str) -> bool | None: return self.condition_values.get(condition_id)

@dataclass(slots=True)
class Recommendation:
    ref: NodeRef
    name: str
    state: str
    rationale: str
    tier: int  # 1 active priority, 2 critical milestone, 3 stalled, 4 revisit

@dataclass(slots=True)
class PriorityItem:
    rank: int
    ref: NodeRef
    name: str
    rationale: str

@dataclass(slots=True)
class ConnectionStatus:
    connected: bool
    host: str
    port: int
    database: str
    message: str = ""

    @property
    def endpoint(self) -> str: return f"{self.host}:{self.port}/{self.database}"

    @property
    def metadata(self) -> str: return self.endpoint if self.connected else "not connected"

@dataclass(slots=True)
class ChatThread:
    id: str
    title: str = "New conversation"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int = 0

@dataclass(slots=True)
class ChatMessage:
    id: str
    thread_id: str
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None

@dataclass(slots=True)
class CustomQuery:
    id: str
    label: str
    prompt: str
    favorite: bool = False
    sort_index: int = 0

@dataclass(slots=True)
class CalendarItem:
    kind: str  # "task" | "external"
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    state: str = ""
    read_only: bool = False
    detail: str = ""

@dataclass(slots=True)
class SyncState:
    status: str = "never"  # never | ok | stale | error | disconnected
    last_sync_at: datetime | None = None
    message: str = ""
    account_email: str = ""
    connected: bool = False

    @property
    def warning(self) -> str:
        if self.status == "ok": return ""
        if self.last_sync_at:
            stamp = self.last_sync_at.strftime("%b %d, %Y %H:%M")
            return f"Sync Unavailable — Data Stale as of {stamp}"
        if self.connected: return "Sync Unavailable — no local snapshot yet"
        return "Google Calendar is not connected"

# Tables that the read-only RAG assistant may see.
PUBLIC_TABLES = (
    "tasks",
    "projects",
    "conditions",
    "task_preconditions",
    "project_preconditions",
    "task_prerequisites",
    "project_prerequisites",
    "task_recurrence_rules",
    "task_availability_windows",
    "notes",
    "task_notes",
    "project_notes",
    "condition_notes",
    "events",
    "calendar_events",
)

def refs_names(snapshot: WorkspaceSnapshot, refs: Iterable[NodeRef]) -> list[str]:
    return [snapshot.get_name(ref) for ref in refs]
