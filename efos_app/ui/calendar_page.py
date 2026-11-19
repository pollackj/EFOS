"""Google Calendar-style four-view interface over local tasks and cached events."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import EFOSError
from ..formatting import (
    fmt_datetime,
    month_grid,
    month_label,
    week_bounds,
    month_bounds,
    day_bounds,
    hour_label,
)
from ..models import CalendarItem, SyncState
from ..service import EFOSService, WorkspaceView
from ..workers import CalendarConnectWorker, CalendarSyncWorker
from .icons import icon
from .theme import c, rgba, state_color
from .widgets import Badge, Divider, SegmentedControl

HOUR_HEIGHT = 54
# Vertical breathing room inside the scrollable time grid: the 12 AM label at
# 00:00 must never be clipped when the view is scrolled to the very top, and
# the 24:00 closing border needs air below it when scrolled to the very bottom.
GRID_PAD = 14
GUTTER = 58
VIEWS = ("Day", "Week", "Month", "Schedule")
# The page watchdog sits above the service's own 90 s OAuth deadline so the
# actionable timeout message normally comes from the worker; this is only a
# last-resort reset for a wedged thread.
OAUTH_UI_TIMEOUT_MS = 180_000

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

def item_tone(item: CalendarItem) -> str:
    if item.kind == "external":
        return c("teal")
    return state_color(item.state) if item.state else c("task")

def item_fill(item: CalendarItem) -> QColor:
    color = QColor(item_tone(item))
    color.setAlpha(52 if item.kind == "external" else 66)
    return color

def _elide(painter: QPainter, text: str, width: float) -> str:
    metrics = QFontMetricsF(painter.font())
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(10.0, width))

class _ItemCanvas(QWidget):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.items: list[CalendarItem] = []
        self._hits: list[tuple[QRectF, CalendarItem]] = []
        self.setMouseTracking(True)

    def _register(self, rect: QRectF, item: CalendarItem) -> None:
        self._hits.append((rect, item))

    def mouseDoubleClickEvent(self, event):  # type: ignore[no-untyped-def]
        point = event.position()
        for rect, item in reversed(self._hits):
            if rect.contains(point):
                if item.kind == "task":
                    self.open_requested.emit(item.id)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

class DayCanvas(_ItemCanvas):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.setFixedHeight(GRID_PAD * 2 + 24 * HOUR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_items(self, day: datetime, items: list[CalendarItem]) -> None:
        self.day = day.replace(hour=0, minute=0, second=0, microsecond=0)
        self.items = [item for item in items if not item.all_day]
        self.updateGeometry()
        self.update()

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._hits = []
        width = self.width()
        painter.fillRect(self.rect(), QColor(c("surface")))
        painter.setFont(QFont("Segoe UI", 8))
        for hour in range(24):
            y = GRID_PAD + hour * HOUR_HEIGHT
            painter.setPen(QPen(QColor(c("border")), 1))
            painter.drawLine(GUTTER, y, width, y)
            painter.setPen(QColor(c("faint")))
            painter.drawText(QRectF(0, y - 7, GUTTER - 8, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hour_label(hour))
        painter.setPen(QPen(QColor(c("border")), 1))
        painter.drawLine(GUTTER, GRID_PAD, GUTTER, GRID_PAD + 24 * HOUR_HEIGHT)
        # Crisp closing border at 24:00, with GRID_PAD of breathing room below.
        painter.setPen(QPen(QColor(c("border_hi")), 1))
        painter.drawLine(
            GUTTER, GRID_PAD + 24 * HOUR_HEIGHT, width, GRID_PAD + 24 * HOUR_HEIGHT
        )

        layout_items = _overlap_layout(self.items)
        for item, column, columns in layout_items:
            start = max(item.start, self.day)
            end = min(item.end, self.day + timedelta(days=1))
            top = GRID_PAD + (start - self.day).total_seconds() / 3600 * HOUR_HEIGHT
            bottom = GRID_PAD + (end - self.day).total_seconds() / 3600 * HOUR_HEIGHT
            height = max(18.0, bottom - top)
            available = width - GUTTER - 8
            rect = QRectF(
                GUTTER + 4 + column * (available / columns),
                top + 1,
                max(20.0, available / columns - 4),
                height - 2,
            )
            self._draw_item(painter, rect, item)
            self._register(rect, item)

    def _draw_item(self, painter: QPainter, rect: QRectF, item: CalendarItem) -> None:
        painter.setPen(QPen(QColor(item_tone(item)), 1))
        painter.setBrush(item_fill(item))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QPen(QColor(item_tone(item)), 3))
        painter.drawLine(QPointF(rect.left() + 1, rect.top() + 4), QPointF(rect.left() + 1, rect.bottom() - 4))
        painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        painter.setPen(QColor(c("text")))
        painter.drawText(
            rect.adjusted(7, 3, -4, -2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            _elide(painter, item.title, rect.width() - 12),
        )
        if rect.height() > 32:
            painter.setFont(QFont("Segoe UI", 7))
            painter.setPen(QColor(c("muted")))
            painter.drawText(
                rect.adjusted(7, 16, -4, -2),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                f"{item.start:%H:%M} – {item.end:%H:%M}",
            )

def _overlap_layout(
    items: list[CalendarItem],
) -> list[tuple[CalendarItem, int, int]]:
    if not items:
        return []
    ordered = sorted(items, key=lambda item: (item.start, item.end))
    columns_end: list[datetime] = []
    assigned: list[tuple[CalendarItem, int]] = []
    for item in ordered:
        placed = False
        for index, end in enumerate(columns_end):
            if end <= item.start:
                columns_end[index] = item.end
                assigned.append((item, index))
                placed = True
                break
        if not placed:
            assigned.append((item, len(columns_end)))
            columns_end.append(item.end)
    total = max(1, len(columns_end))
    return [(item, column, total) for item, column in assigned]

class DayView(QWidget):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.allday = QFrame()
        self.allday.setProperty("card", "flat")
        self.allday_layout = QHBoxLayout(self.allday)
        self.allday_layout.setContentsMargins(10, 5, 10, 5)
        self.allday_layout.setSpacing(6)
        layout.addWidget(self.allday)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.canvas = DayCanvas()
        self.canvas.open_requested.connect(self.open_requested)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)

    def set_data(self, day: datetime, items: list[CalendarItem]) -> None:
        _clear_layout(self.allday_layout)
        label = QLabel("ALL-DAY")
        label.setProperty("role", "sectionTitle")
        self.allday_layout.addWidget(label)
        self.allday_layout.addStretch(1)
        around = [
            item
            for item in items
            if item.all_day and item.start.date() <= day.date() < item.end.date()
        ]
        if not around:
            empty = QLabel("No all-day events")
            empty.setStyleSheet(f"color: {c('faint')}; font-size: 10px;")
            self.allday_layout.addWidget(empty)
        for item in around:
            chip = QLabel(item.title)
            chip.setToolTip(f"{item.title} · external calendar")
            chip.setStyleSheet(
                f"background: {rgba(item_tone(item), 0.18)}; color: {c('text')}; "
                f"border: 1px solid {rgba(item_tone(item), 0.45)}; border-radius: 6px; "
                "padding: 2px 8px; font-size: 10px;"
            )
            self.allday_layout.addWidget(chip)
        self.allday_layout.addStretch(1)
        self.canvas.set_items(day, items)
        QTimer.singleShot(
            0,
            lambda: self.scroll.verticalScrollBar().setValue(
                8 * HOUR_HEIGHT - 20 + GRID_PAD
            ),
        )

class WeekAllDayCanvas(_ItemCanvas):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.week_start = datetime.now()
        self.items: list[CalendarItem] = []
        self.setFixedHeight(34)

    def set_items(self, week_start: datetime, items: list[CalendarItem]) -> None:
        self.week_start = week_start
        self.items = [
            item
            for item in items
            if item.all_day or (item.end.date() - item.start.date()).days >= 1
        ]
        rows = self._rows()
        self.setFixedHeight(max(34, 8 + rows * 22))
        self.update()

    def _rows(self) -> int:
        spans = []
        for item in self.items:
            start_col = max(0, (item.start.date() - self.week_start.date()).days)
            end_col = min(6, (item.end - timedelta(seconds=1)).date().toordinal() - self.week_start.date().toordinal())
            spans.append((start_col, max(start_col, end_col)))
        spans.sort()
        row_ends: list[int] = []
        for start_col, end_col in spans:
            for index, end in enumerate(row_ends):
                if end < start_col:
                    row_ends[index] = end_col
                    break
            else:
                row_ends.append(end_col)
        return max(1, len(row_ends))

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._hits = []
        painter.fillRect(self.rect(), QColor(c("surface")))
        column_width = (self.width() - GUTTER) / 7
        row_ends: list[int] = []
        for item in self.items:
            start_col = max(0, (item.start.date() - self.week_start.date()).days)
            end_col = min(6, (item.end - timedelta(seconds=1)).date().toordinal() - self.week_start.date().toordinal())
            end_col = max(start_col, end_col)
            row = len(row_ends)
            for index, value in enumerate(row_ends):
                if value < start_col:
                    row = index
                    row_ends[index] = end_col
                    break
            else:
                row_ends.append(end_col)
            rect = QRectF(GUTTER + start_col * column_width + 2, 4 + row * 22, (end_col - start_col + 1) * column_width - 4, 18)
            painter.setPen(QPen(QColor(item_tone(item)), 1))
            painter.setBrush(item_fill(item))
            painter.drawRoundedRect(rect, 5, 5)
            painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            painter.setPen(QColor(c("text")))
            painter.drawText(
                rect.adjusted(6, 1, -4, -1),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _elide(painter, item.title, rect.width() - 12),
            )
            self._register(rect, item)

class WeekCanvas(_ItemCanvas):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.week_start = datetime.now()
        self.setFixedHeight(GRID_PAD * 2 + 24 * HOUR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_items(self, week_start: datetime, items: list[CalendarItem]) -> None:
        self.week_start = week_start
        self.items = [
            item
            for item in items
            if not item.all_day
            and (item.end.date() - item.start.date()).days < 1
        ]
        self.update()

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._hits = []
        width = self.width()
        painter.fillRect(self.rect(), QColor(c("surface")))
        column_width = (width - GUTTER) / 7
        grid_bottom = GRID_PAD + 24 * HOUR_HEIGHT
        painter.setFont(QFont("Segoe UI", 8))
        for hour in range(24):
            y = GRID_PAD + hour * HOUR_HEIGHT
            painter.setPen(QPen(QColor(c("border")), 1))
            painter.drawLine(GUTTER, y, width, y)
            painter.setPen(QColor(c("faint")))
            painter.drawText(
                QRectF(0, y - 7, GUTTER - 8, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                hour_label(hour),
            )
        today = datetime.now().date()
        for day_index in range(7):
            day_date = (self.week_start + timedelta(days=day_index)).date()
            x = GUTTER + day_index * column_width
            painter.setPen(QPen(QColor(c("border")), 1))
            painter.drawLine(QPointF(x, GRID_PAD), QPointF(x, grid_bottom))
            if day_date == today:
                painter.setPen(Qt.PenStyle.NoPen)
                highlight = QColor(c("accent"))
                highlight.setAlphaF(0.06)
                painter.setBrush(highlight)
                painter.drawRect(
                    QRectF(x, GRID_PAD, column_width, 24 * HOUR_HEIGHT)
                )
        # Crisp closing border at 24:00 with GRID_PAD of breathing room below.
        painter.setPen(QPen(QColor(c("border_hi")), 1))
        painter.drawLine(GUTTER, grid_bottom, width, grid_bottom)
        for day_index in range(7):
            day = (self.week_start + timedelta(days=day_index)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_items = [
                item
                for item in self.items
                if item.start.date() == day.date()
            ]
            layout_items = _overlap_layout(day_items)
            for item, column, columns in layout_items:
                start = max(item.start, day)
                end = min(item.end, day + timedelta(days=1))
                top = GRID_PAD + (start - day).total_seconds() / 3600 * HOUR_HEIGHT
                bottom = GRID_PAD + (end - day).total_seconds() / 3600 * HOUR_HEIGHT
                available = column_width
                rect = QRectF(
                    GUTTER + day_index * column_width + 3 + column * (available / columns),
                    top + 1,
                    max(16.0, available / columns - 5),
                    max(16.0, bottom - top - 2),
                )
                painter.setPen(QPen(QColor(item_tone(item)), 1))
                painter.setBrush(item_fill(item))
                painter.drawRoundedRect(rect, 5, 5)
                painter.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
                painter.setPen(QColor(c("text")))
                painter.drawText(
                    rect.adjusted(4, 2, -3, -2),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    _elide(painter, item.title, rect.width() - 7),
                )
                self._register(rect, item)

class WeekView(QWidget):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header = QWidget()
        self.header_layout = QHBoxLayout(self.header)
        self.header_layout.setContentsMargins(GUTTER, 0, 0, 0)
        self.header_layout.setSpacing(0)
        layout.addWidget(self.header)
        self.allday = WeekAllDayCanvas()
        self.allday.open_requested.connect(self.open_requested)
        layout.addWidget(self.allday)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.canvas = WeekCanvas()
        self.canvas.open_requested.connect(self.open_requested)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)

    def set_data(self, week_start: datetime, items: list[CalendarItem]) -> None:
        _clear_layout(self.header_layout)
        self.header_layout.setContentsMargins(GUTTER, 0, 0, 0)
        self.header_layout.setSpacing(0)
        for index in range(7):
            day = week_start + timedelta(days=index)
            label = QLabel(f"{day.strftime('%a').upper()}\n{day.strftime('%b %d')}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFixedHeight(40)
            today = day.date() == datetime.now().date()
            label.setStyleSheet(
                f"color: {c('accent') if today else c('muted')}; font-size: 10px; "
                f"font-weight: {'700' if today else '500'};"
            )
            self.header_layout.addWidget(label, 1)
        self.allday.set_items(week_start, items)
        self.canvas.set_items(week_start, items)
        QTimer.singleShot(
            0,
            lambda: self.scroll.verticalScrollBar().setValue(
                8 * HOUR_HEIGHT - 20 + GRID_PAD
            ),
        )

class MonthCell(QFrame):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CalendarCell")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(104)
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(7, 5, 7, 5)
        self.layout_box.setSpacing(2)

    def set_day(
        self,
        day: date,
        items: list[CalendarItem],
        *,
        in_month: bool,
        is_today: bool,
    ) -> None:
        _clear_layout(self.layout_box)
        self.setProperty("today", "true" if is_today else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        number = QLabel(str(day.day))
        number.setStyleSheet(
            f"color: {c('text') if in_month else c('faint')}; font-size: 11px; "
            f"font-weight: {'700' if is_today else '600'};"
            + (
                f"background: {c('accent')}; color: white; border-radius: 8px; padding: 0 4px;"
                if is_today
                else ""
            )
        )
        header.addWidget(number)
        header.addStretch(1)
        self.layout_box.addLayout(header)
        visible = items[:3]
        for item in visible:
            row = QLabel()
            external = item.kind == "external"
            prefix = "" if item.all_day else item.start.strftime("%H:%M ") 
            row.setText(f"{prefix}{item.title}")
            row.setToolTip(
                f"{item.title}\n{fmt_datetime(item.start)} → {fmt_datetime(item.end)}"
            )
            row.setStyleSheet(
                f"color: {c('text_soft') if external else c('text')}; font-size: 9px; "
                f"background: {rgba(item_tone(item), 0.12)}; border-radius: 4px; "
                f"border-left: 2px solid {item_tone(item)}; padding: 1px 3px;"
            )
            row.setCursor(
                Qt.CursorShape.PointingHandCursor
                if not external
                else Qt.CursorShape.ArrowCursor
            )
            row.mouseDoubleClickEvent = (  # type: ignore[method-assign]
                lambda event, item_id=item.id, external=external: self._activate(event, item_id, external)
            )
            self.layout_box.addWidget(row)
        hidden = len(items) - len(visible)
        if hidden > 0:
            more = QLabel(f"+{hidden} more")
            more.setStyleSheet(f"color: {c('accent')}; font-size: 8px; font-weight: 700;")
            self.layout_box.addWidget(more)
        self.layout_box.addStretch(1)

    def _activate(self, event, item_id: str, external: bool) -> None:  # type: ignore[no-untyped-def]
        if not external:
            self.open_requested.emit(item_id)
        event.accept()

class MonthView(QWidget):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header = QWidget()
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        for name in ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"):
            label = QLabel(name)
            label.setProperty("role", "sectionTitle")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header_layout.addWidget(label, 1)
        layout.addWidget(self.header)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        layout.addWidget(self.grid_host, 1)
        self.cells: list[MonthCell] = []
        for index in range(42):
            cell = MonthCell()
            cell.open_requested.connect(self.open_requested)
            self.cells.append(cell)
            self.grid.addWidget(cell, index // 7, index % 7)

    def set_data(self, anchor: datetime, items: list[CalendarItem]) -> None:
        grid = month_grid(anchor)
        today = date.today()
        for index, day in enumerate(day for row in grid for day in row):
            day_items = sorted(
                [
                    item
                    for item in items
                    if item.start.date() <= day < max(item.end.date(), item.start.date() + timedelta(days=1))
                ],
                key=lambda item: (not item.all_day, item.start),
            )
            self.cells[index].set_day(day, day_items, in_month=day.month == anchor.month, is_today=day == today)

class ScheduleView(QWidget):
    open_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.host = QWidget()
        self.content = QVBoxLayout(self.host)
        self.content.setContentsMargins(8, 6, 12, 12)
        self.content.setSpacing(6)
        self.scroll.setWidget(self.host)
        layout.addWidget(self.scroll)

    def set_data(self, anchor: datetime, items: list[CalendarItem]) -> None:
        _clear_layout(self.content)
        if not items:
            empty = QLabel("No scheduled records in this range. Sync Google Calendar or schedule a task.")
            empty.setProperty("role", "muted")
            empty.setWordWrap(True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.content.addWidget(empty)
            self.content.addStretch(1)
            return
        groups: dict[date, list[CalendarItem]] = {}
        for item in sorted(items, key=lambda entry: (entry.start, entry.title.casefold())):
            groups.setdefault(item.start.date(), []).append(item)
        for day in sorted(groups):
            header = QLabel(day.strftime("%A, %B %d, %Y"))
            header.setStyleSheet(
                f"color: {c('text')}; font-size: 12px; font-weight: 700; "
                "padding: 10px 2px 2px 2px;"
            )
            self.content.addWidget(header)
            self.content.addWidget(Divider())
            for item in groups[day]:
                row = QFrame()
                row.setCursor(
                    Qt.CursorShape.PointingHandCursor
                    if item.kind == "task"
                    else Qt.CursorShape.ArrowCursor
                )
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(4, 6, 4, 6)
                row_layout.setSpacing(10)
                when = QLabel(
                    "all-day"
                    if item.all_day
                    else f"{item.start.strftime('%H:%M')} – {item.end.strftime('%H:%M')}"
                )
                when.setFixedWidth(96)
                when.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
                row_layout.addWidget(when)
                bar = QFrame()
                bar.setFixedSize(3, 26)
                bar.setStyleSheet(f"background: {item_tone(item)}; border-radius: 2px;")
                row_layout.addWidget(bar)
                text = QVBoxLayout()
                text.setSpacing(0)
                title = QLabel(item.title)
                title.setStyleSheet(
                    f"color: {c('text')}; font-size: 11px; font-weight: 600;"
                )
                text.addWidget(title)
                detail = QLabel(
                    "Google Calendar · read-only"
                    if item.kind == "external"
                    else f"EFOS task · {item.state}"
                )
                detail.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
                text.addWidget(detail)
                row_layout.addLayout(text, 1)
                row_layout.addWidget(
                    Badge("GOOGLE" if item.kind == "external" else "EFOS", "accent" if item.kind == "external" else "active")
                )
                if item.kind == "task":
                    row.mouseDoubleClickEvent = (  # type: ignore[method-assign]
                        lambda event, item_id=item.id: self._activate(event, item_id)
                    )
                self.content.addWidget(row)
        self.content.addStretch(1)

    def _activate(self, event, item_id: str) -> None:  # type: ignore[no-untyped-def]
        self.open_requested.emit(item_id)
        event.accept()

class CalendarPage(QWidget):
    open_node = pyqtSignal(object)
    toast_requested = pyqtSignal(str, str)

    def __init__(self, service: EFOSService, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.view: WorkspaceView | None = None
        self.anchor = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.mode = "week"
        self.sync_state = SyncState()
        self._sync_worker: CalendarSyncWorker | None = None
        self._connect_worker: CalendarConnectWorker | None = None
        # Failsafe: the OAuth worker resets the button itself, and this timer
        # guarantees "Waiting for Google…" can never persist indefinitely even
        # if the worker thread wedges inside the browser hand-off.
        self._connect_watchdog = QTimer(self)
        self._connect_watchdog.setSingleShot(True)
        self._connect_watchdog.timeout.connect(self._connect_timed_out)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 14, 24, 18)
        root.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.prev_button = QPushButton()
        self.prev_button.setIcon(icon("chevron_left", c("muted"), 15))
        self.prev_button.setFixedWidth(34)
        self.prev_button.clicked.connect(lambda: self._navigate(-1))
        toolbar.addWidget(self.prev_button)
        self.today_button = QPushButton("Today")
        self.today_button.clicked.connect(self._today)
        toolbar.addWidget(self.today_button)
        self.next_button = QPushButton()
        self.next_button.setIcon(icon("chevron_right", c("muted"), 15))
        self.next_button.setFixedWidth(34)
        self.next_button.clicked.connect(lambda: self._navigate(1))
        toolbar.addWidget(self.next_button)
        self.range_label = QLabel("")
        self.range_label.setStyleSheet(
            f"color: {c('text')}; font-size: 15px; font-weight: 650; padding: 0 8px;"
        )
        toolbar.addWidget(self.range_label)
        toolbar.addStretch(1)
        self.view_switch = SegmentedControl(list(VIEWS))
        self.view_switch.select(1, emit=False)
        self.view_switch.changed.connect(self._change_view)
        toolbar.addWidget(self.view_switch)
        self.connect_button = QPushButton("Connect Google")
        self.connect_button.setIcon(icon("google", c("muted"), 14))
        self.connect_button.clicked.connect(self._connect_google)
        toolbar.addWidget(self.connect_button)
        self.sync_button = QPushButton("Sync")
        self.sync_button.setIcon(icon("sync", c("muted"), 14))
        self.sync_button.clicked.connect(lambda: self.sync_now())
        toolbar.addWidget(self.sync_button)
        root.addLayout(toolbar)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            f"color: {c('amber')}; background: {rgba('amber', 0.08)}; "
            f"border: 1px solid {rgba('amber', 0.24)}; border-radius: 8px; "
            "padding: 6px 10px; font-size: 10px;"
        )
        self.warning.hide()
        root.addWidget(self.warning)

        self.stack = QStackedWidget()
        self.day_view = DayView()
        self.week_view = WeekView()
        self.month_view = MonthView()
        self.schedule_view = ScheduleView()
        for widget in (self.day_view, self.week_view, self.month_view, self.schedule_view):
            widget.open_requested.connect(self._open_task)
            self.stack.addWidget(widget)
        root.addWidget(self.stack, 1)
        self._refresh_sync_state()

    # ---------------------------------------------------------------- navigation
    def _change_view(self, index: int) -> None:
        self.mode = ("day", "week", "month", "schedule")[index]
        self.reload()

    def _navigate(self, direction: int) -> None:
        if self.mode == "day":
            self.anchor += timedelta(days=direction)
        elif self.mode == "week":
            self.anchor += timedelta(days=7 * direction)
        elif self.mode == "month":
            year = self.anchor.year
            month = self.anchor.month + direction
            if month < 1:
                year, month = year - 1, 12
            elif month > 12:
                year, month = year + 1, 1
            self.anchor = self.anchor.replace(year=year, month=month, day=1)
        else:
            self.anchor += timedelta(days=30 * direction)
        self.reload()

    def _today(self) -> None:
        self.anchor = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.reload()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # Entering Calendar with stored credentials should refresh a stale
        # snapshot without waiting for the 15-minute background timer.
        QTimer.singleShot(0, self._maybe_auto_sync)

    def refresh(self, view: WorkspaceView) -> None:
        self.view = view
        self._refresh_sync_state()
        self.reload()
        QTimer.singleShot(0, self._maybe_auto_sync)

    def _maybe_auto_sync(self) -> None:
        """Sync in the background when valid stored credentials are stale.

        The check is deliberately best-effort: a failure here must never raise
        into the UI thread or interrupt page rendering.
        """
        if self._sync_worker is not None or self._connect_worker is not None:
            return
        try:
            state = self.service.calendar_sync_state()
        except Exception:
            return
        if not state.connected:
            return
        if state.status == "ok" and state.last_sync_at is not None:
            try:
                age = datetime.now() - state.last_sync_at
            except (TypeError, ValueError):
                age = None
            if age is not None and age < timedelta(minutes=5):
                return
        self.sync_now()

    def reload(self) -> None:
        start, end, label = self._range()
        self.range_label.setText(label)
        try:
            items = self.service.calendar_items(start, end)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            items = []
        index = ("day", "week", "month", "schedule").index(self.mode)
        self.stack.setCurrentIndex(index)
        self.prev_button.setEnabled(index != 3)
        self.next_button.setEnabled(index != 3)
        if self.mode == "day":
            self.day_view.set_data(self.anchor, items)
        elif self.mode == "week":
            week_start, _ = week_bounds(self.anchor)
            self.week_view.set_data(week_start, items)
        elif self.mode == "month":
            self.month_view.set_data(self.anchor, items)
        else:
            self.schedule_view.set_data(self.anchor, items)

    def _range(self) -> tuple[datetime, datetime, str]:
        if self.mode == "day":
            start, end = day_bounds(self.anchor)
            return start, end, self.anchor.strftime("%A, %B %d, %Y")
        if self.mode == "week":
            start, end = week_bounds(self.anchor)
            return (start, end, f"{start.strftime('%b %d')} – {(end - timedelta(days=1)).strftime('%b %d, %Y')}")
        if self.mode == "month":
            start, end = month_bounds(self.anchor)
            grid = month_grid(self.anchor)
            first = grid[0][0]
            last = grid[-1][-1]
            return (
                datetime.combine(first, time.min),
                datetime.combine(last + timedelta(days=1), time.min),
                month_label(self.anchor),
            )
        start = self.anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            start, start + timedelta(days=60),
            f"Agenda · {start.strftime('%b %d')} – {(start + timedelta(days=60)).strftime('%b %d, %Y')}",
        )

    # ------------------------------------------------------------------- sync
    def _set_banner(self, text: str, tone: str = "warn") -> None:
        """Render the Calendar banner with a consistent tone-aware palette."""
        styles = {
            "ok": (c("green"), rgba("green", 0.06), rgba("green", 0.20)),
            "warn": (c("amber"), rgba("amber", 0.08), rgba("amber", 0.24)),
            "error": (c("red"), rgba("red", 0.08), rgba("red", 0.24)),
        }
        foreground, background, border = styles.get(tone, styles["warn"])
        self.warning.setText(text)
        self.warning.setStyleSheet(
            f"color: {foreground}; background: {background}; "
            f"border: 1px solid {border}; border-radius: 8px; "
            "padding: 6px 10px; font-size: 10px;"
        )
        self.warning.show()

    def _refresh_sync_state(self) -> None:
        try:
            self.sync_state = self.service.calendar_sync_state()
        except EFOSError:
            self.sync_state = SyncState()
        state = self.sync_state
        if state.status == "ok" and state.last_sync_at is not None:
            # Completed sync: show the freshly persisted timestamp, never a
            # placeholder "now" that hides a missing sync.
            stamp = state.last_sync_at.strftime("%b %d, %H:%M")
            self._set_banner(f"Google Calendar synchronized · {stamp}", "ok")
        else:
            self._set_banner(
                state.warning or "Google Calendar is not connected", "warn"
            )
        self.connect_button.setEnabled(bool(self.service.config.google_enabled))
        self.sync_button.setEnabled(state.connected)
        if not self.service.config.google_enabled:
            self.connect_button.setToolTip(
                "Set EFOS_GOOGLE_CLIENT_ID, EFOS_GOOGLE_CLIENT_SECRET, and "
                "EFOS_GOOGLE_CALENDAR_ID to enable read-only OAuth."
            )

    def sync_now(self, *, force_full: bool = False) -> None:
        if self._sync_worker is not None:
            return
        if not self.service.config.google_enabled and not self.sync_state.connected:
            message = "Google OAuth credentials are not configured."
            self.toast_requested.emit(message, "error")
            self._set_banner(message, "error")
            return
        self.sync_button.setEnabled(False)
        self._set_banner("Synchronizing incremental changes…", "warn")
        self._sync_worker = CalendarSyncWorker(self.service, force_full=force_full, parent=self)
        self._sync_worker.state_ready.connect(self._sync_ready)
        self._sync_worker.failed.connect(self._sync_failed)
        self._sync_worker.finished.connect(self._sync_finished)
        self._sync_worker.start()

    def _sync_finished(self) -> None:
        self.sync_button.setEnabled(self.sync_state.connected)
        if self._sync_worker is not None:
            self._sync_worker.deleteLater()
            self._sync_worker = None

    def _sync_ready(self, state: SyncState) -> None:
        # The service persists last_sync_at before returning, so this refresh
        # shows the new timestamp and turns the banner green immediately.
        self.sync_state = state
        self._refresh_sync_state()
        self.reload()

    def _sync_failed(self, message: str) -> None:
        self.toast_requested.emit(message, "error")
        self._refresh_sync_state()
        self._set_banner(f"Google Calendar sync failed — {message}", "error")

    # ------------------------------------------------------------------ OAuth
    def _reset_connect_button(self) -> None:
        self.connect_button.setText("Connect Google")
        self.connect_button.setEnabled(bool(self.service.config.google_enabled))

    def _connect_google(self) -> None:
        if not self.service.config.google_enabled:
            message = (
                "Set EFOS_GOOGLE_CLIENT_ID, EFOS_GOOGLE_CLIENT_SECRET, and "
                "EFOS_GOOGLE_CALENDAR_ID first."
            )
            self.toast_requested.emit(message, "error")
            self._set_banner(message, "error")
            return
        if self._connect_worker is not None:
            self._set_banner(
                "Google authorization is still in progress — finish the consent "
                "screen in your browser.",
                "warn",
            )
            return
        self.connect_button.setEnabled(False)
        self.connect_button.setText("Waiting for Google…")
        self._set_banner(
            "Waiting for Google authorization — a browser tab should open. "
            "Complete the consent screen there, then return to EFOS.",
            "warn",
        )
        self._connect_worker = CalendarConnectWorker(self.service, self)
        self._connect_worker.state_ready.connect(self._connect_ready)
        self._connect_worker.failed.connect(self._connect_failed)
        self._connect_worker.finished.connect(self._connect_finished)
        self._connect_worker.start()
        self._connect_watchdog.start(OAUTH_UI_TIMEOUT_MS)

    def _connect_timed_out(self) -> None:
        if self._connect_worker is None:
            return
        self._reset_connect_button()
        message = (
            "Google authorization is taking too long. Check your browser for the "
            "consent tab, then press Connect Google to try again."
        )
        self.toast_requested.emit(message, "error")
        self._set_banner(message, "error")

    def _connect_finished(self) -> None:
        # Runs after state_ready/failed in every case, so the button can never
        # remain stuck on "Waiting for Google…".
        self._connect_watchdog.stop()
        self._reset_connect_button()
        if self._connect_worker is not None:
            self._connect_worker.deleteLater()
            self._connect_worker = None

    def _connect_ready(self, state: SyncState) -> None:
        self._connect_watchdog.stop()
        self.sync_state = state
        self._refresh_sync_state()
        self.reload()

    def _connect_failed(self, message: str) -> None:
        self._connect_watchdog.stop()
        # Reset immediately instead of waiting for finished(): the user must
        # see an actionable banner and a usable button the moment OAuth fails.
        self._reset_connect_button()
        self.toast_requested.emit(message, "error")
        self._refresh_sync_state()
        self._set_banner(
            f"Google Calendar connection failed — {message}", "error"
        )

    def _open_task(self, task_id: str) -> None:
        from ..models import NodeKind, NodeRef

        self.open_node.emit(NodeRef(NodeKind.TASK, task_id))

    def shutdown(self) -> None:
        self._connect_watchdog.stop()
        for worker in (self._sync_worker, self._connect_worker):
            if worker is not None and worker.isRunning():
                worker.wait(1500)
