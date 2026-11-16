"""Overview and the primary Dashboard workspace with Manage and Flow tabs."""
from __future__ import annotations

from datetime import datetime, timedelta

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..formatting import fmt_relative, schedule_span, truncate
from ..logic import availability_summary
from ..models import NodeKind, NodeRef, ProjectState, TaskState
from ..service import EFOSService, WorkspaceView
from .flow_page import FlowWorkspace
from .graph import LayoutStore
from .icons import icon
from .inspector import InspectorPanel
from .theme import c, display_font, rgba, state_color
from .widgets import (Badge, Card, ClickableRow, Divider, MetricCard, SectionHeader, SegmentedControl)

def _clear_layout(layout) -> None:  # type: ignore[no-untyped-def]
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif child is not None:
            _clear_layout(child)

class StatusRing(QWidget):
    def __init__(self, ready: int, active: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.ready = ready
        self.active = max(active, 1)
        self.setFixedSize(148, 148)

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(14, 14, self.width() - 28, self.height() - 28)
        painter.setPen(QPen(QColor(c("border")), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(rect, 0, 360 * 16)
        fraction = min(1.0, self.ready / self.active) if self.active else 0.0
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor(c("accent")))
        gradient.setColorAt(1, QColor(c("teal")))
        pen = QPen(QColor(c("accent")), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        pen.setBrush(gradient)
        painter.setPen(pen)
        painter.drawArc(rect, 90 * 16, -int(360 * 16 * fraction))
        painter.setPen(QColor(c("text")))
        font = QFont("Segoe UI Variable Display", 25)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect.adjusted(0, -10, 0, -10), Qt.AlignmentFlag.AlignCenter, str(self.ready))
        painter.setPen(QColor(c("muted")))
        small = QFont("Segoe UI", 8)
        small.setBold(True)
        small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        painter.setFont(small)
        painter.drawText(rect.adjusted(0, 38, 0, 38), Qt.AlignmentFlag.AlignCenter, "READY NOW")

class OverviewPage(QWidget):
    create_requested = pyqtSignal(object)
    open_node = pyqtSignal(object)
    open_dashboard = pyqtSignal()
    open_registry = pyqtSignal()
    open_chat = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view: WorkspaceView | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        outer.addWidget(self.scroll)
        self.host = QWidget()
        self.content = QVBoxLayout(self.host)
        self.content.setContentsMargins(26, 20, 26, 26)
        self.content.setSpacing(18)
        self.scroll.setWidget(self.host)

    def refresh(self, view: WorkspaceView) -> None:
        self.view = view
        _clear_layout(self.content)
        snapshot = view.snapshot
        active = [
            task for task in snapshot.tasks.values() if task.current_state.is_active
        ]
        ready = [
            task
            for task in active
            if task.current_state is TaskState.READY
            and view.evaluation.task_status(task.id).ready
        ]
        projects = list(snapshot.projects.values())
        open_projects = [p for p in projects if p.current_state.is_open]
        stalled = [p for p in projects if p.current_state is ProjectState.STALLED]
        scheduled = sorted(
            (task for task in active if task.has_schedule),
            key=lambda task: task.scheduled_start or datetime.max,
        )
        self.content.addWidget(self._hero(ready=len(ready), active=len(active), projects=len(open_projects)))
        self.content.addLayout(self._metrics(view, ready, open_projects, stalled, scheduled))
        if not snapshot.tasks and not snapshot.projects and not snapshot.conditions:
            self.content.addWidget(self._empty())
            self.content.addStretch(1)
            return
        grid = QHBoxLayout()
        grid.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._ready_card(ready, view))
        left.addWidget(self._schedule_card(scheduled))
        right = QVBoxLayout()
        right.setSpacing(16)
        right.addWidget(self._projects_card(open_projects))
        right.addWidget(self._health_card(view))
        grid.addLayout(left, 3)
        grid.addLayout(right, 2)
        self.content.addLayout(grid)
        self.content.addStretch(1)

    # ------------------------------------------------------------------ hero
    def _hero(self, *, ready: int, active: int, projects: int) -> QWidget:
        hero = QFrame()
        hero.setObjectName("HeroCard")
        hero.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        hero.setMinimumHeight(202)
        hero.setStyleSheet(
            "#HeroCard { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {rgba('accent', 0.16)}, stop:0.55 {c('surface')}, stop:1 {c('surface')}); "
            f"border: 1px solid {c('border')}; border-radius: 16px; }}"
        )
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(24)
        left = QVBoxLayout()
        left.setSpacing(8)
        eyebrow = QLabel(datetime.now().strftime("%A · %B %d").upper())
        eyebrow.setStyleSheet(f"color: {c('accent')}; font-size: 10px; font-weight: 700; letter-spacing: 2px;")
        left.addWidget(eyebrow)
        title = QLabel("The Big Picture")
        title.setStyleSheet(
            f"color: {c('text')}; font-family: 'Segoe UI Variable Display'; "
            "font-size: 29px; font-weight: 650;"
        )
        left.addWidget(title)
        description = QLabel("Task status and Project resolution is based on satisfying preconditions and prerequisites. ")
        description.setStyleSheet(f"color: {c('muted')}; font-size: 12px;")
        description.setWordWrap(True)
        left.addWidget(description)
        left.addSpacing(6)
        stats = QHBoxLayout()
        stats.setSpacing(22)
        for value, label, tone in (
            (str(ready), "ready now", "green"),
            (str(active), "active tasks", "task"),
            (str(projects), "open projects", "project"),
        ):
            block = QVBoxLayout()
            block.setSpacing(0)
            number = QLabel(value)
            number.setStyleSheet(
                f"color: {c(tone)}; font-family: 'Segoe UI Variable Display'; "
                "font-size: 21px; font-weight: 650;"
            )
            block.addWidget(number)
            text = QLabel(label)
            text.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
            block.addWidget(text)
            stats.addLayout(block)
        left.addLayout(stats)
        left.addStretch(1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        open_dashboard = QPushButton("Open dashboard")
        open_dashboard.setProperty("variant", "primary")
        open_dashboard.clicked.connect(self.open_dashboard)
        new_task = QPushButton("New task")
        new_task.clicked.connect(lambda: self.create_requested.emit(NodeKind.TASK))
        ask = QPushButton("Ask assistant")
        ask.clicked.connect(self.open_chat)
        actions.addWidget(open_dashboard)
        actions.addWidget(new_task)
        actions.addWidget(ask)
        actions.addStretch(1)
        left.addLayout(actions)
        layout.addLayout(left, 1)
        layout.addWidget(StatusRing(ready, max(active, 1)), 0, Qt.AlignmentFlag.AlignVCenter)
        return hero

    def _metrics(self, view, ready, projects, stalled, scheduled) -> QHBoxLayout:  # type: ignore[no-untyped-def]
        layout = QHBoxLayout()
        layout.setSpacing(14)
        blocked = sum(
            1
            for task in view.snapshot.tasks.values()
            if task.current_state is TaskState.BLOCKED
        )
        cards = [
            ("Ready tasks", str(len(ready)), "barriers fully cleared", "task", "task"),
            ("Open projects", str(len(projects)), f"{len(stalled)} stalled", "project", "project"),
            ("Scheduled blocks", str(len(scheduled)), "local timeline", "calendar", "accent"),
            ("Blocked tasks", str(blocked), "unresolved dependency", "lock", "amber"),
        ]
        for label, value, hint, icon_name, tone in cards:
            layout.addWidget(MetricCard(label, value, hint, icon_name, tone), 1)
        return layout

    # ------------------------------------------------------------- ready
    def _ready_card(self, ready, view) -> QWidget:  # type: ignore[no-untyped-def]
        frame = Card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        action = QPushButton("Open dashboard")
        action.setProperty("variant", "ghost")
        action.clicked.connect(self.open_dashboard)
        layout.addWidget(
            SectionHeader(
                "Ready now",
                "Tasks whose conjunctive dependency barrier is satisfied."
                if ready
                else "No task currently satisfies every dependency.",
                action,
            )
        )
        layout.addWidget(Divider())
        if not ready:
            empty = QLabel("No ready tasks. Resolve a precondition or complete a prerequisite.")
            empty.setProperty("role", "muted")
            empty.setWordWrap(True)
            empty.setContentsMargins(4, 14, 4, 14)
            layout.addWidget(empty)
            return frame
        for task in sorted(ready, key=lambda item: (
                item.scheduled_start is None,
                item.scheduled_start or datetime.max,
                item.name.casefold(),
            ))[:6]:
            row = ClickableRow()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 9, 8, 9)
            row_layout.setSpacing(10)
            mark = QLabel()
            mark.setPixmap(icon("task", c("task"), 16).pixmap(16, 16))
            mark.setFixedSize(28, 28)
            mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
            mark.setStyleSheet(f"background: {rgba('task', 0.08)}; border-radius: 8px;")
            row_layout.addWidget(mark)
            text = QVBoxLayout()
            text.setSpacing(1)
            title = QLabel(truncate(task.name, 54))
            title.setStyleSheet(f"color: {c('text')}; font-size: 12px; font-weight: 600;")
            text.addWidget(title)
            meta_parts = [schedule_span(task)]
            if task.has_custom_availability:
                meta_parts.append(availability_summary(task.availability))
            meta = QLabel(" · ".join(meta_parts))
            meta.setStyleSheet(f"color: {c('faint')}; font-size: 10px;")
            text.addWidget(meta)
            row_layout.addLayout(text, 1)
            row_layout.addWidget(Badge("READY", "ready"))
            row.clicked.connect(lambda _=False, tid=task.id: self.open_node.emit(NodeRef(NodeKind.TASK, tid)))
            layout.addWidget(row)
        layout.addStretch(1)
        return frame

    def _schedule_card(self, scheduled) -> QWidget:  # type: ignore[no-untyped-def]
        frame = Card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionHeader("Upcoming schedule", "Scheduled task blocks.", None))
        layout.addWidget(Divider())
        if not scheduled:
            empty = QLabel("No scheduled blocks. Open a task to save a schedule.")
            empty.setProperty("role", "muted")
            empty.setWordWrap(True)
            empty.setContentsMargins(4, 14, 4, 14)
            layout.addWidget(empty)
            return frame
        upcoming = [
            task
            for task in scheduled
            if task.scheduled_start and task.scheduled_start >= datetime.now() - timedelta(hours=1)
        ]
        display = (upcoming or scheduled)[:5]
        for task in display:
            row = ClickableRow()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 8, 8)
            row_layout.setSpacing(10)
            day = QVBoxLayout()
            day.setSpacing(0)
            date = QLabel(task.scheduled_start.strftime("%d") if task.scheduled_start else "—")
            date.setAlignment(Qt.AlignmentFlag.AlignCenter)
            date.setStyleSheet(
                f"color: {c('accent')}; font-family: 'Segoe UI Variable Display'; "
                "font-size: 17px; font-weight: 650;"
            )
            day.addWidget(date)
            month = QLabel(task.scheduled_start.strftime("%b").upper() if task.scheduled_start else "")
            month.setAlignment(Qt.AlignmentFlag.AlignCenter)
            month.setStyleSheet(f"color: {c('faint')}; font-size: 8px; font-weight: 700;")
            day.addWidget(month)
            row_layout.addLayout(day)
            text = QVBoxLayout()
            text.setSpacing(1)
            title = QLabel(truncate(task.name, 42))
            title.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px; font-weight: 600;")
            text.addWidget(title)
            when = QLabel(
                f"{task.scheduled_start.strftime('%H:%M') if task.scheduled_start else ''} – "
                f"{task.scheduled_end.strftime('%H:%M') if task.scheduled_end else ''}"
            )
            when.setStyleSheet(f"color: {c('faint')}; font-size: 10px;")
            text.addWidget(when)
            row_layout.addLayout(text, 1)
            row_layout.addWidget(Badge(task.current_state.label, task.current_state.value))

            row.clicked.connect(lambda _=False, tid=task.id: self.open_node.emit(NodeRef(NodeKind.TASK, tid)))
            layout.addWidget(row)
        layout.addStretch(1)
        return frame

    def _projects_card(self, projects) -> QWidget:  # type: ignore[no-untyped-def]
        frame = Card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionHeader("Project resolution", "Dependency gates by project.", None))
        layout.addWidget(Divider())
        if not projects:
            empty = QLabel("No open projects.")
            empty.setProperty("role", "muted")
            empty.setContentsMargins(4, 14, 4, 14)
            layout.addWidget(empty)
            return frame
        for project in sorted(projects, key=lambda item: item.name.casefold())[:6]:
            row = ClickableRow()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(9, 9, 8, 9)
            row_layout.setSpacing(9)
            mark = QLabel()
            mark.setPixmap(icon("project", c("project"), 16).pixmap(16, 16))
            mark.setFixedSize(28, 28)
            mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
            mark.setStyleSheet(f"background: {rgba('project', 0.08)}; border-radius: 8px;")
            row_layout.addWidget(mark)
            text = QVBoxLayout()
            text.setSpacing(1)
            title = QLabel(truncate(project.name, 36))
            title.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px; font-weight: 600;")
            text.addWidget(title)
            meta = QLabel(
                "Requirements Undefined"
                if project.resolution_conditions_unknown
                else f"{self.view.snapshot.dependency_count(NodeRef(NodeKind.PROJECT, project.id))} dependencies"
            )
            meta.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
            text.addWidget(meta)
            row_layout.addLayout(text, 1)
            row_layout.addWidget(Badge(project.current_state.label, project.current_state.value))
            row.clicked.connect(lambda _=False, pid=project.id: self.open_node.emit(NodeRef(NodeKind.PROJECT, pid)))
            layout.addWidget(row)
        layout.addStretch(1)
        return frame

    def _health_card(self, view) -> QWidget:  # type: ignore[no-untyped-def]
        frame = Card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addWidget(SectionHeader("Dependency health", "Three-valued facts and binary states.", None))
        layout.addWidget(Divider())
        values = list(view.evaluation.condition_values.values())
        true = sum(1 for value in values if value is True)
        false = sum(1 for value in values if value is False)
        unknown = len(values) - true - false
        total = max(len(values), 1)
        for label, count, tone in (("TRUE", true, "green"), ("FALSE", false, "red"), ("UNKNOWN", unknown, "muted")):
            row = QHBoxLayout()
            row.setSpacing(8)
            name = QLabel(label)
            name.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
            name.setFixedWidth(60)
            row.addWidget(name)
            track = QFrame()
            track.setFixedHeight(5)
            track.setStyleSheet(f"background: {c('surface_3')}; border-radius: 2px;")
            track_layout = QHBoxLayout(track)
            track_layout.setContentsMargins(0, 0, 0, 0)
            fill = QFrame()
            fill.setStyleSheet(f"background: {c(tone)}; border-radius: 2px;")
            fill.setFixedWidth(int(170 * count / total))
            track_layout.addWidget(fill)
            track_layout.addStretch(1)
            row.addWidget(track, 1)
            number = QLabel(str(count))
            number.setStyleSheet(f"color: {c(tone)}; font-size: 10px; font-weight: 700;")
            number.setFixedWidth(24)
            number.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(number)
            layout.addLayout(row)
        layout.addSpacing(2)
        snapshot = view.snapshot
        for label, value, tone in (
            ("Preconditions", sum(len(v) for v in snapshot.task_preconditions.values())
             + sum(len(v) for v in snapshot.project_preconditions.values()), "teal"),
            ("Prerequisites", sum(len(v) for v in snapshot.task_prerequisites.values())
             + sum(len(v) for v in snapshot.project_prerequisites.values()), "blue"),
            ("Undefined projects", sum(
                1 for project in snapshot.projects.values()
                if project.resolution_conditions_unknown
            ), "amber"),
            ("Audit events", self._event_count(), "faint"),
        ):
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {c(tone)}; font-size: 8px;")
            row.addWidget(dot)
            name = QLabel(label)
            name.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
            row.addWidget(name, 1)
            number = QLabel(str(value))
            number.setStyleSheet(f"color: {c('text_soft')}; font-size: 10px; font-weight: 600;")
            row.addWidget(number)
            layout.addLayout(row)
        layout.addStretch(1)
        return frame

    def _event_count(self) -> str:
        return "append-only"

    def _empty(self) -> QWidget:
        frame = Card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(12)
        title = QLabel("No workspace data yet")
        title.setProperty("role", "h2")
        layout.addWidget(title)
        copy = QLabel(
            "Start with a task, project, or external condition. Attach dependencies on "
            "the canvas or from the shared inspector."
        )
        copy.setProperty("role", "muted")
        copy.setWordWrap(True)
        layout.addWidget(copy)
        actions = QHBoxLayout()
        new_task = QPushButton("Create first task")
        new_task.setProperty("variant", "primary")
        new_task.clicked.connect(lambda: self.create_requested.emit(NodeKind.TASK))
        new_condition = QPushButton("Create condition")
        new_condition.clicked.connect(lambda: self.create_requested.emit(NodeKind.CONDITION))
        actions.addWidget(new_task)
        actions.addWidget(new_condition)
        actions.addStretch(1)
        layout.addLayout(actions)
        return frame


