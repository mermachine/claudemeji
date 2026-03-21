"""
resolver.py - maps creature state to animation action names

This is the ONLY place that knows the animation vocabulary.
Pure function, no side effects, no state.
"""

from __future__ import annotations
from claudemeji.creature import (
    CreatureState, CreatureEvent, Posture, SpeedTier, CarryPhase, ClimbSurface,
)

# fall distance thresholds (pixels)
FALL_TINY = 40       # below this: no landing animation
FALL_SOFT = 400      # below this: gentle landing; above: full dramatic tumble


def resolve_animation(state: CreatureState, event: CreatureEvent | None = None) -> str:
    """Map creature state + optional discrete event to an animation action name.

    Priority: event → carry phase → launched/falling → posture+speed → default.
    Returns a canonical action name (e.g. "walk", "climb", "fall").
    """
    # discrete events always win — they play as one-shots
    if event is not None:
        return _EVENT_ANIMS.get(event, "stand")

    # carry has sub-phases that each need different animations
    if state.posture == Posture.CARRYING:
        if state.carry_phase == CarryPhase.CARRY:
            return "window_carry"
        return _CARRY_PHASE_ANIMS.get(state.carry_phase, "window_carry")

    # falling: jump pose while launched (ascending), fall pose while descending.
    # fall_distance only affects landing events, not in-flight animation.
    if state.posture == Posture.FALLING:
        return "jump" if state.launched else "fall"

    # walking speed determines which animation
    if state.posture == Posture.WALKING:
        return _SPEED_ANIMS.get(state.speed_tier, "walk")

    # hanging: ceiling vs wall variants
    if state.posture == Posture.HANGING:
        if state.climb_surface == ClimbSurface.CEILING:
            return "hang_ceiling"
        return "hang"

    # everything else maps directly from posture
    return _POSTURE_ANIMS.get(state.posture, "stand")


# --- mapping tables ---

_EVENT_ANIMS = {
    CreatureEvent.LANDED_HARD:   "land",
    CreatureEvent.LANDED_SOFT:   "land_soft",
    CreatureEvent.LANDED_TINY:   "stand",      # no animation, just resume
    CreatureEvent.TRIPPED:       "trip",
    CreatureEvent.THREW_WINDOW:  "window_throw",
    CreatureEvent.CARRY_CHEERED: "window_carry_cheer",
}

_SPEED_ANIMS = {
    SpeedTier.STILL:  "stand",
    SpeedTier.CRAWL:  "crawl",
    SpeedTier.WALK:   "walk",
    SpeedTier.RUN:    "run",
    SpeedTier.SPRINT: "sprint",
}

_CARRY_PHASE_ANIMS = {
    CarryPhase.JUMP:         "jump",
    CarryPhase.GRAB_FALL:    "fall",
    CarryPhase.PERCH:        "window_carry_perch",
    CarryPhase.CARRY:        "window_carry",
    CarryPhase.THROW_WINDUP: "window_throw",
}

_POSTURE_ANIMS = {
    Posture.STANDING: "stand",
    Posture.SITTING:  "sit_idle",
    Posture.FALLING:  "fall",
    Posture.CLIMBING: "climb",
    Posture.CEILING:  "ceiling",
    Posture.DRAGGED:  "drag",
    Posture.HANGING:  "hang",
    Posture.PUSHING:  "window_push",
    Posture.PEEKING:  "window_peek",
    Posture.CARRYING: "window_carry",
}
