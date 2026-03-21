"""
slot.py - MikuSlot: one Miku instance with all per-session state

Each slot owns a SpritePlayer, PhysicsEngine, RestlessnessEngine, StateMachine,
and all the signal wiring that was previously inline in main(). The conductor
creates/destroys slots as sessions come and go.

Pure helper functions (_resolve_idle, _resolve_drag_context) live at module level
since they have no state. The AX worker thread is shared globally (imported from main).
"""

from __future__ import annotations

import os
import random
from typing import Callable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication

from claudemeji.config import Config
from claudemeji.sprite import SpritePlayer
from claudemeji.physics import PhysicsEngine
from claudemeji.state import StateMachine
from claudemeji.platform_utils import apply_macos_window_fixes, set_window_floating, set_window_above
from claudemeji.windows import get_window_infos
from claudemeji.restlessness import RestlessnessEngine
from claudemeji.resolver import resolve_animation
from claudemeji.creature import CreatureState, CreatureEvent
import claudemeji.window_wrangler as _wrangler


REACTION_LOCK_MS = 4000
TOOL_SAFETY_LOCK_MS = 30000
UNINTERRUPTABLE = {"fall", "drag", "react_good", "react_bad", "subagent", "spawned",
                   "jump", "window_throw", "window_carry_cheer", "trip"}
FORCE_ACTIONS = {"drag", "fall", "react_bad"}


# --- idle / drag resolution (pure functions) ---

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


def _resolve_drag_context(config: Config | None, restlessness: int) -> str | None:
    """Pick drag context based on restlessness tier (r0-r4), falling back to lower tiers."""
    if not config:
        return None
    base = config.actions.get("drag")
    if not base:
        return None
    for lvl in range(restlessness, -1, -1):
        key = f"r{lvl}"
        if key in base.contexts:
            return key
    return None


