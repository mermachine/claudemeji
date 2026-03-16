"""
window.py - transparent frameless always-on-top overlay window

The main trick: WA_TranslucentBackground + FramelessWindowHint + WindowStaysOnTopHint.
Click-through is handled per-platform via setMask on the sprite region only,
so the non-sprite area passes mouse events to whatever's behind.
"""

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QPoint, QRect
from PyQt6.QtGui import QPainter, QRegion
import sys


class MascotWindow(QWidget):
    def __init__(self):
        super().__init__()

        self._sprite_rect = QRect(0, 0, 128, 128)  # updated by sprite.py
        self._drag_offset = QPoint()
        self._dragging = False

        self._setup_window()

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool  # keeps it off the taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        # start in bottom-right area - will be overridden by saved position later
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen:
            self.move(screen.width() - 200, screen.height() - 200)

        self.resize(128, 128)
        self.show()

    def update_sprite_rect(self, rect: QRect):
        """Called by the sprite renderer when the active region changes (e.g. size)."""
        self._sprite_rect = rect
        self.resize(rect.size())
        # restrict mouse hit-testing to just the sprite so clicks pass through elsewhere
        self.setMask(QRegion(rect))

    def paintEvent(self, event):
        # actual drawing is handled by sprite.py via a child label or direct painter calls
        # this base just ensures transparency
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)

    # --- drag to reposition ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.pos()

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.move(self.pos() + event.pos() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def contextMenuEvent(self, event):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("Quit", self._quit)
        menu.exec(event.globalPos())

    def _quit(self):
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()
