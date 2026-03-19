"""
main.py - entry point, wires everything together

Animation resolution uses compound state:
  posture  - what she's physically doing (from PhysicsEngine)
  context  - for drag: restlessness-tier variant (r0-r4)

_play(action, force=False) resolves the right ActionDef variant and calls
player.play(name, posture, context).
"""
from __future__ import annotations

import sys
import os
import argparse
import random
import subprocess
import threading
import queue
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from claudemeji.config import load as load_config, Config
from claudemeji.sprite import SpritePlayer
from claudemeji.physics import PhysicsEngine
from claudemeji.state import StateMachine
from claudemeji.watcher import HookWatcher
from claudemeji.platform_utils import apply_macos_window_fixes, set_window_floating, set_window_above
from claudemeji.windows import get_window_infos, get_platform_tuples, is_available as windows_available
from claudemeji.restlessness import RestlessnessEngine
import claudemeji.window_wrangler as _wrangler

REACTION_LOCK_MS = 4000
TOOL_SAFETY_LOCK_MS = 30000
UNINTERRUPTABLE = {"fall", "drag", "react_good", "react_bad", "subagent", "spawned",
                   "jump", "window_throw", "window_carry_cheer", "trip"}
# actions that always hard-cut, never queue behind an outro
FORCE_ACTIONS = {"drag", "fall", "react_bad"}


# --- threaded AX helper ---

_ax_skip_count = 0
_ax_queue = queue.Queue()
_ax_worker_running = False

def _ax_worker():
    """Single persistent worker thread for AX API calls."""
    while True:
        fn, args = _ax_queue.get()
        if fn is None:
            break
        try:
            result = fn(*args)
            if fn.__name__ not in ("move_window_by", "move_window_to"):
                print(f"[claudemeji] AX {fn.__name__} → {result}")
        except Exception as e:
            print(f"[claudemeji] AX error in {fn.__name__}: {e}")

def _ensure_ax_worker():
    global _ax_worker_running
    if not _ax_worker_running:
        _ax_worker_running = True
        t = threading.Thread(target=_ax_worker, daemon=True)
        t.start()

def _ax_threaded(fn, *args):
    """Queue an AX API call for the worker thread (avoids blocking the UI)."""
    global _ax_skip_count
    if not _wrangler.is_trusted():
        _ax_skip_count += 1
        if _ax_skip_count <= 3 or _ax_skip_count % 50 == 0:
            print(f"[claudemeji] AX call skipped — no Accessibility permission ({_ax_skip_count} total skips)")
        return
    _ax_skip_count = 0
    _ensure_ax_worker()
    # for per-tick moves, drop stale entries (only latest position matters)
    if fn.__name__ in ("move_window_to", "move_window_by"):
        # drain any pending move calls — only the latest matters
        drained = 0
        while not _ax_queue.empty():
            try:
                peek_fn, _ = _ax_queue.get_nowait()
                if peek_fn.__name__ not in ("move_window_to", "move_window_by"):
                    # put non-move calls back... actually just drop, they're stale too
                    pass
                drained += 1
            except queue.Empty:
                break
    _ax_queue.put((fn, args))


# --- idle / drag resolution ---

def _resolve_idle(config: Config | None, restlessness: int) -> str:
    """Pick an idle action from the pool based on restlessness level."""
    if not config:
        return "sit_idle"
    pool = ["sit_idle", "stand"]
    for name, adef in config.actions.items():
        is_numbered_idle = name.startswith("idle") and name[4:].isdigit()
        if (is_numbered_idle or adef.idle_tier) and adef.min_restlessness <= restlessness:
            pool.append(name)
    return random.choice(pool)


def _resolve_drag_context(config: Config | None, restlessness: int,
                          intensity: str = "calm") -> str | None:
    """Pick drag context: try r{level}_{intensity}, fall back to r{level}, then base.
    Two axes: restlessness picks the anger tier, intensity picks the dangle variant."""
    if not config:
        return None
    base = config.actions.get("drag")
    if not base:
        return None
    # try most specific first, then fall back
    for lvl in range(restlessness, -1, -1):
        if intensity != "calm":
            key = f"r{lvl}_{intensity}"
            if key in base.contexts:
                return key
        key = f"r{lvl}"
        if key in base.contexts:
            return key
    # try bare intensity (no restlessness tier)
    if intensity != "calm" and intensity in base.contexts:
        return intensity
    return None