class MikuSlot:
    """One Miku instance — owns a SpritePlayer, PhysicsEngine, and all per-session state.

    The slot encapsulates everything that was previously wired together inline
    in main(). Multiple slots can coexist in the same QApplication, each with
    independent physics, animation, and event handling.
    """

    def __init__(
        self,
        session_id: str,
        config: Config | None,
        ax_threaded: Callable,
        scale: float = 1.0,
        solo: bool = False,
        entry_action: str = "stand",
        init_x: int | None = None,
        init_y: int | None = None,
    ):
        self.session_id = session_id
        self.solo = solo
        self._config = config
        self._ax_threaded = ax_threaded
        self._destroyed = False

        # --- sub-miku tracking (in-process slots instead of subprocess) ---
        self._sub_mikus: list[MikuSlot] = []
        self._sub_counter = 0

        # --- sprite player ---

        self.player = SpritePlayer()
        self.player.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.player.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        if scale != 1.0:
            self.player.set_scale(scale)

        if config:
            try:
                self.player.set_image_dir(config.pack.img_dir_path)
                for name, action_def in config.actions.items():
                    self.player.register_action(name, action_def)
                self.player.play(entry_action)
            except FileNotFoundError as e:
                print(f"[claudemeji:{session_id}] Could not load sprite pack: {e}")

        app = QApplication.instance()
        screen = app.primaryScreen().availableGeometry()
        x = init_x if init_x is not None else screen.width() - 148
        y = init_y if init_y is not None else screen.height() - 148
        self.player.move(x, y)
        self.player.show()

        # --- macOS window fixes ---
        # _z_lowered: True when layered with a non-top window (suppresses pin reapply)
        self._z_lowered = False

        def _reapply_pin():
            if not self._z_lowered:
                apply_macos_window_fixes(self.player)

        QTimer.singleShot(100, lambda: apply_macos_window_fixes(self.player))
        self._pin_timer = QTimer()
        self._pin_timer.setInterval(2000)
        self._pin_timer.timeout.connect(_reapply_pin)
        self._pin_timer.start()

        # --- physics ---

        self.physics = PhysicsEngine(window=self.player)

        if config:
            import claudemeji.physics as _physics_mod
            # NOTE: module-level global — shared across all slots.
            # fine as long as all slots use the same config.
            _physics_mod.WINDOW_PULL_DISTANCE = config.physics.window_pull_distance
            facing = config.physics.default_facing
            self.player._native_facing = facing
            self.player.set_facing(facing)
            self.physics._facing = facing

        # window interactions → threaded AX calls
        self.physics.pull_window.connect(
            lambda pid, rect, dy: self._ax_threaded(_wrangler.move_window_by, pid, rect, 0, dy))
        self.physics.window_move_to.connect(
            lambda pid, x, y: self._ax_threaded(_wrangler.move_window_to, pid, x, y))

        def on_window_throw(pid, rect, direction):
            print(f"[claudemeji:{session_id}] THROW window (pid={pid}, dir={direction})")
            self._ax_threaded(_wrangler.throw_and_minimize, pid, rect,
                              self.physics._screen_rect(), direction)

        self.physics.window_throw.connect(on_window_throw)
        self.physics.window_toss_up.connect(
            lambda pid, rect: self._ax_threaded(_wrangler.toss_window_up, pid, rect))

        # z-ordering
        def on_z_context_changed(window_number, z_index):
            if window_number == 0 or z_index < 0:
                if self._z_lowered:
                    self._z_lowered = False
                    set_window_floating(self.player)
            else:
                self._z_lowered = True
                set_window_above(self.player, window_number)

        self.physics.z_context_changed.connect(on_z_context_changed)

        # --- restlessness ---

        self.restless = RestlessnessEngine()

        def on_restless_level(level):
            self.physics.set_restlessness(level)
            if level == 0:
                print(f"[claudemeji:{session_id}] restlessness cleared")

        self.restless.level_changed.connect(on_restless_level)

        def on_wrangle_window(level):
            if self.solo:
                return
            try:
                infos = get_window_infos(own_pid=os.getpid())
                if not infos:
                    return
                # disabled: window chaos comes from miku's visible interactions only
                if not _wrangler.is_trusted() and _wrangler.is_available():
                    _wrangler.request_trust()
            except Exception as e:
                print(f"[claudemeji:{session_id}] wrangle setup error: {e}")

        self.restless.wrangle_window.connect(on_wrangle_window)
        self.restless.start()

        # --- animation plumbing ---

        self._current_posture = ["standing"]  # mutable ref — debug panel reads [0] on refresh
        self._oneshot_locked = False
        self._oneshot_posture = None  # posture when oneshot started — clear lock if posture changes

        def _play(action: str, force: bool = False):
            if action in ("sit_idle", "idle"):
                action = _resolve_idle(config, self.restless.level)
            resolved_name = config.resolve_action(action) if config else action
            posture = self._current_posture[0]
            context = (_resolve_drag_context(config, self.restless.level)
                       if action == "drag" else None)
            self.player.play(resolved_name, posture=posture, context=context,
                             force=(force or action in FORCE_ACTIONS))
            resolved_def = self.player.current_def()
            self.physics.set_action_walk_speed(resolved_def.walk_speed if resolved_def else 0.0)
            self.physics.set_action_offset_y(resolved_def.offset_y if resolved_def else 0)

        self._play = _play

        def on_posture_changed(posture):
            self._current_posture[0] = posture
            _play(self.player.current_action())

        self.physics.posture_changed.connect(on_posture_changed)
        self.physics.facing_changed.connect(self.player.set_facing)

        # drag — clear oneshot lock for immediate mouse response
        def on_drag_start(pos):
            self._oneshot_locked = False
            self.physics.on_drag_start(pos)
            self.restless.notify_grabbed()
            _play("drag")

        def on_drag_release(pos):
            self._oneshot_locked = False
            self.physics.on_drag_release(pos)
            _play("fall")

        self.player.drag_started.connect(on_drag_start)
        self.player.drag_moved.connect(self.physics.on_drag_move)
        self.player.drag_released.connect(on_drag_release)

        # one-shot finished
        def on_one_shot_finished():
            self._oneshot_locked = False
            if self.physics._event_locked:
                _play(self.state_machine.state.action)
            else:
                state = self.physics._build_creature_state()
                on_creature_state(state)

        self.player.one_shot_finished.connect(on_one_shot_finished)

        # creature state → animation
        def on_creature_state(state):
            if self._oneshot_locked:
                # posture change overrides oneshot lock — physical state has fundamentally changed,
                # the old one-shot animation (e.g. landing bounce) is no longer relevant
                if state.posture != self._oneshot_posture:
                    self._oneshot_locked = False
                else:
                    return
            if state.is_event_locked:
                return
            action = resolve_animation(state)
            if action in ("sit_idle", "idle", "stand"):
                if state.posture.value == "sitting":
                    action = _resolve_idle(config, self.restless.level)
                elif state.posture.value == "standing" and action == "sit_idle":
                    action = _resolve_idle(config, self.restless.level)
            # skip if already playing this action (avoids restarting one-frame actions)
            current = self.player.current_action()
            if current == action:
                return
            # debug: catch standing-while-walking
            if state.posture.value == "walking" and action in ("stand", "sit_idle"):
                print(f"[claudemeji:{session_id}] BUG? posture=WALKING but action={action} "
                      f"(speed={state.speed_tier.name}, locked={self._oneshot_locked})")
            _play(action, force=True)

        def on_creature_event(event):
            action = resolve_animation(CreatureState(), event)
            _play(action, force=True)
            resolved_def = self.player.current_def()
            if resolved_def and not resolved_def.loop:
                self._oneshot_locked = True
                self._oneshot_posture = self.physics._build_creature_state().posture

        self.physics.creature_state_changed.connect(on_creature_state)
        self.physics.creature_event.connect(on_creature_event)

        self.physics.start()

        # --- state machine + event lock ---

        self._event_unlock_timer = QTimer()
        self._event_unlock_timer.setSingleShot(True)
        self._event_unlock_timer.timeout.connect(self.physics.unlock)

        self.state_machine = StateMachine(on_change=lambda state: self._on_state_change(state))

        # --- context menu ---
        self.player.add_context_action("Debug panel\u2026", self._open_debug_panel)

    # --- public API ---

    def handle_event(self, event: dict):
        """Process a hook event for this session."""
        if self._destroyed:
            return

        etype = event.get("event_type", "")
        tool_name = event.get("tool_name", "")
        self.restless.notify_event()

        if etype == "tool_end":
            self._event_unlock_timer.stop()
            self.physics.unlock()

        if etype == "tool_start" and tool_name in ("Agent", "Task"):
            self._spawn_sub_miku()
        elif etype == "subagent_stop":
            self._dismiss_sub_miku()

        self.state_machine.handle_event(event)

        if etype == "tool_start":
            self._event_unlock_timer.start(TOOL_SAFETY_LOCK_MS)
        elif etype != "tool_end":
            self._event_unlock_timer.start(REACTION_LOCK_MS)

    def handle_wait_triggered(self):
        """Synthetic event: tool_start seen but no tool_end for 3s."""
        if self._destroyed:
            return
        if self.state_machine.state.action == "bash":
            return
        self.state_machine.handle_event({"event_type": "tool_start", "tool_name": "_wait"})
        self._event_unlock_timer.start(TOOL_SAFETY_LOCK_MS)

    def handle_wait_cleared(self):
        """Synthetic event: tool_end arrived after wait state."""
        if self._destroyed:
            return
        self.physics.unlock()
        self._play("stand")

    def handle_idle(self):
        """Synthetic event: no events for idle timeout."""
        if self._destroyed:
            return
        self.physics.unlock()

    def update_platforms(self, platforms: list):
        """Receive shared platform list from the conductor."""
        self.physics.update_platforms(platforms)

    def destroy(self):
        """Tear down this slot — hide player, stop physics, clean up."""
        if self._destroyed:
            return
        self._destroyed = True

        # tear down sub-mikus
        for sub in self._sub_mikus:
            sub.destroy()
        self._sub_mikus.clear()

        self._pin_timer.stop()
        self._event_unlock_timer.stop()
        self.restless.stop()
        self.physics.stop()
        self.player.hide()
        self.player.deleteLater()
        print(f"[claudemeji:{self.session_id}] slot destroyed")

    # --- internal ---

    def _on_state_change(self, state):
        current = self.player.current_action()
        if current in UNINTERRUPTABLE:
            print(f"[claudemeji:{self.session_id}] event {state.action!r} deferred (currently {current!r})")
            return
        print(f"[claudemeji:{self.session_id}] event \u2192 {state.action} (posture: {self._current_posture[0]})")
        self.physics.lock_for_event()
        self._play(state.action)

    def _spawn_sub_miku(self):
        """Create an in-process sub-miku slot (instead of a subprocess)."""
        pos = self.player.pos()
        self._sub_counter += 1
        sub_id = f"{self.session_id}:sub{self._sub_counter}"
        offset = 20 if self._sub_counter % 2 == 0 else -20
        sub = MikuSlot(
            session_id=sub_id,
            config=self._config,
            ax_threaded=self._ax_threaded,
            scale=0.5,
            solo=True,
            entry_action="spawned",
            init_x=pos.x() + offset,
            init_y=pos.y(),
        )
        # give the sub-miku the jump burst after a short delay
        direction = 1 if offset >= 0 else -1
        QTimer.singleShot(200, lambda: sub.physics.jump_burst(direction))
        self._sub_mikus.append(sub)
        # clean up any already-destroyed subs
        self._sub_mikus = [s for s in self._sub_mikus if not s._destroyed]

    def _dismiss_sub_miku(self):
        """Tear down the most recently spawned sub-miku."""
        if self._sub_mikus:
            self._sub_mikus.pop().destroy()

    def _open_debug_panel(self):
        # import here to avoid circular deps — _show_debug_panel lives in main.py
        from claudemeji.main import _show_debug_panel
        _show_debug_panel(
            self.player, self.physics, self.restless,
            self._play, self._current_posture,
            panel_id=self.session_id,
        )
