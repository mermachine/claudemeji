"""
sprite.py - spritesheet/individual-frame loader and animation player

ActionDef supports compound state resolution:
  - postures: dict of posture-name → ActionDef override
    e.g. react_good when sitting plays heart bubble instead of jump
  - contexts: dict of context-key → ActionDef override
    e.g. drag at restlessness 3 plays distressed dangle
  - previous: dict of previous-action-name → ActionDef override
    e.g. sit_idle coming from fall plays a bounce intro

Resolution order: context > previous > posture > self
"""

from __future__ import annotations
import os
import random
from PyQt6.QtGui import QPixmap, QPainter, QTransform
from PyQt6.QtCore import QTimer, QRect, Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget


class SpriteSheet:
    """Loads a spritesheet and slices it into frames by index."""

    def __init__(self, path: str, frame_width: int, frame_height: int):
        self.pixmap = QPixmap(path)
        if self.pixmap.isNull():
            raise FileNotFoundError(f"Could not load spritesheet: {path}")
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.cols = max(1, self.pixmap.width() // frame_width)

    def frame(self, index: int) -> QPixmap:
        row = index // self.cols
        col = index % self.cols
        rect = QRect(
            col * self.frame_width,
            row * self.frame_height,
            self.frame_width,
            self.frame_height,
        )
        return self.pixmap.copy(rect)


def _flip_pixmap(px: QPixmap) -> QPixmap:
    return px.transformed(QTransform().scale(-1, 1))


class ActionDef:
    """
    Definition of a single named action's animation.

    Compound state resolution via .resolve(posture, context, previous):
      context   - restlessness tier for drag variants (r0-r4)
      previous  - previous action name (for transition-specific variants)
      posture   - the physical posture at the time of the event (standing/sitting/etc.)
    Resolution order: context > previous > posture > self
    After resolution, if the result has variants, one is picked randomly (A/B selection).
    """

    def __init__(
        self,
        frames: list[int] | None = None,
        files: list[str] | None = None,
        fps: int = 8,
        loop: bool = True,
        next_action: str | None = None,  # legacy compat — ignored at runtime
        postures: dict[str, ActionDef] | None = None,
        contexts: dict[str, ActionDef] | None = None,
        previous: dict[str, ActionDef] | None = None,
        intro_files: list[str] | None = None,
        outro_files: list[str] | None = None,
        flip: bool = False,           # legacy compat — ignored at runtime
        min_restlessness: int = 0,    # for idle tiers: minimum restlessness to be eligible
        walk_speed: float = 0.0,      # nonzero = sprite moves while animating (px/tick)
        idle_tier: bool = False,      # if true, eligible for idle pool selection
        variants: list[ActionDef] | None = None,  # A/B variant alternatives
    ):
        self.frames = frames or []
        self.files = files or []
        self.fps = fps
        self.loop = loop
        self.flip = flip               # kept so old configs don't crash on load
        self.postures = postures or {}   # posture name → ActionDef
        self.contexts = contexts or {}   # context key → ActionDef
        self.previous = previous or {}   # previous action name → ActionDef
        self.intro_files = intro_files or []   # plays once before main loop
        self.outro_files = outro_files or []   # plays once after loop ends
        self.min_restlessness = min_restlessness
        self.walk_speed = walk_speed     # 0 = stationary, >0 = moves while playing
        self.idle_tier = idle_tier       # joins the idle pool when eligible
        self.variants = variants or []   # alternate animations picked randomly

    def frame_count(self) -> int:
        return len(self.files) if self.files else len(self.frames)

    def resolve(self, posture: str = "standing", context: str | None = None,
                previous: str | None = None) -> ActionDef:
        """Return the best ActionDef for the given state.
        Order: context > previous > posture > self, then A/B variant selection."""
        if context and context in self.contexts:
            result = self.contexts[context]
        elif previous and previous in self.previous:
            result = self.previous[previous]
        elif posture in self.postures:
            result = self.postures[posture]
        else:
            result = self
        # A/B variant: randomly pick from [result] + result.variants
        if result.variants:
            return random.choice([result] + result.variants)
        return result


class SpritePlayer(QWidget):
    """
    Renders the current animation frame.
    play() now accepts posture + context for compound state resolution.
    """

    drag_started      = pyqtSignal(object)
    drag_moved        = pyqtSignal(object)
    drag_released     = pyqtSignal(object)
    one_shot_finished = pyqtSignal()  # emitted when a non-looping action completes

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sheet: SpriteSheet | None = None
        self._img_dir: str = ""
        self._file_cache: dict[str, QPixmap] = {}
        self._scale: float = 1.0
        self._context_actions: list = []  # list of (label_or_callable, callback)
        self._actions: dict[str, ActionDef] = {}

        self._current_action_name: str = "sit_idle"  # canonical name (for uninterruptable checks)
        self._previous_action_name: str = "sit_idle" # action that was playing before current
        self._current_def: ActionDef | None = None    # resolved variant being played
        self._phase: str = "loop"   # "intro" | "loop" | "outro"
        self._frame_index: int = 0
        self._current_pixmap: QPixmap | None = None
        self._facing: str = "left"   # "left" or "right" — current facing direction
        self._native_facing: str = "left"  # which way sprites are drawn natively
        # queued transition: play this after the current outro finishes
        self._queued_action: tuple | None = None  # (action_name, posture, context)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance_frame)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    # --- setup ---

    def set_scale(self, factor: float):
        self._scale = factor
        self._file_cache.clear()  # invalidate cached pixmaps at old scale

    def set_image_dir(self, path: str):
        self._img_dir = path
        self._file_cache.clear()

    def load_sheet(self, path: str, frame_width: int, frame_height: int):
        self._sheet = SpriteSheet(path, frame_width, frame_height)
        self.resize(frame_width, frame_height)

    def register_action(self, name: str, action: ActionDef):
        self._actions[name] = action

    def set_facing(self, direction: str):
        """Set facing direction ("left" or "right"). Flips sprite when "left"."""
        if direction not in ("left", "right"):
            return
        if direction != self._facing:
            self._facing = direction
            self._file_cache.clear()  # cached pixmaps are pre-scaled but not pre-flipped
            self._update_pixmap()
            self.update()

    # --- playback ---

    def play(self, action_name: str, posture: str = "standing", context: str | None = None,
             force: bool = False):
        """
        Start playing an action.

        Soft transition (force=False, default):
          If the current action has outro_files and we are in the "loop" phase,
          queue the new action and let the outro play first.

        Hard cut (force=True):
          Immediately switch regardless of any pending outro.
        """
        if action_name not in self._actions:
            action_name = "sit_idle"

        base = self._actions[action_name]
        prev = self._current_action_name
        resolved = base.resolve(posture, context, previous=prev)

        # don't restart if already playing the same resolved variant (and no queued action)
        if (action_name == self._current_action_name
                and resolved is self._current_def
                and self._queued_action is None
                and not force):
            return

        # soft transition: if current loop has an outro, queue and let outro finish
        if (not force
                and self._current_def is not None
                and self._current_def.outro_files
                and self._phase == "loop"):
            self._queued_action = (action_name, posture, context)
            self._phase = "outro"
            self._frame_index = 0
            self._update_pixmap()
            self.update()
            self._restart_timer()
            return

        self._queued_action = None
        self._previous_action_name = self._current_action_name
        self._current_action_name = action_name
        self._current_def = resolved
        self._frame_index = 0
        self._phase = "intro" if resolved.intro_files else "loop"
        self._update_pixmap()
        self.update()
        self._restart_timer()

    def current_action(self) -> str:
        return self._current_action_name

    def current_def(self) -> ActionDef | None:
        return self._current_def

    def previous_action(self) -> str:
        return self._previous_action_name

    # --- internals ---

    def _phase_files(self) -> list[str]:
        """Return the frame list for the current phase."""
        d = self._current_def
        if d is None:
            return []
        if self._phase == "intro":
            return d.intro_files
        if self._phase == "outro":
            return d.outro_files
        return d.files if d.files else []

    def _restart_timer(self):
        files = self._phase_files()
        if files:
            interval = max(1, int(1000 / (self._current_def.fps if self._current_def else 8)))
            self._timer.start(interval)
        else:
            self._timer.stop()

    def _advance_frame(self):
        d = self._current_def
        if not d:
            return

        files = self._phase_files()
        if not files:
            return

        self._frame_index += 1
        if self._frame_index >= len(files):
            if self._phase == "intro":
                # intro done → start loop
                self._phase = "loop"
                self._frame_index = 0
            elif self._phase == "loop":
                if d.loop:
                    self._frame_index = 0
                else:
                    # one-shot done → outro or signal finished
                    if d.outro_files:
                        self._phase = "outro"
                        self._frame_index = 0
                    else:
                        self._frame_index = len(files) - 1
                        self._timer.stop()
                        QTimer.singleShot(0, self.one_shot_finished.emit)
                        return
            elif self._phase == "outro":
                # outro done → queued transition or signal finished
                self._frame_index = len(files) - 1
                self._timer.stop()
                if self._queued_action is not None:
                    queued = self._queued_action
                    self._queued_action = None
                    QTimer.singleShot(0, lambda q=queued: self.play(*q, force=True))
                else:
                    QTimer.singleShot(0, self.one_shot_finished.emit)
                return

        self._update_pixmap()
        self.update()

    def _update_pixmap(self):
        d = self._current_def
        if not d:
            return

        files = self._phase_files()
        if files:
            idx = min(self._frame_index, len(files) - 1)
            px = self._load_file(files[idx])
        elif self._sheet and d.frames:
            px = self._sheet.frame(d.frames[self._frame_index])
        else:
            return

        # flip when facing differs from native sprite direction
        if self._facing != self._native_facing:
            px = _flip_pixmap(px)

        self._current_pixmap = px
        if px and not px.isNull() and self.size() != px.size():
            self.resize(px.size())

    def _load_file(self, filename: str) -> QPixmap:
        if filename in self._file_cache:
            return self._file_cache[filename]
        path = os.path.join(self._img_dir, filename)
        px = QPixmap(path)
        if px.isNull():
            print(f"[claudemeji] warning: could not load {path}")
        elif self._scale != 1.0:
            w = int(px.width() * self._scale)
            h = int(px.height() * self._scale)
            px = px.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._file_cache[filename] = px
        return px

    # --- rendering ---

    def paintEvent(self, event):
        if not self._current_pixmap or self._current_pixmap.isNull():
            return
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.drawPixmap(0, 0, self._current_pixmap)

    # --- drag ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_started.emit(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.drag_moved.emit(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_released.emit(event.globalPosition().toPoint())

    def add_context_action(self, label, callback):
        """Add an item to the right-click menu. label can be a callable for dynamic text."""
        self._context_actions.append((label, callback))

    def contextMenuEvent(self, event):
        from PyQt6.QtWidgets import QMenu, QApplication
        menu = QMenu(self)
        for label_or_fn, callback in self._context_actions:
            label = label_or_fn() if callable(label_or_fn) else label_or_fn
            menu.addAction(label, callback)
        if self._context_actions:
            menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)
        menu.exec(event.globalPos())