# --- tray icon ---

def _make_mushroom_icon():
    """Draw a bold mushroom silhouette for the system tray.
    Menu bar icons need to be simple, high-contrast glyphs —
    no fine detail, just recognizable shape at 22px."""
    from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QPainterPath
    from PyQt6.QtCore import Qt, QPointF

    size = 44  # 2x for retina, renders at 22pt
    px = QPixmap(size, size)
    px.fill(QColor(0, 0, 0, 0))

    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    fg = QColor(240, 240, 240)  # white-ish, standard for dark menu bars

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(fg))

    # stem: solid tapered rectangle
    stem = QPainterPath()
    stem.moveTo(16, 26)
    stem.lineTo(28, 26)
    stem.lineTo(27, 39)
    stem.quadTo(22, 42, 17, 39)
    stem.closeSubpath()
    p.drawPath(stem)

    # cap: big solid dome, the main recognizable shape
    cap = QPainterPath()
    cap.moveTo(4, 27)
    cap.quadTo(4, 7, 22, 5)
    cap.quadTo(40, 7, 40, 27)
    cap.closeSubpath()
    p.drawPath(cap)

    # two spots punched out of the cap (transparent holes = visual detail)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    p.drawEllipse(QPointF(15, 17), 3.5, 3.5)
    p.drawEllipse(QPointF(28, 13), 3, 3)

    p.end()
    return px


# --- debug panel ---

# keep a reference so the non-modal dialog doesn't get garbage collected
_debug_dialog = None


