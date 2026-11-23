"""Qt thread workers for network and probe work.

Database access inside workers goes through the thread-safe connection pool;
Qt widgets are never touched off the GUI thread.
"""
from __future__ import annotations
from PyQt6.QtCore import QThread, pyqtSignal
from .rag_service import ChatAnswer, RagPipeline
from .service import EFOSService, WorkspaceView

class ConnectionProbeWorker(QThread):
    result_ready = pyqtSignal(object)

    def __init__(self, service: EFOSService, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self) -> None:
        try: status = self.service.connection_status()
        except Exception as exc:  # pragma: no cover - defensive
            from .models import ConnectionStatus

            config = self.service.config
            status = ConnectionStatus(
                False, config.db_host, config.db_port, config.db_name, str(exc)
            )
        self.result_ready.emit(status)


class ChatWorker(QThread):
    answer_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        pipeline: RagPipeline,
        question: str,
        view: WorkspaceView,
        history: list[dict[str, str]],
        parent=None,
    ):
        super().__init__(parent)
        self.pipeline = pipeline
        self.question = question
        self.view = view
        self.history = history

    def run(self) -> None:
        try:
            answer: ChatAnswer = self.pipeline.ask(self.question, view=self.view, history=self.history)
            self.answer_ready.emit(answer)
        except Exception as exc: self.failed.emit(str(exc))


class CalendarSyncWorker(QThread):
    state_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: EFOSService, *, force_full: bool = False, parent=None):
        super().__init__(parent)
        self.service = service
        self.force_full = force_full

    def run(self) -> None:
        try:
            state = self.service.sync_calendar(force_full=self.force_full)
            self.state_ready.emit(state)
        except Exception as exc: self.failed.emit(_failure_text(exc, "syncing Google Calendar"))


class CalendarConnectWorker(QThread):
    state_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, service: EFOSService, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self) -> None:
        # Every failure path — missing configuration, browser launch failure,
        # timeout, denied consent, token exchange error — must surface as a
        # ``failed`` signal so the Calendar button can reset from
        # "Waiting for Google…" instead of hanging indefinitely.
        try:
            if not self.service.config.google_enabled:
                self.failed.emit(
                    "Set EFOS_GOOGLE_CLIENT_ID, EFOS_GOOGLE_CLIENT_SECRET, and "
                    "EFOS_GOOGLE_CALENDAR_ID first."
                )
                return
            state = self.service.connect_calendar()
            self.state_ready.emit(state)
        except Exception as exc: self.failed.emit(_failure_text(exc, "authorizing Google Calendar"))

def _failure_text(exc: BaseException, action: str) -> str:
    """Readable failure text even for exceptions with an empty ``str()``."""
    message = str(exc).strip()
    if message: return message
    return f"{type(exc).__name__} while {action}."
