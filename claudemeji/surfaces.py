"""
surfaces.py - platform/surface queries for window walking

Pure functions for finding walkable surfaces, detecting occlusion,
and computing soft walls. Platforms are (QRect, pid, window_number, z_index)
tuples ordered front-to-back.
"""

from __future__ import annotations
from PyQt6.QtCore import QRect

# how close (px) a position must be to a surface to count as "on" it
SURFACE_TOLERANCE = 4.0


# --- platform tuple accessors ---

def plat_rect(p) -> QRect:   return p[0]
def plat_pid(p) -> int:      return p[1]
def plat_winnum(p) -> int:   return p[2] if len(p) > 2 else 0
def plat_zidx(p) -> int:     return p[3] if len(p) > 3 else 0


# --- surface queries ---

def find_surface_below(platforms, x: float, y_from: float, miku_w: float,
                       miku_h: float, screen_floor: float,
                       ignore_pid: int = 0,
                       only_visible: bool = False) -> float:
    """Highest surface at x that is at or below y_from.
    ignore_pid: skip platforms owned by this pid (for drop-through).
    only_visible: skip surfaces occluded by higher-z windows at x."""
    best = screen_floor
    for plat in platforms:
        rect, pid = plat_rect(plat), plat_pid(plat)
        if ignore_pid and pid == ignore_pid:
            continue
        if x + miku_w > rect.left() and x < rect.right():
            surface = float(rect.top()) - miku_h
            if surface >= y_from and surface < best:
                if only_visible and is_surface_occluded(platforms, plat, x, miku_w):
                    continue
                best = surface
    return best


def is_surface_occluded(platforms, target_plat, x: float, miku_w: float) -> bool:
    """Is the top surface of target_plat hidden by a higher-z window at x?
    A surface is occluded if a window in front covers the same x range at the
    target's top edge (meaning miku would be invisible standing there)."""
    target_rect = plat_rect(target_plat)
    target_top = float(target_rect.top())
    target_zidx = plat_zidx(target_plat)
    for plat in platforms:
        if plat_zidx(plat) >= target_zidx:
            break  # reached target's depth — nothing in front left
        rect = plat_rect(plat)
        # does this window cover the landing zone at the target's top?
        if (rect.left() < x + miku_w and rect.right() > x
                and rect.top() <= target_top and rect.bottom() > target_top):
            return True
    return False


def surface_at(platforms, x: float, floor_y: float, miku_w: float,
               miku_h: float, screen_floor: float) -> bool:
    """Is there a walkable surface at (x, floor_y)?"""
    if abs(floor_y - screen_floor) < SURFACE_TOLERANCE:
        return True
    for plat in platforms:
        rect = plat_rect(plat)
        if x + miku_w > rect.left() and x < rect.right():
            surface = float(rect.top()) - miku_h
            if abs(surface - floor_y) < SURFACE_TOLERANCE:
                return True
    return False


def find_platform_at(platforms, x: float, floor_y: float, miku_w: float,
                     miku_h: float, screen_floor: float):
    """Which platform is at floor_y? Returns full platform tuple or None."""
    if abs(floor_y - screen_floor) < SURFACE_TOLERANCE:
        return None
    for plat in platforms:
        rect = plat_rect(plat)
        if x + miku_w > rect.left() and x < rect.right():
            surface = float(rect.top()) - miku_h
            if abs(surface - floor_y) < SURFACE_TOLERANCE:
                return plat
    return None


def occlusion_wall_ahead(platforms, standing_plat, x: float, walk_dir: int,
                         miku_w: float, miku_h: float,
                         sprint_speed: float) -> float | None:
    """Find the x boundary where an occluding window blocks further travel.
    Returns the x position of the soft wall, or None if the path is clear.
    The wall is placed so miku peeks out by ~half her width."""
    if standing_plat is None:
        return None
    standing_zidx = plat_zidx(standing_plat)
    standing_rect = plat_rect(standing_plat)
    standing_top = float(standing_rect.top())
    # miku's body extends from standing_top - miku_h (head) to standing_top (feet)
    miku_top = standing_top - miku_h
    peek_amount = miku_w * 0.4  # how far she peeks out from behind the occluder

    for plat in platforms:
        if plat_zidx(plat) >= standing_zidx:
            break  # only check windows in front of what she's standing on
        rect = plat_rect(plat)
        # does this window overlap vertically with miku's body?
        if rect.bottom() <= miku_top or rect.top() >= standing_top:
            continue
        if walk_dir > 0:
            wall_x = float(rect.left()) - miku_w + peek_amount
            # return wall if ahead OR just passed (overshoot by up to sprint_speed)
            if wall_x >= x - sprint_speed and wall_x < x + miku_w * 4:
                return wall_x
        elif walk_dir < 0:
            wall_x = float(rect.right()) - peek_amount
            if wall_x <= x + sprint_speed and wall_x > x - miku_w * 4:
                return wall_x
    return None
