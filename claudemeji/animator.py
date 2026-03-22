"""
animator.py - visual sprite animation tool for claudemeji

A drag-and-drop animation editor with:
  - Large sprite palette for browsing all pack frames
  - Drag-and-drop timeline for sequencing frames
  - Big preview with transport controls (play/pause/step/speed)
  - Onion skin toggle (ghost previous frame)
  - Full config.toml load/save compatibility

Run: python3 -m claudemeji.animator
"""

from __future__ import annotations
import os
import sys

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QCheckBox, QComboBox, QScrollArea, QFileDialog, QSplitter,
    QFrame, QGridLayout, QSpinBox, QMessageBox, QSlider,
    QToolButton, QSizePolicy, QAbstractItemView,
)
from PyQt6.QtCore import (
    Qt, QSize, QTimer, QMimeData, pyqtSignal, QPoint, QRect,
)
from PyQt6.QtGui import (
    QPixmap, QIcon, QPainter, QDrag, QColor, QPen, QFont, QPalette,
)

from claudemeji.state import ACTIONS
from claudemeji.sprite import ActionDef


# ── constants ────────────────────────────────────────────────────────────────

PALETTE_THUMB_SIZE = 96      # sprite palette thumbnails
TIMELINE_FRAME_SIZE = 80     # timeline frame thumbnails
PREVIEW_SIZE = 256           # animation preview

MIME_FRAME = "application/x-claudemeji-frame"

# colors
C_BG = "#0e0e1a"
C_PANEL = "#151525"
C_SURFACE = "#1c1c32"
C_BORDER = "#2a2a4a"
C_ACCENT = "#6366f1"
C_ACCENT_HOVER = "#818cf8"
C_ACCENT_DIM = "#3730a3"
C_TEXT = "#e2e8f0"
C_TEXT_DIM = "#94a3b8"
C_TEXT_MUTED = "#64748b"
C_SUCCESS = "#34d399"
C_WARNING = "#fbbf24"
C_DANGER = "#f87171"
C_ONION = "#6366f180"


# ── action metadata (shared with editor.py) ──────────────────────────────────

ACTION_DESCRIPTIONS: dict[str, str] = {
    "stand":         "Neutral standing pose (no-animation default)",
    "walk":          "Walking (flipped programmatically for direction)",
    "run":           "Fast walk (restless locomotion variant)",
    "sprint":        "Full dash at high restlessness",
    "crawl":         "Deliberate belly crawl movement",
    "trip":          "Stumble/pratfall during run (one-shot)",
    "jump":          "Impulse jump toward a target",
    "fall":          "Falling / thrown",
    "climb":         "Wall climbing (flipped for right wall)",
    "ceiling":       "Ceiling crawl (flipped for direction)",
    "hang":          "Hanging/dangling on a wall (stationary)",
    "hang_ceiling":  "Hanging from ceiling (stationary)",
    "sit_idle":      "Sitting idle animation (from idle pool)",
    "plan":          "Planning mode — EnterPlanMode tool",
    "think":         "Thinking between tool calls",
    "read":          "Reading — Read/Grep/Glob tools",
    "type":          "Typing — Edit/Write tools",
    "bash":          "Running a command — Bash tool",
    "wait":          "Waiting on long process",
    "react_good":    "Success reaction",
    "react_bad":     "Error reaction",
    "drag":          "Being picked up / dragged",
    "subagent":      "Parent split animation — spawning a subagent (Agent/Task tools)",
    "spawned":       "Subagent entrance — jump up from parent, fall down",
    "window_push":        "Pushing/dragging a window (restlessness >= 2)",
    "window_throw":       "Throwing a window — arc + minimize (restlessness >= 4)",
    "window_carry":       "Walking with a grabbed window",
    "window_carry_perch": "Perched on window corner before grabbing",
    "window_carry_run":   "Running with a grabbed window",
    "window_carry_throw": "Winding up to throw a carried window",
    "window_carry_cheer": "Celebration after throwing a carried window",
}

ACTION_POSTURES: dict[str, list[str]] = {
    "sit_idle":   ["sitting"],
    "stand":      ["sitting"],
    "plan":       ["sitting"],
    "think":      ["sitting"],
    "read":       ["sitting"],
    "wait":       ["sitting"],
    "react_good": ["sitting"],
    "window_push": ["hanging", "climbing"],
}

# Context variants available per action (drag context = restlessness tier)
ACTION_CONTEXTS: dict[str, list[str]] = {
    "drag": ["r0", "r1", "r2", "r3", "r4"],
}


# ── draggable sprite palette ─────────────────────────────────────────────────

