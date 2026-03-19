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
  level 3+: window push/drag, window carry (grab + walk with window)
  level 4+: window throw (minimize), carry → throw (grab, carry, hurl)

Z-ordering:
  Platforms carry (QRect, pid, window_number, z_index) where z_index 0 = frontmost.
  When miku interacts with a non-topmost window, z_context_changed is emitted so
  main.py can lower her NSWindow level to match. Occlusion checks prevent her from
  walking/climbing too far behind higher-z windows (soft wall at ~half sprite width).
"""

from __future__ import annotations
import random
from dataclasses import dataclass
from enum import Enum, auto
from PyQt6.QtCore import QObject, QTimer, QPoint, QRect, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication


# --- constants ---

TICK_MS = 16
GRAVITY = 0.6
MAX_FALL_SPEED = 20
WALK_SPEED   = 2
RUN_SPEED    = 4
SPRINT_SPEED = 8     # full dash at high restlessness
IDLE_WANDER_INTERVAL = (180, 420)
VELOCITY_HISTORY = 3     # fewer samples = snappier throw response
SITTING_TIMEOUT = 300
SURFACE_TOLERANCE = 4.0

# wall climbing / ceiling crawling
WALL_GRAB_CHANCE    = 0.35
CLIMB_SPEED         = 1.5
CLIMB_DURATION      = (120, 360)
CEILING_CRAWL_SPEED = 1.2
CEILING_DURATION    = (120, 300)

# hanging
HANG_CHANCE         = 0.35
HANG_DURATION       = (180, 420)

# jumping
JUMP_IMPULSE_X      = 8.0
JUMP_IMPULSE_Y      = -16.0
JUMP_MIN_HEIGHT     = -10.0

# cursor following (restlessness-gated)
CURSOR_FOLLOW_CHANCE = {2: 0.15, 3: 0.30, 4: 0.45}
CURSOR_CHASE_TICKS   = {2: (90, 180), 3: (120, 250), 4: (180, 400)}
CURSOR_LUNGE_RANGE_X = 350     # horizontal distance to stop and lunge from (she doesn't need to be right under!)
CURSOR_LUNGE_BUDGET  = {2: (1, 2), 3: (2, 3), 4: (2, 4)}
CURSOR_LUNGE_COOLDOWN = 25     # ticks between lunges (land + reorient)

# window pushing/dragging
WINDOW_PUSH_SPEED     = 1.0
WINDOW_PUSH_DURATION  = (180, 600)

# window peeking
WINDOW_PEEK_DURATION  = (120, 300)

# window throwing
WINDOW_THROW_ARC_STEPS = 15

# window carrying (grab window, walk/run with it, optionally throw)
CARRY_PERCH_TICKS      = (90, 160)    # ~1.5-2.5s sitting on edge before grab
CARRY_WALK_DURATION    = (250, 500)   # how long she walks carrying the window
CARRY_WALK_SPEED       = 1.5          # slower than normal walk (she's hauling a window!)
CARRY_RUN_SPEED        = 3.0          # still slower than normal run
CARRY_ABORT_PERCH      = 0.10         # chance to bail after perching (she jumped for this, commit!)
CARRY_ABORT_PER_TICK   = 0.002        # small per-tick chance to drop window during carry
CARRY_THROW_CHANCE     = {3: 0.30, 4: 0.55}  # chance to throw vs gently set down at end

# trip/stumble (rare event during run, gated by restlessness)
TRIP_CHANCE = {2: 0.03, 3: 0.06, 4: 0.10}  # per wander decision while running

# edge leap (deliberate jump off platform edge instead of just falling)
EDGE_LEAP_CHANCE     = 0.40   # chance to leap instead of fall when walking off edge
EDGE_LEAP_IMPULSE_X  = 5.0   # horizontal impulse for edge leap
EDGE_LEAP_IMPULSE_Y  = -8.0  # upward impulse for edge leap

# drag intensity (how far sprite dangles from grab point)
DRAG_CALM_DISTANCE   = 30    # within this many px of grab point = calm
DRAG_MILD_DISTANCE   = 80    # 30-80px = mild dangle; beyond 80 = strong
DRAG_INTENSITY_LEVELS = ("calm", "mild", "strong")

# throw-to-wall (high velocity release → guaranteed wall cling)
THROW_VELOCITY_THRESHOLD = 6.0    # velocity magnitude to count as "thrown"
THROWN_WALL_GRAB_CHANCE  = 0.90   # near-guaranteed wall grab when thrown into a wall
THROW_VELOCITY_SCALE     = 1.5   # amplify release velocity for satisfying throws

# side-climbing window pull
SIDE_PULL_SPEED       = 0.3
SIDE_PULL_INTERVAL    = 8

# window pull (sprite weight on windows)
WINDOW_PULL_DISTANCE = 0   # override via [physics] window_pull_distance in config.toml
WINDOW_PULL_SPEED    = 0.5
WINDOW_PULL_INTERVAL = 5

# restlessness modifiers: (wander_interval_mul, wall_grab_chance, climb_duration_mul)
_RESTLESS_PARAMS = {
    0: (1.0,  0.00, 1.0),   # calm: no climbing
    1: (0.6,  0.25, 1.0),   # fidgety: shorter pauses, sometimes climbs
    2: (0.4,  0.35, 1.2),   # climby: more climbing, window interactions
    3: (0.35, 0.45, 1.4),   # grabby: frequent climbing, window interactions
    4: (0.25, 0.50, 1.6),   # feral: lots of everything
}

WINDOW_SEEK_CHANCE = {2: 0.15, 3: 0.25, 4: 0.35}


# --- enums ---

class PhysicsState(Enum):
    GROUNDED       = auto()
    FALLING        = auto()
    WALL_LEFT      = auto()
    WALL_RIGHT     = auto()
    CEILING        = auto()
    DRAGGED        = auto()
    PUSHING_WINDOW  = auto()
    PEEKING         = auto()
    CARRYING_WINDOW = auto()


class PostureState(Enum):
    STANDING = "standing"
    SITTING  = "sitting"
    WALKING  = "walking"
    FALLING  = "falling"
    CLIMBING = "climbing"
    CEILING  = "ceiling"
    DRAGGED  = "dragged"
    HANGING  = "hanging"
    PUSHING  = "pushing"
    PEEKING  = "peeking"
    CARRYING = "carrying"


# --- small state bundles ---

@dataclass
class Vec2:
    x: float = 0.0
    y: float = 0.0


@dataclass
class ClimbState:
    ticks: int = 0
    pin_x: float = 0.0       # x position pinned to while climbing
    ceiling_dir: int = 1     # horizontal crawl direction on ceiling
    hanging: bool = False
    window: tuple | None = None   # (QRect, pid) when climbing a window side
    side_pull_counter: int = 0
    side_pull_cumulative: float = 0.0  # total downward pull applied to window


@dataclass
class PushState:
    window: tuple | None = None   # (QRect, pid)
    corner: str = "left"
    ticks: int = 0
    direction: int = 1
    window_x: float = 0.0        # current absolute window x position
    window_y: float = 0.0        # current absolute window y position


@dataclass
class CarryState:
    window: tuple | None = None   # (QRect, pid)
    phase: str = "jump"           # jump → grab_fall → carry → throw_windup → done
    ticks: int = 0
    walk_dir: int = 1
    running: bool = False
    window_x: float = 0.0        # current absolute window x position
    window_y: float = 0.0        # current absolute window y position
    grab_y: float = 0.0          # y position to grab the window at (bottom of window)
    vel_y: float = 0.0           # vertical velocity during jump/grab_fall
    offset_x: float = 0.0        # fixed offset: window_x = sprite_x + offset_x
    offset_y: float = 0.0        # fixed offset: window_y = sprite_y + offset_y

    def reset(self):
        self.window = None
        self.phase = "jump"
        self.ticks = 0
        self.window_x = 0.0
        self.window_y = 0.0
        self.grab_y = 0.0
        self.vel_y = 0.0
        self.offset_x = 0.0
        self.offset_y = 0.0


@dataclass
class PullState:
    """Tracks weight-pulling on the window we're standing on."""
    standing_on: tuple | None = None   # (QRect, pid) or None
    applied: float = 0.0
    tick_counter: int = 0

    def reset(self):
        self.standing_on = None
        self.applied = 0.0
        self.tick_counter = 0


@dataclass
class CursorChaseState:
    """Tracks an active cursor chase: sprint → lunge → lunge → give up."""
    active: bool = False
    lunges_left: int = 0
    cooldown: int = 0        # ticks until next lunge allowed (land + reorient)
    phase: str = "approach"  # "approach" (sprinting toward) or "lunging" (in the air / recovering)

    def reset(self):
        self.active = False
        self.lunges_left = 0
        self.cooldown = 0
        self.phase = "approach"


# --- helpers ---

def _restless_params(level: int) -> tuple:
    return _RESTLESS_PARAMS.get(level, _RESTLESS_PARAMS[0])


def _weighted_choice(options: list[tuple[str, float]]) -> str:
    """Pick from [(name, weight), ...] using weighted random selection."""
    total = sum(w for _, w in options)
    roll = random.random() * total
    cumulative = 0.0
    for name, weight in options:
        cumulative += weight
        if roll < cumulative:
            return name
    return options[-1][0]  # fallback


