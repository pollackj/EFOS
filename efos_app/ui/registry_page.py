"""Sortable registry, object-type filters, and autocomplete search."""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import EFOSError
from ..formatting import fmt_relative, recurrence_span, schedule_span, truncate
from ..logic import availability_summary, condition_truth_label
from ..models import (
    NodeKind,
    NodeRef,
    ProjectState,
    TaskState,
)
from ..service import EFOSService, WorkspaceView
from .dialogs import confirm_dialog
from .icons import icon
from .inspector import InspectorPanel
from .theme import c, rgba
from .widgets import (
    EmptyState,
    InspectorSplitter,
    SegmentedControl,
    page_margins,
    toolbar_row,
)


TASK_HEADERS = ["TASK", "STATUS", "CONDITIONS", "RECURRING", "CREATED"]
PROJECT_HEADERS = ["PROJECT", "STATE", "CONDITIONS", "CREATED"]
CONDITION_HEADERS = ["CONDITION", "VALUE", "CREATED"]
ALL_HEADERS = ["OBJECT", "TYPE", "STATE", "CONDITIONS", "CREATED"]

TASK_KEYS = ["name", "state", "conditions", "recurring", "created"]
PROJECT_KEYS = ["name", "state", "conditions", "created"]
CONDITION_KEYS = ["name", "value", "created"]
ALL_KEYS = ["name", "kind", "state", "conditions", "created"]

VALUE_ORDER = {False: 0, None: 1, True: 2}


