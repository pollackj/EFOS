"""RAG Assistant: two-pane conversation and query launchpad with a shared shell."""
from __future__ import annotations
from datetime import datetime
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..errors import EFOSError
from ..models import ChatMessage, NodeRef
from ..rag_service import PRIORITY_PROMPT, ChatAnswer, RagPipeline
from ..service import EFOSService, WorkspaceView
from .dialogs import CustomQueryDialog, confirm_dialog
from .icons import icon
from .theme import c, rgba
from .widgets import (
    Badge,
    Divider,
    IconButton,
    InspectorSplitter,
    SectionLabel,
    page_margins,
    toolbar_row,
)
from ..workers import ChatWorker

DEFAULT_PRESETS = [
    ("Which Tasks are ready now?", "Which Tasks are ready now?"),
    ("List active Projects", "List active Projects"),
    ("What conditions are unknown?", "What conditions are unknown?"),
    (PRIORITY_PROMPT, PRIORITY_PROMPT),
]

DISCLAIMER = "It is NOT recommended to use the EFOS Assistant as a general chatbot."

def _clear_layout(layout) -> None:  # type: ignore[no-untyped-def]
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif child is not None:
            _clear_layout(child)

class Composer(QPlainTextEdit):
    submitted = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)

class ChatPage(QWidget):
    toast_requested = pyqtSignal(str, str)

    def __init__(self, service: EFOSService, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.pipeline = RagPipeline(service.config, service)
        self.view: WorkspaceView | None = None
        self.thread_id: str | None = None
        self.messages: list[ChatMessage] = []
        self._worker: ChatWorker | None = None
        self._worker_thread_id: str | None = None
        self._pending: QWidget | None = None
        self.cached_queries: list = []
        self._user_bubbles: list[tuple[QFrame, QLabel, str]] = []

        # Assistant uses the same shell as Dashboard and Registry: 24 px page
        # padding, one compact toolbar row, then a 20 px-gutter two-pane
        # splitter. The right "Query the workspace" pane is the fourth
        # Inspector-width surface (300–420 px) and shares the rounded card
        # styling instead of bleeding into the window edge.
        root = QVBoxLayout(self)
        page_margins(root)
        root.setSpacing(12)

        self.toolbar, toolbar_layout = toolbar_row()
        toolbar_layout.addLayout(self._thread_bar(), 1)
        root.addWidget(self.toolbar)

        splitter = InspectorSplitter()
        splitter.addWidget(self._chat_card())
        splitter.addWidget(self._launchpad())
        splitter.setSizes([900, 360])
        splitter.setStretchFactor(0, 1)
        root.addWidget(splitter, 1)
        self.splitter = splitter

    # ------------------------------------------------------------- chat card
    def _chat_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("AssistantChatCard")
        card.setProperty("card", True)
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        self.stream_scroll = QScrollArea()
        self.stream_scroll.setWidgetResizable(True)
        self.stream_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stream_host = QWidget()
        self.stream = QVBoxLayout(self.stream_host)
        self.stream.setContentsMargins(2, 4, 8, 4)
        self.stream.setSpacing(12)
        self.stream_scroll.setWidget(self.stream_host)
        layout.addWidget(self.stream_scroll, 1)
        self._render_empty_stream()
        layout.addWidget(self._composer_frame())
        self.disclaimer = QLabel(DISCLAIMER)
        self.disclaimer.setStyleSheet(f"color: {c('faint')}; font-size: 10px; padding: 0 4px;")
        self.disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.disclaimer)
        return card

    # ------------------------------------------------------------------ thread
    def _thread_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(7)
        label = QLabel("THREAD")
        label.setProperty("role", "sectionTitle")
        bar.addWidget(label)
        self.thread_combo = QComboBox()
        self.thread_combo.setMinimumWidth(260)
        self.thread_combo.currentIndexChanged.connect(self._thread_changed)
        bar.addWidget(self.thread_combo, 1)
        new_button = IconButton("plus", "New conversation", size=15)
        new_button.clicked.connect(self._new_thread)
        bar.addWidget(new_button)
        rename_button = IconButton("edit", "Rename conversation", size=14)
        rename_button.clicked.connect(self._rename_thread)
        bar.addWidget(rename_button)
        delete_button = IconButton("trash", "Delete conversation", size=14)
        delete_button.clicked.connect(self._delete_thread)
        bar.addWidget(delete_button)
        return bar

    def _composer_frame(self) -> QWidget:
        frame = QFrame()
        frame.setProperty("card", "true")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        self.composer = Composer()
        self.composer.setPlaceholderText("Ask about the workspace…")
        self.composer.setMaximumHeight(104)
        self.composer.setMinimumHeight(58)
        self.composer.submitted.connect(self._send_current)
        layout.addWidget(self.composer, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setProperty("variant", "primary")
        self.send_button.setIcon(icon("send", "#FFFFFF", 15))
        self.send_button.setFixedHeight(38)
        self.send_button.clicked.connect(self._send_current)
        layout.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        return frame

    # --------------------------------------------------------------- launchpad
    def _launchpad(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("AssistantQueryCard")
        panel.setProperty("card", True)
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # The splitter owns the width; these bounds mirror the Inspector's
        # 300–420 px interactive range.
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(420)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.launch_scroll = QScrollArea()
        self.launch_scroll.setWidgetResizable(True)
        self.launch_scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self.launch_scroll)
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)

        heading = QLabel("Query the workspace")
        heading.setProperty("role", "h2")
        layout.addWidget(heading)
        narrative = QLabel(
            "Presets and custom queries submit directly into the active conversation "
            "thread. Routing and SQL diagnostics remain visible below."
        )
        narrative.setProperty("role", "muted")
        narrative.setWordWrap(True)
        layout.addWidget(narrative)

        layout.addWidget(SectionLabel("Default queries"))
        for label, prompt in DEFAULT_PRESETS:
            button = QPushButton(label)
            button.setProperty("stateAction", True)
            button.setIcon(icon("spark", c("accent"), 14))
            button.setToolTip(prompt)
            button.clicked.connect(lambda _=False, text=prompt: self._submit(text))
            layout.addWidget(button)

        layout.addWidget(Divider())
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(SectionLabel("Custom queries"))
        header.addStretch(1)
        add = QPushButton("New query")
        add.setIcon(icon("plus", c("muted"), 13))
        add.clicked.connect(self._new_custom_query)
        header.addWidget(add)
        layout.addLayout(header)
        self.custom_host = QWidget()
        self.custom_layout = QVBoxLayout(self.custom_host)
        self.custom_layout.setContentsMargins(0, 0, 0, 0)
        self.custom_layout.setSpacing(5)
        layout.addWidget(self.custom_host)

        layout.addWidget(Divider())
        layout.addWidget(SectionLabel("Context envelope"))
        envelope = QFrame()
        envelope.setObjectName("DiagnosticPanel")
        envelope.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        envelope_layout = QVBoxLayout(envelope)
        envelope_layout.setContentsMargins(11, 9, 11, 9)
        envelope_layout.setSpacing(3)
        self.envelope_label = QLabel("")
        self.envelope_label.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
        self.envelope_label.setWordWrap(True)
        self.envelope_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        envelope_layout.addWidget(self.envelope_label)
        layout.addWidget(envelope)

        layout.addWidget(SectionLabel("Two-pass pipeline"))
        pipeline = QFrame()
        pipeline.setObjectName("DiagnosticPanel")
        pipeline.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pipeline_layout = QVBoxLayout(pipeline)
        pipeline_layout.setContentsMargins(11, 9, 11, 9)
        pipeline_layout.setSpacing(3)
        self.pipeline_label = QLabel("Idle · two model calls per message.")
        self.pipeline_label.setStyleSheet(f"color: {c('muted')}; font-size: 10px;")
        self.pipeline_label.setWordWrap(True)
        self.pipeline_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pipeline_layout.addWidget(self.pipeline_label)
        layout.addWidget(pipeline)
        layout.addStretch(1)
        self.launch_scroll.setWidget(host)
        return panel

    # --------------------------------------------------------------- lifecycle
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # Startup landing on Assistant (or returning to the page) must open the
        # conversation pinned to its newest message.
        self._schedule_scroll_to_bottom()

    def refresh(self, view: WorkspaceView) -> None:
        self.view = view
        self._refresh_threads()
        self._refresh_custom_queries()
        self._refresh_envelope()
        if self.thread_id is None and not self.messages:
            self._render_empty_stream()
        self._schedule_scroll_to_bottom()

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(1500)

    # ------------------------------------------------------------------ scroll
    def _scroll_to_bottom(self) -> None:
        bar = self.stream_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _schedule_scroll_to_bottom(self) -> None:
        """Pin the stream to its bottom after Qt has laid the bubbles out."""
        QTimer.singleShot(0, self._scroll_to_bottom)

    # ------------------------------------------------------------------ threads
    def _refresh_threads(self) -> None:
        try:
            threads = self.service.threads()
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.thread_combo.blockSignals(True)
        self.thread_combo.clear()
        for thread in threads:
            self.thread_combo.addItem(f"{thread.title}  ·  {thread.message_count} msg", thread.id)
        if not threads:
            self.thread_combo.addItem("No conversations yet — send a query", None)
        if self.thread_id:
            index = self.thread_combo.findData(self.thread_id)
            if index >= 0:
                self.thread_combo.setCurrentIndex(index)
        self.thread_combo.blockSignals(False)
        if self.thread_id is None and threads:
            self.thread_id = threads[0].id
            self._load_thread(self.thread_id, render=True)
        elif self.thread_id and self.thread_combo.findData(self.thread_id) < 0:
            self.thread_id = threads[0].id if threads else None
            if self.thread_id:
                self._load_thread(self.thread_id, render=True)
            else:
                self.messages = []
                self._render_empty_stream()

    def _thread_changed(self, index: int) -> None:
        thread_id = self.thread_combo.itemData(index)
        if not thread_id:
            return
        self.thread_id = thread_id
        self._load_thread(thread_id, render=True)

    def _load_thread(self, thread_id: str, *, render: bool = True) -> None:
        try:
            thread, messages = self.service.load_thread(thread_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.thread_id = thread_id
        self.messages = messages
        if render:
            self._render_messages()
        self._schedule_scroll_to_bottom()

    def _new_thread(self) -> None:
        try:
            thread_id = self.service.create_thread()
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.thread_id = thread_id
        self.messages = []
        self._refresh_threads()
        index = self.thread_combo.findData(thread_id)
        if index >= 0:
            self.thread_combo.blockSignals(True)
            self.thread_combo.setCurrentIndex(index)
            self.thread_combo.blockSignals(False)
        self._render_empty_stream()
        self.composer.setFocus()

    def _rename_thread(self) -> None:
        if not self.thread_id:
            return
        current = self.thread_combo.currentText().split("  ·  ")[0]
        title, ok = QInputDialog.getText(self, "Rename conversation", "Title", text=current)
        if not ok or not title.strip():
            return
        try:
            self.service.rename_thread(self.thread_id, title.strip())
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._refresh_threads()

    def _delete_thread(self) -> None:
        if not self.thread_id:
            return
        if not confirm_dialog(self, "Delete conversation?", "The stored message history will be removed."):
            return
        try:
            self.service.delete_thread(self.thread_id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self.thread_id = None
        self.messages = []
        self._refresh_threads()
        if self.thread_id is None:
            self._render_empty_stream()

    # ----------------------------------------------------------------- sending
    def _send_current(self) -> None:
        text = self.composer.toPlainText().strip()
        if not text:
            return
        self.composer.clear()
        self._submit(text)

    def _ensure_thread(self) -> bool:
        if self.thread_id:
            return True
        try:
            self.thread_id = self.service.create_thread()
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return False
        self.messages = []
        self._refresh_threads()
        self._render_messages()
        return True

    def _submit(self, text: str, *, label: str | None = None) -> None:
        text = (text or "").strip()
        if not text or self._worker is not None:
            return
        if not self._ensure_thread():
            return
        assert self.thread_id is not None and self.view is not None
        metadata = {"query_label": label} if label else {}
        try:
            self.service.append_message(self.thread_id, "user", text, metadata)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._load_thread(self.thread_id, render=True)
        self._refresh_threads()
        history = [
            {"role": message.role, "content": message.content}
            for message in self.messages[-8:]
            if message.role in ("user", "assistant")
        ]
        self._pending = self._add_pending_bubble()
        self.send_button.setEnabled(False)
        self._worker_thread_id = self.thread_id
        self._worker = ChatWorker(self.pipeline, text, self.view, history, self)
        self._worker.answer_ready.connect(self._answer_ready)
        self._worker.failed.connect(self._answer_failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _worker_finished(self) -> None:
        self.send_button.setEnabled(True)
        self._worker_thread_id = None
        if self._pending is not None:
            try:
                self._pending.deleteLater()
            except RuntimeError:
                pass
            self._pending = None
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _answer_ready(self, answer: ChatAnswer) -> None:
        target_thread = self._worker_thread_id or self.thread_id
        if target_thread is None:
            return
        try:
            self.service.append_message(
                target_thread,
                "assistant",
                answer.text,
                {
                    "route": answer.route,
                    "title": answer.title,
                    "sql": answer.sql,
                    "columns": answer.columns,
                    "rows": answer.rows[:50],
                    "elapsed_ms": answer.elapsed_ms,
                    "route_reason": answer.route_reason,
                    "model_calls": answer.model_calls,
                    "context_chars": answer.context_chars,
                },
            )
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._update_pipeline(answer)
        if self.thread_id == target_thread:
            self._load_thread(target_thread, render=True)
        self._refresh_threads()

    def _answer_failed(self, message: str) -> None:
        target_thread = self._worker_thread_id or self.thread_id
        if target_thread is None:
            self.toast_requested.emit(message, "error")
            return
        try:
            self.service.append_message(target_thread, "assistant", f"Request failed: {message}", {"route": "ERROR"})
        except EFOSError:
            pass
        self.toast_requested.emit(message, "error")
        if self.thread_id == target_thread:
            self._load_thread(target_thread, render=True)
        self._refresh_threads()
        self.pipeline_label.setText(f"Pipeline failed · {message}")

    def _update_pipeline(self, answer: ChatAnswer) -> None:
        if answer.route == "PRIORITIES":
            self.pipeline_label.setText(
                "Specialized local analyzer · read-only\n"
                f"Route: PRIORITIES ({answer.route_reason})\n"
                f"Model calls: {answer.model_calls} · elapsed {answer.elapsed_ms} ms"
            )
        elif answer.sql:
            self.pipeline_label.setText(
                "Pass 1 · intent routing\n"
                f"Route: QUERY ({answer.route_reason})\n"
                "Pass 2 · read-only SQL compiler\n"
                f"Title: {answer.title or 'Query result'}\n"
                f"Rows: {len(answer.rows)} · model calls: {answer.model_calls} · "
                f"elapsed {answer.elapsed_ms} ms\n"
                f"Context: {answer.context_chars} chars\n\n{answer.sql[:420]}"
            )
        else:
            self.pipeline_label.setText(
                "Pass 1 · intent routing\n"
                f"Route: {answer.route} ({answer.route_reason})\n"
                f"Pass 2 · grounded response · model calls: {answer.model_calls}\n"
                f"Elapsed {answer.elapsed_ms} ms · context {answer.context_chars} chars"
            )

    # ------------------------------------------------------------------ stream
    def _render_empty_stream(self) -> None:
        _clear_layout(self.stream)
        self.stream.addStretch(1)
        mark = QLabel()
        mark.setPixmap(icon("chat", c("accent"), 30).pixmap(30, 30))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stream.addWidget(mark)
        title = QLabel("Ask the workspace")
        title.setProperty("role", "h2")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stream.addWidget(title)
        hint = QLabel(
            "Use a launchpad query or write your own. The active conversation thread "
            "preserves message memory."
        )
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        self.stream.addWidget(hint)
        self.stream.addStretch(1)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_user_bubble_widths()

    def _sync_user_bubble_widths(self) -> None:
        stream_w = self.stream_scroll.viewport().width()
        if stream_w <= 100:
            return
        max_user_w = int(min(stream_w, 960) * 0.72)
        for frame, text, raw in getattr(self, "_user_bubbles", []):
            try:
                unwrapped_w = text.fontMetrics().horizontalAdvance(raw) + 32
                frame.setMaximumWidth(max_user_w)
                if unwrapped_w > max_user_w or "\n" in raw:
                    text.setWordWrap(True)
                    text.setMinimumWidth(max_user_w - 32)
                else:
                    text.setWordWrap(False)
                    text.setMinimumWidth(0)
            except RuntimeError:
                continue

    def _render_messages(self) -> None:
        self._user_bubbles = []
        _clear_layout(self.stream)
        if not self.messages:
            self._render_empty_stream()
            return
        for message in self.messages:
            self.stream.addWidget(self._bubble(message))
        self.stream.addStretch(1)
        self._sync_user_bubble_widths()
        # One deferred pass after layout, then a short follow-up so wrapped
        # bubbles that grow during the first layout still land at the bottom.
        self._schedule_scroll_to_bottom()
        QTimer.singleShot(30, self._scroll_to_bottom)

    def _bubble(self, message: ChatMessage) -> QWidget:
        assistant = message.role != "user"
        frame = QFrame()
        if assistant:
            frame.setStyleSheet("QFrame { background: transparent; border: none; }")
            frame.setMaximumWidth(960)
        else:
            frame.setObjectName("ChatBubbleUser")
            frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4 if assistant else 14, 6 if assistant else 10, 4 if assistant else 14, 10)
        layout.setSpacing(6)

        meta = message.metadata or {}
        header = QHBoxLayout()
        header.setSpacing(6)
        speaker = QLabel("ASSISTANT" if assistant else "YOU")
        speaker.setStyleSheet(f"color: {c('faint')}; font-size: 8px; font-weight: 700; letter-spacing: 1px;")
        header.addWidget(speaker)
        route = meta.get("route")
        if assistant and route:
            tone = {
                "QUERY": "accent",
                "CHAT": "unknown",
                "PRIORITIES": "ready",
                "ERROR": "false",
            }.get(str(route), "unknown")
            header.addWidget(Badge(str(route), tone))
        if assistant and meta.get("query_label"):
            header.addWidget(Badge(str(meta["query_label"])[:28], "unknown"))
        header.addStretch(1)
        layout.addLayout(header)

        text = QLabel(message.content)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setStyleSheet(
            f"color: {c('text_soft') if assistant else c('text')}; "
            "font-size: 12.5px; line-height: 1.45;"
        )

        if assistant:
            text.setWordWrap(True)
            text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        else:
            self._user_bubbles.append((frame, text, message.content))

        layout.addWidget(text)

        if assistant and meta.get("route") == "QUERY" and meta.get("sql"):
            disclosure = QLabel(f"SQL · {meta.get('title') or 'query'}")
            disclosure.setStyleSheet(f"color: {c('accent')}; font-size: 9px;")
            disclosure.setToolTip(str(meta.get("sql"))[:1200])
            layout.addWidget(disclosure)

        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        if assistant:
            holder_layout.addWidget(frame, 1)
            holder_layout.addStretch(0)
        else:
            holder_layout.addStretch(1)
            holder_layout.addWidget(frame, 0)
        return holder

    def _add_pending_bubble(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("ChatBubbleAssistant")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(13, 10, 13, 10)
        label = QLabel("Routing the request through the two-pass pipeline…")
        label.setStyleSheet(f"color: {c('muted')}; font-size: 11px;")
        layout.addWidget(label)
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(frame, 0)
        holder_layout.addStretch(1)
        self.stream.insertWidget(max(0, self.stream.count() - 1), holder)
        self._schedule_scroll_to_bottom()
        return holder

    # ----------------------------------------------------------- custom queries
    def _refresh_custom_queries(self) -> None:
        try:
            self.cached_queries = self.service.custom_queries()
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        _clear_layout(self.custom_layout)
        if not self.cached_queries:
            hint = QLabel(
                "No custom queries. Create one to save a concise label with a longer "
                "backend prompt."
            )
            hint.setProperty("role", "faint")
            hint.setWordWrap(True)
            self.custom_layout.addWidget(hint)
            return
        for index, query in enumerate(self.cached_queries):
            self.custom_layout.addWidget(self._custom_row(index, query))

    def _custom_row(self, index: int, query) -> QWidget:  # type: ignore[no-untyped-def]
        frame = QFrame()
        frame.setProperty("card", "flat")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(7, 5, 6, 5)
        layout.setSpacing(4)
        star = IconButton("star", "Unpin favorite" if query.favorite else "Pin favorite", size=13, color=c("amber") if query.favorite else c("faint"))
        star.setFixedSize(24, 24)
        star.clicked.connect(lambda _=False, q=query: self._toggle_favorite(q.id, not q.favorite))
        layout.addWidget(star)
        button = QPushButton(query.label)
        button.setProperty("variant", "ghost")
        button.setToolTip(query.prompt)
        button.setStyleSheet(
            f"QPushButton {{ text-align: left; color: {c('text_soft')}; "
            "font-size: 11px; padding: 4px 2px; border: none; }}"
            f"QPushButton:hover {{ color: {c('text')}; }}"
        )
        button.clicked.connect(lambda _=False, label=query.label, prompt=query.prompt: self._submit(prompt, label=label))
        layout.addWidget(button, 1)
        up = IconButton("chevron_left", "Move up", size=11)
        up.setFixedSize(20, 20)
        up.setEnabled(index > 0)
        up.clicked.connect(lambda _=False, i=index: self._move_custom(i, -1))
        layout.addWidget(up)
        down = IconButton("chevron_right", "Move down", size=11)
        down.setFixedSize(20, 20)
        down.setEnabled(index < len(self.cached_queries) - 1)
        down.clicked.connect(lambda _=False, i=index: self._move_custom(i, 1))
        layout.addWidget(down)
        edit = IconButton("edit", "Edit query", size=11)
        edit.setFixedSize(20, 20)
        edit.clicked.connect(lambda _=False, q=query: self._edit_custom(q))
        layout.addWidget(edit)
        delete = IconButton("trash", "Delete query", size=11)
        delete.setFixedSize(20, 20)
        delete.clicked.connect(lambda _=False, q=query: self._delete_custom(q))
        layout.addWidget(delete)
        return frame

    def _new_custom_query(self) -> None:
        dialog = CustomQueryDialog(parent=self)
        if not dialog.exec():
            return
        label, prompt = dialog.values()
        try:
            self.service.create_custom_query(label, prompt)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._refresh_custom_queries()

    def _edit_custom(self, query) -> None:  # type: ignore[no-untyped-def]
        dialog = CustomQueryDialog(query.label, query.prompt, self)
        if not dialog.exec():
            return
        label, prompt = dialog.values()
        try:
            self.service.update_custom_query(query.id, label=label, prompt=prompt, favorite=query.favorite)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._refresh_custom_queries()

    def _delete_custom(self, query) -> None:  # type: ignore[no-untyped-def]
        if not confirm_dialog(self, "Delete custom query?", f"“{query.label}” will be removed."):
            return
        try:
            self.service.delete_custom_query(query.id)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._refresh_custom_queries()

    def _toggle_favorite(self, query_id: str, favorite: bool) -> None:
        try:
            self.service.set_custom_query_favorite(query_id, favorite)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._refresh_custom_queries()

    def _move_custom(self, index: int, delta: int) -> None:
        target = index + delta
        if not 0 <= target < len(self.cached_queries):
            return
        order = [query.id for query in self.cached_queries]
        order[index], order[target] = order[target], order[index]
        try:
            self.service.reorder_custom_queries(order)
        except EFOSError as exc:
            self.toast_requested.emit(str(exc), "error")
            return
        self._refresh_custom_queries()

    # --------------------------------------------------------------- envelope
    def _refresh_envelope(self) -> None:
        if self.view is None:
            return
        metrics = self.service.context_metrics(self.view)
        self.envelope_label.setText(
            "SCHEMA public · explicit dependency model\n"
            f"tasks {metrics['tasks']} · projects {metrics['projects']} · "
            f"conditions {metrics['conditions']}\n"
            f"preconditions {metrics['preconditions']} · "
            f"prerequisites {metrics['prerequisites']} · links {metrics['links']}\n"
            f"ready tasks {metrics['ready_tasks']}\n"
            f"context envelope {metrics['context_chars']:,} chars"
        )
