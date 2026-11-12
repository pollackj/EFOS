"""Modal host for the shared inspector (legacy entry point, kept compatible)."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ..models import NodeRef
from ..service import EFOSService
from .inspector import InspectorPanel
from .theme import c, enable_native_title_bar


class EntityDialog(QDialog):
    mutated = pyqtSignal(str)

    def __init__(
        self, service: EFOSService, ref: NodeRef, parent: QWidget | None = None
    ):
        super().__init__(parent)
        self.service = service
        self.ref = ref
        self.view = service.load_view(reconcile=False)
        self.setWindowTitle("EFOS · Inspector")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(430, 850)
        self.setMinimumSize(420, 680)
        self.setStyleSheet(
            f"QDialog {{ background: {c('surface')}; border: 1px solid {c('border_hi')}; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 10, 6)
        title = QLabel("Inspector")
        title.setProperty("role", "eyebrow")
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        header_layout.addWidget(close)
        root.addWidget(header)
        self.inspector = InspectorPanel(service)
        self.inspector.set_ref(ref, self.view)
        self.inspector.mutated.connect(self._on_mutated)
        self.inspector.toast_requested.connect(self._toast)
        root.addWidget(self.inspector, 1)

    def _on_mutated(self, message: str) -> None:
        self.view = self.service.load_view(reconcile=False)
        self.inspector.set_view(self.view)
        self.mutated.emit(message)

    def _toast(self, message: str, level: str) -> None:
        box = QMessageBox(self)
        box.setIcon(
            QMessageBox.Icon.Warning if level == "error" else QMessageBox.Icon.Information
        )
        box.setText(message)
        box.exec()

    def showEvent(self, event):  # type: ignore[no-untyped-def]
        super().showEvent(event)
        enable_native_title_bar(self)
