"""Visual system for EFOS: dark/light palettes, QSS, and native window chrome.

Dark is the absolute first-run default. The active theme is persisted in the
local UI-state JSON file by the main window and restored across restarts.
"""
from __future__ import annotations

import ctypes
import sys

from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

THEME_DARK = "dark"
THEME_LIGHT = "light"
DEFAULT_THEME = THEME_DARK

PALETTES: dict[str, dict[str, str]] = {
    THEME_DARK: {
        "bg": "#090B10",
        "bg_raised": "#0D1017",
        "surface": "#11151D",
        "surface_2": "#161C26",
        "surface_3": "#1C2431",
        "hover": "#202938",
        "border": "#232B39",
        "border_hi": "#344055",
        "outline": "#2C3647",
        "text": "#E9EDF5",
        "text_soft": "#C4CBDA",
        "muted": "#8B96AA",
        "faint": "#5F6B7F",
        "accent": "#4A5568",
        "accent_2": "#2D3748",
        "teal": "#46D7C4",
        "amber": "#F1B45C",
        "green": "#58D6A0",
        "red": "#F0747D",
        "pink": "#E687BA",
        "blue": "#6EA8FE",
        "task": "#6EA8FE",
        "project": "#A78BFA",
        "condition": "#F1B45C",
        "row_alt": "#121822",
        "selection": "#1E2940",
        "canvas": "#0A0D13",
        "canvas_dot": "rgba(255, 255, 255, 0.035)",
        "canvas_dot_accent": "rgba(142, 168, 255, 0.05)",
        "sidebar": "#080A0F",
        "titlebar": "#07090D",
        "statusbar": "#080A0F",
        "input": "#0C1017",
        "input_focus": "#0E131B",
        "shadow": "rgba(0, 0, 0, 0.55)",
    },
    THEME_LIGHT: {
        "bg": "#F2F5FA",
        "bg_raised": "#E9EEF6",
        "surface": "#FFFFFF",
        "surface_2": "#F6F8FC",
        "surface_3": "#EDF2F9",
        "hover": "#E5ECF6",
        "border": "#D5DDEA",
        "border_hi": "#B6C3D6",
        "outline": "#AFBCCE",
        "text": "#17202E",
        "text_soft": "#33415A",
        "muted": "#5C6B82",
        "faint": "#8493A8",
        "accent": "#4F6BED",
        "accent_2": "#7157D6",
        "teal": "#0F8C80",
        "amber": "#A96A0A",
        "green": "#12784B",
        "red": "#C1374A",
        "pink": "#AB4680",
        "blue": "#2D69C9",
        "task": "#2D69C9",
        "project": "#6D4BD0",
        "condition": "#A96A0A",
        "row_alt": "#F7F9FD",
        "selection": "#DCE6FA",
        "canvas": "#F8FAFE",
        "canvas_dot": "rgba(23, 32, 46, 0.06)",
        "canvas_dot_accent": "rgba(79, 107, 237, 0.07)",
        "sidebar": "#EBF0F8",
        "titlebar": "#E6ECF5",
        "statusbar": "#EBF0F8",
        "input": "#FFFFFF",
        "input_focus": "#FFFFFF",
        "shadow": "rgba(38, 54, 80, 0.14)",
    },
}

_current = DEFAULT_THEME

FONT_FAMILY = "Segoe UI Variable Text"
FONT_DISPLAY = "Segoe UI Variable Display"

STATE_COLORS = {
    "available": "green",
    "unavailable": "faint",
    "completed": "blue",
    "deferred": "amber",
    "active": "accent",
    "stalled": "amber",
    "shelved": "faint",
    "resolved": "green",
    "true": "green",
    "false": "red",
    "unknown": "muted",
}


def current_theme() -> str:
    return _current


def is_dark() -> bool:
    return _current == THEME_DARK


def set_theme(name: str) -> str:
    global _current
    _current = THEME_LIGHT if str(name).lower() == THEME_LIGHT else THEME_DARK
    return _current


def c(key: str) -> str:
    palette = PALETTES[_current]
    return palette.get(key, PALETTES[THEME_DARK].get(key, "#FFFFFF"))


def state_color(value: str) -> str:
    return c(STATE_COLORS.get(str(value), "muted"))


def rgba(key_or_color: str, alpha: float) -> str:
    """Return a Qt rgba() value from a palette key or a hex color."""
    color = c(key_or_color) if key_or_color in PALETTES[_current] else key_or_color
    value = QColor(color)
    return (
        f"rgba({value.red()}, {value.green()}, {value.blue()}, "
        f"{max(0.0, min(1.0, alpha)):.2f})"
    )


