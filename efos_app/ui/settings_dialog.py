"""Local EFOS settings: three appearance choices, three Inspector widths.

The two preference groups (Appearance and Inspector width) are rendered as
vertically aligned, exclusive three-column ``GreenCheckBox`` groups: a centred
green square sits above a centred label in every column, so ``300 px`` always
lines up beneath ``Dark``, ``360 px`` beneath ``Graphite``, and ``420 px``
beneath ``Slate``.

Everything here is *local preference* state. It is persisted by the main window
through ``efos_app.config.save_ui_state`` and never touches PostgreSQL.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import (
    APPEARANCE_OPTIONS,
    normalise_appearance_value,
)
from .widgets import Divider, GreenCheckBox, SectionLabel

LANDING_VIEWS = (
    "Overview",
    "Dashboard",
    "Flow",
    "Projects",
    "Tasks",
    "Calendar",
    "Assistant",
)

# The Inspector stays interactively draggable between these bounds on every
# workspace page; Settings offers the three named stops only.
INSPECTOR_WIDTH_PRESETS = (300, 360, 420)
MIN_INSPECTOR_WIDTH = INSPECTOR_WIDTH_PRESETS[0]
MAX_INSPECTOR_WIDTH = INSPECTOR_WIDTH_PRESETS[-1]
INSPECTOR_WIDTH_OPTIONS = tuple(
    (f"{width} px", width) for width in INSPECTOR_WIDTH_PRESETS
)


def nearest_inspector_width(value) -> int:  # type: ignore[no-untyped-def]
    """Snap any live Inspector width onto the closest Settings preset."""
    try:
        width = int(round(float(value)))
    except (TypeError, ValueError):
        width = 360
    width = max(MIN_INSPECTOR_WIDTH, min(MAX_INSPECTOR_WIDTH, width))
    return min(
        INSPECTOR_WIDTH_PRESETS,
        key=lambda preset: (abs(preset - width), preset),
    )


class _OptionColumn(QWidget):
    """Centred green square above a centred label; the whole column toggles."""

    def __init__(
        self, box: GreenCheckBox, label: QLabel, parent: QWidget | None = None
    ):
        super().__init__(parent)
        self._box = box
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(box)
        layout.addWidget(label)

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self._box.setChecked(not self._box.isChecked())
        super().mouseReleaseEvent(event)


class SettingsDialog(QDialog):
    """Modal settings surface opened from the sidebar gear."""

    theme_value_changed = pyqtSignal(int)
    landing_view_changed = pyqtSignal(str)
    flow_auto_layout_changed = pyqtSignal(bool)
    inspector_width_changed = pyqtSignal(int)

    def __init__(
        self,
        *,
        theme_value: int,
        landing_view: str,
        flow_auto_layout: bool,
        inspector_width: int,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("EFOS Settings")
        self.setModal(True)
        # A fixed floor keeps all three option columns fully visible on
        # platforms whose dialog size hint is not propagated immediately.
        self.setMinimumSize(560, 620)
        self.setMaximumWidth(720)
        self._appearance_value = normalise_appearance_value(theme_value)
        self._emitted_theme = self._appearance_value
        self._width_value = nearest_inspector_width(inspector_width)
        self._saved_width = self._width_value
        self._width_committed = False
        self._landing_view = str(landing_view)
        self._flow_auto_layout = bool(flow_auto_layout)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        heading = QLabel("Settings")
        heading.setProperty("role", "h2")
        root.addWidget(heading)
        subtitle = QLabel(
            "Appearance and startup behaviour are stored locally. The PostgreSQL "
            "schema and dependency model are untouched."
        )
        subtitle.setProperty("role", "muted")
        subtitle.setWordWrap(True)
        subtitle.setMinimumWidth(0)
        root.addWidget(subtitle)

        root.addWidget(self._appearance_card())
        root.addWidget(self._behaviour_card())

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.storage_note = QLabel("Saved to ui_state.json · no database changes")
        self.storage_note.setProperty("role", "faint")
        footer.addWidget(self.storage_note, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.accept)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

        self._theme_timer = QTimer(self)
        self._theme_timer.setSingleShot(True)
        self._theme_timer.setInterval(260)
        self._theme_timer.timeout.connect(self._flush_theme)
        self._sync_controls()

    # --------------------------------------------------------------- layout
    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("card", True)
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        return card, layout

    def _row(self, label_text: str, widget: QWidget) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        label = QLabel(label_text)
        label.setStyleSheet("font-size: 12px;")
        label.setMinimumWidth(150)
        layout.addWidget(label, 0)
        layout.addWidget(widget, 1)
        return holder

    def _option_column(self, label_text: str) -> tuple[QWidget, GreenCheckBox]:
        """One centred green square above one centred, crisp option label."""
        box = GreenCheckBox("", centered=True)
        box.setFixedHeight(22)
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        box.setAccessibleName(label_text)
        box.setToolTip(label_text)
        label = QLabel(label_text)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        label.setStyleSheet(
            "font-size: 12px; font-weight: 600; letter-spacing: 0.2px;"
        )
        return _OptionColumn(box, label), box

    def _choice_group(
        self,
        options: tuple[tuple[str, int], ...],
        current: int,
        on_selected,
    ) -> tuple[QWidget, dict[int, GreenCheckBox]]:
        """Build an exclusive three-column group of centred checkboxes.

        The columns share one grid with equal stretch, so two groups stacked in
        the same card line up exactly: column 1 under column 1, and so on.
        """
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(0)
        boxes: dict[int, GreenCheckBox] = {}
        for column, (label_text, value) in enumerate(options):
            widget, box = self._option_column(label_text)
            box.setChecked(value == current)
            box.toggled.connect(
                lambda checked, v=value: on_selected(v, bool(checked))
            )
            grid.addWidget(widget, 0, column)
            grid.setColumnStretch(column, 1)
            boxes[value] = box
        return holder, boxes

    def _appearance_card(self) -> QFrame:
        card, layout = self._card()
        layout.addWidget(SectionLabel("Appearance"))
        copy = QLabel(
            "Three curated palettes from the dark-to-slate spectrum: Dark is "
            "Deep Dark, Graphite is the midpoint, and Slate is the lifted "
            "slate. Text contrast is preserved at every choice."
        )
        copy.setProperty("role", "muted")
        copy.setWordWrap(True)
        copy.setMinimumWidth(0)
        layout.addWidget(copy)

        self.appearance_choices, self.appearance_boxes = self._choice_group(
            APPEARANCE_OPTIONS,
            self._appearance_value,
            self._on_appearance_selected,
        )
        layout.addWidget(self.appearance_choices)

        layout.addWidget(Divider())
        layout.addWidget(SectionLabel("Inspector width"))
        self.width_choices, self.width_boxes = self._choice_group(
            INSPECTOR_WIDTH_OPTIONS,
            self._width_value,
            self._on_width_selected,
        )
        layout.addWidget(self.width_choices)
        width_hint = QLabel(
            "Drag the Inspector edge between 300 and 420 px at any time; "
            "reopening Settings selects the nearest preset and Save applies the "
            "chosen width."
        )
        width_hint.setProperty("role", "faint")
        width_hint.setWordWrap(True)
        width_hint.setMinimumWidth(0)
        layout.addWidget(width_hint)
        return card

    def _behaviour_card(self) -> QFrame:
        card, layout = self._card()
        layout.addWidget(SectionLabel("Startup & layout"))

        self.landing_combo = QComboBox()
        self.landing_combo.addItems(LANDING_VIEWS)
        self.landing_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        if self.landing_combo.findText(self._landing_view) < 0:
            self.landing_combo.setCurrentIndex(0)
        else:
            self.landing_combo.setCurrentText(self._landing_view)
        self.landing_combo.currentTextChanged.connect(self._on_landing_view)
        layout.addWidget(self._row("Default landing view", self.landing_combo))

        self.flow_check = GreenCheckBox("Auto-layout the Flow canvas on startup")
        self.flow_check.setToolTip(
            "When enabled, the first Flow visit after launch renders the clean "
            "auto-layout view as if Auto-layout had been clicked."
        )
        self.flow_check.setChecked(bool(self._flow_auto_layout))
        self.flow_check.toggled.connect(self._on_flow_auto_layout)
        layout.addWidget(self.flow_check)
        return card

    # ---------------------------------------------------------------- state
    def set_values(
        self,
        *,
        theme_value: int,
        landing_view: str,
        flow_auto_layout: bool,
        inspector_width: int,
    ) -> None:
        """Resynchronise the controls after an external change."""
        self._appearance_value = normalise_appearance_value(theme_value)
        self._emitted_theme = self._appearance_value
        self._width_value = nearest_inspector_width(inspector_width)
        self._saved_width = self._width_value
        self._width_committed = False
        self._sync_controls()
        self.landing_combo.blockSignals(True)
        if self.landing_combo.findText(landing_view) >= 0:
            self.landing_combo.setCurrentText(landing_view)
        self.landing_combo.blockSignals(False)
        self._flow_auto_layout = bool(flow_auto_layout)
        self.flow_check.blockSignals(True)
        self.flow_check.setChecked(self._flow_auto_layout)
        self.flow_check.blockSignals(False)

    def sync_theme_value(self, value: int) -> None:
        self.set_values(
            theme_value=value,
            landing_view=self.landing_combo.currentText(),
            flow_auto_layout=self.flow_check.isChecked(),
            inspector_width=self._width_value,
        )

    def _sync_controls(self) -> None:
        for value, box in self.appearance_boxes.items():
            box.blockSignals(True)
            box.setChecked(value == self._appearance_value)
            box.blockSignals(False)
        for value, box in self.width_boxes.items():
            box.blockSignals(True)
            box.setChecked(value == self._width_value)
            box.blockSignals(False)

    # ----------------------------------------------------------------- state
    @property
    def appearance_value(self) -> int:
        return self._appearance_value

    @property
    def width_value(self) -> int:
        return self._width_value

    # --------------------------------------------------------------- events
    @staticmethod
    def _select_exclusive(
        boxes: dict[int, GreenCheckBox], value: int, checked: bool
    ) -> bool:
        """Keep exactly one box checked inside a group.

        Clicking an already-checked box would otherwise uncheck it and leave the
        group empty; in that case the selection is restored. Returns ``True``
        only when ``value`` is the new group selection.
        """
        if not checked:
            box = boxes.get(value)
            if box is not None:
                box.blockSignals(True)
                box.setChecked(True)
                box.blockSignals(False)
            return False
        for other, box in boxes.items():
            if other != value and box.isChecked():
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
        return True

    def _on_appearance_selected(self, value: int, checked: bool) -> None:
        if not self._select_exclusive(self.appearance_boxes, value, checked):
            return
        self._appearance_value = normalise_appearance_value(value)
        self._theme_timer.start()

    def _on_width_selected(self, value: int, checked: bool) -> None:
        if not self._select_exclusive(self.width_boxes, value, checked):
            return
        self._width_value = nearest_inspector_width(value)

    def _flush_theme(self) -> None:
        self._theme_timer.stop()
        value = self._appearance_value
        if value == self._emitted_theme:
            return
        self._emitted_theme = value
        self.theme_value_changed.emit(value)

    def _commit_width(self) -> None:
        """Apply the chosen preset width once, when the dialog is saved."""
        if self._width_committed:
            return
        self._width_committed = True
        self._saved_width = nearest_inspector_width(self._width_value)
        self.inspector_width_changed.emit(self._saved_width)

    def _on_landing_view(self, value: str) -> None:
        if value:
            self.landing_view_changed.emit(value)

    def _on_flow_auto_layout(self, checked: bool) -> None:
        self.flow_auto_layout_changed.emit(bool(checked))

    def _close(self) -> None:
        self.accept()

    def done(self, result: int) -> None:  # noqa: N802 - Qt override
        if result == QDialog.DialogCode.Accepted:
            self._flush_theme()
            self._commit_width()
        else:
            self._theme_timer.stop()
        super().done(result)