def _show_debug_panel(player: SpritePlayer, physics: PhysicsEngine,
                      restless: RestlessnessEngine, play_fn, posture_ref: list):
    """Non-modal debug panel with live state, controls, and targeted actions."""
    global _debug_dialog
    if _debug_dialog is not None:
        _debug_dialog.raise_()
        _debug_dialog.activateWindow()
        return

    from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                                 QSlider, QLabel, QPushButton, QComboBox,
                                 QGroupBox, QRadioButton, QButtonGroup,
                                 QFrame, QScrollArea, QWidget)
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont
    from claudemeji.physics import _plat_rect, _plat_pid, _plat_zidx

    mono = QFont("Menlo, Monaco, Courier New")
    mono.setPointSize(11)

    dialog = QDialog()
    dialog.setWindowTitle("claudemeji debug")
    dialog.setMinimumWidth(380)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def on_close():
        global _debug_dialog
        _debug_dialog = None
    dialog.destroyed.connect(on_close)

    main_layout = QVBoxLayout()
    main_layout.setSpacing(6)
    main_layout.setContentsMargins(10, 10, 10, 10)

    # =============================================
    # 1. LIVE STATE MONITOR
    # =============================================
    state_group = QGroupBox("State")
    state_grid = QGridLayout()
    state_grid.setSpacing(2)

    state_labels = {}
    fields = [
        ("action",    "Action"),
        ("posture",   "Posture"),
        ("physics",   "Physics"),
        ("facing",    "Facing"),
        ("pos",       "Position"),
        ("vel",       "Velocity"),
        ("z",         "Z-order"),
        ("locked",    "Locked"),
        ("platforms", "Platforms"),
        ("standing",  "Standing on"),
    ]
    for i, (key, label) in enumerate(fields):
        name_lbl = QLabel(f"{label}:")
        name_lbl.setFont(mono)
        name_lbl.setStyleSheet("color: #888;")
        val_lbl = QLabel("—")
        val_lbl.setFont(mono)
        val_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        state_grid.addWidget(name_lbl, i, 0)
        state_grid.addWidget(val_lbl, i, 1)
        state_labels[key] = val_lbl

    state_group.setLayout(state_grid)
    main_layout.addWidget(state_group)

    def _standing_on_text():
        plat = physics._pull.standing_on
        if plat is None:
            return "screen floor"
        # find the app name from window infos
        pid = _plat_pid(plat)
        z = _plat_zidx(plat)
        try:
            infos = get_window_infos(own_pid=os.getpid())
            for info in infos:
                if info.pid == pid:
                    name = info.name or "?"
                    title = (info.title[:20] + "...") if len(info.title) > 20 else info.title
                    return f"{name} z={z}" + (f" [{title}]" if title else "")
        except Exception:
            pass
        return f"pid={pid} z={z}"

    def refresh_state():
        pos = player.pos()
        state_labels["action"].setText(player.current_action())
        state_labels["posture"].setText(posture_ref[0])
        state_labels["physics"].setText(physics._state.name)
        state_labels["facing"].setText(physics._facing)
        state_labels["pos"].setText(f"{pos.x()}, {pos.y()}")
        state_labels["vel"].setText(f"{physics._vel.x:.1f}, {physics._vel.y:.1f}")
        if physics._z_window_number == 0:
            state_labels["z"].setText("floating")
            state_labels["z"].setStyleSheet("color: #4a9;")
        else:
            state_labels["z"].setText(f"win #{physics._z_window_number} (z={physics._z_index})")
            state_labels["z"].setStyleSheet("color: #c84;")
        state_labels["locked"].setText("yes" if physics._event_locked else "no")
        state_labels["locked"].setStyleSheet("color: #c44;" if physics._event_locked else "")
        state_labels["platforms"].setText(str(len(physics._platforms)))
        state_labels["standing"].setText(_standing_on_text())

    dialog._refresh_timer = QTimer()
    dialog._refresh_timer.setInterval(250)
    dialog._refresh_timer.timeout.connect(refresh_state)
    dialog._refresh_timer.start()
    refresh_state()

    # =============================================
    # 2. CONTROLS
    # =============================================
    ctrl_group = QGroupBox("Controls")
    ctrl_layout = QVBoxLayout()
    ctrl_layout.setSpacing(6)

    # pause / resume
    paused = [False]
    pause_btn = QPushButton("Pause physics")
    def toggle_pause():
        if paused[0]:
            physics.start()
            pause_btn.setText("Pause physics")
        else:
            physics.stop()
            pause_btn.setText("Resume physics")
        paused[0] = not paused[0]
    pause_btn.clicked.connect(toggle_pause)
    ctrl_layout.addWidget(pause_btn)

    # restlessness
    rest_row = QHBoxLayout()
    rest_label = QLabel(f"Restlessness: {restless.level}")
    rest_slider = QSlider(Qt.Orientation.Horizontal)
    rest_slider.setRange(0, 4)
    rest_slider.setValue(restless.level)
    rest_slider.valueChanged.connect(lambda v: (rest_label.setText(f"Restlessness: {v}"),
                                                restless._set_level(v)))
    restless.level_changed.connect(lambda v: (rest_label.setText(f"Restlessness: {v}"),
                                              rest_slider.setValue(v)))
    rest_row.addWidget(rest_label)
    rest_row.addWidget(rest_slider)
    ctrl_layout.addLayout(rest_row)

    # animation picker
    anim_row = QHBoxLayout()
    anim_combo = QComboBox()
    action_names = sorted(player._actions.keys())
    anim_combo.addItems(action_names)
    if player.current_action() in action_names:
        anim_combo.setCurrentText(player.current_action())
    def debug_play_anim():
        physics.lock_for_event()
        play_fn(anim_combo.currentText(), force=True)
        QTimer.singleShot(5000, physics.unlock)
    anim_play_btn = QPushButton("Play anim")
    anim_play_btn.setToolTip("Play animation only (locks physics)")
    anim_play_btn.clicked.connect(debug_play_anim)
    anim_row.addWidget(anim_combo)
    anim_row.addWidget(anim_play_btn)
    ctrl_layout.addLayout(anim_row)

    # offset sliders
    offset_row = QHBoxLayout()
    offset_row.addWidget(QLabel("Offset:"))
    offset_sliders = {}
    for axis, getter, setter_fn in [
        ("X", lambda: physics._offset.x, lambda v: physics.set_offset(float(v), physics._offset.y)),
        ("Y", lambda: physics._offset.y, lambda v: physics.set_offset(physics._offset.x, float(v))),
    ]:
        lbl = QLabel(f"{axis}:")
        lbl.setFixedWidth(16)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-25, 25)
        slider.setValue(int(getter()))
        val_lbl = QLabel(str(int(getter())))
        val_lbl.setFixedWidth(28)
        slider.valueChanged.connect(lambda v, l=val_lbl, fn=setter_fn: (l.setText(str(v)), fn(v)))
        offset_row.addWidget(lbl)
        offset_row.addWidget(slider)
        offset_row.addWidget(val_lbl)
        offset_sliders[axis] = slider
    reset_btn = QPushButton("0")
    reset_btn.setFixedWidth(24)
    reset_btn.setToolTip("Reset offset")
    reset_btn.clicked.connect(lambda: (offset_sliders["X"].setValue(0),
                                       offset_sliders["Y"].setValue(0),
                                       physics.set_offset(0, 0)))
    offset_row.addWidget(reset_btn)
    ctrl_layout.addLayout(offset_row)

    ctrl_group.setLayout(ctrl_layout)
    main_layout.addWidget(ctrl_group)

    # =============================================
    # 3. ACTIONS
    # =============================================
    actions_group = QGroupBox("Actions")
    actions_layout = QVBoxLayout()
    actions_layout.setSpacing(6)

    # --- movement (no target) ---
    move_label = QLabel("Movement:")
    move_label.setStyleSheet("font-weight: bold;")
    actions_layout.addWidget(move_label)

    move_row1 = QHBoxLayout()
    fall_btn = QPushButton("Fall")
    fall_btn.clicked.connect(physics._start_falling)
    jump_btn = QPushButton("Jump random")
    def jump_random():
        screen = physics._screen_rect()
        tx = random.randint(screen.left() + 100, screen.right() - 100)
        ty = random.randint(screen.top(), screen.bottom() - 200)
        physics.jump_toward(float(tx), float(ty))
    jump_btn.clicked.connect(jump_random)
    move_row1.addWidget(fall_btn)
    move_row1.addWidget(jump_btn)
    actions_layout.addLayout(move_row1)

    move_row2 = QHBoxLayout()
    for label, direction, sprint in [
        ("Walk L", -1, False), ("Walk R", 1, False),
        ("Sprint L", -1, True), ("Sprint R", 1, True),
    ]:
        btn = QPushButton(label)
        btn.clicked.connect(lambda checked=False, d=direction, s=sprint:
                            physics._start_walking(d, sprint=s))
        move_row2.addWidget(btn)
    actions_layout.addLayout(move_row2)

    # --- separator ---
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFrameShadow(QFrame.Shadow.Sunken)
    actions_layout.addWidget(sep)

    # --- window-targeted actions ---
    target_label = QLabel("Window target:")
    target_label.setStyleSheet("font-weight: bold;")
    actions_layout.addWidget(target_label)

    # window picker
    win_row = QHBoxLayout()
    win_combo = QComboBox()
    win_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    _window_infos = []  # stash for lookup

    def refresh_windows():
        nonlocal _window_infos
        win_combo.clear()
        try:
            all_infos = get_window_infos(own_pid=os.getpid())
            # filter to windows on miku's current screen + exclude parent process
            screen = physics._screen_rect()
            _window_infos = [info for info in all_infos
                             if info.rect.intersects(screen) and info.pid != os.getppid()]
            for info in _window_infos:
                title = (info.title[:25] + "..") if len(info.title) > 25 else (info.title or "—")
                label = f"z{info.z_index} {info.name}: {title}"
                win_combo.addItem(label)
        except Exception as e:
            win_combo.addItem(f"(error: {e})")
            _window_infos = []

    refresh_windows()
    refresh_win_btn = QPushButton("Refresh")
    refresh_win_btn.setFixedWidth(60)
    refresh_win_btn.clicked.connect(refresh_windows)
    win_row.addWidget(win_combo, 1)
    win_row.addWidget(refresh_win_btn)
    actions_layout.addLayout(win_row)

    # corner picker
    corner_row = QHBoxLayout()
    corner_row.addWidget(QLabel("Corner:"))
    corner_left = QRadioButton("Left")
    corner_right = QRadioButton("Right")
    corner_left.setChecked(True)
    corner_group = QButtonGroup()
    corner_group.addButton(corner_left)
    corner_group.addButton(corner_right)
    corner_row.addWidget(corner_left)
    corner_row.addWidget(corner_right)
    corner_row.addStretch()
    actions_layout.addLayout(corner_row)

    def _selected_window():
        """Return (rect, pid, corner) for the selected window, or None.
        Refreshes the rect from the live platform list if possible."""
        idx = win_combo.currentIndex()
        if idx < 0 or idx >= len(_window_infos):
            return None
        info = _window_infos[idx]
        corner = "left" if corner_left.isChecked() else "right"
        for plat in physics._platforms:
            if plat[1] == info.pid:
                return plat[0], info.pid, corner
        return info.rect, info.pid, corner

    # states where window actions can't fire
    from claudemeji.physics import PhysicsState as _PS
    _blocked_states = {_PS.DRAGGED, _PS.CARRYING_WINDOW, _PS.PUSHING_WINDOW, _PS.PEEKING}

    # action buttons — all use jump_and_do (jump to corner, then act on landing)
    win_action_btns = []

    win_actions_row1 = QHBoxLayout()
    for label, action_key in [
        ("Jump to", "jump_to"),
        ("Push", "push"),
        ("Peek", "peek"),
    ]:
        btn = QPushButton(label)
        def make_handler(action=action_key):
            def handler():
                sel = _selected_window()
                if sel is None:
                    return
                rect, pid, corner = sel
                if action == "jump_to":
                    mw = float(player.width())
                    mh = float(player.height())
                    tx = float(rect.left()) if corner == "left" else float(rect.right()) - mw
                    ty = float(rect.top()) - mh
                    physics.jump_toward(tx, ty)
                else:
                    physics.jump_and_do(action, rect, pid, corner)
            return handler
        btn.clicked.connect(make_handler())
        win_actions_row1.addWidget(btn)
        win_action_btns.append(btn)
    actions_layout.addLayout(win_actions_row1)

    win_actions_row2 = QHBoxLayout()
    for label, action_key in [
        ("Carry", "carry"),
        ("Throw", "throw"),
        ("Side toss", "side_toss"),
    ]:
        btn = QPushButton(label)
        def make_handler2(action=action_key):
            def handler():
                sel = _selected_window()
                if sel is None:
                    return
                rect, pid, corner = sel
                if action == "carry":
                    # carry already jumps to window as part of its sequence
                    physics.start_window_carry(rect, pid, corner)
                else:
                    physics.jump_and_do(action, rect, pid, corner)
            return handler
        btn.clicked.connect(make_handler2())
        win_actions_row2.addWidget(btn)
        win_action_btns.append(btn)
    actions_layout.addLayout(win_actions_row2)

    # gray out window action buttons when in a blocked state
    def _update_button_states():
        blocked = physics._state in _blocked_states
        no_windows = len(_window_infos) == 0
        for btn in win_action_btns:
            btn.setEnabled(not blocked and not no_windows)
    dialog._refresh_timer.timeout.connect(_update_button_states)
    _update_button_states()

    actions_group.setLayout(actions_layout)
    main_layout.addWidget(actions_group)

    dialog.setLayout(main_layout)
    dialog.show()  # non-modal — miku keeps running
    _debug_dialog = dialog