class RegistryPage(QWidget):
    create_requested = pyqtSignal(object)
    open_flow = pyqtSignal(object)
    mutated = pyqtSignal(str)
    toast_requested = pyqtSignal(str, str)

    def __init__(self, service: EFOSService, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.view: WorkspaceView | None = None
        self.kind: NodeKind | None = NodeKind.TASK
        self.sort_key = "name"
        self.sort_ascending = True
        self.rows: list[dict] = []
        self._suggestion_refs: dict[str, NodeRef] = {}
        self._selected_ref: NodeRef | None = None

        root = QVBoxLayout(self)
        page_margins(root)
        root.setSpacing(12)

        self.toolbar, top = toolbar_row()
        self.segments = SegmentedControl(["All", "Tasks", "Projects", "Conditions"])
        self.segments.select(1, emit=False)
        self.segments.changed.connect(self._change_filter)
        top.addWidget(self.segments)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tasks, projects, conditions…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(300)
        self.search.addAction(icon("search", c("muted"), 15), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textEdited.connect(self._search_edited)
        self.search.textChanged.connect(self._populate)
        top.addWidget(self.search, 1)
        self.flow_button = QPushButton("Open in flow")
        self.flow_button.setEnabled(False)
        self.flow_button.clicked.connect(self._open_selected_in_flow)
        top.addWidget(self.flow_button)
        self.new_button = QPushButton("New task")
        self.new_button.setProperty("variant", "primary")
        self.new_button.setIcon(icon("plus", "#FFFFFF", 14))
        self.new_button.clicked.connect(
            lambda: self.create_requested.emit(self.kind or NodeKind.TASK)
        )
        top.addWidget(self.new_button)
        root.addWidget(self.toolbar)

        splitter = InspectorSplitter()
        # The shared 20 px gutter separates the sortable table from the
        # Inspector on every workspace page.
        table_shell = QWidget()
        table_layout = QVBoxLayout(table_shell)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setFixedHeight(38)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_by_column)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.doubleClicked.connect(lambda _: self._open_selected_in_flow())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        table_stack = QStackedLayout()
        table_stack.setStackingMode(QStackedLayout.StackingMode.StackOne)
        table_stack.addWidget(self.table)
        self.empty_overlay = QWidget()
        self.empty_overlay.setStyleSheet(f"background: {rgba('bg', 0.55)};")
        empty_layout = QVBoxLayout(self.empty_overlay)
        self.empty_button = QPushButton("Create task")
        
        # 1. Theme-aware colors, adaptive text contrast, and active mouse :hover transitions
        self.empty_button.setStyleSheet("""
            QPushButton {
                background-color: #2D3748;
                color: #FFFFFF;
                border: 1px solid #4A5568;  /* Force a solid, visible gray border */
                border-radius: 6px;
                padding: 8px 18px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #4A5568;
                border: 1px solid #718096; /* Highlight border slightly on hover */
            }
        """)
        
        self.empty_button.clicked.connect(
            lambda: self.create_requested.emit(self.kind or NodeKind.TASK)
        )
        self.empty_state = EmptyState(
            "No matching records",
            "Create a record or adjust the search and object-type filter.",
            "registry",
            self.empty_button,
        )
        
        # 2. Add visual breathing room spacer directly beneath the icon
        empty_layout.addSpacing(12)
        empty_layout.addWidget(self.empty_state)
        empty_layout.addStretch(1)
        table_stack.addWidget(self.empty_overlay)
        self.table_stack = table_stack
        stack_host = QWidget()
        stack_host.setLayout(table_stack)
        table_layout.addWidget(stack_host, 1)

        footer = QHBoxLayout()
        footer.setSpacing(12)
        footer.setContentsMargins(0, 0, 12, 0)
        self.count_label = QLabel("0 records")
        self.count_label.setProperty("role", "faint")
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        self.sort_label = QLabel("Click a column header to sort")
        self.sort_label.setProperty("role", "faint")
        footer.addWidget(self.sort_label)
        table_layout.addLayout(footer)
        splitter.addWidget(table_shell)

        self.inspector = InspectorPanel(self.service)
        self.inspector.mutated.connect(self._inspector_mutated)
        self.inspector.toast_requested.connect(self.toast_requested)
        splitter.addWidget(self.inspector)
        splitter.setSizes([880, 350])
        splitter.setStretchFactor(0, 1)
        root.addWidget(splitter, 1)
        self.splitter = splitter

        self.completer = QCompleter(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setMaxVisibleItems(9)
        self.completer.activated[str].connect(self._suggestion_activated)
        self.search.setCompleter(self.completer)
        self._style_popup()
        self._update_headers()

    def _style_popup(self) -> None:
        popup = self.completer.popup()
        popup.setStyleSheet(
            f"QListView {{ background: {c('surface')}; color: {c('text')}; "
            f"border: 1px solid {c('border_hi')}; border-radius: 9px; padding: 4px; "
            f"font-size: 12px; }}"
            f"QListView::item {{ padding: 7px 10px; border-radius: 6px; }}"
            f"QListView::item:selected {{ background: {c('selection')}; color: {c('text')}; }}"
        )

    # ------------------------------------------------------------------ state
    def refresh(self, view: WorkspaceView) -> None:
        self.view = view
        self._refresh_completer()
        self._populate()
        if self._selected_ref is not None:
            if view.snapshot.exists(self._selected_ref):
                self.inspector.set_view(view)
            else:
                self._selected_ref = None
                self.inspector.set_ref(None, view)
        else:
            self.inspector.set_ref(None, view)

    def flush_notes(self) -> bool:
        return self.inspector.commit_note()

    def _change_filter(self, index: int) -> None:
        if not self.inspector.commit_note():
            return
        self.kind = [None, NodeKind.TASK, NodeKind.PROJECT, NodeKind.CONDITION][index]
        self.sort_key = "name"
        self.sort_ascending = True
        self._selected_ref = None
        self.inspector.set_ref(None, self.view)
        label = (self.kind.label.lower() if self.kind else "record")
        self.new_button.setText(f"New {label}")
        self.empty_button.setText(f"Create {label}")
        self._update_headers()
        self._refresh_completer()
        self._populate()

    def _headers(self) -> tuple[list[str], list[str]]:
        if self.kind is NodeKind.TASK:
            return TASK_HEADERS, TASK_KEYS
        if self.kind is NodeKind.PROJECT:
            return PROJECT_HEADERS, PROJECT_KEYS
        if self.kind is NodeKind.CONDITION:
            return CONDITION_HEADERS, CONDITION_KEYS
        return ALL_HEADERS, ALL_KEYS

    def _update_headers(self) -> None:
        headers, keys = self._headers()
        labels = []
        for header, key in zip(headers, keys):
            arrow = ""
            if key == self.sort_key:
                arrow = " ▲" if self.sort_ascending else " ▼"
            labels.append(header + arrow)
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(labels)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

    def _sort_by_column(self, column: int) -> None:
        _, keys = self._headers()
        if not 0 <= column < len(keys):
            return
        key = keys[column]
        if key == self.sort_key:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_key = key
            self.sort_ascending = True
        self._update_headers()
        self._populate()

    # ----------------------------------------------------------------- search
    def _search_edited(self, _text: str) -> None:
        self._refresh_completer()

    def _refresh_completer(self) -> None:
        if self.view is None:
            self.completer.setModel(None)
            return
        entries: list[str] = []
        self._suggestion_refs = {}
        snapshot = self.view.snapshot
        kinds = [self.kind] if self.kind else [NodeKind.TASK, NodeKind.PROJECT, NodeKind.CONDITION]
        for kind in kinds:
            refs = []
            if kind is NodeKind.TASK:
                refs = [
                    NodeRef(kind, task.id)
                    for task in snapshot.tasks.values()
                ]
            elif kind is NodeKind.PROJECT:
                refs = [
                    NodeRef(kind, project.id)
                    for project in snapshot.projects.values()
                ]
            else:
                refs = [
                    NodeRef(kind, condition.id)
                    for condition in snapshot.conditions.values()
                ]
            for ref in sorted(
                refs, key=lambda item: snapshot.get_name(item).casefold()
            ):
                label = f"{kind.label} · {snapshot.get_name(ref)}"
                if self.kind is None:
                    label += f"  ({snapshot.get_state(ref)})"
                entries.append(label)
                self._suggestion_refs[label] = ref
        model = self.completer.model()
        from PyQt6.QtCore import QStringListModel

        if not isinstance(model, QStringListModel):
            model = QStringListModel(self)
            self.completer.setModel(model)
        model.setStringList(entries)

    def _suggestion_activated(self, text: str) -> None:
        ref = self._suggestion_refs.get(text)
        if ref is None:
            return
        if not self.inspector.commit_note():
            return
        target_index = {
            None: 0,
            NodeKind.TASK: 1,
            NodeKind.PROJECT: 2,
            NodeKind.CONDITION: 3,
        }[ref.kind]
        self.kind = ref.kind
        self.segments.select(target_index, emit=False)
        label = self.kind.label.lower()
        self.new_button.setText(f"New {label}")
        self.empty_button.setText(f"Create {label}")
        self.sort_key = "name"
        self.sort_ascending = True
        self._update_headers()
        self.search.blockSignals(True)
        self.search.setText(self.view.snapshot.get_name(ref) if self.view else "")
        self.search.blockSignals(False)
        self._populate()
        self._select_ref(ref)
        self._selected_ref = ref
        self.inspector.set_ref(ref, self.view)
        self.search.setFocus()

    # ---------------------------------------------------------------- populate
    def _rows(self) -> list[dict]:
        if self.view is None:
            return []
        snapshot = self.view.snapshot
        evaluation = self.view.evaluation
        rows: list[dict] = []
        for task in snapshot.tasks.values():
            rows.append(
                {
                    "ref": NodeRef(NodeKind.TASK, task.id),
                    "kind": "task",
                    "name": task.name,
                    "state": task.current_state.value,
                    "conditions": snapshot.dependency_count(NodeRef(NodeKind.TASK, task.id)),
                    "recurring": bool(task.is_recurring),
                    "created": task.created_at,
                    "value": None,
                    "tooltip": (
                        f"{task.name}\nStatus: {task.current_state.label}\n"
                        f"Availability: {availability_summary(task.availability)}\n"
                        f"Schedule: {schedule_span(task)}\n"
                        f"Recurrence: {recurrence_span(task)}"
                    ),
                    "meta": schedule_span(task),
                }
            )
        for project in snapshot.projects.values():
            rows.append(
                {
                    "ref": NodeRef(NodeKind.PROJECT, project.id),
                    "kind": "project",
                    "name": project.name,
                    "state": project.current_state.value,
                    "conditions": snapshot.dependency_count(
                        NodeRef(NodeKind.PROJECT, project.id)
                    ),
                    "recurring": False,
                    "created": project.created_at,
                    "value": None,
                    "tooltip": (
                        f"{project.name}\nRequirements undefined: "
                        f"{project.resolution_conditions_unknown}"
                    ),
                    "meta": "Requirements Undefined"
                    if project.resolution_conditions_unknown
                    else "Requirements defined",
                }
            )
        for condition in snapshot.conditions.values():
            value = evaluation.condition_value(condition.id)
            rows.append(
                {
                    "ref": NodeRef(NodeKind.CONDITION, condition.id),
                    "kind": "condition",
                    "name": condition.display_name,
                    "state": "true" if value is True else ("false" if value is False else "unknown"),
                    "conditions": len(snapshot.attached_preconditions(condition.id)),
                    "recurring": False,
                    "created": condition.created_at,
                    "value": value,
                    "tooltip": f"{condition.display_name}\nValue: {condition_truth_label(value)}",
                    "meta": condition_truth_label(value),
                }
            )
        return rows

    def _populate(self) -> None:
        if self.view is None:
            return
        query = self.search.text().strip().casefold()
        rows = self._rows()
        if self.kind is not None:
            rows = [row for row in rows if row["kind"] == self.kind.value]
        if query:
            rows = [
                row
                for row in rows
                if query in str(row["name"]).casefold()
                or query in str(row["state"]).casefold()
            ]
        rows = self._sorted(rows)
        self.rows = rows
        self.table.blockSignals(True)
        self._update_headers()
        self.table.setRowCount(len(rows))
        headers, keys = self._headers()
        for index, row in enumerate(rows):
            cells = self._cells(row, keys)
            for column, (text, color, align) in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(color))
                item.setData(Qt.ItemDataRole.UserRole, row["ref"].key)
                item.setToolTip(row["tooltip"])
                if column == 0:
                    item.setIcon(
                        icon(row["kind"], c(row["kind"]), 15)
                    )
                    font = item.font()
                    font.setWeight(QFont.Weight.DemiBold)
                    item.setFont(font)
                if align == "right":
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                else:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(index, column, item)
        self.table.blockSignals(False)
        has_rows = bool(rows)
        self.table_stack.setCurrentWidget(self.table if has_rows else self.empty_overlay)
        total = len(self._rows()) if self.kind is None else len(
            [row for row in self._rows() if row["kind"] == self.kind.value]
        )
        filtered_note = f" · {len(rows)} matching" if query or self.kind is None else ""
        self.count_label.setText(
            f"{len(rows)} of {total} record{'s' if total != 1 else ''}{filtered_note}"
        )
        self.sort_label.setText(
            f"Sorted by {self.sort_key} · {'ascending' if self.sort_ascending else 'descending'}"
        )
        if self._selected_ref is not None:
            self._select_ref(self._selected_ref)
        else:
            self.table.clearSelection()
        self.flow_button.setEnabled(self._selected_ref is not None)

    def _cells(self, row: dict, keys: list[str]) -> list[tuple[str, str, str]]:
        colors = {
            "ready": c("green"),
            "blocked": c("faint"),
            "completed": c("blue"),
            "deferred": c("amber"),
            "active": c("accent"),
            "stalled": c("amber"),
            "shelved": c("faint"),
            "resolved": c("green"),
            "true": c("green"),
            "false": c("red"),
            "unknown": c("muted"),
        }
        cells: list[tuple[str, str, str]] = []
        for key in keys:
            if key == "name":
                cells.append((row["name"], c("text"), "left"))
            elif key == "kind":
                cells.append((str(row["kind"]).title(), c("muted"), "left"))

            elif key == "state":
                label = (
                    condition_truth_label(row["value"])
                    if row["kind"] == "condition"
                    else str(row["state"]).title()
                )
                color_value = colors.get(str(row["state"]), c("text_soft"))
                cells.append((label, color_value, "left"))

            elif key == "conditions":
                cells.append((str(row["conditions"]), c("text_soft"), "left"))
            elif key == "recurring":
                cells.append(("Yes" if row["recurring"] else "No", c("text_soft"), "left"))
            elif key == "value":
                label = condition_truth_label(row["value"])
                cells.append((label, colors.get(str(row["state"]), c("muted")), "left"))
            elif key == "created":
                cells.append((fmt_relative(row["created"]), c("faint"), "right"))
            else:
                cells.append((str(row.get(key, "")), c("text_soft"), "left"))
        return cells

    def _sorted(self, rows: list[dict]) -> list[dict]:
        key = self.sort_key

        def value(row: dict):  # type: ignore[no-untyped-def]
            if key == "value":
                return VALUE_ORDER.get(row["value"], 1)
            if key == "created":
                return row["created"]
            if key == "conditions" or key == "recurring":
                return row[key]
            return str(row.get(key, "")).casefold()

        present = [row for row in rows if value(row) is not None]
        missing = [row for row in rows if value(row) is None]
        present.sort(key=value, reverse=not self.sort_ascending)
        return present + missing

    # --------------------------------------------------------------- selection
    def _selected(self) -> NodeRef | None:
        model = self.table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        try:
            return NodeRef.parse(data)
        except (ValueError, TypeError):
            return None

    def _select_ref(self, ref: NodeRef) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == ref.key:
                self.table.selectRow(row)
                return

    def _on_selection(self) -> None:
        ref = self._selected()
        if ref is None:
            self.flow_button.setEnabled(False)
            return
        if ref != self._selected_ref:
            if not self.inspector.commit_note():
                return
            self._selected_ref = ref
            self.inspector.set_ref(ref, self.view)
        self.flow_button.setEnabled(True)

    def _open_selected_in_flow(self) -> None:
        ref = self._selected()
        if ref is not None:
            self.open_flow.emit(ref)

    def _inspector_mutated(self, message: str) -> None:
        if self._selected_ref is not None and self.view is not None:
            if not self.view.snapshot.exists(self._selected_ref):
                self._selected_ref = None
        self.mutated.emit(message)

    def _context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        self.table.selectRow(index.row())
        ref = self._selected()
        if ref is None or self.view is None:
            return
        menu = QMenu(self)
        menu.addAction("Open properties", lambda: self.inspector.set_ref(ref, self.view))
        menu.addAction("Open in flow", lambda: self.open_flow.emit(ref))
        menu.addSeparator()
        if ref.kind is NodeKind.TASK:
            task = self.view.snapshot.tasks.get(ref.id)
            if task and task.current_state in (TaskState.COMPLETED, TaskState.DEFERRED):
                menu.addAction("Reopen task", lambda: self._reopen(ref))
        elif ref.kind is NodeKind.PROJECT:
            project = self.view.snapshot.projects.get(ref.id)
            if project and project.current_state is ProjectState.SHELVED:
                menu.addAction("Unshelve project", lambda: self._unshelve(ref))
            if project and project.current_state is ProjectState.STALLED:
                menu.addAction("Reactivate project", lambda: self._reactivate(ref))
        menu.addAction("Delete record", lambda: self._delete(ref))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _reopen(self, ref: NodeRef) -> None:
        try:
            self.service.reopen_task(ref.id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Task reopened · dependency evaluation re-triggered")

    def _unshelve(self, ref: NodeRef) -> None:
        try:
            self.service.unshelve_project(ref.id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Project unshelved")

    def _reactivate(self, ref: NodeRef) -> None:
        try:
            self.service.reactivate_project(ref.id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.mutated.emit("Project reactivated")

    def _delete(self, ref: NodeRef) -> None:
        if self.view is None:
            return
        name = self.view.snapshot.get_name(ref)
        if not confirm_dialog(
            self, "Delete record?", f"“{name}” and all dependency links will be removed."
        ):
            return
        try:
            if ref.kind is NodeKind.TASK:
                self.service.delete_task(ref.id)
            elif ref.kind is NodeKind.PROJECT:
                self.service.delete_project(ref.id)
            else:
                self.service.delete_condition(ref.id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._selected_ref = None
        self.inspector.set_ref(None, self.view)
        self.mutated.emit("Record deleted")
