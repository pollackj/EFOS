"""Shared metadata inspector used by Manage, Registry, and the Flow canvas.

One component owns property editing, dependency attachment/removal, recurrence,
schedules, notes (blur-save), lifecycle actions, and the canvas gesture guide.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from PyQt6.QtCore import QDateTime, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..errors import EFOSError
from ..formatting import (
    fmt_datetime,
    fmt_relative,
    recurrence_span,
    schedule_span,
    truncate,
)
from ..logic import (availability_state_text, availability_summary, condition_truth_label, gate_summary)
from ..models import (
    Condition,
    Edge,
    EdgeKind,
    NodeKind,
    NodeRef,
    Project,
    ProjectState,
    Task,
    TaskState,
)
from ..service import EFOSService, WorkspaceView
from .dialogs import (AvailabilityDialog, RecurrenceDialog, ScheduleConflictDialog, confirm_dialog)
from .icons import icon
from .theme import c, rgba, state_color
from .widgets import Badge, Divider, GreenCheckBox, IconButton, SectionLabel

def _clear_layout(layout) -> None:  # type: ignore[no-untyped-def]
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif child is not None: _clear_layout(child)

class NoteEdit(QPlainTextEdit):
    focus_lost = pyqtSignal()

    def focusOutEvent(self, event):  # type: ignore[no-untyped-def]
        super().focusOutEvent(event)
        self.focus_lost.emit()

class InspectorPanel(QFrame):
    mutated = pyqtSignal(str)
    toast_requested = pyqtSignal(str, str)
    closed = pyqtSignal()

    def __init__(
        self,
        service: EFOSService,
        *,
        docs: bool = False,
        closeable: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.service = service
        self.docs = docs
        self.closeable = closeable
        self.view: WorkspaceView | None = None
        self.ref: NodeRef | None = None
        self._note_edit: NoteEdit | None = None
        self.name_edit: QLineEdit | None = None
        self._saved_note = ""
        self._note_dirty = False
        self._loading = False
        self._schedule_start: QDateTimeEdit | None = None
        self._schedule_end: QDateTimeEdit | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # The inspector owns vertical scrolling only. A long dependency title
        # must never create a horizontal scrollbar or push the Attach button
        # outside the panel, so horizontal scrolling is disabled entirely and
        # the inner host is allowed to shrink to the viewport width.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.scroll)
        self.host = QWidget()
        self.host.setMinimumWidth(0)
        self.host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.content = QVBoxLayout(self.host)
        self.content.setContentsMargins(18, 16, 18, 18)
        self.content.setSpacing(12)
        # The default constraint lets the host shrink horizontally while the
        # layout's minimum height still drives the vertical scrollbar.
        self.content.setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)
        self.scroll.setWidget(self.host)
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        self._empty()

    # ------------------------------------------------------------------ state
    def _empty(self) -> None:
        _clear_layout(self.content)
        self.name_edit = None
        self._note_edit = None
        self._note_dirty = False
        self.content.addSpacing(40)
        mark = QLabel()
        mark.setPixmap(icon("task", c("faint"), 30).pixmap(30, 30))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content.addWidget(mark)
        title = QLabel("No record selected")
        title.setProperty("role", "h2")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setMinimumWidth(0)
        self.content.addWidget(title)
        hint = _wrap_label(QLabel("Select a task, project, or condition to inspect and edit its metadata."))
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content.addWidget(hint)
        if self.docs: self._render_docs()
        self.content.addStretch(1)

    def set_ref(self, ref: NodeRef | None, view: WorkspaceView | None = None) -> None:
        if ref is None:
            self.commit_note()
            self.ref = None
            self._empty()
            return
        if self.ref == ref and view is not None and self.view is not view:
            if self._note_edit is not None and self._note_edit.hasFocus():
                self.view = view
                return
        if self.ref != ref: self.commit_note()
        self.ref = ref
        if view is not None: self.view = view
        self.render()

    def set_view(self, view: WorkspaceView, ref: NodeRef | None = None) -> None:
        if ref is not None and ref != self.ref:
            self.set_ref(ref, view)
            return
        self.view = view
        if self._note_edit is not None and self._note_edit.hasFocus(): return
        if self._note_dirty: self.commit_note(announce=False)
        if self.name_edit is not None and self.name_edit.hasFocus(): return
        if self.ref is None: self._empty()
        else: self.render()

    # ------------------------------------------------------------------ notes
    def has_pending_note(self) -> bool:
        return bool(self._note_dirty and self._note_edit is not None)

    def commit_note(self, *, announce: bool = True) -> bool:
        if self.ref is None or self._note_edit is None: return True
        if not self._note_dirty: return True
        text = self._note_edit.toPlainText()
        if not text.strip():
            # Empty edits never erase historical content. Restore the saved block.
            self._loading = True
            self._note_edit.setPlainText(self._saved_note)
            self._loading = False
            self._note_dirty = False
            if announce:
                self.toast_requested.emit("Empty note edits are not saved. Use the trash icon to delete.", "info")
            return True
        try: saved = self.service.save_note(self.ref, text)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return False
        if saved:
            self._saved_note = text
            self._note_dirty = False
            if announce:
                self.toast_requested.emit("Note saved", "success")
        return True

    def _note_changed(self) -> None:
        if not self._loading: self._note_dirty = True

    def _delete_note(self) -> None:
        if self.ref is None: return
        if not confirm_dialog(self, "Delete note?", "The saved note content will be removed permanently."): return
        try: self.service.delete_note(self.ref)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._saved_note = ""
        self._note_dirty = False
        if self._note_edit is not None:
            self._loading = True
            self._note_edit.setPlainText("")
            self._loading = False
        self.mutated.emit("Note deleted")

    # ------------------------------------------------------------------ render
    def render(self) -> None:
        if self.ref is None or self.view is None:
            self._empty()
            return
        if self._note_edit is not None and self._note_edit.hasFocus(): return
        if self._note_dirty: self.commit_note(announce=False)
        self._loading = True
        try:
            _clear_layout(self.content)
            self.name_edit = None
            self._note_edit = None
            self._note_dirty = False
            if self.ref.kind is NodeKind.TASK: self._render_task()
            elif self.ref.kind is NodeKind.PROJECT: self._render_project()
            else: self._render_condition()
            self._render_dependencies()
            self._render_notes()
            if self.docs: self._render_docs()
            # Destructive actions always live at the very bottom, below NOTE.
            self._render_delete_for_ref()
            self.content.addStretch(1)
        finally: self._loading = False

    # ------------------------------------------------------------------ header
    def _header(self, title: str, kind_text: str, state: str | None, tone: str) -> QWidget:
        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        top = QHBoxLayout()
        top.setSpacing(8)
        eyebrow = QLabel(kind_text)
        eyebrow.setProperty("role", "eyebrow")
        top.addWidget(eyebrow)
        top.addStretch(1)
        if self.closeable:
            close = IconButton("close", "Close inspector", size=14)
            close.setFixedSize(24, 24)
            close.clicked.connect(self.closed)
            top.addWidget(close)
        layout.addLayout(top)
        self.name_label = _wrap_label(QLabel(title))
        self.name_label.setCursor(QCursor(Qt.CursorShape.IBeamCursor))
        self.name_label.setToolTip("Click to rename")
        self.name_label.setStyleSheet(
            f"QLabel {{ background: transparent; border: 1px solid transparent; border-radius: 6px; "
            f"color: {c('text')}; font-size: 17px; font-weight: 650; padding: 3px 4px; }}"
            f"QLabel:hover {{ border-color: {c('border')}; }}"
        )
        self.name_edit = QLineEdit(title)
        self.name_edit.setStyleSheet(
            f"QLineEdit {{ background: {c('input')}; border: 1px solid {c('accent')}; "
            f"color: {c('text')}; font-size: 17px; font-weight: 650; padding: 3px 4px; }}"
        )
        self.name_edit.hide()

        def _start_rename(event) -> None:  # type: ignore[no-untyped-def]
            if self.name_edit is not None:
                self.name_label.hide()
                self.name_edit.show()
                self.name_edit.setFocus()
                self.name_edit.selectAll()
            event.accept()

        self.name_label.mousePressEvent = _start_rename  # type: ignore[method-assign]
        self.name_edit.editingFinished.connect(self._rename)
        layout.addWidget(self.name_label)
        layout.addWidget(self.name_edit)

        if state is not None:
            row = QHBoxLayout()
            row.setSpacing(6)
            badge = Badge(state.upper(), tone)
            row.addWidget(badge)
            row.addStretch(1)
            layout.addLayout(row)
        return frame

    def _rename(self) -> None:
        if self.ref is None or self.view is None or self.name_edit is None: return
        name = self.name_edit.text().strip()
        current = self.view.snapshot.get_name(self.ref)
        if self.name_edit is not None and self.name_edit.isVisible():
            self.name_edit.hide()
            self.name_label.show()
        if name == current: return
        if not name:
            self.name_edit.setText(current)
            self.toast_requested.emit("Name cannot be empty.", "error")
            return
        self.commit_note()
        try:
            if self.ref.kind is NodeKind.TASK:
                task = self.view.snapshot.tasks[self.ref.id]
                self.service.update_task(self.ref.id, name=name, is_recurring=task.is_recurring)
            elif self.ref.kind is NodeKind.PROJECT: self.service.update_project(self.ref.id, name=name)
            else:
                condition = self.view.snapshot.conditions[self.ref.id]
                self.service.update_condition(self.ref.id, display_name=name, evaluation_state=condition.evaluation_state)
        except EFOSError as exc:
            self.name_edit.setText(current)
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Record renamed")

    # ------------------------------------------------------------------ task
    def _render_task(self) -> None:
        assert self.ref is not None and self.view is not None
        task = self.view.snapshot.tasks.get(self.ref.id)
        if task is None:
            self._empty()
            return
        self.content.addWidget(self._header(task.name, "TASK", task.current_state.value, task.current_state.value))

        status = self.view.evaluation.task_status(task.id)
        if task.current_state is TaskState.BLOCKED:
            if status.ready:
                msg = (
                    "Blocked · no unsatisfied dependency was found. Run Recompute to "
                    "re-derive the Task's Status."
                )
            elif status.blockers:
                msg = "Blocked · " + "; ".join(status.blockers[:3])
                if len(status.blockers) > 3:
                    msg += f"; and {len(status.blockers) - 3} more"
            else: msg = "Blocked · an unsatisfied dependency prevents Ready Status."
            lock = _wrap_label(QLabel(msg))
            lock.setStyleSheet(
                f"color: {c('amber')}; background: {rgba('amber', 0.07)}; "
                f"border: 1px solid {rgba('amber', 0.22)}; border-radius: 8px; "
                "padding: 8px 10px; font-size: 10px;"
            )
            self.content.addWidget(lock)

        now = datetime.now()
        if task.has_custom_availability:
            availability_line = (
                f"{availability_summary(task.availability)} · "
                f"{availability_state_text(task.availability, now)}"
            )
        else: availability_line = "24/7 — always available"
        meta = [
            ("Status", task.current_state.label),
            ("Availability", availability_line),
            ("Schedule", schedule_span(task)),
            ("Recurrence", recurrence_span(task)),
            ("Requirements", gate_summary(self.view.snapshot, self.view.evaluation, self.ref)),
            ("Created", fmt_relative(task.created_at)),
        ]
        for key, value in meta: self.content.addWidget(_kv(key, value))

        if task.current_state.is_active:
            availability_row = QHBoxLayout()
            availability_row.setSpacing(7)
            edit_availability = QPushButton("Edit availability…")
            edit_availability.clicked.connect(
                lambda _=False, task_id=task.id: self._edit_availability(task_id)
            )
            availability_row.addWidget(edit_availability)
            availability_row.addStretch(1)
            self.content.addLayout(availability_row)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        if task.current_state is TaskState.READY:
            complete = QPushButton("Complete task")
            complete.setProperty("variant", "primary")
            complete.setIcon(icon("check", "#FFFFFF", 14))
            complete.setEnabled(status.ready)
            complete.clicked.connect(lambda: self._complete_task(task.id))
            actions.addWidget(complete)
        if task.current_state.is_active:
            defer = QPushButton("Defer task")
            defer.clicked.connect(lambda: self._defer_task(task.id))
            actions.addWidget(defer)
        if task.current_state in (TaskState.COMPLETED, TaskState.DEFERRED):
            reopen = QPushButton("Reopen")
            reopen.setProperty("variant", "primary")
            reopen.clicked.connect(lambda: self._reopen_task(task.id))
            actions.addWidget(reopen)
        actions.addStretch(1)
        self.content.addLayout(actions)

        if task.current_state.is_active:
            self._render_schedule(task)
        else:
            self._render_recurrence(task)

    def _render_recurrence_controls(self, task: Task) -> None:
        self.content.addSpacing(4)
        self.content.addWidget(_sub_label("RECURRENCE"))
        row = QHBoxLayout()
        row.setSpacing(8)
        check = GreenCheckBox("Recurring")
        check.setChecked(task.recurrence is not None)

        # Render a crisp green checkmark inside the indicator when checked
        if task.recurrence is not None:
            from PyQt6.QtCore import QBuffer, QIODevice
            import base64
            pm = icon("check", c("green"), 12).pixmap(12, 12)
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            pm.save(buf, "PNG")
            b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
            check.setStyleSheet(
                check.styleSheet()
                + f"QCheckBox::indicator:checked {{ image: url(data:image/png;base64,{b64}); }}"
            )
        check.toggled.connect(
            lambda checked, task_id=task.id: self._toggle_recurring(task_id, checked)
        )
        row.addWidget(check)
        row.addStretch(1)
        edit = QPushButton("Edit recurrence…" if task.recurrence else "Set recurrence…")
        edit.clicked.connect(
            lambda _=False, task_id=task.id: self._edit_recurrence(task_id)
        )
        row.addWidget(edit)
        self.content.addLayout(row)
        summary = _wrap_label(
            QLabel(task.recurrence.summary() if task.recurrence else "Not recurring")
        )
        summary.setStyleSheet(f"color: {c('faint')}; font-size: 10px;")
        self.content.addWidget(summary)

    def _render_recurrence(self, task: Task) -> None:
        self.content.addWidget(Divider())
        self._render_recurrence_controls(task)

    def _toggle_recurring(self, task_id: str, checked: bool) -> None:
        if self._loading:
            return
        task = self.view.snapshot.tasks.get(task_id) if self.view else None
        if task is None:
            return
        if checked and task.recurrence is None:
            self._edit_recurrence(task_id, force_new=True)
            return
        if not checked and task.recurrence is not None:
            if not confirm_dialog(
                self,
                "Remove recurrence?",
                "The stored recurrence rule will be deleted.",
                confirm_text="Remove",
            ):
                if self.view is not None:
                    self.render()
                return
            try:
                self.service.delete_rule(task_id)
            except EFOSError as exc:
                self.toast_requested.emit(str(exc), "error")
                return
            self.mutated.emit("Recurrence removed")

    def _edit_recurrence(self, task_id: str, *, force_new: bool = False) -> None:
        rule = None if force_new else self.service.get_rule(task_id)
        start, end = self._read_schedule()
        if start is None or end is None:
            task = self.view.snapshot.tasks.get(task_id) if self.view else None
            if task and task.scheduled_start and task.scheduled_end:
                start, end = task.scheduled_start, task.scheduled_end
            else:
                start = datetime.now().replace(minute=0, second=0, microsecond=0)
                end = start + timedelta(hours=1)
        if end <= start:
            self.toast_requested.emit(
                "Schedule End must be after Start before setting recurrence.", "error"
            )
            if self.view is not None:
                self.render()
            return
        dialog = RecurrenceDialog(rule, self, window_start=start, window_end=end)
        if not dialog.exec():
            if self.view is not None:
                self.render()
            return
        values = dialog.values()
        try:
            if values["removed"]:
                self.service.delete_rule(task_id)
                self.mutated.emit("Recurrence removed")
                return
            # Ensure the unified Schedule window is saved alongside the recurrence rule
            self.service.save_task_schedule(task_id, start, end)
            self.service.save_rule(
                task_id,
                frequency=values["frequency"],
                interval=values["interval"],
                by_day=values["by_day"],
                by_month_day=values["by_month_day"],
                occurrence_count=values["occurrence_count"],
                until_date=values["until_date"],
                reset_hour=start.hour,
                reset_minute=start.minute,
                end_hour=end.hour,
                end_minute=end.minute,
            )
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Recurrence saved")

    def _edit_availability(self, task_id: str) -> None:
        task = self.view.snapshot.tasks.get(task_id) if self.view else None
        if task is None:
            return
        self.commit_note()
        dialog = AvailabilityDialog(task.availability, self)
        if not dialog.exec():
            return
        try:
            self.service.save_task_availability(task_id, dialog.values())
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Availability saved · Status unchanged")

    def _render_schedule(self, task: Task) -> None:
        self.content.addWidget(Divider())
        self.content.addWidget(SectionLabel("Schedule"))
        self._schedule_start = QDateTimeEdit()
        self._schedule_end = QDateTimeEdit()
        for edit in (self._schedule_start, self._schedule_end):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("MMM d, yyyy HH:mm")
            edit.setMinimumHeight(34)
        start = task.scheduled_start or datetime.now().replace(minute=0, second=0, microsecond=0)
        end = task.scheduled_end or (start + timedelta(hours=1))
        self._schedule_start.setDateTime(QDateTime(start))
        self._schedule_end.setDateTime(QDateTime(end))
        self.content.addWidget(_kv_widget("Start", self._schedule_start))
        self.content.addWidget(_kv_widget("End", self._schedule_end))

        # Nested RECURRENCE section inside SCHEDULE
        self._render_recurrence_controls(task)

        self.content.addSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(7)
        save = QPushButton("Save schedule")
        save.setProperty("variant", "primary")
        save.clicked.connect(lambda _=False, task_id=task.id: self._save_schedule(task_id))
        row.addWidget(save)
        clear = QPushButton("Clear")
        clear.setEnabled(task.has_schedule)
        clear.clicked.connect(lambda _=False, task_id=task.id: self._clear_schedule(task_id))
        row.addWidget(clear)
        row.addStretch(1)
        self.content.addLayout(row)
        hint = _wrap_label(
            QLabel(
                "Collisions with local tasks and the synchronized Google cache are "
                "checked before saving; Force Save preserves the local block."
            )
        )
        hint.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
        self.content.addWidget(hint)

    def _read_schedule(self) -> tuple[datetime | None, datetime | None]:
        if not self._schedule_start or not self._schedule_end:
            return None, None
        return (
            self._schedule_start.dateTime().toPyDateTime(),
            self._schedule_end.dateTime().toPyDateTime(),
        )

    def _save_schedule(self, task_id: str) -> None:
        start, end = self._read_schedule()
        if start is None or end is None:
            return
        if end <= start:
            self.toast_requested.emit("Schedule end must be after its start.", "error")
            return
        try:
            conflicts = self.service.schedule_collisions(
                start, end, exclude_task_id=task_id
            )
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        if conflicts:
            dialog = ScheduleConflictDialog(conflicts, self)
            if not dialog.exec() or not dialog.force:
                return
        try:
            self.service.save_task_schedule(task_id, start, end)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Schedule saved")

    def _clear_schedule(self, task_id: str) -> None:
        try:
            self.service.save_task_schedule(task_id, None, None)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Schedule cleared")

    def _complete_task(self, task_id: str) -> None:
        self.commit_note()
        try:
            self.service.complete_task(task_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Task completed")

    def _defer_task(self, task_id: str) -> None:
        self.commit_note()
        try:
            self.service.defer_task(task_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Task deferred")

    def _reopen_task(self, task_id: str) -> None:
        try:
            self.service.reopen_task(task_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Task reopened · dependencies re-evaluated")

    # --------------------------------------------------------------- project
    def _render_project(self) -> None:
        assert self.ref is not None and self.view is not None
        project = self.view.snapshot.projects.get(self.ref.id)
        if project is None:
            self._empty()
            return
        self.content.addWidget(
            self._header(
                project.name,
                "PROJECT",
                project.current_state.value,
                project.current_state.value,
            )
        )
        undefined = project.resolution_conditions_unknown
        if undefined:
            banner = _wrap_label(
                QLabel(
                    "Requirements Undefined — automated resolution is blocked until a "
                    "valid precondition or prerequisite is attached."
                )
            )
            banner.setStyleSheet(
                f"color: {c('amber')}; background: {rgba('amber', 0.07)}; "
                f"border: 1px solid {rgba('amber', 0.24)}; border-radius: 8px; "
                "padding: 8px 10px; font-size: 10px;"
            )
            self.content.addWidget(banner)
        else:
            defined = _wrap_label(
                QLabel(
                    "Requirements defined · automated resolution evaluates the "
                    "conjunctive dependency gate."
                )
            )
            defined.setStyleSheet(
                f"color: {c('green')}; background: {rgba('green', 0.06)}; "
                f"border: 1px solid {rgba('green', 0.20)}; border-radius: 8px; "
                "padding: 7px 10px; font-size: 10px;"
            )
            self.content.addWidget(defined)

        status = self.view.evaluation.project_status(project.id)
        # An undefined Project has no dependency gate to summarize. Showing the
        # generic "satisfied by default" line would contradict the amber banner.
        requirements = (
            "Undefined · dependency required"
            if undefined
            else gate_summary(self.view.snapshot, self.view.evaluation, self.ref)
        )
        satisfied = "—" if undefined else f"{status.satisfied} of {status.total}"
        meta = [
            ("Status", project.current_state.label),
            ("Requirements", requirements),
            ("Satisfied", satisfied),
            ("Created", fmt_relative(project.created_at)),
        ]
        for key, value in meta:
            self.content.addWidget(_kv(key, value))

        actions = QHBoxLayout()
        actions.setSpacing(7)
        if project.current_state in (ProjectState.ACTIVE, ProjectState.STALLED):
            shelve = QPushButton("Shelve project")
            shelve.clicked.connect(lambda _=False, pid=project.id: self._shelve(pid))
            actions.addWidget(shelve)
        if project.current_state is ProjectState.ACTIVE:
            stall = QPushButton("Mark stalled")
            stall.clicked.connect(lambda _=False, pid=project.id: self._stall(pid))
            actions.addWidget(stall)
        if project.current_state in (ProjectState.SHELVED, ProjectState.STALLED):
            activate = QPushButton("Reactivate" if project.current_state is ProjectState.STALLED else "Unshelve")
            activate.setProperty("variant", "primary")
            activate.clicked.connect(lambda _=False, pid=project.id: self._activate(pid, project.current_state))
            actions.addWidget(activate)
        actions.addStretch(1)
        self.content.addLayout(actions)

    def _shelve(self, project_id: str) -> None:
        self.commit_note()
        try:
            self.service.shelve_project(project_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Project shelved")

    def _stall(self, project_id: str) -> None:
        try:
            self.service.stall_project(project_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Project marked stalled")

    def _activate(self, project_id: str, state: ProjectState) -> None:
        try:
            if state is ProjectState.SHELVED:
                self.service.unshelve_project(project_id)
            else:
                self.service.reactivate_project(project_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Project reactivated · dependencies re-evaluated")

    # ------------------------------------------------------------- condition
    def _render_condition(self) -> None:
        assert self.ref is not None and self.view is not None
        condition = self.view.snapshot.conditions.get(self.ref.id)
        if condition is None:
            self._empty()
            return
        value = self.view.evaluation.condition_value(condition.id)
        self.content.addWidget(
            self._header(
                condition.display_name,
                "CONDITION · EXTERNAL FACT",
                condition_truth_label(value),
                "true" if value is True else ("false" if value is False else "unknown"),
            )
        )
        self.content.addWidget(_kv("Value", condition_truth_label(value)))
        self.content.addWidget(_kv("Created", fmt_relative(condition.created_at)))
        attached = self.view.snapshot.attached_preconditions(condition.id)
        self.content.addWidget(_kv("Attached targets", str(len(attached))))

        self.content.addWidget(SectionLabel("Manual evaluation"))
        row = QHBoxLayout()
        row.setSpacing(6)
        for label, tone, target in (
            ("TRUE", "green", True),
            ("FALSE", "red", False),
            ("UNKNOWN", "muted", None),
        ):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(value is target)
            button.setStyleSheet(
                f"QPushButton {{ color: {c(tone)}; font-weight: 700; font-size: 10px; }}"
                f"QPushButton:checked {{ background: {rgba(tone, 0.14)}; "
                f"border-color: {c(tone)}; }}"
            )
            button.clicked.connect(
                lambda _=False, cid=condition.id, v=target: self._set_condition(cid, v)
            )
            row.addWidget(button)
        row.addStretch(1)
        self.content.addLayout(row)

        if attached:
            self.content.addWidget(SectionLabel("Attached targets"))
            for target in attached:
                row_frame = QFrame()
                row_layout = QHBoxLayout(row_frame)
                row_layout.setContentsMargins(0, 2, 0, 2)
                row_layout.setSpacing(6)
                name = _wrap_label(
                    QLabel(
                        f"{target.kind.label} · "
                        f"{truncate(self.view.snapshot.get_name(target), 28)}"
                    )
                )
                name.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px;")
                row_layout.addWidget(name, 1)
                remove = IconButton("close", "Detach precondition", size=12)
                remove.setFixedSize(24, 24)
                remove.clicked.connect(
                    lambda _=False, t=target, cid=condition.id: self._remove_precondition(t, cid)
                )
                row_layout.addWidget(remove)
                self.content.addWidget(row_frame)

    def _set_condition(self, condition_id: str, value: bool | None) -> None:
        try:
            self.service.set_condition_value(condition_id, value)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Condition value updated")

    # ------------------------------------------------------------ dependencies
    def _render_dependencies(self) -> None:
        if self.ref is None or self.view is None or self.ref.kind is NodeKind.CONDITION:
            return
        snapshot = self.view.snapshot
        evaluation = self.view.evaluation
        editable = True
        if self.ref.kind is NodeKind.TASK:
            task = snapshot.tasks.get(self.ref.id)
            editable = bool(task and task.current_state.is_active)
        else:
            project = snapshot.projects.get(self.ref.id)
            editable = bool(project and project.current_state is not ProjectState.RESOLVED)

        undefined_project = False
        if self.ref.kind is NodeKind.PROJECT:
            project = snapshot.projects.get(self.ref.id)
            undefined_project = bool(project and project.resolution_conditions_unknown)

        count = snapshot.dependency_count(self.ref)
        # An undefined Project has no dependency definition at all. A plain em
        # dash matches the "Satisfied: —" metadata row above; the ∅ glyph
        # renders as a missing-glyph box in Segoe UI's uppercase SectionLabel.
        header_count = "\u2014" if undefined_project else str(count)
        self.content.addWidget(Divider())
        self.content.addWidget(SectionLabel(f"Dependencies · {header_count}"))
        if not editable:
            self.content.addWidget(
                _empty_line(
                    "Historical record · dependencies are read-only. Reopen or "
                    "reactivate the record to change its requirements."
                )
            )

        preconditions = sorted(
            snapshot.preconditions_for(self.ref),
            key=lambda cid: snapshot.conditions[cid].display_name.casefold()
            if cid in snapshot.conditions
            else cid,
        )
        prerequisites = sorted(
            snapshot.prerequisites_for(self.ref),
            key=lambda source: (
                source.kind.value,
                snapshot.get_name(source).casefold(),
            ),
        )

        # Sub-headers only exist when their group actually has attached items.
        if preconditions:
            self.content.addWidget(_sub_label("PRECONDITIONS · 3VL EXTERNAL FACTS"))
            for condition_id in preconditions:
                condition = snapshot.conditions.get(condition_id)
                if condition is None:
                    continue
                status = evaluation.for_ref(self.ref)
                value = status.precondition_values.get(condition_id) if status else None
                row = QFrame()
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 2, 0, 2)
                layout.setSpacing(6)
                badge = Badge(
                    condition_truth_label(value),
                    "true" if value is True else ("false" if value is False else "unknown"),
                )
                badge.setMinimumWidth(62)
                layout.addWidget(badge)
                name = _wrap_label(QLabel(truncate(condition.display_name, 28)))
                name.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px;")
                layout.addWidget(name, 1)
                if editable:
                    remove = IconButton("close", "Detach precondition", size=12)
                    remove.setFixedSize(24, 24)
                    remove.clicked.connect(
                        lambda _=False, cid=condition_id: self._remove_precondition(
                            self.ref, cid
                        )
                    )
                    layout.addWidget(remove)
                self.content.addWidget(row)

        if prerequisites:
            self.content.addWidget(_sub_label("PREREQUISITES · 2VL INTERNAL STATES"))
            for source in prerequisites:
                status = evaluation.for_ref(self.ref)
                value = status.prerequisite_values.get(source) if status else False
                row = QFrame()
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 2, 0, 2)
                layout.setSpacing(6)
                badge = Badge("TRUE" if value else "FALSE", "true" if value else "false")
                badge.setMinimumWidth(52)
                layout.addWidget(badge)
                text = _wrap_label(
                    QLabel(
                        f"{source.kind.label} · "
                        f"{truncate(snapshot.get_name(source), 24)}"
                    )
                )
                text.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px;")
                text.setToolTip(f"Current state: {snapshot.get_state(source)}")
                layout.addWidget(text, 1)
                if editable:
                    remove = IconButton("close", "Detach prerequisite", size=12)
                    remove.setFixedSize(24, 24)
                    remove.clicked.connect(
                        lambda _=False, src=source: self._remove_prerequisite(
                            self.ref, src
                        )
                    )
                    layout.addWidget(remove)
                self.content.addWidget(row)

        if editable:
            candidates: list[tuple[str, tuple[str, str]]] = []
            for condition in sorted(
                snapshot.conditions.values(),
                key=lambda item: item.display_name.casefold(),
            ):
                if condition.id in preconditions:
                    continue
                candidates.append(
                    (
                        f"Condition · {condition.display_name}",
                        ("precondition", condition.id),
                    )
                )
            for ref in sorted(
                (
                    ref
                    for ref in snapshot.all_refs()
                    if ref.kind is not NodeKind.CONDITION and ref != self.ref
                ),
                key=lambda item: (
                    item.kind.value,
                    snapshot.get_name(item).casefold(),
                ),
            ):
                if ref in prerequisites:
                    continue
                candidates.append(
                    (
                        f"{ref.kind.label} · {snapshot.get_name(ref)}",
                        ("prerequisite", ref.key),
                    )
                )

            if count == 0:
                self.content.addWidget(
                    _empty_line(
                        "No dependencies attached · choose a Condition (precondition) "
                        "or a Task/Project (prerequisite)."
                    )
                )
            if candidates:
                combo = QComboBox()
                # Long candidate titles must never widen the Inspector. The
                # combo shrinks to the available row width and elides instead.
                combo.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
                )
                combo.setMinimumContentsLength(14)
                combo.setMinimumWidth(0)
                combo.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
                )
                combo.setMaxVisibleItems(12)
                for label, payload in candidates:
                    combo.addItem(label, payload)
                combo.view().setMinimumWidth(0)
                combo.view().setTextElideMode(Qt.TextElideMode.ElideRight)
                combo.setToolTip(
                    "Attach a Condition as a 3VL precondition, or a Task/Project as "
                    "a 2VL prerequisite."
                )
                add_row = QHBoxLayout()
                add_row.setSpacing(6)
                add_row.addWidget(combo, 1)
                add = QPushButton("Attach")
                add.setToolTip("Attach the selected dependency to this record")
                add.clicked.connect(
                    lambda _=False, cb=combo: self._attach_from_combo(cb.currentData())
                )
                add_row.addWidget(add, 0)
                self.content.addLayout(add_row)

        status = evaluation.for_ref(self.ref)
        if status and status.blockers:
            self.content.addWidget(_sub_label("UNRESOLVED BARRIERS"))
            for blocker in status.blockers[:6]:
                line = _wrap_label(QLabel(f"• {blocker}"))
                line.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
                self.content.addWidget(line)

    def _attach_from_combo(self, payload: tuple[str, str] | None) -> None:
        if not payload or self.ref is None:
            return
        kind, value = payload
        if kind == "precondition":
            self._add_precondition(value)
        else:
            self._add_prerequisite(value)

    def _add_precondition(self, condition_id: str | None) -> None:
        if not condition_id or self.ref is None:
            return
        try:
            self.service.connect(NodeRef(NodeKind.CONDITION, condition_id), self.ref)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Precondition attached")

    def _remove_precondition(self, target: NodeRef, condition_id: str) -> None:
        try:
            self.service.remove_precondition(target.kind, target.id, condition_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Precondition detached · downstream re-evaluated")

    def _add_prerequisite(self, source_key: str | None) -> None:
        if not source_key or self.ref is None:
            return
        source = NodeRef.parse(source_key)
        try:
            self.service.connect(source, self.ref)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Prerequisite attached")

    def _remove_prerequisite(self, target: NodeRef, source: NodeRef) -> None:
        try:
            self.service.remove_prerequisite(target.kind, target.id, source)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Prerequisite detached · downstream re-evaluated")

    # ------------------------------------------------------------------ notes
    def _render_notes(self) -> None:
        if self.ref is None:
            return
        self.content.addWidget(Divider())
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(SectionLabel("Note"))
        header.addStretch(1)
        self.note_status = QLabel("Saved")
        self.note_status.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
        header.addWidget(self.note_status)
        trash = IconButton("trash", "Delete note", size=14)
        trash.setFixedSize(26, 26)
        trash.clicked.connect(self._delete_note)
        header.addWidget(trash)
        self.content.addLayout(header)

        self._saved_note = self.service.get_note(self.ref)
        self._note_edit = NoteEdit()
        self._note_edit.setPlaceholderText(
            "Write a note. Content saves automatically when focus leaves; empty edits "
            "never erase the saved block."
        )
        self._note_edit.setMinimumHeight(96)
        self._note_edit.setPlainText(self._saved_note)
        self._note_edit.textChanged.connect(self._note_changed)
        self._note_edit.focus_lost.connect(self._on_note_focus_lost)
        self._note_dirty = False
        self.content.addWidget(self._note_edit)

    def _on_note_focus_lost(self) -> None:
        QTimer.singleShot(0, lambda: self.commit_note(announce=False))

    # ------------------------------------------------------------- docs
    def _render_docs(self) -> None:
        self.content.addWidget(Divider())
        self.content.addWidget(SectionLabel("Dependency model"))
        model = _wrap_label(
            QLabel(
                "Dependencies are flat and conjunctive. A Precondition is an external "
                "three-valued fact. A Prerequisite is an internal entity state: a source "
                "Task must be completed, or a source Project resolved. Every attached "
                "dependency must pass; no manual status override exists."
            )
        )
        model.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
        self.content.addWidget(model)
        self.content.addWidget(SectionLabel("Canvas gestures"))
        guide = _wrap_label(
            QLabel(
                "1. Draw a Precondition: drag a Condition node to a Task or Project "
                "target; this inserts a public.task_preconditions or "
                "public.project_preconditions row.\n"
                "2. Draw a Prerequisite: drag a Task or Project node to a downstream "
                "Task or Project target; this inserts a public.task_prerequisites or "
                "public.project_prerequisites row.\n"
                "3. Directionality: every arrow is an operational execution path. A "
                "source node contributes directly to a target node's status or "
                "resolution. Lines never represent parent-child or organizational "
                "membership."
            )
        )
        guide.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
        self.content.addWidget(guide)
        gestures = _wrap_label(
            QLabel(
                "Wheel: zoom · Middle-drag: pan · Double-click an edge or press Delete: "
                "remove a dependency · Drag from the right-hand port to connect."
            )
        )
        gestures.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
        self.content.addWidget(gestures)

    # ---------------------------------------------------------------- delete
    def _render_delete_for_ref(self) -> None:
        """Render the destructive action at the very bottom of the inspector."""
        if self.ref is None or self.view is None:
            return
        name = self.view.snapshot.get_name(self.ref)
        noun = {
            NodeKind.TASK: "task",
            NodeKind.PROJECT: "project",
            NodeKind.CONDITION: "condition",
        }.get(self.ref.kind, "record")
        self._render_delete(noun, name)

    def _render_delete(self, noun: str, name: str) -> None:
        self.content.addSpacing(4)
        delete = QPushButton(f"Delete {noun}")
        delete.setProperty("variant", "danger")
        delete.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        delete.clicked.connect(lambda: self._delete_entity(noun, name))
        self.content.addWidget(delete)

    def _delete_entity(self, noun: str, name: str) -> None:
        if self.ref is None:
            return
        self.commit_note()
        if not confirm_dialog(
            self,
            f"Delete {noun}?",
            f"“{name}” and all of its dependency links and note will be removed permanently.",
        ):
            return
        try:
            if self.ref.kind is NodeKind.TASK:
                self.service.delete_task(self.ref.id)
            elif self.ref.kind is NodeKind.PROJECT:
                self.service.delete_project(self.ref.id)
            else:
                self.service.delete_condition(self.ref.id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.ref = None
        self._empty()
        self.mutated.emit(f"{noun.title()} deleted")

def _wrap_label(label: QLabel) -> QLabel:
    """Make a long label wrap to the real Inspector width instead of expanding it."""
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label

def _kv(key: str, value: str) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 1, 0, 1)
    layout.setSpacing(10)
    label = QLabel(key)
    label.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
    label.setMinimumWidth(88)
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    text = _wrap_label(QLabel(value))
    text.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px;")
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(text, 1)
    return holder

def _kv_widget(key: str, widget: QWidget) -> QWidget:
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 1, 0, 1)
    layout.setSpacing(10)
    label = QLabel(key)
    label.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
    label.setMinimumWidth(52)
    layout.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addWidget(widget, 1)
    return holder

def _sub_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(
        f"color: {c('faint')}; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;"
    )
    return label

def _empty_line(text: str) -> QLabel:
    label = _wrap_label(QLabel(text))
    label.setStyleSheet(f"color: {c('faint')}; font-size: 10px; font-style: italic;")
    return label