# --- main ---

def main():
    parser = argparse.ArgumentParser(description="claudemeji desktop mascot")
    parser.add_argument("--session", default=None, help="Claude Code session ID to follow")
    parser.add_argument("--scale", type=float, default=1.0, help="Sprite scale factor (e.g. 0.5 for sub-Miku)")
    parser.add_argument("--solo", action="store_true", help="Run without event watching (subagent mode)")
    parser.add_argument("--x", type=int, default=None, help="Initial x position")
    parser.add_argument("--y", type=int, default=None, help="Initial y position")
    parser.add_argument("--entry-action", default="stand", help="Action to play on startup")
    args, _ = parser.parse_known_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # pid file for Stop hook
    if args.session:
        pid_dir = os.path.expanduser("~/.claudemeji/pids")
        os.makedirs(pid_dir, exist_ok=True)
        with open(os.path.join(pid_dir, f"{args.session}.pid"), "w") as f:
            f.write(str(os.getpid()))

    config_path = os.environ.get("CLAUDEMEJI_CONFIG", None)
    try:
        config = load_config(config_path) if config_path else load_config()
    except FileNotFoundError as e:
        print(f"[claudemeji] {e}")
        config = None

    # --- sprite player ---

    player = SpritePlayer()
    player.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )
    player.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    if args.scale != 1.0:
        player.set_scale(args.scale)

    if config:
        try:
            if config.pack.is_file_based:
                player.set_image_dir(config.pack.img_dir_path)
            else:
                player.load_sheet(config.pack.sheet_path, config.pack.frame_width,
                                  config.pack.frame_height)
            for name, action_def in config.actions.items():
                player.register_action(name, action_def)
            player.play(args.entry_action)
        except FileNotFoundError as e:
            print(f"[claudemeji] Could not load sprite pack: {e}")

    screen = app.primaryScreen().availableGeometry()
    init_x = args.x if args.x is not None else screen.width() - 148
    init_y = args.y if args.y is not None else screen.height() - 148
    player.move(init_x, init_y)
    player.show()

    # macOS window fixes (reapplied periodically — Qt resets them)
    # only reapply when she's floating (otherwise it fights z-ordering)
    _z_lowered = [False]  # mutable ref: True when layered with a non-top window

    def _reapply_pin():
        if not _z_lowered[0]:
            apply_macos_window_fixes(player)

    QTimer.singleShot(100, lambda: apply_macos_window_fixes(player))
    _pin_timer = QTimer()
    _pin_timer.setInterval(2000)
    _pin_timer.timeout.connect(_reapply_pin)
    _pin_timer.start()

    # --- physics ---

    physics = PhysicsEngine(window=player)

    if config:
        import claudemeji.physics as _physics_mod
        _physics_mod.WINDOW_PULL_DISTANCE = config.physics.window_pull_distance
        facing = config.physics.default_facing
        player._native_facing = facing
        player.set_facing(facing)
        physics._facing = facing

    # platform refresh
    # protect our parent process (e.g. Terminal running us) from being minimized
    _parent_pid = os.getppid()

    def refresh_platforms():
        if windows_available():
            platforms = get_platform_tuples(own_pid=os.getpid())
            # filter to windows that overlap miku's current screen,
            # and exclude our parent process (don't toss our own terminal!)
            screen = physics._screen_rect()
            platforms = [p for p in platforms
                         if p[0].intersects(screen) and p[1] != _parent_pid]
            physics.update_platforms(platforms)
        else:
            physics.update_platforms([])

    _platform_timer = QTimer()
    _platform_timer.setInterval(2000)
    _platform_timer.timeout.connect(refresh_platforms)
    _platform_timer.start()
    refresh_platforms()

    # window interactions (all delegated to threaded AX calls)
    physics.pull_window.connect(
        lambda pid, rect, dy: _ax_threaded(_wrangler.move_window_by, pid, rect, 0, dy))
    physics.window_move_to.connect(
        lambda pid, x, y: _ax_threaded(_wrangler.move_window_to, pid, x, y))

    def on_window_throw(pid, rect, direction):
        print(f"[claudemeji] THROW window (pid={pid}, dir={direction})")
        _ax_threaded(_wrangler.throw_and_minimize, pid, rect, physics._screen_rect(), direction)

    physics.window_throw.connect(on_window_throw)
    physics.window_toss_up.connect(
        lambda pid, rect: _ax_threaded(_wrangler.toss_window_up, pid, rect))

    # z-ordering: layer miku with the window she's interacting with
    def on_z_context_changed(window_number, z_index):
        if window_number == 0 or z_index < 0:
            # float above everything
            if _z_lowered[0]:
                _z_lowered[0] = False
                set_window_floating(player)
                print("[claudemeji] z-order: floating (above all)")
        else:
            # layer with a specific window
            _z_lowered[0] = True
            set_window_above(player, window_number)
            print(f"[claudemeji] z-order: above window #{window_number} (z={z_index})")

    physics.z_context_changed.connect(on_z_context_changed)

    # --- accessibility check ---

    if _wrangler.is_available():
        if _wrangler.is_trusted():
            print("[claudemeji] Accessibility permission: GRANTED ✓")
        else:
            print("[claudemeji] ⚠ Accessibility permission NOT granted!")
            print("[claudemeji]   Window interactions (push, throw, wiggle) will be disabled.")
            print("[claudemeji]   Grant in: System Settings > Privacy & Security > Accessibility")
            _wrangler.request_trust()
    else:
        print("[claudemeji] Accessibility API not available (pyobjc-framework-ApplicationServices missing)")

    # --- restlessness ---

    restless = RestlessnessEngine()

    def on_restless_level(level):
        physics.set_restlessness(level)
        if level == 0:
            print("[claudemeji] restlessness cleared — miku is calm")

    restless.level_changed.connect(on_restless_level)

    def on_wrangle_window(level):
        if args.solo:
            return
        try:
            infos = get_window_infos(own_pid=os.getpid())
            if not infos:
                return
            target = random.choice(infos)
            screen_rect = physics._screen_rect()
            # disabled: window chaos comes from miku's visible interactions only
            pass
            if not _wrangler.is_trusted() and _wrangler.is_available():
                _wrangler.request_trust()
        except Exception as e:
            print(f"[claudemeji] wrangle setup error: {e}")

    restless.wrangle_window.connect(on_wrangle_window)
    restless.start()

    # --- animation plumbing ---

    _current_posture = ["standing"]
    _drag_intensity = ["calm"]  # mutable ref for drag intensity axis

    def _play(action: str, force: bool = False):
        if action in ("sit_idle", "idle"):
            action = _resolve_idle(config, restless.level)
        resolved_name = config.resolve_action(action) if config else action
        posture = _current_posture[0]
        context = (_resolve_drag_context(config, restless.level, _drag_intensity[0])
                   if action == "drag" else None)
        player.play(resolved_name, posture=posture, context=context,
                    force=(force or action in FORCE_ACTIONS))
        resolved_def = player.current_def()
        physics.set_action_walk_speed(resolved_def.walk_speed if resolved_def else 0.0)
        physics.set_action_offset_y(resolved_def.offset_y if resolved_def else 0)

    def on_posture_changed(posture):
        _current_posture[0] = posture
        _play(player.current_action())

    physics.posture_changed.connect(on_posture_changed)
    physics.facing_changed.connect(player.set_facing)

    # drag — intensity changes re-resolve the drag animation mid-drag
    def on_drag_start(pos):
        physics.on_drag_start(pos)
        restless.notify_grabbed()
        _drag_intensity[0] = "calm"
        _play("drag")

    def on_drag_intensity(intensity):
        _drag_intensity[0] = intensity
        if player.current_action() == "drag":
            _play("drag", force=True)

    player.drag_started.connect(on_drag_start)
    player.drag_moved.connect(physics.on_drag_move)
    player.drag_released.connect(lambda pos: (physics.on_drag_release(pos), _play("fall")))
    physics.drag_intensity_changed.connect(on_drag_intensity)

    # one-shot finished
    def on_one_shot_finished():
        if physics._event_locked:
            _play(state_machine.state.action)

    player.one_shot_finished.connect(on_one_shot_finished)

    # locomotion → animation
    # all locomotion force-cuts: physics state changes are immediate,
    # animations must match (no sliding while outro plays, no standing while walking)
    def on_locomotion(action):
        # these override everything, even event lock
        always_force = ("climb", "ceiling", "hang", "hang_ceiling",
                        "jump", "window_push", "window_peek", "window_throw",
                        "window_carry_perch", "window_carry", "window_carry_run",
                        "window_carry_throw", "window_carry_cheer",
                        "trip", "fall")
        if action in always_force:
            _play(action, force=True)
        elif action == "land":
            # always play land animation — even when event-locked, she needs to stop falling
            _play(random.choice(["stand", "sit_idle"]), force=True)
        elif action == "idle":
            if not physics._event_locked:
                _play(_resolve_idle(config, restless.level), force=True)
        elif not physics._event_locked:
            _play(action, force=True)

    physics.locomotion_action.connect(on_locomotion)
    physics.start()

    # --- state machine + event lock ---

    _event_unlock_timer = QTimer()
    _event_unlock_timer.setSingleShot(True)
    _event_unlock_timer.timeout.connect(physics.unlock)

    state_machine = StateMachine(on_change=lambda state: on_state_change(state))

    def on_state_change(state):
        current = player.current_action()
        if current in UNINTERRUPTABLE:
            print(f"[claudemeji] event {state.action!r} deferred (currently {current!r})")
            return
        print(f"[claudemeji] event → {state.action} (posture: {_current_posture[0]})")
        physics.lock_for_event()
        _play(state.action)

    # --- sub-miku tracking ---

    _sub_mikus: list = []

    def _spawn_sub_miku():
        pos = player.pos()
        env = os.environ.copy()
        if config_path:
            env["CLAUDEMEJI_CONFIG"] = config_path
        proc = subprocess.Popen(
            [sys.executable, "-m", "claudemeji.main",
             "--scale", "0.5", "--solo",
             "--entry-action", "spawned",
             "--x", str(pos.x() + (20 if len(_sub_mikus) % 2 == 0 else -20)),
             "--y", str(pos.y())],
            env=env,
        )
        _sub_mikus.append(proc)
        _sub_mikus[:] = [p for p in _sub_mikus if p.poll() is None]

    def _dismiss_sub_miku():
        if _sub_mikus:
            _sub_mikus.pop().terminate()

    # --- debug panel + context menu ---

    player.add_context_action("Debug panel…",
                              lambda: _show_debug_panel(player, physics, restless, _play, _current_posture))

    # --- system tray icon ---

    from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
    from PyQt6.QtGui import QIcon

    tray_icon = QSystemTrayIcon(QIcon(_make_mushroom_icon()), app)
    tray_icon.setToolTip("claudemeji")

    tray_menu = QMenu()
    tray_menu.setStyleSheet("""
        QMenu { background: #1c1c32; color: #e2e8f0; border: 1px solid #2a2a4a; }
        QMenu::item:selected { background: #3730a3; }
        QMenu::separator { background: #2a2a4a; height: 1px; }
    """)

    # show/hide
    _visible = [True]
    def toggle_visibility():
        if _visible[0]:
            player.hide()
            show_action.setText("Show")
        else:
            player.show()
            show_action.setText("Hide")
        _visible[0] = not _visible[0]
    show_action = tray_menu.addAction("Hide", toggle_visibility)

    # debug panel
    tray_menu.addAction("Debug panel…",
                        lambda: _show_debug_panel(player, physics, restless, _play, _current_posture))

    tray_menu.addSeparator()

    # restlessness submenu
    rest_menu = tray_menu.addMenu("Restlessness")
    for level in range(5):
        label = {0: "0 - Calm", 1: "1 - Fidgety", 2: "2 - Climby",
                 3: "3 - Grabby", 4: "4 - Feral"}[level]
        rest_menu.addAction(label, lambda l=level: restless._set_level(l))

    tray_menu.addSeparator()
    tray_menu.addAction("Quit", app.quit)

    tray_icon.setContextMenu(tray_menu)
    tray_icon.activated.connect(lambda reason: toggle_visibility()
                                if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray_icon.show()

    # --- solo mode (no event watching) ---

    if args.solo:
        if args.entry_action == "spawned":
            direction = 1 if (args.x or 0) >= 0 else -1
            QTimer.singleShot(200, lambda: physics.jump_burst(direction))
        sys.exit(app.exec())

    # --- hook watcher ---

    watcher = HookWatcher(session_id=args.session)

    def on_event_received(event):
        etype = event.get("event_type", "")
        tool_name = event.get("tool_name", "")
        restless.notify_event()

        if etype == "tool_end":
            _event_unlock_timer.stop()
            physics.unlock()

        if etype == "tool_start" and tool_name in ("Agent", "Task"):
            _spawn_sub_miku()
        elif etype == "subagent_stop":
            _dismiss_sub_miku()

        state_machine.handle_event(event)

        if etype == "tool_start":
            _event_unlock_timer.start(TOOL_SAFETY_LOCK_MS)
        elif etype != "tool_end":
            _event_unlock_timer.start(REACTION_LOCK_MS)

    watcher.event_received.connect(on_event_received)
    watcher.idle_triggered.connect(physics.unlock)

    def on_wait_triggered():
        if state_machine.state.action == "bash":
            return
        state_machine.handle_event({"event_type": "tool_start", "tool_name": "_wait"})
        _event_unlock_timer.start(TOOL_SAFETY_LOCK_MS)

    watcher.wait_triggered.connect(on_wait_triggered)
    watcher.wait_cleared.connect(lambda: (physics.unlock(), _play("stand")))
    watcher.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