class SpritePalette(QWidget):
    """
    Grid of all sprites available to drag into the timeline.
    Sprites come from a loaded pack folder and/or individually added images.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img_dir = ""
        # maps filename → absolute path (allows loose images outside pack dir)
        self._sprites: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        header = QLabel("Sprite Palette")
        header.setStyleSheet(f"color: {C_TEXT}; font-size: 13px; font-weight: bold;")
        header_row.addWidget(header, 1)

        btn_add = QPushButton("+ Add images…")
        btn_add.setToolTip("Add individual PNG files to the palette")
        btn_add.setStyleSheet(f"""
            QPushButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 4px 10px; border-radius: 4px; color: {C_TEXT_DIM};
                font-size: 11px;
            }}
            QPushButton:hover {{ background: {C_ACCENT_DIM}; border-color: {C_ACCENT}; color: {C_TEXT}; }}
        """)
        btn_add.clicked.connect(self._add_images)
        header_row.addWidget(btn_add)
        layout.addLayout(header_row)

        hint = QLabel("drag sprites into the timeline  \u00b7  double-click to add")
        hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ border: 1px solid {C_BORDER}; border-radius: 6px; background: {C_SURFACE}; }}
            QScrollBar:vertical {{ width: 8px; background: {C_SURFACE}; }}
            QScrollBar::handle:vertical {{ background: {C_BORDER}; border-radius: 4px; min-height: 30px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

        self._grid_widget = QWidget()
        self._grid_widget.setStyleSheet(f"background: {C_SURFACE};")
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(6)
        self._grid.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(self._grid_widget)
        layout.addWidget(scroll, 1)

    # signal so AnimatorWindow can connect a "frame double-clicked" handler
    frame_double_clicked = pyqtSignal(str)  # emits filename

    def load(self, img_dir: str):
        """Load all PNGs from a pack directory, replacing the current pack sprites."""
        self._img_dir = img_dir
        # keep any loose images, replace pack images
        self._sprites = {
            k: v for k, v in self._sprites.items()
            if not v.startswith(img_dir)
        }
        pngs = sorted(
            f for f in os.listdir(img_dir)
            if f.lower().endswith(".png") and f != "icon.png"
        )
        for fname in pngs:
            self._sprites[fname] = os.path.join(img_dir, fname)
        self._rebuild()

    def _add_images(self):
        """Open a file dialog to add individual PNGs to the palette."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add sprite images",
            self._img_dir or os.path.expanduser("~"),
            "PNG images (*.png)"
        )
        for path in paths:
            fname = os.path.basename(path)
            # if a file with this name already exists, use the full path as key
            key = fname if fname not in self._sprites else path
            self._sprites[key] = path
        if paths:
            self._rebuild()

    def _rebuild(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        cols = 4
        for i, (fname, path) in enumerate(sorted(self._sprites.items())):
            thumb = DraggableThumb(fname, path)
            thumb.double_clicked.connect(self.frame_double_clicked.emit)
            self._grid.addWidget(thumb, i // cols, i % cols)


class DraggableThumb(QWidget):
    """A single sprite thumbnail that can be dragged into the timeline."""

    double_clicked = pyqtSignal(str)  # emits filename key

    def __init__(self, filename: str, img_path: str, parent=None):
        """
        filename: the key used in frame lists (basename for pack sprites,
                  full path for loose images added individually)
        img_path: absolute path to the PNG file
        """
        super().__init__(parent)
        self.filename = filename
        self._pixmap = QPixmap(img_path)
        self._hover = False

        self.setFixedSize(PALETTE_THUMB_SIZE + 8, PALETTE_THUMB_SIZE + 22)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip(filename)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # background
        bg = QColor(C_ACCENT_DIM if self._hover else C_SURFACE)
        p.setBrush(bg)
        p.setPen(QPen(QColor(C_ACCENT if self._hover else C_BORDER), 1))
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 6, 6)

        # sprite
        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                PALETTE_THUMB_SIZE, PALETTE_THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = 4 + (PALETTE_THUMB_SIZE - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)

        # label
        p.setPen(QColor(C_TEXT_DIM))
        p.setFont(QFont("monospace", 9))
        label = self.filename.replace(".png", "")
        p.drawText(QRect(0, PALETTE_THUMB_SIZE + 4, self.width(), 18),
                    Qt.AlignmentFlag.AlignHCenter, label)

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._press_pos = event.pos()

    def mouseReleaseEvent(self, event):
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self.filename)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            if (event.pos() - getattr(self, '_press_pos', event.pos())).manhattanLength() < 6:
                return
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(MIME_FRAME, self.filename.encode())
            drag.setMimeData(mime)

            # drag preview
            if not self._pixmap.isNull():
                preview = self._pixmap.scaled(
                    64, 64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                drag.setPixmap(preview)
                drag.setHotSpot(QPoint(preview.width() // 2, preview.height() // 2))

            drag.exec(Qt.DropAction.CopyAction)
            self.setCursor(Qt.CursorShape.OpenHandCursor)


# ── timeline ─────────────────────────────────────────────────────────────────

class TimelineWidget(QWidget):
    """
    Horizontal strip of frames in the current animation sequence.
    Supports drag-and-drop reordering and insertion from the palette.
    """

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filenames: list[str] = []
        self._img_dir: str = ""
        self._selected_index: int = -1
        self._drop_indicator: int = -1  # where a drop would insert
        self._highlight_index: int = -1  # frame being played in preview

        self.setAcceptDrops(True)
        self.setMinimumHeight(TIMELINE_FRAME_SIZE + 52)
        self.setFixedHeight(TIMELINE_FRAME_SIZE + 52)

    def set_img_dir(self, path: str):
        self._img_dir = path

    def set_frames(self, filenames: list[str]):
        self._filenames = list(filenames)
        self._selected_index = -1
        self._highlight_index = -1
        self.update()

    def get_frames(self) -> list[str]:
        return list(self._filenames)

    def set_highlight(self, index: int):
        """Highlight the currently playing frame."""
        if index != self._highlight_index:
            self._highlight_index = index
            self.update()

    def add_frame(self, filename: str):
        self._filenames.append(filename)
        self._selected_index = len(self._filenames) - 1
        self.update()
        self.changed.emit()

    def clear(self):
        self._filenames.clear()
        self._selected_index = -1
        self._highlight_index = -1
        self.update()
        self.changed.emit()

    def _frame_rect(self, index: int) -> QRect:
        x = 8 + index * (TIMELINE_FRAME_SIZE + 6)
        return QRect(x, 18, TIMELINE_FRAME_SIZE, TIMELINE_FRAME_SIZE + 28)

    def _index_at(self, pos: QPoint) -> int:
        """Return the frame index at a position, or -1."""
        for i in range(len(self._filenames)):
            if self._frame_rect(i).contains(pos):
                return i
        return -1

    def _insert_index_at(self, x: int) -> int:
        """Return the insertion index for a drop at x position."""
        for i in range(len(self._filenames)):
            rect = self._frame_rect(i)
            if x < rect.center().x():
                return i
        return len(self._filenames)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # background
        p.setBrush(QColor(C_SURFACE))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(self.rect(), 6, 6)

        if not self._filenames:
            p.setPen(QColor(C_TEXT_MUTED))
            p.setFont(QFont("sans-serif", 12))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "drag sprites here to build an animation")
            return

        for i, fname in enumerate(self._filenames):
            rect = self._frame_rect(i)

            # frame background
            is_selected = (i == self._selected_index)
            is_playing = (i == self._highlight_index)

            if is_playing:
                bg = QColor(C_ACCENT_DIM)
                border = QColor(C_ACCENT)
                border_width = 2
            elif is_selected:
                bg = QColor("#2a2a50")
                border = QColor(C_ACCENT)
                border_width = 2
            else:
                bg = QColor(C_PANEL)
                border = QColor(C_BORDER)
                border_width = 1

            p.setBrush(bg)
            p.setPen(QPen(border, border_width))
            p.drawRoundedRect(rect, 4, 4)

            # sprite thumbnail
            path = os.path.join(self._img_dir, fname)
            px = QPixmap(path)
            if not px.isNull():
                thumb_area = TIMELINE_FRAME_SIZE - 8
                scaled = px.scaled(
                    thumb_area, thumb_area,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                sx = rect.x() + (TIMELINE_FRAME_SIZE - scaled.width()) // 2
                sy = rect.y() + 4 + (thumb_area - scaled.height()) // 2
                p.drawPixmap(sx, sy, scaled)

            # frame number + filename
            p.setPen(QColor(C_TEXT_DIM))
            p.setFont(QFont("monospace", 8))
            label_rect = QRect(rect.x(), rect.bottom() - 22, rect.width(), 10)
            p.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter,
                       fname.replace(".png", ""))

            # frame index
            p.setPen(QColor(C_TEXT_MUTED))
            p.setFont(QFont("monospace", 8))
            idx_rect = QRect(rect.x(), rect.bottom() - 12, rect.width(), 12)
            p.drawText(idx_rect, Qt.AlignmentFlag.AlignHCenter, f"#{i+1}")

        # drop indicator
        if self._drop_indicator >= 0:
            x = 8 + self._drop_indicator * (TIMELINE_FRAME_SIZE + 6) - 3
            p.setPen(QPen(QColor(C_ACCENT), 3))
            p.drawLine(x, 16, x, self.height() - 6)

        # total width for scrolling
        total_w = 16 + len(self._filenames) * (TIMELINE_FRAME_SIZE + 6)
        if total_w > self.minimumWidth():
            self.setMinimumWidth(total_w)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._index_at(event.pos())
            self._selected_index = idx
            self.update()

            if idx >= 0:
                # start internal drag for reordering
                self._drag_start_pos = event.pos()
                self._drag_index = idx

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.MouseButton.LeftButton
                and hasattr(self, '_drag_index') and self._drag_index >= 0):
            # check if we've moved enough to start a drag
            if (event.pos() - self._drag_start_pos).manhattanLength() < 10:
                return

            drag = QDrag(self)
            mime = QMimeData()
            # encode as "reorder:INDEX:FILENAME"
            fname = self._filenames[self._drag_index]
            mime.setData(MIME_FRAME, f"reorder:{self._drag_index}:{fname}".encode())
            drag.setMimeData(mime)

            path = os.path.join(self._img_dir, fname)
            px = QPixmap(path)
            if not px.isNull():
                preview = px.scaled(48, 48,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                drag.setPixmap(preview)
                drag.setHotSpot(QPoint(24, 24))

            drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)

    def mouseDoubleClickEvent(self, event):
        """Double-click a frame to remove it."""
        idx = self._index_at(event.pos())
        if idx >= 0:
            self._filenames.pop(idx)
            self._selected_index = -1
            self._highlight_index = -1
            self.update()
            self.changed.emit()

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_FRAME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME_FRAME):
            self._drop_indicator = self._insert_index_at(event.position().toPoint().x())
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drop_indicator = -1
        self.update()

    def dropEvent(self, event):
        self._drop_indicator = -1
        if not event.mimeData().hasFormat(MIME_FRAME):
            return

        data = bytes(event.mimeData().data(MIME_FRAME)).decode()
        insert_at = self._insert_index_at(event.position().toPoint().x())

        if data.startswith("reorder:"):
            # internal reorder
            _, old_idx_str, fname = data.split(":", 2)
            old_idx = int(old_idx_str)
            self._filenames.pop(old_idx)
            # adjust insert index if we removed before it
            if old_idx < insert_at:
                insert_at -= 1
            self._filenames.insert(insert_at, fname)
        else:
            # new frame from palette
            self._filenames.insert(insert_at, data)

        self._selected_index = insert_at
        self.update()
        self.changed.emit()
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if 0 <= self._selected_index < len(self._filenames):
                self._filenames.pop(self._selected_index)
                self._selected_index = min(self._selected_index,
                                           len(self._filenames) - 1)
                self.update()
                self.changed.emit()

    def focusInEvent(self, event):
        self.update()

    def focusOutEvent(self, event):
        self.update()