# --- platform helpers ---
#
# Platforms are (QRect, pid, window_number, z_index) tuples, ordered front-to-back.
# Helpers use _plat_* accessors for readability.

def _plat_rect(p) -> QRect:   return p[0]
def _plat_pid(p) -> int:      return p[1]
def _plat_winnum(p) -> int:   return p[2] if len(p) > 2 else 0
def _plat_zidx(p) -> int:     return p[3] if len(p) > 3 else 0


def _find_surface_below(platforms, x: float, y_from: float, miku_w: float,
                        miku_h: float, screen_floor: float,
                        ignore_pid: int = 0,
                        only_visible: bool = False) -> float:
    """Highest surface at x that is at or below y_from.
    ignore_pid: skip platforms owned by this pid (for drop-through).
    only_visible: skip surfaces occluded by higher-z windows at x."""
    best = screen_floor
    for plat in platforms:
        rect, pid = _plat_rect(plat), _plat_pid(plat)
        if ignore_pid and pid == ignore_pid:
            continue
        if x + miku_w > rect.left() and x < rect.right():
            surface = float(rect.top()) - miku_h
            if surface >= y_from and surface < best:
                if only_visible and _is_surface_occluded(platforms, plat, x, miku_w):
                    continue
                best = surface
    return best


def _is_surface_occluded(platforms, target_plat, x: float, miku_w: float) -> bool:
    """Is the top surface of target_plat hidden by a higher-z window at x?
    A surface is occluded if a window in front covers the same x range at the
    target's top edge (meaning miku would be invisible standing there)."""
    target_rect = _plat_rect(target_plat)
    target_top = float(target_rect.top())
    target_zidx = _plat_zidx(target_plat)
    for plat in platforms:
        if _plat_zidx(plat) >= target_zidx:
            break  # reached target's depth — nothing in front left
        rect = _plat_rect(plat)
        # does this window cover the landing zone at the target's top?
        if (rect.left() < x + miku_w and rect.right() > x
                and rect.top() <= target_top and rect.bottom() > target_top):
            return True
    return False


def _surface_at(platforms, x: float, floor_y: float, miku_w: float,
                miku_h: float, screen_floor: float) -> bool:
    """Is there a walkable surface at (x, floor_y)?"""
    if abs(floor_y - screen_floor) < SURFACE_TOLERANCE:
        return True
    for plat in platforms:
        rect = _plat_rect(plat)
        if x + miku_w > rect.left() and x < rect.right():
            surface = float(rect.top()) - miku_h
            if abs(surface - floor_y) < SURFACE_TOLERANCE:
                return True
    return False


def _find_platform_at(platforms, x: float, floor_y: float, miku_w: float,
                      miku_h: float, screen_floor: float):
    """Which platform is at floor_y? Returns full platform tuple or None."""
    if abs(floor_y - screen_floor) < SURFACE_TOLERANCE:
        return None
    for plat in platforms:
        rect = _plat_rect(plat)
        if x + miku_w > rect.left() and x < rect.right():
            surface = float(rect.top()) - miku_h
            if abs(surface - floor_y) < SURFACE_TOLERANCE:
                return plat
    return None


def _occlusion_wall_ahead(platforms, standing_plat, x: float, walk_dir: int,
                          miku_w: float, miku_h: float) -> float | None:
    """Find the x boundary where an occluding window blocks further travel.
    Returns the x position of the soft wall, or None if the path is clear.
    The wall is placed so miku peeks out by ~half her width."""
    if standing_plat is None:
        return None
    standing_zidx = _plat_zidx(standing_plat)
    standing_rect = _plat_rect(standing_plat)
    standing_top = float(standing_rect.top())
    # miku's body extends from standing_top - miku_h (head) to standing_top (feet)
    miku_top = standing_top - miku_h
    peek_amount = miku_w * 0.4  # how far she peeks out from behind the occluder

    for plat in platforms:
        if _plat_zidx(plat) >= standing_zidx:
            break  # only check windows in front of what she's standing on
        rect = _plat_rect(plat)
        # does this window overlap vertically with miku's body?
        if rect.bottom() <= miku_top or rect.top() >= standing_top:
            continue
        if walk_dir > 0:
            wall_x = float(rect.left()) - miku_w + peek_amount
            # return wall if ahead OR just passed (overshoot by up to SPRINT_SPEED)
            if wall_x >= x - SPRINT_SPEED and wall_x < x + miku_w * 4:
                return wall_x
        elif walk_dir < 0:
            wall_x = float(rect.right()) - peek_amount
            if wall_x <= x + SPRINT_SPEED and wall_x > x - miku_w * 4:
                return wall_x
    return None


# --- engine ---

