"""
creature.py - the invisible creature's state model

The creature has continuous state (posture, speed, what she's doing) that drives
looping animations, and discrete events (landed, tripped) that drive one-shots.
Physics builds CreatureState snapshots; the animation resolver maps them to action names.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto


class Posture(Enum):
    """What the creature is physically doing. Maps to looping animations."""
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


class SpeedTier(Enum):
    """Movement speed. Determines walk/run/sprint animation selection."""
    STILL  = 0
    CRAWL  = 1
    WALK   = 2
    RUN    = 3
    SPRINT = 4


class CarryPhase(Enum):
    """Sub-states of the window carry sequence."""
    NONE         = auto()
    JUMP         = auto()   # launching toward window
    GRAB_FALL    = auto()   # grabbed corner, falling together
    PERCH        = auto()   # sitting on corner before walking
    CARRY        = auto()   # walking with window
    THROW_WINDUP = auto()   # winding up to throw


class ClimbSurface(Enum):
    """What the creature is climbing on / hanging from."""
    NONE         = auto()
    SCREEN_LEFT  = auto()
    SCREEN_RIGHT = auto()
    WINDOW_LEFT  = auto()
    WINDOW_RIGHT = auto()
    CEILING      = auto()


class CreatureEvent(Enum):
    """Discrete events that play one-shot animations.
    Emitted once at the moment something happens; animation locks until it finishes."""
    LANDED        = auto()   # fall → ground (bounce)
    TRIPPED       = auto()   # stumbled during movement
    THREW_WINDOW  = auto()   # standing throw gesture
    CARRY_CHEERED = auto()   # celebration after carry-throw


@dataclass(frozen=True)
class CreatureState:
    """Immutable snapshot of the creature's continuous physical state.
    Built by physics each tick, emitted only on change.
    The animation resolver reads this to pick looping animations."""
    posture: Posture = Posture.STANDING
    facing: str = "left"
    speed_tier: SpeedTier = SpeedTier.STILL
    carry_phase: CarryPhase = CarryPhase.NONE
    climb_surface: ClimbSurface = ClimbSurface.NONE
    launched: bool = False       # True while falling upward after a jump (show jump pose, not fall)
    is_event_locked: bool = False
    restlessness: int = 0