def ui_font(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(FONT_FAMILY, size)
    font.setWeight(weight)
    return font


def display_font(size: int = 10, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    font = QFont(FONT_DISPLAY, size)
    font.setWeight(weight)
    return font


def apply_theme(app: QApplication, name: str | None = None) -> str:
    if name:
        set_theme(name)
    app.setStyle("Fusion")
    app.setFont(QFont(FONT_FAMILY, 10))
    app.setStyleSheet(build_stylesheet())
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c("bg")))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c("text")))
    palette.setColor(QPalette.ColorRole.Base, QColor(c("surface")))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c("row_alt")))
    palette.setColor(QPalette.ColorRole.Text, QColor(c("text")))
    palette.setColor(QPalette.ColorRole.Button, QColor(c("surface_2")))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c("text")))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c("accent")))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c("bg")))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c("faint")))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c("surface_3")))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c("text")))
    app.setPalette(palette)
    return _current


def enable_native_title_bar(widget: QWidget) -> None:
    """Ask Windows 10/11 to render the native frame to match the active theme."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        dark = 1 if is_dark() else 0
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(ctypes.c_int(dark)), ctypes.sizeof(ctypes.c_int)
        )
        color = 0x000B0909 if is_dark() else 0x00F5ECE6
        for attribute in (35, 34):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(ctypes.c_int(color)),
                ctypes.sizeof(ctypes.c_int),
            )
        corner = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner)
        )
    except Exception:
        return


# Backwards-compatible alias used by dialogs.
enable_dark_title_bar = enable_native_title_bar


def build_stylesheet() -> str:
    t = {}
    for key in PALETTES[_current]:
        t[key] = c(key)
    return f"""
* {{ outline: 0; }}
QWidget {{
    color: {t['text']};
    background: transparent;
    font-family: "{FONT_FAMILY}", "Segoe UI", sans-serif;
    font-size: 13px;
    selection-background-color: {t['accent']};
    selection-color: {t['bg']};
}}
QMainWindow, QWidget#Root, QDialog {{ background-color: {t['bg']}; }}
QWidget#Root {{ border: 1px solid {t['outline']}; }}
QWidget#ContentShell {{ background: {t['bg']}; }}
QFrame#Sidebar {{
    background: {t['sidebar']};
    border-right: 1px solid {t['border']};
}}
QFrame#Header {{
    background: {rgba('bg', 0.94)};
    border-bottom: 1px solid {t['border']};
}}
QFrame#TitleBar {{
    background: {t['titlebar']};
    border-bottom: 1px solid {t['border']};
}}
QFrame#StatusBar {{
    background: {t['statusbar']};
    border-top: 1px solid {t['border']};
}}
QFrame[card="true"] {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 14px;
}}
QFrame[card="raised"] {{
    background: {t['surface_2']};
    border: 1px solid {t['border']};
    border-radius: 14px;
}}
QFrame[card="flat"] {{
    background: {t['bg_raised']};
    border: 1px solid {t['border']};
    border-radius: 12px;
}}
QFrame[divider="true"] {{ background: {t['border']}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#Inspector {{
    background: {t['bg_raised']};
    border-left: 1px solid {t['border']};
}}
QFrame#GraphCanvasShell {{
    background: {t['canvas']};
    border: 1px solid {t['border']};
    border-radius: 14px;
}}
QFrame#SuggestedFocus {{
    background: {rgba('accent', 0.08)};
    border: 1px solid {rgba('accent', 0.30)};
    border-radius: 12px;
}}
QFrame#SuggestedFocus:hover {{
    background: {rgba('accent', 0.14)};
    border-color: {t['accent']};
}}
QFrame#Launchpad {{
    background: {t['surface_2']};
    border-left: 1px solid {t['border']};
}}
QFrame#ChatBubbleUser {{
    background: {rgba('accent', 0.12)};
    border: 1px solid {rgba('accent', 0.26)};
    border-radius: 12px;
}}
QFrame#ChatBubbleAssistant {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 12px;
}}
QFrame#DiagnosticPanel {{
    background: {t['bg_raised']};
    border: 1px solid {t['border']};
    border-radius: 10px;
}}
QFrame#CalendarCell {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 8px;
}}
QFrame#CalendarCell[today="true"] {{
    border-color: {t['accent']};
    background: {rgba('accent', 0.06)};
}}
QPushButton {{
    background: {t['surface_2']};
    color: {t['text_soft']};
    border: 1px solid {t['border']};
    border-radius: 8px;
    padding: 7px 13px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {t['hover']}; border-color: {t['border_hi']}; color: {t['text']}; }}
