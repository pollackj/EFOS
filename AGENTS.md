# EFOS — Agent & Engineering Guide

## Project Overview
- **Application:** EFOS (desktop task/project/condition dependency manager).
- **Stack:** Python 3.x, Qt (`PyQt6`), PostgreSQL (`localhost:5432/efos`), packaged via `build.ps1` (PyInstaller) into `release/EFOS.exe`.

## Core Architecture & Invariants
- **Database Schema:** Frozen unless explicitly requested. Do not alter tables, columns, or migrations when building UI features.
- **UI & Styling (`efos_app/ui/`):**
  - Always use valid palette keys from `theme.py` via `c(...)` (e.g., `surface`, `input`, `border`, `accent`, `text`).
  - Theme system uses a 6-stop (`0`–`5`) dark-to-slate spectrum (Deep Dark at `0` through Neutral Slate at `5`); do not reintroduce light themes above `5`.
  - Do not use `rgba('token_name', alpha)` inside `setStyleSheet` strings unless verified supported by `theme.py`.
  - Always use `GreenCheckBox` from `widgets.py` (crisp green-outlined checkmark) for checkboxes rather than solid-fill `QCheckBox` indicators.
  - When styling custom `QPushButton` subclasses directly via `self.setStyleSheet(...)`, target the subclass name (e.g., `HelpButton { ... }`) rather than `QPushButton`.
  - Reuse shared UI primitives (`SectionLabel`, `Divider`, `GreenCheckBox`, existing layout spacing) so new panels match the Inspector and main window.
- **Graph Layout (`efos_app/ui/graph.py`):**
  - Connected chains flow left-to-right (upstream Tasks/Conditions on the left, downstream Projects on the right), sorted alphabetically by Project with upstream dependencies clustered and vertically centered beside their target.

## Workflow & Verification Rules
1. **Plan First for Multi-File Features:** Before editing multiple files, outline affected modules and shared state changes.
2. **Test Locally:** Verify changes using `.venv\Scripts\python.exe main.py` (or `.venv\Scripts\python.exe -m pytest tests -q`) rather than `uv run` (which fails under sandbox cache locks), confirming clean startup with zero Qt stylesheet or console warnings.
3. **Packaging:** Do NOT run `build.ps1` automatically after minor edits; only run it when explicitly asked to build `release/EFOS.exe`.
4. **Git Hygiene:** Keep commits focused and verify `git status` shows no stray temporary or staged build artifacts.
