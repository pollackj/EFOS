"""Native QGraphics dependency canvas.

Flat nodes only: Tasks, Projects, and external Conditions. There are exactly two
edge kinds — a Condition → entity Precondition and an entity → entity
Prerequisite. Logic gates, composition, and parent-membership shapes are gone.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
)

from ..formatting import fmt_datetime
from ..logic import condition_truth_label, gate_summary
from ..models import (
    Condition,
    Edge,
    EdgeKind,
    NodeKind,
    NodeRef,
    Project,
    ProjectState,
    Task,
    TaskState,
    WorkspaceSnapshot,
)
from ..service import WorkspaceView
from .icons import draw_icon
from .theme import c, rgba, state_color

NODE_WIDTH = 252.0
NODE_HEIGHT = 124.0
NODE_GAP_X = 92.0
NODE_GAP_Y = 36.0
COLUMN_GAP = 92.0

KIND_COLOR = {
    NodeKind.TASK: "task",
    NodeKind.PROJECT: "project",
    NodeKind.CONDITION: "condition",
}
KIND_ICON = {
    NodeKind.TASK: "task",
    NodeKind.PROJECT: "project",
    NodeKind.CONDITION: "condition",
}

class LayoutStore:
    """Node coordinates are UI state, so the canonical schema stays untouched."""

    def __init__(self, path: Path, namespace: str):
        self.path = path
        self.namespace = namespace
        self._data: dict[str, dict[str, list[float]]] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, ValueError):
            self._data = {}

    def positions(self) -> dict[str, tuple[float, float]]:
        bucket = self._data.get(self.namespace, {})
        result: dict[str, tuple[float, float]] = {}
        if isinstance(bucket, dict):
            for key, value in bucket.items():
                if isinstance(value, list) and len(value) == 2:
                    try:
                        result[key] = (float(value[0]), float(value[1]))
                    except (TypeError, ValueError):
                        continue
        return result

    def set_position(self, key: str, x: float, y: float) -> None:
        self._data.setdefault(self.namespace, {})[key] = [round(x, 2), round(y, 2)]

    def set_positions(self, positions: dict[str, tuple[float, float]]) -> None:
        self._data[self.namespace] = {
            key: [round(x, 2), round(y, 2)] for key, (x, y) in positions.items()
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            return

class PortItem(QGraphicsItem):
    def __init__(self, parent: "FlowNodeItem", role: str):
        super().__init__(parent)
        self.node = parent
        self.role = role
        self.setAcceptHoverEvents(True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(4)
        self.setToolTip(
            "Drag from this port to draw a dependency."
            if role == "out"
            else "Incoming dependency target."
        )
        self._hover = False
        self.setPos(QPointF(0 if role == "in" else NODE_WIDTH, NODE_HEIGHT / 2))

    def boundingRect(self) -> QRectF:
        return QRectF(-8, -8, 16, 16)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[no-untyped-def]
        color = QColor(c(KIND_COLOR[self.node.ref.kind]))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 38))
            painter.drawEllipse(QPointF(0, 0), 8, 8)
        painter.setPen(QPen(QColor(c("canvas")), 2.0))
        painter.setBrush(color if self.role == "out" else QColor(c("canvas")))
        painter.drawEllipse(QPointF(0, 0), 4.4, 4.4)
        if self.role == "in":
            painter.setPen(QPen(color, 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(0, 0), 3.0, 3.0)

    def hoverEnterEvent(self, event):  # type: ignore[no-untyped-def]
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # type: ignore[no-untyped-def]
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

class FlowNodeItem(QGraphicsObject):
    toggle_requested = pyqtSignal(str)
    position_committed = pyqtSignal(str, float, float)
    activated = pyqtSignal(object)

    def __init__(self, ref: NodeRef, view: WorkspaceView, scene: "FlowScene"):
        super().__init__()
        self.ref = ref
        self.view = view
        self.scene_ref = scene
        self.snapshot = view.snapshot
        self.evaluation = view.evaluation
        self._hover = False
        self._connection_hover = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setZValue(10)
        self.in_port = PortItem(self, "in")
        self.out_port = PortItem(self, "out")
        self._font_kind = QFont("Segoe UI", 7)
        self._font_kind.setBold(True)
        self._font_kind.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        self._font_title = QFont("Segoe UI Variable Display", 11)
        self._font_title.setWeight(QFont.Weight.DemiBold)
        self._font_body = QFont("Segoe UI", 8)
        self._font_body.setWeight(QFont.Weight.Medium)
        self._font_chip = QFont("Segoe UI", 7)
        self._font_chip.setBold(True)
        self._font_chip.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)

    @property
    def key(self) -> str:
        return self.ref.key

    @property
    def color(self) -> str:
        return c(KIND_COLOR[self.ref.kind])

    def content_rect(self) -> QRectF:
        return QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT)

    def boundingRect(self) -> QRectF:
        return QRectF(-13, -13, NODE_WIDTH + 26, NODE_HEIGHT + 26)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT), 14, 14)
        return path

    @property
    def task(self) -> Task | None:
        return (
            self.snapshot.tasks.get(self.ref.id)
            if self.ref.kind is NodeKind.TASK
            else None
        )

    @property
    def project(self) -> Project | None:
        return (
            self.snapshot.projects.get(self.ref.id)
            if self.ref.kind is NodeKind.PROJECT
            else None
        )

    @property
    def condition(self) -> Condition | None:
        return (
            self.snapshot.conditions.get(self.ref.id)
            if self.ref.kind is NodeKind.CONDITION
            else None
        )

    def _dependency_text(self) -> str:
        pre = len(self.snapshot.preconditions_for(self.ref))
        req = len(self.snapshot.prerequisites_for(self.ref))
        return f"{pre} precondition{'s' if pre != 1 else ''} · {req} prerequisite{'s' if req != 1 else ''}"

    def _subtitle(self) -> str:
        if self.ref.kind is NodeKind.TASK and self.task:
            text = self._dependency_text()
            if self.task.has_custom_availability:
                text += " · custom availability"
            if self.task.scheduled_start:
                text += f" · {fmt_datetime(self.task.scheduled_start)}"
            return text
        if self.ref.kind is NodeKind.PROJECT and self.project:
            text = self._dependency_text()
            if self.project.resolution_conditions_unknown:
                text += " · Requirements Undefined"
            return text
        if self.condition:
            users = len(self.snapshot.attached_preconditions(self.condition.id))
            return f"External fact · {users} dependent {'target' if users == 1 else 'targets'}"
        return ""

    def _title(self) -> str:
        if self.ref.kind is NodeKind.TASK:
            return self.task.name if self.task else "Deleted task"
        if self.ref.kind is NodeKind.PROJECT:
            return self.project.name if self.project else "Deleted project"
        return self.condition.display_name if self.condition else "Deleted condition"

    def _kind_text(self) -> str:
        if self.ref.kind is NodeKind.CONDITION:
            return "CONDITION · FACT"
        return self.ref.kind.value.upper()

    def _state_text(self) -> tuple[str, str] | None:
        if self.ref.kind is NodeKind.TASK and self.task:
            return self.task.current_state.label.upper(), self.task.current_state.value
        if self.ref.kind is NodeKind.PROJECT and self.project:
            return self.project.current_state.label.upper(), self.project.current_state.value
        return None

    def _condition_value(self) -> bool | None:
        if self.ref.kind is not NodeKind.CONDITION:
            return None
        return self.evaluation.condition_value(self.ref.id)

    def _toggle_rect(self) -> QRectF:
        return QRectF(NODE_WIDTH - 78, NODE_HEIGHT - 31, 63, 21)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[no-untyped-def]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.color)
        selected = self.isSelected()
        hover = self._hover
        background = QLinearGradient(0, 0, NODE_WIDTH, NODE_HEIGHT)
        background.setColorAt(0.0, QColor(c("surface_2")))
        background.setColorAt(1.0, QColor(c("surface")))
        content = self.content_rect()

        if self._connection_hover:
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(content.adjusted(-3, -3, 3, 3), 16, 16)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 40 if c("bg").startswith("#F") else 90))
        painter.drawRoundedRect(content.translated(0, 2), 14, 14)

        painter.setBrush(QBrush(background))
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 200 if selected else 95), 1.8 if selected else 1.0))
        painter.drawRoundedRect(content, 14, 14)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(1.2, 13, 3, 31), 1.5, 1.5)

        icon_rect = QRectF(16, 15, 32, 32)
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 28))
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 55), 1))
        painter.drawRoundedRect(icon_rect, 9, 9)
        pix = QPixmap(20, 20)
        pix.fill(Qt.GlobalColor.transparent)
        icon_painter = QPainter(pix)
        icon_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_icon(icon_painter, KIND_ICON[self.ref.kind], QRectF(0, 0, 20, 20), self.color)
        icon_painter.end()
        painter.drawPixmap(22, 21, pix)

        painter.setFont(self._font_kind)
        painter.setPen(QColor(color.red(), color.green(), color.blue(), 230))
        painter.drawText(
            QRectF(56, 15, 130, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._kind_text(),
        )

        if self.ref.kind is NodeKind.CONDITION:
            value = self._condition_value()
            tone = QColor(state_color(condition_truth_label(value).lower()))
            self._draw_pill(painter, QRectF(NODE_WIDTH - 78, 13, 63, 19), condition_truth_label(value), tone)
        else:
            state = self._state_text()
            if state:
                text, value = state
                self._draw_pill(painter, QRectF(NODE_WIDTH - 118, 13, 103, 19), text, QColor(state_color(value)))

        painter.setFont(self._font_title)
        painter.setPen(QColor(c("text")))
        metrics = QFontMetricsF(self._font_title)
        title = metrics.elidedText(self._title(), Qt.TextElideMode.ElideRight, NODE_WIDTH - 32)
        painter.drawText(
            QRectF(16, 50, NODE_WIDTH - 32, 22),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        painter.setFont(self._font_body)
        painter.setPen(QColor(c("muted")))
        body_metrics = QFontMetricsF(self._font_body)
        subtitle = body_metrics.elidedText(self._subtitle(), Qt.TextElideMode.ElideRight, NODE_WIDTH - 32)
        painter.drawText(QRectF(16, 76, NODE_WIDTH - 32, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)

        painter.setPen(QPen(QColor(c("border")), 1))
        painter.drawLine(QPointF(16, NODE_HEIGHT - 31), QPointF(NODE_WIDTH - 16, NODE_HEIGHT - 31))

        painter.setFont(self._font_body)
        painter.setPen(QColor(c("faint")))
        if self.ref.kind is NodeKind.TASK and self.task:
            footer = "Recurring" if self.task.is_recurring else "One-shot"
            if self.task.scheduled_end:
                footer += " · " + self.task.scheduled_end.strftime("%H:%M")
            painter.drawText(
                QRectF(16, NODE_HEIGHT - 27, 150, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                footer,
            )
        elif self.ref.kind is NodeKind.PROJECT and self.project:
            footer = (
                "Requirements Undefined"
                if self.project.resolution_conditions_unknown
                else "Requirements defined"
            )
            painter.drawText(
                QRectF(16, NODE_HEIGHT - 27, 165, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                footer,
            )
        elif self.ref.kind is NodeKind.CONDITION and self.condition:
            painter.drawText(
                QRectF(16, NODE_HEIGHT - 27, 120, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                "Click to toggle",
            )
            value = self._condition_value()
            self._draw_pill(
                painter,
                self._toggle_rect(),
                condition_truth_label(value),
                QColor(state_color(condition_truth_label(value).lower())),
                interactive=True,
            )

    def _draw_pill(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        color: QColor,
        *,
        interactive: bool = False,
    ) -> None:
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 80), 1))
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 28 if interactive else 18))
        painter.drawRoundedRect(rect, 7, 7)
        painter.setFont(self._font_chip)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def itemChange(self, change, value):  # type: ignore[no-untyped-def]
        if change in (
            QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemVisibleHasChanged,
        ):
            if self.scene_ref is not None:
                self.scene_ref.refresh_edges()
            if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
                self.setZValue(12 if value else 10)
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):  # type: ignore[no-untyped-def]
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # type: ignore[no-untyped-def]
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton and self.ref.kind is NodeKind.CONDITION:
            if self._toggle_rect().contains(event.pos()):
                self.toggle_requested.emit(self.ref.id)
                event.accept()
                return
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        super().mouseReleaseEvent(event)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if event.button() == Qt.MouseButton.LeftButton:
            self.position_committed.emit(self.ref.key, self.pos().x(), self.pos().y())

    def mouseDoubleClickEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.ref)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def set_connection_hover(self, active: bool) -> None:
        if active != self._connection_hover:
            self._connection_hover = active
            self.update()

class EdgeItem(QGraphicsItem):
    def __init__(self, edge: Edge, source: FlowNodeItem, target: FlowNodeItem):
        super().__init__()
        self.edge = edge
        self.source = source
        self.target = target
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{edge.kind.label} · double-click to remove")
        self._hover = False
        self._path = QPainterPath()
        self._label_rect = QRectF()
        self._color_key = "teal" if edge.kind is EdgeKind.PRECONDITION else "blue"
        self.setZValue(3 if edge.kind is EdgeKind.PRECONDITION else 2)
        self._font = QFont("Segoe UI", 7)
        self._font.setBold(True)
        self._font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        self.update_path()

    def update_path(self) -> None:
        self.prepareGeometryChange()
        start = self.source.mapToScene(QPointF(NODE_WIDTH, NODE_HEIGHT / 2))
        end = self.target.mapToScene(QPointF(0, NODE_HEIGHT / 2))
        dx = end.x() - start.x()
        bend = max(72.0, min(240.0, abs(dx) * 0.52))
        if dx < 40:
            bend = max(95.0, abs(dx) * 0.2 + 95)
        c1 = QPointF(start.x() + bend, start.y())
        c2 = QPointF(end.x() - bend, end.y())
        path = QPainterPath(start)
        path.cubicTo(c1, c2, end)
        self._path = path
        point = path.pointAtPercent(0.5)
        label_text = "PRE" if self.edge.kind is EdgeKind.PRECONDITION else "PREREQ"
        metrics = QFontMetricsF(self._font)
        width = max(38.0, metrics.horizontalAdvance(label_text) + 12)
        self._label_rect = QRectF(point.x() - width / 2, point.y() - 8, width, 16)
        self._label_text = label_text
        self.update()

    def boundingRect(self) -> QRectF:
        return self._path.boundingRect().united(self._label_rect).adjusted(-10, -10, 10, 10)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(13)
        path = stroker.createStroke(self._path)
        path.addRect(self._label_rect.adjusted(-4, -4, 4, 4))
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[no-untyped-def]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = self.isSelected()
        color = QColor(c(self._color_key))
        alpha = 245 if selected or self._hover else 165
        pen = QPen(
            QColor(color.red(), color.green(), color.blue(), alpha),
            2.2 if selected or self._hover else 1.6,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        )
        if self.edge.kind is EdgeKind.PREREQUISITE:
            pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._path)

        end = self._path.pointAtPercent(1.0)
        before = self._path.pointAtPercent(0.965)
        angle = math.atan2(end.y() - before.y(), end.x() - before.x())
        size = 8.5
        arrow = QPolygonF(
            [
                end,
                end - QPointF(math.cos(angle - 0.55) * size, math.sin(angle - 0.55) * size),
                end - QPointF(math.cos(angle + 0.55) * size, math.sin(angle + 0.55) * size),
            ]
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), alpha))
        painter.drawPolygon(arrow)

        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1))
        painter.setBrush(QColor(c("surface")))
        painter.drawRoundedRect(self._label_rect, 6, 6)
        painter.setFont(self._font)
        painter.setPen(QColor(color.red(), color.green(), color.blue(), 225))
        painter.drawText(self._label_rect, Qt.AlignmentFlag.AlignCenter, self._label_text)

    def hoverEnterEvent(self, event):  # type: ignore[no-untyped-def]
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # type: ignore[no-untyped-def]
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mouseDoubleClickEvent(self, event):  # type: ignore[no-untyped-def]
        scene = self.scene()
        if isinstance(scene, FlowScene):
            scene.edge_delete_requested.emit(self.edge)
        event.accept()

class FlowScene(QGraphicsScene):
    selection_changed = pyqtSignal(object)
    connect_requested = pyqtSignal(object, object)
    edge_delete_requested = pyqtSignal(object)
    condition_toggle_requested = pyqtSignal(str)
    node_activated = pyqtSignal(object)
    layout_changed = pyqtSignal()
    status_message = pyqtSignal(str, str)

    def __init__(self, layout_store: LayoutStore, parent=None):
        super().__init__(parent)
        self.layout_store = layout_store
        self.view_data: WorkspaceView | None = None
        self.node_items: dict[str, FlowNodeItem] = {}
        self.edge_items: list[EdgeItem] = []
        self._loading = False
        self._connection_source: FlowNodeItem | None = None
        self._preview: QGraphicsPathItem | None = None
        self._hover_target: FlowNodeItem | None = None
        self.selectionChanged.connect(self._on_selection_changed)

    def load_view(self, view: WorkspaceView) -> None:
        selected_key = None
        current = self.selectedItems()
        if current and isinstance(current[0], FlowNodeItem):
            selected_key = current[0].key
        self._loading = True
        try:
            self.clear()
            self.node_items.clear()
            self.edge_items.clear()
            self.view_data = view
            self._preview = None
            self._connection_source = None
            self._hover_target = None
            snapshot = view.snapshot
            saved = self.layout_store.positions()
            refs: list[NodeRef] = []
            for task in snapshot.tasks.values():
                if task.current_state.is_active:
                    refs.append(NodeRef(NodeKind.TASK, task.id))
            for project in snapshot.projects.values():
                if project.current_state.is_open:
                    refs.append(NodeRef(NodeKind.PROJECT, project.id))
            for condition in snapshot.conditions.values():
                refs.append(NodeRef(NodeKind.CONDITION, condition.id))
            positions: dict[str, tuple[float, float]] = {}
            missing = [ref for ref in refs if ref.key not in saved]
            if refs and (not saved or len(missing) == len(refs)):
                positions = self._compute_auto_layout(view, refs)
            else:
                positions.update({ref.key: saved[ref.key] for ref in refs if ref.key in saved})
                for ref in missing:
                    positions[ref.key] = self._new_node_position(ref, positions)
            for ref in refs:
                item = FlowNodeItem(ref, view, self)
                item.setPos(QPointF(*positions.get(ref.key, (40, 40))))
                item.toggle_requested.connect(self.condition_toggle_requested)
                item.position_committed.connect(self._position_committed)
                item.activated.connect(self.node_activated)
                self.addItem(item)
                self.node_items[ref.key] = item
            for edge in snapshot.edges():
                source = self.node_items.get(edge.source.key)
                target = self.node_items.get(edge.target.key)
                if source and target:
                    edge_item = EdgeItem(edge, source, target)
                    self.addItem(edge_item)
                    self.edge_items.append(edge_item)
            bounds = self.itemsBoundingRect()
            if bounds.isEmpty():
                self.setSceneRect(-10000, -10000, 20000, 20000)
            else:
                self.setSceneRect(bounds.adjusted(-5000, -5000, 5000, 5000))
            if refs and positions:
                self.layout_store.set_positions(positions)
            if selected_key and selected_key in self.node_items:
                self.node_items[selected_key].setSelected(True)
        finally:
            self._loading = False
        self._on_selection_changed()

    def _depth(self, view: WorkspaceView, ref: NodeRef, seen: set[NodeRef] | None = None) -> int:
        if ref.kind is NodeKind.CONDITION:
            return 0
        seen = set() if seen is None else seen
        if ref in seen:
            return 0
        seen.add(ref)
        sources = view.snapshot.prerequisites_for(ref)
        if not sources:
            return 1
        return 1 + max(self._depth(view, source, seen) for source in sources)

    def _compute_auto_layout(
        self, view: WorkspaceView, refs: list[NodeRef]
    ) -> dict[str, tuple[float, float]]:
        positions: dict[str, tuple[float, float]] = {}
        snapshot = view.snapshot
        
        # Gather all edges for quick lookup of connection status
        all_edges = list(snapshot.edges())
        connected_refs = set()
        for edge in all_edges:
            connected_refs.add(edge.source)
            connected_refs.add(edge.target)

        # 1. Separate connected chain items from standalone items
        chain_refs = [ref for ref in refs if ref in connected_refs]
        standalone_tasks = [ref for ref in refs if ref.kind == NodeKind.TASK and ref not in connected_refs]
        standalone_projects = [ref for ref in refs if ref.kind == NodeKind.PROJECT and ref not in connected_refs]
        conditions = [ref for ref in refs if ref.kind == NodeKind.CONDITION] # conditions can also be handled

        # 2. Layout Connected Chains on the Left
        chain_layers: dict[int, list[NodeRef]] = {}
        for ref in chain_refs:
            chain_layers.setdefault(self._depth(view, ref), []).append(ref)

        left_margin = 40.0
        top_margin = 40.0
        step_x = NODE_WIDTH + COLUMN_GAP
        step_y = NODE_HEIGHT + NODE_GAP_Y

        max_chain_x = left_margin
        for layer in sorted(chain_layers):
            items = sorted(chain_layers[layer], key=lambda item: (item.kind.value, snapshot.get_name(item).casefold()))
            layer_x = left_margin + (layer - 1) * step_x if chain_layers else left_margin
            max_chain_x = max(max_chain_x, layer_x)
            for index, ref in enumerate(items):
                positions[ref.key] = (layer_x, top_margin + index * step_y)

        # Fallback if no chains exist
        if not chain_refs:
            max_chain_x = left_margin + step_x

        # 3. Layout Standalone Items on the Right (Two clean columns)
        standalone_start_x = max_chain_x + step_x + 80.0  # Gap separating chains from standalones
        
        # Standalone Tasks Column
        task_x = standalone_start_x
        for index, ref in enumerate(sorted(standalone_tasks, key=lambda r: snapshot.get_name(r).casefold())):
            positions[ref.key] = (task_x, top_margin + index * step_y)

        # Standalone Projects Column (placed to the right of standalone tasks)
        project_x = standalone_start_x + step_x + 40.0
        for index, ref in enumerate(sorted(standalone_projects, key=lambda r: snapshot.get_name(r).casefold())):
            positions[ref.key] = (project_x, top_margin + index * step_y)

        # Handle any leftover conditions
        cond_x = left_margin
        cond_y = top_margin + (len(chain_refs) * step_y) + 60.0
        for ref in conditions:
            if ref.key not in positions:
                positions[ref.key] = (cond_x, cond_y)
                cond_y += NODE_HEIGHT + NODE_GAP_Y

        return positions

    def _new_node_position(
        self, ref: NodeRef, existing: dict[str, tuple[float, float]]
    ) -> tuple[float, float]:
        kind_positions = [(key, pos) for key, pos in existing.items() if key.split(":", 1)[0] == ref.kind.value]
        base_x = {NodeKind.CONDITION: 40.0, NodeKind.TASK: 420.0, NodeKind.PROJECT: 800.0}[ref.kind]
        if kind_positions:
            base_x = min(pos[0] for _, pos in kind_positions)
            y = max(pos[1] for _, pos in kind_positions) + NODE_HEIGHT + NODE_GAP_Y
        else:
            y = 40.0
        return base_x, y

    def auto_layout(self) -> None:
        if not self.view_data:
            return
        positions = self._compute_auto_layout(self.view_data, [item.ref for item in self.node_items.values()])
        for key, (x, y) in positions.items():
            item = self.node_items.get(key)
            if item:
                item.setPos(QPointF(x, y))
        self.layout_store.set_positions(positions)
        self.layout_store.save()
        self.refresh_edges()
        bounds = self.itemsBoundingRect()
        self.setSceneRect(bounds.adjusted(-5000, -5000, 5000, 5000))
        self.layout_changed.emit()

    def _position_committed(self, key: str, x: float, y: float) -> None:
        self.layout_store.set_position(key, x, y)
        self.layout_store.save()
        self.refresh_edges()
        self.layout_changed.emit()

    def refresh_edges(self) -> None:
        for edge in self.edge_items:
            edge.update_path()

    def _on_selection_changed(self) -> None:
        if self._loading:
            return
        items = self.selectedItems()
        if not items:
            self.selection_changed.emit(None)
            return
        item = items[0]
        if isinstance(item, FlowNodeItem):
            self.selection_changed.emit(item.ref)
        elif isinstance(item, EdgeItem):
            self.selection_changed.emit(item.edge)
        else:
            self.selection_changed.emit(None)

    def select_ref(self, ref: NodeRef | None) -> None:
        if self._loading:
            return
        self.clearSelection()
        if ref and ref.key in self.node_items:
            self.node_items[ref.key].setSelected(True)

    def start_connection(self, node: FlowNodeItem) -> None:
        self.cancel_connection()
        self._connection_source = node
        self._preview = QGraphicsPathItem()
        self._preview.setPen(QPen(QColor(c("accent")), 2.0, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap))
        self._preview.setZValue(90)
        self.addItem(self._preview)
        self.status_message.emit(
            "Drag to a target · Condition → Task/Project = Precondition · "
            "Task/Project → Task/Project = Prerequisite",
            "info",
        )

    def update_connection(self, scene_pos: QPointF) -> None:
        if not self._preview or not self._connection_source:
            return
        start = self._connection_source.mapToScene(QPointF(NODE_WIDTH, NODE_HEIGHT / 2))
        dx = scene_pos.x() - start.x()
        bend = max(55.0, min(240.0, abs(dx) * 0.5))
        path = QPainterPath(start)
        path.cubicTo(QPointF(start.x() + bend, start.y()), QPointF(scene_pos.x() - bend, scene_pos.y()), scene_pos)
        self._preview.setPath(path)
        target = self._node_at(scene_pos)
        if target is not self._hover_target:
            if self._hover_target:
                self._hover_target.set_connection_hover(False)
            self._hover_target = target if target is not self._connection_source else None
            if self._hover_target:
                self._hover_target.set_connection_hover(True)

    def finish_connection(self, scene_pos: QPointF) -> None:
        target = self._node_at(scene_pos)
        source = self._connection_source
        self.cancel_connection()
        if source and target and target is not source:
            self.connect_requested.emit(source.ref, target.ref)
        elif source and target is source:
            self.status_message.emit("A node cannot depend on itself.", "error")
        elif source and target is None:
            self.status_message.emit("Release the connection on another node.", "info")

    def cancel_connection(self) -> None:
        if self._hover_target:
            self._hover_target.set_connection_hover(False)
            self._hover_target = None
        if self._preview:
            self.removeItem(self._preview)
            self._preview = None
        self._connection_source = None

    def _node_at(self, point: QPointF) -> FlowNodeItem | None:
        for item in self.items(point):
            cursor: QGraphicsItem | None = item
            while cursor is not None:
                if isinstance(cursor, FlowNodeItem):
                    return cursor
                cursor = cursor.parentItem()
        return None

    def remove_edge_selected(self) -> bool:
        for item in self.selectedItems():
            if isinstance(item, EdgeItem):
                self.edge_delete_requested.emit(item.edge)
                return True
        return False

    def selected_edge(self) -> Edge | None:
        for item in self.selectedItems():
            if isinstance(item, EdgeItem):
                return item.edge
        return None

    def has_content(self) -> bool:
        return bool(self.node_items)

class FlowView(QGraphicsView):
    status_message = pyqtSignal(str, str)

    def __init__(self, scene: FlowScene, parent=None):
        super().__init__(scene, parent)
        self.flow_scene = scene
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._panning = False
        self._pan_start = None
        self._connecting = False
        self._zoom = 1.0
        scene.status_message.connect(self.status_message)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # type: ignore[no-untyped-def]
        painter.fillRect(rect, QColor(c("canvas")))
        grid = 26.0
        left = math.floor(rect.left() / grid) * grid
        top = math.floor(rect.top() / grid) * grid
        base = QColor(c("text"))
        painter.setPen(Qt.PenStyle.NoPen)
        dot = QColor(base)
        dot.setAlpha(10 if c("bg").startswith("#0") else 18)
        painter.setBrush(dot)
        x = left
        while x < rect.right():
            y = top
            while y < rect.bottom():
                painter.drawEllipse(QPointF(x, y), 0.9, 0.9)
                y += grid
            x += grid
        accent = QColor(c("accent"))
        accent.setAlpha(14 if c("bg").startswith("#0") else 22)
        painter.setBrush(accent)
        x = math.floor(rect.left() / (grid * 5)) * grid * 5
        while x < rect.right():
            y = math.floor(rect.top() / (grid * 5)) * grid * 5
            while y < rect.bottom():
                painter.drawEllipse(QPointF(x, y), 1.35, 1.35)
                y += grid * 5
            x += grid * 5

    def wheelEvent(self, event):  # type: ignore[no-untyped-def]
        if event.angleDelta().y() == 0:
            return
        factor = 1.13 if event.angleDelta().y() > 0 else 1 / 1.13
        new_zoom = self._zoom * factor
        if not 0.32 <= new_zoom <= 2.2:
            return
        self._zoom = new_zoom
        self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            item = self.itemAt(event.position().toPoint())
            if item is None:
                self._panning = True
                self._pan_start = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return
            if isinstance(item, PortItem) and item.role == "out":
                self._connecting = True
                self.setDragMode(QGraphicsView.DragMode.NoDrag)
                self.flow_scene.start_connection(item.node)
                self.flow_scene.update_connection(self.mapToScene(event.position().toPoint()))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # type: ignore[no-untyped-def]
        if self._panning and self._pan_start is not None:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            # Pan smoothly using underlying scrollbar values
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            event.accept()
            return
        if self._connecting:
            self.flow_scene.update_connection(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[no-untyped-def]
        if self._panning:
            self._panning = False
            self._pan_start = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        if self._connecting and event.button() == Qt.MouseButton.LeftButton:
            self._connecting = False
            self.flow_scene.finish_connection(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):  # type: ignore[no-untyped-def]
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.flow_scene.remove_edge_selected():
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape:
            if self._connecting:
                self._connecting = False
                self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
                self.flow_scene.cancel_connection()
                event.accept()
                return
        super().keyPressEvent(event)

    def fit_content(self) -> None:
        rect = self.flow_scene.itemsBoundingRect()
        if rect.isEmpty():
            self.resetTransform()
            self._zoom = 1.0
            return
        self.fitInView(rect.adjusted(-90, -80, 110, 100), Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom = self.transform().m11()
        if self._zoom > 1.0:
            self.resetTransform()
            self._zoom = 1.0

    def reset_view(self) -> None:
        self.resetTransform()
        self._zoom = 1.0
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)

    def zoom_in(self) -> None:
        if self._zoom < 2.2:
            self._zoom *= 1.15
            self.scale(1.15, 1.15)

    def zoom_out(self) -> None:
        if self._zoom > 0.32:
            self._zoom /= 1.15
            self.scale(1 / 1.15, 1 / 1.15)