QPushButton:pressed {{ background: {t['surface_3']}; }}
QPushButton:disabled {{ color: {t['faint']}; border-color: {t['border']}; background: {t['surface_2']}; }}
QPushButton[variant="primary"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['accent']}, stop:1 {t['accent_2']});
    color: #FFFFFF;
    border: 1px solid {t['accent']};
    font-weight: 700;
}}
QPushButton[variant="primary"]:hover {{ background: {t['accent']}; }}
QPushButton[variant="primary"]:pressed {{ background: {t['accent_2']}; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; color: {t['muted']}; }}
QPushButton[variant="ghost"]:hover {{ background: {t['surface_2']}; color: {t['text']}; border-color: {t['border']}; }}
QPushButton[variant="danger"] {{ color: {t['red']}; background: {rgba('red', 0.07)}; border: 1px solid {rgba('red', 0.22)}; }}
QPushButton[variant="danger"]:hover {{ background: {rgba('red', 0.14)}; }}
QPushButton[variant="subtle"] {{ background: {rgba('text', 0.03)}; border: 1px solid {t['border']}; }}
QPushButton[variant="icon"] {{ padding: 6px; min-width: 18px; min-height: 18px; border-radius: 8px; background: transparent; border: 1px solid transparent; }}
QPushButton[variant="icon"]:hover {{ background: {t['surface_2']}; border-color: {t['border']}; }}
QPushButton[nav="true"] {{
    text-align: left;
    padding: 10px 12px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 9px;
    color: {t['muted']};
    font-size: 12px;
    font-weight: 550;
}}
QPushButton[nav="true"]:hover {{ background: {t['surface_2']}; color: {t['text_soft']}; }}
QPushButton[nav="true"]:checked {{ background: {t['selection']}; color: {t['text']}; border: 1px solid {t['border_hi']}; }}
QPushButton[chip="true"] {{ border-radius: 13px; padding: 5px 11px; font-size: 11px; background: {t['surface_2']}; border: 1px solid {t['border']}; color: {t['muted']}; }}
QPushButton[chip="true"]:checked {{ color: {t['text']}; background: {t['selection']}; border-color: {t['border_hi']}; }}
QPushButton[stateAction="true"] {{ text-align: left; padding: 8px 11px; font-size: 12px; }}
QPushButton[stateAction="true"][danger="true"] {{ color: {t['red']}; }}
QPushButton[windowControl="true"] {{ background: transparent; border: 1px solid transparent; border-radius: 6px; color: {t['muted']}; padding: 0; font-family: "Segoe UI Symbol", "Segoe UI"; font-size: 12px; }}
QPushButton[windowControl="true"]:hover {{ background: {t['surface_3']}; color: {t['text']}; border-color: {t['border']}; }}
QPushButton[windowControl="true"][closeButton="true"]:hover {{ background: #B3323D; color: #FFFFFF; border-color: #C8424D; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QDateTimeEdit, QSpinBox, QTimeEdit {{
    background: {t['input']};
    border: 1px solid {t['border']};
    border-radius: 8px;
    padding: 7px 10px;
    color: {t['text']};
    selection-background-color: {t['accent']};
    selection-color: {t['bg']};
}}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QComboBox:hover, QDateTimeEdit:hover {{ border-color: {t['border_hi']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QDateTimeEdit:focus {{ border-color: {t['accent']}; background: {t['input_focus']}; }}
QLineEdit:disabled, QComboBox:disabled, QDateTimeEdit:disabled {{ color: {t['faint']}; background: {t['surface_2']}; }}
QLineEdit[invalid="true"] {{ border-color: {t['red']}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border_hi']}; border-radius: 8px; color: {t['text']}; selection-background-color: {t['selection']}; padding: 4px; outline: 0; }}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 3px 8px; }}
QDateTimeEdit::drop-down, QTimeEdit::drop-down {{ border: none; width: 22px; }}
QCheckBox {{ color: {t['text_soft']}; spacing: 8px; font-size: 12px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {t['border_hi']}; background: {t['input']}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}

QLabel {{ background: transparent; }}
QLabel[role="h1"] {{ font-family: "{FONT_DISPLAY}", "Segoe UI"; font-size: 27px; font-weight: 650; color: {t['text']}; }}
QLabel[role="h2"] {{ font-family: "{FONT_DISPLAY}", "Segoe UI"; font-size: 17px; font-weight: 600; color: {t['text']}; }}
QLabel[role="h3"] {{ font-size: 13px; font-weight: 600; color: {t['text']}; }}
QLabel[role="eyebrow"] {{ font-size: 10px; font-weight: 700; color: {t['faint']}; letter-spacing: 2px; }}
QLabel[role="muted"] {{ color: {t['muted']}; font-size: 12px; }}
QLabel[role="faint"] {{ color: {t['faint']}; font-size: 11px; }}
QLabel[role="metric"] {{ font-family: "{FONT_DISPLAY}", "Segoe UI"; font-size: 31px; font-weight: 650; color: {t['text']}; }}
QLabel[role="mono"] {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 11px; color: {t['muted']}; }}
QLabel[role="danger"] {{ color: {t['red']}; font-size: 12px; }}
QLabel[role="success"] {{ color: {t['green']}; font-size: 12px; }}
QLabel[role="sectionTitle"] {{ font-size: 11px; font-weight: 700; color: {t['muted']}; letter-spacing: 1px; }}
QLabel[badge="true"] {{ border-radius: 9px; padding: 3px 8px; font-size: 10px; font-weight: 700; letter-spacing: 0.3px; }}
QLabel[tone="available"], QLabel[tone="resolved"], QLabel[tone="true"] {{ color: {t['green']}; background: {rgba('green', 0.11)}; border: 1px solid {rgba('green', 0.24)}; }}
QLabel[tone="unavailable"], QLabel[tone="shelved"], QLabel[tone="unknown"] {{ color: {t['muted']}; background: {rgba('muted', 0.09)}; border: 1px solid {rgba('muted', 0.18)}; }}
QLabel[tone="completed"] {{ color: {t['blue']}; background: {rgba('blue', 0.11)}; border: 1px solid {rgba('blue', 0.24)}; }}
QLabel[tone="deferred"], QLabel[tone="stalled"] {{ color: {t['amber']}; background: {rgba('amber', 0.10)}; border: 1px solid {rgba('amber', 0.24)}; }}
QLabel[tone="false"], QLabel[tone="error"] {{ color: {t['red']}; background: {rgba('red', 0.10)}; border: 1px solid {rgba('red', 0.24)}; }}
QLabel[tone="active"] {{ color: {t['accent']}; background: {rgba('accent', 0.10)}; border: 1px solid {rgba('accent', 0.22)}; }}
QLabel[tone="accent"] {{ color: {t['accent']}; background: {rgba('accent', 0.10)}; border: 1px solid {rgba('accent', 0.22)}; }}
QLabel[tone="warn"] {{ color: {t['amber']}; background: {rgba('amber', 0.10)}; border: 1px solid {rgba('amber', 0.24)}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border_hi']}; min-height: 30px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['border_hi']}; min-width: 30px; border-radius: 4px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QTableWidget {{
    background: {t['surface']};
    alternate-background-color: {t['row_alt']};
    border: 1px solid {t['border']};
    border-radius: 12px;
    gridline-color: transparent;
    color: {t['text_soft']};
    selection-background-color: {t['selection']};
    selection-color: {t['text']};
}}
QTableWidget::item {{ padding: 8px 10px; border-bottom: 1px solid {t['border']}; }}
QTableWidget::item:hover {{ background: {t['hover']}; }}
QHeaderView::section {{
    background: {t['surface_2']};
    color: {t['muted']};
    border: none;
    border-bottom: 1px solid {t['border']};
    padding: 9px 10px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
}}
QHeaderView::section:hover {{ background: {t['hover']}; color: {t['text']}; }}
QTableCornerButton::section {{ background: {t['surface_2']}; border: none; }}

QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border_hi']}; border-radius: 7px; padding: 5px 8px; font-size: 11px; }}
QMenu {{ background: {t['surface']}; border: 1px solid {t['border_hi']}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; color: {t['text_soft']}; }}
QMenu::item:selected {{ background: {t['selection']}; color: {t['text']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 8px; }}

QSplitter::handle {{ background: transparent; width: 1px; }}
QSplitter::handle:hover {{ background: {t['border_hi']}; }}
QGraphicsView {{ border: none; background: transparent; }}
QProgressBar {{ background: {t['surface_3']}; border: none; border-radius: 3px; height: 5px; text-align: center; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
"""
