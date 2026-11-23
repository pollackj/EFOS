"""Small, dependency-free line icon set drawn with QPainter."""
from __future__ import annotations
import math
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF)
from .theme import c

def icon(name: str, color: str | None = None, size: int = 18) -> QIcon:
    color = color or c("muted")
    result = QIcon()
    for mode, tint in (
        (QIcon.Mode.Normal, color),
        (QIcon.Mode.Selected, c("accent")),
        (QIcon.Mode.Active, color),
        (QIcon.Mode.Disabled, c("faint")),
    ):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_icon(painter, name, QRectF(0, 0, size, size), tint)
        painter.end()
        result.addPixmap(pix, mode, QIcon.State.Off)
    return result

def draw_icon(painter: QPainter, name: str, rect: QRectF, color: str) -> None:
    painter.save()
    scale = rect.width() / 24.0
    painter.translate(rect.topLeft())
    painter.scale(scale, scale)
    pen = QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    name = name.replace("nav_", "")

    if name == "grid":
        for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)): painter.drawRoundedRect(QRectF(x, y, 7, 7), 2, 2)
    elif name == "flow":
        painter.drawEllipse(QPointF(5, 12), 2.4, 2.4)
        painter.drawEllipse(QPointF(19, 6), 2.4, 2.4)
        painter.drawEllipse(QPointF(19, 18), 2.4, 2.4)
        path = QPainterPath(QPointF(7.2, 11.2))
        path.cubicTo(11, 10.2, 12.6, 6.8, 16.6, 6.2)
        painter.drawPath(path)
        path2 = QPainterPath(QPointF(7.2, 12.8))
        path2.cubicTo(11, 13.8, 12.6, 17.2, 16.6, 17.8)
        painter.drawPath(path2)
    elif name == "registry":
        for y in (6, 12, 18):
            painter.drawEllipse(QPointF(5, y), 1.2, 1.2)
            painter.drawLine(QPointF(9, y), QPointF(20, y))
    elif name == "calendar":
        painter.drawRoundedRect(QRectF(3.5, 5.5, 17, 15), 2.5, 2.5)
        painter.drawLine(QPointF(3.5, 10), QPointF(20.5, 10))
        painter.drawLine(QPointF(8, 3.5), QPointF(8, 7))
        painter.drawLine(QPointF(16, 3.5), QPointF(16, 7))
        painter.drawEllipse(QPointF(8.5, 14.5), 1.1, 1.1)
        painter.drawEllipse(QPointF(12, 14.5), 1.1, 1.1)
        painter.drawEllipse(QPointF(15.5, 14.5), 1.1, 1.1)
    elif name == "chat":
        path = QPainterPath()
        path.addRoundedRect(QRectF(3.5, 4.5, 17, 13), 3, 3)
        painter.drawPath(path)
        painter.drawLine(QPointF(8, 17.5), QPointF(7, 21))
        painter.drawLine(QPointF(7, 21), QPointF(12.5, 17.5))
        painter.drawLine(QPointF(8, 9.5), QPointF(16, 9.5))
        painter.drawLine(QPointF(8, 13), QPointF(13, 13))
    elif name == "plus":
        painter.drawLine(QPointF(12, 5), QPointF(12, 19))
        painter.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "minus": painter.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "refresh":
        rect_arc = QRectF(4.5, 4.5, 15, 15)
        painter.drawArc(rect_arc, 30 * 16, 280 * 16)
        path = QPainterPath(QPointF(18.4, 4.2))
        path.lineTo(20.5, 8.6)
        path.lineTo(16.0, 8.4)
        painter.drawPath(path)
    elif name == "sync":
        rect_arc = QRectF(4.5, 4.5, 15, 15)
        painter.drawArc(rect_arc, 40 * 16, 280 * 16)
        path = QPainterPath(QPointF(18.4, 4.2))
        path.lineTo(20.5, 8.6)
        path.lineTo(16.0, 8.4)
        painter.drawPath(path)
    elif name == "close":
        painter.drawLine(QPointF(6, 6), QPointF(18, 18))
        painter.drawLine(QPointF(18, 6), QPointF(6, 18))
    elif name == "chevron_down": painter.drawPolyline(QPolygonF([QPointF(6, 9), QPointF(12, 15), QPointF(18, 9)]))
    elif name == "chevron_right": painter.drawPolyline(QPolygonF([QPointF(9, 6), QPointF(15, 12), QPointF(9, 18)]))
    elif name == "chevron_left": painter.drawPolyline(QPolygonF([QPointF(15, 6), QPointF(9, 12), QPointF(15, 18)]))
    elif name == "search":
        painter.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        painter.drawLine(QPointF(15, 15), QPointF(20, 20))
    elif name == "check": painter.drawPolyline(QPolygonF([QPointF(5, 12.5), QPointF(10, 17), QPointF(19.5, 7)]))
    elif name == "alert":
        path = QPainterPath(QPointF(12, 3.5))
        path.lineTo(22, 20.5)
        path.lineTo(2, 20.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(12, 9), QPointF(12, 14))
        painter.drawEllipse(QPointF(12, 17.3), 0.8, 0.8)
    elif name == "trash":
        painter.drawLine(QPointF(4.5, 7), QPointF(19.5, 7))
        painter.drawLine(QPointF(9, 7), QPointF(9.5, 4))
        painter.drawLine(QPointF(9.5, 4), QPointF(14.5, 4))
        painter.drawLine(QPointF(14.5, 4), QPointF(15, 7))
        path = QPainterPath(QPointF(6.5, 7))
        path.lineTo(7.5, 20)
        path.lineTo(16.5, 20)
        path.lineTo(17.5, 7)
        painter.drawPath(path)
        painter.drawLine(QPointF(10.5, 10.5), QPointF(10.7, 16.5))
        painter.drawLine(QPointF(13.5, 10.5), QPointF(13.3, 16.5))
    elif name == "edit":
        path = QPainterPath(QPointF(5, 19))
        path.lineTo(6.3, 14)
        path.lineTo(15.5, 4.8)
        path.lineTo(19.2, 8.5)
        path.lineTo(10, 17.7)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(13.7, 6.6), QPointF(17.4, 10.3))
    elif name == "clock":
        painter.drawEllipse(QPointF(12, 12), 8.3, 8.3)
        painter.drawLine(QPointF(12, 7), QPointF(12, 12.3))
        painter.drawLine(QPointF(12, 12.3), QPointF(16, 14.2))
    elif name == "link":
        path = QPainterPath(QPointF(9.7, 14.3))
        path.cubicTo(7.2, 11.8, 9.2, 8.2, 12.5, 8.2)
        path.lineTo(15, 8.2)
        painter.drawPath(path)
        path2 = QPainterPath(QPointF(14.3, 9.7))
        path2.cubicTo(16.8, 12.2, 14.8, 15.8, 11.5, 15.8)
        path2.lineTo(9, 15.8)
        painter.drawPath(path2)
        painter.drawLine(QPointF(8, 12), QPointF(16, 12))
    elif name == "task":
        painter.drawRoundedRect(QRectF(4, 4, 16, 16), 3, 3)
        painter.drawPolyline(QPolygonF([QPointF(8, 12), QPointF(11, 15), QPointF(16.5, 9)]))
    elif name == "project":
        path = QPainterPath(QPointF(3.5, 7))
        path.lineTo(9, 7)
        path.lineTo(11, 9.5)
        path.lineTo(20.5, 9.5)
        path.lineTo(20.5, 19)
        path.lineTo(3.5, 19)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "condition":
        painter.drawEllipse(QPointF(12, 12), 6.2, 6.2)
        painter.drawLine(QPointF(12, 2.8), QPointF(12, 5.8))
        painter.drawLine(QPointF(12, 18.2), QPointF(12, 21.2))
        painter.drawLine(QPointF(2.8, 12), QPointF(5.8, 12))
        painter.drawLine(QPointF(18.2, 12), QPointF(21.2, 12))
    elif name == "fit":
        painter.drawLine(QPointF(4, 9), QPointF(4, 4))
        painter.drawLine(QPointF(4, 4), QPointF(9, 4))
        painter.drawLine(QPointF(15, 4), QPointF(20, 4))
        painter.drawLine(QPointF(20, 4), QPointF(20, 9))
        painter.drawLine(QPointF(20, 15), QPointF(20, 20))
        painter.drawLine(QPointF(20, 20), QPointF(15, 20))
        painter.drawLine(QPointF(9, 20), QPointF(4, 20))
        painter.drawLine(QPointF(4, 20), QPointF(4, 15))
    elif name == "layout":
        painter.drawRoundedRect(QRectF(3.5, 4.5, 6, 6), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(14.5, 4.5, 6, 6), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(3.5, 14.5, 6, 6), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(14.5, 14.5, 6, 6), 1.5, 1.5)
    elif name == "align_start":
        painter.drawLine(QPointF(3.5, 3.5), QPointF(3.5, 20.5))
        painter.drawRoundedRect(QRectF(7.0, 5.0, 13.5, 5.5), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(7.0, 13.5, 9.0, 5.5), 1.5, 1.5)
    elif name == "align_finish":
        painter.drawLine(QPointF(20.5, 3.5), QPointF(20.5, 20.5))
        painter.drawRoundedRect(QRectF(3.5, 5.0, 13.5, 5.5), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(8.0, 13.5, 9.0, 5.5), 1.5, 1.5)
    elif name == "zoom_in":
        painter.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        painter.drawLine(QPointF(15, 15), QPointF(20, 20))
        painter.drawLine(QPointF(7.5, 10.5), QPointF(13.5, 10.5))
        painter.drawLine(QPointF(10.5, 7.5), QPointF(10.5, 13.5))
    elif name == "zoom_out":
        painter.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        painter.drawLine(QPointF(15, 15), QPointF(20, 20))
        painter.drawLine(QPointF(7.5, 10.5), QPointF(13.5, 10.5))
    elif name == "send":
        path = QPainterPath(QPointF(3.5, 11.5))
        path.lineTo(20.5, 4)
        path.lineTo(14, 20.5)
        path.lineTo(11, 13.8)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(11, 13.8), QPointF(20.5, 4))
    elif name == "database":
        painter.drawEllipse(QRectF(4, 3.5, 16, 5.5))
        painter.drawLine(QPointF(4, 6.2), QPointF(4, 17.8))
        painter.drawLine(QPointF(20, 6.2), QPointF(20, 17.8))
        painter.drawArc(QRectF(4, 14.5, 16, 5.5), 180 * 16, 180 * 16)
        painter.drawArc(QRectF(4, 9, 16, 5.5), 180 * 16, 180 * 16)
    elif name == "spark":
        path = QPainterPath(QPointF(12, 2.5))
        path.lineTo(14.2, 9.2)
        path.lineTo(21, 12)
        path.lineTo(14.2, 14.8)
        path.lineTo(12, 21.5)
        path.lineTo(9.8, 14.8)
        path.lineTo(3, 12)
        path.lineTo(9.8, 9.2)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "sensor":
        painter.drawEllipse(QPointF(12, 12), 2.2, 2.2)
        painter.drawArc(QRectF(6.5, 6.5, 11, 11), 30 * 16, 120 * 16)
        painter.drawArc(QRectF(6.5, 6.5, 11, 11), 210 * 16, 120 * 16)
        painter.drawArc(QRectF(3, 3, 18, 18), 30 * 16, 120 * 16)
        painter.drawArc(QRectF(3, 3, 18, 18), 210 * 16, 120 * 16)
    elif name == "google":
        painter.drawEllipse(QPointF(12, 12), 8, 8)
        painter.drawLine(QPointF(12, 4), QPointF(12, 12))
        painter.drawLine(QPointF(12, 12), QPointF(19, 12))
    elif name == "arrow_right":
        painter.drawLine(QPointF(4, 12), QPointF(20, 12))
        painter.drawPolyline(QPolygonF([QPointF(14, 6), QPointF(20, 12), QPointF(14, 18)]))
    elif name == "more":
        for x in (6, 12, 18): painter.drawEllipse(QPointF(x, 12), 1.2, 1.2)
    elif name == "lock":
        painter.drawRoundedRect(QRectF(5, 10, 14, 10), 2, 2)
        painter.drawArc(QRectF(8, 4, 8, 12), 0, 180 * 16)
    elif name == "unlock":
        painter.drawRoundedRect(QRectF(5, 10, 14, 10), 2, 2)
        painter.drawArc(QRectF(8, 4, 8, 12), 20 * 16, 160 * 16)
    elif name == "sun":
        painter.drawEllipse(QPointF(12, 12), 4.2, 4.2)
        for angle in range(0, 360, 45):
            radians = math.radians(angle)
            painter.drawLine(
                QPointF(12 + math.cos(radians) * 7.4, 12 + math.sin(radians) * 7.4),
                QPointF(12 + math.cos(radians) * 10.2, 12 + math.sin(radians) * 10.2),
            )
    elif name == "moon":
        path = QPainterPath()
        path.addEllipse(QRectF(5.5, 3.5, 15, 15))
        painter.drawPath(path)
        painter.setBrush(QColor(c("surface")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(15.5, 8.5), 6.2, 6.2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
    elif name == "star":
        points = []
        for index in range(10):
            radius = 9.0 if index % 2 == 0 else 3.9
            angle = math.radians(-90 + index * 36)
            points.append(QPointF(12 + math.cos(angle) * radius, 12 + math.sin(angle) * radius))
        painter.drawPolygon(QPolygonF(points))
    elif name == "pin":
        path = QPainterPath(QPointF(8, 3.5))
        path.lineTo(16, 3.5)
        path.lineTo(14.4, 9)
        path.lineTo(18, 12)
        path.lineTo(6, 12)
        path.lineTo(9.6, 9)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(12, 12), QPointF(12, 20.5))
    elif name == "note":
        painter.drawRoundedRect(QRectF(4.5, 3.5, 15, 17), 2.5, 2.5)
        painter.drawLine(QPointF(8, 8), QPointF(16, 8))
        painter.drawLine(QPointF(8, 12), QPointF(16, 12))
        painter.drawLine(QPointF(8, 16), QPointF(13, 16))
    elif name == "external":
        painter.drawRoundedRect(QRectF(4.5, 7.5, 11, 12), 2, 2)
        painter.drawLine(QPointF(12, 4), QPointF(20, 4))
        painter.drawLine(QPointF(20, 4), QPointF(20, 12))
        painter.drawLine(QPointF(20, 4), QPointF(11, 13))
    elif name == "list":
        for y in (5, 10, 15, 20):
            painter.drawLine(QPointF(8, y), QPointF(20, y))
            painter.drawEllipse(QPointF(4.5, y), 1.0, 1.0)
    elif name == "settings":
        painter.drawEllipse(QPointF(12, 12), 3.2, 3.2)
        painter.drawEllipse(QPointF(12, 12), 8.2, 8.2)
        for angle in range(0, 360, 45):
            radians = math.radians(angle)
            painter.drawLine(
                QPointF(12 + math.cos(radians) * 8.2, 12 + math.sin(radians) * 8.2),
                QPointF(12 + math.cos(radians) * 10.4, 12 + math.sin(radians) * 10.4),
            )
    elif name == "image":
        painter.drawRoundedRect(QRectF(3.5, 5, 17, 14), 2, 2)
        painter.drawEllipse(QPointF(8.5, 9.5), 1.5, 1.5)
        painter.drawPolyline(QPolygonF([QPointF(5, 17), QPointF(10, 12), QPointF(14, 16), QPointF(17, 13), QPointF(20, 16)]))
    else: painter.drawEllipse(QPointF(12, 12), 7, 7)
    painter.restore()

def brand_pixmap(size: int = 64) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64.0, size / 64.0)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(c("surface_3")))
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 17, 17)
    pen = QPen(QColor(c("accent")), 5.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(18, 43), QPointF(31, 31))
    painter.drawLine(QPointF(31, 31), QPointF(45, 21))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(c("teal")))
    painter.drawEllipse(QPointF(18, 43), 6.5, 6.5)
    painter.setBrush(QColor(c("accent")))
    painter.drawEllipse(QPointF(31, 31), 6.5, 6.5)
    painter.setBrush(QColor(c("amber")))
    painter.drawEllipse(QPointF(45, 21), 6.5, 6.5)
    painter.end()
    return pix

def dotted_line_pixmap(width: int = 120, height: int = 12) -> QPixmap:
    """Colored dotted-line graphic used in the EFOS brand lockup."""
    pix = QPixmap(width, height)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    colors = [c("accent"), c("teal"), c("amber"), c("accent_2")]
    painter.setPen(Qt.PenStyle.NoPen)
    for index in range(14):
        x = 4 + index * ((width - 8) / 14)
        painter.setBrush(QColor(colors[index % len(colors)]))
        painter.drawEllipse(QPointF(x, height / 2), 1.6, 1.6)
    painter.end()
    return pix
