"""Reusable UI primitives with live dark/light palette support."""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .icons import brand_pixmap, dotted_line_pixmap, icon
from .theme import c, display_font, rgba, ui_font

# ---------------------------------------------------------------------------
# Unified two-pane page shell geometry
# ---------------------------------------------------------------------------
# Dashboard (Manage + Flow), Registry, and Assistant all share one shell: an
# identical 24 px outer page pad, one compact toolbar row of the same height,
# and a 20 px QSplitter gutter between the main pane and the Inspector/Query
# pane. Keeping the numbers here makes the four surfaces provably identical.
PAGE_MARGIN = 24
PANE_GUTTER = 20
TOOLBAR_HEIGHT = 40


def page_margins(layout) -> None:  # type: ignore[no-untyped-def]
    """Apply the shared 24 px page padding to an outer page layout."""
    layout.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)


def toolbar_row() -> tuple[QWidget, QHBoxLayout]:
    """Create the shared compact toolbar row that precedes every pane splitter."""
    holder = QWidget()
    holder.setFixedHeight(TOOLBAR_HEIGHT)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    return holder, layout


def configure_pane_splitter(splitter) -> None:  # type: ignore[no-untyped-def]
    """Apply the shared 20 px gutter and non-collapsible panes."""
    splitter.setChildrenCollapsible(False)
    splitter.setHandleWidth(PANE_GUTTER)


