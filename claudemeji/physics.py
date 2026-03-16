"""
physics.py - window movement, gravity, surface detection, idle locomotion

Tracks PostureState - a higher-level view of what the mascot is physically
doing, used by the animation system to pick posture variants.

PostureState:
  STANDING  - grounded, stationary, recently active
  SITTING   - grounded, stationary for SITTING_TIMEOUT ticks (settled in)
  WALKING   - grounded, moving horizontally
  FALLING   - airborne
  CLIMBING  - on a wall (actively climbing upward)
  CEILING   - crawling along ceiling
  DRAGGED   - being held by cursor
  HANGING   - dangling on wall or ceiling without moving
  PUSHING   - pushing/dragging a window
  PEEKING   - peeking from a window corner

Behaviors gated by restlessness:
  level 0: calm, basic wandering
  level 1: shorter pauses, starts climbing
  level 2+: cursor following, window peek/push, fast walk
  level 3+: window push/drag
  level 4+: window throw (minimize)
"""

from __future__ import annotations
import random
from dataclasses import dataclass
from enum import Enum, auto
from PyQt6.QtCore import QObject, QTimer, QPoint, QRect, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication


TICK_MS = 16
GRAVITY = 0.6
MAX_FALL_SPEED = 20
WALK_SPEED = 2
RUN_SPEED  = 4          # fast walk at higher restlessness
IDLE_WANDER_INTERVAL = (180, 420)
VELOCITY_HISTORY = 5
SITTING_TIMEOUT = 300   # ticks of stillness before transitioning to SITTING (~5s)
SURFACE_TOLERANCE = 4.0  # pixels: how close to a surface counts as "on it"

# wall climbing / ceiling crawling (base values; restlessness multiplies these)
WALL_GRAB_CHANCE    = 0.35         # chance to grab a screen wall when falling into it
CLIMB_SPEED         = 1.5          # px/tick upward while climbing
CLIMB_DURATION      = (120, 360)   # ticks (~2-6s) on wall before falling or reaching top
CEILING_CRAWL_SPEED = 1.2          # px/tick horizontal on ceiling
CEILING_DURATION    = (120, 300)   # ticks (~2-5s) on ceiling before letting go

# hanging: stop climbing, just dangle on wall or ceiling
HANG_CHANCE         = 0.35         # chance to hang instead of falling when climb expires
HANG_DURATION       = (180, 420)   # ticks (~3-7s) of hanging before letting go

# jumping: impulse-based launch toward a target
JUMP_IMPULSE_X      = 6.0          # horizontal speed component
JUMP_IMPULSE_Y      = -12.0        # vertical speed component (upward)
JUMP_MIN_HEIGHT     = -8.0         # minimum upward component even for horizontal jumps

# cursor following (restlessness-gated)
CURSOR_FOLLOW_CHANCE = {2: 0.15, 3: 0.30, 4: 0.45}  # per wander decision
CURSOR_JUMP_DISTANCE = 250         # jump toward cursor if within this many px
CURSOR_JUMP_CHANCE   = 0.35        # chance to jump vs walk when following cursor

# window pushing/dragging
WINDOW_PUSH_SPEED     = 1.0        # px/tick while pushing a window
WINDOW_PUSH_DURATION  = (180, 600) # ticks (~3-10s) of pushing before releasing
WINDOW_PUSH_EMIT_INTERVAL = 3      # emit push signal every N ticks

# window peeking
WINDOW_PEEK_DURATION  = (120, 300) # ticks (~2-5s) of peeking

# window throwing
WINDOW_THROW_ARC_STEPS = 15        # animation steps for throw arc

# side-climbing window pull (climbing on a window's side pulls it down)
SIDE_PULL_SPEED       = 0.3        # px/tick downward pull while climbing a window side
SIDE_PULL_INTERVAL    = 8          # emit pull every N ticks while side-climbing

# restlessness modifiers per level
# each entry is (wander_interval_multiplier, wall_grab_chance, climb_duration_multiplier)
_RESTLESS_PARAMS = {
    0: (1.0,  0.00, 1.0),   # calm: no climbing
    1: (0.6,  0.25, 1.0),   # fidgety: shorter wander pauses, sometimes climbs
    2: (0.4,  0.35, 1.2),   # climby: more climbing, but balanced with other behaviors
    3: (0.35, 0.45, 1.4),   # grabby: frequent climbing, window interactions
    4: (0.25, 0.50, 1.6),   # feral: lots of everything
}

# chance to seek a window to interact with (walk toward it) per wander decision
WINDOW_SEEK_CHANCE = {2: 0.15, 3: 0.25, 4: 0.35}


