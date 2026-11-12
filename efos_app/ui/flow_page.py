"""Flow workspace: the flat dependency canvas plus the shared inspector."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..errors import EFOSError
from ..models import NodeKind, NodeRef
from ..service import EFOSService, WorkspaceView
from .graph import FlowScene, FlowView, LayoutStore
from .icons import icon
from .inspector import InspectorPanel
from .theme import c
from .widgets import IconButton, InlineBanner


class FlowWorkspace(QWidget):
    create_requested = pyqtSignal(object)
    mutated = pyqtSignal(str)
    toast_requested = pyqtSignal(str, str)

    def __init__(self, service: EFOSService, layout_store: LayoutStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.view: WorkspaceView | None = None
        self._known_keys: frozenset[str] = frozenset()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 20)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(7)
        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        title = QLabel("Dependency canvas")
        title.setProperty("role", "h2")
        text_col.addWidget(title)
        subtitle = QLabel("Flat nodes · Condition → entity = Precondition · entity → entity = Prerequisite")
        subtitle.setProperty("role", "faint")
        text_col.addWidget(subtitle)
        toolbar.addLayout(text_col, 1)
        self.layout_button = IconButton("layout", "Auto layout")
        self.layout_button.clicked.connect(self._auto_layout)
        toolbar.addWidget(self.layout_button)
        self.fit_button = IconButton("fit", "Fit to content")
        self.fit_button.clicked.connect(lambda: self.viewport.fit_content())
        toolbar.addWidget(self.fit_button)
        self.zoom_out_button = IconButton("zoom_out", "Zoom out")
        self.zoom_out_button.clicked.connect(lambda: self.viewport.zoom_out())
        toolbar.addWidget(self.zoom_out_button)
        self.zoom_in_button = IconButton("zoom_in", "Zoom in")
        self.zoom_in_button.clicked.connect(lambda: self.viewport.zoom_in())
        toolbar.addWidget(self.zoom_in_button)
        new_task = QPushButton("Task")
        new_task.setIcon(icon("task", c("muted"), 14))
        new_task.clicked.connect(lambda: self.create_requested.emit(NodeKind.TASK))
        toolbar.addWidget(new_task)
        new_project = QPushButton("Project")
        new_project.setIcon(icon("project", c("muted"), 14))
        new_project.clicked.connect(lambda: self.create_requested.emit(NodeKind.PROJECT))
        toolbar.addWidget(new_project)
        new_condition = QPushButton("Condition")
        new_condition.setIcon(icon("condition", c("muted"), 14))
        new_condition.clicked.connect(lambda: self.create_requested.emit(NodeKind.CONDITION))
        toolbar.addWidget(new_condition)
        root.addLayout(toolbar)

        self.banner = InlineBanner()
        self.banner.dismissed.connect(lambda: None)
        root.addWidget(self.banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        canvas_shell = QFrame()
        canvas_shell.setObjectName("GraphCanvasShell")
        canvas_layout = QVBoxLayout(canvas_shell)
        canvas_layout.setContentsMargins(1, 1, 1, 1)
        canvas_layout.setSpacing(0)
        self.scene = FlowScene(layout_store)
        self.viewport = FlowView(self.scene)
        canvas_layout.addWidget(self.viewport, 1)
        splitter.addWidget(canvas_shell)
        self.inspector = InspectorPanel(service, docs=True)
        self.inspector.mutated.connect(self.mutated)
        self.inspector.toast_requested.connect(self.toast_requested)
        splitter.addWidget(self.inspector)
        splitter.setSizes([900, 340])
        splitter.setStretchFactor(0, 1)
        root.addWidget(splitter, 1)

        self.scene.selection_changed.connect(self._selection_changed)
        self.scene.connect_requested.connect(self._connect)
        self.scene.edge_delete_requested.connect(self._disconnect)
        self.scene.condition_toggle_requested.connect(self._toggle_condition)
        self.scene.node_activated.connect(self._activate_node)
        self.scene.status_message.connect(self._status)
        self.scene.layout_changed.connect(self._banner_hint)

    # ---------------------------------------------------------------- public
    def refresh(self, view: WorkspaceView) -> None:
        self.view = view
        self.scene.load_view(view)
        keys = frozenset(self.scene.node_items)
        if keys != self._known_keys:
            self._known_keys = keys
            if len(keys) <= 10:
                QTimer.singleShot(30, self.viewport.fit_content)
            else:
                QTimer.singleShot(30, self.viewport.reset_view)
        if self.inspector.ref is not None:
            if self.inspector.ref.key in self.scene.node_items:
                self.inspector.set_view(view)
            else:
                self.inspector.set_ref(None, view)
        else:
            self.inspector.set_ref(None, view)

    def select_ref(self, ref: NodeRef) -> bool:
        if ref.key in self.scene.node_items:
            self.scene.select_ref(ref)
            return True
        return False

    def flush_notes(self) -> bool:
        return self.inspector.commit_note()

    def shutdown(self) -> None:
        self.inspector.commit_note(announce=False)

    # --------------------------------------------------------------- handlers
    def _selection_changed(self, item) -> None:
        if item is None:
            return
        if isinstance(item, NodeRef):
            if not self.inspector.commit_note():
                return
            self.inspector.set_ref(item, self.view)
        else:
            self._status("Dependency selected · press Delete to remove it", "info")

    def _activate_node(self, ref: NodeRef) -> None:
        if not self.inspector.commit_note():
            return
        self.inspector.set_ref(ref, self.view)

    def _connect(self, source: NodeRef, target: NodeRef) -> None:
        try:
            kind = self.service.connect(source, target)
        except EFOSError as exc:
            self.banner.show_message(str(exc), "error")
            return
        self.banner.dismiss()
        label = "Precondition" if kind.value == "precondition" else "Prerequisite"
        self.mutated.emit(f"{label} attached")

    def _disconnect(self, edge) -> None:  # type: ignore[no-untyped-def]
        try:
            self.service.disconnect(edge)
        except EFOSError as exc:
            self.banner.show_message(str(exc), "error")
            return
        self.inspector.set_ref(None, self.view)
        self.mutated.emit("Dependency removed · downstream re-evaluated")

    def _toggle_condition(self, condition_id: str) -> None:
        try:
            value = self.service.cycle_condition_value(condition_id)
        except EFOSError as exc:
            self.banner.show_message(str(exc), "error")
            return
        from ..logic import condition_truth_label

        self.mutated.emit(f"Condition set to {condition_truth_label(value)}")

    def _auto_layout(self) -> None:
        self.scene.auto_layout()
        self.viewport.fit_content()

    def _status(self, message: str, level: str) -> None:
        if level == "error":
            self.banner.show_message(message, "error")

    def _banner_hint(self) -> None:
        return
