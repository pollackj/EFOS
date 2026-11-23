"""Headless UI mechanics tests: navigation, sorting, notes, assistant, calendar."""
from __future__ import annotations

from datetime import datetime, timedelta

from PyQt6.QtCore import QPoint, QTime, QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton

from efos_app.config import APP_VERSION
from efos_app.models import NodeKind, NodeRef, SyncState, TaskState
from efos_app.rag_service import PRIORITY_PROMPT
from efos_app.ui.calendar_page import GRID_PAD, HOUR_HEIGHT, DayCanvas, WeekCanvas
from efos_app.ui.dialogs import AvailabilityDialog
from efos_app.ui.graph import LayoutStore
from efos_app.ui.main_window import MainWindow
from efos_app.ui.settings_dialog import (
    CODE_TO_FONT_SCALE,
    FONT_SIZE_OPTIONS,
    FONT_SCALE_TO_CODE,
    INSPECTOR_WIDTH_PRESETS,
    LANDING_VIEWS,
    nearest_inspector_width,
)
from efos_app.ui.theme import (
    APPEARANCE_VALUES,
    c as theme_color,
    current_theme,
    current_theme_value,
    fs,
    preset_name,
    set_font_scale,
)
from efos_app.ui.widgets import (
    PANE_GUTTER,
    PAGE_MARGIN,
    TOOLBAR_HEIGHT,
    GreenCheckBox,
)

def _labels(widget) -> list[str]:  # type: ignore[no-untyped-def]
    return [label.text() for label in widget.findChildren(QLabel)]

def _buttons(widget) -> list[str]:  # type: ignore[no-untyped-def]
    return [button.text() for button in widget.findChildren(QPushButton)]

def _window(service, qapp, name: str = "layout.json") -> MainWindow:
    store = LayoutStore(service.config.data_dir / f"ui_{name}", service.config.db_display)
    window = MainWindow(service, service.config, store)
    window.resize(1500, 950)
    window.show()
    qapp.processEvents()
    return window

def test_window_navigation_sidebar_and_status(service, qapp):
    service.create_task("Visible task")
    window = _window(service, qapp, "nav.json")
    assert window.stack.count() == 5
    assert [button.text() for button in window.nav_buttons] == [
        "Overview",
        "Dashboard",
        "Registry",
        "Assistant",
        "Calendar",
    ]
    # The legacy ENGINE panel and its metadata are gone.
    all_labels = _labels(window)
    assert "ENGINE" not in all_labels
    assert not any("Reactive evaluation" in text for text in all_labels)
    # PostgreSQL indicator moved to the bottom-left status bar.
    assert window.db_label.text() == "Postgres"
    assert window.db_meta.text() == service.config.db_display
    # The scheduler slogan is gone; the version sits beside the SQL guard.
    assert window.footer_version.text() == (
        f"EFOS {APP_VERSION} · explicit dependency model"
    )
    assert not any("Scheduler · 60s pulse" in text for text in all_labels)
    # Settings mirrors the workspace nav rows: line-art icon, same gutter and
    # height, left-aligned label, and it never keeps a checked state.
    assert window.settings_button.text() == "Settings"
    assert not window.settings_button.icon().isNull()
    assert window.settings_button.isCheckable() is False
    nav = window.nav_buttons[0]
    assert window.settings_button.iconSize() == nav.iconSize()
    assert window.settings_button.minimumHeight() == nav.minimumHeight()
    assert window.settings_button.geometry().x() == nav.geometry().x()
    assert window.settings_button.width() == nav.width()
    # The top-right quick theme toggle is gone; only window controls remain.
    assert not hasattr(window.title_bar, "theme_button")
    assert len(window.title_bar.findChildren(QPushButton)) == 3
    # The Manage Inspector starts level with the scrollable content, matching
    # Flow and Registry rather than floating above the first content card.
    manage_top = window.dashboard.manage_inspector.mapToGlobal(
        window.dashboard.manage_inspector.rect().topLeft()
    ).y()
    scroll_top = window.dashboard.manage_scroll.mapToGlobal(
        window.dashboard.manage_scroll.rect().topLeft()
    ).y()
    assert manage_top == scroll_top
    # The duplicate large "Dashboard" heading is gone: Manage | Flow, the
    # Flow-only canvas tools, and the create actions share one compact toolbar
    # row as tall as Registry's and Assistant's.
    dashboard = window.dashboard
    assert not any(
        label.text() == "Dashboard" for label in dashboard.findChildren(QLabel)
    )
    assert dashboard.toolbar.height() == TOOLBAR_HEIGHT
    assert dashboard.toolbar.isAncestorOf(dashboard.tabs)
    assert all(dashboard.toolbar.isAncestorOf(button) for button in dashboard._create_buttons)
    assert [button.text() for button in dashboard._create_buttons] == [
        "New task",
        "New project",
        "New condition",
    ]
    for button in dashboard.tabs.buttons:
        assert button.parentWidget() is dashboard.tabs
        assert dashboard.toolbar.isAncestorOf(button)
    assert dashboard.tabs.current() == 0
    # Flow-only canvas tools live on the same row and appear only on Flow.
    assert dashboard.flow_tools.isHidden()
    dashboard.show_flow()
    qapp.processEvents()
    assert not dashboard.flow_tools.isHidden()
    dashboard.show_manage()
    qapp.processEvents()
    assert dashboard.flow_tools.isHidden()
    # The toolbar and the Tasks/Projects cards below it share the page's left
    # edge, so the buttons no longer float inside a wider heading block.
    first_card = dashboard.manage_content.itemAt(0).widget()
    assert first_card is not None
    assert dashboard.toolbar.mapTo(dashboard, QPoint(0, 0)).x() == PAGE_MARGIN
    assert first_card.mapTo(dashboard, QPoint(0, 0)).x() == PAGE_MARGIN
    assert dashboard.tabs.mapTo(dashboard, QPoint(0, 0)).x() == PAGE_MARGIN
    # The status-bar version and SQL guard keep a visible gutter.
    guard = next(
        label
        for label in window.findChildren(QLabel)
        if label.text() == "READ-ONLY SQL GUARD"
    )
    version_right = window.footer_version.mapToGlobal(
        window.footer_version.rect().topRight()
    ).x()
    guard_left = guard.mapToGlobal(guard.rect().topLeft()).x()
    assert guard_left - version_right >= 10
    assert window.footer_version.width() >= window.footer_version.sizeHint().width()
    # The empty-state inspector has no stray horizontal or vertical track.
    empty_scroll = window.dashboard.manage_inspector.scroll
    assert empty_scroll.horizontalScrollBar().maximum() == 0
    assert empty_scroll.verticalScrollBar().maximum() == 0
    for index in range(5):
        window.set_page(index)
        qapp.processEvents()
        assert window.stack.currentIndex() == index
    window.scheduler.pulse()
    qapp.processEvents()
    assert not window.grab().isNull()
    # The version and SQL guard stay separated and unclipped at the minimum
    # window width.
    window.resize(1180, 760)
    qapp.processEvents()
    assert window.footer_version.width() >= window.footer_version.sizeHint().width()
    version_right = window.footer_version.mapToGlobal(
        window.footer_version.rect().topRight()
    ).x()
    guard_left = guard.mapToGlobal(guard.rect().topLeft()).x()
    assert guard_left - version_right >= 10
    window.close()
    qapp.processEvents()

