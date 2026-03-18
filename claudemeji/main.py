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
from claudemeji.platform_utils import apply_macos_window_fixes
from claudemeji.windows import get_window_infos, is_available as windows_available
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


# --- debug panel ---

def _show_debug_panel(player: SpritePlayer, physics: PhysicsEngine,
                      restless: RestlessnessEngine, play_fn, posture_ref: list):
    """Pop up the debug/tuning dialog."""
    from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                 QSlider, QLabel, QPushButton, QComboBox)
    from PyQt6.QtCore import Qt

    dialog = QDialog()
    dialog.setWindowTitle("claudemeji debug")
    dialog.setFixedWidth(320)
    layout = QVBoxLayout()
    layout.setSpacing(8)

    # current state display
    action_label = QLabel(f"Action: {player.current_action()}  |  Posture: {posture_ref[0]}")
    action_label.setStyleSheet("font-weight: bold;")
    layout.addWidget(action_label)
    refresh = QTimer()
    refresh.setInterval(250)
    refresh.timeout.connect(
        lambda: action_label.setText(
            f"Action: {player.current_action()}  |  Posture: {posture_ref[0]}"
            f"  |  Facing: {physics._facing}"
        )
    )
    refresh.start()

    # pause / resume
    paused = [False]
    pause_btn = QPushButton("⏸ Pause physics")
    def toggle_pause():
        if paused[0]:
            physics.start()
            pause_btn.setText("⏸ Pause physics")
        else:
            physics.stop()
            pause_btn.setText("▶ Resume physics")
        paused[0] = not paused[0]
    pause_btn.clicked.connect(toggle_pause)
    layout.addWidget(pause_btn)

    # animation picker
    layout.addWidget(QLabel("Play animation:"))
    anim_row = QHBoxLayout()
    combo = QComboBox()
    actions = sorted(player._actions.keys())
    combo.addItems(actions)
    if player.current_action() in actions:
        combo.setCurrentText(player.current_action())
    def debug_play():
        physics.lock_for_event()
        play_fn(combo.currentText(), force=True)
        QTimer.singleShot(5000, physics.unlock)
    play_btn = QPushButton("Play")
    play_btn.clicked.connect(debug_play)
    anim_row.addWidget(combo)
    anim_row.addWidget(play_btn)
    layout.addLayout(anim_row)

    # restlessness slider
    layout.addWidget(QLabel(""))
    rest_label = QLabel(f"Restlessness: {restless.level}")
    rest_slider = QSlider(Qt.Orientation.Horizontal)
    rest_slider.setRange(0, 4)
    rest_slider.setValue(restless.level)
    rest_slider.valueChanged.connect(lambda v: (rest_label.setText(f"Restlessness: {v}"),
                                                restless._set_level(v)))
    restless.level_changed.connect(lambda v: (rest_label.setText(f"Restlessness: {v}"),
                                              rest_slider.setValue(v)))
    layout.addWidget(rest_label)
    layout.addWidget(rest_slider)
    calm_btn = QPushButton("Force calm (level 0)")
    calm_btn.clicked.connect(lambda: restless._set_level(0))
    layout.addWidget(calm_btn)

    # position offset sliders
    layout.addWidget(QLabel(""))
    layout.addWidget(QLabel("Position offset (debug):"))
    for axis, getter, setter_fn in [
        ("X", lambda: physics._offset.x, lambda v: physics.set_offset(float(v), physics._offset.y)),
        ("Y", lambda: physics._offset.y, lambda v: physics.set_offset(physics._offset.x, float(v))),
    ]:
        row = QHBoxLayout()
        row.addWidget(QLabel(f"{axis}:"))
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-200, 200)
        slider.setValue(int(getter()))
        val_label = QLabel(str(int(getter())))
        slider.valueChanged.connect(lambda v, lbl=val_label, fn=setter_fn: (lbl.setText(str(v)), fn(v)))
        row.addWidget(slider)
        row.addWidget(val_label)
        layout.addLayout(row)
        # stash on dialog so reset can find them
        setattr(dialog, f"_slider_{axis.lower()}", slider)

    reset_btn = QPushButton("Reset offset")
    reset_btn.clicked.connect(lambda: (dialog._slider_x.setValue(0), dialog._slider_y.setValue(0),
                                       physics.set_offset(0, 0)))
    layout.addWidget(reset_btn)

    dialog.setLayout(layout)
    dialog.show()
    dialog.exec()


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
    QTimer.singleShot(100, lambda: apply_macos_window_fixes(player))
    _pin_timer = QTimer()
    _pin_timer.setInterval(2000)
    _pin_timer.timeout.connect(lambda: apply_macos_window_fixes(player))
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
            infos = get_window_infos(own_pid=os.getpid())
            # filter to windows that overlap miku's current screen,
            # and exclude our parent process (don't toss our own terminal!)
            screen = physics._screen_rect()
            platforms = [(info.rect, info.pid) for info in infos
                         if info.rect.intersects(screen) and info.pid != _parent_pid]
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