class PhysicsEngine(QObject):
    locomotion_action      = pyqtSignal(str)
    posture_changed        = pyqtSignal(str)
    facing_changed         = pyqtSignal(str)
    pull_window            = pyqtSignal(int, object, float)
    window_move_to         = pyqtSignal(int, float, float)  # pid, abs_x, abs_y
    window_throw           = pyqtSignal(int, object, str)
    window_toss_up         = pyqtSignal(int, object)        # pid, rect — toss upward, no minimize
    drag_intensity_changed = pyqtSignal(str)  # "calm", "mild", "strong"
    # z-ordering: emits (window_number, z_index) when miku should be layered
    # with a specific window, or (0, -1) when she should float above everything
    z_context_changed      = pyqtSignal(int, int)  # window_number, z_index

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        self._vel = Vec2()
        self._state = PhysicsState.FALLING
        self._posture = PostureState.FALLING
        self._walk_dir = 0
        self._wander_ticks = 0
        self._still_ticks = 0
        self._event_locked = False
        self._running = False
        self._sprinting = False
        self._following_cursor = False
        self._chase = CursorChaseState()
        self._action_walk_speed = 0.0
        self._action_offset_y = 0    # per-action vertical shift (e.g. sitting sprites)
        self._applied_offset_y = 0   # offset that was used in the last move() call
        self._facing = "left"
        self._offset = Vec2()
        self._floor_y = 0.0
        self._restlessness = 0

        # drag
        self._dragged = False
        self._drag_offset = QPoint()
        self._cursor_history: list[QPoint] = []
        self._drag_intensity: str = "calm"  # current dangle level during drag
        self._thrown: bool = False           # True after high-velocity release (boosts wall grab)

        # grouped state
        self._climb = ClimbState()
        self._push = PushState()
        self._pull = PullState()
        self._carry = CarryState()
        self._peek_ticks = 0
        self._drop_through_pid: int = 0  # temporarily ignore this platform's pid when falling

        # platforms: (QRect, pid, window_number, z_index) ordered front-to-back
        self._platforms: list[tuple] = []

        # z-ordering: which window she's currently layered with (0 = floating)
        self._z_window_number: int = 0
        self._z_index: int = -1

        # pending window action: jump to a window, then do something on landing
        # stores (action_name, QRect, pid, corner) or None
        self._pending_window_action: tuple | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    # --- start / stop ---

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    @property
    def current_posture(self) -> str:
        return self._posture.value

    # --- restlessness ---

    def set_restlessness(self, level: int):
        self._restlessness = max(0, min(4, level))
        if level >= 1 and self._state == PhysicsState.GROUNDED and self._walk_dir == 0:
            self._wander_ticks = 1

    def _wall_grab_chance(self) -> float:
        return _restless_params(self._restlessness)[1]

    def _climb_duration(self) -> tuple[int, int]:
        mul = _restless_params(self._restlessness)[2]
        return int(CLIMB_DURATION[0] * mul), int(CLIMB_DURATION[1] * mul)

    # --- platforms ---

    def update_platforms(self, platforms: list):
        """Store platforms as (QRect, pid, window_number, z_index) tuples.
        Accepts both legacy (QRect, pid) and extended (QRect, pid, wnum, zidx) formats."""
        normalized = []
        for entry in platforms:
            if isinstance(entry, tuple):
                if len(entry) >= 4:
                    normalized.append(entry)
                elif len(entry) >= 2:
                    normalized.append((entry[0], entry[1], 0, len(normalized)))
                else:
                    normalized.append((entry[0], 0, 0, len(normalized)))
            else:
                normalized.append((entry, 0, 0, len(normalized)))
        self._platforms = normalized

    # --- event animation lock ---

    def lock_for_event(self):
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING,
                           PhysicsState.PUSHING_WINDOW, PhysicsState.PEEKING,
                           PhysicsState.CARRYING_WINDOW):
            return
        self._event_locked = True
        self._walk_dir = 0
        self._vel.x = 0
        self._following_cursor = False
        self._chase.reset()
        self._action_walk_speed = 0.0

    def unlock(self):
        self._event_locked = False
        self._schedule_wander()

    def force_reland(self):
        if self._state == PhysicsState.GROUNDED:
            self._start_falling()

    def set_action_walk_speed(self, speed: float):
        self._action_walk_speed = speed
        if speed > 0 and self._state == PhysicsState.GROUNDED and self._walk_dir == 0:
            self._walk_dir = random.choice([-1, 1])
            self._set_facing("left" if self._walk_dir < 0 else "right")
            self._set_posture(PostureState.WALKING)

    def set_action_offset_y(self, offset_y: int):
        self._action_offset_y = offset_y

    def set_offset(self, dx: float, dy: float):
        self._offset = Vec2(dx, dy)

    # --- common transitions ---

    def _start_falling(self):
        self._state = PhysicsState.FALLING
        self._set_posture(PostureState.FALLING)
        self.locomotion_action.emit("fall")

    def _start_walking(self, direction: int, run: bool = False, sprint: bool = False):
        self._walk_dir = direction
        self._still_ticks = 0
        self._running = run or sprint
        self._sprinting = sprint
        # note: _following_cursor is NOT cleared here — chase approach uses _start_walking.
        # it's cleared by _end_chase, _decide_wander, lock_for_event, etc. instead.
        self._set_posture(PostureState.WALKING)
        self._set_facing("left" if direction < 0 else "right")
        action = "sprint" if sprint else ("run" if run else "walk")
        self.locomotion_action.emit(action)

    def _return_to_ground(self):
        """Transition from push/peek/carry back to grounded idle."""
        self._push.window = None
        self._walk_dir = 0
        self._running = False
        self._sprinting = False
        self._state = PhysicsState.GROUNDED
        self._set_posture(PostureState.STANDING)
        self._set_z_context(None)  # back to floating
        self.locomotion_action.emit("stand")
        self._schedule_wander()

    def _start_wall_climb(self, wall: PhysicsState, window_info: tuple | None = None):
        self._state = wall
        self._vel = Vec2()
        self._climb.ticks = random.randint(*self._climb_duration())
        self._climb.pin_x = float(self._window.pos().x())
        self._climb.hanging = False
        self._climb.window = window_info
        self._climb.side_pull_counter = 0
        self._climb.side_pull_cumulative = 0.0
        self._set_posture(PostureState.CLIMBING)
        self._set_facing("left" if wall == PhysicsState.WALL_LEFT else "right")
        # z-ordering: layer with the window we're climbing (screen edges stay floating)
        if window_info and not self._is_topmost_platform(window_info):
            self._set_z_context(window_info)
        elif window_info is None:
            self._set_z_context(None)  # screen edge = float
        self.locomotion_action.emit("climb")

    def _maybe_hang_or_fall(self, hang_action: str = "hang"):
        """Climb/ceiling expired — either hang or fall."""
        if random.random() < HANG_CHANCE:
            self._climb.hanging = True
            self._climb.ticks = random.randint(*HANG_DURATION)
            self._set_posture(PostureState.HANGING)
            self.locomotion_action.emit(hang_action)
        else:
            self._climb.hanging = False
            self._climb.window = None
            self._start_falling()

    def _land(self, floor_y: float):
        self._floor_y = floor_y
        self._thrown = False
        self._state = PhysicsState.GROUNDED
        self._walk_dir = 0
        self._still_ticks = 0
        self._running = False
        self._sprinting = False
        self._set_posture(PostureState.STANDING)
        self._schedule_wander()
        self.locomotion_action.emit("land")
        # check if we landed on a window (for weight-pulling + z-ordering)
        miku_w, miku_h = float(self._window.width()), float(self._window.height())
        x = float(self._window.pos().x())
        screen_floor = float(self._screen_rect().bottom() - self._window.height())
        standing = _find_platform_at(self._platforms, x, floor_y, miku_w, miku_h, screen_floor)
        self._pull = PullState(standing_on=standing)
        # z-ordering: layer with the window we landed on (or float if on screen floor)
        if standing and not self._is_topmost_platform(standing):
            self._set_z_context(standing)
        else:
            self._set_z_context(None)
        # pending window action: fire if we landed near the target
        self._check_pending_action(x, floor_y, miku_w, miku_h)

    # --- pending window actions ---

    def jump_and_do(self, action: str, window_rect: QRect, pid: int, corner: str):
        """Jump toward a window corner and perform an action on landing.
        Actions: 'push', 'peek', 'throw', 'side_toss', 'carry'."""
        self._pending_window_action = (action, window_rect, pid, corner)
        # calculate jump target: the corner of the window
        mw = float(self._window.width())
        mh = float(self._window.height())
        if corner == "left":
            tx = float(window_rect.left()) - mw + 4
        else:
            tx = float(window_rect.right()) - 4
        ty = float(window_rect.top()) - mh
        print(f"[claudemeji] pending: jump to {action} window (pid={pid}, corner={corner})")
        self.jump_toward(tx, ty)

    def _check_pending_action(self, x: float, floor_y: float,
                               miku_w: float, miku_h: float):
        """Check if a pending window action should fire after landing."""
        if self._pending_window_action is None:
            return
        action, rect, pid, corner = self._pending_window_action
        self._pending_window_action = None

        # check: did we land on or near the target window?
        # match by pid (landed on it) or by horizontal proximity to its edges
        landed_on_target = False
        fresh_rect = rect
        for plat in self._platforms:
            if _plat_pid(plat) == pid:
                fresh_rect = _plat_rect(plat)
                break

        # on the target window's surface?
        surface_y = float(fresh_rect.top()) - miku_h
        if abs(floor_y - surface_y) < 8.0:
            landed_on_target = True
        # or near a side edge (within 3 sprite widths horizontally)?
        elif (abs(x - float(fresh_rect.left())) < miku_w * 3
              or abs(x - float(fresh_rect.right())) < miku_w * 3):
            landed_on_target = True

        if landed_on_target:
            # snap to the correct corner for push/peek
            if action in ("push", "peek", "throw", "side_toss"):
                if corner == "left":
                    snap_x = float(fresh_rect.left()) - miku_w + 4
                else:
                    snap_x = float(fresh_rect.right()) - 4
                self._window.move(int(snap_x), int(floor_y))
            print(f"[claudemeji] pending: landed near target, firing {action}")
            if action == "push":
                self.start_window_push(fresh_rect, pid, corner)
            elif action == "peek":
                self.start_window_peek(fresh_rect, pid, corner)
            elif action == "throw":
                self.start_window_throw(fresh_rect, pid, corner)
            elif action == "side_toss":
                self.start_window_side_toss(fresh_rect, pid, corner)
            elif action == "carry":
                self.start_window_carry(fresh_rect, pid, corner)
        else:
            print(f"[claudemeji] pending: missed target window, clearing")

    # --- drag ---

    def on_drag_start(self, cursor_global: QPoint):
        self._dragged = True
        self._thrown = False
        self._drag_intensity = "calm"
        self._pull.reset()
        self._carry.reset()
        self._state = PhysicsState.DRAGGED
        self._vel = Vec2()
        self._drag_offset = cursor_global - self._window.pos()
        self._cursor_history.clear()
        self._set_posture(PostureState.DRAGGED)
        self._set_z_context(None)  # float when held by user
        self.locomotion_action.emit("drag")

    def on_drag_move(self, cursor_global: QPoint):
        if not self._dragged:
            return
        self._cursor_history.append(cursor_global)
        if len(self._cursor_history) > VELOCITY_HISTORY:
            self._cursor_history.pop(0)
        self._window.move(cursor_global - self._drag_offset)

        # track drag intensity: how far is she dangling from the grab point?
        pos = self._window.pos()
        sprite_cx = pos.x() + self._window.width() / 2
        cursor_x = cursor_global.x()
        offset = abs(cursor_x - sprite_cx)

        if offset < DRAG_CALM_DISTANCE:
            intensity = "calm"
        elif offset < DRAG_MILD_DISTANCE:
            intensity = "mild"
        else:
            intensity = "strong"

        if intensity != self._drag_intensity:
            self._drag_intensity = intensity
            self.drag_intensity_changed.emit(intensity)

        # update facing based on which side she's dangling from
        # if sprite center is left of cursor, she's dangling left
        dangle_dir = "left" if sprite_cx < cursor_x else "right"
        self._set_facing(dangle_dir)

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
            # zero out tiny movements, then amplify for satisfying throws
            vx = (vx * THROW_VELOCITY_SCALE) if abs(vx) > 2.0 else 0.0
            vy = (vy * THROW_VELOCITY_SCALE) if abs(vy) > 1.5 else 0.0
            self._vel = Vec2(vx, vy)
            speed = (vx ** 2 + vy ** 2) ** 0.5
            self._thrown = speed >= THROW_VELOCITY_THRESHOLD
        else:
            self._vel = Vec2()
            self._thrown = False

        self.locomotion_action.emit("fall")

    # --- jumping ---

    def jump_toward(self, target_x: float, target_y: float, desperate: bool = False):
        """Jump toward a target. desperate=True for bigger, wilder arcs (cursor lunges)."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.PUSHING_WINDOW):
            return
        pos = self._window.pos()
        x, y = float(pos.x()), float(pos.y())
        dx, dy = target_x - x, target_y - y
        hdist = abs(dx)
        vdist = abs(dy)
        dist = max(1.0, (dx * dx + dy * dy) ** 0.5)

        # scale impulse with distance — further = bigger jump
        dist_scale = min(1.5, max(0.6, dist / 300.0))
        # add randomness: ±25% variation so each jump feels different
        jitter = 0.75 + random.random() * 0.5  # 0.75–1.25

        if desperate:
            dist_scale *= 1.2
            jitter = 0.75 + random.random() * 0.5

        # horizontal: aim toward target
        dir_x = 1.0 if dx > 0 else -1.0 if dx < 0 else 0.0
        ix = dir_x * JUMP_IMPULSE_X * min(2.0, max(0.4, hdist / 150.0)) * jitter

        # vertical: depends on whether target is above or below
        if dy > 0:
            # target is BELOW — dive! hop outward and let gravity do the work
            # enough upward to clear window edges, strong horizontal to get out there
            iy = -8.0 * jitter
            ix *= 1.5  # extra horizontal so she clears the platform edge
        else:
            # target is above — jump UP
            vert_scale = dist_scale
            if desperate:
                vert_scale *= min(1.3, max(1.0, vdist / 400.0))
            iy = JUMP_IMPULSE_Y * vert_scale * jitter

        # clamp: good air but not orbital
        iy = max(-30.0, min(iy, -6.0 if desperate else -5.0))

        self._vel.x = ix
        self._vel.y = iy
        self._pull.reset()
        self._climb.hanging = False
        self._climb.window = None
        if dir_x != 0:
            self._set_facing("right" if dir_x > 0 else "left")
        self._state = PhysicsState.FALLING
        self._set_posture(PostureState.FALLING)
        self.locomotion_action.emit("jump")

    def jump_burst(self, direction: int = 1):
        self._vel.x = direction * JUMP_IMPULSE_X * 0.8
        self._vel.y = JUMP_IMPULSE_Y * 0.6
        self._set_facing("right" if direction > 0 else "left")
        self._state = PhysicsState.FALLING
        self._set_posture(PostureState.FALLING)

    # --- window interactions: push / peek / throw ---

    def start_window_push(self, window_rect: QRect, pid: int, corner: str):
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING):
            return
        self._push = PushState(
            window=(window_rect, pid),
            corner=corner,
            ticks=random.randint(*WINDOW_PUSH_DURATION),
            direction=1 if corner == "left" else -1,
            window_x=float(window_rect.x()),
            window_y=float(window_rect.y()),
        )
        self._set_facing("right" if corner == "left" else "left")
        self._state = PhysicsState.PUSHING_WINDOW
        self._set_posture(PostureState.PUSHING)
        self._pull.reset()
        self._set_z_context(self._find_platform_by_pid(pid))
        self.locomotion_action.emit("window_push")

    def start_window_peek(self, window_rect: QRect, pid: int, corner: str):
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING):
            return
        self._push.window = (window_rect, pid)  # reuse for position reference
        self._peek_ticks = random.randint(*WINDOW_PEEK_DURATION)
        self._set_facing("right" if corner == "left" else "left")
        self._state = PhysicsState.PEEKING
        self._set_posture(PostureState.PEEKING)
        self._set_z_context(self._find_platform_by_pid(pid))
        self.locomotion_action.emit("window_peek")

    def start_window_carry(self, window_rect: QRect, pid: int, corner: str = "left"):
        """Begin carry sequence: jump to window corner, grab, fall together, walk."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING,
                           PhysicsState.CARRYING_WINDOW):
            return
        pos = self._window.pos()
        mx = float(pos.x())
        my = float(pos.y())
        mh = float(self._window.height())

        # target: bottom corner of the window (she grabs from below)
        target_x = float(window_rect.left()) if corner == "left" else float(window_rect.right()) - self._window.width()
        target_y = float(window_rect.bottom()) - mh  # her feet at the window's bottom edge
        walk_dir = 1 if corner == "left" else -1  # carry away from the corner she grabbed

        # calculate jump velocity to reach the window bottom
        dy = target_y - my
        # jump impulse: enough to reach the window, with a bit of extra
        vy = -max(10.0, min(20.0, abs(dy) * 0.12 + 8.0))
        dx = target_x - mx
        vx = max(-10.0, min(10.0, dx * 0.08))

        self._carry = CarryState(
            window=(window_rect, pid),
            phase="jump",
            ticks=120,  # safety timeout for jump phase
            walk_dir=walk_dir,
            running=(self._restlessness >= 4 and random.random() < 0.4),
            window_x=float(window_rect.x()),
            window_y=float(window_rect.y()),
            grab_y=target_y,
            vel_y=vy,
        )
        self._vel = Vec2(vx, vy)
        self._pull.reset()
        self._state = PhysicsState.CARRYING_WINDOW
        self._set_posture(PostureState.FALLING)
        self._set_facing("left" if walk_dir < 0 else "right")
        self.locomotion_action.emit("jump")
        print(f"[claudemeji] carry: jump to window corner={corner} vy={vy:.1f} target_y={target_y:.0f}")

    def start_window_throw(self, window_rect: QRect, pid: int, corner: str):
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING):
            return
        self._set_facing("right" if corner == "left" else "left")
        throw_dir = "left" if corner == "left" else "right"
        self.locomotion_action.emit("window_throw")
        self.window_throw.emit(pid, window_rect, throw_dir)

    def start_window_side_toss(self, window_rect: QRect, pid: int, corner: str):
        """Grab the side of a window and toss it upward. No horizontal miku movement."""
        if self._state in (PhysicsState.DRAGGED, PhysicsState.FALLING,
                           PhysicsState.CARRYING_WINDOW):
            return
        self._set_facing("right" if corner == "left" else "left")
        self.locomotion_action.emit("window_throw")
        # toss up — sometimes minimize (rest 4), sometimes just bounce
        if self._restlessness >= 4 and random.random() < 0.4:
            self.window_throw.emit(pid, window_rect, "up")
        else:
            self.window_toss_up.emit(pid, window_rect)

    # --- main tick: dispatch to per-state handlers ---

    def _tick(self):
        if self._dragged:
            return

        screen = self._screen_rect()
        pos = self._window.pos()
        w, h = self._window.width(), self._window.height()
        # subtract the offsets that were applied in the LAST move() call
        x = float(pos.x()) - self._offset.x
        y = float(pos.y()) - self._offset.y - self._applied_offset_y

        screen_floor = float(screen.bottom() - h)
        ceil_y  = screen.top()
        left_x  = screen.left()
        right_x = screen.right() - w

        bounds = (screen_floor, ceil_y, left_x, right_x)

        if self._state == PhysicsState.FALLING:
            x, y = self._tick_falling(x, y, bounds)
        elif self._state == PhysicsState.GROUNDED:
            x, y = self._tick_grounded(x, y, bounds)
        elif self._state in (PhysicsState.WALL_LEFT, PhysicsState.WALL_RIGHT):
            x, y = self._tick_wall(x, y, bounds)
        elif self._state == PhysicsState.CEILING:
            x, y = self._tick_ceiling(x, y, bounds)
        elif self._state == PhysicsState.PUSHING_WINDOW:
            x, y = self._tick_pushing(x, y, bounds)
        elif self._state == PhysicsState.PEEKING:
            x, y = self._tick_peeking(x, y, bounds)
        elif self._state == PhysicsState.CARRYING_WINDOW:
            x, y = self._tick_carrying(x, y, bounds)

        # safety clamp
        x = max(float(left_x), min(x, float(right_x)))
        y = max(float(ceil_y), min(y, screen_floor))

        self._applied_offset_y = self._action_offset_y
        self._window.move(int(x + self._offset.x),
                          int(y + self._offset.y + self._action_offset_y))

    # --- per-state tick handlers ---

    def _tick_falling(self, x, y, bounds):
        screen_floor, ceil_y, left_x, right_x = bounds
        miku_w, miku_h = float(self._window.width()), float(self._window.height())

        self._vel.y = min(self._vel.y + GRAVITY, MAX_FALL_SPEED)
        old_y = y
        x += self._vel.x
        y += self._vel.y

        # keep facing consistent with horizontal movement during flight
        if abs(self._vel.x) > 0.5:
            self._set_facing("right" if self._vel.x > 0 else "left")

        # land on surface (ignore drop-through platform, skip occluded surfaces)
        target_floor = _find_surface_below(self._platforms, x, old_y, miku_w, miku_h,
                                           screen_floor, ignore_pid=self._drop_through_pid,
                                           only_visible=True)
        if y >= target_floor:
            y = target_floor
            self._vel = Vec2()
            self._drop_through_pid = 0  # clear on landing
            self._land(target_floor)
        elif y <= ceil_y:
            y = ceil_y
            if self._thrown and not self._event_locked:
                # thrown into ceiling — grab on!
                self._thrown = False
                self._climb.ceiling_dir = 1 if self._vel.x >= 0 else -1
                self._vel = Vec2()
                self._state = PhysicsState.CEILING
                self._climb.ticks = random.randint(*CEILING_DURATION)
                self._climb.hanging = False
                self._climb.window = None
                self._set_posture(PostureState.CEILING)
                self._set_z_context(None)  # ceiling = float
                self._set_facing("right" if self._climb.ceiling_dir > 0 else "left")
                self.locomotion_action.emit("ceiling")
            else:
                self._vel.y = abs(self._vel.y) * 0.3

        # hard clamp: never fall below screen
        if y > screen_floor:
            y = screen_floor
            self._vel = Vec2()
            self._drop_through_pid = 0
            self._land(screen_floor)

        # wall grab on screen edges — thrown sprites grab much more reliably
        grab_chance = THROWN_WALL_GRAB_CHANCE if self._thrown else self._wall_grab_chance()
        if x <= left_x:
            x = left_x
            if not self._event_locked and random.random() < grab_chance:
                self._thrown = False
                self._start_wall_climb(PhysicsState.WALL_LEFT)
            else:
                self._vel.x = abs(self._vel.x) * 0.5
        elif x >= right_x:
            x = right_x
            if not self._event_locked and random.random() < grab_chance:
                self._thrown = False
                self._start_wall_climb(PhysicsState.WALL_RIGHT)
            else:
                self._vel.x = -abs(self._vel.x) * 0.5

        return x, y

    def _tick_grounded(self, x, y, bounds):
        screen_floor, _, left_x, right_x = bounds
        miku_w, miku_h = float(self._window.width()), float(self._window.height())

        # friction
        self._vel.x *= 0.7
        if abs(self._vel.x) < 0.5:
            self._vel.x = 0.0

        if not self._event_locked:
            # active cursor chase: keep steering / lunging
            if self._following_cursor and self._chase.active:
                self._tick_cursor_chase()

            self._wander_ticks -= 1
            if self._wander_ticks <= 0:
                self._following_cursor = False
                self._chase.reset()
                self._decide_wander()

            if self._action_walk_speed > 0:
                speed = self._action_walk_speed
            elif self._sprinting:
                speed = SPRINT_SPEED
            elif self._running:
                speed = RUN_SPEED
            else:
                speed = WALK_SPEED
            x += self._walk_dir * speed

            # occlusion wall: when walking on ANY window surface, check if a
            # higher-z window would hide her. Applies even when currently floating
            # (she might walk from a visible area into an occluded one).
            if self._walk_dir != 0:
                standing = _find_platform_at(self._platforms, x, self._floor_y,
                                             miku_w, miku_h, screen_floor)
                if standing is not None:
                    occ_wall = _occlusion_wall_ahead(self._platforms, standing, x,
                                                     self._walk_dir, miku_w, miku_h)
                    if occ_wall is not None:
                        # dynamically lower z-context as she approaches the occluder
                        if not self._is_topmost_platform(standing):
                            self._set_z_context(standing)
                        if self._walk_dir > 0 and x >= occ_wall:
                            x = occ_wall
                            self._walk_dir = -self._walk_dir
                            self._set_facing("left")
                            self.locomotion_action.emit("walk")
                        elif self._walk_dir < 0 and x <= occ_wall:
                            x = occ_wall
                            self._walk_dir = -self._walk_dir
                            self._set_facing("right")
                            self.locomotion_action.emit("walk")
                    elif self._is_topmost_platform(standing):
                        # walked away from occluder — back to floating
                        self._set_z_context(None)

            # wall collisions
            x, climbed = self._handle_ground_walls(x, left_x, right_x)
            if climbed:
                self._following_cursor = False
                self._chase.reset()
                return x, y

        # sitting timer
        if self._walk_dir == 0 and not self._event_locked:
            self._still_ticks += 1
            if self._still_ticks >= SITTING_TIMEOUT and self._posture != PostureState.SITTING:
                self._set_posture(PostureState.SITTING)
        else:
            self._still_ticks = 0

        # window pull (sprite weight) — skip when event-locked so she stays put while working
        if not self._event_locked:
            y = self._tick_window_pull(y)

        # unstick: if on a window (not screen floor) and restless, sometimes jump off
        on_window = abs(self._floor_y - screen_floor) > SURFACE_TOLERANCE
        standing_platform = (_find_platform_at(self._platforms, x, self._floor_y,
                                               miku_w, miku_h, screen_floor)
                             if on_window else None)
        if (on_window and not self._event_locked and self._restlessness >= 1
                and self._walk_dir == 0 and self._still_ticks > 60):
            # chance per tick to get bored and jump off (scales with restlessness)
            bail_chance = {1: 0.003, 2: 0.008, 3: 0.015, 4: 0.025}.get(self._restlessness, 0)
            if random.random() < bail_chance:
                self._pull.reset()
                # mark this platform as drop-through so she falls past it
                if standing_platform:
                    self._drop_through_pid = standing_platform[1]
                # if chasing cursor and it's below, dive toward it
                if self._following_cursor and self._chase.active:
                    try:
                        cursor = QCursor.pos()
                        cx, cy = float(cursor.x()), float(cursor.y())
                        if cy > y:  # cursor is below
                            print(f"[claudemeji] DIVE off window toward cursor!")
                            self.jump_toward(cx, cy, desperate=True)
                            return x, y
                    except Exception:
                        pass
                # otherwise just hop off in a random direction
                direction = random.choice([-1, 1])
                self._vel.x = direction * JUMP_IMPULSE_X * 0.5
                self._vel.y = JUMP_IMPULSE_Y * 0.2  # tiny hop
                self._set_facing("right" if direction > 0 else "left")
                self._state = PhysicsState.FALLING
                self._set_posture(PostureState.FALLING)
                self.locomotion_action.emit("jump")
                print(f"[claudemeji] bored on window, hopping off")
                return x, y

        # edge detection: walked off surface? (skip when event-locked — she's working, don't knock her off)
        if (not self._event_locked
                and not _surface_at(self._platforms, x, self._floor_y, miku_w, miku_h, screen_floor)):
            self._pull.reset()
            if self._following_cursor:
                # chasing cursor off an edge — leap toward it!
                try:
                    cursor = QCursor.pos()
                    cx, cy = float(cursor.x()), float(cursor.y())
                    self.jump_toward(cx, cy, desperate=True)
                    print("[claudemeji] cursor chase: EDGE LEAP toward cursor")
                except Exception:
                    self._start_falling()
            elif (self._walk_dir != 0
                    and not self._event_locked
                    and random.random() < EDGE_LEAP_CHANCE):
                # deliberate edge leap: sometimes jump off instead of just falling
                self._vel.x = self._walk_dir * EDGE_LEAP_IMPULSE_X
                self._vel.y = EDGE_LEAP_IMPULSE_Y
                self._set_facing("left" if self._walk_dir < 0 else "right")
                self._state = PhysicsState.FALLING
                self._set_posture(PostureState.FALLING)
                self.locomotion_action.emit("jump")
            else:
                self._start_falling()

        return x, y

    def _handle_ground_walls(self, x, left_x, right_x) -> tuple[float, bool]:
        """Handle screen-edge collisions while grounded. Returns (x, climbed)."""
        grab_chance = self._wall_grab_chance() * 0.3
        if x <= left_x:
            x = left_x
            if random.random() < grab_chance:
                self._pull.reset()
                self._start_wall_climb(PhysicsState.WALL_LEFT)
                return x, True
            self._walk_dir = 1
            self._set_facing("right")
            self.locomotion_action.emit("walk")
        elif x >= right_x:
            x = right_x
            if random.random() < grab_chance:
                self._pull.reset()
                self._start_wall_climb(PhysicsState.WALL_RIGHT)
                return x, True
            self._walk_dir = -1
            self._set_facing("left")
            self.locomotion_action.emit("walk")
        return x, False

    def _tick_window_pull(self, y) -> float:
        """Apply sprite weight pulling a window down. Returns updated y."""
        pull = self._pull
        if (pull.standing_on is not None
                and WINDOW_PULL_DISTANCE > 0
                and pull.applied < WINDOW_PULL_DISTANCE):
            pull.tick_counter += 1
            if pull.tick_counter >= WINDOW_PULL_INTERVAL:
                pull.tick_counter = 0
                delta = min(WINDOW_PULL_SPEED * WINDOW_PULL_INTERVAL,
                            WINDOW_PULL_DISTANCE - pull.applied)
                if delta > 0:
                    rect, pid = _plat_rect(pull.standing_on), _plat_pid(pull.standing_on)
                    pull.applied += delta
                    self._floor_y += delta
                    y += delta
                    self.pull_window.emit(pid, rect, pull.applied)
        return y

    def _tick_wall(self, x, y, bounds):
        screen_floor, ceil_y, left_x, right_x = bounds
        miku_w, miku_h = float(self._window.width()), float(self._window.height())
        x = self._climb.pin_x
        on_screen_edge = (abs(x - left_x) < 2.0 or abs(x - right_x) < 2.0)

        if self._climb.hanging:
            self._climb.ticks -= 1
            if self._climb.ticks <= 0:
                self._climb.hanging = False
                self._climb.window = None
                self._start_falling()
            return x, y

        # actively climbing upward
        y -= CLIMB_SPEED

        # climbing behind another window? allow peeking out ~40% but stop if too deep
        if self._climb.window is not None and not on_screen_edge:
            cw_rect, cw_pid = self._climb.window[0], self._climb.window[1]
            side = "left" if self._state == PhysicsState.WALL_LEFT else "right"
            overlap = self._climb_occlusion_overlap(cw_rect, cw_pid, side, y, miku_h)
            if overlap > miku_h * 0.6:
                # too deep behind the occluder — stop climbing, hang or fall
                self._maybe_hang_or_fall("hang")
                return x, y

        # side-climbing a window: pull it down
        if self._climb.window is not None:
            self._climb.side_pull_counter += 1
            if self._climb.side_pull_counter >= SIDE_PULL_INTERVAL:
                self._climb.side_pull_counter = 0
                self._climb.side_pull_cumulative += SIDE_PULL_SPEED * SIDE_PULL_INTERVAL
                cw_rect, cw_pid = self._climb.window[0], self._climb.window[1]
                self.pull_window.emit(cw_pid, cw_rect, self._climb.side_pull_cumulative)

        # reached top of screen wall → transition to ceiling
        if on_screen_edge and y <= ceil_y:
            y = float(ceil_y)
            self._climb.ceiling_dir = 1 if self._state == PhysicsState.WALL_LEFT else -1
            self._state = PhysicsState.CEILING
            self._climb.ticks = random.randint(*CEILING_DURATION)
            self._climb.hanging = False
            self._climb.window = None
            self._set_posture(PostureState.CEILING)
            self._set_z_context(None)  # ceiling = screen edge, float above everything
            self._set_facing("right" if self._climb.ceiling_dir > 0 else "left")
            self.locomotion_action.emit("ceiling")
            return x, y

        self._climb.ticks -= 1

        # reached top of a window we're climbing
        top_of_surface = _find_surface_below(self._platforms, x, y - 1, miku_w, miku_h, screen_floor)
        if not on_screen_edge and top_of_surface < y:
            # jump inward so she lands on the surface (not teetering on edge)
            self._climb.window = None
            self._climb.hanging = False
            inward = 20.0
            if self._state == PhysicsState.WALL_LEFT:
                x += inward
            else:
                x -= inward
            y = top_of_surface
            self._land(top_of_surface)
        elif self._climb.ticks <= 0:
            self._maybe_hang_or_fall("hang")

        return x, y

    def _tick_ceiling(self, x, y, bounds):
        _, ceil_y, left_x, right_x = bounds
        y = float(ceil_y)

        if self._climb.hanging:
            self._climb.ticks -= 1
            if self._climb.ticks <= 0:
                self._climb.hanging = False
                self._start_falling()
            return x, y

        # crawl along ceiling
        prev_dir = self._climb.ceiling_dir
        x += self._climb.ceiling_dir * CEILING_CRAWL_SPEED
        if x <= left_x:
            x = float(left_x)
            self._climb.ceiling_dir = 1
        elif x >= right_x:
            x = float(right_x)
            self._climb.ceiling_dir = -1
        if self._climb.ceiling_dir != prev_dir:
            self._set_facing("right" if self._climb.ceiling_dir > 0 else "left")
            self.locomotion_action.emit("ceiling")

        self._climb.ticks -= 1
        if self._climb.ticks <= 0:
            self._maybe_hang_or_fall("hang_ceiling")

        return x, y

    def _tick_pushing(self, x, y, bounds):
        _, _, left_x, right_x = bounds
        push = self._push

        step = push.direction * WINDOW_PUSH_SPEED
        x += step
        push.window_x += step  # window tracks sprite movement 1:1
        push.ticks -= 1

        if push.window:
            pw_pid = push.window[1]
            self.window_move_to.emit(pw_pid, push.window_x, push.window_y)

        if push.ticks <= 0 or x <= left_x or x >= right_x:
            x = max(float(left_x), min(x, float(right_x)))
            self._return_to_ground()

        return x, y

    def _tick_peeking(self, x, y, _bounds):
        self._peek_ticks -= 1
        if self._peek_ticks <= 0:
            self._return_to_ground()
        return x, y

    def _tick_carrying(self, x, y, bounds):
        screen_floor, _, left_x, right_x = bounds
        carry = self._carry
        if carry.window is None:
            self._carry.reset()
            self._return_to_ground()
            return x, y

        w_rect, w_pid = carry.window[0], carry.window[1]
        carry.ticks -= 1

        if carry.phase == "jump":
            # flying toward window corner — normal projectile physics
            self._vel.y = min(self._vel.y + GRAVITY, MAX_FALL_SPEED)
            x += self._vel.x
            y += self._vel.y

            # update facing during flight
            if abs(self._vel.x) > 0.5:
                self._set_facing("right" if self._vel.x > 0 else "left")

            # reached window height (or close enough) → grab!
            close_enough = abs(y - carry.grab_y) < float(self._window.height()) * 0.5
            if y <= carry.grab_y + 10 or (self._vel.y >= 0 and close_enough):
                # refresh window position — it may have moved since we started jumping
                w_rect_now = carry.window[0]
                fresh = self._find_platform_by_pid(carry.window[1])
                if fresh is not None:
                    fresh_rect = _plat_rect(fresh)
                    carry.window_x = float(fresh_rect.x())
                    carry.window_y = float(fresh_rect.y())
                    w_rect_now = fresh_rect
                mw = float(self._window.width())
                # snap to window corner — both x and y
                if carry.walk_dir > 0:
                    # grabbed left corner, snap to left edge
                    x = carry.window_x
                else:
                    # grabbed right corner, snap to right edge
                    x = carry.window_x + float(w_rect_now.width()) - mw
                y = float(w_rect_now.bottom()) - float(self._window.height())
                # lock offset: window position = sprite position + offset (forever in sync)
                carry.offset_x = carry.window_x - x
                carry.offset_y = carry.window_y - y
                carry.phase = "grab_fall"
                carry.vel_y = max(0, self._vel.y)  # keep any downward momentum
                self._vel = Vec2()
                self._set_posture(PostureState.CARRYING)
                self._set_facing("left" if carry.walk_dir < 0 else "right")
                self.locomotion_action.emit("window_carry_perch")
                print(f"[claudemeji] carry: GRABBED window at ({x:.0f}, {y:.0f})")

            # abort: timed out, hit ground, or apex too far from target
            missed_apex = self._vel.y >= 0 and not close_enough
            if carry.ticks <= 0 or y >= screen_floor or missed_apex:
                if missed_apex:
                    print(f"[claudemeji] carry: jump too short (y={y:.0f}, target={carry.grab_y:.0f}), aborting")
                else:
                    print("[claudemeji] carry: jump missed window, aborting")
                self._carry.reset()
                self._vel.y = max(0, self._vel.y)  # keep falling naturally
                self._state = PhysicsState.FALLING
                self._set_posture(PostureState.FALLING)
                self.locomotion_action.emit("fall")
                return x, y

        elif carry.phase == "grab_fall":
            # falling together — sprite and window drop as one
            carry.vel_y = min(carry.vel_y + GRAVITY, MAX_FALL_SPEED)
            y += carry.vel_y
            # window derived from sprite position + locked offset
            carry.window_x = x + carry.offset_x
            carry.window_y = y + carry.offset_y
            self.window_move_to.emit(w_pid, carry.window_x, carry.window_y)

            # landed — lift window to hold height, then start carrying!
            if y >= screen_floor:
                y = screen_floor
                # adjust offset so window is held ~2/3 up her body
                mh = float(self._window.height())
                carry.offset_y -= mh * 0.6
                carry.window_x = x + carry.offset_x
                carry.window_y = y + carry.offset_y
                self.window_move_to.emit(w_pid, carry.window_x, carry.window_y)
                carry.vel_y = 0
                carry.phase = "carry"
                carry.ticks = random.randint(*CARRY_WALK_DURATION)
                action = "window_carry_run" if carry.running else "window_carry"
                self.locomotion_action.emit(action)
                print(f"[claudemeji] carry: landed! walking with window "
                      f"({'run' if carry.running else 'walk'}, {carry.ticks} ticks)")

        elif carry.phase == "carry":
            # walking/running with the window following
            speed = CARRY_RUN_SPEED if carry.running else CARRY_WALK_SPEED
            step = carry.walk_dir * speed
            x += step
            # window derived from sprite position — always perfectly in sync
            carry.window_x = x + carry.offset_x
            self.window_move_to.emit(w_pid, carry.window_x, carry.window_y)

            # per-tick abort chance (she might just... drop it)
            if random.random() < CARRY_ABORT_PER_TICK:
                print("[claudemeji] carry: abort mid-carry (oops, dropped it)")
                self._carry.reset()
                self._return_to_ground()
                return x, y

            # hit screen edge or ticks expired → throw or drop
            at_edge = x <= left_x or x >= right_x
            if carry.ticks <= 0 or at_edge:
                x = max(float(left_x), min(x, float(right_x)))
                throw_chance = CARRY_THROW_CHANCE.get(self._restlessness, 0)
                if random.random() < throw_chance:
                    carry.phase = "throw_windup"
                    carry.ticks = 20
                    self.locomotion_action.emit("window_carry_throw")
                    print("[claudemeji] carry: winding up to THROW!")
                else:
                    print("[claudemeji] carry: set window down gently")
                    self._carry.reset()
                    self._return_to_ground()
                    return x, y

        elif carry.phase == "throw_windup":
            carry.window_x = x + carry.offset_x
            self.window_move_to.emit(w_pid, carry.window_x, carry.window_y)
            if carry.ticks <= 0:
                throw_dir = "left" if carry.walk_dir < 0 else "right"
                current_rect = QRect(int(carry.window_x), int(carry.window_y),
                                     w_rect.width(), w_rect.height())
                print(f"[claudemeji] carry: THROW window! (dir={throw_dir})")
                self.window_throw.emit(w_pid, current_rect, throw_dir)
                self.locomotion_action.emit("window_carry_cheer")
                self._carry.reset()
                self._state = PhysicsState.GROUNDED
                self._set_posture(PostureState.STANDING)
                self._schedule_wander()
                return x, y

        return x, y

    # --- wander decisions ---

    def _schedule_wander(self):
        mul = _restless_params(self._restlessness)[0]
        lo = int(IDLE_WANDER_INTERVAL[0] * mul)
        hi = int(IDLE_WANDER_INTERVAL[1] * mul)
        self._wander_ticks = random.randint(max(1, lo), max(2, hi))

    def _decide_wander(self):
        rest = self._restlessness

        # at higher restlessness, try special behaviors first
        if rest >= 2 and not self._event_locked:
            if self._try_special_behavior(rest):
                self._schedule_wander()
                return
            # log failures occasionally (not every tick)
            if not self._platforms and random.random() < 0.05:
                print(f"[claudemeji] wander: no platforms detected (window interactions unavailable)")

        # trip check: if currently running/sprinting, small chance to stumble
        if (self._running or self._sprinting) and rest >= 2:
            trip_chance = TRIP_CHANCE.get(rest, 0)
            if random.random() < trip_chance:
                self._walk_dir = 0
                self._running = False
                self._sprinting = False
                self._set_posture(PostureState.STANDING)
                self.locomotion_action.emit("trip")
                self._schedule_wander()
                return

        # movement options with weights
        calm = rest <= 1
        direction = random.choice([-1, 1])
        options = [
            ("walk",   0.35 if calm else 0.20),
            ("stand",  0.20 if calm else 0.10),
            ("idle",   0.10 if calm else 0.05),
            ("crawl",  0.05 if calm else 0.10),  # deliberate belly crawl
        ]
        if rest >= 2:
            options.append(("run", 0.25))
        if rest >= 3:
            options.append(("sprint", 0.15))

        choice = _weighted_choice(options)

        if choice == "walk":
            self._start_walking(direction)
        elif choice == "run":
            self._start_walking(direction, run=True)
        elif choice == "sprint":
            self._start_walking(direction, sprint=True)
        elif choice == "crawl":
            # deliberate crawl: physics moves her via action_walk_speed feedback
            self._walk_dir = direction
            self._still_ticks = 0
            self._running = False
            self._sprinting = False
            self._following_cursor = False
            self._set_posture(PostureState.WALKING)
            self._set_facing("left" if direction < 0 else "right")
            self.locomotion_action.emit("crawl")
        elif choice == "idle":
            self._walk_dir = 0
            self._running = False
            self._sprinting = False
            self._following_cursor = False
            self._set_posture(PostureState.STANDING)
            self.locomotion_action.emit("idle")
        else:
            # stand: stop, hold frame, wander timer picks next
            self._walk_dir = 0
            self._running = False
            self._sprinting = False
            self._following_cursor = False
            self._set_posture(PostureState.STANDING)
            self.locomotion_action.emit("stand")

        self._schedule_wander()

    def _try_special_behavior(self, rest: int) -> bool:
        """Try cursor-follow, nearby window, or window-seek. Falls through on failure."""
        # build a shuffled list of behaviors to try (weighted by restlessness)
        behaviors: list[tuple[str, float]] = []
        cursor_chance = CURSOR_FOLLOW_CHANCE.get(rest, 0)
        if cursor_chance > 0:
            behaviors.append(("cursor", cursor_chance))
        if rest >= 2:
            behaviors.append(("window_near", 0.15))
        if rest >= 3:
            behaviors.append(("window_carry", 0.20))
        window_seek = WINDOW_SEEK_CHANCE.get(rest, 0)
        if window_seek > 0:
            behaviors.append(("window_seek", window_seek))

        if not behaviors:
            return False

        # pick one weighted, but if it fails, try the others
        order = []
        remaining = list(behaviors)
        while remaining:
            pick = _weighted_choice(remaining)
            order.append(pick)
            remaining = [(n, w) for n, w in remaining if n != pick]

        for behavior in order:
            if behavior == "cursor" and self._try_cursor_follow():
                print(f"[claudemeji] special: cursor follow (rest={rest})")
                return True
            elif behavior == "window_near":
                nearby = self._nearby_window()
                if nearby:
                    corner, w_rect, w_pid = nearby
                    print(f"[claudemeji] special: window interact near corner={corner} (rest={rest})")
                    self._do_window_interaction(w_rect, w_pid, corner)
                    return True
            elif behavior == "window_carry":
                target = self._pick_random_window()
                if target:
                    corner, w_rect, w_pid = target
                    # only carry windows whose bottom is above miku's feet
                    # (she needs to reach under to grab it)
                    miku_feet = float(self._window.pos().y()) + float(self._window.height())
                    if float(w_rect.bottom()) < miku_feet - 10:
                        print(f"[claudemeji] special: CARRY window (pid={w_pid}, corner={corner}, rest={rest})")
                        self.start_window_carry(w_rect, w_pid, corner)
                        return True
                    # window too low to carry — side toss instead!
                    elif rest >= 3:
                        print(f"[claudemeji] special: SIDE TOSS window (pid={w_pid}, corner={corner}, rest={rest})")
                        self.start_window_side_toss(w_rect, w_pid, corner)
                        return True
            elif behavior == "window_seek":
                target = self._pick_random_window()
                if target:
                    corner, w_rect, w_pid = target
                    target_x = float(w_rect.left() if corner == "left" else w_rect.right())
                    target_y = float(w_rect.top()) - self._window.height()
                    print(f"[claudemeji] special: jump to window corner={corner} (rest={rest})")
                    self.jump_toward(target_x, target_y)
                    return True

        return False

    def _do_window_interaction(self, w_rect, w_pid, corner):
        rest = self._restlessness
        roll = random.random()
        if rest >= 4 and roll < 0.25:
            print(f"[claudemeji] window interaction: SIDE TOSS (pid={w_pid}, corner={corner})")
            self.start_window_side_toss(w_rect, w_pid, corner)
        elif rest >= 3 and roll < 0.50:
            print(f"[claudemeji] window interaction: PUSH (pid={w_pid}, corner={corner})")
            self.start_window_push(w_rect, w_pid, corner)
        else:
            # rest 2: peek only. rest 3+: peek or push
            print(f"[claudemeji] window interaction: PEEK (pid={w_pid}, corner={corner})")
            self.start_window_peek(w_rect, w_pid, corner)

    def _try_cursor_follow(self) -> bool:
        """Start a cursor chase: sprint toward cursor, then lunge at it a few times."""
        rest = self._restlessness
        try:
            cursor = QCursor.pos()
        except Exception:
            return False
        pos = self._window.pos()
        mx = float(pos.x()) + self._window.width() / 2
        my = float(pos.y()) + self._window.height() / 2
        cx, cy = float(cursor.x()), float(cursor.y())
        dx = cx - mx
        dist = ((cx - mx) ** 2 + (cy - my) ** 2) ** 0.5

        if dist < 30:
            return False

        # set up the chase state
        budget_range = CURSOR_LUNGE_BUDGET.get(rest, (1, 2))
        self._chase = CursorChaseState(
            active=True,
            lunges_left=random.randint(*budget_range),
            cooldown=0,
            phase="approach",
        )
        self._following_cursor = True

        # start sprinting toward cursor
        sprint = rest >= 3
        direction = 1 if dx > 0 else -1
        self._start_walking(direction, run=True, sprint=sprint)

        # commit to the chase
        chase_range = CURSOR_CHASE_TICKS.get(rest, (90, 180))
        self._wander_ticks = random.randint(*chase_range)

        print(f"[claudemeji] cursor chase: BEGIN ({'sprint' if sprint else 'run'}, "
              f"dist={dist:.0f}, {self._chase.lunges_left} lunges, rest={rest})")
        return True

    # --- surface/window queries ---

    def _window_wall_at(self, x: float, walk_dir: int):
        """Check if walking into the side of a window. Returns (wall_side, QRect, pid) or None.
        Only matches windows that actually extend down to miku's level and aren't
        occluded by another window in front. Used for deliberate climb targeting,
        not for walk collision."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        miku_y = float(self._window.pos().y())
        miku_bottom = miku_y + miku_h
        tolerance = 4.0

        for plat in self._platforms:
            rect, pid = _plat_rect(plat), _plat_pid(plat)
            # window must overlap vertically with miku (not floating above or below)
            if rect.top() >= miku_bottom or rect.bottom() <= miku_y:
                continue
            if walk_dir > 0:
                wall_x = float(rect.left()) - miku_w
                if abs(x - wall_x) < tolerance:
                    if not self._is_occluded_side(rect, pid, "left", miku_y, miku_bottom):
                        return ("right", rect, pid)
            elif walk_dir < 0:
                wall_x = float(rect.right())
                if abs(x - wall_x) < tolerance:
                    if not self._is_occluded_side(rect, pid, "right", miku_y, miku_bottom):
                        return ("left", rect, pid)
        return None

    def _is_occluded_side(self, target_rect: QRect, target_pid: int,
                          side: str, y_top: float, y_bottom: float) -> bool:
        """Check if another window covers the target window's side at miku's height.
        Platforms are front-to-back ordered, so any earlier entry that covers the
        target's side means it's occluded."""
        edge_x = float(target_rect.left()) if side == "left" else float(target_rect.right())
        for plat in self._platforms:
            rect, pid = _plat_rect(plat), _plat_pid(plat)
            if pid == target_pid:
                break  # reached the target itself — nothing in front occludes it
            # does this window cover the target's edge at miku's height?
            if (rect.left() <= edge_x <= rect.right()
                    and rect.top() < y_bottom and rect.bottom() > y_top):
                return True
        return False

    def _tick_cursor_chase(self):
        """Cursor chase tick: approach → lunge → recover → lunge → give up."""
        chase = self._chase
        if not chase.active:
            self._following_cursor = False
            return

        try:
            cursor = QCursor.pos()
        except Exception:
            self._end_chase()
            return

        pos = self._window.pos()
        mx = float(pos.x()) + self._window.width() / 2
        my = float(pos.y()) + self._window.height() / 2
        cx, cy = float(cursor.x()), float(cursor.y())
        dx = cx - mx
        hdist = abs(dx)  # horizontal distance — this is what matters for lunging

        # out of lunges → done
        if chase.lunges_left <= 0:
            print(f"[claudemeji] cursor chase: out of lunges, giving up")
            self._end_chase()
            return

        # cooldown between lunges (recovering after landing)
        if chase.cooldown > 0:
            chase.cooldown -= 1
            return

        if chase.phase == "approach":
            # cursor is below us? DIVE — drop through this platform toward it
            if cy > my + 50 and hdist < CURSOR_LUNGE_RANGE_X:
                chase.phase = "lunging"
                chase.lunges_left -= 1
                self._walk_dir = 0
                self._set_facing("left" if dx < 0 else "right")
                # find what we're standing on and ignore it during the fall
                miku_w = float(self._window.width())
                miku_h = float(self._window.height())
                screen_floor = float(self._screen_rect().bottom() - miku_h)
                plat = _find_platform_at(self._platforms, float(self._window.pos().x()),
                                         self._floor_y, miku_w, miku_h, screen_floor)
                if plat:
                    self._drop_through_pid = plat[1]
                print(f"[claudemeji] cursor chase: DIVE DOWN! (hdist={hdist:.0f}, "
                      f"below by {cy - my:.0f}px, {chase.lunges_left} left)")
                self.jump_toward(cx, cy, desperate=True)
                chase.cooldown = CURSOR_LUNGE_COOLDOWN
                return

            if hdist > CURSOR_LUNGE_RANGE_X:
                # far away: steer toward cursor (with dead zone to prevent flip-flop)
                if hdist > 80:
                    desired_dir = 1 if dx > 0 else -1
                    if desired_dir != self._walk_dir:
                        self._walk_dir = desired_dir
                        self._set_facing("left" if desired_dir < 0 else "right")
            else:
                # within lunge range — stop, face cursor, LUNGE!
                chase.phase = "lunging"
                chase.lunges_left -= 1
                self._walk_dir = 0
                self._set_facing("left" if dx < 0 else "right")
                print(f"[claudemeji] cursor chase: LUNGE! (hdist={hdist:.0f}, "
                      f"{chase.lunges_left} left)")
                self.jump_toward(cx, cy, desperate=True)
                chase.cooldown = CURSOR_LUNGE_COOLDOWN
                return

        elif chase.phase == "lunging":
            # in the air or just landed — wait for grounded
            if self._state == PhysicsState.GROUNDED:
                if chase.lunges_left > 0:
                    chase.phase = "approach"
                    # reposition: run to offset from cursor, not directly under
                    # pick a side to approach from (biased toward current facing)
                    offset = random.choice([-1, 1]) * random.randint(100, 300)
                    reposition_dir = 1 if (cx + offset) > mx else -1
                    sprint = self._restlessness >= 3
                    self._start_walking(reposition_dir, run=True, sprint=sprint)
                    chase.cooldown = CURSOR_LUNGE_COOLDOWN
                    print(f"[claudemeji] cursor chase: landed, repositioning "
                          f"({chase.lunges_left} lunges left)")
                else:
                    print(f"[claudemeji] cursor chase: last lunge done, giving up")
                    self._end_chase()

    def _end_chase(self):
        """End the cursor chase cleanly."""
        self._chase.reset()
        self._following_cursor = False
        # she failed to catch it — brief pause before doing something else
        self._walk_dir = 0
        self._set_posture(PostureState.STANDING)
        self.locomotion_action.emit("stand")
        self._schedule_wander()

    def _platform_standing_on(self):
        """Return (QRect, pid) if miku is standing on a window, else None."""
        if self._state != PhysicsState.GROUNDED:
            return None
        screen = self._screen_rect()
        screen_floor = float(screen.bottom() - self._window.height())
        if abs(self._floor_y - screen_floor) < SURFACE_TOLERANCE:
            return None  # on the screen floor, not a window
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        x = float(self._window.pos().x())
        return _find_platform_at(self._platforms, x, self._floor_y, miku_w, miku_h, screen_floor)

    def _nearby_window(self, max_dist: float = 200.0):
        """Find a window near miku for interaction. Returns (corner, QRect, pid) or None.
        Checks proximity to any edge of the window, not just corners."""
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        pos = self._window.pos()
        mx, my = float(pos.x()), float(pos.y())
        miku_cx = mx + miku_w / 2
        miku_cy = my + miku_h / 2

        best = None
        best_dist = max_dist

        for plat in self._platforms:
            rect, pid = _plat_rect(plat), _plat_pid(plat)
            # closest point on window rect to miku center
            cx = max(float(rect.left()), min(miku_cx, float(rect.right())))
            cy = max(float(rect.top()), min(miku_cy, float(rect.bottom())))
            dist = ((miku_cx - cx) ** 2 + (miku_cy - cy) ** 2) ** 0.5
            if dist < best_dist:
                # pick corner: which side is miku on?
                corner = "left" if miku_cx < float(rect.left() + rect.right()) / 2 else "right"
                best = (corner, rect, pid)
                best_dist = dist

        return best

    def _pick_random_window(self):
        """Pick a random window to walk/jump toward. Returns (corner, QRect, pid) or None."""
        if not self._platforms:
            return None
        mx = float(self._window.pos().x())

        # any on-screen window is fair game
        candidates = []
        for plat in self._platforms:
            rect, pid = _plat_rect(plat), _plat_pid(plat)
            left_dist = abs(mx - rect.left())
            right_dist = abs(mx - rect.right())
            corner = "left" if left_dist < right_dist else "right"
            candidates.append((corner, rect, pid))

        return random.choice(candidates) if candidates else None

    # --- z-ordering ---

    def _set_z_context(self, platform_tuple=None):
        """Update z-ordering context. Pass a platform tuple to layer with that window,
        or None to float above everything."""
        if platform_tuple is None:
            wnum, zidx = 0, -1
        else:
            wnum = _plat_winnum(platform_tuple)
            zidx = _plat_zidx(platform_tuple)
        if wnum != self._z_window_number or zidx != self._z_index:
            self._z_window_number = wnum
            self._z_index = zidx
            self.z_context_changed.emit(wnum, zidx)

    def _is_topmost_platform(self, platform_tuple) -> bool:
        """Is this the frontmost window at miku's current x position?
        Checks if any window in front overlaps miku's standing area."""
        if platform_tuple is None:
            return True
        target_zidx = _plat_zidx(platform_tuple)
        target_rect = _plat_rect(platform_tuple)
        miku_w = float(self._window.width())
        miku_h = float(self._window.height())
        x = float(self._window.pos().x())
        miku_top = float(target_rect.top()) - miku_h  # where miku's head is
        miku_bottom = float(target_rect.top())         # where miku's feet are
        for plat in self._platforms:
            if _plat_zidx(plat) >= target_zidx:
                return True  # nothing in front covers this spot
            rect = _plat_rect(plat)
            # does this window cover miku's body at her standing position?
            if (rect.left() < x + miku_w and rect.right() > x
                    and rect.top() < miku_bottom and rect.bottom() > miku_top):
                return False
        return True

    def _find_platform_by_pid(self, pid: int):
        """Find the platform tuple for a given pid. Returns tuple or None."""
        for plat in self._platforms:
            if _plat_pid(plat) == pid:
                return plat
        return None

    def _climb_occlusion_overlap(self, target_rect: QRect, target_pid: int,
                                  side: str, y: float, miku_h: float) -> float:
        """How many pixels of miku's height are hidden behind higher-z windows
        while climbing the target window's side? Returns 0 if fully visible."""
        miku_top = y
        miku_bottom = y + miku_h
        edge_x = float(target_rect.left()) if side == "left" else float(target_rect.right())
        total_overlap = 0.0
        for plat in self._platforms:
            pid = _plat_pid(plat)
            if pid == target_pid:
                break  # reached target — only check windows in front
            rect = _plat_rect(plat)
            if rect.left() <= edge_x <= rect.right():
                # how much of miku does this window cover?
                cover_top = max(miku_top, float(rect.top()))
                cover_bottom = min(miku_bottom, float(rect.bottom()))
                if cover_bottom > cover_top:
                    total_overlap = max(total_overlap, cover_bottom - cover_top)
        return total_overlap

    # --- helpers ---

    def _set_posture(self, posture: PostureState):
        if posture != self._posture:
            self._posture = posture
            self.posture_changed.emit(posture.value)

    def _set_facing(self, direction: str):
        if direction != self._facing:
            self._facing = direction
            self.facing_changed.emit(direction)

    def _screen_rect(self) -> QRect:
        pos = self._window.pos()
        center = QPoint(
            pos.x() + self._window.width() // 2,
            pos.y() + self._window.height() // 2,
        )
        screen = QApplication.screenAt(center)
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
