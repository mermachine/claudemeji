"""
main.py - entry point, wires everything together

Two modes:
  1. Conductor mode (default, no --session): MikuManager + MultiHookWatcher
     manages multiple concurrent Miku instances, one per Claude Code session.
  2. Single-session mode (--session ID or --solo): one MikuSlot, backward compat
     with the old per-process model.
"""
from __future__ import annotations

import sys
import os
import argparse
import random
import threading
import queue
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from claudemeji.config import load as load_config
from claudemeji.sprite import SpritePlayer
from claudemeji.physics import PhysicsEngine
from claudemeji.windows import get_window_infos, get_platform_tuples, is_available as windows_available
from claudemeji.restlessness import RestlessnessEngine
import claudemeji.window_wrangler as _wrangler


# --- threaded AX helper (shared across all slots) ---

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
                print(f"[claudemeji] AX {fn.__name__} \u2192 {result}")
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
            print(f"[claudemeji] AX call skipped \u2014 no Accessibility permission ({_ax_skip_count} total skips)")
        return
    _ax_skip_count = 0
    _ensure_ax_worker()
    # for per-tick moves, drop stale entries (only latest position matters)
    if fn.__name__ in ("move_window_to", "move_window_by"):
        while not _ax_queue.empty():
            try:
                _ax_queue.get_nowait()
            except queue.Empty:
                break
    _ax_queue.put((fn, args))


# --- tray icon ---

def _make_mushroom_icon():
    """Draw a bold mushroom silhouette for the system tray.
    Menu bar icons need to be simple, high-contrast glyphs \u2014
    no fine detail, just recognizable shape at 22px."""
    from PyQt6.QtGui import QPixmap, QPainter, QColor, QBrush, QPainterPath
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

# per-slot debug dialogs (keyed by panel_id) so multiple can be open
_debug_dialogs: dict[str, object] = {}


