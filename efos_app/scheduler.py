"""Application-level background scheduling.

A lightweight QTimer pulse runs once every 60 seconds. It evaluates recurring
Tasks against their configured window, resets completed instances exactly once
per window (idempotent), and leaves dependency enforcement to the database
trigger. No PostgreSQL server extension is required.
"""
from __future__ import annotations
import logging
from datetime import datetime
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from .service import EFOSService

log = logging.getLogger(__name__)

PULSE_MS = 60_000

class BackgroundScheduler(QObject):
    pulse_completed = pyqtSignal(object, object, object)  # results, timestamp, changes
    pulse_failed = pyqtSignal(str)

    def __init__(self, service: EFOSService, *, interval_ms: int = PULSE_MS, parent: QObject | None = None):
        super().__init__(parent)
        self.service = service
        self.timer = QTimer(self)
        self.timer.setInterval(max(5_000, int(interval_ms)))
        self.timer.timeout.connect(self.pulse)
        self.last_pulse: datetime | None = None
        self._running = False

    def start(self) -> None:
        self.timer.start()
        self._running = True

    def stop(self) -> None:
        self.timer.stop()
        self._running = False

    @property
    def running(self) -> bool: return self._running

    def pulse(self) -> None:
        try: results, changes = self.service.pulse()
        except Exception as exc:  # pragma: no cover - DB defensive
            log.warning("Scheduler pulse failed: %s", exc)
            self.pulse_failed.emit(str(exc))
            return
        self.last_pulse = datetime.now()
        self.pulse_completed.emit(results, self.last_pulse, changes)