def test_suggested_focus_card_navigates_without_mutation(service, qapp):
    task = service.create_task("Focus me")
    window = _window(service, qapp, "focus.json")
    recommendation = window.focus_card.recommendation
    assert recommendation is not None and recommendation.ref.id == task
    before = service.repo.get_task(task).current_state
    QTest.mouseClick(
        window.focus_card,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        window.focus_card.rect().center(),
    )
    qapp.processEvents()
    assert window.dashboard.stack.currentIndex() == 0
    assert window.dashboard.manage_inspector.ref == NodeRef(NodeKind.TASK, task)
    assert service.repo.get_task(task).current_state == before
    window.close()
    qapp.processEvents()

def test_registry_typed_sorting_and_autocomplete(service, qapp):
    ready = service.create_task("Alpha ready")
    done = service.create_task("Beta done")
    service.complete_task(done)
    deferred = service.create_task("Gamma deferred")
    service.defer_task(deferred)
    condition = service.create_condition("Lock", evaluation_state=False)
    locked = service.create_task("Delta locked")
    service.connect(NodeRef(NodeKind.CONDITION, condition), NodeRef(NodeKind.TASK, locked))

    extra = service.create_task("Epsilon extra")
    extra_condition = service.create_condition("Extra ready", evaluation_state=True)
    second = service.create_condition("Second ready", evaluation_state=True)
    service.connect(
        NodeRef(NodeKind.CONDITION, extra_condition), NodeRef(NodeKind.TASK, extra)
    )
    service.connect(NodeRef(NodeKind.CONDITION, second), NodeRef(NodeKind.TASK, extra))
    service.load_view()

    window = _window(service, qapp, "registry.json")
    window.set_page(2)
    qapp.processEvents()
    registry = window.registry
    assert registry.table.rowCount() >= 4
    # A horizontal gutter separates the sortable table from the Inspector.
    table_right = registry.table.mapToGlobal(registry.table.rect().topRight()).x()
    inspector_left = registry.inspector.mapToGlobal(
        registry.inspector.rect().topLeft()
    ).x()
    assert inspector_left - table_right >= 12
    assert registry.splitter.handleWidth() == PANE_GUTTER

    # Tasks open on the operational Status priority: Blocked → Ready →
    # Completed → Deferred, alphabetized by task name inside each status.
    assert registry.sort_key == "state"
    assert registry.sort_ascending is True
    default_states = [
        registry.table.item(row, 1).text()
        for row in range(registry.table.rowCount())
    ]
    assert default_states == [
        "Blocked",
        "Ready",
        "Ready",
        "Completed",
        "Deferred",
    ]
    assert "▲" in registry.table.horizontalHeaderItem(1).text()

    registry._sort_by_column(1)  # STATUS flips to descending
    qapp.processEvents()
    states = [
        registry.table.item(row, 1).text() for row in range(registry.table.rowCount())
    ]
    assert states == list(reversed(default_states))
    assert registry.table.horizontalHeaderItem(1).text().endswith("▼")

    registry._sort_by_column(1)  # and back to the operational ascending order
    qapp.processEvents()
    states = [
        registry.table.item(row, 1).text() for row in range(registry.table.rowCount())
    ]
    assert states == default_states
    assert registry.table.horizontalHeaderItem(1).text().endswith("▲")

    registry._sort_by_column(2)  # conditions numeric count
    qapp.processEvents()
    counts = [
        int(registry.table.item(row, 2).text())
        for row in range(registry.table.rowCount())
    ]
    assert counts == sorted(counts)

    # Autocomplete suggestions update on individual keystrokes and selection
    # locks the context into the shared inspector.
    registry.search.setFocus()
    QTest.keyClicks(registry.search, "Delta")
    qapp.processEvents()
    assert "Task · Delta locked" in registry._suggestion_refs
    registry._suggestion_activated("Task · Delta locked")
    qapp.processEvents()
    assert registry.inspector.ref == NodeRef(NodeKind.TASK, locked)
    assert registry.table.rowCount() >= 1

    # The All filter includes all three object types.
    registry.segments.select(0)
    qapp.processEvents()
    assert registry.table.columnCount() == 5
    window.close()
    qapp.processEvents()

def test_assistant_launchpad_presets_custom_and_priority(service, qapp):
    task = service.create_task("Priority task")
    window = _window(service, qapp, "chat.json")
    window.set_page(3)
    qapp.processEvents()
    chat = window.chat
    assert chat.disclaimer.text() == (
        "It is NOT recommended to use the EFOS Assistant as a general chatbot."
    )
    assert chat.launch_scroll.isVisible()
    texts = _buttons(chat)
    for preset in (
        "Which Tasks are ready now?",
        "List active Projects",
        "What conditions are unknown?",
        PRIORITY_PROMPT,
    ):
        assert preset in texts

    service.create_custom_query("Overdue Audit", "A long backend instruction payload")
    chat.refresh(window.view)
    qapp.processEvents()
    assert "Overdue Audit" in _buttons(chat)

    chat._submit(PRIORITY_PROMPT)
    for _ in range(200):
        qapp.processEvents()
        QTest.qWait(20)
        if chat._worker is None and chat.thread_id is not None:
            break
    thread_id = chat.thread_id
    assert thread_id is not None
    _, messages = service.load_thread(thread_id)
    assert messages[-1].role == "assistant"
    assert messages[-1].metadata.get("route") == "PRIORITIES"
    assert "priorities" in messages[-1].content.lower()
    assert "Specialized local analyzer" in chat.pipeline_label.text()

    # The preset injected into the existing thread; no new thread was spawned.
    assert service.repo.count_threads() == 1
    window.close()
    qapp.processEvents()

def test_calendar_four_views_and_stale_warning(service, qapp):
    start = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    service.create_task("Calendar task", start=start, end=start + timedelta(hours=1))
    window = _window(service, qapp, "calendar.json")
    window.set_page(4)
    qapp.processEvents()
    calendar = window.calendar
    for index, mode in enumerate(("day", "week", "month", "schedule")):
        calendar.view_switch.select(index)
        qapp.processEvents()
        assert calendar.stack.currentIndex() == index
        assert calendar.mode == mode
    assert len(calendar.month_view.cells) == 42

    service.repo.save_calendar_account(
        "google",
        "user@example.com",
        "primary",
        status="stale",
        last_sync_at=start - timedelta(days=2),
        error="offline",
    )
    calendar._refresh_sync_state()
    assert calendar.warning.text().startswith("Sync Unavailable — Data Stale as of")
    window.close()
    qapp.processEvents()

def test_calendar_month_overflow_counter(service, qapp):
    day = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    service.repo.save_calendar_account(
        "google", "user@example.com", "primary", status="ok", last_sync_at=day
    )
    service.repo.upsert_calendar_events(
        "google",
        "primary",
        [
            {
                "id": f"evt-{index}",
                "summary": f"Event {index}",
                "description": "",
                "location": "",
                "status": "confirmed",
                "start_at": day + timedelta(minutes=index * 30),
                "end_at": day + timedelta(minutes=index * 30 + 25),
                "all_day": False,
            }
            for index in range(5)
        ],
    )
    window = _window(service, qapp, "calendar_overflow.json")
    window.set_page(4)
    window.calendar.view_switch.select(2)
    qapp.processEvents()
    labels = _labels(window.calendar.month_view)
    assert any(text.startswith("+") and text.endswith("more") for text in labels)
    window.close()
    qapp.processEvents()

