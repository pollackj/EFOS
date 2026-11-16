"""Main window shell, navigation, Suggested Focus, status, and coordination."""
from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QByteArray, QEvent, QTimer, Qt
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    APP_DISPLAY_NAME,
    APP_VERSION,
    AppConfig,
    load_ui_state,
    save_ui_state,
)
from ..errors import EFOSError
from ..models import ConnectionStatus, NodeKind, NodeRef, PriorityItem
from ..scheduler import BackgroundScheduler
from ..service import EFOSService, WorkspaceView
from ..workers import ConnectionProbeWorker
from .calendar_page import CalendarPage
from .chat_page import ChatPage
from .dashboard import DashboardPage, OverviewPage
from .dialogs import CreateEntityDialog, RecurrenceDialog
from .flow_page import FlowWorkspace
from .graph import LayoutStore
from .icons import brand_pixmap, icon
from .registry_page import RegistryPage
from .theme import (
    DEFAULT_THEME,
    THEME_DARK,
    THEME_LIGHT,
    apply_theme,
    c,
    current_theme,
    enable_native_title_bar,
    rgba,
    set_theme,
)
from .widgets import (
    Badge,
    BrandHeader,
    NavButton,
    StatusDot,
    SuggestedFocusCard,
    Toast,
)

PAGE_META = [
    ("OVERVIEW", "Workspace snapshot", "Your current endeavors at a glance"),
    ("WORKSPACE", "Dashboard", "Manage metadata and map flat dependency chains."),
    ("REGISTRY", "Canonical record", "Every task, project, and condition in one sortable grid."),
    ("REASONING", "AI assistant", "Grounded two-pass routing over the live workspace."),
    ("SCHEDULE", "Calendar", "Local tasks and synchronized Google Calendar events."),
]


