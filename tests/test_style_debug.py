"""Theme regression checks: six palette stops, three appearance choices, contrast."""
from __future__ import annotations

import pytest

from efos_app.ui import theme


def test_six_palette_stops_share_the_same_token_set():
    keys = set(theme.PALETTES[0])
    assert sorted(theme.PALETTES) == [0, 1, 2, 3, 4, 5]
    for stop in theme.THEME_STEPS:
        assert set(theme.PALETTES[stop]) == keys
    # The end points are the authored Deep Dark and Neutral Slate palettes.
    assert theme.PALETTES[0]["bg"] == "#090B10"
    assert theme.PALETTES[5]["bg"] == "#171A1F"


def test_light_range_is_gone_and_three_appearance_choices_use_stops_0_2_4():
    assert not hasattr(theme, "THEME_LIGHT")
    assert 50 not in theme.PALETTES
    assert 100 not in theme.PALETTES
    # The six-stop spectrum is intact; the Settings surface offers three of it.
    assert theme.THEME_STEPS == (0, 1, 2, 3, 4, 5)
    assert theme.APPEARANCE_OPTIONS == (
        ("Dark", 0),
        ("Graphite", 2),
        ("Slate", 4),
    )
    assert theme.APPEARANCE_VALUES == (0, 2, 4)
    assert theme.THEME_PRESETS == {
        theme.THEME_DARK: 0,
        theme.THEME_GRAPHITE: 2,
        theme.THEME_SLATE: 4,
    }
    assert theme.preset_name(0) == theme.THEME_DARK
    assert theme.preset_name(2) == theme.THEME_GRAPHITE
    assert theme.preset_name(4) == theme.THEME_SLATE
    assert theme.preset_name(5) == theme.THEME_SLATE
    assert [theme.appearance_label(value) for value in theme.APPEARANCE_VALUES] == [
        "Dark",
        "Graphite",
        "Slate",
    ]
    # The six stop names themselves are unchanged for the palette machinery.
    assert [theme.theme_step_label(stop) for stop in theme.THEME_STEPS] == [
        "Deep Dark",
        "Charcoal",
        "Graphite",
        "Steel",
        "Slate",
        "Neutral Slate",
    ]


def test_appearance_choices_match_the_palette_stops_exactly():
    original = theme.current_theme_value()
    try:
        for value in theme.APPEARANCE_VALUES:
            theme.set_theme_value(value)
            assert theme.current_theme_value() == value
            for key in ("bg", "surface", "text", "accent"):
                assert theme.c(key) == theme.PALETTES[value][key]
    finally:
        theme.set_theme_value(original)


def test_stops_get_lighter_from_deep_dark_to_neutral_slate():
    luminance = [
        theme._relative_luminance(theme._parse_color(theme.PALETTES[stop]["bg"])[:3])
        for stop in theme.THEME_STEPS
    ]
    assert luminance == sorted(luminance)
    assert luminance[0] < luminance[-1]


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        # Current notched stops: 0–1 Dark, 2–3 Graphite, 4–5 Slate.
        (0, 0),
        (1, 0),
        (2, 2),
        (3, 2),
        (4, 4),
        (5, 4),
        # Retired 0–100 slider: 0–10 Dark, 11–30 Graphite, 31+ Slate.
        (6, 0),
        (10, 0),
        (11, 2),
        (20, 2),
        (30, 2),
        (31, 4),
        (40, 4),
        (50, 4),
        (75, 4),
        (100, 4),
    ],
)
def test_persisted_values_migrate_onto_three_appearance_choices(persisted, expected):
    original = theme.current_theme_value()
    try:
        assert theme.normalise_appearance_value(persisted) == expected
        assert theme.set_theme_value(persisted) == expected
    finally:
        theme.set_theme_value(original)


def test_legacy_theme_names_migrate_onto_appearance_choices():
    original = theme.current_theme_value()
    try:
        assert theme.theme_value_for("light") == 4
        assert theme.theme_value_for("deep dark") == 0
        assert theme.theme_value_for("charcoal") == 0
        assert theme.theme_value_for("graphite") == 2
        assert theme.theme_value_for("steel") == 2
        assert theme.theme_value_for("slate") == 4
        assert theme.theme_value_for("neutral slate") == 4
        assert theme.theme_value_for("dark") == 0
        assert theme.theme_value_for("40") == 4
        assert theme.set_theme("light") == 4
    finally:
        theme.set_theme_value(original)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, 0),
        ({"theme": "dark"}, 0),
        ({"theme": "light"}, 4),
        ({"theme_value": 2}, 2),
        ({"theme_value": 3}, 2),
        ({"theme_value": 4}, 4),
        ({"theme_value": 20}, 2),
        ({"theme_value": 40}, 4),
        ({"theme_value": 100}, 4),
        # A numeric value wins over a stale name.
        ({"theme_value": 0, "theme": "light"}, 0),
        # ``None`` falls through to the name.
        ({"theme_value": None, "theme": "graphite"}, 2),
    ],
)
def test_persisted_ui_state_migrates_cleanly(state, expected):
    assert theme.migrate_persisted_appearance(dict(state)) == expected


def test_text_contrast_stays_legible_across_all_six_stops():
    for value in theme.THEME_STEPS:
        background = theme._parse_color(theme.PALETTES[value]["bg"])[:3]
        foreground = theme._parse_color(theme.PALETTES[value]["text"])[:3]
        ratio = theme._contrast_ratio(foreground, background)
        assert ratio >= 4.0, f"text contrast {ratio:.2f} at theme stop {value}"
