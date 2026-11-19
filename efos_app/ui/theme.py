"""Visual system for EFOS: six notched dark-to-slate stops and QSS.

The palette engine keeps six discrete steps:

* ``0`` — Deep Dark (the absolute first-run default)
* ``1``–``4`` — progressively lifted Charcoal, Graphite, Steel, and Slate steps
* ``5`` — Neutral Slate, a balanced low-glare grey dark

Only the two end-point palettes are authored; every interior stop is
interpolated perceptually (OKLab) between them and the foreground tokens are
contrast-corrected against the resulting background so text stays legible at
*every* stop. The retired light 51–100 range no longer exists.

The Settings surface exposes three curated *appearance choices* drawn from that
spectrum — Dark (stop ``0``), Graphite (stop ``2``), and Slate (stop ``4``).
Legacy persisted values (the 0–100 slider, the 0–5 stops, and old theme names)
migrate onto one of those three stops through
:func:`normalise_appearance_value`. The active stop is persisted locally by the
main window; the PostgreSQL schema is never touched.
"""
from __future__ import annotations

import ctypes
import math
import re
import sys

from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

THEME_DARK = "dark"
THEME_GRAPHITE = "graphite"
THEME_SLATE = "slate"
DEFAULT_THEME = THEME_DARK

# The Settings dialog offers three exclusive appearance choices. Each choice is
# an existing palette stop, so the six-stop dark→slate spectrum survives intact
# while the UI stays simple:
#   Dark     → stop 0 (Deep Dark, the first-run default, formerly persisted 0)
#   Graphite → stop 2 (formerly persisted 20)
#   Slate    → stop 4 (formerly persisted 40)
APPEARANCE_OPTIONS: tuple[tuple[str, int], ...] = (
    ("Dark", 0),
    ("Graphite", 2),
    ("Slate", 4),
)
APPEARANCE_VALUES = tuple(value for _label, value in APPEARANCE_OPTIONS)
APPEARANCE_LABELS = {value: label for label, value in APPEARANCE_OPTIONS}

# Appearance names resolve to the curated stop. Stop 5 (Neutral Slate) and the
# retired light range remain valid *palette* inputs but are no longer offered.
THEME_PRESETS: dict[str, int] = {
    THEME_DARK: 0,
    THEME_GRAPHITE: 2,
    THEME_SLATE: 4,
}
THEME_STEPS = (0, 1, 2, 3, 4, 5)
# Backwards-compatible alias for code that iterates the stop list.
PRESET_VALUES = THEME_STEPS
THEME_STEP_LABELS = {
    0: "Deep Dark",
    1: "Charcoal",
    2: "Graphite",
    3: "Steel",
    4: "Slate",
    5: "Neutral Slate",
}
# Names persisted by builds that exposed the 0–100 slider. ``light`` migrates
# onto Slate, the most lifted appearance choice available.
LEGACY_THEME_NAMES = {
    "light": 4,
    "deep dark": 0,
    "charcoal": 0,
    "steel": 2,
    "neutral slate": 4,
}