class PhysicsState(Enum):
    GROUNDED       = auto()
    FALLING        = auto()
    WALL_LEFT      = auto()
    WALL_RIGHT     = auto()
    CEILING        = auto()
    DRAGGED        = auto()
    PUSHING_WINDOW = auto()  # walking while dragging/pushing a window
    PEEKING        = auto()  # peeking from a window corner


class PostureState(Enum):
    STANDING = "standing"
    SITTING  = "sitting"
    WALKING  = "walking"
    FALLING  = "falling"
    CLIMBING = "climbing"
    CEILING  = "ceiling"
    DRAGGED  = "dragged"
    HANGING  = "hanging"   # dangling on wall or ceiling without climbing
    PUSHING  = "pushing"   # pushing/dragging a window
    PEEKING  = "peeking"   # peeking from a window corner


@dataclass
class Vec2:
    x: float = 0.0
    y: float = 0.0


WINDOW_PULL_DISTANCE = 0   # how far sprite weight pulls windows down (0 = disabled)
                           # override via [physics] window_pull_distance in config.toml
WINDOW_PULL_SPEED    = 0.5 # px/tick downward pull rate
WINDOW_PULL_INTERVAL = 5   # only emit pull signal every N ticks (avoid thrashing AX API)


class PhysicsEngine(QObject):
    locomotion_action = pyqtSignal(str)
    posture_changed   = pyqtSignal(str)    # PostureState.value
    facing_changed    = pyqtSignal(str)    # "left" or "right"
    pull_window       = pyqtSignal(int, object, float)  # (pid, QRect, delta_y)
    push_window_move  = pyqtSignal(int, object, float, float)  # (pid, QRect, dx, dy)
    window_throw      = pyqtSignal(int, object, str)    # (pid, QRect, direction)

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        self._vel = Vec2()
        self._state = PhysicsState.FALLING
        self._posture = PostureState.FALLING
        self._walk_dir = 0
        self._wander_ticks = 0
        self._still_ticks = 0        # counts up while grounded+still
        self._event_locked = False
        self._dragged = False
        self._drag_offset = QPoint()
        self._cursor_history: list[QPoint] = []
        self._platforms: list = []   # list of (QRect, pid) tuples for walkable window surfaces
        self._floor_y: float = 0.0   # y where she's currently standing (set on land)
        self._climb_ticks: int = 0   # countdown while on wall or ceiling
        self._climb_x: float = 0.0  # x position to pin to while climbing (screen edge or window side)
        self._ceiling_dir: int = 1   # horizontal crawl direction on ceiling (-1 left, 1 right)
        self._running: bool = False  # True when doing fast walk (restless "run" action)
        self._hanging: bool = False  # True when hanging (not climbing) on wall or ceiling

        # side-climbing window pull: climbing a window side pulls it down
        self._climbing_window: tuple | None = None  # (QRect, pid) when climbing a window (not screen edge)
        self._side_pull_counter: int = 0

        # window pulling: sprite weight pulls windows down
        self._standing_on_window: tuple | None = None  # (QRect, pid) or None
        self._window_pull_applied: float = 0.0
        self._pull_tick_counter: int = 0

        # window pushing/dragging state
        self._push_window_info: tuple | None = None  # (QRect, pid)
        self._push_corner: str = "left"              # which corner sprite is on ("left" or "right")
        self._push_ticks: int = 0
        self._push_dir: int = 1                      # walking direction while pushing

        # window peeking state
        self._peek_ticks: int = 0
        self._peek_corner: str = "left"

        # cursor following
        self._following_cursor: bool = False

        # position offset (debug)
        self._offset = Vec2()  # added to final position each tick

        self._restlessness = 0  # 0–4, set by RestlessnessEngine via set_restlessness()
        self._facing = "left"   # current facing direction (default matches most sprite packs)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    @property
    def current_posture(self) -> str:
        return self._posture.value

    def set_restlessness(self, level: int):
        """Called by RestlessnessEngine when level changes (0–4)."""
        self._restlessness = max(0, min(4, level))
        # if she's sitting still and we get restless, nudge her into motion
        if level >= 1 and self._state == PhysicsState.GROUNDED and self._walk_dir == 0:
            self._wander_ticks = 1   # trigger a wander decision next tick

    def _restless_wall_grab_chance(self) -> float:
        return _RESTLESS_PARAMS.get(self._restlessness, _RESTLESS_PARAMS[0])[1]

    def _restless_climb_duration(self) -> tuple[int, int]:
        mul = _RESTLESS_PARAMS.get(self._restlessness, _RESTLESS_PARAMS[0])[2]
        lo  = int(CLIMB_DURATION[0] * mul)
        hi  = int(CLIMB_DURATION[1] * mul)
        return lo, hi

    # --- event animation lock ---

    def update_platforms(self, platforms: list):
        """Update list of window platforms. Each entry is (QRect, pid) or just QRect."""
        self._platforms = platforms

    def force_reland(self):
        """Drop from current position so she re-lands on updated surfaces."""
        if self._state == PhysicsState.GROUNDED:
            self._state = PhysicsState.FALLING
            self._set_posture(PostureState.FALLING)
            self.locomotion_action.emit("fall")

    def lock_for_event(self):
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING,
                           PhysicsState.PUSHING_WINDOW, PhysicsState.PEEKING):
            return
        self._event_locked = True
        self._walk_dir = 0
        self._vel.x = 0
        self._following_cursor = False

    def unlock(self):
        self._event_locked = False
        self._schedule_wander()

    # --- drag ---

    def on_drag_start(self, cursor_global: QPoint):
        self._dragged = True
        self._reset_window_pull()
        self._state = PhysicsState.DRAGGED
        self._vel = Vec2()
        self._drag_offset = cursor_global - self._window.pos()
        self._cursor_history.clear()
        self._set_posture(PostureState.DRAGGED)
        self.locomotion_action.emit("drag")

    def on_drag_move(self, cursor_global: QPoint):
        if not self._dragged:
            return
        self._cursor_history.append(cursor_global)
        if len(self._cursor_history) > VELOCITY_HISTORY:
            self._cursor_history.pop(0)
        self._window.move(cursor_global - self._drag_offset)

    def on_drag_release(self, cursor_global: QPoint):
        if not self._dragged:
            return
        self._dragged = False
        self._state = PhysicsState.FALLING
        self._set_posture(PostureState.FALLING)

        if len(self._cursor_history) >= 2:
            dx = cursor_global.x() - self._cursor_history[0].x()
            dy = cursor_global.y() - self._cursor_history[0].y()
            n = len(self._cursor_history)
            vx, vy = dx / n, dy / n
            self._vel.x = vx if abs(vx) > 3.0 else 0.0
            self._vel.y = vy if abs(vy) > 2.0 else 0.0
        else:
            self._vel = Vec2()

        self.locomotion_action.emit("fall")

    # --- jumping ---

    def jump_toward(self, target_x: float, target_y: float):
        """Launch sprite toward a target position with a parabolic arc."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.PUSHING_WINDOW):
            return
        pos = self._window.pos()
        x, y = float(pos.x()), float(pos.y())
        dx = target_x - x
        dy = target_y - y
        dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
        # normalize and apply impulse; always have a minimum upward component
        self._vel.x = (dx / dist) * JUMP_IMPULSE_X
        self._vel.y = min(JUMP_MIN_HEIGHT, (dy / dist) * JUMP_IMPULSE_Y)
        self._reset_window_pull()
        self._hanging = False
        self._climbing_window = None
        self._state = PhysicsState.FALLING
        self._set_posture(PostureState.FALLING)
        self.locomotion_action.emit("jump")

    def jump_burst(self, direction: int = 1):
        """Burst outward (for subagent spawn). direction: 1=right, -1=left."""
        self._vel.x = direction * JUMP_IMPULSE_X * 0.8
        self._vel.y = JUMP_IMPULSE_Y * 0.6
        self._state = PhysicsState.FALLING
        self._set_posture(PostureState.FALLING)

    # --- window interaction: push/drag ---

    def start_window_push(self, window_rect: QRect, pid: int, corner: str):
        """Start pushing a window. corner: "left" or "right"."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING):
            return
        self._push_window_info = (window_rect, pid)
        self._push_corner = corner
        self._push_ticks = random.randint(*WINDOW_PUSH_DURATION)
        # push direction: from left corner → push right, from right corner → push left
        self._push_dir = 1 if corner == "left" else -1
        # face the window (opposite of push direction)
        self._set_facing("right" if corner == "left" else "left")
        self._state = PhysicsState.PUSHING_WINDOW
        self._set_posture(PostureState.PUSHING)
        self._reset_window_pull()
        self.locomotion_action.emit("window_push")

    def start_window_peek(self, window_rect: QRect, pid: int, corner: str):
        """Start peeking from a window corner."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING):
            return
        self._push_window_info = (window_rect, pid)  # reuse for position reference
        self._peek_corner = corner
        self._peek_ticks = random.randint(*WINDOW_PEEK_DURATION)
        self._set_facing("right" if corner == "left" else "left")
        self._state = PhysicsState.PEEKING
        self._set_posture(PostureState.PEEKING)
        self.locomotion_action.emit("window_peek")

    def start_window_throw(self, window_rect: QRect, pid: int, corner: str):
        """Grab a window corner and throw it. Emits window_throw signal for main to handle."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING):
            return
        # face the window, throw direction is AWAY from window (over shoulder)
        self._set_facing("right" if corner == "left" else "left")
        throw_dir = "left" if corner == "left" else "right"
        self.locomotion_action.emit("window_throw")
        self.window_throw.emit(pid, window_rect, throw_dir)

    def set_offset(self, dx: float, dy: float):
        """Set debug position offset."""
        self._offset = Vec2(dx, dy)

    # --- main tick ---

    def _tick(self):
        if self._dragged:
            return

        screen = self._screen_rect()
        pos = self._window.pos()
        w, h = self._window.width(), self._window.height()
        x, y = float(pos.x()), float(pos.y())

        screen_floor = float(screen.bottom() - h)
        ceil_y  = screen.top()
        left_x  = screen.left()
        right_x = screen.right() - w

        if self._state == PhysicsState.FALLING:
            self._vel.y = min(self._vel.y + GRAVITY, MAX_FALL_SPEED)
            old_y = y
            x += self._vel.x
            y += self._vel.y

            target_floor = self._find_surface_below(x, old_y, screen_floor)
            if y >= target_floor:
                y = target_floor
                self._vel.y = 0
                self._vel.x = 0
                self._land(target_floor)
            elif y <= ceil_y:
                y = ceil_y
                self._vel.y = abs(self._vel.y) * 0.3

            # hard clamp: never fall below the screen floor
            if y > screen_floor:
                y = screen_floor
                self._vel.y = 0
                self._vel.x = 0
                self._land(screen_floor)

            if x <= left_x:
                x = left_x
                if not self._event_locked and random.random() < self._restless_wall_grab_chance():
                    self._start_wall_climb(PhysicsState.WALL_LEFT)
                else:
                    self._vel.x = abs(self._vel.x) * 0.5
            elif x >= right_x:
                x = right_x
                if not self._event_locked and random.random() < self._restless_wall_grab_chance():
                    self._start_wall_climb(PhysicsState.WALL_RIGHT)
                else:
                    self._vel.x = -abs(self._vel.x) * 0.5

        elif self._state == PhysicsState.GROUNDED:
            self._vel.x *= 0.7
            if abs(self._vel.x) < 0.5:
                self._vel.x = 0.0

            if not self._event_locked:
                self._wander_ticks -= 1
                if self._wander_ticks <= 0:
                    self._decide_wander()

                speed = RUN_SPEED if self._running else WALK_SPEED
                x += self._walk_dir * speed

                if x <= left_x:
                    x = left_x
                    # walking into walls: much lower grab chance (climbing is for intentional jumps)
                    if random.random() < self._restless_wall_grab_chance() * 0.3:
                        self._reset_window_pull()
                        self._start_wall_climb(PhysicsState.WALL_LEFT)
                    else:
                        self._walk_dir = 1
                        self._set_facing("right")
                        self.locomotion_action.emit("walk")
                elif x >= right_x:
                    x = right_x
                    if random.random() < self._restless_wall_grab_chance() * 0.3:
                        self._reset_window_pull()
                        self._start_wall_climb(PhysicsState.WALL_RIGHT)
                    else:
                        self._walk_dir = -1
                        self._set_facing("left")
                        self.locomotion_action.emit("walk")
                elif self._walk_dir != 0:
                    # check if walking into the side of a window
                    wall_hit = self._window_wall_at(x, self._walk_dir)
                    if wall_hit and random.random() < self._restless_wall_grab_chance() * 0.3:
                        wall_side, w_rect, w_pid = wall_hit
                        self._reset_window_pull()
                        wall_state = PhysicsState.WALL_RIGHT if wall_side == "right" else PhysicsState.WALL_LEFT
                        self._start_wall_climb(wall_state, window_info=(w_rect, w_pid))

            # sitting timer
            if self._walk_dir == 0 and not self._event_locked:
                self._still_ticks += 1
                if self._still_ticks >= SITTING_TIMEOUT and self._posture != PostureState.SITTING:
                    self._set_posture(PostureState.SITTING)
            else:
                self._still_ticks = 0

            # window pull: sprite weight gradually drags the window down
            if (self._standing_on_window is not None
                    and WINDOW_PULL_DISTANCE > 0
                    and self._window_pull_applied < WINDOW_PULL_DISTANCE):
                self._pull_tick_counter += 1
                if self._pull_tick_counter >= WINDOW_PULL_INTERVAL:
                    self._pull_tick_counter = 0
                    delta = min(WINDOW_PULL_SPEED * WINDOW_PULL_INTERVAL,
                                WINDOW_PULL_DISTANCE - self._window_pull_applied)
                    if delta > 0:
                        rect, pid = self._standing_on_window
                        self._window_pull_applied += delta
                        self._floor_y += delta
                        y += delta
                        self.pull_window.emit(pid, rect, delta)

            # edge detection: walked off a surface?
            if not self._surface_at(x, self._floor_y, screen_floor):
                self._reset_window_pull()
                self._state = PhysicsState.FALLING
                self._set_posture(PostureState.FALLING)
                self.locomotion_action.emit("fall")

        elif self._state in (PhysicsState.WALL_LEFT, PhysicsState.WALL_RIGHT):
            x = self._climb_x
            on_screen_edge = (abs(x - left_x) < 2.0 or abs(x - right_x) < 2.0)

            if self._hanging:
                # hanging: just dangle, don't climb
                self._climb_ticks -= 1
                if self._climb_ticks <= 0:
                    self._hanging = False
                    self._climbing_window = None
                    self._state = PhysicsState.FALLING
                    self._set_posture(PostureState.FALLING)
                    self.locomotion_action.emit("fall")
            else:
                # climbing upward
                y -= CLIMB_SPEED

                # side-climbing a window: pull it down
                if self._climbing_window is not None:
                    self._side_pull_counter += 1
                    if self._side_pull_counter >= SIDE_PULL_INTERVAL:
                        self._side_pull_counter = 0
                        cw_rect, cw_pid = self._climbing_window
                        delta = SIDE_PULL_SPEED * SIDE_PULL_INTERVAL
                        self.pull_window.emit(cw_pid, cw_rect, delta)

                if on_screen_edge and y <= ceil_y:
                    # reached the top of a screen wall — transition to ceiling
                    y = float(ceil_y)
                    self._ceiling_dir = 1 if self._state == PhysicsState.WALL_LEFT else -1
                    self._state = PhysicsState.CEILING
                    self._climb_ticks = random.randint(*CEILING_DURATION)
                    self._hanging = False
                    self._climbing_window = None
                    self._set_posture(PostureState.CEILING)
                    self._set_facing("right" if self._ceiling_dir > 0 else "left")
                    self.locomotion_action.emit("ceiling")
                else:
                    self._climb_ticks -= 1
                    # check if reached top of a window we're climbing
                    top_of_surface = self._find_surface_below(x, y - 1, screen_floor)
                    climbed_over = not on_screen_edge and top_of_surface < y
                    if climbed_over:
                        # reached window top — jump inward so she lands on the surface
                        # (not teetering on the edge where she'd immediately fall off)
                        self._climbing_window = None
                        self._hanging = False
                        inward = 20.0  # px inward from the wall edge
                        if self._state == PhysicsState.WALL_LEFT:
                            x += inward
                        else:
                            x -= inward
                        y = top_of_surface
                        self._land(top_of_surface)
                    elif self._climb_ticks <= 0:
                        # climb duration expired — hang or fall
                        if random.random() < HANG_CHANCE:
                            self._hanging = True
                            self._climb_ticks = random.randint(*HANG_DURATION)
                            self._set_posture(PostureState.HANGING)
                            self.locomotion_action.emit("hang")
                        else:
                            self._hanging = False
                            self._climbing_window = None
                            self._state = PhysicsState.FALLING
                            self._set_posture(PostureState.FALLING)
                            self.locomotion_action.emit("fall")

        elif self._state == PhysicsState.CEILING:
            y = float(ceil_y)
            if self._hanging:
                # hanging from ceiling: stationary
                self._climb_ticks -= 1
                if self._climb_ticks <= 0:
                    self._hanging = False
                    self._state = PhysicsState.FALLING
                    self._set_posture(PostureState.FALLING)
                    self.locomotion_action.emit("fall")
            else:
                # crawl along ceiling, flip sprite when bouncing off screen edges
                prev_dir = self._ceiling_dir
                x += self._ceiling_dir * CEILING_CRAWL_SPEED
                if x <= left_x:
                    x = float(left_x)
                    self._ceiling_dir = 1
                elif x >= right_x:
                    x = float(right_x)
                    self._ceiling_dir = -1
                if self._ceiling_dir != prev_dir:
                    self._set_facing("right" if self._ceiling_dir > 0 else "left")
                    self.locomotion_action.emit("ceiling")
                self._climb_ticks -= 1
                if self._climb_ticks <= 0:
                    # maybe hang instead of falling
                    if random.random() < HANG_CHANCE:
                        self._hanging = True
                        self._climb_ticks = random.randint(*HANG_DURATION)
                        self._set_posture(PostureState.HANGING)
                        self.locomotion_action.emit("hang_ceiling")
                    else:
                        self._state = PhysicsState.FALLING
                        self._set_posture(PostureState.FALLING)
                        self.locomotion_action.emit("fall")

        elif self._state == PhysicsState.PUSHING_WINDOW:
            # walking slowly while pushing/dragging a window
            x += self._push_dir * WINDOW_PUSH_SPEED
            self._push_ticks -= 1

            # emit window move signal periodically
            if self._push_window_info and self._push_ticks % WINDOW_PUSH_EMIT_INTERVAL == 0:
                pw_rect, pw_pid = self._push_window_info
                dx_push = self._push_dir * WINDOW_PUSH_SPEED * WINDOW_PUSH_EMIT_INTERVAL
                self.push_window_move.emit(pw_pid, pw_rect, dx_push, 0)

            # done pushing, or hit screen edge
            if self._push_ticks <= 0 or x <= left_x or x >= right_x:
                x = max(float(left_x), min(x, float(right_x)))
                self._push_window_info = None
                self._state = PhysicsState.GROUNDED
                self._set_posture(PostureState.STANDING)
                self._schedule_wander()
                # no emission — hold last frame, wander timer will pick next action

        elif self._state == PhysicsState.PEEKING:
            # peeking from window corner: stationary, countdown
            self._peek_ticks -= 1
            if self._peek_ticks <= 0:
                self._push_window_info = None
                self._state = PhysicsState.GROUNDED
                self._set_posture(PostureState.STANDING)
                self._schedule_wander()
                # no emission — hold last frame, wander timer will pick next action

        # final safety clamp: never let her leave the screen bounds
        x = max(float(left_x), min(x, float(right_x)))
        y = max(float(ceil_y), min(y, screen_floor))

        # apply debug offset (doesn't affect physics, just rendering position)
        self._window.move(int(x + self._offset.x), int(y + self._offset.y))


    # --- helpers ---

    def _set_posture(self, posture: PostureState):
        if posture != self._posture:
            self._posture = posture
            self.posture_changed.emit(posture.value)

    def _set_facing(self, direction: str):
        if direction != self._facing:
            self._facing = direction
            self.facing_changed.emit(direction)

    def _land(self, floor_y: float):
        self._floor_y = floor_y
        self._state = PhysicsState.GROUNDED
        self._walk_dir = 0
        self._still_ticks = 0
        self._running = False
        self._set_posture(PostureState.STANDING)
        self._schedule_wander()
        # signal landing so fall animation can play its outro and finish
        self.locomotion_action.emit("land")
        # check if we landed on a window (for weight-pulling)
        self._standing_on_window = self._find_platform_at(floor_y)
        self._window_pull_applied = 0.0
        self._pull_tick_counter = 0

    def _start_wall_climb(self, wall: PhysicsState, window_info: tuple | None = None):
        self._state = wall
        self._vel = Vec2()
        self._climb_ticks = random.randint(*self._restless_climb_duration())
        self._climb_x = float(self._window.pos().x())  # pin to current x
        self._hanging = False
        self._climbing_window = window_info  # (QRect, pid) if climbing a window, None for screen edge
        self._side_pull_counter = 0
        self._set_posture(PostureState.CLIMBING)
        # left wall: sprite faces left (toward wall). right wall: faces right.
        self._set_facing("left" if wall == PhysicsState.WALL_LEFT else "right")
        self.locomotion_action.emit("climb")

    def _unpack_platform(self, entry) -> tuple:
        """Unpack a platform entry: (QRect, pid) or bare QRect → (QRect, pid)."""
        if isinstance(entry, tuple):
            return entry[0], entry[1]
        return entry, 0  # bare QRect, no pid

    def _find_surface_below(self, x: float, y_from: float, screen_floor: float) -> float:
        """Highest surface at position x that is at or below y_from (first thing she'd land on)."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        best = screen_floor
        for entry in self._platforms:
            platform, _pid = self._unpack_platform(entry)
            if x + miku_w > platform.left() and x < platform.right():
                surface = float(platform.top()) - miku_h
                # must be at or below y_from (she's above it), and better than current best
                if surface >= y_from and surface < best:
                    best = surface
        return best

    def _surface_at(self, x: float, floor_y: float, screen_floor: float) -> bool:
        """Is there a walkable surface at (x, floor_y)? Used for edge detection."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        if abs(floor_y - screen_floor) < SURFACE_TOLERANCE:
            return True
        for entry in self._platforms:
            platform, _pid = self._unpack_platform(entry)
            if x + miku_w > platform.left() and x < platform.right():
                surface = float(platform.top()) - miku_h
                if abs(surface - floor_y) < SURFACE_TOLERANCE:
                    return True
        return False

    def _find_platform_at(self, floor_y: float):
        """Find which platform we're standing on (if any). Returns (QRect, pid) or None."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        x = float(self._window.pos().x())
        screen = self._screen_rect()
        screen_floor = float(screen.bottom() - self._window.height())
        # if standing on screen floor, not on a window
        if abs(floor_y - screen_floor) < SURFACE_TOLERANCE:
            return None
        for entry in self._platforms:
            platform, pid = self._unpack_platform(entry)
            if x + miku_w > platform.left() and x < platform.right():
                surface = float(platform.top()) - miku_h
                if abs(surface - floor_y) < SURFACE_TOLERANCE:
                    return (platform, pid)
        return None

    def _reset_window_pull(self):
        """Reset window pull state (called when leaving a platform)."""
        self._standing_on_window = None
        self._window_pull_applied = 0.0
        self._pull_tick_counter = 0

    def _window_wall_at(self, x: float, walk_dir: int):
        """Check if miku is walking into the side of a window.
        Returns (wall_side, QRect, pid) or None. wall_side is 'left'/'right'."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        miku_y = float(self._window.pos().y())
        miku_bottom = miku_y + miku_h
        tolerance = 4.0

        for entry in self._platforms:
            platform, _pid = self._unpack_platform(entry)
            # window must extend above miku's feet (something to climb)
            if platform.top() >= miku_bottom:
                continue
            # walking right → hitting a window's left side
            if walk_dir > 0:
                wall_x = float(platform.left()) - miku_w
                if abs(x - wall_x) < tolerance:
                    return ("right", platform, _pid)
            # walking left → hitting a window's right side
            elif walk_dir < 0:
                wall_x = float(platform.right())
                if abs(x - wall_x) < tolerance:
                    return ("left", platform, _pid)
        return None

    def _nearby_window(self, max_dist: float = 100.0):
        """Find a window near miku's position for interaction.
        Returns (corner, QRect, pid) or None. corner is 'left'/'right'."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        pos = self._window.pos()
        mx, my = float(pos.x()), float(pos.y())
        miku_bottom = my + miku_h

        for entry in self._platforms:
            platform, pid = self._unpack_platform(entry)
            # window top must be near miku's feet (reachable height)
            if abs(platform.top() - miku_bottom) > max_dist:
                continue
            # check left corner
            if abs(mx - platform.left()) < max_dist:
                return ("left", platform, pid)
            # check right corner
            if abs((mx + miku_w) - platform.right()) < max_dist:
                return ("right", platform, pid)
        return None

    def _pick_random_window(self):
        """Pick a random window to walk toward for interaction.
        Returns (corner, QRect, pid) or None."""
        if not self._platforms:
            return None
        miku_h = float(self._window.height())
        miku_y = float(self._window.pos().y())
        miku_bottom = miku_y + miku_h
        screen = self._screen_rect()
        screen_floor = float(screen.bottom() - self._window.height())

        # only consider windows roughly at the same level (on the same surface)
        candidates = []
        for entry in self._platforms:
            platform, pid = self._unpack_platform(entry)
            # window top within ~200px of miku's feet vertically
            if abs(platform.top() - miku_bottom) < 200:
                # pick the closer corner
                mx = float(self._window.pos().x())
                left_dist = abs(mx - platform.left())
                right_dist = abs(mx - platform.right())
                corner = "left" if left_dist < right_dist else "right"
                candidates.append((corner, platform, pid))

        if not candidates:
            return None
        return random.choice(candidates)

    def _schedule_wander(self):
        mul = _RESTLESS_PARAMS.get(self._restlessness, _RESTLESS_PARAMS[0])[0]
        lo  = int(IDLE_WANDER_INTERVAL[0] * mul)
        hi  = int(IDLE_WANDER_INTERVAL[1] * mul)
        self._wander_ticks = random.randint(max(1, lo), max(2, hi))

    def _decide_wander(self):
        rest = self._restlessness

        # at higher restlessness, roll for special behaviors before falling back to walk/sit
        if rest >= 2:
            roll = random.random()
            # divide the probability space among special behaviors
            cursor_chance = CURSOR_FOLLOW_CHANCE.get(rest, 0)
            window_near_chance = 0.15 if rest >= 2 else 0
            window_seek_chance = WINDOW_SEEK_CHANCE.get(rest, 0)
            # cumulative thresholds
            t_cursor = cursor_chance
            t_window_near = t_cursor + window_near_chance
            t_window_seek = t_window_near + window_seek_chance

            if roll < t_cursor:
                if self._try_cursor_follow():
                    self._schedule_wander()
                    return

            elif roll < t_window_near:
                nearby = self._nearby_window()
                if nearby:
                    corner, w_rect, w_pid = nearby
                    self._do_window_interaction(w_rect, w_pid, corner)
                    self._schedule_wander()
                    return

            elif roll < t_window_seek:
                target = self._pick_random_window()
                if target:
                    corner, w_rect, w_pid = target
                    # jump toward the window — much more dynamic than walking
                    target_x = float(w_rect.left() if corner == "left" else w_rect.right())
                    target_y = float(w_rect.top()) - self._window.height()
                    self.jump_toward(target_x, target_y)
                    self._schedule_wander()
                    return

        # all movement options are weighted choices — no default dominates
        use_run = rest >= 2 and random.random() < 0.3
        options = []

        # walk/run (always available, dominant at low restlessness)
        walk_w = 0.35 if rest <= 1 else 0.25
        options.append(("walk_left", walk_w))
        options.append(("walk_right", walk_w))

        # stand still (just hold pose, no animation change)
        stand_w = 0.20 if rest <= 1 else 0.10
        options.append(("stand", stand_w))

        # idle animation (sit_idle / idle tiers — a deliberate behavior, not a default)
        idle_w = 0.10 if rest <= 1 else 0.05
        options.append(("idle", idle_w))

        # weighted random pick
        total = sum(w for _, w in options)
        roll = random.random() * total
        cumulative = 0.0
        choice = "stand"
        for name, weight in options:
            cumulative += weight
            if roll < cumulative:
                choice = name
                break

        if choice == "walk_left":
            self._walk_dir = -1
            self._still_ticks = 0
            self._running = use_run
            self._following_cursor = False
            self._set_posture(PostureState.WALKING)
            self._set_facing("left")
            self.locomotion_action.emit("run" if use_run else "walk")
        elif choice == "walk_right":
            self._walk_dir = 1
            self._still_ticks = 0
            self._running = use_run
            self._following_cursor = False
            self._set_posture(PostureState.WALKING)
            self._set_facing("right")
            self.locomotion_action.emit("run" if use_run else "walk")
        elif choice == "idle":
            # deliberate idle animation — main.py resolves to sit_idle/idle1-5
            self._walk_dir = 0
            self._running = False
            self._following_cursor = False
            self._set_posture(PostureState.STANDING)
            self.locomotion_action.emit("idle")
        else:
            # stand: just stop, hold frame, don't emit — wander timer picks next
            self._walk_dir = 0
            self._running = False
            self._following_cursor = False
            self._set_posture(PostureState.STANDING)
        self._schedule_wander()

    def _do_window_interaction(self, w_rect, w_pid, corner):
        """Pick and start a window interaction based on restlessness."""
        rest = self._restlessness
        roll = random.random()
        if rest >= 4 and roll < 0.25:
            self.start_window_throw(w_rect, w_pid, corner)
        elif rest >= 3 and roll < 0.50:
            self.start_window_push(w_rect, w_pid, corner)
        elif roll < 0.60:
            self.start_window_peek(w_rect, w_pid, corner)
        else:
            self.start_window_push(w_rect, w_pid, corner)

    def _try_cursor_follow(self) -> bool:
        """Try to walk/jump toward cursor. Returns True if following."""
        try:
            cursor = QCursor.pos()
        except Exception:
            return False
        pos = self._window.pos()
        mx = float(pos.x()) + self._window.width() / 2
        my = float(pos.y()) + self._window.height() / 2
        cx, cy = float(cursor.x()), float(cursor.y())
        dx = cx - mx
        dy = cy - my
        dist = (dx * dx + dy * dy) ** 0.5

        if dist < 30:
            return False  # already at cursor

        # jump if close enough and lucky
        if dist < CURSOR_JUMP_DISTANCE and random.random() < CURSOR_JUMP_CHANCE:
            self.jump_toward(cx, cy)
            self._following_cursor = True
            return True

        # walk toward cursor
        self._walk_dir = 1 if dx > 0 else -1
        self._still_ticks = 0
        self._running = self._restlessness >= 3
        self._following_cursor = True
        self._set_posture(PostureState.WALKING)
        self._set_facing("right" if dx > 0 else "left")
        self.locomotion_action.emit("run" if self._running else "walk")
        return True

    def _screen_rect(self) -> QRect:
        # use whichever screen Miku is currently on, not always primary
        pos = self._window.pos()
        center = QPoint(
            pos.x() + self._window.width() // 2,
            pos.y() + self._window.height() // 2,
        )
        screen = QApplication.screenAt(center)
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