class DashboardPage(QWidget):
    create_requested = pyqtSignal(object)
    open_node = pyqtSignal(object)
    mutated = pyqtSignal(str)
    toast_requested = pyqtSignal(str, str)
    open_registry = pyqtSignal()

    def __init__(self, service: EFOSService, layout_store: LayoutStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.view: WorkspaceView | None = None
        self.selected_ref: NodeRef | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 18)
        root.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(12)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title = QLabel("Dashboard")
        title.setProperty("role", "h2")
        text_col.addWidget(title)
        subtitle = QLabel("Manage metadata in place, or map dependencies on the flat canvas.")
        subtitle.setProperty("role", "faint")
        text_col.addWidget(subtitle)
        # Add the text column without a stretch factor first
        top.addLayout(text_col)
        top.addSpacing(20)
        self.tabs = SegmentedControl(["Manage", "Flow"])
        # Inject standard sizing padding to scale them up beautifully
        self.tabs.setMinimumHeight(36)
        self.tabs.setMinimumWidth(160)
        self.tabs.setStyleSheet("""
            QPushButton {
                font-size: 13px;
                font-weight: bold;
                padding: 6px 16px;
            }
        """ + self.tabs.styleSheet())
        self.tabs.changed.connect(self._switch_tab)
        top.addWidget(self.tabs)
        # Finally, add an empty stretch factor at the far end to push the remaining empty space to the right
        top.addStretch(1)
        root.addLayout(top)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.manage = self._build_manage()
        self.flow = FlowWorkspace(service, layout_store)
        self.flow.create_requested.connect(self.create_requested)
        self.flow.mutated.connect(self._flow_mutated)
        self.flow.toast_requested.connect(self.toast_requested)
        self.stack.addWidget(self.manage)
        self.stack.addWidget(self.flow)

    # ---------------------------------------------------------------- manage
    def _build_manage(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        left_shell = QWidget()
        left_layout = QVBoxLayout(left_shell)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        actions = QHBoxLayout()
        actions.setSpacing(7)
        for kind, label, icon_name in (
            (NodeKind.TASK, "New task", "task"),
            (NodeKind.PROJECT, "New project", "project"),
            (NodeKind.CONDITION, "New condition", "condition"),
        ):
            button = QPushButton(label)
            button.setIcon(icon(icon_name, c("muted"), 14))
            button.clicked.connect(lambda _=False, k=kind: self.create_requested.emit(k))
            actions.addWidget(button)
        actions.addStretch(1)
        left_layout.addLayout(actions)
        self.manage_scroll = QScrollArea()
        self.manage_scroll.setWidgetResizable(True)
        self.manage_host = QWidget()
        self.manage_content = QVBoxLayout(self.manage_host)
        self.manage_content.setContentsMargins(2, 2, 8, 8)
        self.manage_content.setSpacing(14)
        self.manage_scroll.setWidget(self.manage_host)
        left_layout.addWidget(self.manage_scroll, 1)
        splitter.addWidget(left_shell)
        self.manage_inspector = InspectorPanel(self.service)
        self.manage_inspector.mutated.connect(self.mutated)
        self.manage_inspector.toast_requested.connect(self.toast_requested)
        splitter.addWidget(self.manage_inspector)
        splitter.setSizes([820, 360])
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter)
        return page

    def refresh(self, view: WorkspaceView) -> None:
        self.view = view
        self._populate_manage()
        self.flow.refresh(view)
        if self.selected_ref is not None:
            hidden = (
                self.selected_ref.kind is NodeKind.TASK
                and self.selected_ref.id in view.snapshot.tasks
                and view.snapshot.tasks[self.selected_ref.id].current_state.is_terminal
            )
            if view.snapshot.exists(self.selected_ref) and not hidden:
                self.manage_inspector.set_view(view)
            else:
                self.selected_ref = None
                self.manage_inspector.set_ref(None, view)
        else:
            self.manage_inspector.set_ref(None, view)

    def _populate_manage(self) -> None:
        if self.view is None:
            return
        _clear_layout(self.manage_content)
        snapshot = self.view.snapshot
        active_tasks = sorted(
            (task for task in snapshot.tasks.values() if task.current_state.is_active),
            key=lambda task: (task.name.casefold()),
        )
        projects = sorted(snapshot.projects.values(), key=lambda item: item.name.casefold())
        conditions = sorted(snapshot.conditions.values(), key=lambda item: item.display_name.casefold())
        self.manage_content.addWidget(
            self._group(
                "Tasks",
                f"{len(active_tasks)} active · completed and deferred records live in the Registry",
                [
                    self._record_row(
                        NodeRef(NodeKind.TASK, task.id),
                        task.name,
                        "task",
                        task.current_state.value,
                        self._task_meta(task),
                    )
                    for task in active_tasks
                ],
                empty="No active tasks.",
            )
        )
        self.manage_content.addWidget(
            self._group(
                "Projects",
                f"{len(projects)} projects · stalled and shelved require explicit action",
                [
                    self._record_row(
                        NodeRef(NodeKind.PROJECT, project.id),
                        project.name,
                        "project",
                        project.current_state.value,
                        "Requirements Undefined"
                        if project.resolution_conditions_unknown
                        else f"{snapshot.dependency_count(NodeRef(NodeKind.PROJECT, project.id))} dependencies",
                    )
                    for project in projects
                ],
                empty="No projects.",
            )
        )
        self.manage_content.addWidget(
            self._group(
                "Conditions",
                f"{len(conditions)} external facts · TRUE, FALSE, or UNKNOWN",
                [
                    self._record_row(
                        NodeRef(NodeKind.CONDITION, condition.id),
                        condition.display_name,
                        "condition",
                        self.view.evaluation.condition_value(condition.id),
                        "External precondition"
                        if condition.evaluation_state is not None
                        else "Unknown / NULL",
                    )
                    for condition in conditions
                ],
                empty="No conditions.",
            )
        )
        self.manage_content.addStretch(1)

    @staticmethod
    def _task_meta(task) -> str:  # type: ignore[no-untyped-def]
        parts = [schedule_span(task)]
        if task.has_custom_availability:
            parts.append(availability_summary(task.availability))
        return " · ".join(parts)

    def _group(self, title: str, subtitle: str, rows: list[QWidget], *, empty: str) -> QWidget:
        frame = Card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(SectionHeader(title, subtitle, None))
        layout.addWidget(Divider())
        if not rows:
            label = QLabel(empty)
            label.setProperty("role", "muted")
            label.setContentsMargins(4, 10, 4, 10)
            layout.addWidget(label)
        for row in rows:
            layout.addWidget(row)
        return frame

    def _record_row(self, ref: NodeRef, name: str, icon_name: str, state, meta: str) -> QWidget:
        row = ClickableRow()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(9)
        mark = QLabel()
        mark.setPixmap(icon(icon_name, c(icon_name), 15).pixmap(15, 15))
        mark.setFixedSize(26, 26)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(f"background: {rgba(icon_name, 0.08)}; border-radius: 7px;")
        layout.addWidget(mark)
        text = QVBoxLayout()
        text.setSpacing(0)
        title = QLabel(truncate(name, 62))
        title.setStyleSheet(
            f"color: {c('text')}; font-size: 12px; "
            f"font-weight: {'700' if self.selected_ref == ref else '600'};"
        )
        text.addWidget(title)
        sub = QLabel(meta)
        sub.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
        text.addWidget(sub)
        layout.addLayout(text, 1)
        if ref.kind is NodeKind.CONDITION:
            from ..logic import condition_truth_label

            label = condition_truth_label(state)
            tone = "true" if state is True else ("false" if state is False else "unknown")
            layout.addWidget(Badge(label, tone))
        else:
            layout.addWidget(Badge(str(state).upper(), str(state)))
        if self.selected_ref == ref:
            row.setStyleSheet(
                f"ClickableRow {{ background: {rgba('accent', 0.10)}; "
                f"border: 1px solid {rgba('accent', 0.30)}; border-radius: 9px; }}"
            )
        row.clicked.connect(lambda _=False, r=ref: self.select_manage(r))
        return row

    # ------------------------------------------------------------------- tabs
    def _switch_tab(self, index: int) -> None:
        self.manage_inspector.commit_note()
        self.flow.flush_notes()
        self.stack.setCurrentIndex(index)

    def show_manage(self) -> None:
        self.tabs.select(0, emit=False)
        self.stack.setCurrentIndex(0)

    def show_flow(self) -> None:
        self.tabs.select(1, emit=False)
        self.stack.setCurrentIndex(1)

    def select_manage(self, ref: NodeRef) -> None:
        if self.view is None:
            return
        if not self.manage_inspector.commit_note():
            return
        if not self.view.snapshot.exists(ref):
            self.toast_requested.emit("That record no longer exists.", "error")
            return
        self.selected_ref = ref
        self.show_manage()
        self._populate_manage()
        self.manage_inspector.set_ref(ref, self.view)

    def select_flow(self, ref: NodeRef) -> bool:
        self.show_flow()
        return self.flow.select_ref(ref)

    def _flow_mutated(self, message: str) -> None:
        self.mutated.emit(message)

    def flush_notes(self) -> bool:
        manage_ok = self.manage_inspector.commit_note()
        flow_ok = self.flow.flush_notes()
        return manage_ok and flow_ok

    def shutdown(self) -> None:
        self.manage_inspector.commit_note(announce=False)
        self.flow.shutdown()
