"""Modal dialogs: creation, recurrence, collisions, setup, and schema errors."""
from __future__ import annotations
import secrets
from datetime import datetime
from PyQt6.QtCore import QDate, QTime, Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import (DbProfile, save_runtime_password, save_runtime_profile)
from ..db import provision_database, test_connection
from ..errors import EFOSError
from ..formatting import fmt_datetime
from ..logic import availability_summary, normalize_availability_windows
from ..models import (AvailabilityWindow, CalendarItem, NodeKind, RecurrenceFrequency, RecurrenceRule)
from .theme import c, enable_native_title_bar, rgba
from .widgets import Badge, Card, Divider, GreenCheckBox, IconButton

class BaseDialog(QDialog):
    def __init__(self, parent: QWidget | None, title: str, subtitle: str = "", width: int = 520):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setModal(True)
        self.setMinimumWidth(width)
        self.setStyleSheet(f"QDialog {{ background: {c('surface')}; border: 1px solid {c('border_hi')}; }}")
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 22, 24, 20)
        self.root.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(12)
        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        eyebrow = QLabel("EFOS")
        eyebrow.setProperty("role", "eyebrow")
        text_col.addWidget(eyebrow)
        heading = QLabel(title)
        heading.setProperty("role", "h2")
        text_col.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setProperty("role", "muted")
            sub.setWordWrap(True)
            text_col.addWidget(sub)
        header.addLayout(text_col, 1)
        close = IconButton("close", "Close")
        close.clicked.connect(self.reject)
        header.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.root.addLayout(header)

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def showEvent(self, event):  # type: ignore[no-untyped-def]
        super().showEvent(event)
        enable_native_title_bar(self)

class CreateEntityDialog(BaseDialog):
    def __init__(self, kind: NodeKind, parent: QWidget | None = None):
        labels = {
            NodeKind.TASK: (
                "New task",
                "Task",
                "Tasks become Ready only when every precondition is TRUE and "
                "every prerequisite source is complete.",
            ),
            NodeKind.PROJECT: (
                "New project",
                "Project",
                "A new project starts with undefined requirements. Automated "
                "resolution is blocked until dependencies are attached.",
            ),
            NodeKind.CONDITION: (
                "New condition",
                "Condition",
                "Conditions are external three-valued facts: TRUE, FALSE, or UNKNOWN.",
            ),
        }
        title, self.noun, subtitle = labels[kind]
        super().__init__(parent, title, subtitle, 500)
        self.kind = kind

        name_label = QLabel("NAME")
        name_label.setProperty("role", "sectionTitle")
        self.root.addWidget(name_label)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(
            {
                "Task": "e.g. Draft the launch brief",
                "Project": "e.g. Q4 launch",
                "Condition": "e.g. Design approved",
            }[self.noun]
        )
        self.name_edit.setMinimumHeight(38)
        self.root.addWidget(self.name_edit)

        self.value_combo: QComboBox | None = None
        self.recurring: QCheckBox | None = None
        if kind is NodeKind.CONDITION:
            value_label = QLabel("INITIAL VALUE")
            value_label.setProperty("role", "sectionTitle")
            self.root.addWidget(value_label)
            self.value_combo = QComboBox()
            self.value_combo.addItem("UNKNOWN / NULL (blocks targets)", None)
            self.value_combo.addItem("FALSE", False)
            self.value_combo.addItem("TRUE", True)
            self.value_combo.setMinimumHeight(36)
            self.root.addWidget(self.value_combo)
        elif kind is NodeKind.TASK:
            self.recurring = GreenCheckBox("Recurring task")
            self.root.addWidget(self.recurring)
        else:
            hint = QLabel(
                "Requirements Undefined is set automatically and drops when the first "
                "precondition or prerequisite is attached."
            )
            hint.setProperty("role", "faint")
            hint.setWordWrap(True)
            self.root.addWidget(hint)

        self.error_label = QLabel("")
        self.error_label.setProperty("role", "danger")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.root.addWidget(self.error_label)

        self.root.addSpacing(4)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.create_button = QPushButton(f"Create {self.noun.lower()}")
        self.create_button.setProperty("variant", "primary")
        self.create_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.create_button.clicked.connect(self._validate)
        self.create_button.setDefault(True)
        buttons.addWidget(self.create_button)
        self.root.addLayout(buttons)
        self.name_edit.returnPressed.connect(self._validate)
        self.name_edit.setFocus()

    def _validate(self) -> None:
        if not self.name_edit.text().strip():
            self.error_label.setText(f"{self.noun} name cannot be empty.")
            self.error_label.show()
            self.name_edit.setFocus()
            return
        self.accept()

    def values(self) -> dict:
        data = {"name": self.name_edit.text().strip()}
        if self.kind is NodeKind.CONDITION and self.value_combo:
            data["evaluation_state"] = self.value_combo.currentData()
        if self.kind is NodeKind.TASK:
            data["is_recurring"] = bool(self.recurring and self.recurring.isChecked())
        return data