class TitleBar(QFrame):
    """Frameless title bar with native move and resize support."""

    def __init__(self, owner: "MainWindow"):
        super().__init__(owner)
        self.owner = owner
        self.setObjectName("TitleBar")
        self.setFixedHeight(38)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(brand_pixmap(18))
        mark.setFixedSize(18, 18)
        mark.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(mark)
        name = QLabel("EFOS")
        name.setStyleSheet(
            f"color: {c('text')}; font-size: 11px; font-weight: 700; letter-spacing: 1px;"
        )
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(name)
        separator = QFrame()
        separator.setFixedSize(1, 13)
        separator.setStyleSheet(f"background: {c('border')};")
        separator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(separator)
        product = QLabel(APP_DISPLAY_NAME)
        product.setStyleSheet(f"color: {c('muted')}; font-size: 9px;")
        product.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(product)
        layout.addStretch(1)
        self.theme_button = QPushButton()
        self.theme_button.setProperty("variant", "icon")
        self.theme_button.setFixedSize(30, 26)
        self.theme_button.setIcon(
            icon("sun" if current_theme() == THEME_DARK else "moon", c("muted"), 14)
        )
        self.theme_button.setToolTip(
            "Switch to light theme"
            if current_theme() == THEME_DARK
            else "Switch to dark theme"
        )
        self.theme_button.clicked.connect(owner.toggle_theme)
        layout.addWidget(self.theme_button)
        self.min_button = self._control("—", "Minimize")
        self.max_button = self._control("□", "Maximize")
        self.close_button = self._control("✕", "Close", close=True)
        self.min_button.clicked.connect(owner.showMinimized)
        self.max_button.clicked.connect(self.toggle_maximize)
        self.close_button.clicked.connect(owner.close)
        layout.addWidget(self.min_button)
        layout.addWidget(self.max_button)
        layout.addWidget(self.close_button)

    def _control(self, text: str, tooltip: str, *, close: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("windowControl", True)
        if close:
            button.setProperty("closeButton", True)
        button.setFixedSize(42 if close else 34, 27)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        return button

    def toggle_maximize(self) -> None:
        if self.owner.isMaximized():
            self.owner.showNormal()
        else:
            self.owner.showMaximized()
        self.sync_state()

    def sync_state(self) -> None:
        self.max_button.setText("❐" if self.owner.isMaximized() else "□")
        self.max_button.setToolTip("Restore" if self.owner.isMaximized() else "Maximize")

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.owner.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ResizeHandle(QWidget):
    def __init__(self, owner: "MainWindow", edges: Qt.Edge, cursor: Qt.CursorShape):
        super().__init__(owner.centralWidget())
        self.owner = owner
        self.edges = edges
        self.setCursor(cursor)
        self.setStyleSheet("background: transparent;")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.owner.windowHandle()
            if handle is not None:
                handle.startSystemResize(self.edges)
                event.accept()
                return
        super().mousePressEvent(event)

class MainWindow(QMainWindow):
    def __init__(self, service: EFOSService, config: AppConfig, layout_store: LayoutStore):
        super().__init__()
        self.service = service
        self.config = config
        self.layout_store = layout_store
        self.view: WorkspaceView | None = None
        self.pages: list[QWidget] = []
        self.nav_buttons: list[NavButton] = []
        self._last_page = 0
        self._db_connected: bool | None = None
        self._probe_worker: ConnectionProbeWorker | None = None
        self.ui_state = load_ui_state()
        saved_theme = str(self.ui_state.get("theme", DEFAULT_THEME))
        set_theme(saved_theme)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, saved_theme)
        self._build_window()
        self._build_shortcuts()
        self._restore_window_state()
        self.toast = Toast(self)
        self.refresh_all(initial=True)
        self.scheduler = BackgroundScheduler(service, parent=self)
        self.scheduler.pulse_completed.connect(self._pulse_completed)
        self.scheduler.pulse_failed.connect(
            lambda message: self.show_toast(f"Recurrence pulse failed: {message}", "error")
        )
        self.scheduler.start()
        self._probe_timer = QTimer(self)
        self._probe_timer.timeout.connect(self._probe_connection)
        self._probe_timer.start(15_000)
        self._initial_probe_timer = QTimer(self)
        self._initial_probe_timer.setSingleShot(True)
        self._initial_probe_timer.timeout.connect(self._probe_connection)
        self._initial_probe_timer.start(1200)
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._periodic_sync)
        self._sync_timer.start(15 * 60_000)
        self._initial_sync_timer = QTimer(self)
        self._initial_sync_timer.setSingleShot(True)
        self._initial_sync_timer.timeout.connect(self._periodic_sync)
        self._initial_sync_timer.start(4000)
        # Target the main window container wrapper and its immediate borders to prevent layout masking
        self.setStyleSheet("""
            QMainWindow {
                border: 1px solid #3a3b3c;
            }
            #central_widget, QFrame#main_container, .MainWindowClass {
                border: 1px solid #3a3b3c;
                border-radius: 4px;
            }
        """ + self.styleSheet())

    # ------------------------------------------------------------------ window
    def _restore_window_state(self) -> None:
        geometry = self.ui_state.get("geometry")
        if geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(str(geometry).encode()))
            except (ValueError, TypeError):
                pass
        state = self.ui_state.get("window_state")
        if state:
            try:
                self.restoreState(QByteArray.fromBase64(str(state).encode()))
            except (ValueError, TypeError):
                pass

    def _build_window(self) -> None:
        self.setWindowTitle(f"EFOS · {APP_DISPLAY_NAME}")
        self.setWindowIcon(QIcon(brand_pixmap(128)))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(1500, 950)
        self.setMinimumSize(1180, 760)
        self.setStyleSheet(f"QMainWindow {{ background: {c('bg')}; }}")

        # 1. Initialize the core PyQt interface panel widget
        central = QWidget()
        central.setObjectName("Root")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        central.setStyleSheet(
            f"QWidget#Root {{ background: {c('bg')}; }}"
        )
        
        # 2. Build the top-level outer border frame layer
        border_frame = QFrame()
        border_frame.setObjectName("GlobalBorderChassis")
        border_frame.setStyleSheet("""
            #GlobalBorderChassis {
                border: 1px solid #3a3b3c;
                border-radius: 4px;
                background-color: #1a1b1c;
            }
            QWidget#Root {
                border-radius: 3px;
            }
        """)
        
        # 3. Securely pack the core app layout inside the border envelope
        frame_layout = QVBoxLayout(border_frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        frame_layout.addWidget(central)
                # Force the interior panels to clip natively inside the chassis boundaries
        central.setGraphicsEffect(None) # Clear any default layer overlays

        # 4. Make the border frame container the absolute face of the application window
        self.setCentralWidget(border_frame)
        
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(self)
        outer.addWidget(self.title_bar)

        body = QWidget()
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._sidebar())
        shell = QWidget()
        shell.setObjectName("ContentShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(self._header())
        self.stack = QStackedWidget()
        shell_layout.addWidget(self.stack, 1)
        shell_layout.addWidget(self._status_bar())
        root.addWidget(shell, 1)
        outer.addWidget(body, 1)
        
        # 5. Connect the core widget references so theme shifting works flawlessly
        self.central_widget = border_frame  
        self._build_pages()
        self.resize_handles = [
            ResizeHandle(self, Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
            ResizeHandle(self, Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            ResizeHandle(self, Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
            ResizeHandle(self, Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            ResizeHandle(
                self,
                Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                Qt.CursorShape.SizeFDiagCursor,
            ),
            ResizeHandle(
                self,
                Qt.Edge.RightEdge | Qt.Edge.BottomEdge,
                Qt.CursorShape.SizeFDiagCursor,
            ),
            ResizeHandle(
                self,
                Qt.Edge.RightEdge | Qt.Edge.TopEdge,
                Qt.CursorShape.SizeBDiagCursor,
            ),
            ResizeHandle(
                self,
                Qt.Edge.LeftEdge | Qt.Edge.BottomEdge,
                Qt.CursorShape.SizeBDiagCursor,
            ),
        ]

    def _build_pages(self) -> None:
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            widget.deleteLater()
        self.overview = OverviewPage()
        self.dashboard = DashboardPage(self.service, self.layout_store)
        self.registry = RegistryPage(self.service)
        self.chat = ChatPage(self.service)
        self.calendar = CalendarPage(self.service)
        self.pages = [
            self.overview,
            self.dashboard,
            self.registry,
            self.chat,
            self.calendar,
        ]
        for page in self.pages:
            self.stack.addWidget(page)
        self.overview.create_requested.connect(self.create_entity)
        self.overview.open_node.connect(self.open_node)
        self.overview.open_dashboard.connect(lambda: self.set_page(1))
        self.overview.open_registry.connect(lambda: self.set_page(2))
        self.overview.open_chat.connect(lambda: self.set_page(3))
        self.dashboard.create_requested.connect(self.create_entity)
        self.dashboard.mutated.connect(self.on_mutated)
        self.dashboard.toast_requested.connect(self.show_toast)
        self.registry.create_requested.connect(self.create_entity)
        self.registry.open_flow.connect(self.open_in_flow)
        self.registry.mutated.connect(self.on_mutated)
        self.registry.toast_requested.connect(self.show_toast)
        self.calendar.open_node.connect(self.open_node)
        self.calendar.toast_requested.connect(self.show_toast)
        self.chat.toast_requested.connect(self.show_toast)
        if self.stack.currentIndex() < 0 or self.stack.currentIndex() >= len(self.pages):
            self.set_page(0)

    def _sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(238)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 18, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(BrandHeader())
        layout.addSpacing(14)
        work = QLabel("WORKSPACE")
        work.setProperty("role", "eyebrow")
        work.setContentsMargins(10, 0, 0, 3)
        layout.addWidget(work)

        group = QButtonGroup(self)
        group.setExclusive(True)
        self.nav_buttons = []
        definitions = [
            ("Overview", "grid"),
            ("Dashboard", "flow"),
            ("Registry", "registry"),
            ("Assistant", "chat"),
            ("Calendar", "calendar"),
        ]
        for index, (label, icon_name) in enumerate(definitions):
            button = NavButton(label, icon_name)
            button.setToolTip(f"{label}  ·  Ctrl+{index + 1}")
            button.clicked.connect(lambda _=False, i=index: self.set_page(i))
            group.addButton(button, index)
            layout.addWidget(button)
            self.nav_buttons.append(button)
        layout.addSpacing(12)
        self.focus_card = SuggestedFocusCard()
        self.focus_card.clicked.connect(self.open_recommendation)
        layout.addWidget(self.focus_card)
        layout.addStretch(1)
        version = QLabel(f"EFOS {APP_VERSION} · explicit dependency model")
        version.setStyleSheet(
            f"color: {c('faint')}; font-size: 8px; letter-spacing: 0.3px;"
        )
        version.setWordWrap(True)
        layout.addWidget(version)
        return sidebar

    def _header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(80)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(26, 12, 22, 12)
        layout.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.eyebrow = QLabel("OVERVIEW")
        self.eyebrow.setProperty("role", "eyebrow")
        titles.addWidget(self.eyebrow)
        self.page_title = QLabel("Workspace snapshot")
        self.page_title.setStyleSheet(
            f"color: {c('text')}; font-family: 'Segoe UI Variable Display'; "
            "font-size: 22px; font-weight: 650;"
        )
        titles.addWidget(self.page_title)
        self.page_subtitle = QLabel("Your current endeavors at a glance")
        self.page_subtitle.setStyleSheet(f"color: {c('muted')}; font-size: 11px;")
        titles.addWidget(self.page_subtitle)
        layout.addLayout(titles, 1)
        self.header_links = Badge("0 LINKS", "accent")
        layout.addWidget(self.header_links)
        self.clock = QLabel()
        self.clock.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.clock.setStyleSheet(f"color: {c('text_soft')}; font-size: 11px;")
        layout.addWidget(self.clock)
        recompute = QPushButton("Recompute")
        recompute.setIcon(icon("refresh", c("muted"), 15))
        recompute.setToolTip("Re-evaluate every dependency gate now (Ctrl+R)")
        recompute.clicked.connect(self.recompute)
        layout.addWidget(recompute)
        self.new_button = QPushButton("New")
        self.new_button.setProperty("variant", "primary")
        self.new_button.setIcon(icon("plus", "#FFFFFF", 15))
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(self._new_menu)
        layout.addWidget(self.new_button)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(30000)
        self._tick_clock()
        return header

    def _status_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(30)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(8)
        self.db_dot = StatusDot(c("green"), 8)
        layout.addWidget(self.db_dot)
        self.db_label = QLabel("Postgres")
        self.db_label.setStyleSheet(
            f"color: {c('text_soft')}; font-size: 10px; font-weight: 600;"
        )
        layout.addWidget(self.db_label)
        self.db_meta = QLabel(self.config.db_display)
        self.db_meta.setStyleSheet(f"color: {c('muted')}; font-size: 9px;")
        layout.addWidget(self.db_meta)
        separator = QFrame()
        separator.setFixedSize(1, 12)
        separator.setStyleSheet(f"background: {c('border')};")
        layout.addWidget(separator)
        self.footer_counts = QLabel("0 tasks · 0 projects · 0 conditions")
        self.footer_counts.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
        layout.addWidget(self.footer_counts)
        layout.addStretch(1)
        self.footer_status = QLabel("Scheduler · 60s pulse · schema verified")
        self.footer_status.setStyleSheet(f"color: {c('faint')}; font-size: 9px;")
        layout.addWidget(self.footer_status)
        guard = QLabel("READ-ONLY SQL GUARD")
        guard.setStyleSheet(
            f"color: {c('accent')}; font-size: 8px; font-weight: 700; "
            "letter-spacing: 0.8px;"
        )
        layout.addWidget(guard)
        return bar

    def _build_shortcuts(self) -> None:
        for index in range(5):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.set_page(i))
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(
            lambda: self.create_entity(NodeKind.TASK)
        )
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self.recompute)
        QShortcut(QKeySequence("Ctrl+K"), self).activated.connect(
            lambda: (self.set_page(3), self.chat.composer.setFocus())
        )

    def _tick_clock(self) -> None:
        now = datetime.now()
        self.clock.setText(now.strftime("%a %b %d\n%H:%M"))

    # ------------------------------------------------------------- updating
    def refresh_all(self, *, initial: bool = False, message: str | None = None) -> None:
        try:
            view = self.service.load_view()
        except EFOSError as exc:
            self._apply_connection(
                ConnectionStatus(
                    False,
                    self.config.db_host,
                    self.config.db_port,
                    self.config.db_name,
                    str(exc),
                )
            )
            self.show_toast(str(exc), "error")
            return
        self.view = view
        for page in self.pages:
            try:
                page.refresh(view)
            except Exception as exc:  # pragma: no cover - defensive
                self.show_toast(f"{type(exc).__name__}: {exc}", "error")
        snapshot = view.snapshot
        self.header_links.setText(f"{snapshot.count_edges()} LINKS")
        self.footer_counts.setText(
            f"{len(snapshot.tasks)} tasks · {len(snapshot.projects)} projects · "
            f"{len(snapshot.conditions)} conditions"
        )
        self.focus_card.set_recommendation(view.recommendation)
        self._apply_connection(
            ConnectionStatus(
                True,
                self.config.db_host,
                self.config.db_port,
                self.config.db_name,
            )
        )
        if message:
            self.show_toast(message, "success")
        elif view.changes and not initial:
            labels = ", ".join(change.name for change in view.changes[:2])
            suffix = "…" if len(view.changes) > 2 else ""
            self.show_toast(f"Dependency update · {labels}{suffix}", "info")

    def _apply_connection(self, status: ConnectionStatus) -> None:
        self.db_dot.set_color(c("green") if status.connected else c("red"))
        self.db_meta.setText(status.endpoint if status.connected else "not connected")
        self.db_meta.setStyleSheet(
            f"color: {c('muted') if status.connected else c('red')}; font-size: 9px;"
        )
        if status.connected:
            self.footer_status.setText("Scheduler · 60s pulse · schema verified")
        else:
            self.footer_status.setText("Database connection lost · cached view retained")
        if self._db_connected is True and not status.connected:
            self.show_toast("PostgreSQL disconnected. Showing the last cached view.", "error")
        self._db_connected = status.connected

    def on_mutated(self, message: str) -> None:
        self.refresh_all(message=message)

    def recompute(self) -> None:
        try:
            view = self.service.refresh()
        except EFOSError as exc:
            self.show_toast(str(exc), "error")
            return
        self.refresh_all()
        if view.changes:
            self.show_toast(f"{len(view.changes)} derived transitions applied", "success")
        else:
            self.show_toast("All dependency barriers are stable", "info")

    # ------------------------------------------------------------ connection
    def _probe_connection(self) -> None:
        if self._probe_worker is not None and self._probe_worker.isRunning():
            return
        self._probe_worker = ConnectionProbeWorker(self.service, self)
        self._probe_worker.result_ready.connect(self._apply_connection)
        self._probe_worker.finished.connect(self._probe_finished)
        self._probe_worker.start()

    def _probe_finished(self) -> None:
        if self._probe_worker is not None:
            self._probe_worker.deleteLater()
            self._probe_worker = None

    # -------------------------------------------------------------- scheduler
    def _pulse_completed(self, results, timestamp, changes) -> None:
        fired = [item for item in results if item[2] == "reset"]
        blocked = [item for item in results if item[2] == "blocked by dependencies"]
        if not results and not changes:
            self.footer_status.setText(
                f"Scheduler pulse · {timestamp.strftime('%H:%M:%S')} · stable"
            )
            return
        self.footer_status.setText(
            f"Scheduler pulse · {timestamp.strftime('%H:%M:%S')} · "
            f"{len(fired)} reset · {len(changes)} transition"
        )
        if fired:
            self.refresh_all(
                message=f"Recurrence reset · {', '.join(item[1] for item in fired)}"
            )
        elif changes:
            names = ", ".join(change.name for change in changes[:2])
            suffix = "…" if len(changes) > 2 else ""
            self.refresh_all(message=f"Derived transition · {names}{suffix}")
        elif blocked:
            self.show_toast(
                "Recurring reset blocked by unresolved dependencies (logged).", "info"
            )

    # ---------------------------------------------------------------- calendar
    def _periodic_sync(self) -> None:
        try:
            state = self.service.calendar_sync_state()
        except Exception:
            return
        if state.connected:
            self.calendar.sync_now()

    # ------------------------------------------------------------ navigation
    def flush_notes(self) -> bool:
        for page in (self.dashboard, self.registry):
            try:
                if not page.flush_notes():
                    return False
            except Exception:
                return False
        return True

    def set_page(self, index: int) -> None:
        if not 0 <= index < len(self.pages):
            return
        if index != self.stack.currentIndex() and not self.flush_notes():
            return
        self.stack.setCurrentIndex(index)
        self._last_page = index
        for position, button in enumerate(self.nav_buttons):
            button.setChecked(position == index)
        eyebrow, title, subtitle = PAGE_META[index]
        self.eyebrow.setText(eyebrow)
        self.page_title.setText(title)
        self.page_subtitle.setText(subtitle)
        if index == 4 and self.view:
            self.calendar.refresh(self.view)
        if index == 3 and self.view:
            self.chat.refresh(self.view)

    def open_node(self, ref: NodeRef) -> None:
        if not self.flush_notes():
            return
        self.set_page(1)
        self.dashboard.select_manage(ref)

    def open_in_flow(self, ref: NodeRef) -> None:
        if not self.flush_notes():
            return
        self.set_page(1)
        visible = self.dashboard.select_flow(ref)
        if not visible:
            self.show_toast(
                "Completed and deferred tasks are hidden from the canvas; they remain "
                "visible in the Registry.",
                "info",
            )

    def open_recommendation(self) -> None:
        recommendation = self.view.recommendation if self.view else None
        if recommendation is None:
            return
        self.open_node(recommendation.ref)

    # -------------------------------------------------------------- actions
    def _new_menu(self) -> None:
        menu = QMenu(self)
        action_task = menu.addAction(icon("task", c("accent"), 15), "New task")
        action_project = menu.addAction(icon("project", c("accent"), 15), "New project")
        action_condition = menu.addAction(
            icon("condition", c("accent"), 15), "New condition"
        )
        chosen = menu.exec(
            self.new_button.mapToGlobal(self.new_button.rect().bottomLeft())
        )
        if chosen is action_task:
            self.create_entity(NodeKind.TASK)
        elif chosen is action_project:
            self.create_entity(NodeKind.PROJECT)
        elif chosen is action_condition:
            self.create_entity(NodeKind.CONDITION)

    def create_entity(self, kind: NodeKind) -> None:
        if not self.flush_notes():
            return
        dialog = CreateEntityDialog(kind, self)
        if not dialog.exec():
            return
        values = dialog.values()
        try:
            if kind is NodeKind.TASK:
                new_id = self.service.create_task(values["name"])
                if values.get("is_recurring"):
                    recurrence = RecurrenceDialog(None, self)
                    if recurrence.exec() and not recurrence.values()["removed"]:
                        rule = recurrence.values()
                        self.service.save_rule(
                            new_id,
                            frequency=rule["frequency"],
                            interval=rule["interval"],
                            by_day=rule["by_day"],
                            by_month_day=rule["by_month_day"],
                            occurrence_count=rule["occurrence_count"],
                            until_date=rule["until_date"],
                            reset_hour=rule["reset_hour"],
                            reset_minute=rule["reset_minute"],
                        )
            elif kind is NodeKind.PROJECT:
                new_id = self.service.create_project(values["name"])
            else:
                new_id = self.service.create_condition(
                    values["name"],
                    evaluation_state=values.get("evaluation_state"),
                )
        except EFOSError as exc:
            self.show_toast(str(exc), "error")
            return
        self.refresh_all()
        self.open_node(NodeRef(kind, new_id))
        self.show_toast(f"{kind.label} created", "success")

    def show_toast(self, message: str, level: str = "success") -> None:
        self.toast.notify(message, level)

    # ------------------------------------------------------------------ theme
    def toggle_theme(self) -> None:
        chat_worker = getattr(self.chat, "_worker", None)
        sync_worker = getattr(self.calendar, "_sync_worker", None)
        connect_worker = getattr(self.calendar, "_connect_worker", None)
        if any(
            worker is not None and worker.isRunning()
            for worker in (chat_worker, sync_worker, connect_worker)
        ):
            self.show_toast(
                "Wait for the active assistant or calendar request to finish before "
                "switching themes.",
                "info",
            )
            return
        if not self.flush_notes():
            return
        target = THEME_LIGHT if current_theme() == THEME_DARK else THEME_DARK
        index = self.stack.currentIndex()
        selected = self.dashboard.selected_ref
        manage_tab = self.dashboard.stack.currentIndex()
        thread = self.chat.thread_id
        calendar_mode = self.calendar.mode
        calendar_anchor = self.calendar.anchor
        save_ui_state({"theme": target})
        self.ui_state = load_ui_state()
        set_theme(target)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, target)
        self._rebuild_interface(
            index=index,
            selected=selected,
            manage_tab=manage_tab,
            thread=thread,
            calendar_mode=calendar_mode,
            calendar_anchor=calendar_anchor,
        )

    def _rebuild_interface(
        self,
        *,
        index: int,
        selected: NodeRef | None,
        manage_tab: int,
        thread: str | None,
        calendar_mode: str,
        calendar_anchor: datetime,
    ) -> None:
        old_central = self.takeCentralWidget()
        if hasattr(self, "_clock_timer") and self._clock_timer is not None:
            self._clock_timer.stop()
            self._clock_timer.deleteLater()
        for page in getattr(self, "pages", []):
            try:
                page.shutdown()
            except Exception:
                pass
        if old_central is not None:
            old_central.deleteLater()
        self._build_window()
        self.title_bar.sync_state()
        self._position_resize_handles()
        if hasattr(self, "toast") and self.toast is not None:
            self.toast.deleteLater()
        self.toast = Toast(self)
        self.refresh_all()
        if selected is not None and self.view is not None:
            self.dashboard.select_manage(selected)
        self.dashboard.stack.setCurrentIndex(manage_tab)
        self.dashboard.tabs.select(manage_tab, emit=False)
        if thread and self.view is not None:
            self.chat.thread_id = thread
            self.chat.refresh(self.view)
            try:
                self.chat._load_thread(thread, render=True)
            except EFOSError:
                pass
        mode_index = {"day": 0, "week": 1, "month": 2, "schedule": 3}.get(
            calendar_mode, 1
        )
        self.calendar.mode = calendar_mode
        self.calendar.anchor = calendar_anchor
        self.calendar.view_switch.select(mode_index, emit=False)
        self.set_page(min(index, len(self.pages) - 1))
        self.calendar.reload()
        QTimer.singleShot(30, lambda: enable_native_title_bar(self))

    # ----------------------------------------------------------------- events
    def _position_resize_handles(self) -> None:
        if not hasattr(self, "resize_handles") or not hasattr(self, "central_widget"):
            return
        visible = not self.isMaximized()
        rect = self.central_widget.rect()
        thickness = 6
        corner = 11
        left, right, top, bottom = self.resize_handles[:4]
        left.setGeometry(0, corner, thickness, max(0, rect.height() - corner * 2))
        right.setGeometry(
            rect.width() - thickness,
            corner,
            thickness,
            max(0, rect.height() - corner * 2),
        )
        top.setGeometry(corner, 0, max(0, rect.width() - corner * 2), thickness)
        bottom.setGeometry(
            corner,
            rect.height() - thickness,
            max(0, rect.width() - corner * 2),
            thickness,
        )
        self.resize_handles[4].setGeometry(0, 0, corner, corner)
        self.resize_handles[5].setGeometry(
            rect.width() - corner, rect.height() - corner, corner, corner
        )
        self.resize_handles[6].setGeometry(rect.width() - corner, 0, corner, corner)
        self.resize_handles[7].setGeometry(0, rect.height() - corner, corner, corner)
        for handle in self.resize_handles:
            handle.setVisible(visible)
            if visible:
                handle.raise_()

    def resizeEvent(self, event):  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if hasattr(self, "toast"):
            self.toast.reposition()
        self._position_resize_handles()

    def showEvent(self, event):  # type: ignore[no-untyped-def]
        super().showEvent(event)
        if hasattr(self, "title_bar"):
            self.title_bar.sync_state()
        self._position_resize_handles()
        QTimer.singleShot(30, lambda: enable_native_title_bar(self))

    def changeEvent(self, event):  # type: ignore[no-untyped-def]
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.sync_state()
            self._position_resize_handles()

    def closeEvent(self, event):  # type: ignore[no-untyped-def]
        try:
            self.flush_notes()
        except Exception:
            pass
        self.layout_store.save()
        for page in getattr(self, "pages", []):
            try:
                page.shutdown()
            except Exception:
                pass
        for timer_name in (
            "_initial_probe_timer",
            "_initial_sync_timer",
            "_probe_timer",
            "_sync_timer",
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        probe = getattr(self, "_probe_worker", None)
        if probe is not None and probe.isRunning():
            probe.wait(1500)
        if hasattr(self, "scheduler"):
            self.scheduler.stop()
        save_ui_state(
            {
                "theme": current_theme(),
                "geometry": bytes(self.saveGeometry().toBase64()).decode(),
                "window_state": bytes(self.saveState().toBase64()).decode(),
            }
        )
        try:
            self.service.close()
        except Exception:
            pass
        super().closeEvent(event)