def test_notes_blur_save_and_context_intercept(service, qapp):
    task = service.create_task("Note task")
    window = _window(service, qapp, "notes.json")
    window.dashboard.select_manage(NodeRef(NodeKind.TASK, task))
    qapp.processEvents()
    inspector = window.dashboard.manage_inspector
    assert inspector._note_edit is not None
    inspector._note_edit.setFocus()
    inspector._note_edit.setPlainText("First saved note")
    qapp.processEvents()
    assert inspector.has_pending_note()
    # Navigating away intercepts the context change and writes synchronously.
    window.set_page(3)
    qapp.processEvents()
    assert service.get_note(NodeRef(NodeKind.TASK, task)) == "First saved note"

    # An empty edit must never erase the stored block.
    window.set_page(1)
    window.dashboard.select_manage(NodeRef(NodeKind.TASK, task))
    qapp.processEvents()
    inspector = window.dashboard.manage_inspector
    inspector._note_edit.setPlainText("")
    inspector.commit_note()
    assert service.get_note(NodeRef(NodeKind.TASK, task)) == "First saved note"

    # A real focus-out (blur) also commits automatically.
    inspector._note_edit.setFocus()
    inspector._note_edit.setPlainText("Blur-saved content")
    window.new_button.setFocus()
    qapp.processEvents()
    QTest.qWait(60)
    qapp.processEvents()
    assert service.get_note(NodeRef(NodeKind.TASK, task)) == "Blur-saved content"
    window.close()
    qapp.processEvents()

def test_completed_task_hidden_from_operational_views(service, qapp):
    task = service.create_task("Lifecycle task")
    window = _window(service, qapp, "lifecycle.json")
    window.dashboard.select_manage(NodeRef(NodeKind.TASK, task))
    qapp.processEvents()
    service.complete_task(task)
    window.refresh_all()
    qapp.processEvents()
    # Dashboard queue and shared inspector drop the completed task.
    assert window.dashboard.manage_inspector.ref is None
    # Flow canvas drops the node.
    key = NodeRef(NodeKind.TASK, task).key
    assert key not in window.dashboard.flow.scene.node_items
    # Registry retains it for history, sorting, and the Reopen action.
    window.set_page(2)
    qapp.processEvents()
    names = [
        window.registry.table.item(row, 0).text()
        for row in range(window.registry.table.rowCount())
    ]
    assert "Lifecycle task" in names
    service.reopen_task(task)
    window.refresh_all()
    qapp.processEvents()
    assert key in window.dashboard.flow.scene.node_items
    window.close()
    qapp.processEvents()

def test_canvas_port_drag_creates_precondition(service, qapp):
    from PyQt6.QtCore import QPointF
    from efos_app.ui.graph import NODE_HEIGHT, NODE_WIDTH

    condition = service.create_condition("Drag precondition", evaluation_state=True)
    task = service.create_task("Drag target")
    window = _window(service, qapp, "drag.json")
    window.set_page(1)
    window.dashboard.show_flow()
    qapp.processEvents()
    flow = window.dashboard.flow
    scene = flow.scene
    viewport = flow.viewport
    source = scene.node_items[NodeRef(NodeKind.CONDITION, condition).key]
    target = scene.node_items[NodeRef(NodeKind.TASK, task).key]
    source_pos = viewport.mapFromScene(
        source.mapToScene(QPointF(NODE_WIDTH, NODE_HEIGHT / 2))
    )
    target_pos = viewport.mapFromScene(
        target.mapToScene(QPointF(NODE_WIDTH / 2, NODE_HEIGHT / 2))
    )
    QTest.mousePress(
        viewport.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        source_pos,
    )
    QTest.mouseMove(viewport.viewport(), target_pos, 20)
    QTest.mouseRelease(
        viewport.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        target_pos,
    )
    qapp.processEvents()
    snapshot = service.repo.snapshot()
    assert condition in snapshot.task_condition_ids(task)
    window.close()
    qapp.processEvents()

def test_registry_sort_value_and_null_created_semantics():
    from efos_app.ui.registry_page import RegistryPage

    class SortingProbe:
        pass

    page = SortingProbe()
    page.sort_key = "value"
    page.sort_ascending = True
    rows = [{"value": True}, {"value": None}, {"value": False}]
    assert [
        row["value"] for row in RegistryPage._sorted(page, rows)
    ] == [False, None, True]

    stamp = datetime(2026, 11, 9, 10, 0)
    page.sort_key = "created"
    page.sort_ascending = True
    rows = [
        {"created": stamp},
        {"created": None},
        {"created": stamp - timedelta(days=1)},
    ]
    assert [
        row["created"] for row in RegistryPage._sorted(page, rows)
    ] == [stamp - timedelta(days=1), stamp, None]
    page.sort_ascending = False
    assert [
        row["created"] for row in RegistryPage._sorted(page, rows)
    ] == [stamp, stamp - timedelta(days=1), None]

def test_appearance_choices_are_dark_graphite_slate_and_persist(service, qapp):
    from efos_app.config import load_ui_state

    def settle() -> None:
        # Theme rebuilds recreate the Calendar page, which may immediately start
        # a background probe or sync. Let those finish before the next change.
        window._sync_timer.stop()
        for attr in ("_sync_worker", "_connect_worker"):
            worker = getattr(window.calendar, attr, None)
            if worker is not None:
                worker.wait(3000)
                setattr(window.calendar, attr, None)
        qapp.processEvents()

    window = _window(service, qapp, "theme.json")
    settle()
    window.dashboard.show_flow()

    start_value = current_theme_value()
    assert start_value in APPEARANCE_VALUES
    # The header carries no quick theme toggle any more.
    assert not hasattr(window.title_bar, "theme_button")
    window.change_theme_value(0)
    qapp.processEvents()
    assert window.dashboard.stack.currentIndex() == 1
    assert current_theme_value() == 0
    assert load_ui_state().get("theme_value") == 0
    assert load_ui_state().get("theme") == preset_name(0) == "dark"
    assert window.centralWidget() is not None
    assert not window.grab().isNull()
    settle()

    window.change_theme_value(2)
    qapp.processEvents()
    assert current_theme_value() == 2
    assert load_ui_state().get("theme_value") == 2
    assert load_ui_state().get("theme") == preset_name(2) == "graphite"
    settle()

    # The retired stop 5 (Neutral Slate) is no longer offered: it collapses
    # onto the Slate appearance choice.
    window.change_theme_value(5)
    qapp.processEvents()
    assert current_theme_value() == 4
    assert load_ui_state().get("theme_value") == 4
    assert load_ui_state().get("theme") == preset_name(4) == "slate"
    settle()

    # Legacy 0–100 percentages migrate onto the three curated choices.
    window.change_theme_value(20)
    qapp.processEvents()
    assert current_theme_value() == 2
    settle()
    window.change_theme_value(40)
    qapp.processEvents()
    assert current_theme_value() == 4

    settle()
    window.change_theme_value(start_value)
    qapp.processEvents()
    assert current_theme_value() == start_value
    assert current_theme() == preset_name(start_value)
    window.close()
    qapp.processEvents()


