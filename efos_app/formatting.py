"""Presentation helpers shared by pages and inspectors."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from .models import NodeKind, ProjectState, TaskState

def fmt_datetime(value: datetime | None, *, include_year: bool = False) -> str:
    if value is None:
        return "Unscheduled"
    pattern = "%b %d, %Y · %H:%M" if include_year else "%b %d · %H:%M"
    return value.strftime(pattern).replace(" 0", " ")

def fmt_time(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "—"

def fmt_date(value: datetime | None) -> str:
    return value.strftime("%b %d").replace(" 0", " ") if value else "—"

def fmt_relative(value: datetime | None) -> str:
    if value is None:
        return "No timestamp"
    now = datetime.now()
    delta = value - now
    seconds = int(delta.total_seconds())
    if abs(seconds) < 60:
        return "just now" if seconds <= 0 else "in <1 min"
    if abs(seconds) < 3600:
        mins = abs(seconds) // 60
        return f"{mins}m ago" if seconds < 0 else f"in {mins}m"
    if abs(seconds) < 86400:
        hours = abs(seconds) // 3600
        return f"{hours}h ago" if seconds < 0 else f"in {hours}h"
    days = abs(seconds) // 86400
    if days < 14:
        return f"{days}d ago" if seconds < 0 else f"in {days}d"
    return fmt_date(value)

def fmt_duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m" if mins else f"{hours}h"

def truncate(text: str, length: int = 42) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"

def state_tone(state: TaskState | ProjectState | None) -> str:
    if state is None:
        return "unknown"
    return state.value

def kind_label(kind: NodeKind) -> str:
    return kind.label

def schedule_span(task) -> str:  # type: ignore[no-untyped-def]
    """The Task's scheduled block only; recurrence is presented separately."""
    if not task.scheduled_start or not task.scheduled_end:
        return "Unscheduled"
    if task.scheduled_start.date() == task.scheduled_end.date():
        span = f"{fmt_datetime(task.scheduled_start)} – {fmt_time(task.scheduled_end)}"
    else:
        span = f"{fmt_datetime(task.scheduled_start)} – {fmt_datetime(task.scheduled_end)}"
    return span

def recurrence_span(task) -> str:  # type: ignore[no-untyped-def]
    rule = getattr(task, "recurrence", None)
    if not rule:
        return "Not recurring"
    window = (
        f" · {rule.reset_hour:02d}:{rule.reset_minute:02d}–"
        f"{rule.end_hour:02d}:{rule.end_minute:02d}"
    )
    return f"{rule.summary()}{window}"

def availability_span(task) -> str:  # type: ignore[no-untyped-def]
    from .logic import availability_summary

    return availability_summary(getattr(task, "availability", []))

# ----------------------------------------------------------------- calendar
def day_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)

def week_bounds(value: datetime) -> tuple[datetime, datetime]:
    """Sunday-to-Saturday week, matching Google Calendar's default."""
    start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=(start.weekday() + 1) % 7)
    return start, start + timedelta(days=7)

def month_bounds(value: datetime) -> tuple[datetime, datetime]:
    first = value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        following = first.replace(year=first.year + 1, month=1)
    else:
        following = first.replace(month=first.month + 1)
    return first, following

def month_grid(value: datetime) -> list[list[date]]:
    """Six Sunday-to-Saturday rows covering the displayed month."""
    first = value.replace(day=1).date()
    offset = (first.weekday() + 1) % 7
    grid_start = first - timedelta(days=offset)
    return [
        [grid_start + timedelta(days=row * 7 + column) for column in range(7)]
        for row in range(6)
    ]

def month_label(value: datetime) -> str:
    return value.strftime("%B %Y")

def same_day(left: datetime | date, right: datetime | date) -> bool:
    return left.year == right.year and left.month == right.month and left.day == right.day

def date_from(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value

def hour_label(hour: int) -> str:
    if hour == 0:
        return "12 AM"
    if hour < 12:
        return f"{hour} AM"
    if hour == 12:
        return "12 PM"
    return f"{hour - 12} PM"

def month_range_label(value: datetime) -> str:
    return f"{calendar.month_name[value.month]} {value.year}"