def _show_debug_panel(player: SpritePlayer, physics: PhysicsEngine,
                      restless: RestlessnessEngine, play_fn, posture_ref: list,
                      panel_id: str = "default"):
    """Non-modal debug panel with live state, controls, and targeted actions."""
    global _debug_dialogs
    existing = _debug_dialogs.get(panel_id)
    if existing is not None:
        existing.raise_()
        existing.activateWindow()
        return

    from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                                 QSlider, QLabel, QPushButton, QComboBox,
                                 QGroupBox, QRadioButton, QButtonGroup,
                                 QFrame)
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QFont
    from claudemeji.surfaces import plat_rect as _plat_rect, plat_pid as _plat_pid, plat_zidx as _plat_zidx

    mono = QFont("Menlo, Monaco, Courier New")
    mono.setPointSize(11)

    dialog = QDialog()
    short_id = panel_id[:12] if len(panel_id) > 12 else panel_id
    dialog.setWindowTitle(f"claudemeji debug [{short_id}]")
    dialog.setMinimumWidth(380)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def on_close():
        _debug_dialogs.pop(panel_id, None)
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
        val_lbl = QLabel("\u2014")
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
            screen = physics._screen_rect()
            _window_infos = [info for info in all_infos
                             if info.rect.intersects(screen) and info.pid != os.getppid()]
            for info in _window_infos:
                title = (info.title[:25] + "..") if len(info.title) > 25 else (info.title or "\u2014")
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
        idx = win_combo.currentIndex()
        if idx < 0 or idx >= len(_window_infos):
            return None
        info = _window_infos[idx]
        corner = "left" if corner_left.isChecked() else "right"
        for plat in physics._platforms:
            if plat[1] == info.pid:
                return plat[0], info.pid, corner
        return info.rect, info.pid, corner

    from claudemeji.physics import PhysicsState as _PS
    _blocked_states = {_PS.DRAGGED, _PS.CARRYING_WINDOW, _PS.PUSHING_WINDOW}

    win_action_btns = []

    win_actions_row1 = QHBoxLayout()
    for label, action_key in [
        ("Jump to", "jump_to"),
        ("Push", "push"),
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
                    physics.start_window_carry(rect, pid, corner)
                else:
                    physics.jump_and_do(action, rect, pid, corner)
            return handler
        btn.clicked.connect(make_handler2())
        win_actions_row2.addWidget(btn)
        win_action_btns.append(btn)
    actions_layout.addLayout(win_actions_row2)

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
    dialog.show()  # non-modal
    _debug_dialogs[panel_id] = dialog


# --- tray builders ---

def _build_tray_conductor(app, manager):
    """Build the system tray icon for conductor mode (multi-session)."""
    from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
    from PyQt6.QtGui import QIcon

    tray_icon = QSystemTrayIcon(QIcon(_make_mushroom_icon()), app)
    tray_icon.setToolTip("claudemeji (conductor)")

    tray_menu = QMenu()
    tray_menu.setStyleSheet("""
        QMenu { background: #1c1c32; color: #e2e8f0; border: 1px solid #2a2a4a; }
        QMenu::item:selected { background: #3730a3; }
        QMenu::separator { background: #2a2a4a; height: 1px; }
    """)

    _visible = [True]
    def toggle_all():
        for slot in manager.slots.values():
            if _visible[0]:
                slot.player.hide()
            else:
                slot.player.show()
        show_action.setText("Show all" if _visible[0] else "Hide all")
        _visible[0] = not _visible[0]
    show_action = tray_menu.addAction("Hide all", toggle_all)

    tray_menu.addSeparator()

    # sessions submenu (dynamic)
    sessions_menu = tray_menu.addMenu("Sessions")
    def refresh_sessions_menu():
        sessions_menu.clear()
        if not manager.slots:
            sessions_menu.addAction("(no active sessions)").setEnabled(False)
            return
        for sid, slot in manager.slots.items():
            label = f"{sid[:12]}..."
            sub = sessions_menu.addMenu(label)
            sub.addAction("Debug panel\u2026",
                          lambda s=slot: _show_debug_panel(
                              s.player, s.physics, s.restless,
                              s._play, s._current_posture,
                              panel_id=s.session_id))
            rest_sub = sub.addMenu("Restlessness")
            for level in range(5):
                rest_label = {0: "0 - Calm", 1: "1 - Fidgety", 2: "2 - Climby",
                              3: "3 - Grabby", 4: "4 - Feral"}[level]
                rest_sub.addAction(rest_label, lambda l=level, s=slot: s.restless._set_level(l))
            sub.addSeparator()
            if slot.player.isVisible():
                sub.addAction("Pause", lambda s=slot: s.player.hide())
            else:
                sub.addAction("Resume", lambda s=slot: s.player.show())
            sub.addAction("Stop", lambda s=slot, sid=sid: (
                manager.force_destroy(sid)))
    sessions_menu.aboutToShow.connect(refresh_sessions_menu)

    tray_menu.addSeparator()
    tray_menu.addAction("Quit", app.quit)

    tray_icon.setContextMenu(tray_menu)
    tray_icon.activated.connect(lambda reason: toggle_all()
                                if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray_icon.show()
    return tray_icon


def _build_tray_single(app, slot):
    """Build the system tray icon for single-session mode (backward compat)."""
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

    _visible = [True]
    def toggle_visibility():
        if _visible[0]:
            slot.player.hide()
            show_action.setText("Show")
        else:
            slot.player.show()
            show_action.setText("Hide")
        _visible[0] = not _visible[0]
    show_action = tray_menu.addAction("Hide", toggle_visibility)

    tray_menu.addAction("Debug panel\u2026",
                        lambda: _show_debug_panel(slot.player, slot.physics, slot.restless,
                                                  slot._play, slot._current_posture,
                                                  panel_id=slot.session_id))

    tray_menu.addSeparator()

    rest_menu = tray_menu.addMenu("Restlessness")
    for level in range(5):
        label = {0: "0 - Calm", 1: "1 - Fidgety", 2: "2 - Climby",
                 3: "3 - Grabby", 4: "4 - Feral"}[level]
        rest_menu.addAction(label, lambda l=level: slot.restless._set_level(l))

    tray_menu.addSeparator()
    tray_menu.addAction("Quit", app.quit)

    tray_icon.setContextMenu(tray_menu)
    tray_icon.activated.connect(lambda reason: toggle_visibility()
                                if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray_icon.show()
    return tray_icon


# --- main ---

def main():
    parser = argparse.ArgumentParser(description="claudemeji desktop mascot")
    parser.add_argument("--session", default=None, help="Claude Code session ID to follow (single-session mode)")
    parser.add_argument("--scale", type=float, default=1.0, help="Sprite scale factor (e.g. 0.5 for sub-Miku)")
    parser.add_argument("--solo", action="store_true", help="Run without event watching (subagent mode)")
    parser.add_argument("--x", type=int, default=None, help="Initial x position")
    parser.add_argument("--y", type=int, default=None, help="Initial y position")
    parser.add_argument("--entry-action", default="stand", help="Action to play on startup")
    args, _ = parser.parse_known_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # catch Python exceptions in Qt slots — without this, PyQt6 calls abort() and we get
    # a silent crash with no traceback (only visible in macOS crash reports)
    def _qt_exception_hook(exc_type, exc_value, exc_tb):
        import traceback
        print("[claudemeji] *** UNHANDLED EXCEPTION IN QT SLOT ***", flush=True)
        traceback.print_exception(exc_type, exc_value, exc_tb)
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _qt_exception_hook

    config_path = os.environ.get("CLAUDEMEJI_CONFIG", None)
    try:
        config = load_config(config_path) if config_path else load_config()
    except FileNotFoundError as e:
        print(f"[claudemeji] {e}")
        config = None

    # --- accessibility check (once, shared) ---

    if _wrangler.is_available():
        if _wrangler.is_trusted():
            print("[claudemeji] Accessibility permission: GRANTED")
        else:
            print("[claudemeji] Accessibility permission NOT granted!")
            print("[claudemeji]   Window interactions (push, throw, wiggle) will be disabled.")
            print("[claudemeji]   Grant in: System Settings > Privacy & Security > Accessibility")
            _wrangler.request_trust()
    else:
        print("[claudemeji] Accessibility API not available (pyobjc-framework-ApplicationServices missing)")

    # --- decide mode ---

    use_conductor = not args.session and not args.solo

    if use_conductor:
        # ============================================================
        # CONDUCTOR MODE: manage multiple sessions in one process
        # ============================================================
        from claudemeji.conductor import MikuManager
        from claudemeji.multi_watcher import MultiHookWatcher

        # ensure only one conductor runs at a time
        pid_dir = os.path.expanduser("~/.claudemeji/pids")
        os.makedirs(pid_dir, exist_ok=True)
        conductor_pid_path = os.path.join(pid_dir, "conductor.pid")
        if os.path.exists(conductor_pid_path):
            try:
                old_pid = int(open(conductor_pid_path).read().strip())
                # check if that process is actually alive
                os.kill(old_pid, 0)  # signal 0 = existence check, doesn't kill
                print(f"[claudemeji] conductor already running (pid {old_pid}), exiting")
                sys.exit(0)
            except (ProcessLookupError, ValueError):
                pass  # stale pid file, take over
            except PermissionError:
                # process exists but we can't signal it — still alive
                print(f"[claudemeji] conductor already running (pid {old_pid}), exiting")
                sys.exit(0)
        with open(conductor_pid_path, "w") as f:
            f.write(str(os.getpid()))

        print("[claudemeji] starting in CONDUCTOR mode (multi-session)")

        manager = MikuManager(config, _ax_threaded)
        multi_watcher = MultiHookWatcher()

        # wire: watcher events → manager routing
        multi_watcher.event_received.connect(manager.on_session_event)
        multi_watcher.start()

        tray_icon = _build_tray_conductor(app, manager)

        # clean up conductor pid on exit
        def cleanup():
            try:
                os.unlink(conductor_pid_path)
            except OSError:
                pass
            manager.destroy_all()
            multi_watcher.stop()

        app.aboutToQuit.connect(cleanup)

    else:
        # ============================================================
        # SINGLE-SESSION / SOLO MODE: one MikuSlot, backward compat
        # ============================================================
        from claudemeji.slot import MikuSlot
        from claudemeji.watcher import HookWatcher

        # pid file for Stop hook (single-session)
        if args.session:
            pid_dir = os.path.expanduser("~/.claudemeji/pids")
            os.makedirs(pid_dir, exist_ok=True)
            with open(os.path.join(pid_dir, f"{args.session}.pid"), "w") as f:
                f.write(str(os.getpid()))

        session_id = args.session or "solo"

        slot = MikuSlot(
            session_id=session_id,
            config=config,
            ax_threaded=_ax_threaded,
            scale=args.scale,
            solo=args.solo,
            entry_action=args.entry_action,
            init_x=args.x,
            init_y=args.y,
        )

        # platform refresh for single-session mode (conductor does this itself)
        _parent_pid = os.getppid()

        def refresh_platforms():
            if windows_available():
                platforms = get_platform_tuples(own_pid=os.getpid())
                screen = slot.physics._screen_rect()
                platforms = [p for p in platforms
                             if p[0].intersects(screen) and p[1] != _parent_pid]
                slot.update_platforms(platforms)
            else:
                slot.update_platforms([])

        _platform_timer = QTimer()
        _platform_timer.setInterval(2000)
        _platform_timer.timeout.connect(refresh_platforms)
        _platform_timer.start()
        refresh_platforms()

        tray_icon = _build_tray_single(app, slot)

        # --- solo mode: just physics, no event watching ---
        if args.solo:
            if args.entry_action == "spawned":
                direction = 1 if (args.x or 0) >= 0 else -1
                QTimer.singleShot(200, lambda: slot.physics.jump_burst(direction))
            sys.exit(app.exec())

        # --- hook watcher (single-session) ---

        watcher = HookWatcher(session_id=args.session)

        watcher.event_received.connect(slot.handle_event)
        watcher.idle_triggered.connect(slot.handle_idle)
        watcher.wait_triggered.connect(slot.handle_wait_triggered)
        watcher.wait_cleared.connect(slot.handle_wait_cleared)
        watcher.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
