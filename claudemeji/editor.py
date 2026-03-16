"""
editor.py - sprite pack configuration editor

Lets users visually assign frames to each action and preview the result.
Saves to config.toml.  Can also load an existing config.toml to continue editing.

Layout:
  Left:   action list (dot = has frames)
  Center: variant selector + frame sequence + fps/loop/flip/next controls
  Right:  live preview (reuses SpritePlayer)
  Bottom: scrollable thumbnail grid of all frames in the pack

Variant selector appears when the selected action supports posture or context
overrides — e.g. drag shows "base | context: run | context: type | …" and
think/read/wait/react_good/sit_idle show "base | posture: sitting".
"""

from __future__ import annotations
import os
import sys

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton,
    QCheckBox, QComboBox, QScrollArea, QFileDialog, QSplitter,
    QFrame, QGridLayout, QSpinBox, QMessageBox,
    QToolButton,
)
from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QIcon

from claudemeji.state import ACTIONS
from claudemeji.sprite import ActionDef, SpritePlayer


# ── action metadata ───────────────────────────────────────────────────────────

ACTION_DESCRIPTIONS: dict[str, str] = {
    "walk_left":     "Walking left (idle locomotion)",
    "walk_right":    "Walking right (idle locomotion — usually flip=true)",
    "fall":          "Falling / being thrown",
    "climb":         "Climbing left wall",
    "climb_right":   "Climbing right wall (usually flip=true)",
    "ceiling":       "Crawling along ceiling — moving left",
    "ceiling_right": "Crawling along ceiling — moving right (usually flip=true)",
    "sit_idle":      "Standing/sitting still (default idle, always available)",
    "idle1":         "Idle tier 1 — configurable min_restlessness",
    "idle2":         "Idle tier 2 — configurable min_restlessness",
    "idle3":         "Idle tier 3 — configurable min_restlessness",
    "idle4":         "Idle tier 4 — configurable min_restlessness",
    "idle5":         "Idle tier 5 — configurable min_restlessness",
    "plan":          "Planning mode — EnterPlanMode tool",
    "think":         "Thinking — shown between tool calls",
    "read":          "Reading — Read, Grep, Glob, WebSearch tools",
    "type":          "Typing — Edit, Write tools",
    "run":           "Fast walk (restless locomotion variant)",
    "bash":          "Running a command — Bash tool",
    "wait":          "Waiting — long-running process",
    "react_good":    "Celebrate! — task success, session start",
    "react_bad":     "Oops! — tool error or denied",
    "drag":          "Being picked up / dragged",
    "subagent":      "Parent split animation — spawning a subagent (Agent/Task tools)",
    "spawned":       "Subagent entrance — jump up from parent, fall down",
}

# Posture variants available per action
ACTION_POSTURES: dict[str, list[str]] = {
    "sit_idle":   ["sitting"],
    "plan":       ["sitting"],
    "think":      ["sitting"],
    "read":       ["sitting"],
    "wait":       ["sitting"],
    "react_good": ["sitting"],
}

# Context variants available per action (drag context = restlessness tier)
ACTION_CONTEXTS: dict[str, list[str]] = {
    "drag": ["r0", "r1", "r2", "r3", "r4"],
}


THUMBNAIL_SIZE = 64
PREVIEW_SIZE   = 128


# ── small preview widget ──────────────────────────────────────────────────────

class PreviewPlayer(SpritePlayer):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.setStyleSheet("background: #1a1a2e; border: 1px solid #444;")

    def showEvent(self, event):
        # embedded widget — skip the floating-overlay setup from SpritePlayer
        super().showEvent(event)


# ── frame sequence strip ──────────────────────────────────────────────────────

