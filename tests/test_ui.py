"""Headless UI mechanics tests: navigation, sorting, notes, assistant, calendar."""
from __future__ import annotations

from datetime import datetime, timedelta

from PyQt6.QtCore import QTime, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLabel, QPushButton

from efos_app.models import NodeKind, NodeRef, TaskState
from efos_app.rag_service import PRIORITY_PROMPT
from efos_app.ui.dialogs import AvailabilityDialog
from efos_app.ui.graph import LayoutStore
from efos_app.ui.main_window import MainWindow
from efos_app.ui.theme import current_theme

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
    for index in range(5):
        window.set_page(index)
        qapp.processEvents()
        assert window.stack.currentIndex() == index
    window.scheduler.pulse()
    qapp.processEvents()
    assert not window.grab().isNull()
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

    registry._sort_by_column(1)  # state ascending
    qapp.processEvents()
    states = [
        registry.table.item(row, 1).text() for row in range(registry.table.rowCount())
    ]
    assert states == sorted(states)
    assert states == [
        "Blocked",
        "Completed",
        "Deferred",
        "Ready",
        "Ready",
    ]
    assert "▲" in registry.table.horizontalHeaderItem(1).text()

    registry._sort_by_column(1)  # state descending
    qapp.processEvents()
    assert registry.table.horizontalHeaderItem(1).text().endswith("▼")
    assert registry.table.item(0, 1).text() == "Ready"

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

def test_theme_toggle_is_persisted_and_rebuilds_light_theme(service, qapp):
    window = _window(service, qapp, "theme.json")
    window._sync_timer.stop()
    window.dashboard.show_flow()
    before = current_theme()
    window.toggle_theme()
    qapp.processEvents()
    assert window.dashboard.stack.currentIndex() == 1
    after = current_theme()
    assert after != before
    from efos_app.config import load_ui_state

    assert load_ui_state().get("theme") == after
    assert window.centralWidget() is not None
    assert not window.grab().isNull()
    window._sync_timer.stop()
    for attr in ("_sync_worker", "_connect_worker"):
        worker = getattr(window.calendar, attr, None)
        if worker is not None:
            worker.wait(3000)
            setattr(window.calendar, attr, None)
    qapp.processEvents()
    window.toggle_theme()
    qapp.processEvents()
    assert current_theme() == before
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