# Authored end-point palettes. Every stop must define the same token set; the
# six notched stops are expanded from these two by _build_palette_stops().
_RAW_STOPS: dict[int, dict[str, str]] = {
    0: {
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
    5: {
        "bg": "#171A1F",
        "bg_raised": "#1C2026",
        "surface": "#22272E",
        "surface_2": "#282E36",
        "surface_3": "#30373F",
        "hover": "#363E48",
        "border": "#353C46",
        "border_hi": "#4B5563",
        "outline": "#414A56",
        "text": "#E7EAEE",
        "text_soft": "#C2C8D0",
        "muted": "#8C95A1",
        "faint": "#626C78",
        "accent": "#7C8CA3",
        "accent_2": "#55637A",
        "teal": "#4EC9B8",
        "amber": "#E3AE63",
        "green": "#62C795",
        "red": "#E07981",
        "pink": "#D98FB7",
        "blue": "#7FA6E0",
        "task": "#7FA6E0",
        "project": "#A995E0",
        "condition": "#E3AE63",
        "row_alt": "#1F242B",
        "selection": "#2F3A4A",
        "canvas": "#15181D",
        "canvas_dot": "rgba(255, 255, 255, 0.040)",
        "canvas_dot_accent": "rgba(160, 180, 220, 0.060)",
        "sidebar": "#14171C",
        "titlebar": "#121519",
        "statusbar": "#14171C",
        "input": "#1A1E24",
        "input_focus": "#1E232A",
        "shadow": "rgba(0, 0, 0, 0.45)",
    },
}

# Foreground tokens are corrected against the interpolated backgrounds. The
# thresholds follow WCAG: body text 7:1, secondary 5:1, muted 4:1, faint 3:1.
_CONTRAST_FLOORS = {
    "text": 7.0,
    "text_soft": 5.0,
    "muted": 4.0,
    "faint": 3.0,
}
_CONTRAST_BACKGROUNDS = ("bg", "surface", "input")
_RGBA_RE = re.compile(
    r"rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)"
    r"(?:\s*,\s*([0-9.]+)\s*)?\)",
    re.IGNORECASE,
)

FONT_FAMILY = "Segoe UI Variable Text"
FONT_DISPLAY = "Segoe UI Variable Display"

STATE_COLORS = {
    "ready": "green",
    "blocked": "faint",
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

_current_value = 0
_cache: dict[int, dict[str, str]] = {}


# ---------------------------------------------------------------------------
# Color maths
# ---------------------------------------------------------------------------
def _parse_color(value: str) -> tuple[int, int, int, int]:
    text = str(value).strip()
    if text.startswith("#"):
        raw = text[1:]
        if len(raw) == 3:
            raw = "".join(channel * 2 for channel in raw)
        if len(raw) == 8:
            red, green, blue, alpha = (
                int(raw[index : index + 2], 16) for index in (0, 2, 4, 6)
            )
            return red, green, blue, alpha
        if len(raw) == 6:
            return (
                int(raw[0:2], 16),
                int(raw[2:4], 16),
                int(raw[4:6], 16),
                255,
            )
    match = _RGBA_RE.match(text)
    if match:
        red, green, blue = (int(round(float(match.group(i)))) for i in (1, 2, 3))
        raw_alpha = match.group(4)
        alpha = 255 if raw_alpha is None else int(round(float(raw_alpha) * 255))
        return (
            max(0, min(255, red)),
            max(0, min(255, green)),
            max(0, min(255, blue)),
            max(0, min(255, alpha)),
        )
    return 255, 255, 255, 255


def _is_rgba(value: str) -> bool:
    return str(value).strip().lower().startswith("rgba")


def _format_color(rgb: tuple[float, float, float], alpha: float, *, rgba: bool) -> str:
    red, green, blue = (max(0, min(255, int(round(channel)))) for channel in rgb)
    if rgba or alpha < 254.5:
        return f"rgba({red}, {green}, {blue}, {max(0.0, min(1.0, alpha / 255.0)):.3f})"
    return f"#{red:02X}{green:02X}{blue:02X}"


def _srgb_to_linear(channel: float) -> float:
    value = channel / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> float:
    value = max(0.0, min(1.0, value))
    if value <= 0.0031308:
        return value * 12.92 * 255.0
    return (1.055 * (value ** (1.0 / 2.4)) - 0.055) * 255.0


def _rgb_to_oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    red, green, blue = (_srgb_to_linear(channel) for channel in rgb)
    l_value = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    m_value = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    s_value = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    l_root = math.copysign(abs(l_value) ** (1.0 / 3.0), l_value)
    m_root = math.copysign(abs(m_value) ** (1.0 / 3.0), m_value)
    s_root = math.copysign(abs(s_value) ** (1.0 / 3.0), s_value)
    lightness = 0.2104542553 * l_root + 0.7936177850 * m_root - 0.0040720468 * s_root
    a_value = 1.9779984951 * l_root - 2.4285922050 * m_root + 0.4505937099 * s_root
    b_value = 0.0259040371 * l_root + 0.7827717662 * m_root - 0.8086757660 * s_root
    return lightness, a_value, b_value


def _oklab_to_rgb(lab: tuple[float, float, float]) -> tuple[float, float, float]:
    lightness, a_value, b_value = lab
    l_root = lightness + 0.3963377774 * a_value + 0.2158037573 * b_value
    m_root = lightness - 0.1055613458 * a_value - 0.0638541728 * b_value
    s_root = lightness - 0.0894841775 * a_value - 1.2914855480 * b_value
    l_value = l_root**3
    m_value = m_root**3
    s_value = s_root**3
    red = 4.0767416621 * l_value - 3.3077115913 * m_value + 0.2309699292 * s_value
    green = -1.2684380046 * l_value + 2.6097574011 * m_value - 0.3413193965 * s_value
    blue = -0.0041960863 * l_value - 0.7034186147 * m_value + 1.7076147010 * s_value
    return _linear_to_srgb(red), _linear_to_srgb(green), _linear_to_srgb(blue)


def _mix_colors(start: str, end: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    start_rgba = _parse_color(start)
    end_rgba = _parse_color(end)
    lab = tuple(
        plane[0] + (plane[1] - plane[0]) * amount
        for plane in zip(_rgb_to_oklab(start_rgba[:3]), _rgb_to_oklab(end_rgba[:3]))
    )
    red, green, blue = _oklab_to_rgb(lab)  # type: ignore[arg-type]
    alpha = start_rgba[3] + (end_rgba[3] - start_rgba[3]) * amount
    return _format_color(
        (red, green, blue), alpha, rgba=_is_rgba(start) or _is_rgba(end)
    )


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    red, green, blue = (
        _srgb_to_linear(channel) for channel in rgb[:3]
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    high, low = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (high + 0.05) / (low + 0.05)


def _meets_contrast(
    rgb: tuple[int, int, int], backgrounds: list[tuple[int, int, int]], floor: float
) -> bool:
    return min(_contrast_ratio(rgb, background) for background in backgrounds) >= floor


def _ensure_contrast(
    value: str,
    backgrounds: list[tuple[int, int, int]],
    floor: float,
) -> str:
    """Nudge a foreground color toward white/black until it meets the floor."""
    source = _parse_color(value)
    rgb = source[:3]
    if _meets_contrast(rgb, backgrounds, floor):
        return value
    white = (255, 255, 255)
    black = (0, 0, 0)
    white_score = min(_contrast_ratio(white, background) for background in backgrounds)
    black_score = min(_contrast_ratio(black, background) for background in backgrounds)
    target = white if white_score >= black_score else black
    best = target
    low, high = 0.0, 1.0
    for _ in range(18):
        amount = (low + high) / 2.0
        candidate = tuple(
            channel + (goal - channel) * amount for channel, goal in zip(rgb, target)
        )
        if _meets_contrast(candidate, backgrounds, floor):  # type: ignore[arg-type]
            best = candidate
            high = amount
        else:
            low = amount
    return _format_color(
        (best[0], best[1], best[2]), source[3], rgba=_is_rgba(value)
    )


# ---------------------------------------------------------------------------
# Palette stops
# ---------------------------------------------------------------------------
def _build_palette_stops() -> dict[int, dict[str, str]]:
    """Expand the two authored end points into the six notched stops.

    Stop ``n`` mixes the Deep Dark and Neutral Slate end points at ``n / 5``
    in OKLab, then applies the same WCAG foreground correction used for the
    former continuous slider. Stop ``n`` therefore reproduces the retired
    value ``n * 10`` exactly.
    """
    low, high = _RAW_STOPS[0], _RAW_STOPS[5]
    span = THEME_STEPS[-1] - THEME_STEPS[0]
    stops: dict[int, dict[str, str]] = {}
    for value in THEME_STEPS:
        amount = (value - THEME_STEPS[0]) / span
        if amount <= 0:
            result = dict(low)
        elif amount >= 1:
            result = dict(high)
        else:
            result = {key: _mix_colors(low[key], high[key], amount) for key in low}
        backgrounds = [
            _parse_color(result[key])[:3] for key in _CONTRAST_BACKGROUNDS
        ]
        for key, floor in _CONTRAST_FLOORS.items():
            result[key] = _ensure_contrast(result[key], backgrounds, floor)
        stops[value] = result
    return stops


PALETTES: dict[int, dict[str, str]] = _build_palette_stops()


def _stop_palette(value: int) -> dict[str, str]:
    value = max(THEME_STEPS[0], min(THEME_STEPS[-1], int(value)))
    return dict(PALETTES[value])


def _palette() -> dict[str, str]:
    palette = _cache.get(_current_value)
    if palette is None:
        palette = _stop_palette(_current_value)
        _cache[_current_value] = palette
    return palette


# ---------------------------------------------------------------------------
# Public theme API
# ---------------------------------------------------------------------------
def current_theme_value() -> int:
    return _current_value


def normalise_theme_value(value: int | float | str) -> int:
    """Clamp any supplied value onto the six-step ``0``–``5`` palette scale.

    This is the *palette* scale used by :data:`PALETTES`; the Settings surface
    offers only the three curated appearance stops. Values above ``5`` came
    from the retired 0–100 slider and are rescaled by one decade first, so a
    persisted ``40`` lands on stop ``4``.
    """
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    if numeric > THEME_STEPS[-1]:
        numeric = int(round(numeric / 10.0))
    return max(THEME_STEPS[0], min(THEME_STEPS[-1], numeric))


def normalise_appearance_value(value: int | float | str | None) -> int:
    """Coerce any persisted appearance preference onto Dark, Graphite or Slate.

    Migration rules (matching the three Settings choices):

    * stops ``0``–``1`` → ``0`` Dark · stops ``2``–``3`` → ``2`` Graphite ·
      stops ``4``–``5`` → ``4`` Slate
    * legacy 0–100 slider values: ``0``–``10`` → Dark, ``11``–``30`` →
      Graphite, ``31`` and above → Slate
    * names: ``dark`` → Dark, ``graphite`` → Graphite, ``slate`` → Slate, and
      the retired ``light`` name → Slate

    Values ``0``–``5`` are treated as the current notched stops (the currency
    written by recent builds); anything above ``5`` is a retired percentage.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in THEME_PRESETS:
            return THEME_PRESETS[text]
        if text in LEGACY_THEME_NAMES:
            return LEGACY_THEME_NAMES[text]
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return APPEARANCE_VALUES[0]
    if numeric <= THEME_STEPS[-1]:
        stop = max(THEME_STEPS[0], int(round(numeric)))
        if stop <= 1:
            return 0
        return 2 if stop <= 3 else 4
    legacy = int(round(numeric))
    if legacy <= 10:
        return 0
    if legacy <= 30:
        return 2
    return 4


def migrate_persisted_appearance(state: dict) -> int:
    """Resolve a persisted ``ui_state`` mapping to a curated appearance stop.

    ``theme_value`` wins over the older ``theme`` name; when neither is present
    the Deep Dark default applies. Callers persist the returned stop so legacy
    files are rewritten once and stay migrated.
    """
    raw = state.get("theme_value")
    if raw is None:
        raw = state.get("theme", DEFAULT_THEME)
    return normalise_appearance_value(raw)


def theme_step_label(value: int) -> str:
    """Human-readable name for a notched palette stop (``4`` → ``Slate``)."""
    return THEME_STEP_LABELS[normalise_theme_value(value)]


def appearance_label(value: int | float | str | None) -> str:
    """Human-readable label for a curated appearance stop (``2`` → Graphite)."""
    return APPEARANCE_LABELS[normalise_appearance_value(value)]


def theme_value_for(name: str) -> int:
    """Resolve a persisted theme name (including legacy ``light``) to a stop."""
    return normalise_appearance_value(name)


def preset_name(value: int | float | str) -> str:
    target = normalise_appearance_value(value)
    for name, stop in THEME_PRESETS.items():
        if stop == target:
            return name
    return THEME_DARK


def current_theme() -> str:
    return preset_name(_current_value)


def set_theme_value(value: int | float | str) -> int:
    """Apply a curated appearance stop (legacy values are migrated first)."""
    global _current_value
    _current_value = normalise_appearance_value(value)
    return _current_value


def set_theme(name: str | int) -> int:
    """Backwards-compatible setter accepting a preset name or numeric value."""
    if isinstance(name, int):
        return set_theme_value(name)
    return set_theme_value(theme_value_for(name))


def is_dark() -> bool:
    return _relative_luminance(_parse_color(c("bg"))[:3]) < 0.38


def c(key: str) -> str:
    palette = _palette()
    return palette.get(key, PALETTES[0].get(key, "#FFFFFF"))


def state_color(value: str) -> str:
    return c(STATE_COLORS.get(str(value), "muted"))


def rgba(key_or_color: str, alpha: float) -> str:
    """Return a Qt rgba() value from a palette key or a hex color."""
    color = c(key_or_color) if key_or_color in PALETTES[0] else key_or_color
    red, green, blue, _ = _parse_color(color)
    return (
        f"rgba({red}, {green}, {blue}, "
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


def apply_theme(
    app: QApplication,
    name: str | int | None = None,
    *,
    value: int | None = None,
) -> str:
    if value is not None:
        set_theme_value(value)
    elif name is not None:
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
    highlight_text = (
        "#FFFFFF"
        if _contrast_ratio(
            _parse_color(c("accent"))[:3], (255, 255, 255)
        )
        >= _contrast_ratio(_parse_color(c("accent"))[:3], (0, 0, 0))
        else "#000000"
    )
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(highlight_text))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c("faint")))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c("surface_3")))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c("text")))
    app.setPalette(palette)
    return current_theme()


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
        background = _parse_color(c("bg"))
        color = background[0] | (background[1] << 8) | (background[2] << 16)
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
    t = {key: c(key) for key in PALETTES[0]}
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
QCheckBox::indicator:hover {{ border-color: {t['green']}; }}
QCheckBox::indicator:checked {{ background: {t['green']}; border-color: {t['green']}; }}

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
QLabel[tone="ready"], QLabel[tone="resolved"], QLabel[tone="true"] {{ color: {t['green']}; background: {rgba('green', 0.11)}; border: 1px solid {rgba('green', 0.24)}; }}
QLabel[tone="blocked"], QLabel[tone="shelved"], QLabel[tone="unknown"] {{ color: {t['muted']}; background: {rgba('muted', 0.09)}; border: 1px solid {rgba('muted', 0.18)}; }}
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