class FrameSequenceWidget(QWidget):
    """Horizontal scrollable strip of the current frame list; click a frame to remove it."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filenames: list[str] = []
        self._img_dir: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        lbl = QLabel("Frame sequence  (click frames below to add, click here to remove)")
        lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(lbl)

        self._scroll = QScrollArea()
        self._scroll.setFixedHeight(THUMBNAIL_SIZE + 20)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setWidgetResizable(True)

        self._inner = QWidget()
        self._row = QHBoxLayout(self._inner)
        self._row.setContentsMargins(4, 4, 4, 4)
        self._row.setSpacing(4)
        self._row.addStretch()
        self._scroll.setWidget(self._inner)
        layout.addWidget(self._scroll)

        btn_clear = QPushButton("Clear sequence")
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(self.clear)
        layout.addWidget(btn_clear)

    def set_img_dir(self, path: str):
        self._img_dir = path

    def set_frames(self, filenames: list[str]):
        self._filenames = list(filenames)
        self._rebuild()

    def get_frames(self) -> list[str]:
        return list(self._filenames)

    def add_frame(self, filename: str):
        self._filenames.append(filename)
        self._rebuild()
        self.changed.emit()

    def clear(self):
        self._filenames.clear()
        self._rebuild()
        self.changed.emit()

    def _rebuild(self):
        while self._row.count() > 1:
            item = self._row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for i, fname in enumerate(self._filenames):
            frame = self._make_thumb(fname, i)
            self._row.insertWidget(i, frame)

    def _make_thumb(self, filename: str, index: int) -> QWidget:
        container = QWidget()
        container.setFixedSize(THUMBNAIL_SIZE + 4, THUMBNAIL_SIZE + 20)
        v = QVBoxLayout(container)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(1)

        btn = QToolButton()
        btn.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        btn.setToolTip(f"Click to remove {filename}")

        path = os.path.join(self._img_dir, filename)
        px = QPixmap(path)
        if not px.isNull():
            btn.setIcon(QIcon(px.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)))
            btn.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        else:
            btn.setText("?")

        idx = index
        btn.clicked.connect(lambda: self._remove(idx))
        v.addWidget(btn)

        lbl = QLabel(filename.replace(".png", ""))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #888; font-size: 9px;")
        v.addWidget(lbl)

        return container

    def _remove(self, index: int):
        if 0 <= index < len(self._filenames):
            self._filenames.pop(index)
            self._rebuild()
            self.changed.emit()


# ── pack thumbnail grid ───────────────────────────────────────────────────────

class PackThumbnailGrid(QWidget):
    """All PNGs in the pack; click to append to the current sequence."""

    frame_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._img_dir = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("All frames in pack  (click to add to sequence)")
        lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        layout.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(4)
        scroll.setWidget(self._grid_widget)
        layout.addWidget(scroll)

    def load(self, img_dir: str):
        self._img_dir = img_dir
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        pngs = sorted(
            f for f in os.listdir(img_dir)
            if f.lower().endswith(".png") and f != "icon.png"
        )

        cols = 8
        for i, fname in enumerate(pngs):
            btn = QToolButton()
            btn.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE + 16)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setText(fname.replace(".png", ""))
            btn.setToolTip(fname)

            path = os.path.join(img_dir, fname)
            px = QPixmap(path)
            if not px.isNull():
                btn.setIcon(QIcon(px.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
                btn.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))

            name = fname
            btn.clicked.connect(lambda checked=False, n=name: self.frame_clicked.emit(n))
            self._grid.addWidget(btn, i // cols, i % cols)


# ── main editor window ────────────────────────────────────────────────────────

class EditorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("claudemeji - sprite pack editor")
        self.resize(1100, 800)

        self._img_dir = ""
        self._pack_path = ""
        self._current_action = ACTIONS[0]
        self._current_variant = "base"  # "base" | "postures/<name>" | "contexts/<name>"

        # action name → ActionDef (including .postures / .contexts sub-defs)
        self._action_defs: dict[str, ActionDef] = {
            a: ActionDef(files=[], fps=8, loop=True, next_action="sit_idle")
            for a in ACTIONS
        }

        self._build_ui()
        self._apply_style()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ── top bar ──
        top = QHBoxLayout()
        self._pack_label = QLabel("No pack loaded")
        self._pack_label.setStyleSheet("color: #ccc;")
        btn_open = QPushButton("Open pack folder…")
        btn_open.clicked.connect(self._open_pack)
        btn_load = QPushButton("Load config.toml…")
        btn_load.clicked.connect(self._load_config_file)
        btn_save = QPushButton("Save config.toml")
        btn_save.clicked.connect(self._save_config)
        top.addWidget(self._pack_label, 1)
        top.addWidget(btn_open)
        top.addWidget(btn_load)
        top.addWidget(btn_save)
        root.addLayout(top)

        # ── main horizontal splitter: [action list | center panel | preview] ──
        main_split = QSplitter(Qt.Orientation.Horizontal)

        # action list
        action_panel = QWidget()
        al = QVBoxLayout(action_panel)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(QLabel("Actions"))
        self._action_list = QListWidget()
        self._action_list.setFixedWidth(160)
        for a in ACTIONS:
            item = QListWidgetItem(a)
            item.setToolTip(ACTION_DESCRIPTIONS.get(a, ""))
            self._action_list.addItem(item)
        self._action_list.setCurrentRow(0)
        self._action_list.currentTextChanged.connect(self._on_action_selected)
        al.addWidget(self._action_list)
        main_split.addWidget(action_panel)

        # center panel
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(4, 0, 4, 0)
        cl.setSpacing(6)

        # action description banner
        self._action_desc = QLabel()
        self._action_desc.setWordWrap(True)
        self._action_desc.setStyleSheet(
            "color: #aaa; font-size: 11px; padding: 4px 6px;"
            "background: #1a1a2e; border-radius: 3px;"
        )
        cl.addWidget(self._action_desc)

        # variant selector row (hidden when action has no variants)
        variant_row = QHBoxLayout()
        self._variant_label = QLabel("Variant:")
        self._variant_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self._variant_combo = QComboBox()
        self._variant_combo.setMinimumWidth(180)
        self._variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        variant_row.addWidget(self._variant_label)
        variant_row.addWidget(self._variant_combo)
        variant_row.addStretch()
        cl.addLayout(variant_row)

        # frame sequence strip
        self._sequence = FrameSequenceWidget()
        self._sequence.changed.connect(self._on_sequence_changed)
        cl.addWidget(self._sequence)

        # controls row: fps · loop · flip · next_action
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("fps:"))
        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(8)
        self._fps_spin.valueChanged.connect(self._on_controls_changed)
        ctrl.addWidget(self._fps_spin)

        self._loop_check = QCheckBox("loop")
        self._loop_check.setChecked(True)
        self._loop_check.stateChanged.connect(self._on_controls_changed)
        ctrl.addWidget(self._loop_check)

        self._flip_check = QCheckBox("flip")
        self._flip_check.setChecked(False)
        self._flip_check.setToolTip("Mirror sprite horizontally (for right-facing variants)")
        self._flip_check.stateChanged.connect(self._on_controls_changed)
        ctrl.addWidget(self._flip_check)

        # min_restlessness (visible for idle* actions)
        self._min_rest_label = QLabel("min restlessness:")
        self._min_rest_label.setStyleSheet("color: #888; font-size: 11px;")
        ctrl.addWidget(self._min_rest_label)
        self._min_rest_spin = QSpinBox()
        self._min_rest_spin.setRange(0, 4)
        self._min_rest_spin.setValue(0)
        self._min_rest_spin.setToolTip("Minimum restlessness level to include this idle in the pool")
        self._min_rest_spin.valueChanged.connect(self._on_controls_changed)
        ctrl.addWidget(self._min_rest_spin)

        ctrl.addStretch()
        cl.addLayout(ctrl)

        # physics row (window_pull_distance)
        phys_row = QHBoxLayout()
        phys_lbl = QLabel("window_pull_distance:")
        phys_lbl.setStyleSheet("color: #888; font-size: 11px;")
        phys_lbl.setToolTip(
            "How far (px) sprite weight pulls windows down when standing on them.\n"
            "0 = disabled. Saved to [physics] in config.toml."
        )
        self._window_pull_spin = QSpinBox()
        self._window_pull_spin.setRange(0, 200)
        self._window_pull_spin.setValue(0)
        self._window_pull_spin.setToolTip(phys_lbl.toolTip())
        phys_row.addWidget(phys_lbl)
        phys_row.addWidget(self._window_pull_spin)
        phys_row.addStretch()
        cl.addLayout(phys_row)

        cl.addStretch()
        main_split.addWidget(center)

        # preview panel
        preview_panel = QWidget()
        preview_panel.setFixedWidth(PREVIEW_SIZE + 24)
        pv = QVBoxLayout(preview_panel)
        pv.setContentsMargins(4, 0, 4, 0)
        pv.addWidget(QLabel("Preview"))
        self._preview = PreviewPlayer()
        pv.addWidget(self._preview)
        self._preview_status = QLabel("no frames")
        self._preview_status.setStyleSheet("color: #888; font-size: 10px;")
        pv.addWidget(self._preview_status)

        # transition from: lets you test outro → intro flows
        pv.addWidget(QLabel(""))  # spacer
        trans_lbl = QLabel("Transition from:")
        trans_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        pv.addWidget(trans_lbl)
        self._from_combo = QComboBox()
        self._from_combo.addItem("(none)")
        self._from_combo.addItems(ACTIONS)
        self._from_combo.currentTextChanged.connect(self._on_transition_from_changed)
        pv.addWidget(self._from_combo)
        self._prev_label = QLabel("Previous: —")
        self._prev_label.setStyleSheet("color: #666; font-size: 10px;")
        pv.addWidget(self._prev_label)

        pv.addStretch()
        main_split.addWidget(preview_panel)

        main_split.setStretchFactor(0, 0)
        main_split.setStretchFactor(1, 1)
        main_split.setStretchFactor(2, 0)
        root.addWidget(main_split, 1)

        # bottom: pack thumbnail grid
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #444;")
        root.addWidget(sep)

        self._thumb_grid = PackThumbnailGrid()
        self._thumb_grid.setFixedHeight(THUMBNAIL_SIZE + 80)
        self._thumb_grid.frame_clicked.connect(self._on_frame_clicked)
        root.addWidget(self._thumb_grid)

        # init variant selector for first action
        self._update_variant_selector(self._current_action)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #12121f; color: #ddd; }
            QListWidget { background: #1a1a2e; border: 1px solid #333; }
            QListWidget::item:selected { background: #2a2a5e; }
            QPushButton { background: #2a2a5e; border: 1px solid #555;
                          padding: 4px 10px; border-radius: 3px; }
            QPushButton:hover { background: #3a3a7e; }
            QToolButton { background: #1a1a2e; border: 1px solid #333;
                          font-size: 9px; color: #aaa; }
            QToolButton:hover { background: #2a2a5e; border-color: #7a7aff; }
            QScrollArea { border: none; }
            QSpinBox, QComboBox { background: #1a1a2e; border: 1px solid #444;
                                  padding: 2px; color: #ddd; }
            QCheckBox { color: #ddd; }
            QLabel { color: #ddd; }
            QSplitter::handle { background: #333; }
        """)

    # ── variant helpers ───────────────────────────────────────────────────────

    def _variant_keys(self, action_name: str) -> list[str]:
        """Return all variant keys for an action: ["base", "postures/sitting", ...]"""
        keys = ["base"]
        for p in ACTION_POSTURES.get(action_name, []):
            keys.append(f"postures/{p}")
        for c in ACTION_CONTEXTS.get(action_name, []):
            keys.append(f"contexts/{c}")
        # previous variants: all other actions as options
        for a in ACTIONS:
            if a != action_name:
                keys.append(f"previous/{a}")
        return keys

    def _variant_label_text(self, key: str) -> str:
        if key == "base":
            return "base"
        kind, name = key.split("/", 1)
        if kind == "previous":
            return f"from: {name}"
        return f"{kind[:-1]}: {name}"  # "postures/sitting" → "posture: sitting"

    def _update_variant_selector(self, action_name: str):
        """Rebuild the variant combo for the given action; preserve current variant if valid."""
        keys = self._variant_keys(action_name)
        has_variants = len(keys) > 1

        self._variant_label.setVisible(has_variants)
        self._variant_combo.setVisible(has_variants)

        self._variant_combo.blockSignals(True)
        self._variant_combo.clear()
        for k in keys:
            self._variant_combo.addItem(self._variant_label_text(k), k)

        # keep current variant if it's valid for this action, else fall back to base
        if self._current_variant in keys:
            self._variant_combo.setCurrentIndex(keys.index(self._current_variant))
        else:
            self._current_variant = "base"
            self._variant_combo.setCurrentIndex(0)

        self._variant_combo.blockSignals(False)

    def _get_variant_def(self, action_name: str, variant: str) -> ActionDef:
        """Return the ActionDef for the given variant key."""
        base = self._action_defs[action_name]
        if variant == "base":
            return base
        kind, name = variant.split("/", 1)
        if kind == "postures":
            return base.postures.get(name, ActionDef(files=[], fps=8, loop=True))
        if kind == "contexts":
            return base.contexts.get(name, ActionDef(files=[], fps=8, loop=True))
        if kind == "previous":
            return base.previous.get(name, ActionDef(files=[], fps=8, loop=True))
        return base

    # ── interactions ──────────────────────────────────────────────────────────

    def _open_pack(self):
        folder = QFileDialog.getExistingDirectory(self, "Select sprite pack folder")
        if not folder:
            return

        img_dir = os.path.join(folder, "img")
        if not os.path.isdir(img_dir):
            img_dir = folder

        self._pack_path = folder
        self._img_dir = img_dir
        self._pack_label.setText(f"Pack: {folder}")

        self._sequence.set_img_dir(img_dir)
        self._thumb_grid.load(img_dir)
        self._preview.set_image_dir(img_dir)

        # reset defs when loading a new pack
        self._action_defs = {
            a: ActionDef(files=[], fps=8, loop=True, next_action="sit_idle")
            for a in ACTIONS
        }
        self._current_variant = "base"
        self._load_action_into_ui(self._current_action)

    def _load_config_file(self):
        """Load an existing config.toml — populates all action defs including variants."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open config.toml",
            os.path.expanduser("~/.claudemeji"),
            "TOML files (*.toml)"
        )
        if not path:
            return

        # parse with tomllib/tomli
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

        # load pack path — offer to open the folder if not already loaded
        pack_data = data.get("sprite_pack", {})
        pack_path = os.path.expanduser(pack_data.get("path", ""))
        img_subdir = pack_data.get("img_dir", "")

        if pack_path:
            img_dir = os.path.join(pack_path, img_subdir) if img_subdir else pack_path
            if os.path.isdir(img_dir):
                self._pack_path = pack_path
                self._img_dir = img_dir
                self._pack_label.setText(f"Pack: {pack_path}")
                self._sequence.set_img_dir(img_dir)
                self._thumb_grid.load(img_dir)
                self._preview.set_image_dir(img_dir)
            else:
                QMessageBox.warning(
                    self, "Pack folder not found",
                    f"Sprite pack not found at:\n{img_dir}\n\n"
                    "Use 'Open pack folder…' to locate it manually."
                )

        # parse action defs (reuse config.py's parser)
        from claudemeji.config import _parse_action_def
        self._action_defs = {
            a: ActionDef(files=[], fps=8, loop=True, next_action="sit_idle")
            for a in ACTIONS
        }
        for name, adef_raw in data.get("actions", {}).items():
            if name in self._action_defs:
                self._action_defs[name] = _parse_action_def(adef_raw)

        # load physics
        physics_data = data.get("physics", {})
        self._window_pull_spin.setValue(physics_data.get("window_pull_distance", 0))

        self._current_variant = "base"
        self._load_action_into_ui(self._current_action)
        self._refresh_action_list()

    def _on_action_selected(self, action_name: str):
        if not action_name:
            return
        action_name = action_name.rstrip(" ●")
        self._save_current_action()
        self._current_action = action_name
        self._current_variant = "base"   # reset to base when switching actions
        self._load_action_into_ui(action_name)

    def _on_variant_changed(self, index: int):
        if index < 0:
            return
        self._save_current_action()
        self._current_variant = self._variant_combo.itemData(index)
        self._populate_controls_from_variant()
        self._update_preview()

    def _load_action_into_ui(self, action_name: str):
        desc = ACTION_DESCRIPTIONS.get(action_name, "")
        self._action_desc.setText(f"<b>{action_name}</b>  —  {desc}")
        self._update_variant_selector(action_name)
        # show min_restlessness only for idle tier actions
        is_idle_tier = action_name.startswith("idle") and action_name[4:].isdigit()
        self._min_rest_label.setVisible(is_idle_tier)
        self._min_rest_spin.setVisible(is_idle_tier)
        self._populate_controls_from_variant()
        self._update_preview()

    def _populate_controls_from_variant(self):
        """Fill sequence + controls widgets from the current action+variant def."""
        adef = self._get_variant_def(self._current_action, self._current_variant)

        self._sequence.set_frames(adef.files)

        self._fps_spin.blockSignals(True)
        self._fps_spin.setValue(adef.fps)
        self._fps_spin.blockSignals(False)

        self._loop_check.blockSignals(True)
        self._loop_check.setChecked(adef.loop)
        self._loop_check.blockSignals(False)

        self._flip_check.blockSignals(True)
        self._flip_check.setChecked(adef.flip)
        self._flip_check.blockSignals(False)

        self._min_rest_spin.blockSignals(True)
        self._min_rest_spin.setValue(adef.min_restlessness)
        self._min_rest_spin.blockSignals(False)

    def _save_current_action(self):
        """Write the current UI state back into _action_defs for the active action+variant."""
        new_def = ActionDef(
            files=self._sequence.get_frames(),
            fps=self._fps_spin.value(),
            loop=self._loop_check.isChecked(),
            flip=self._flip_check.isChecked(),
            min_restlessness=self._min_rest_spin.value(),
        )

        base = self._action_defs[self._current_action]

        if self._current_variant == "base":
            # Replace base but preserve sub-defs
            self._action_defs[self._current_action] = ActionDef(
                files=new_def.files,
                fps=new_def.fps,
                loop=new_def.loop,
                flip=new_def.flip,
                postures=base.postures,
                contexts=base.contexts,
                previous=base.previous,
                min_restlessness=new_def.min_restlessness,
            )
        else:
            kind, name = self._current_variant.split("/", 1)
            if kind == "postures":
                base.postures[name] = new_def
            elif kind == "contexts":
                base.contexts[name] = new_def
            elif kind == "previous":
                base.previous[name] = new_def

    def _on_frame_clicked(self, filename: str):
        self._sequence.add_frame(filename)

    def _on_sequence_changed(self):
        self._save_current_action()
        self._update_preview()
        self._refresh_action_list()

    def _on_controls_changed(self):
        self._save_current_action()
        self._update_preview()

    def _update_preview(self):
        if not self._img_dir:
            return
        adef = self._get_variant_def(self._current_action, self._current_variant)
        if not adef.files:
            self._preview_status.setText("no frames")
            return

        self._preview.register_action("_preview", adef)
        self._preview.play("_preview")
        n = len(adef.files)
        self._preview_status.setText(
            f"{n} frame{'s' if n != 1 else ''}  "
            f"@ {adef.fps}fps  {'loop' if adef.loop else 'once'}"
            + ("  [flip]" if adef.flip else "")
        )
        self._prev_label.setText(f"Previous: {self._preview.previous_action()}")

    def _on_transition_from_changed(self, from_action: str):
        """Play the 'from' action briefly, then soft-transition to current action."""
        if not self._img_dir or from_action == "(none)":
            self._update_preview()
            return
        from_def = self._action_defs.get(from_action)
        if not from_def or not from_def.files:
            self._update_preview()
            return
        # register both actions so the preview player can transition between them
        self._preview.register_action("_from", from_def)
        adef = self._get_variant_def(self._current_action, self._current_variant)
        self._preview.register_action("_preview", adef)
        self._preview.play("_from", force=True)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(1500, self._update_preview)

    def _refresh_action_list(self):
        """Put a dot next to actions that have any frames configured (base or variant)."""
        for i in range(self._action_list.count()):
            item = self._action_list.item(i)
            name = item.text().rstrip(" ●")
            adef = self._action_defs.get(name)
            has_frames = bool(
                adef and (
                    adef.files
                    or any(v.files for v in adef.postures.values())
                    or any(v.files for v in adef.contexts.values())
                )
            )
            item.setText(f"{name} ●" if has_frames else name)

    # ── save ─────────────────────────────────────────────────────────────────

    def _save_config(self):
        self._save_current_action()

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
            "# claudemeji config - generated by sprite pack editor\n",
            "\n",
        ]

        # [physics] — only if non-default
        pull_dist = self._window_pull_spin.value()
        if pull_dist != 0:
            lines += [
                "[physics]\n",
                f"window_pull_distance = {pull_dist}\n",
                "\n",
            ]

        lines += [
            "[sprite_pack]\n",
            f'path = "{self._pack_path}"\n',
        ]
        if img_subdir:
            lines.append(f'img_dir = "{img_subdir}"\n')
        lines.append("\n")

        def _emit_def(adef: ActionDef, section: str) -> None:
            """Append a TOML section for a single ActionDef (no sub-tables)."""
            if not adef.files:
                return
            lines.append(f"[{section}]\n")
            files_str = ", ".join(f'"{f}"' for f in adef.files)
            lines.append(f"files = [{files_str}]\n")
            lines.append(f"fps = {adef.fps}\n")
            lines.append(f"loop = {'true' if adef.loop else 'false'}\n")
            if adef.flip:
                lines.append("flip = true\n")
            if adef.min_restlessness > 0:
                lines.append(f"min_restlessness = {adef.min_restlessness}\n")
            lines.append("\n")

        # emit all actions (base + posture variants + context variants)
        for action_name in ACTIONS:
            adef = self._action_defs[action_name]
            base_has  = bool(adef.files)
            var_has   = (
                any(v.files for v in adef.postures.values()) or
                any(v.files for v in adef.contexts.values()) or
                any(v.files for v in adef.previous.values())
            )
            if not base_has and not var_has:
                continue

            _emit_def(adef, f"actions.{action_name}")

            for posture_name, pdef in adef.postures.items():
                _emit_def(pdef, f"actions.{action_name}.postures.{posture_name}")

            for ctx_name, cdef in adef.contexts.items():
                _emit_def(cdef, f"actions.{action_name}.contexts.{ctx_name}")

            for prev_name, prev_def in adef.previous.items():
                _emit_def(prev_def, f"actions.{action_name}.previous.{prev_name}")

        # commented action_aliases for anything left unconfigured
        unconfigured = [
            a for a in ACTIONS
            if not self._action_defs[a].files
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
    app.setApplicationName("claudemeji editor")
    win = EditorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