class RecurrenceDialog(BaseDialog):
    """Google Calendar-style recurrence editor mapped to task_recurrence_rules."""

    def __init__(
        self,
        rule: RecurrenceRule | None = None,
        parent: QWidget | None = None,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ):
        super().__init__(
            parent,
            "Recurrence",
            "Define the repeat pattern for this task. Each occurrence uses the "
            "Start and End time window configured in the task's Schedule section.",
            540,
        )
        self.rule = rule
        self._window_start = window_start
        self._window_end = window_end
        frequency = rule.frequency if rule else RecurrenceFrequency.WEEKLY
        interval = rule.interval if rule else 1
        by_day = rule.by_day_list if rule and rule.by_day else []
        by_month_day = rule.by_month_day if rule else datetime.now().day
        count = rule.occurrence_count if rule else None
        until = rule.until_date if rule else None

        self.frequency = QComboBox()
        for value in RecurrenceFrequency: self.frequency.addItem(value.label, value)
        self.frequency.setCurrentIndex(max(0, self.frequency.findData(frequency)))
        self.frequency.currentIndexChanged.connect(self._sync_visibility)
        self.root.addWidget(self._labeled("REPEAT", self.frequency))

        repeat_row = QHBoxLayout()
        repeat_row.setSpacing(8)
        every = QLabel("Every")
        every.setStyleSheet(f"color: {c('muted')}; font-size: 12px;")
        repeat_row.addWidget(every)
        self.interval = QSpinBox()
        self.interval.setRange(1, 99)
        self.interval.setValue(max(1, interval))
        self.interval.setFixedWidth(66)
        repeat_row.addWidget(self.interval)
        self.unit_label = QLabel("week(s)")
        self.unit_label.setStyleSheet(f"color: {c('muted')}; font-size: 12px;")
        repeat_row.addWidget(self.unit_label)
        repeat_row.addStretch(1)
        self.root.addLayout(repeat_row)

        self.weekday_box = QWidget()
        weekday_layout = QVBoxLayout(self.weekday_box)
        weekday_layout.setContentsMargins(0, 0, 0, 0)
        weekday_layout.setSpacing(5)
        day_label = QLabel("REPEAT ON")
        day_label.setProperty("role", "sectionTitle")
        weekday_layout.addWidget(day_label)
        day_row = QHBoxLayout()
        day_row.setSpacing(4)
        self.day_checks: dict[str, QCheckBox] = {}
        codes = (("MO", "M"), ("TU", "T"), ("WE", "W"), ("TH", "T"), ("FR", "F"), ("SA", "S"), ("SU", "S"))
        for code, letter in codes:
            check = GreenCheckBox(letter)
            check.setToolTip(code)
            check.setChecked(code in by_day)
            self.day_checks[code] = check
            day_row.addWidget(check)
        day_row.addStretch(1)
        weekday_layout.addLayout(day_row)
        self.root.addWidget(self.weekday_box)

        self.month_box = QWidget()
        month_layout = QHBoxLayout(self.month_box)
        month_layout.setContentsMargins(0, 0, 0, 0)
        month_layout.setSpacing(8)
        month_label = QLabel("DAY OF MONTH")
        month_label.setProperty("role", "sectionTitle")
        month_layout.addWidget(month_label)
        self.month_day = QSpinBox()
        self.month_day.setRange(-31, 31)
        self.month_day.setValue(int(by_month_day or 1))
        self.month_day.setToolTip("Negative values count from the end of the month.")
        self.month_day.setFixedWidth(76)
        month_layout.addWidget(self.month_day)
        month_layout.addStretch(1)
        self.root.addWidget(self.month_box)

        self.root.addWidget(Divider())
        ends_label = QLabel("ENDS")
        ends_label.setProperty("role", "sectionTitle")
        self.root.addWidget(ends_label)
        self.ends_combo = QComboBox()
        self.ends_combo.addItem("Never", "never")
        self.ends_combo.addItem("After a number of occurrences", "count")
        self.ends_combo.addItem("On a date", "until")
        if count is not None: self.ends_combo.setCurrentIndex(1)
        elif until is not None: self.ends_combo.setCurrentIndex(2)
        self.ends_combo.currentIndexChanged.connect(self._sync_visibility)
        self.root.addWidget(self.ends_combo)

        ends_row = QHBoxLayout()
        ends_row.setSpacing(8)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 999)
        self.count_spin.setValue(count or 10)
        self.count_spin.setPrefix("after ")
        self.count_spin.setSuffix(" occurrences")
        self.count_spin.setVisible(count is not None)
        ends_row.addWidget(self.count_spin)
        self.until_edit = QDateEdit()
        self.until_edit.setCalendarPopup(True)
        self.until_edit.setDisplayFormat("MMM d, yyyy")
        if until: self.until_edit.setDate(QDate(until.year, until.month, until.day))
        else: self.until_edit.setDate(QDate.currentDate().addMonths(3))
        self.until_edit.setVisible(until is not None)
        ends_row.addWidget(self.until_edit)
        ends_row.addStretch(1)
        self.root.addLayout(ends_row)

        self.summary = QLabel("")
        self.summary.setProperty("role", "faint")
        self.summary.setWordWrap(True)
        self.root.addWidget(self.summary)
        self.error_label = QLabel("")
        self.error_label.setProperty("role", "danger")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        remove = QPushButton("Remove recurrence")
        remove.setProperty("variant", "danger")
        remove.clicked.connect(self._remove)
        if rule is not None: buttons.addWidget(remove)
        save = QPushButton("Save recurrence")
        save.setProperty("variant", "primary")
        save.clicked.connect(self._validate)
        save.setDefault(True)
        buttons.addWidget(save)
        self.root.addLayout(buttons)

        for check in self.day_checks.values(): check.toggled.connect(self._update_summary)
        self.interval.valueChanged.connect(self._update_summary)
        self.month_day.valueChanged.connect(self._update_summary)
        self.ends_combo.currentIndexChanged.connect(self._update_summary)
        self.removed = False
        self._sync_visibility()
        self._update_summary()

    def _labeled(self, text: str, widget: QWidget) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        label = QLabel(text)
        label.setProperty("role", "sectionTitle")
        layout.addWidget(label)
        layout.addWidget(widget)
        return holder

    def _frequency(self) -> RecurrenceFrequency:
        return self.frequency.currentData() or RecurrenceFrequency.WEEKLY

    def _sync_visibility(self) -> None:
        frequency = self._frequency()
        self.weekday_box.setVisible(frequency is RecurrenceFrequency.WEEKLY)
        self.month_box.setVisible(frequency in (RecurrenceFrequency.MONTHLY, RecurrenceFrequency.YEARLY))
        self.unit_label.setText(
            {
                RecurrenceFrequency.DAILY: "day(s)",
                RecurrenceFrequency.WEEKLY: "week(s)",
                RecurrenceFrequency.MONTHLY: "month(s)",
                RecurrenceFrequency.YEARLY: "year(s)",
            }[frequency]
        )
        mode = self.ends_combo.currentData()
        self.count_spin.setVisible(mode == "count")
        self.until_edit.setVisible(mode == "until")
        self._update_summary()

    def _update_summary(self) -> None:
        if not hasattr(self, "summary"): return
        frequency = self._frequency()
        interval = self.interval.value()
        days = [code for code, check in self.day_checks.items() if check.isChecked()]
        frequency_text = {
            RecurrenceFrequency.DAILY: "day",
            RecurrenceFrequency.WEEKLY: "week",
            RecurrenceFrequency.MONTHLY: "month",
            RecurrenceFrequency.YEARLY: "year",
        }[frequency]
        text = f"Every {interval} {frequency_text}{'s' if interval != 1 else ''}"
        if frequency is RecurrenceFrequency.WEEKLY and days:
            names = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
            text += " on " + ", ".join(names[day] for day in days)
        if frequency in (RecurrenceFrequency.MONTHLY, RecurrenceFrequency.YEARLY):
            text += f" on day {self.month_day.value()}"
        mode = self.ends_combo.currentData()
        if mode == "count": text += f", {self.count_spin.value()} occurrences"
        elif mode == "until": text += f", until {self.until_edit.date().toString('MMM d, yyyy')}"
        if self._window_start and self._window_end:
            text += f" · {self._window_start:%H:%M}–{self._window_end:%H:%M}"
        self.summary.setText(text)

    def _validate(self) -> None:
        if self._frequency() is RecurrenceFrequency.WEEKLY and not any(
            check.isChecked() for check in self.day_checks.values()
        ):
            self.error_label.setText("Select at least one weekday.")
            self.error_label.show()
            return
        if self._frequency() in (
            RecurrenceFrequency.MONTHLY,
            RecurrenceFrequency.YEARLY,
        ) and self.month_day.value() == 0:
            self.error_label.setText("Month day cannot be 0.")
            self.error_label.show()
            return
        self.accept()

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    def values(self) -> dict:
        frequency = self._frequency()
        days = [code for code, check in self.day_checks.items() if check.isChecked()]
        mode = self.ends_combo.currentData()
        start_h = self._window_start.hour if self._window_start else (self.rule.reset_hour if self.rule else 18)
        start_m = self._window_start.minute if self._window_start else (self.rule.reset_minute if self.rule else 0)
        end_h = self._window_end.hour if self._window_end else (self.rule.end_hour if self.rule else 19)
        end_m = self._window_end.minute if self._window_end else (self.rule.end_minute if self.rule else 0)
        return {
            "frequency": frequency,
            "interval": self.interval.value(),
            "by_day": ",".join(days) if frequency is RecurrenceFrequency.WEEKLY else None,
            "by_month_day": self.month_day.value()
            if frequency in (RecurrenceFrequency.MONTHLY, RecurrenceFrequency.YEARLY)
            else None,
            "occurrence_count": self.count_spin.value() if mode == "count" else None,
            "until_date": datetime(
                self.until_edit.date().year(),
                self.until_edit.date().month(),
                self.until_edit.date().day(),
                23,
                59,
            )
            if mode == "until"
            else None,
            "reset_hour": start_h,
            "reset_minute": start_m,
            "end_hour": end_h,
            "end_minute": end_m,
            "removed": self.removed,
        }

