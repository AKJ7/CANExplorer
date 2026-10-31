"""A QML item painting the bit-transition raster of the selected identifier (see core/reverse/raster.py).

The raster is built by RasterController into a core `Raster`; this item turns it into an indexed image (one pixel
per bit per column, four colours for zero/one × stable/changed) and scales it to the item, with byte separators
and labels drawn over it. The image is rebuilt only when the raster changes, not on every repaint."""

from typing import Optional

from PyQt6.QtCore import QObject, QRectF, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt6.QtQml import qmlRegisterType
from PyQt6.QtQuick import QQuickPaintedItem

MARGIN_LEFT = 34  # Room for the byte labels
MARGIN_RIGHT = 8
MARGIN_TOP = 6
MARGIN_BOTTOM = 18  # Room for the frame-count axis


class BitRaster(QQuickPaintedItem):
    """Paints a RasterController's raster: bits down, frames across, byte 0 at the top."""

    controller_changed = pyqtSignal()
    style_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._controller = None
        self._colors = [QColor('#24263d'), QColor('#3498db'), QColor('#6b4a1a'), QColor('#f39c12')]
        self._grid_color = QColor('#2e3150')
        self._text_color = QColor('#8b8fa8')
        self._image: Optional[QImage] = None
        self._image_for = None  # The Raster object the cached image was built from

    @pyqtProperty(QObject, notify=controller_changed)
    def controller(self):
        return self._controller

    @controller.setter
    def controller(self, controller):
        if controller is self._controller:
            return
        if self._controller is not None:
            self._controller.changed.disconnect(self._on_changed)
        self._controller = controller
        if controller is not None:
            controller.changed.connect(self._on_changed)
        self.controller_changed.emit()
        self._on_changed()

    def _on_changed(self) -> None:
        self._image = None  # Rebuild on the next paint
        self.update()

    @pyqtProperty("QVariantList", notify=style_changed)
    def colors(self):
        return self._colors

    @colors.setter
    def colors(self, colors):
        # Expected order: zero, one, zero-changed, one-changed
        if len(colors) == 4:
            self._colors = [QColor(color) for color in colors]
            self._image = None
            self.style_changed.emit()
            self.update()

    @pyqtProperty(QColor, notify=style_changed)
    def gridColor(self):
        return self._grid_color

    @gridColor.setter
    def gridColor(self, color):
        self._grid_color = QColor(color)
        self.style_changed.emit()
        self.update()

    @pyqtProperty(QColor, notify=style_changed)
    def textColor(self):
        return self._text_color

    @textColor.setter
    def textColor(self, color):
        self._text_color = QColor(color)
        self.style_changed.emit()
        self.update()

    def _raster(self):
        return self._controller.raster() if self._controller is not None else None

    def _build_image(self, raster) -> Optional[QImage]:
        if raster is None or raster.columns == 0 or raster.rows == 0:
            return None
        columns, rows = raster.columns, raster.rows
        stride = (columns + 3) & ~3  # Indexed8 scanlines are 4-byte aligned
        buffer = bytearray(stride * rows)
        cells = raster.cells
        for row in range(rows):
            start = row * stride
            buffer[start : start + columns] = cells[row * columns : (row + 1) * columns]
        image = QImage(bytes(buffer), columns, rows, stride, QImage.Format.Format_Indexed8)
        image.setColorCount(4)
        for index, color in enumerate(self._colors):
            image.setColor(index, color.rgb())
        return image.copy()  # Own the pixels, so `buffer` can be released

    def paint(self, painter: QPainter) -> None:
        width, height = self.width(), self.height()
        plot = QRectF(MARGIN_LEFT, MARGIN_TOP, width - MARGIN_LEFT - MARGIN_RIGHT, height - MARGIN_TOP - MARGIN_BOTTOM)
        if plot.width() < 10 or plot.height() < 10:
            return
        raster = self._raster()
        if self._image is None or self._image_for is not raster:
            self._image = self._build_image(raster)
            self._image_for = raster
        font = QFont(painter.font())
        font.setPixelSize(10)
        painter.setFont(font)
        if self._image is None:
            painter.setPen(self._text_color)
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No frames for this identifier yet")
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)  # Crisp cells
        painter.drawImage(plot, self._image)

        # Byte separators and labels: each byte is 8 rows
        bytes_count = raster.length
        row_height = plot.height() / raster.rows
        painter.setPen(QPen(self._grid_color, 1))
        for byte in range(bytes_count + 1):
            y = plot.top() + byte * 8 * row_height
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
        painter.setPen(self._text_color)
        for byte in range(bytes_count):
            y = plot.top() + (byte + 0.5) * 8 * row_height
            painter.drawText(
                QRectF(0, y - 7, MARGIN_LEFT - 5, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(byte),
            )
        # Frame-count axis
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 2, plot.width(), MARGIN_BOTTOM - 2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            "frame 0",
        )
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 2, plot.width(), MARGIN_BOTTOM - 2),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
            f"{raster.frames:,}",
        )


_registered = False


def register_bit_raster() -> None:
    global _registered
    if not _registered:
        qmlRegisterType(BitRaster, "CanExplorer.Plots", 1, 0, "BitRaster")
        _registered = True