class InspectorSplitter(QSplitter):
    """Two-pane splitter that keeps the Inspector/Query pane at a fixed width.

    The persisted 300–420 px width is stored on the splitter, so a page that is
    created hidden still opens with the right pane size the first time it is
    shown, and window resizes grow the main pane instead of the Inspector. A
    user drag updates the width through the main window's ``splitterMoved``
    handler, which calls :meth:`set_pane_width` again.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        configure_pane_splitter(self)
        self._pane_width: int | None = None

    def set_pane_width(self, width: int) -> None:
        self._pane_width = max(1, int(width))
        self._apply_pane_width()

    def pane_width(self) -> int | None:
        return self._pane_width

    def _apply_pane_width(self) -> None:
        if self._pane_width is None:
            return
        # QSplitter sizes exclude the handle, so subtract the 20 px gutter to
        # land the Inspector/Query pane on the exact requested pixel width.
        available = self.width() - self.handleWidth()
        if available <= self._pane_width + 1:
            return
        self.setSizes([max(1, available - self._pane_width), self._pane_width])

    def resizeEvent(self, event):  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._apply_pane_width()

    def showEvent(self, event):  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self._apply_pane_width()

class Card(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, raised: bool = False, flat: bool = False):
        super().__init__(parent)
        self.setProperty("card", "raised" if raised else ("flat" if flat else "true"))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

class ClickableRow(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(
            "ClickableRow { background: transparent; border: 1px solid transparent; "
            f"border-radius: 9px; }} ClickableRow:hover {{ background: {c('hover')}; "
            f"border-color: {c('border')}; }}"
        )

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

class Badge(QLabel):
    def __init__(self, text: str = "", tone: str = "unknown", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setProperty("badge", True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_tone(tone)

    def set_tone(self, tone: str, text: str | None = None) -> None:
        if text is not None:
            self.setText(text)
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)

class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("divider", True)
        self.setFixedHeight(1)

class IconButton(QPushButton):
    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        parent: QWidget | None = None,
        *,
        color: str | None = None,
        size: int = 18,
    ):
        super().__init__(parent)
        self.setProperty("variant", "icon")
        self.setIcon(icon(icon_name, color or c("muted"), size))
        self.setIconSize(QSize(size, size))
        self.setFixedSize(32, 32)
        if tooltip:
            self.setToolTip(tooltip)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

class NavButton(QPushButton):
    def __init__(
        self,
        text: str,
        icon_name: str,
        parent: QWidget | None = None,
        *,
        checkable: bool = True,
    ):
        super().__init__(text, parent)
        self._icon_name = icon_name
        self.setProperty("nav", True)
        self.setCheckable(checkable)
        self.setIcon(icon(icon_name, c("muted"), 18))
        self.setIconSize(QSize(18, 18))
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(39)
        if checkable:
            self.toggled.connect(self._sync_icon)

    def _sync_icon(self, checked: bool) -> None:
        self.setIcon(icon(self._icon_name, c("accent") if checked else c("muted"), 18))

    def sync_theme(self) -> None:
        self._sync_icon(self.isChecked())

class SegmentedControl(QWidget):
    changed = pyqtSignal(int)

    def __init__(self, labels: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.setStyleSheet(
            f"SegmentedControl {{ background: {c('bg_raised')}; border: 1px solid {c('border')}; "
            "border-radius: 10px; }"
        )
        self.buttons: list[QPushButton] = []
        for index, label in enumerate(labels):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("chip", True)
            button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            button.clicked.connect(lambda _=False, i=index: self.select(i))
            layout.addWidget(button)
            self.buttons.append(button)
        if self.buttons:
            self.buttons[0].setChecked(True)

    def select(self, index: int, *, emit: bool = True) -> None:
        for position, button in enumerate(self.buttons):
            button.setChecked(position == index)
        if emit:
            self.changed.emit(index)

    def current(self) -> int:
        for index, button in enumerate(self.buttons):
            if button.isChecked():
                return index
        return 0

class SectionHeader(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        action: QWidget | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setMaximumHeight(54 if subtitle else 30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        title_label = QLabel(title)
        title_label.setProperty("role", "h2")
        text_col.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setProperty("role", "muted")
            subtitle_label.setWordWrap(True)
            text_col.addWidget(subtitle_label)
        layout.addLayout(text_col)
        layout.addStretch(1)
        if action is not None:
            layout.addWidget(action)

class MetricCard(Card):
    def __init__(
        self,
        label: str,
        value: str = "0",
        hint: str = "",
        icon_name: str = "grid",
        tone_key: str = "accent",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setMinimumHeight(124)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        tone = c(tone_key)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 15)
        outer.setSpacing(4)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label.upper())
        self.label.setProperty("role", "eyebrow")
        top.addWidget(self.label)
        top.addStretch(1)
        icon_label = QLabel()
        icon_label.setPixmap(icon(icon_name, tone, 18).pixmap(18, 18))
        icon_label.setStyleSheet(
            f"background: {rgba(tone, 0.08)}; border: 1px solid {rgba(tone, 0.18)}; "
            "border-radius: 8px; padding: 4px;"
        )
        top.addWidget(icon_label)
        outer.addLayout(top)
        outer.addStretch(1)
        self.value = QLabel(value)
        self.value.setProperty("role", "metric")
        self.value.setStyleSheet(f"color: {c('text')};")
        outer.addWidget(self.value)
        self.hint = QLabel(hint)
        self.hint.setProperty("role", "faint")
        outer.addWidget(self.hint)

    def set_value(self, value: str, hint: str | None = None) -> None:
        self.value.setText(value)
        if hint is not None:
            self.hint.setText(hint)

class EmptyState(QWidget):
    def __init__(
        self,
        title: str,
        body: str,
        icon_name: str = "flow",
        action: QWidget | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 42, 32, 42)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark = QLabel()
        mark.setPixmap(icon(icon_name, c("accent"), 30).pixmap(30, 30))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(58, 58)
        mark.setStyleSheet(
            f"background: {c('surface')}; border: 1px solid {c('border')}; border-radius: 16px;"
        )
        layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(10)
        heading = QLabel(title)
        heading.setProperty("role", "h2")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        copy = QLabel(body)
        copy.setProperty("role", "muted")
        copy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        copy.setWordWrap(True)
        copy.setMaximumWidth(470)
        layout.addWidget(copy, 0, Qt.AlignmentFlag.AlignHCenter)
        if action is not None:
            layout.addSpacing(6)
            layout.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)

class InlineBanner(QFrame):
    dismissed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setVisible(False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 10, 9)
        layout.setSpacing(9)
        self.icon_label = QLabel()
        layout.addWidget(self.icon_label)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {c('text')}; font-size: 12px;")
        layout.addWidget(self.message, 1)
        self.close_button = IconButton("close", "Dismiss", size=14)
        self.close_button.setFixedSize(24, 24)
        self.close_button.clicked.connect(self.dismiss)
        layout.addWidget(self.close_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

    def show_message(self, message: str, level: str = "error", timeout_ms: int = 6500) -> None:
        tone_key = {"error": "red", "info": "accent", "success": "green", "warn": "amber"}.get(
            level, "accent"
        )
        self.icon_label.setPixmap(
            icon("alert" if level == "error" else "sensor", c(tone_key), 16).pixmap(16, 16)
        )
        self.message.setText(message)
        self.setStyleSheet(
            f"InlineBanner {{ background: {rgba(tone_key, 0.07)}; "
            f"border: 1px solid {rgba(tone_key, 0.22)}; border-radius: 9px; }}"
        )
        self.show()
        self.raise_()
        if timeout_ms > 0:
            self._timer.start(timeout_ms)

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()
        self.dismissed.emit()

class Toast(QFrame):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setVisible(False)
        self.setStyleSheet(
            f"Toast {{ background: {c('surface')}; border: 1px solid {c('border_hi')}; "
            "border-radius: 10px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 14, 9)
        layout.setSpacing(8)
        self.icon_label = QLabel()
        layout.addWidget(self.icon_label)
        self.label = QLabel()
        self.label.setStyleSheet(f"color: {c('text')}; font-size: 12px;")
        layout.addWidget(self.label)
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.effect.setOpacity(0.0)
        self.animation = QPropertyAnimation(self.effect, b"opacity", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide_animated)

    def notify(self, message: str, level: str = "success") -> None:
        tone_key = {"success": "green", "error": "red", "info": "accent", "warn": "amber"}.get(
            level, "accent"
        )
        self.icon_label.setPixmap(
            icon("check" if level == "success" else "alert", c(tone_key), 16).pixmap(16, 16)
        )
        self.label.setText(message)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.animation.stop()
        self.animation.setStartValue(self.effect.opacity())
        self.animation.setEndValue(1.0)
        self.animation.start()
        self._timer.start(3200)

    def reposition(self) -> None:
        if self.parentWidget() is None:
            return
        parent = self.parentWidget()
        self.move(parent.width() - self.width() - 26, parent.height() - self.height() - 54)

    def hide_animated(self) -> None:
        self.animation.stop()
        self.animation.setStartValue(self.effect.opacity())
        self.animation.setEndValue(0.0)
        self.animation.finished.connect(self._hide_now)
        self.animation.start()

    def _hide_now(self) -> None:
        try:
            self.animation.finished.disconnect(self._hide_now)
        except TypeError:
            pass
        self.hide()

class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = text
        super().setText(text)
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event):  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = QFontMetrics(self.font())
        clipped = metrics.elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(0, self.width() - 2)
        )
        super().setText(clipped)

class BrandHeader(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(3)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        mark = QLabel()
        mark.setPixmap(brand_pixmap(34))
        mark.setFixedSize(34, 34)
        top.addWidget(mark)
        word = QLabel("EFOS")
        word.setFont(display_font(17, QFont.Weight.Bold))
        word.setStyleSheet(f"color: {c('text')}; letter-spacing: 2px;")
        top.addWidget(word)
        top.addStretch(1)
        layout.addLayout(top)
        sub = QLabel("EXECUTIVE CONSOLE")
        sub.setStyleSheet(
            f"color: {c('faint')}; font-size: 8px; font-weight: 700; letter-spacing: 1.8px;"
        )
        layout.addWidget(sub)
        dotted = QLabel()
        dotted.setPixmap(dotted_line_pixmap(160, 10))
        dotted.setFixedSize(160, 10)
        layout.addWidget(dotted)

class StatusDot(QWidget):
    def __init__(self, color: str | None = None, size: int = 8, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color or c("green"))
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        painter.end()

class TogglePill(QPushButton):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFixedHeight(26)
        self._value: bool | None = None
        self.set_value(None)

    def set_value(self, value: bool | None) -> None:
        self._value = value
        if value is True:
            self.setText("TRUE")
            color = c("green")
        elif value is False:
            self.setText("FALSE")
            color = c("red")
        else:
            self.setText("UNKNOWN")
            color = c("muted")
        self.setStyleSheet(
            f"QPushButton {{ color: {color}; background: {rgba(color, 0.07)}; "
            f"border: 1px solid {rgba(color, 0.22)}; "
            "border-radius: 7px; padding: 2px 9px; font-size: 10px; font-weight: 700; "
            "letter-spacing: 0.5px; }"
            f"QPushButton:hover {{ background: {rgba(color, 0.13)}; }}"
        )

    def value(self) -> bool | None:
        return self._value

class KeyValueRow(QWidget):
    def __init__(
        self, key: str, value: str, parent: QWidget | None = None, *, mono: bool = False
    ):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)
        key_label = QLabel(key)
        key_label.setStyleSheet(f"color: {c('muted')}; font-size: 11px;")
        key_label.setMinimumWidth(88)
        value_label = QLabel(value)
        value_label.setWordWrap(True)
        value_label.setStyleSheet(
            f"color: {c('text')}; font-size: 11px; "
            f"font-family: {'Cascadia Mono, Consolas, monospace' if mono else 'Segoe UI'};"
        )
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(key_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(value_label, 1)

class SectionLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text.upper(), parent)
        self.setProperty("role", "sectionTitle")

class SuggestedFocusCard(QFrame):
    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SuggestedFocus")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(126)
        self.setToolTip("Open the shared metadata inspector for this item")
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(13, 12, 13, 12)
        self.layout_box.setSpacing(6)
        self.eyebrow = QLabel("SUGGESTED FOCUS")
        self.eyebrow.setProperty("role", "eyebrow")
        self.layout_box.addWidget(self.eyebrow)
        self.title = QLabel("")
        self.title.setStyleSheet(
            f"color: {c('text')}; font-size: 12px; font-weight: 700;"
        )
        self.title.setWordWrap(True)
        self.layout_box.addWidget(self.title)
        self.state_badge = Badge("", "unknown")
        self.layout_box.addWidget(self.state_badge, 0, Qt.AlignmentFlag.AlignLeft)
        self.rationale = QLabel("")
        self.rationale.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
        self.rationale.setWordWrap(True)
        self.layout_box.addWidget(self.rationale)
        self.layout_box.addStretch(1)
        self.empty = QLabel("No ready Tasks or active Projects to recommend")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
        self.layout_box.addWidget(self.empty)
        self.empty.hide()
        self.recommendation = None

    def set_recommendation(self, recommendation) -> None:  # type: ignore[no-untyped-def]
        self.recommendation = recommendation
        if recommendation is None:
            self.title.hide()
            self.state_badge.hide()
            self.rationale.hide()
            self.empty.show()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        self.empty.hide()
        self.title.show()
        self.state_badge.show()
        self.rationale.show()
        self.title.setText(recommendation.name)
        self.state_badge.set_tone(recommendation.state, recommendation.state.upper())
        self.rationale.setText(recommendation.rationale)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.recommendation is not None
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

class GreenCheckBox(QCheckBox):
    """Checkbox that paints a crisp green checkmark on Windows and macOS.

    ``centered=True`` paints the indicator horizontally centered inside the
    widget instead of at its left edge. The Settings appearance/width presets
    use it for their label-less option squares, which gives the whole column
    width a comfortable click target.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        centered: bool = False,
    ):
        super().__init__(text, parent)
        self._centered = bool(centered)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(
            f"QCheckBox {{ color: {c('text_soft')}; font-size: 11px; spacing: 8px; }}"
            "QCheckBox::indicator { width: 16px; height: 16px; background: transparent; border: none; }"
        )

    def hitButton(self, pos) -> bool:  # type: ignore[override]
        # The indicator is compact, but the Settings preset columns rely on the
        # whole centered square (and its surrounding width) being clickable.
        return self.rect().contains(pos)

    def paintEvent(self, event):  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = (self.height() - 16) / 2.0
        x = (self.width() - 16) / 2.0 if self._centered else 0.0
        box = QRectF(x + 0.5, y + 0.5, 15.0, 15.0)
        if self.isChecked():
            painter.setPen(QPen(QColor(c("green")), 1.4))
            painter.setBrush(QColor(rgba("green", 0.16)))
            painter.drawRoundedRect(box, 4.0, 4.0)
            pen = QPen(QColor(c("green")), 2.1)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(
                int(x + 3.5), int(y + 8), int(x + 6.5), int(y + 11)
            )
            painter.drawLine(
                int(x + 6.5), int(y + 11), int(x + 11.5), int(y + 5)
            )
        else:
            painter.setPen(QPen(QColor(c("border_hi")), 1.2))
            painter.setBrush(QColor(c("input")))
            painter.drawRoundedRect(box, 4.0, 4.0)
        painter.end()