class AvailabilityDialog(BaseDialog):
    """Optional temporal availability editor.

    Availability is intentionally separate from Status, the scheduled block, and
    recurrence. It defaults to 24/7 and never changes dependency evaluation.
    """

    def __init__(
        self,
        windows: list[AvailabilityWindow] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(
            parent,
            "Availability",
            "Choose when this Task can actually be performed in the real world. "
            "Availability does not change its Status, schedule, or recurrence, and "
            "it never blocks completion. Times use the local system clock.",
            620,
        )
        self._values: list[AvailabilityWindow] = []
        initial = list(windows or [])
        self.always = GreenCheckBox("24 / 7 — always available")
        self.always.setChecked(not initial)
        self.always.toggled.connect(self._sync_visibility)
        self.root.addWidget(self.always)

        self.custom = QWidget()
        custom_layout = QVBoxLayout(self.custom)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        custom_layout.setSpacing(8)
        heading = QLabel("CUSTOM AVAILABILITY")
        heading.setProperty("role", "sectionTitle")
        custom_layout.addWidget(heading)
        hint = QLabel(
            "Select the weekdays for each window. Add as many windows as needed; a "
            "window that ends before it starts is treated as crossing midnight."
        )
        hint.setProperty("role", "faint")
        hint.setWordWrap(True)
        custom_layout.addWidget(hint)

        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        custom_layout.addWidget(self.rows_host)
        self._rows: list[dict] = []

        add_row = QHBoxLayout()
        add_row.addStretch(1)
        add_button = QPushButton("Add time window")
        add_button.clicked.connect(lambda: self._add_row())
        add_row.addWidget(add_button)
        custom_layout.addLayout(add_row)

        self.summary = QLabel("")
        self.summary.setProperty("role", "faint")
        self.summary.setWordWrap(True)
        custom_layout.addWidget(self.summary)
        self.root.addWidget(self.custom)

        self.error_label = QLabel("")
        self.error_label.setProperty("role", "danger")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save availability")
        save.setProperty("variant", "primary")
        save.clicked.connect(self._validate)
        save.setDefault(True)
        buttons.addWidget(save)
        self.root.addLayout(buttons)

        if initial:
            grouped: dict[tuple[int, int], set[int]] = {}
            for window in initial:
                grouped.setdefault((window.start_minute, window.end_minute), set()).add(window.weekday)
            for (start, end), days in sorted(grouped.items()):
                self._add_row(days=days, start=start, end=end)
        if not self._rows: self._add_row(days=set(range(7)))
        self._sync_visibility()
        self._update_summary()

    def _add_row(self, *, days: set[int] | None = None, start: int = 9 * 60, end: int = 17 * 60) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        checks: dict[int, QCheckBox] = {}
        for weekday, letter in enumerate(("M", "T", "W", "T", "F", "S", "S")):
            check = GreenCheckBox(letter)
            check.setToolTip(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[weekday])
            check.setChecked(days is None or weekday in days)
            check.toggled.connect(self._update_summary)
            checks[weekday] = check
            layout.addWidget(check)
        layout.addSpacing(6)
        start_edit = QTimeEdit()
        start_edit.setDisplayFormat("HH:mm")
        start_edit.setTime(QTime((start // 60) % 24, start % 60))
        start_edit.setMinimumWidth(76)
        start_edit.timeChanged.connect(self._update_summary)
        layout.addWidget(start_edit)
        dash = QLabel("–")
        dash.setStyleSheet(f"color: {c('muted')};")
        layout.addWidget(dash)
        end_edit = QTimeEdit()
        end_edit.setDisplayFormat("HH:mm")
        end_edit.setTime(QTime((end // 60) % 24, end % 60))
        end_edit.setMinimumWidth(76)
        end_edit.timeChanged.connect(self._update_summary)
        layout.addWidget(end_edit)
        remove = IconButton("close", "Remove this window", size=12)
        remove.setFixedSize(24, 24)
        remove.clicked.connect(lambda _=False, r=row: self._remove_row(r))
        layout.addWidget(remove)
        layout.addStretch(1)
        entry = {"widget": row, "checks": checks, "start": start_edit, "end": end_edit}
        self._rows.append(entry)
        self.rows_layout.addWidget(row)

    def _remove_row(self, row: QWidget) -> None:
        for entry in list(self._rows):
            if entry["widget"] is row:
                self._rows.remove(entry)
                row.hide()
                row.setParent(None)
                row.deleteLater()
                break
        self._update_summary()

    def _sync_visibility(self) -> None:
        self.custom.setVisible(not self.always.isChecked())
        self._update_summary()

    def _window_values(self) -> list[AvailabilityWindow]:
        windows: list[AvailabilityWindow] = []
        for entry in self._rows:
            days = [day for day, check in entry["checks"].items() if check.isChecked()]
            if not days:
                raise ValueError("Select at least one weekday for each availability window.")
            start = entry["start"].time()
            end = entry["end"].time()
            start_minute = start.hour() * 60 + start.minute()
            end_minute = end.hour() * 60 + end.minute()
            if end_minute == 0: end_minute = 24 * 60
            for day in days: windows.append(AvailabilityWindow(day, start_minute, end_minute))
        return normalize_availability_windows(windows)

    def _update_summary(self) -> None:
        if not hasattr(self, "summary"): return
        self.error_label.hide()
        if self.always.isChecked():
            self.summary.setText("24/7 — always available")
            return
        try: windows = self._window_values()
        except Exception as exc:  # noqa: BLE001 - user-facing validation text
            self.summary.setText(str(exc))
            return
        self.summary.setText(availability_summary(windows) if windows else "No availability windows configured.")

    def _validate(self) -> None:
        if self.always.isChecked():
            self._values = []
            self.accept()
            return
        try: windows = self._window_values()
        except Exception as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        if not windows:
            self.error_label.setText("Add at least one availability window.")
            self.error_label.show()
            return
        self._values = windows
        self.accept()

    def values(self) -> list[AvailabilityWindow]: return list(self._values)

class ScheduleConflictDialog(BaseDialog):
    def __init__(self, conflicts: list[CalendarItem], parent: QWidget | None = None):
        super().__init__(
            parent,
            "Schedule collision detected",
            "The requested block overlaps existing records. Review the collisions, "
            "then choose whether to force-save the local scheduling configuration.",
            620,
        )
        self.conflicts = conflicts
        self.force = False
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(320)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(8)
        if not conflicts:
            empty = QLabel("No collision details were returned.")
            empty.setProperty("role", "muted")
            layout.addWidget(empty)
        for conflict in conflicts:
            layout.addWidget(_conflict_row(conflict))
        layout.addStretch(1)
        scroll.setWidget(content)
        self.root.addWidget(scroll)
        warning = QLabel("Force Save keeps the local block even though a collision remains.")
        warning.setStyleSheet(
            f"color: {c('amber')}; background: {rgba('amber', 0.07)}; "
            f"border: 1px solid {rgba('amber', 0.22)}; border-radius: 8px; "
            "padding: 8px 10px; font-size: 11px;"
        )
        warning.setWordWrap(True)
        self.root.addWidget(warning)
        self.root.addWidget(Divider())
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        force = QPushButton("Force Save")
        force.setProperty("variant", "primary")
        force.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        force.clicked.connect(self._force)
        buttons.addWidget(force)
        self.root.addLayout(buttons)

    def _force(self) -> None:
        self.force = True
        self.accept()

def _conflict_row(conflict: CalendarItem) -> QFrame:
    row = Card(raised=True)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(10)
    source = Badge("GOOGLE" if conflict.kind == "external" else "EFOS", "accent" if conflict.kind == "external" else "active")
    source.setMinimumWidth(64)
    layout.addWidget(source, 0, Qt.AlignmentFlag.AlignTop)
    text = QVBoxLayout()
    text.setSpacing(2)
    title = QLabel(conflict.title)
    title.setStyleSheet(f"color: {c('text')}; font-size: 12px; font-weight: 600;")
    title.setWordWrap(True)
    text.addWidget(title)
    span = QLabel(f"{fmt_datetime(conflict.start)} → {fmt_datetime(conflict.end)}")
    span.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
    text.addWidget(span)
    if conflict.detail:
        detail = QLabel(conflict.detail)
        detail.setStyleSheet(f"color: {c('faint')}; font-size: 10px;")
        detail.setWordWrap(True)
        text.addWidget(detail)
    layout.addLayout(text, 1)
    return row

class CustomQueryDialog(BaseDialog):
    def __init__(self, label: str = "", prompt: str = "", parent: QWidget | None = None):
        super().__init__(
            parent,
            "Custom query",
            "The display label is independent of the instruction payload sent to the "
            "model. Labels stay concise; the payload can be extensive.",
            560,
        )
        self.label_edit = QLineEdit(label)
        self.label_edit.setPlaceholderText("e.g. Overdue audit")
        self.label_edit.setMinimumHeight(36)
        self.prompt_edit = QPlainTextEdit(prompt)
        self.prompt_edit.setPlaceholderText("Write the full instruction sent to the assistant…")
        self.prompt_edit.setMinimumHeight(170)
        self.root.addWidget(self._labeled("LABEL", self.label_edit))
        self.root.addWidget(self._labeled("BACKEND INSTRUCTION PAYLOAD", self.prompt_edit))
        self.error_label = QLabel("")
        self.error_label.setProperty("role", "danger")
        self.error_label.hide()
        self.root.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Save query")
        save.setProperty("variant", "primary")
        save.clicked.connect(self._validate)
        save.setDefault(True)
        buttons.addWidget(save)
        self.root.addLayout(buttons)

    def _labeled(self, text: str, widget: QWidget) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        label = QLabel(text)
        label.setProperty("role", "sectionTitle")
        layout.addWidget(label)
        layout.addWidget(widget)
        return holder

    def _validate(self) -> None:
        if not self.label_edit.text().strip():
            self.error_label.setText("A label is required.")
            self.error_label.show()
            return
        if not self.prompt_edit.toPlainText().strip():
            self.error_label.setText("The backend instruction payload cannot be empty.")
            self.error_label.show()
            return
        self.accept()

    def values(self) -> tuple[str, str]: return self.label_edit.text().strip(), self.prompt_edit.toPlainText().strip()

class SchemaMismatchDialog(BaseDialog):
    def __init__(self, message: str, discrepancies: list[str], parent: QWidget | None = None):
        super().__init__(
            parent,
            "Database schema validation failed",
            "Startup was aborted. EFOS did not modify, repair, or drop any object.",
            680,
        )
        body = QLabel(message)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {c('text_soft')}; font-size: 12px;")
        self.root.addWidget(body)
        report = QPlainTextEdit()
        report.setReadOnly(True)
        report.setPlainText("\n".join(f"• {item}" for item in discrepancies))
        report.setMinimumHeight(240)
        self.root.addWidget(report)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close EFOS")
        close.setProperty("variant", "primary")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        self.root.addLayout(buttons)

class DatabaseSetupDialog(BaseDialog):
    """Single-step PostgreSQL setup: provisions schema and auto-generates the restricted app role."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "efos",
        app_user: str = "efos_app",
        parent: QWidget | None = None,
    ):
        super().__init__(
            parent,
            "Database setup",
            "Connect to your PostgreSQL server to initialize the EFOS database. "
            "EFOS automatically creates a restricted daily runtime role and stores "
            "its credentials securely in Windows Credential Manager.",
            560,
        )
        self.result_values: dict | None = None
        self._default_app_user = app_user or "efos_app"
        self._generated_app_password = secrets.token_urlsafe(32)

        connection = QWidget()
        grid = QGridLayout(connection)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        self.host_edit = QLineEdit(host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        self.database_edit = QLineEdit(database)
        grid.addWidget(QLabel("HOST"), 0, 0)
        grid.addWidget(self.host_edit, 0, 1)
        grid.addWidget(QLabel("PORT"), 0, 2)
        grid.addWidget(self.port_spin, 0, 3)
        grid.addWidget(QLabel("DATABASE"), 1, 0)
        grid.addWidget(self.database_edit, 1, 1, 1, 3)
        self.root.addWidget(connection)

        admin_frame = Card(flat=True)
        admin_layout = QVBoxLayout(admin_frame)
        admin_layout.setContentsMargins(14, 12, 14, 12)
        admin_layout.setSpacing(6)
        admin_title = QLabel("POSTGRESQL ADMINISTRATOR")
        admin_title.setProperty("role", "sectionTitle")
        admin_layout.addWidget(admin_title)
        admin_row = QHBoxLayout()
        self.admin_user = QLineEdit("postgres")
        self.admin_password = QLineEdit()
        self.admin_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.admin_password.setPlaceholderText("postgres password")
        admin_row.addWidget(self.admin_user)
        admin_row.addWidget(self.admin_password)
        admin_layout.addLayout(admin_row)
        self.admin_test = QPushButton("Test connection")
        self.admin_test.clicked.connect(self._test_admin)
        admin_layout.addWidget(self.admin_test)
        self.root.addWidget(admin_frame)

        role_note = QLabel(
            f"Daily runtime will automatically use a restricted '{self._default_app_user}' "
            "database role with a generated secret stored in Windows Credential Manager."
        )
        role_note.setProperty("role", "faint")
        role_note.setWordWrap(True)
        self.root.addWidget(role_note)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {c('muted')}; font-size: 11px;")
        self.root.addWidget(self.status)
        self.root.addWidget(Divider())
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Quit")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.init_button = QPushButton("Initialize database")
        self.init_button.setProperty("variant", "primary")
        self.init_button.clicked.connect(self._initialize)
        self.init_button.setDefault(True)
        buttons.addWidget(self.init_button)
        self.root.addLayout(buttons)
        self.admin_password.returnPressed.connect(self._initialize)
        self.admin_password.setFocus()
        self.inspection = False

    def profiles(self) -> tuple[DbProfile, DbProfile]:
        host = self.host_edit.text().strip()
        port = self.port_spin.value()
        admin_user = self.admin_user.text().strip()
        app_user = self._default_app_user
        if app_user == admin_user: app_user = f"{admin_user}_app"
        admin = DbProfile(host, port, admin_user, self.admin_password.text(), "postgres")
        target = DbProfile(host, port, app_user, self._generated_app_password, self.database_edit.text().strip())
        return admin, target

    def _test_admin(self) -> None:
        admin, _ = self.profiles()
        ok, message = test_connection(admin)
        self._show_status(ok, message)

    def _show_status(self, ok: bool, message: str) -> None:
        color = c("green") if ok else c("red")
        self.status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self.status.setText(("Connected. " if ok else "Failed. ") + message)

    def _initialize(self) -> None:
        admin, target = self.profiles()
        if not target.database:
            self._show_status(False, "Database name is required.")
            return
        if not admin.password:
            self._show_status(False, "PostgreSQL administrator password is required.")
            return
        self.init_button.setEnabled(False)
        self.status.setStyleSheet(f"color: {c('muted')}; font-size: 11px;")
        self.status.setText("Creating database, executing the canonical schema, verifying…")
        try:
            provision_database(admin, target)
            save_runtime_profile(
                {
                    "host": target.host,
                    "port": target.port,
                    "user": target.user,
                    "database": target.database,
                    "admin_user": admin.user,
                }
            )
            save_runtime_password(target.user, target.password)
            self._show_status(True, "Database initialized and restricted application role granted.")
            self.result_values = {
                "host": target.host,
                "port": target.port,
                "user": target.user,
                "database": target.database,
                "admin_user": admin.user,
            }
            self.accept()
        except EFOSError as exc:
            self._show_status(False, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._show_status(False, f"{type(exc).__name__}: {exc}")
        finally:
            self.init_button.setEnabled(True)

def confirm_dialog(
    parent: QWidget | None,
    title: str,
    body: str,
    *,
    confirm_text: str = "Delete",
    destructive: bool = True,
) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(title)
    box.setInformativeText(body)
    box.setIcon(QMessageBox.Icon.Warning if destructive else QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok)
    ok = box.button(QMessageBox.StandardButton.Ok)
    if ok:
        ok.setText(confirm_text)
        if destructive:
            ok.setStyleSheet(
                f"QPushButton {{ color: {c('red')}; background: {rgba('red', 0.10)}; "
                f"border: 1px solid {rgba('red', 0.35)}; border-radius: 7px; "
                "padding: 6px 14px; }}"
            )
    cancel = box.button(QMessageBox.StandardButton.Cancel)
    if cancel: cancel.setText("Cancel")
    enable_native_title_bar(box)
    return box.exec() == QMessageBox.StandardButton.Ok