def test_project_inspector_undefined_requirements_are_consistent(service, qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QComboBox, QSizePolicy

    project = service.create_project("Undefined project")
    window = _window(service, qapp, "undefined_project.json")
    window.dashboard.select_manage(NodeRef(NodeKind.PROJECT, project))
    qapp.processEvents()
    inspector = window.dashboard.manage_inspector
    labels = _labels(inspector)

    assert any(text.startswith("Requirements Undefined") for text in labels)
    assert "Undefined · dependency required" in labels
    assert "Satisfied" in labels
    assert "—" in labels
    # An undefined Project uses the standard em dash, not the ∅ glyph that
    # renders as a missing-glyph box in Segoe UI's uppercase SectionLabel.
    assert "DEPENDENCIES · —" in labels
    assert "DEPENDENCIES · ∅" not in labels
    # Empty dependency groups collapse instead of stacking empty sub-headers.
    assert "PRECONDITIONS · 3VL EXTERNAL FACTS" not in labels
    assert "PREREQUISITES · 2VL INTERNAL STATES" not in labels
    assert any("No dependencies attached" in text for text in labels)
    # Previous is no longer part of any inspector view.
    assert "Previous" not in labels
    # The inspector is a vertical-only surface.
    assert (
        inspector.scroll.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert (
        inspector.scroll.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    # Delete lives below NOTE at the very bottom of the content column.
    def layout_index(widget) -> int:
        for index in range(inspector.content.count()):
            if inspector.content.itemAt(index).widget() is widget:
                return index
        return -1

    delete = next(
        button
        for button in inspector.findChildren(QPushButton)
        if button.text() == "Delete project"
    )
    assert layout_index(delete) > layout_index(inspector._note_edit)

    # Long dependency candidates cannot widen the panel.
    for other in ("A" * 140, "B" * 140):
        service.create_task(other)
    short = service.create_task("Short task")
    window.refresh_all()
    window.dashboard.select_manage(NodeRef(NodeKind.TASK, short))
    qapp.processEvents()
    inspector = window.dashboard.manage_inspector
    combos = inspector.findChildren(QComboBox)
    assert combos
    for combo in combos:
        assert combo.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
        assert combo.minimumWidth() == 0
    assert inspector.scroll.horizontalScrollBar().maximum() == 0
    assert inspector.host.width() <= inspector.scroll.viewport().width() + 1
    attach = next(
        button for button in inspector.findChildren(QPushButton)
        if button.text() == "Attach"
    )
    assert attach.isVisible()
    attach_right = attach.mapTo(inspector, attach.rect().topLeft()).x() + attach.width()
    assert attach_right <= inspector.width()
    # The narrowest persisted width still wraps and elides instead of scrolling.
    window._settings_width_changed(300)
    qapp.processEvents()
    assert inspector.scroll.horizontalScrollBar().maximum() == 0
    assert inspector.host.width() <= inspector.scroll.viewport().width() + 1
    attach_right = attach.mapTo(inspector, attach.rect().topLeft()).x() + attach.width()
    assert attach_right <= inspector.width()
    window.close()
    qapp.processEvents()


def test_settings_persist_landing_view_width_and_flow_auto_layout(service, qapp):
    from efos_app.config import load_ui_state

    service.create_project("Settings project")
    window = _window(service, qapp, "settings.json")
    window._settings_landing_changed("Flow")
    window._settings_flow_changed(False)
    window._settings_width_changed(400)
    qapp.processEvents()
    state = load_ui_state()
    assert state.get("landing_view") == "Flow"
    assert state.get("flow_auto_layout") is False
    assert state.get("inspector_width") == 400
    window.close()
    qapp.processEvents()

    reopened = _window(service, qapp, "settings_reopened.json")
    qapp.processEvents()
    assert reopened.stack.currentIndex() == 1
    assert reopened.dashboard.stack.currentIndex() == 1
    assert reopened._landing_view == "Flow"
    assert reopened._flow_auto_layout is False
    assert reopened._inspector_width == 400
    widths = reopened.dashboard.splitter.sizes()
    assert len(widths) == 2 and 390 <= widths[1] <= 410
    # Assistant's Query pane follows the same 300–420 px preset once shown.
    reopened.set_page(3)
    qapp.processEvents()
    chat_widths = reopened.chat.splitter.sizes()
    assert len(chat_widths) == 2 and 390 <= chat_widths[1] <= 410
    assert reopened.dashboard.flow._startup_auto_layout is False
    reopened.close()
    qapp.processEvents()

    # Restore the local defaults for any later test that opens a window.
    window = _window(service, qapp, "settings_restore.json")
    window._settings_landing_changed("Dashboard")
    window._settings_flow_changed(True)
    window._settings_width_changed(360)
    window.close()
    qapp.processEvents()


def test_landing_view_options_route_to_the_right_surface(service, qapp):
    service.create_project("Landing project")
    window = _window(service, qapp, "landing.json")
    assert LANDING_VIEWS == (
        "Overview",
        "Dashboard",
        "Flow",
        "Projects",
        "Tasks",
        "Calendar",
        "Assistant",
    )
    for target, page in (
        ("Overview", 0),
        ("Calendar", 4),
        ("Assistant", 3),
        ("Flow", 1),
        ("Projects", 1),
        ("Tasks", 1),
        ("Dashboard", 1),
    ):
        window._landing_view = target
        window._apply_landing_view()
        qapp.processEvents()
        assert window.stack.currentIndex() == page, target
        if target == "Overview":
            assert window.stack.currentWidget() is window.overview
        if target == "Flow":
            assert window.dashboard.stack.currentIndex() == 1
        if target in ("Projects", "Tasks", "Dashboard"):
            assert window.dashboard.stack.currentIndex() == 0
    window.close()
    qapp.processEvents()


def test_sidebar_gear_opens_settings_dialog(service, qapp):
    window = _window(service, qapp, "settings_gear.json")
    observed: dict[str, bool] = {"opened": False}

    def close_dialog() -> None:
        dialog = window._settings_dialog
        observed["opened"] = dialog is not None
        if dialog is not None:
            dialog._close()

    QTimer.singleShot(80, close_dialog)
    QTest.mouseClick(window.settings_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert observed["opened"] is True
    assert window._settings_dialog is None
    window.close()
    qapp.processEvents()


def test_settings_dialog_offers_aligned_exclusive_three_option_groups(service, qapp):
    from efos_app.config import load_ui_state

    window = _window(service, qapp, "settings_dialog.json")
    observed: dict[str, object] = {}
    target = 2 if current_theme_value() != 2 else 4

    def interact() -> None:
        dialog = window._settings_dialog
        observed["dialog"] = dialog is not None
        if dialog is None:
            return
        observed["appearance_values"] = sorted(dialog.appearance_boxes)
        observed["width_values"] = sorted(dialog.width_boxes)
        observed["appearance_labels"] = [
            label.text() for label in dialog.appearance_choices.findChildren(QLabel)
        ]
        observed["width_labels"] = [
            label.text() for label in dialog.width_choices.findChildren(QLabel)
        ]
        observed["boxes_are_green"] = all(
            isinstance(box, GreenCheckBox) and box._centered
            for box in list(dialog.appearance_boxes.values())
            + list(dialog.width_boxes.values())
        )
        observed["checked"] = [
            value for value, box in dialog.appearance_boxes.items() if box.isChecked()
        ]
        # The two groups line up column for column: Dark/300, Graphite/360,
        # Slate/420 share the same horizontal centers.
        observed["appearance_centers"] = [
            label.mapToGlobal(label.rect().center()).x()
            for label in dialog.appearance_choices.findChildren(QLabel)
        ]
        observed["width_centers"] = [
            label.mapToGlobal(label.rect().center()).x()
            for label in dialog.width_choices.findChildren(QLabel)
        ]
        # Exclusive selection: choosing one unchecks the other two.
        QTest.mouseClick(dialog.appearance_boxes[4], Qt.MouseButton.LeftButton)
        QTest.mouseClick(dialog.appearance_boxes[target], Qt.MouseButton.LeftButton)
        observed["exclusive"] = (
            dialog.appearance_boxes[target].isChecked(),
            sum(1 for box in dialog.appearance_boxes.values() if box.isChecked()),
        )
        # Clicking the checked choice keeps it checked: never an empty group.
        QTest.mouseClick(dialog.appearance_boxes[target], Qt.MouseButton.LeftButton)
        observed["sticky"] = dialog.appearance_boxes[target].isChecked()
        # Width choices are applied on Save, not on click.
        observed["width_before"] = window.dashboard.splitter.sizes()[1]
        QTest.mouseClick(dialog.width_boxes[300], Qt.MouseButton.LeftButton)
        observed["width_during"] = window.dashboard.splitter.sizes()[1]
        dialog._flush_theme()
        observed["theme_after"] = current_theme_value()
        observed["green_flow_check"] = isinstance(dialog.flow_check, GreenCheckBox)
        observed["flow_text"] = dialog.flow_check.text()
        observed["combo"] = [
            dialog.landing_combo.itemText(index)
            for index in range(dialog.landing_combo.count())
        ]
        dialog._close()

    QTimer.singleShot(80, interact)
    window.open_settings()
    qapp.processEvents()

    assert observed.get("dialog") is True
    assert observed["appearance_values"] == [0, 2, 4]
    assert observed["width_values"] == list(INSPECTOR_WIDTH_PRESETS)
    assert observed["appearance_labels"] == ["Dark", "Graphite", "Slate"]
    assert observed["width_labels"] == ["300 px", "360 px", "420 px"]
    assert observed["boxes_are_green"] is True
    assert len(observed["checked"]) == 1
    assert observed["exclusive"] == (True, 1)
    assert observed["sticky"] is True
    assert all(
        abs(left - right) <= 1
        for left, right in zip(
            observed["appearance_centers"], observed["width_centers"]
        )
    )
    assert observed["width_before"] == observed["width_during"]
    assert observed["theme_after"] == target
    assert current_theme_value() == target
    assert observed["green_flow_check"] is True
    assert observed["flow_text"] == "Auto-layout the Flow canvas on startup"
    assert "Assistant" in observed["combo"]
    # Save applies the newly chosen 300 px preset.
    assert load_ui_state().get("inspector_width") == 300
    assert abs(window.dashboard.splitter.sizes()[1] - 300) <= 12
    assert window.centralWidget() is not None
    assert not window.grab().isNull()
    window.close()
    qapp.processEvents()


def test_settings_width_snaps_to_nearest_preset_without_applying_on_cancel(
    service, qapp
):
    from efos_app.config import load_ui_state

    window = _window(service, qapp, "settings_width.json")
    # A live drag can sit between presets; Settings selects the nearest one.
    window._settings_width_changed(400)
    qapp.processEvents()
    assert load_ui_state().get("inspector_width") == 400
    observed: dict[str, object] = {}

    def interact() -> None:
        dialog = window._settings_dialog
        observed["selected"] = dialog.width_value
        observed["checked"] = [
            value for value, box in dialog.width_boxes.items() if box.isChecked()
        ]
        observed["nearest"] = nearest_inspector_width(400)
        dialog.reject()

    QTimer.singleShot(80, interact)
    window.open_settings()
    qapp.processEvents()
    assert observed["nearest"] == 420
    assert observed["selected"] == 420
    assert observed["checked"] == [420]
    # Cancel must not snap the live splitter or the persisted width.
    assert load_ui_state().get("inspector_width") == 400
    assert abs(window.dashboard.splitter.sizes()[1] - 400) <= 12
    window.close()
    qapp.processEvents()


def test_flow_startup_auto_layout_runs_only_once(service, qapp):
    service.create_task("Flow startup task")
    window = _window(service, qapp, "flow_startup.json")
    flow = window.dashboard.flow
    flow.set_startup_auto_layout(True)
    flow.set_startup_layout_pending(True)
    calls = {"count": 0}
    original = flow._auto_layout

    def probe() -> None:
        calls["count"] += 1
        original()

    flow._auto_layout = probe  # type: ignore[method-assign]
    window.dashboard.show_flow()
    qapp.processEvents()
    assert calls["count"] == 1
    # Revisiting Flow during the same launch must not re-run the layout.
    window.dashboard.show_flow()
    qapp.processEvents()
    assert calls["count"] == 1
    window.close()
    qapp.processEvents()


def test_availability_dialog_and_status_are_presented_separately(service, qapp):
    task = service.create_task("Business call")
    scheduled_start = datetime.now().replace(microsecond=0) + timedelta(days=2)
    service.save_task_schedule(
        task, scheduled_start, scheduled_start + timedelta(hours=1)
    )

    dialog = AvailabilityDialog([], None)
    qapp.processEvents()
    assert dialog.always.isChecked()
    dialog._validate()
    assert dialog.values() == []

    dialog.always.setChecked(False)
    assert len(dialog._rows) == 1
    row = dialog._rows[0]
    for weekday, check in row["checks"].items():
        check.setChecked(weekday == 0)
    row["start"].setTime(QTime(9, 0))
    row["end"].setTime(QTime(17, 0))
    dialog._validate()
    values = dialog.values()
    assert len(values) == 1
    assert values[0].weekday == 0
    assert values[0].start_minute == 9 * 60
    assert values[0].end_minute == 17 * 60
    service.save_task_availability(task, values)
    stored = service.load_view().snapshot.tasks[task]
    assert stored.has_custom_availability is True

    window = _window(service, qapp, "availability.json")
    window.dashboard.select_manage(NodeRef(NodeKind.TASK, task))
    qapp.processEvents()
    inspector = window.dashboard.manage_inspector
    labels = _labels(inspector)
    for field in ("Status", "Availability", "Schedule", "Recurrence"):
        assert field in labels
    assert "Previous" not in labels
    # A Ready Task scheduled in the future remains Ready.
    assert "Ready" in labels

    gate = service.create_condition("Future gate", evaluation_state=False)
    blocked = service.create_task("Gated future task")
    service.save_task_schedule(
        blocked, scheduled_start, scheduled_start + timedelta(hours=1)
    )
    service.connect(
        NodeRef(NodeKind.CONDITION, gate), NodeRef(NodeKind.TASK, blocked)
    )
    window.refresh_all()
    window.dashboard.select_manage(NodeRef(NodeKind.TASK, blocked))
    qapp.processEvents()
    labels = _labels(window.dashboard.manage_inspector)
    assert "Blocked" in labels
    assert not any("Scheduled window" in text for text in labels)
    window.close()
    qapp.processEvents()


def test_unified_two_pane_geometry_across_workspace_views(service, qapp):
    service.create_task("Geometry task")
    window = _window(service, qapp, "geometry.json")
    qapp.processEvents()

    def top(widget) -> int:  # type: ignore[no-untyped-def]
        return widget.mapTo(window, QPoint(0, 0)).y()

    def bottom(widget) -> int:  # type: ignore[no-untyped-def]
        return widget.mapTo(window, QPoint(0, widget.height())).y()

    # Identical 24 px outer padding on the three page containers. Flow lives
    # inside the already-padded Dashboard stack, so it has no extra margin.
    for page in (window.dashboard, window.registry, window.chat):
        margins = page.layout().contentsMargins()
        assert (
            margins.left(),
            margins.top(),
            margins.right(),
            margins.bottom(),
        ) == (PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)

    measured: list[tuple[str, object]] = []
    window.set_page(1)
    window.dashboard.show_manage()
    qapp.processEvents()
    assert window.dashboard.splitter.handleWidth() == PANE_GUTTER
    measured.append(("Manage", window.dashboard.splitter))

    window.dashboard.show_flow()
    qapp.processEvents()
    assert window.dashboard.flow.splitter.handleWidth() == PANE_GUTTER
    measured.append(("Flow", window.dashboard.flow.splitter))

    window.set_page(2)
    qapp.processEvents()
    assert window.registry.splitter.handleWidth() == PANE_GUTTER
    measured.append(("Registry", window.registry.splitter))

    window.set_page(3)
    qapp.processEvents()
    assert window.chat.splitter.handleWidth() == PANE_GUTTER
    measured.append(("Assistant", window.chat.splitter))

    tops: list[int] = []
    bottoms: list[int] = []
    for name, splitter in measured:
        left = splitter.widget(0)
        right = splitter.widget(1)
        assert left is not None and right is not None, name
        assert abs(top(left) - top(right)) <= 1, name
        assert abs(bottom(left) - bottom(right)) <= 1, name
        tops.append(top(left))
        bottoms.append(bottom(left))
    # Both panes start and end at the same Y in every view, and every view
    # shares that geometry.
    assert len(set(tops)) == 1, tops
    assert len(set(bottoms)) == 1, bottoms

    # Assistant's Query pane is a rounded card constrained to 300–420 px.
    query = window.chat.splitter.widget(1)
    assert isinstance(query, QFrame)
    assert query.property("card") is True
    assert query.minimumWidth() == 300
    assert query.maximumWidth() == 420
    assert 300 <= window.chat.splitter.sizes()[1] <= 420
    window.close()
    qapp.processEvents()


def test_assistant_auto_scrolls_to_bottom_and_uses_the_shared_splitter(service, qapp):
    window = _window(service, qapp, "chat_scroll.json")
    window.set_page(3)
    qapp.processEvents()
    chat = window.chat
    assert chat.splitter.handleWidth() == PANE_GUTTER
    assert isinstance(chat.splitter.widget(0), QFrame)
    assert chat.splitter.widget(0).property("card") is True

    long_thread = service.create_thread()
    for index in range(24):
        service.append_message(
            long_thread, "user", f"Question {index} " + "context " * 14
        )
        service.append_message(
            long_thread, "assistant", f"Answer {index} " + "detail " * 46
        )
    short_thread = service.create_thread()
    service.append_message(short_thread, "user", "Short question")

    def settle() -> None:
        qapp.processEvents()
        QTest.qWait(140)
        qapp.processEvents()

    chat.thread_id = long_thread
    chat._load_thread(long_thread, render=True)
    settle()
    bar = chat.stream_scroll.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum()

    # Switching threads re-pins the new conversation to its bottom.
    chat.thread_id = short_thread
    chat._load_thread(short_thread, render=True)
    settle()
    assert bar.value() == bar.maximum()

    chat.thread_id = long_thread
    chat._load_thread(long_thread, render=True)
    settle()
    assert bar.value() == bar.maximum()

    # Returning to the page pins the conversation to its newest message.
    bar.setValue(0)
    window.set_page(1)
    window.set_page(3)
    settle()
    assert bar.value() == bar.maximum()
    window.close()
    qapp.processEvents()


def test_calendar_time_grid_has_padding_and_a_closing_24_hour_border(service, qapp):
    window = _window(service, qapp, "calendar_grid.json")
    window.set_page(4)
    qapp.processEvents()
    calendar = window.calendar
    expected_height = GRID_PAD * 2 + 24 * HOUR_HEIGHT
    assert GRID_PAD >= 12
    for canvas in (calendar.day_view.canvas, calendar.week_view.canvas):
        assert isinstance(canvas, (DayCanvas, WeekCanvas))
        assert canvas.height() == expected_height
        # The 12 AM label rect opens at GRID_PAD - 7, so it can never be clipped
        # when the grid is scrolled all the way to the top.
        assert GRID_PAD - 7 >= 5
        # The 24:00 closing border sits GRID_PAD above the canvas bottom.
        assert expected_height - (GRID_PAD + 24 * HOUR_HEIGHT) == GRID_PAD
        # Painting must not raise (grab() forces a full paintEvent).
        assert not canvas.grab().isNull()

    for view in (calendar.day_view, calendar.week_view):
        bar = view.scroll.verticalScrollBar()
        bar.setValue(0)
        qapp.processEvents()
        assert bar.value() == 0
        assert bar.maximum() >= view.canvas.height() - view.scroll.viewport().height()
        bar.setValue(bar.maximum())
        qapp.processEvents()
        assert bar.value() == bar.maximum()
    window.close()
    qapp.processEvents()


def test_calendar_connect_failure_always_resets_the_button_and_banner(service, qapp):
    window = _window(service, qapp, "calendar_oauth.json")
    window.set_page(4)
    qapp.processEvents()
    calendar = window.calendar

    calendar.connect_button.setText("Waiting for Google…")
    calendar.connect_button.setEnabled(False)
    calendar._connect_failed("Google OAuth client credentials are not configured.")
    assert calendar.connect_button.text() == "Connect Google"
    assert "connection failed" in calendar.warning.text().lower()
    assert "not configured" in calendar.warning.text()
    assert not calendar.warning.isHidden()

    # A wedged worker is caught by the watchdog instead of hanging forever.
    calendar._connect_worker = type(
        "Probe", (), {"isRunning": lambda self: True}
    )()
    calendar.connect_button.setText("Waiting for Google…")
    calendar.connect_button.setEnabled(False)
    calendar._connect_timed_out()
    assert calendar.connect_button.text() == "Connect Google"
    assert "too long" in calendar.warning.text().lower()
    calendar._connect_worker = None

    # finished() normalises the label even when no signal preceded it.
    calendar.connect_button.setText("Waiting for Google…")
    calendar.connect_button.setEnabled(False)
    calendar._connect_finished()
    assert calendar.connect_button.text() == "Connect Google"
    assert not calendar.warning.isHidden()
    window.close()
    qapp.processEvents()


def test_calendar_auto_syncs_stale_credentials_and_shows_a_green_banner(
    service, qapp, monkeypatch
):
    window = _window(service, qapp, "calendar_autosync.json")
    window.set_page(4)
    qapp.processEvents()
    calendar = window.calendar

    stale_at = datetime.now().replace(microsecond=0) - timedelta(hours=2)
    stale = SyncState(status="stale", last_sync_at=stale_at, connected=True)

    calls: list[dict] = []
    monkeypatch.setattr(calendar, "sync_now", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(calendar.service, "calendar_sync_state", lambda: stale)
    calendar._maybe_auto_sync()
    assert calls, "stale stored credentials should trigger a background sync"

    # A fresh snapshot is left alone.
    fresh = SyncState(
        status="ok", last_sync_at=datetime.now().replace(microsecond=0), connected=True
    )
    monkeypatch.setattr(calendar.service, "calendar_sync_state", lambda: fresh)
    calls.clear()
    calendar._maybe_auto_sync()
    assert not calls

    # Completion updates the persisted timestamp and turns the banner green
    # immediately (the service persists last_sync_at before returning).
    completed_at = datetime.now().replace(microsecond=0)
    completed = SyncState(status="ok", last_sync_at=completed_at, connected=True)
    monkeypatch.setattr(calendar.service, "calendar_sync_state", lambda: completed)
    calendar._sync_ready(completed)
    assert calendar.warning.text() == (
        "Google Calendar synchronized · "
        f"{completed_at.strftime('%b %d, %H:%M')}"
    )
    assert f"color: {theme_color('green')}" in calendar.warning.styleSheet()
    window.close()
    qapp.processEvents()


def test_text_size_presets_scale_fs_with_a_readable_floor(service, qapp):
    """The three Text size presets persist and ``fs`` never drops below 8 px."""
    from efos_app.config import load_ui_state
    from efos_app.ui import theme as theme_module

    original = theme_module._FONT_PRESET
    try:
        set_font_scale("standard")
        # The 8 px floor protects small authored sizes at the default preset.
        assert fs(1) == 8
        assert fs(7) == 8
        assert fs(8) == 8
        assert fs(13) == 13
        assert fs(24) == 24

        set_font_scale("large")
        # 8-11 px labels are lifted to a legible 13 px floor.
        assert fs(8) == 13
        assert fs(10) == 13
        assert fs(11) == 13
        assert fs(12) == round(12 * 1.15)
        assert fs(24) == round(24 * 1.15)

        set_font_scale("xlarge")
        assert fs(8) == 15
        assert fs(10) == 15
        assert fs(11) == 15
        assert fs(12) == round(12 * 1.30)
        assert fs(24) == round(24 * 1.30)
    finally:
        set_font_scale(original)

    assert [label for label, _code in FONT_SIZE_OPTIONS] == [
        "Default",
        "Large",
        "Extra Large",
    ]
    assert FONT_SCALE_TO_CODE == {"standard": 0, "large": 1, "xlarge": 2}
    assert CODE_TO_FONT_SCALE == {0: "standard", 1: "large", 2: "xlarge"}

    window = _window(service, qapp, "font_size.json")
    window.change_font_scale("large")
    qapp.processEvents()
    assert window._font_scale == "large"
    assert load_ui_state().get("font_scale") == "large"
    assert fs(10) >= 13
    # The page heading picks up the scaled size instead of a literal string.
    assert f"font-size: {fs(24)}px" in window.page_title.styleSheet()
    window.change_font_scale("xlarge")
    qapp.processEvents()
    assert window._font_scale == "xlarge"
    assert load_ui_state().get("font_scale") == "xlarge"
    assert fs(10) >= 15
    window.close()
    qapp.processEvents()

    reopened = _window(service, qapp, "font_size_reopened.json")
    assert reopened._font_scale == "xlarge"
    observed: dict[str, object] = {}

    def interact() -> None:
        dialog = reopened._settings_dialog
        if dialog is None:
            return
        observed["labels"] = [
            label.text() for label in dialog.font_choices.findChildren(QLabel)
        ]
        observed["codes"] = sorted(dialog.font_boxes)
        observed["checked"] = [
            value for value, box in dialog.font_boxes.items() if box.isChecked()
        ]
        dialog._close()

    QTimer.singleShot(80, interact)
    reopened.open_settings()
    qapp.processEvents()
    assert observed.get("labels") == ["Default", "Large", "Extra Large"]
    assert observed.get("codes") == [0, 1, 2]
    assert observed.get("checked") == [2]

    # Restore the local default so later windows open at the standard scale.
    reopened.change_font_scale("standard")
    qapp.processEvents()
    assert load_ui_state().get("font_scale") == "standard"
    assert fs(10) == 10
    reopened.close()
    qapp.processEvents()


def test_registry_defaults_to_operational_status_priority(service, qapp):
    """Tasks open sorted Blocked → Ready → Completed → Deferred by name."""
    ready_a = service.create_task("Alpha ready")
    ready_b = service.create_task("Beta ready")
    done = service.create_task("Gamma done")
    service.complete_task(done)
    parked = service.create_task("Delta parked")
    service.defer_task(parked)
    gate = service.create_condition("Blocking gate", evaluation_state=False)
    blocked_a = service.create_task("Echo blocked")
    blocked_b = service.create_task("Zulu blocked")
    service.connect(NodeRef(NodeKind.CONDITION, gate), NodeRef(NodeKind.TASK, blocked_a))
    service.connect(NodeRef(NodeKind.CONDITION, gate), NodeRef(NodeKind.TASK, blocked_b))
    service.load_view()

    window = _window(service, qapp, "registry_status.json")
    window.set_page(2)
    qapp.processEvents()
    registry = window.registry
    assert registry.kind is NodeKind.TASK
    assert registry.sort_key == "state"
    assert registry.sort_ascending is True

    def column_texts(column: int) -> list[str]:
        return [
            registry.table.item(row, column).text()
            for row in range(registry.table.rowCount())
        ]

    assert column_texts(1) == [
        "Blocked",
        "Blocked",
        "Ready",
        "Ready",
        "Completed",
        "Deferred",
    ]
    # Alphabetical inside each status group.
    assert column_texts(0) == [
        "Echo blocked",
        "Zulu blocked",
        "Alpha ready",
        "Beta ready",
        "Gamma done",
        "Delta parked",
    ]
    assert "▲" in registry.table.horizontalHeaderItem(1).text()
    assert "Blocked → Ready → Completed → Deferred" in registry.sort_label.text()

    # Column clicks still toggle direction and re-sort the same priority.
    registry._sort_by_column(1)
    qapp.processEvents()
    assert registry.table.horizontalHeaderItem(1).text().endswith("▼")
    assert column_texts(1)[0] == "Deferred"
    registry._sort_by_column(1)
    qapp.processEvents()
    assert registry.table.horizontalHeaderItem(1).text().endswith("▲")
    assert column_texts(1)[0] == "Blocked"

    # Every other header keeps working, alphabetically by task name.
    registry._sort_by_column(0)
    qapp.processEvents()
    names = column_texts(0)
    assert names == sorted(names)
    assert "▲" in registry.table.horizontalHeaderItem(0).text()

    # Returning to Tasks restores the operational default, not the last click.
    registry._sort_by_column(2)
    qapp.processEvents()
    registry._change_filter(1)
    qapp.processEvents()
    assert registry.sort_key == "state"
    assert registry.sort_ascending is True
    assert column_texts(1)[0] == "Blocked"
    window.close()
    qapp.processEvents()


def test_dashboard_toolbar_is_pinned_to_the_left_workspace_column(service, qapp):
    """The toolbar spans only the left column and centres its view title."""
    service.create_task("Toolbar task")
    window = _window(service, qapp, "toolbar_alignment.json")
    window.set_page(1)
    dashboard = window.dashboard
    dashboard.show_manage()
    qapp.processEvents()

    def column_span() -> tuple[int, int]:
        splitter = dashboard._active_splitter()
        left = dashboard.toolbar.mapTo(dashboard, QPoint(0, 0)).x()
        return left, left + splitter.sizes()[0]

    def content_span() -> tuple[int, int]:
        left = dashboard.left_toolbar.mapTo(dashboard, QPoint(0, 0)).x()
        return left, left + dashboard.left_toolbar.width()

    def title_centre() -> int:
        title = dashboard.canvas_title
        return title.mapTo(dashboard, QPoint(title.width() // 2, 0)).x()

    column_left, column_right = column_span()
    content_left, content_right = content_span()
    # The right toolbar zone matches the Inspector pane width across the gutter.
    assert dashboard.toolbar_gutter.width() == PANE_GUTTER
    assert dashboard.right_toolbar.width() == dashboard.splitter.sizes()[1]
    assert content_left == column_left
    assert abs(content_right - column_right) <= 2
    assert dashboard.tabs.mapTo(dashboard, QPoint(0, 0)).x() == column_left
    assert dashboard.canvas_title.text() == "Active Workspace"
    centre = title_centre()
    assert column_left + (column_right - column_left) * 0.2 <= centre <= (
        column_left + (column_right - column_left) * 0.8
    )
    title_left = dashboard.canvas_title.mapTo(dashboard, QPoint(0, 0)).x()
    title_right = title_left + dashboard.canvas_title.width()
    tabs_right = dashboard.tabs.mapTo(dashboard, QPoint(dashboard.tabs.width(), 0)).x()
    assert title_left >= tabs_right

    dashboard.show_flow()
    qapp.processEvents()
    assert dashboard.canvas_title.text() == "Dependency Canvas"
    assert not dashboard.flow_tools.isHidden()
    column_left, column_right = column_span()
    content_left, content_right = content_span()
    assert content_left == column_left
    assert abs(content_right - column_right) <= 2
    centre = title_centre()
    assert column_left + (column_right - column_left) * 0.2 <= centre <= (
        column_left + (column_right - column_left) * 0.8
    )
    title_left = dashboard.canvas_title.mapTo(dashboard, QPoint(0, 0)).x()
    title_right = title_left + dashboard.canvas_title.width()
    tools_left = dashboard.flow_tools.mapTo(dashboard, QPoint(0, 0)).x()
    tools_right = tools_left + dashboard.flow_tools.width()
    assert title_right <= tools_left
    visible_actions = [
        button for button in dashboard._create_buttons if not button.isHidden()
    ]
    assert abs(tools_right - content_right) <= 2
    assert all(not button.isHidden() for button in dashboard._create_buttons)
    first_left = dashboard._create_buttons[0].mapTo(dashboard, QPoint(0, 0)).x()
    assert first_left >= column_right + PANE_GUTTER - 2

    # A narrower window keeps the same left-column alignment.
    window.resize(1280, 800)
    qapp.processEvents()
    dashboard.sync_toolbar()
    qapp.processEvents()
    column_left, column_right = column_span()
    content_left, content_right = content_span()
    assert content_left == column_left
    assert abs(content_right - column_right) <= 2
    assert column_left + (column_right - column_left) * 0.2 <= title_centre() <= (
        column_left + (column_right - column_left) * 0.8
    )
    window.close()
    qapp.processEvents()

def test_flow_zoom_and_toolbar_survive_interface_rebuilds(service, qapp):
    """Theme/font rebuilds keep the Flow zoom, centre, tab, and toolbar state."""
    for name in ("Flow alpha", "Flow beta"):
        service.create_task(name)
    window = _window(service, qapp, "flow_state.json")
    window.set_page(1)
    dashboard = window.dashboard
    dashboard.show_flow()
    qapp.processEvents()
    QTest.qWait(90)
    qapp.processEvents()
    viewport = dashboard.flow.viewport
    viewport.reset_view()
    viewport.zoom_in()
    viewport.zoom_in()
    node = next(iter(dashboard.flow.scene.node_items.values()))
    viewport.centerOn(node.mapToScene(0, 0))
    qapp.processEvents()
    expected_zoom = viewport.transform().m11()
    expected_centre = viewport.mapToScene(viewport.viewport().rect().center())
    assert expected_zoom > 1.2

    window._rebuild_interface(
        index=1,
        selected=None,
        manage_tab=1,
        thread=None,
        calendar_mode="week",
        calendar_anchor=window.calendar.anchor,
        flow_pending=False,
    )
    qapp.processEvents()
    QTest.qWait(140)
    qapp.processEvents()
    dashboard = window.dashboard
    viewport = dashboard.flow.viewport
    assert dashboard.stack.currentIndex() == 1
    assert dashboard.tabs.current() == 1
    assert not dashboard.flow_tools.isHidden()
    assert dashboard.canvas_title.text() == "Dependency Canvas"
    # The create actions and canvas tools still share the aligned toolbar row.
    assert dashboard.toolbar.height() == TOOLBAR_HEIGHT
    assert abs(viewport.transform().m11() - expected_zoom) < 0.03
    centre = viewport.mapToScene(viewport.viewport().rect().center())
    assert abs(centre.x() - expected_centre.x()) < 60
    assert abs(centre.y() - expected_centre.y()) < 60

    # A font rebuild and a theme rebuild both preserve the same viewport state.
    for kind, apply_change in (
        ("font", lambda: window.change_font_scale("large")),
        (
            "theme",
            lambda: window.change_theme_value(
                2 if current_theme_value() != 2 else 4
            ),
        ),
    ):
        before = window.dashboard.flow.viewport.transform().m11()
        assert apply_change() is True
        qapp.processEvents()
        QTest.qWait(140)
        qapp.processEvents()
        dashboard = window.dashboard
        assert dashboard.stack.currentIndex() == 1, kind
        assert not dashboard.flow_tools.isHidden(), kind
        assert abs(dashboard.flow.viewport.transform().m11() - before) < 0.03, kind

    window.change_font_scale("standard")
    qapp.processEvents()
    window.close()
    qapp.processEvents()
