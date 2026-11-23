"""Flow workspace: the flat dependency canvas plus the shared inspector."""
from __future__ import annotations
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (QFrame, QVBoxLayout, QWidget)
from ..errors import EFOSError
from ..models import NodeRef
from ..service import EFOSService, WorkspaceView
from .graph import FlowScene, FlowView, LayoutStore
from .inspector import InspectorPanel
from .theme import fs
from .widgets import InlineBanner, InspectorSplitter

class FlowWorkspace(QWidget):
    create_requested = pyqtSignal(object)
    mutated = pyqtSignal(str)
    toast_requested = pyqtSignal(str, str)

    def __init__(self, service: EFOSService, layout_store: LayoutStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.view: WorkspaceView | None = None
        self._known_keys: frozenset[str] = frozenset()
        self._startup_layout_pending = True
        self._startup_auto_layout = True
        # A zoom/centre snapshot captured before a theme or font rebuild; the
        # next refresh applies it instead of auto-fitting the canvas.
        self._pending_view_state: dict | None = None
        # The Dashboard page owns the single compact toolbar (Manage | Flow,
        # canvas tools, create actions) and this page fills the stack directly,
        # so the canvas and Inspector start at the same Y as every other page.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(max(8, fs(9)))

        self.banner = InlineBanner()
        self.banner.dismissed.connect(lambda: None)
        root.addWidget(self.banner)

        splitter = InspectorSplitter()
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
        self.splitter = splitter

        self.scene.selection_changed.connect(self._selection_changed)
        self.scene.connect_requested.connect(self._connect)
        self.scene.edge_delete_requested.connect(self._disconnect)
        self.scene.condition_toggle_requested.connect(self._toggle_condition)
        self.scene.node_activated.connect(self._activate_node)
        self.scene.status_message.connect(self._status)
        self.scene.layout_changed.connect(self._banner_hint)

    # ---------------------------------------------------------------- public
    def set_startup_auto_layout(self, enabled: bool) -> None:
        """Configure whether the first Flow visit after launch auto-lays out."""
        self._startup_auto_layout = bool(enabled)

    @property
    def startup_layout_pending(self) -> bool: return self._startup_layout_pending

    def set_startup_layout_pending(self, pending: bool) -> None: self._startup_layout_pending = bool(pending)

    def maybe_startup_layout(self) -> None:
        """Apply the startup auto-layout once, when Flow is first opened."""
        if not self._startup_layout_pending: return
        self._startup_layout_pending = False
        if self._startup_auto_layout and self.scene.node_items: self._auto_layout()

    def refresh(self, view: WorkspaceView) -> None:
        had_nodes = bool(self._known_keys)
        preserved = self.capture_view_state() if had_nodes and self._pending_view_state is None else None
        self.view = view
        self.scene.load_view(view)
        keys = frozenset(self.scene.node_items)
        if keys != self._known_keys:
            self._known_keys = keys

            def _restore_or_fit() -> None:
                if self._pending_view_state is not None: self._apply_pending_view_state(clear=True)
                elif preserved is not None: self.viewport.restore_view_state(preserved)
                elif len(keys) <= 10: self.viewport.fit_content()
                else: self.viewport.reset_view()

            QTimer.singleShot(30, _restore_or_fit)
        if self.inspector.ref is not None:
            if self.inspector.ref.key in self.scene.node_items: self.inspector.set_view(view)
            else: self.inspector.set_ref(None, view)
        else: self.inspector.set_ref(None, view)

    # ------------------------------------------------------------- view state
    def capture_view_state(self) -> dict:
        """Snapshot the canvas zoom and centre for a later restore."""
        return self.viewport.capture_view_state()

    def restore_view_state(self, state: dict | None) -> bool:
        """Restore a zoom/centre snapshot, surviving the refresh auto-fit."""
        if not state: return False
        self._pending_view_state = dict(state)
        self.viewport.restore_view_state(self._pending_view_state)
        QTimer.singleShot(0, self._apply_pending_view_state)
        # The post-refresh fit runs at 30 ms; re-apply after it as a safety net.
        QTimer.singleShot(60, lambda: self._apply_pending_view_state(clear=True))
        return True

    def _apply_pending_view_state(self, *, clear: bool = False) -> None:
        state = self._pending_view_state
        if state is None: return
        self.viewport.restore_view_state(state)
        if clear: self._pending_view_state = None

    def select_ref(self, ref: NodeRef) -> bool:
        if ref.key in self.scene.node_items:
            self.scene.select_ref(ref)
            return True
        return False

    def flush_notes(self) -> bool: return self.inspector.commit_note()

    def shutdown(self) -> None: self.inspector.commit_note(announce=False)

    # --------------------------------------------------------------- handlers
    def _selection_changed(self, item) -> None:
        if item is None: return
        if isinstance(item, NodeRef):
            if not self.inspector.commit_note(): return
            self.inspector.set_ref(item, self.view)
        else: self._status("Dependency selected · press Delete to remove it", "info")

    def _activate_node(self, ref: NodeRef) -> None:
        if not self.inspector.commit_note(): return
        self.inspector.set_ref(ref, self.view)

    def _connect(self, source: NodeRef, target: NodeRef) -> None:
        try: kind = self.service.connect(source, target)
        except EFOSError as exc:
            self.banner.show_message(str(exc), "error")
            return
        self.banner.dismiss()
        label = "Precondition" if kind.value == "precondition" else "Prerequisite"
        self.mutated.emit(f"{label} attached")

    def _disconnect(self, edge) -> None:  # type: ignore[no-untyped-def]
        try: self.service.disconnect(edge)
        except EFOSError as exc:
            self.banner.show_message(str(exc), "error")
            return
        self.inspector.set_ref(None, self.view)
        self.mutated.emit("Dependency removed · downstream re-evaluated")

    def _toggle_condition(self, condition_id: str) -> None:
        try: value = self.service.cycle_condition_value(condition_id)
        except EFOSError as exc:
            self.banner.show_message(str(exc), "error")
            return
        from ..logic import condition_truth_label

        self.mutated.emit(f"Condition set to {condition_truth_label(value)}")

    def auto_layout(self) -> None:
        """Re-run the deterministic left-to-right canvas layout."""
        self._auto_layout()

    @property
    def layout_mode(self) -> str: return self.scene.layout_mode

    def set_layout_mode(self, mode: str, *, relayout: bool = True) -> None:
        self.scene.layout_mode = "sink" if mode == "sink" else "source"
        if relayout: self._auto_layout()

    def _auto_layout(self) -> None:
        self.scene.auto_layout()
        self.viewport.fit_content()

    def _status(self, message: str, level: str) -> None:
        if level == "error": self.banner.show_message(message, "error")

    def _banner_hint(self) -> None: return