# ── animation preview ────────────────────────────────────────────────────────

class AnimationPreview(QWidget):
    """Large animation preview with onion skin support."""

    frame_changed = pyqtSignal(int)  # emits current frame index

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img_dir = ""
        self._frames: list[str] = []
        self._fps = 8
        self._loop = True
        self._onion_skin = False
        self._current_frame = 0
        self._playing = False
        self._checkerboard = True

        self.setFixedSize(PREVIEW_SIZE + 16, PREVIEW_SIZE + 16)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def set_img_dir(self, path: str):
        self._img_dir = path

    def set_animation(self, frames: list[str], fps: int, loop: bool):
        self._frames = list(frames)
        self._fps = max(1, fps)
        self._loop = loop
        self._current_frame = 0
        if self._playing:
            self._restart_timer()
        self.update()
        self.frame_changed.emit(0)

    def set_onion_skin(self, enabled: bool):
        self._onion_skin = enabled
        self.update()

    def play(self):
        if not self._frames:
            return
        self._playing = True
        self._restart_timer()

    def pause(self):
        self._playing = False
        self._timer.stop()

    def stop(self):
        self._playing = False
        self._timer.stop()
        self._current_frame = 0
        self.update()
        self.frame_changed.emit(0)

    def step_forward(self):
        if not self._frames:
            return
        self._current_frame = (self._current_frame + 1) % len(self._frames)
        self.update()
        self.frame_changed.emit(self._current_frame)

    def step_back(self):
        if not self._frames:
            return
        self._current_frame = (self._current_frame - 1) % len(self._frames)
        self.update()
        self.frame_changed.emit(self._current_frame)

    def is_playing(self) -> bool:
        return self._playing

    def _restart_timer(self):
        interval = max(1, int(1000 / self._fps))
        self._timer.start(interval)

    def _advance(self):
        if not self._frames:
            return
        self._current_frame += 1
        if self._current_frame >= len(self._frames):
            if self._loop:
                self._current_frame = 0
            else:
                self._current_frame = len(self._frames) - 1
                self.pause()
        self.update()
        self.frame_changed.emit(self._current_frame)

    def _load_pixmap(self, filename: str) -> QPixmap:
        return QPixmap(os.path.join(self._img_dir, filename))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # outer border
        p.setBrush(QColor(C_SURFACE))
        p.setPen(QPen(QColor(C_BORDER), 1))
        p.drawRoundedRect(0, 0, self.width(), self.height(), 8, 8)

        # checkerboard background (shows transparency)
        inner = QRect(8, 8, PREVIEW_SIZE, PREVIEW_SIZE)
        if self._checkerboard:
            check_size = 16
            for row in range(PREVIEW_SIZE // check_size + 1):
                for col in range(PREVIEW_SIZE // check_size + 1):
                    cx = inner.x() + col * check_size
                    cy = inner.y() + row * check_size
                    cr = QRect(cx, cy, check_size, check_size).intersected(inner)
                    if (row + col) % 2 == 0:
                        p.fillRect(cr, QColor("#1a1a30"))
                    else:
                        p.fillRect(cr, QColor("#222240"))

        if not self._frames:
            p.setPen(QColor(C_TEXT_MUTED))
            p.setFont(QFont("sans-serif", 11))
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, "no frames")
            return

        # onion skin: previous frame at half opacity
        if self._onion_skin and self._current_frame > 0:
            prev_px = self._load_pixmap(self._frames[self._current_frame - 1])
            if not prev_px.isNull():
                scaled = prev_px.scaled(
                    PREVIEW_SIZE, PREVIEW_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = inner.x() + (PREVIEW_SIZE - scaled.width()) // 2
                y = inner.y() + (PREVIEW_SIZE - scaled.height()) // 2
                p.setOpacity(0.3)
                p.drawPixmap(x, y, scaled)
                p.setOpacity(1.0)

        # current frame
        px = self._load_pixmap(self._frames[self._current_frame])
        if not px.isNull():
            scaled = px.scaled(
                PREVIEW_SIZE, PREVIEW_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = inner.x() + (PREVIEW_SIZE - scaled.width()) // 2
            y = inner.y() + (PREVIEW_SIZE - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)

        # frame counter
        p.setPen(QColor(C_TEXT_DIM))
        p.setFont(QFont("monospace", 10))
        counter = f"{self._current_frame + 1} / {len(self._frames)}"
        p.drawText(QRect(8, self.height() - 22, PREVIEW_SIZE, 18),
                   Qt.AlignmentFlag.AlignRight, counter)


# ── transport controls ───────────────────────────────────────────────────────

class TransportBar(QWidget):
    """Play/pause/stop/step controls."""

    play_clicked = pyqtSignal()
    pause_clicked = pyqtSignal()
    stop_clicked = pyqtSignal()
    step_back_clicked = pyqtSignal()
    step_forward_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        btn_style = f"""
            QPushButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 6px 12px; border-radius: 4px;
                color: {C_TEXT}; font-size: 14px; font-weight: bold;
                min-width: 36px;
            }}
            QPushButton:hover {{ background: {C_ACCENT_DIM}; border-color: {C_ACCENT}; }}
            QPushButton:pressed {{ background: {C_ACCENT}; }}
        """

        self._btn_back = QPushButton("\u23ea")
        self._btn_back.setToolTip("Step back")
        self._btn_back.clicked.connect(self.step_back_clicked.emit)
        self._btn_back.setStyleSheet(btn_style)

        self._btn_play = QPushButton("\u25b6")
        self._btn_play.setToolTip("Play")
        self._btn_play.clicked.connect(self.play_clicked.emit)
        self._btn_play.setStyleSheet(btn_style)

        self._btn_pause = QPushButton("\u23f8")
        self._btn_pause.setToolTip("Pause")
        self._btn_pause.clicked.connect(self.pause_clicked.emit)
        self._btn_pause.setStyleSheet(btn_style)

        self._btn_stop = QPushButton("\u23f9")
        self._btn_stop.setToolTip("Stop / reset")
        self._btn_stop.clicked.connect(self.stop_clicked.emit)
        self._btn_stop.setStyleSheet(btn_style)

        self._btn_fwd = QPushButton("\u23e9")
        self._btn_fwd.setToolTip("Step forward")
        self._btn_fwd.clicked.connect(self.step_forward_clicked.emit)
        self._btn_fwd.setStyleSheet(btn_style)

        layout.addStretch()
        layout.addWidget(self._btn_back)
        layout.addWidget(self._btn_play)
        layout.addWidget(self._btn_pause)
        layout.addWidget(self._btn_stop)
        layout.addWidget(self._btn_fwd)
        layout.addStretch()


# ── main animator window ─────────────────────────────────────────────────────

class AnimatorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("claudemeji animator")
        self.resize(1200, 900)

        self._img_dir = ""
        self._pack_path = ""
        self._current_action = ACTIONS[0]
        self._current_variant = "base"
        self._current_phase = "loop"  # "intro" | "loop" | "outro"

        self._action_defs: dict[str, ActionDef] = {
            a: ActionDef(files=[], fps=8, loop=True)
            for a in ACTIONS
        }

        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        top_btn_style = f"""
            QPushButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 6px 14px; border-radius: 4px; color: {C_TEXT};
                font-size: 12px;
            }}
            QPushButton:hover {{ background: {C_ACCENT_DIM}; border-color: {C_ACCENT}; }}
        """
        small_label_style = f"color: {C_TEXT_DIM}; font-size: 11px;"

        # ── top bar: pack info + load/save + global settings ──
        top = QHBoxLayout()
        top.setSpacing(8)
        self._pack_label = QLabel("No pack loaded")
        self._pack_label.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 12px;")

        btn_open = QPushButton("Open Pack...")
        btn_open.clicked.connect(self._open_pack)
        btn_load = QPushButton("Load Config...")
        btn_load.clicked.connect(self._load_config_file)
        btn_save = QPushButton("Save Config")
        btn_save.clicked.connect(self._save_config)
        for btn in (btn_open, btn_load, btn_save):
            btn.setStyleSheet(top_btn_style)

        top.addWidget(self._pack_label, 1)
        top.addWidget(btn_open)
        top.addWidget(btn_load)
        top.addWidget(btn_save)

        # global settings (pack-wide, not per-action)
        top.addSpacing(16)
        sep_lbl = QLabel("|")
        sep_lbl.setStyleSheet(f"color: {C_BORDER}; font-size: 14px;")
        top.addWidget(sep_lbl)
        top.addSpacing(4)

        facing_lbl = QLabel("Faces:")
        facing_lbl.setStyleSheet(small_label_style)
        facing_lbl.setToolTip("Which direction sprites face natively (before any flipping)")
        top.addWidget(facing_lbl)
        self._facing_combo = QComboBox()
        self._facing_combo.addItems(["left", "right"])
        self._facing_combo.setCurrentText("left")
        self._facing_combo.setFixedWidth(60)
        top.addWidget(self._facing_combo)

        top.addSpacing(8)
        pull_lbl = QLabel("Pull:")
        pull_lbl.setStyleSheet(small_label_style)
        pull_lbl.setToolTip("How far (px) sprite weight pulls windows down (0 = disabled)")
        top.addWidget(pull_lbl)
        self._window_pull_spin = QSpinBox()
        self._window_pull_spin.setRange(0, 200)
        self._window_pull_spin.setValue(0)
        self._window_pull_spin.setFixedWidth(60)
        top.addWidget(self._window_pull_spin)

        root.addLayout(top)

        # ── main area: [action list | preview + transport | sprite palette] ──
        main_split = QSplitter(Qt.Orientation.Horizontal)

        # ── left column: action list + variant selector ──
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        action_header = QLabel("Actions")
        action_header.setStyleSheet(f"color: {C_TEXT}; font-size: 13px; font-weight: bold;")
        left_layout.addWidget(action_header)

        self._action_list = QListWidget()
        self._action_list.setFixedWidth(180)
        self._action_list.setStyleSheet(f"""
            QListWidget {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                border-radius: 6px; font-size: 12px; color: {C_TEXT};
                outline: none;
            }}
            QListWidget::item {{
                padding: 6px 10px; border-radius: 3px;
            }}
            QListWidget::item:selected {{
                background: {C_ACCENT_DIM}; color: {C_TEXT};
            }}
            QListWidget::item:hover {{
                background: {C_BORDER};
            }}
        """)
        self._custom_actions: list[str] = []  # dynamic idle actions
        self._rebuild_action_list()
        self._action_list.setCurrentRow(0)
        self._action_list.currentTextChanged.connect(self._on_action_selected)
        left_layout.addWidget(self._action_list, 1)

        # add/remove custom idle buttons
        idle_btn_row = QHBoxLayout()
        variant_btn_style = f"""
            QToolButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                color: {C_TEXT}; font-size: 14px; font-weight: bold;
                min-width: 24px; min-height: 24px; border-radius: 4px;
            }}
            QToolButton:hover {{ background: {C_ACCENT_DIM}; border-color: {C_ACCENT}; }}
            QToolButton:disabled {{ color: {C_TEXT_MUTED}; }}
        """
        add_idle_btn = QToolButton()
        add_idle_btn.setText("+ Add idle")
        add_idle_btn.setToolTip("Add a custom idle animation (appears in idle pool)")
        add_idle_btn.setStyleSheet(variant_btn_style + "QToolButton { min-width: 80px; font-size: 11px; }")
        add_idle_btn.clicked.connect(self._add_custom_idle)
        self._remove_idle_btn = QToolButton()
        self._remove_idle_btn.setText("\u2212 Remove")
        self._remove_idle_btn.setToolTip("Remove selected custom idle")
        self._remove_idle_btn.setStyleSheet(variant_btn_style + "QToolButton { min-width: 80px; font-size: 11px; }")
        self._remove_idle_btn.setEnabled(False)
        self._remove_idle_btn.clicked.connect(self._remove_custom_idle)
        idle_btn_row.addWidget(add_idle_btn)
        idle_btn_row.addWidget(self._remove_idle_btn)
        idle_btn_row.addStretch()
        left_layout.addLayout(idle_btn_row)

        # action description
        self._action_desc = QLabel()
        self._action_desc.setWordWrap(True)
        self._action_desc.setStyleSheet(
            f"color: {C_TEXT_DIM}; font-size: 11px; padding: 6px 8px;"
            f"background: {C_SURFACE}; border-radius: 4px;"
        )
        left_layout.addWidget(self._action_desc)

        # variant selector
        variant_row = QHBoxLayout()
        self._variant_label = QLabel("Variant:")
        self._variant_label.setStyleSheet(small_label_style)
        self._variant_combo = QComboBox()
        self._variant_combo.setMinimumWidth(140)
        self._variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        variant_row.addWidget(self._variant_label)
        variant_row.addWidget(self._variant_combo, 1)

        self._add_variant_btn = QToolButton()
        self._add_variant_btn.setText("+")
        self._add_variant_btn.setToolTip("Add A/B variant (random alternate animation)")
        self._add_variant_btn.setStyleSheet(variant_btn_style)
        self._add_variant_btn.clicked.connect(self._add_variant)
        variant_row.addWidget(self._add_variant_btn)

        self._remove_variant_btn = QToolButton()
        self._remove_variant_btn.setText("\u2212")
        self._remove_variant_btn.setToolTip("Remove selected A/B variant")
        self._remove_variant_btn.setStyleSheet(variant_btn_style)
        self._remove_variant_btn.setEnabled(False)
        self._remove_variant_btn.clicked.connect(self._remove_variant)
        variant_row.addWidget(self._remove_variant_btn)

        left_layout.addLayout(variant_row)
        main_split.addWidget(left)

        # ── center column: preview + transport ──
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(8)
        center_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        preview_header = QLabel("Preview")
        preview_header.setStyleSheet(f"color: {C_TEXT}; font-size: 13px; font-weight: bold;")
        preview_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_layout.addWidget(preview_header)

        self._preview = AnimationPreview()
        center_layout.addWidget(self._preview, 0, Qt.AlignmentFlag.AlignHCenter)

        # transport
        self._transport = TransportBar()
        self._transport.play_clicked.connect(self._on_play)
        self._transport.pause_clicked.connect(self._on_pause)
        self._transport.stop_clicked.connect(self._on_stop)
        self._transport.step_back_clicked.connect(self._preview.step_back)
        self._transport.step_forward_clicked.connect(self._preview.step_forward)
        center_layout.addWidget(self._transport)

        # preview info + onion skin + transition from (grouped under transport)
        info_row = QHBoxLayout()
        info_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_info = QLabel("no frames")
        self._preview_info.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 11px;")
        info_row.addWidget(self._preview_info)
        info_row.addSpacing(12)
        self._onion_check = QCheckBox("Onion skin")
        self._onion_check.setToolTip("Ghost previous frame at 30% opacity")
        self._onion_check.stateChanged.connect(
            lambda s: self._preview.set_onion_skin(s == Qt.CheckState.Checked.value)
        )
        info_row.addWidget(self._onion_check)
        center_layout.addLayout(info_row)

        trans_row = QHBoxLayout()
        trans_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trans_lbl = QLabel("Transition from:")
        trans_lbl.setStyleSheet(small_label_style)
        trans_row.addWidget(trans_lbl)
        self._from_combo = QComboBox()
        self._from_combo.addItem("(none)")
        self._from_combo.addItems(ACTIONS)
        self._from_combo.currentTextChanged.connect(self._on_transition_from_changed)
        trans_row.addWidget(self._from_combo)
        center_layout.addLayout(trans_row)

        center_layout.addStretch()
        main_split.addWidget(center)

        # ── right column: sprite palette ──
        self._palette = SpritePalette()
        self._palette.frame_double_clicked.connect(self._on_frame_double_clicked)
        main_split.addWidget(self._palette)

        main_split.setStretchFactor(0, 0)  # action list: fixed
        main_split.setStretchFactor(1, 0)  # preview: fixed
        main_split.setStretchFactor(2, 1)  # palette: stretches

        root.addWidget(main_split, 1)

        # ── bottom: phase selector + timeline + per-action controls ──
        timeline_header_row = QHBoxLayout()
        tl_label = QLabel("Timeline")
        tl_label.setStyleSheet(f"color: {C_TEXT}; font-size: 13px; font-weight: bold;")
        timeline_header_row.addWidget(tl_label)

        # phase selector: Intro | Loop | Outro
        phase_style_inactive = f"""
            QPushButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 4px 14px; color: {C_TEXT_DIM}; font-size: 11px;
            }}
            QPushButton:hover {{ background: {C_ACCENT_DIM}; color: {C_TEXT}; border-color: {C_ACCENT}; }}
        """
        phase_style_active = f"""
            QPushButton {{
                background: {C_ACCENT_DIM}; border: 1px solid {C_ACCENT};
                padding: 4px 14px; color: {C_TEXT}; font-size: 11px; font-weight: bold;
            }}
        """

        self._btn_intro = QPushButton("Intro")
        self._btn_loop  = QPushButton("Loop")
        self._btn_outro = QPushButton("Outro")

        self._btn_intro.setStyleSheet(phase_style_inactive + "QPushButton { border-radius: 0; border-top-left-radius: 4px; border-bottom-left-radius: 4px; border-right: none; }")
        self._btn_loop.setStyleSheet(phase_style_active  + "QPushButton { border-radius: 0; border-right: none; }")
        self._btn_outro.setStyleSheet(phase_style_inactive + "QPushButton { border-radius: 0; border-top-right-radius: 4px; border-bottom-right-radius: 4px; }")

        self._phase_btns = {
            "intro": self._btn_intro,
            "loop":  self._btn_loop,
            "outro": self._btn_outro,
        }
        self._phase_styles = {
            "active":   phase_style_active,
            "inactive": phase_style_inactive,
        }

        self._btn_intro.clicked.connect(lambda: self._set_phase("intro"))
        self._btn_loop.clicked.connect(lambda: self._set_phase("loop"))
        self._btn_outro.clicked.connect(lambda: self._set_phase("outro"))

        phase_hint = QLabel("drag to reorder  \u00b7  double-click or del to remove")
        phase_hint.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")

        timeline_header_row.addSpacing(8)
        timeline_header_row.addWidget(self._btn_intro)
        timeline_header_row.addWidget(self._btn_loop)
        timeline_header_row.addWidget(self._btn_outro)
        timeline_header_row.addSpacing(12)
        timeline_header_row.addWidget(phase_hint)
        timeline_header_row.addStretch()

        btn_clear_tl = QPushButton("Clear")
        btn_clear_tl.setStyleSheet(f"""
            QPushButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 4px 10px; border-radius: 4px; color: {C_TEXT_DIM};
                font-size: 11px;
            }}
            QPushButton:hover {{ background: #3a1a1a; border-color: {C_DANGER}; color: {C_DANGER}; }}
        """)
        btn_clear_tl.clicked.connect(self._clear_timeline)
        timeline_header_row.addWidget(btn_clear_tl)

        root.addLayout(timeline_header_row)

        tl_scroll = QScrollArea()
        tl_scroll.setFixedHeight(TIMELINE_FRAME_SIZE + 58)
        tl_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        tl_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tl_scroll.setWidgetResizable(True)
        tl_scroll.setStyleSheet(f"""
            QScrollArea {{ border: 1px solid {C_BORDER}; border-radius: 6px; background: {C_SURFACE}; }}
            QScrollBar:horizontal {{ height: 8px; background: {C_SURFACE}; }}
            QScrollBar::handle:horizontal {{ background: {C_BORDER}; border-radius: 4px; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        """)

        self._timeline = TimelineWidget()
        self._timeline.changed.connect(self._on_timeline_changed)
        self._timeline.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        tl_scroll.setWidget(self._timeline)
        root.addWidget(tl_scroll)

        # ── per-action controls (below timeline) ──
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(12)

        ctrl_row.addWidget(QLabel("FPS:"))
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(8)
        self._fps_spin.setFixedWidth(60)
        self._fps_spin.valueChanged.connect(self._on_controls_changed)
        ctrl_row.addWidget(self._fps_spin)

        self._loop_check = QCheckBox("Loop")
        self._loop_check.setChecked(True)
        self._loop_check.stateChanged.connect(self._on_controls_changed)
        ctrl_row.addWidget(self._loop_check)

        ctrl_row.addSpacing(8)

        # walk speed (conditional)
        self._walk_speed_label = QLabel("Walk speed:")
        self._walk_speed_spin = QSpinBox()
        self._walk_speed_spin.setRange(0, 10)
        self._walk_speed_spin.setValue(0)
        self._walk_speed_spin.setFixedWidth(50)
        self._walk_speed_spin.setToolTip("Movement speed during animation (px/tick)")
        self._walk_speed_spin.valueChanged.connect(self._on_controls_changed)
        ctrl_row.addWidget(self._walk_speed_label)
        ctrl_row.addWidget(self._walk_speed_spin)

        # min restlessness + idle tier (conditional)
        self._min_rest_label = QLabel("Min restless:")
        self._min_rest_spin = QSpinBox()
        self._min_rest_spin.setRange(0, 4)
        self._min_rest_spin.setValue(0)
        self._min_rest_spin.setFixedWidth(50)
        self._min_rest_spin.setToolTip("Minimum restlessness level to include this idle in the pool")
        self._min_rest_spin.valueChanged.connect(self._on_controls_changed)
        ctrl_row.addWidget(self._min_rest_label)
        ctrl_row.addWidget(self._min_rest_spin)

        self._idle_tier_check = QCheckBox("Idle tier")
        self._idle_tier_check.setToolTip("Include in idle pool")
        self._idle_tier_check.stateChanged.connect(self._on_controls_changed)
        ctrl_row.addWidget(self._idle_tier_check)

        ctrl_row.addSpacing(8)

        # offset Y (conditional — for sitting/perching actions)
        self._offset_y_label = QLabel("Offset Y:")
        self._offset_y_spin = QSpinBox()
        self._offset_y_spin.setRange(-50, 50)
        self._offset_y_spin.setValue(0)
        self._offset_y_spin.setFixedWidth(60)
        self._offset_y_spin.setToolTip("Vertical pixel shift while action plays (positive = down)")
        self._offset_y_spin.valueChanged.connect(self._on_controls_changed)
        ctrl_row.addWidget(self._offset_y_label)
        ctrl_row.addWidget(self._offset_y_spin)

        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # wire preview frame highlight to timeline
        self._preview.frame_changed.connect(self._timeline.set_highlight)

        # init
        self._update_variant_selector(self._current_action)
        self._load_action_into_ui(self._current_action)

    def _apply_style(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {C_BG}; color: {C_TEXT}; }}
            QComboBox {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 4px 8px; color: {C_TEXT}; border-radius: 4px;
            }}
            QComboBox:hover {{ border-color: {C_ACCENT}; }}
            QComboBox::drop-down {{ border: none; }}
            QSpinBox {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 4px 8px; color: {C_TEXT}; border-radius: 4px;
            }}
            QSpinBox:hover {{ border-color: {C_ACCENT}; }}
            QCheckBox {{ color: {C_TEXT}; font-size: 12px; }}
            QCheckBox::indicator {{
                width: 16px; height: 16px; border-radius: 3px;
                border: 1px solid {C_BORDER}; background: {C_SURFACE};
            }}
            QCheckBox::indicator:checked {{
                background: {C_ACCENT}; border-color: {C_ACCENT};
            }}
            QLabel {{ color: {C_TEXT}; }}
            QSplitter::handle {{ background: {C_BORDER}; width: 2px; }}
        """)

    # ── variant helpers ──────────────────────────────────────────────────────

    def _variant_keys(self, action_name: str) -> list[str]:
        keys = ["base"]
        for p in ACTION_POSTURES.get(action_name, []):
            keys.append(f"postures/{p}")
        for c in ACTION_CONTEXTS.get(action_name, []):
            keys.append(f"contexts/{c}")
        # A/B variants
        base = self._action_defs.get(action_name)
        if base:
            for i in range(len(base.variants)):
                keys.append(f"variants/{i}")
        return keys

    def _variant_label_text(self, key: str) -> str:
        if key == "base":
            return "base"
        kind, name = key.split("/", 1)
        if kind == "variants":
            return f"variant {chr(65 + int(name))}"  # A, B, C...
        return f"{kind[:-1]}: {name}"

    def _update_variant_selector(self, action_name: str):
        keys = self._variant_keys(action_name)
        has_variants = len(keys) > 1

        self._variant_label.setVisible(has_variants)
        self._variant_combo.setVisible(has_variants)

        self._variant_combo.blockSignals(True)
        self._variant_combo.clear()
        for k in keys:
            self._variant_combo.addItem(self._variant_label_text(k), k)
        if self._current_variant in keys:
            self._variant_combo.setCurrentIndex(keys.index(self._current_variant))
        else:
            self._current_variant = "base"
            self._variant_combo.setCurrentIndex(0)
        self._variant_combo.blockSignals(False)

    def _get_variant_def(self, action_name: str, variant: str) -> ActionDef:
        base = self._action_defs[action_name]
        if variant == "base":
            return base
        kind, name = variant.split("/", 1)
        if kind == "postures":
            return base.postures.get(name, ActionDef(files=[], fps=8, loop=True))
        if kind == "contexts":
            return base.contexts.get(name, ActionDef(files=[], fps=8, loop=True))
        if kind == "variants":
            idx = int(name)
            if 0 <= idx < len(base.variants):
                return base.variants[idx]
            return ActionDef(files=[], fps=8, loop=True)
        return base

    # ── interactions ─────────────────────────────────────────────────────────

    def _open_pack(self):
        folder = QFileDialog.getExistingDirectory(self, "Select sprite pack folder")
        if not folder:
            return

        img_dir = os.path.join(folder, "img")
        if not os.path.isdir(img_dir):
            img_dir = folder

        self._pack_path = folder
        self._img_dir = img_dir
        self._pack_label.setText(f"Pack: {os.path.basename(folder)}")

        self._timeline.set_img_dir(img_dir)
        self._palette.load(img_dir)
        self._preview.set_img_dir(img_dir)

        self._action_defs = {
            a: ActionDef(files=[], fps=8, loop=True)
            for a in ACTIONS
        }
        self._current_variant = "base"
        self._load_action_into_ui(self._current_action)

    def _load_config_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open config.toml",
            os.path.expanduser("~/.claudemeji"),
            "TOML files (*.toml)"
        )
        if not path:
            return

        try:
            if sys.version_info >= (3, 11):
                import tomllib
            else:
                import tomli as tomllib  # type: ignore
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Load failed", f"Could not parse config:\n{e}")
            return

        pack_data = data.get("sprite_pack", {})
        pack_path = os.path.expanduser(pack_data.get("path", ""))
        img_subdir = pack_data.get("img_dir", "")

        if pack_path:
            img_dir = os.path.join(pack_path, img_subdir) if img_subdir else pack_path
            if os.path.isdir(img_dir):
                self._pack_path = pack_path
                self._img_dir = img_dir
                self._pack_label.setText(f"Pack: {os.path.basename(pack_path)}")
                self._timeline.set_img_dir(img_dir)
                self._palette.load(img_dir)
                self._preview.set_img_dir(img_dir)
            else:
                QMessageBox.warning(
                    self, "Pack folder not found",
                    f"Sprite pack not found at:\n{img_dir}\n\n"
                    "Use 'Open Pack...' to locate it."
                )

        from claudemeji.config import _parse_action_def
        self._action_defs = {
            a: ActionDef(files=[], fps=8, loop=True)
            for a in ACTIONS
        }
        for name, adef_raw in data.get("actions", {}).items():
            self._action_defs[name] = _parse_action_def(adef_raw)

        physics_data = data.get("physics", {})
        self._window_pull_spin.setValue(physics_data.get("window_pull_distance", 0))
        self._facing_combo.setCurrentText(physics_data.get("default_facing", "left"))

        # rebuild action list to include custom actions from config
        self._rebuild_action_list()
        self._current_variant = "base"
        self._load_action_into_ui(self._current_action)
        self._refresh_action_list()

    def _on_action_selected(self, action_name: str):
        if not action_name or action_name.startswith("---"):
            return
        action_name = action_name.split("  ")[0].strip()  # strip indicators
        self._save_current_to_defs()
        # ensure action exists in defs (custom actions from config)
        if action_name not in self._action_defs:
            self._action_defs[action_name] = ActionDef(files=[], fps=8, loop=True)
        self._current_action = action_name
        self._current_variant = "base"
        # enable/disable remove button (only for custom actions)
        self._remove_idle_btn.setEnabled(action_name in self._custom_actions)
        self._load_action_into_ui(action_name)

    def _on_variant_changed(self, index: int):
        if index < 0:
            return
        self._save_current_to_defs()
        self._current_variant = self._variant_combo.itemData(index)
        # enable remove button only for A/B variants
        is_ab = self._current_variant.startswith("variants/")
        self._remove_variant_btn.setEnabled(is_ab)
        self._populate_controls_from_variant()
        self._update_preview()

    def _add_variant(self):
        """Add a new empty A/B variant to the current action."""
        self._save_current_to_defs()
        base = self._action_defs[self._current_action]
        base.variants.append(ActionDef(files=[], fps=base.fps, loop=base.loop))
        new_key = f"variants/{len(base.variants) - 1}"
        self._current_variant = new_key
        self._update_variant_selector(self._current_action)
        # select the new variant
        for i in range(self._variant_combo.count()):
            if self._variant_combo.itemData(i) == new_key:
                self._variant_combo.setCurrentIndex(i)
                break
        self._remove_variant_btn.setEnabled(True)
        self._refresh_action_list()

    def _remove_variant(self):
        """Remove the currently selected A/B variant."""
        if not self._current_variant.startswith("variants/"):
            return
        idx = int(self._current_variant.split("/", 1)[1])
        base = self._action_defs[self._current_action]
        if 0 <= idx < len(base.variants):
            base.variants.pop(idx)
        self._current_variant = "base"
        self._remove_variant_btn.setEnabled(False)
        self._update_variant_selector(self._current_action)
        self._populate_controls_from_variant()
        self._update_preview()
        self._refresh_action_list()

    def _load_action_into_ui(self, action_name: str):
        desc = ACTION_DESCRIPTIONS.get(action_name, "Custom action")
        self._action_desc.setText(f"{action_name} \u2014 {desc}")
        self._update_variant_selector(action_name)
        adef = self._action_defs.get(action_name)
        # conditional visibility for per-action controls
        is_idle_like = (
            action_name == "sit_idle"
            or (adef and adef.idle_tier)
            or action_name in self._custom_actions
        )
        movement_actions = {"walk", "run", "sprint", "crawl"}
        is_movement = action_name in movement_actions or (adef and adef.walk_speed > 0)
        self._min_rest_label.setVisible(is_idle_like)
        self._min_rest_spin.setVisible(is_idle_like)
        self._idle_tier_check.setVisible(is_idle_like or self._current_variant == "base")
        self._walk_speed_label.setVisible(is_movement or self._current_variant == "base")
        self._walk_speed_spin.setVisible(is_movement or self._current_variant == "base")
        self._populate_controls_from_variant()
        self._update_preview()

    def _set_phase(self, phase: str):
        """Switch the timeline between intro / loop / outro."""
        self._save_current_to_defs()
        self._current_phase = phase

        # update button visuals
        inactive = f"""
            QPushButton {{
                background: {C_SURFACE}; border: 1px solid {C_BORDER};
                padding: 4px 14px; color: {C_TEXT_DIM}; font-size: 11px;
                border-radius: 0;
            }}
            QPushButton:hover {{ background: {C_ACCENT_DIM}; color: {C_TEXT}; border-color: {C_ACCENT}; }}
        """
        active = f"""
            QPushButton {{
                background: {C_ACCENT_DIM}; border: 1px solid {C_ACCENT};
                padding: 4px 14px; color: {C_TEXT}; font-size: 11px; font-weight: bold;
                border-radius: 0;
            }}
        """
        self._btn_intro.setStyleSheet((active if phase == "intro" else inactive)
            + "QPushButton { border-top-left-radius: 4px; border-bottom-left-radius: 4px; border-right: none; }")
        self._btn_loop.setStyleSheet((active if phase == "loop" else inactive)
            + "QPushButton { border-right: none; }")
        self._btn_outro.setStyleSheet((active if phase == "outro" else inactive)
            + "QPushButton { border-top-right-radius: 4px; border-bottom-right-radius: 4px; }")

        self._populate_controls_from_variant()
        self._update_preview()

    def _on_frame_double_clicked(self, filename: str):
        """Double-click in palette appends to current timeline."""
        self._timeline.add_frame(filename)

    def _populate_controls_from_variant(self):
        adef = self._get_variant_def(self._current_action, self._current_variant)

        # show the right phase frames in the timeline
        if self._current_phase == "intro":
            self._timeline.set_frames(adef.intro_files)
        elif self._current_phase == "outro":
            self._timeline.set_frames(adef.outro_files)
        else:
            self._timeline.set_frames(adef.files)

        # fps/loop/next only affect the main loop, grey them out otherwise
        is_loop = self._current_phase == "loop"
        for w in (self._fps_spin, self._loop_check):
            w.setEnabled(is_loop)

        self._fps_spin.blockSignals(True)
        self._fps_spin.setValue(adef.fps)
        self._fps_spin.blockSignals(False)

        self._loop_check.blockSignals(True)
        self._loop_check.setChecked(adef.loop)
        self._loop_check.blockSignals(False)

        self._min_rest_spin.blockSignals(True)
        self._min_rest_spin.setValue(adef.min_restlessness)
        self._min_rest_spin.blockSignals(False)

        self._walk_speed_spin.blockSignals(True)
        self._walk_speed_spin.setValue(int(adef.walk_speed))
        self._walk_speed_spin.blockSignals(False)

        self._offset_y_spin.blockSignals(True)
        self._offset_y_spin.setValue(adef.offset_y)
        self._offset_y_spin.blockSignals(False)

        # idle_tier only applies to base variant
        base = self._action_defs.get(self._current_action)
        self._idle_tier_check.blockSignals(True)
        self._idle_tier_check.setChecked(base.idle_tier if base else False)
        self._idle_tier_check.blockSignals(False)

    def _save_current_to_defs(self):
        """Write the current timeline + controls back into the action def."""
        current_frames = self._timeline.get_frames()
        adef = self._get_variant_def(self._current_action, self._current_variant)

        # update only the phase we're editing
        if self._current_phase == "intro":
            adef.intro_files = current_frames
            return
        elif self._current_phase == "outro":
            adef.outro_files = current_frames
            return

        # loop phase: rebuild the full def
        new_def = ActionDef(
            files=current_frames,
            fps=self._fps_spin.value(),
            loop=self._loop_check.isChecked(),
            intro_files=adef.intro_files,
            outro_files=adef.outro_files,
            min_restlessness=self._min_rest_spin.value(),
            walk_speed=float(self._walk_speed_spin.value()),
            offset_y=self._offset_y_spin.value(),
        )

        base = self._action_defs[self._current_action]
        if self._current_variant == "base":
            self._action_defs[self._current_action] = ActionDef(
                files=new_def.files,
                fps=new_def.fps,
                loop=new_def.loop,
                intro_files=new_def.intro_files,
                outro_files=new_def.outro_files,
                postures=base.postures,
                contexts=base.contexts,
                variants=base.variants,
                min_restlessness=new_def.min_restlessness,
                walk_speed=new_def.walk_speed,
                offset_y=new_def.offset_y,
                idle_tier=self._idle_tier_check.isChecked(),
            )
        else:
            kind, name = self._current_variant.split("/", 1)
            if kind == "postures":
                base.postures[name] = new_def
            elif kind == "contexts":
                base.contexts[name] = new_def
            elif kind == "variants":
                idx = int(name)
                if 0 <= idx < len(base.variants):
                    base.variants[idx] = new_def

    def _on_timeline_changed(self):
        self._save_current_to_defs()
        self._update_preview()
        self._refresh_action_list()

    def _on_controls_changed(self):
        self._save_current_to_defs()
        self._update_preview()

    def _clear_timeline(self):
        self._timeline.clear()

    def _update_preview(self):
        if not self._img_dir:
            return
        adef = self._get_variant_def(self._current_action, self._current_variant)

        # preview shows the currently selected phase
        if self._current_phase == "intro":
            frames = adef.intro_files
            loop = False
        elif self._current_phase == "outro":
            frames = adef.outro_files
            loop = False
        else:
            frames = adef.files
            loop = adef.loop

        self._preview.set_animation(frames, adef.fps, loop)

        # info line shows all three phase counts
        n_intro = len(adef.intro_files)
        n_loop  = len(adef.files)
        n_outro = len(adef.outro_files)
        n = len(frames)

        if n_intro == 0 and n_loop == 0 and n_outro == 0:
            self._preview_info.setText("no frames")
        else:
            duration = n / max(1, adef.fps)
            phase_summary = (
                f"intro:{n_intro}  loop:{n_loop}  outro:{n_outro}  "
                f"\u00b7  {adef.fps}fps  \u00b7  {duration:.1f}s"
            )
            self._preview_info.setText(phase_summary)

        if frames:
            self._preview.play()

    def _on_transition_from_changed(self, from_action: str):
        """Play the 'from' action's frames briefly, then switch to current action."""
        if not self._img_dir or from_action == "(none)":
            self._update_preview()
            return
        from_def = self._action_defs.get(from_action)
        if not from_def or not (from_def.files or from_def.outro_files):
            self._update_preview()
            return
        # play the from-action's outro if it has one, otherwise its loop frames
        frames = from_def.outro_files if from_def.outro_files else from_def.files
        self._preview.set_animation(frames, from_def.fps, False)
        self._preview.play()
        # after 1.5s, switch to current action preview
        QTimer.singleShot(1500, self._update_preview)

    def _on_play(self):
        self._preview.play()

    def _on_pause(self):
        self._preview.pause()

    def _on_stop(self):
        self._preview.stop()

    def _rebuild_action_list(self):
        """Rebuild the action list widget: canonical actions + custom idles from config."""
        current = self._current_action
        self._action_list.blockSignals(True)
        self._action_list.clear()

        # canonical actions
        for a in ACTIONS:
            item = QListWidgetItem(a)
            item.setToolTip(ACTION_DESCRIPTIONS.get(a, ""))
            self._action_list.addItem(item)

        # discover custom actions from loaded config (not in canonical ACTIONS)
        self._custom_actions = []
        for name, adef in self._action_defs.items():
            if name not in ACTIONS:
                self._custom_actions.append(name)

        # add custom actions with visual distinction
        if self._custom_actions:
            sep = QListWidgetItem("--- custom ---")
            sep.setFlags(sep.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            sep.setForeground(QColor(C_TEXT_MUTED))
            self._action_list.addItem(sep)
            for name in self._custom_actions:
                adef = self._action_defs.get(name)
                suffix = " (idle)" if adef and adef.idle_tier else ""
                item = QListWidgetItem(name)
                item.setToolTip(f"Custom action{suffix}")
                self._action_list.addItem(item)

        # restore selection
        for i in range(self._action_list.count()):
            text = self._action_list.item(i).text().split("  ")[0].strip()
            if text == current:
                self._action_list.setCurrentRow(i)
                break
        self._action_list.blockSignals(False)

    def _add_custom_idle(self):
        """Add a new custom idle action."""
        from PyQt6.QtWidgets import QInputDialog
        # find next available idle name
        existing = set(ACTIONS) | set(self._custom_actions)
        n = 1
        while f"idle{n}" in existing:
            n += 1
        default_name = f"idle{n}"
        name, ok = QInputDialog.getText(self, "Add idle", "Action name:", text=default_name)
        if not ok or not name.strip():
            return
        name = name.strip().replace(" ", "_")
        if name in ACTIONS or name in self._custom_actions:
            QMessageBox.warning(self, "Duplicate", f"Action '{name}' already exists.")
            return
        self._save_current_to_defs()
        self._custom_actions.append(name)
        self._action_defs[name] = ActionDef(files=[], fps=8, loop=True, idle_tier=True)
        self._rebuild_action_list()
        # select the new action
        for i in range(self._action_list.count()):
            if self._action_list.item(i).text().split("  ")[0].strip() == name:
                self._action_list.setCurrentRow(i)
                break

    def _remove_custom_idle(self):
        """Remove the currently selected custom action."""
        name = self._current_action
        if name in ACTIONS:
            return  # can't remove canonical actions
        if name not in self._custom_actions:
            return
        self._custom_actions.remove(name)
        if name in self._action_defs:
            del self._action_defs[name]
        self._current_action = ACTIONS[0]
        self._rebuild_action_list()
        self._action_list.setCurrentRow(0)
        self._load_action_into_ui(self._current_action)

    def _refresh_action_list(self):
        for i in range(self._action_list.count()):
            item = self._action_list.item(i)
            name = item.text().split("  ")[0].strip()
            adef = self._action_defs.get(name)
            has_frames = bool(
                adef and (
                    adef.files
                    or any(v.files for v in adef.postures.values())
                    or any(v.files for v in adef.contexts.values())
                    or any(v.files for v in adef.variants)
                )
            )
            item.setText(f"{name}  \u2713" if has_frames else name)

    # ── save config ──────────────────────────────────────────────────────────

    def _save_config(self):
        self._save_current_to_defs()

        if not self._pack_path:
            QMessageBox.warning(self, "No pack", "Open a sprite pack folder first.")
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save config.toml",
            os.path.expanduser("~/.claudemeji/config.toml"),
            "TOML files (*.toml)"
        )
        if not out_path:
            return

        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        img_subdir = os.path.relpath(self._img_dir, self._pack_path)
        if img_subdir == ".":
            img_subdir = ""

        lines: list[str] = [
            "# claudemeji config - generated by animator\n",
            "\n",
        ]

        pull_dist = self._window_pull_spin.value()
        facing = self._facing_combo.currentText()
        has_physics = pull_dist != 0 or facing != "left"
        if has_physics:
            lines.append("[physics]\n")
            if pull_dist != 0:
                lines.append(f"window_pull_distance = {pull_dist}\n")
            if facing != "left":
                lines.append(f'default_facing = "{facing}"\n')
            lines.append("\n")

        lines += [
            "[sprite_pack]\n",
            f'path = "{self._pack_path}"\n',
        ]
        if img_subdir:
            lines.append(f'img_dir = "{img_subdir}"\n')
        lines.append("\n")

        def _emit_def(adef: ActionDef, section: str, is_base: bool = False) -> None:
            has_any = adef.files or adef.intro_files or adef.outro_files
            if not has_any:
                return
            lines.append(f"[{section}]\n")
            if adef.intro_files:
                s = ", ".join(f'"{f}"' for f in adef.intro_files)
                lines.append(f"intro_files = [{s}]\n")
            if adef.files:
                files_str = ", ".join(f'"{f}"' for f in adef.files)
                lines.append(f"files = [{files_str}]\n")
            if adef.outro_files:
                s = ", ".join(f'"{f}"' for f in adef.outro_files)
                lines.append(f"outro_files = [{s}]\n")
            lines.append(f"fps = {adef.fps}\n")
            lines.append(f"loop = {'true' if adef.loop else 'false'}\n")
            if adef.min_restlessness > 0:
                lines.append(f"min_restlessness = {adef.min_restlessness}\n")
            if adef.walk_speed > 0:
                lines.append(f"walk_speed = {adef.walk_speed}\n")
            if adef.offset_y != 0:
                lines.append(f"offset_y = {adef.offset_y}\n")
            if is_base and adef.idle_tier:
                lines.append("idle_tier = true\n")
            lines.append("\n")

        for action_name in self._action_defs:
            adef = self._action_defs[action_name]
            base_has = bool(adef.files or adef.intro_files or adef.outro_files)
            var_has = (
                any(v.files or v.intro_files or v.outro_files for v in adef.postures.values()) or
                any(v.files or v.intro_files or v.outro_files for v in adef.contexts.values()) or
                any(v.files or v.intro_files or v.outro_files for v in adef.variants)
            )
            if not base_has and not var_has:
                continue

            _emit_def(adef, f"actions.{action_name}", is_base=True)

            for posture_name, pdef in adef.postures.items():
                _emit_def(pdef, f"actions.{action_name}.postures.{posture_name}")

            for ctx_name, cdef in adef.contexts.items():
                _emit_def(cdef, f"actions.{action_name}.contexts.{ctx_name}")

            for i, vdef in enumerate(adef.variants):
                vname = chr(ord('a') + i)  # a, b, c...
                _emit_def(vdef, f"actions.{action_name}.variants.{vname}")

        unconfigured = [
            a for a in ACTIONS
            if a in self._action_defs
            and not self._action_defs[a].files
            and not any(v.files for v in self._action_defs[a].postures.values())
        ]
        if unconfigured:
            lines.append("[action_aliases]\n")
            for name in unconfigured:
                lines.append(f'# {name} = "sit_idle"\n')
            lines.append("\n")

        with open(out_path, "w") as f:
            f.writelines(lines)

        QMessageBox.information(self, "Saved", f"Config saved to:\n{out_path}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("claudemeji animator")
    win = AnimatorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
