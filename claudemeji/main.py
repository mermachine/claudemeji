"""
main.py - entry point, wires everything together

Animation resolution uses compound state:
  posture  - what she's physically doing (from PhysicsEngine)
  context  - for drag: restlessness-tier variant (r0-r4)

_play(action, force=False) resolves the right ActionDef variant and calls
player.play(name, posture, context).
"""

import sys
import os
import argparse
import random
import subprocess
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer

from claudemeji.config import load as load_config
from claudemeji.sprite import SpritePlayer
from claudemeji.physics import PhysicsEngine
from claudemeji.state import StateMachine
from claudemeji.watcher import HookWatcher
from claudemeji.platform_utils import apply_macos_window_fixes
from claudemeji.windows import get_window_rects, get_window_infos, is_available as windows_available
from claudemeji.restlessness import RestlessnessEngine
import claudemeji.windows as _windows_mod
import claudemeji.window_wrangler as _wrangler

REACTION_LOCK_MS = 4000    # short lock for one-shot reactions (react_good, spawn, etc.)
TOOL_SAFETY_LOCK_MS = 30000  # fallback in case tool_end never arrives
UNINTERRUPTABLE = {"fall", "drag", "react_good", "react_bad", "subagent", "spawned",
                   "jump", "window_throw"}


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

    # write pid file so the Stop hook can kill us
    if args.session:
        pid_dir = os.path.expanduser("~/.claudemeji/pids")
        os.makedirs(pid_dir, exist_ok=True)
        pid_file = os.path.join(pid_dir, f"{args.session}.pid")
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))

    config_path = os.environ.get("CLAUDEMEJI_CONFIG", None)
    try:
        config = load_config(config_path) if config_path else load_config()
    except FileNotFoundError as e:
        print(f"[claudemeji] {e}")
        config = None

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
                player.load_sheet(
                    config.pack.sheet_path,
                    config.pack.frame_width,
                    config.pack.frame_height,
                )
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
    QTimer.singleShot(100, lambda: apply_macos_window_fixes(player))
    _pin_timer = QTimer()
    _pin_timer.setInterval(2000)
    _pin_timer.timeout.connect(lambda: apply_macos_window_fixes(player))
    _pin_timer.start()

    # physics
    physics = PhysicsEngine(window=player)

    # apply physics tuning from config
    if config:
        import claudemeji.physics as _physics_mod
        _physics_mod.WINDOW_PULL_DISTANCE = config.physics.window_pull_distance
        # set native facing from config (which way sprites are drawn)
        facing = config.physics.default_facing
        player._native_facing = facing
        player.set_facing(facing)
        physics._facing = facing

    # platform refresh - push visible window surfaces to physics every 2s
    def refresh_platforms():
        if windows_available():
            infos = get_window_infos(own_pid=os.getpid())
            platforms = [(info.rect, info.pid) for info in infos]
            physics.update_platforms(platforms)
        else:
            physics.update_platforms([])

    _platform_timer = QTimer()
    _platform_timer.setInterval(2000)
    _platform_timer.timeout.connect(refresh_platforms)
    _platform_timer.start()
    refresh_platforms()  # initial query

    # window pull: sprite weight drags windows down
    def on_pull_window(pid: int, rect, delta_y: float):
        if not _wrangler.is_trusted():
            return
        import threading
        def _safe_pull():
            try:
                _wrangler.move_window_by(pid, rect, 0, delta_y)
            except Exception:
                pass
        threading.Thread(target=_safe_pull, daemon=True).start()

    physics.pull_window.connect(on_pull_window)

    # window push: sprite pushes a window while walking
    def on_push_window_move(pid: int, rect, dx: float, dy: float):
        if not _wrangler.is_trusted():
            return
        import threading
        def _safe_push():
            try:
                _wrangler.move_window_by(pid, rect, dx, dy)
            except Exception:
                pass
        threading.Thread(target=_safe_push, daemon=True).start()

    physics.push_window_move.connect(on_push_window_move)

    # window throw: sprite throws a window (arc + minimize)
    def on_window_throw(pid: int, rect, direction: str):
        if not _wrangler.is_trusted():
            return
        screen = physics._screen_rect()
        print(f"[claudemeji] THROW window (pid={pid}, dir={direction})")
        import threading
        def _safe_throw():
            try:
                _wrangler.throw_and_minimize(pid, rect, screen, direction)
            except Exception as e:
                print(f"[claudemeji] throw error: {e}")
        threading.Thread(target=_safe_throw, daemon=True).start()

    physics.window_throw.connect(on_window_throw)

    # ── restlessness engine ──────────────────────────────────────────────────
    restless = RestlessnessEngine()

    def on_restless_level_changed(level: int):
        physics.set_restlessness(level)
        if level == 0:
            print("[claudemeji] restlessness cleared — miku is calm")

    restless.level_changed.connect(on_restless_level_changed)

    # window wrangling: pick a random window and mess with it
    def on_wrangle_window(level: int):
        if args.solo:
            return
        try:
            infos = get_window_infos(own_pid=os.getpid())
            if not infos:
                return
            target = random.choice(infos)
            screen = physics._screen_rect()

            import threading
            def _safe_wrangle(fn, *a):
                try:
                    fn(*a)
                except Exception as e:
                    print(f"[claudemeji] wrangle error: {e}")

            if level >= 4:
                print(f"[claudemeji] FERAL — tossing {target.name!r} window")
                threading.Thread(
                    target=_safe_wrangle,
                    args=(_wrangler.toss_window, target.pid, target.rect, screen),
                    daemon=True,
                ).start()
            else:
                print(f"[claudemeji] grabby — wiggling {target.name!r} window")
                threading.Thread(
                    target=_safe_wrangle,
                    args=(_wrangler.wiggle_window, target.pid, target.rect),
                    daemon=True,
                ).start()

            # ask for AX permission if we don't have it (first wrangle attempt)
            if not _wrangler.is_trusted() and _wrangler.is_available():
                _wrangler.request_trust()
        except Exception as e:
            print(f"[claudemeji] wrangle setup error: {e}")

    restless.wrangle_window.connect(on_wrangle_window)
    restless.start()

    # right-click → debug panel
    def show_debug_panel():
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QSlider, QLabel, QPushButton, QComboBox)
        from PyQt6.QtCore import Qt

        dialog = QDialog()
        dialog.setWindowTitle("claudemeji debug")
        dialog.setFixedWidth(320)
        layout = QVBoxLayout()
        layout.setSpacing(8)

        # --- current action display ---
        action_label = QLabel(f"Action: {player.current_action()}  |  Posture: {_current_posture[0]}")
        action_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(action_label)
        _action_update_timer = QTimer()
        _action_update_timer.setInterval(250)
        _action_update_timer.timeout.connect(
            lambda: action_label.setText(
                f"Action: {player.current_action()}  |  Posture: {_current_posture[0]}"
                f"  |  Facing: {physics._facing}"
            )
        )
        _action_update_timer.start()

        # --- pause / resume ---
        _paused = [False]
        pause_btn = QPushButton("⏸ Pause physics")
        def toggle_pause():
            if _paused[0]:
                physics.start()
                pause_btn.setText("⏸ Pause physics")
                _paused[0] = False
            else:
                physics.stop()
                pause_btn.setText("▶ Resume physics")
                _paused[0] = True
        pause_btn.clicked.connect(toggle_pause)
        layout.addWidget(pause_btn)

        # --- animation picker ---
        layout.addWidget(QLabel("Play animation:"))
        anim_row = QHBoxLayout()
        combo = QComboBox()
        actions = sorted(player._actions.keys())
        combo.addItems(actions)
        if player.current_action() in actions:
            combo.setCurrentText(player.current_action())
        play_btn = QPushButton("Play")
        def _debug_play():
            # lock physics so it doesn't override, then play the action
            physics.lock_for_event()
            _play(combo.currentText(), force=True)
            # auto-unlock after 5s so she resumes normal behavior
            QTimer.singleShot(5000, physics.unlock)
        play_btn.clicked.connect(_debug_play)
        anim_row.addWidget(combo)
        anim_row.addWidget(play_btn)
        layout.addLayout(anim_row)

        # --- restlessness controls ---
        layout.addWidget(QLabel(""))  # spacer
        rest_label = QLabel(f"Restlessness: {restless.level}")
        rest_slider = QSlider(Qt.Orientation.Horizontal)
        rest_slider.setRange(0, 4)
        rest_slider.setValue(restless.level)

        def on_rest_change(val):
            rest_label.setText(f"Restlessness: {val}")
            restless._set_level(val)

        rest_slider.valueChanged.connect(on_rest_change)
        restless.level_changed.connect(lambda v: (rest_label.setText(f"Restlessness: {v}"),
                                                   rest_slider.setValue(v)))
        layout.addWidget(rest_label)
        layout.addWidget(rest_slider)

        btn_calm = QPushButton("Force calm (level 0)")
        btn_calm.clicked.connect(lambda: restless._set_level(0))
        layout.addWidget(btn_calm)

        # --- position offset (debug) ---
        layout.addWidget(QLabel(""))  # spacer
        layout.addWidget(QLabel("Position offset (debug):"))

        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X:"))
        x_slider = QSlider(Qt.Orientation.Horizontal)
        x_slider.setRange(-200, 200)
        x_slider.setValue(int(physics._offset.x))
        x_val = QLabel(str(int(physics._offset.x)))
        def on_x_offset(val):
            x_val.setText(str(val))
            physics.set_offset(float(val), physics._offset.y)
        x_slider.valueChanged.connect(on_x_offset)
        x_row.addWidget(x_slider)
        x_row.addWidget(x_val)
        layout.addLayout(x_row)

        y_row = QHBoxLayout()
        y_row.addWidget(QLabel("Y:"))
        y_slider = QSlider(Qt.Orientation.Horizontal)
        y_slider.setRange(-200, 200)
        y_slider.setValue(int(physics._offset.y))
        y_val = QLabel(str(int(physics._offset.y)))
        def on_y_offset(val):
            y_val.setText(str(val))
            physics.set_offset(physics._offset.x, float(val))
        y_slider.valueChanged.connect(on_y_offset)
        y_row.addWidget(y_slider)
        y_row.addWidget(y_val)
        layout.addLayout(y_row)

        reset_btn = QPushButton("Reset offset")
        reset_btn.clicked.connect(lambda: (x_slider.setValue(0), y_slider.setValue(0),
                                           physics.set_offset(0, 0)))
        layout.addWidget(reset_btn)

        dialog.setLayout(layout)
        dialog.show()
        dialog.exec()

    player.add_context_action("Debug panel…", show_debug_panel)

    # current posture - updated via signal
    _current_posture = ["standing"]   # list so lambda can mutate it

    def on_posture_changed(posture: str):
        _current_posture[0] = posture
        # refresh current animation variant for new posture
        current = player.current_action()
        _play(current)

    physics.posture_changed.connect(on_posture_changed)
    physics.facing_changed.connect(player.set_facing)

    # actions that always hard-cut, never queue behind an outro
    FORCE_ACTIONS = {"drag", "fall", "react_bad"}

    def _resolve_idle() -> str:
        """Pick an idle action from the pool based on current restlessness."""
        if not config:
            return "sit_idle"
        level = restless.level
        pool = ["sit_idle", "stand"]  # base options
        for name, adef in config.actions.items():
            if name.startswith("idle") and name[4:].isdigit():
                if adef.min_restlessness <= level:
                    pool.append(name)
        return random.choice(pool)

    def _resolve_drag_context():
        """Pick drag context variant based on restlessness (highest tier that exists)."""
        if not config:
            return None
        base = config.actions.get("drag")
        if not base:
            return None
        for lvl in range(restless.level, -1, -1):
            key = f"r{lvl}"
            if key in base.contexts:
                return key
        return None

    def _play(action: str, force: bool = False):
        # idle tier resolution: sit_idle or stand may resolve to idle1-5
        if action in ("sit_idle", "idle"):
            action = _resolve_idle()

        if config:
            resolved_name = config.resolve_action(action)
        else:
            resolved_name = action
        posture = _current_posture[0]

        # drag context: restlessness-based, not activity-based
        context = _resolve_drag_context() if action == "drag" else None

        player.play(resolved_name, posture=posture, context=context,
                    force=(force or action in FORCE_ACTIONS))

    # drag - immediate hard cut (also calms restlessness)
    def on_drag_start(pos):
        physics.on_drag_start(pos)
        restless.notify_grabbed()
        _play("drag")  # force=True via FORCE_ACTIONS

    def on_drag_release(pos):
        physics.on_drag_release(pos)
        _play("fall")  # force=True via FORCE_ACTIONS

    player.drag_started.connect(on_drag_start)
    player.drag_moved.connect(physics.on_drag_move)
    player.drag_released.connect(on_drag_release)

    # one-shot finished: resume from current physics/state
    def on_one_shot_finished():
        if physics._event_locked:
            # still locked for a tool — stay on whatever the state machine says
            _play(state_machine.state.action)
        # else: physics is in control — hold last frame, wander timer handles next action

    player.one_shot_finished.connect(on_one_shot_finished)

    # locomotion
    def on_locomotion(action: str):
        if action in ("climb", "ceiling", "hang", "hang_ceiling",
                      "jump", "window_push", "window_peek", "window_throw"):
            # physics explicitly changed state — always play, always force
            _play(action, force=True)
        elif action == "land":
            # landed — play stand or sit_idle as a soft transition (triggers fall outro)
            # wander timer is already scheduled, so she'll pick her next action naturally
            if not physics._event_locked:
                _play(random.choice(["stand", "sit_idle"]))
        elif action == "idle":
            # deliberate idle behavior — resolve from pool
            if not physics._event_locked:
                _play(_resolve_idle())
        elif not physics._event_locked:
            _play(action)

    physics.locomotion_action.connect(on_locomotion)
    physics.start()

    # event lock timer
    _event_unlock_timer = QTimer()
    _event_unlock_timer.setSingleShot(True)
    _event_unlock_timer.timeout.connect(physics.unlock)

    # state machine
    state_machine = StateMachine(on_change=lambda state: on_state_change(state))

    def on_state_change(state):
        current = player.current_action()
        if current in UNINTERRUPTABLE:
            print(f"[claudemeji] event {state.action!r} deferred (currently {current!r})")
            return
        print(f"[claudemeji] event → {state.action} (posture: {_current_posture[0]})")
        physics.lock_for_event()
        _play(state.action)
        # timer NOT managed here - handled by on_event_received / synthetic event handlers

    # sub-miku tracking (spawned for subagents)
    _sub_mikis: list = []

    def _spawn_sub_miku():
        pos = player.pos()
        env = os.environ.copy()
        if config_path:
            env["CLAUDEMEJI_CONFIG"] = config_path
        proc = subprocess.Popen(
            [sys.executable, "-m", "claudemeji.main",
             "--scale", "0.5", "--solo",
             "--entry-action", "spawned",
             "--x", str(pos.x() + (20 if len(_sub_mikis) % 2 == 0 else -20)),
             "--y", str(pos.y())],
            env=env,
        )
        _sub_mikis.append(proc)
        # prune finished ones
        _sub_mikis[:] = [p for p in _sub_mikis if p.poll() is None]

    def _dismiss_sub_miku():
        if _sub_mikis:
            proc = _sub_mikis.pop()
            proc.terminate()

    if args.solo:
        # solo mode: no event watching, just physics + wandering
        # if entry action is "spawned", burst outward like emerging from parent
        if args.entry_action == "spawned":
            direction = 1 if (args.x or 0) >= 0 else -1
            QTimer.singleShot(200, lambda: physics.jump_burst(direction))
        sys.exit(app.exec())

    # hook watcher
    watcher = HookWatcher(session_id=args.session)

    def on_event_received(event: dict):
        etype = event.get("event_type", "")
        tool_name = event.get("tool_name", "")
        restless.notify_event()   # any hook event resets the idle clock + clears restlessness

        if etype == "tool_end":
            # cancel safety timer and unlock immediately
            _event_unlock_timer.stop()
            physics.unlock()

        # spawn/dismiss sub-miku for subagent tools
        if etype == "tool_start" and tool_name in ("Agent", "Task"):
            _spawn_sub_miku()
        elif etype == "subagent_stop":
            _dismiss_sub_miku()

        state_machine.handle_event(event)
        # set timer AFTER handle_event so we overwrite any stale timer
        if etype == "tool_start":
            _event_unlock_timer.start(TOOL_SAFETY_LOCK_MS)  # long: tool_end will cancel it
        elif etype != "tool_end":
            _event_unlock_timer.start(REACTION_LOCK_MS)  # short: session events, notifications

    watcher.event_received.connect(on_event_received)
    watcher.idle_triggered.connect(lambda: physics.unlock())  # just unlock, hold frame

    def on_wait_triggered():
        # tool is taking ages or waiting for permission - switch to wait animation
        # but don't override "run": bash running long is fine, she's clearly working
        if state_machine.state.action == "bash":
            return
        # inject a synthetic wait event; on_state_change handles lock + play
        state_machine.handle_event({"event_type": "tool_start", "tool_name": "_wait"})
        _event_unlock_timer.start(TOOL_SAFETY_LOCK_MS)

    def on_wait_cleared():
        # tool finally finished - unlock and let physics resume
        physics.unlock()
        _play("stand")

    watcher.wait_triggered.connect(on_wait_triggered)
    watcher.wait_cleared.connect(on_wait_cleared)
    watcher.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
