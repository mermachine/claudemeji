"""
window_wrangler.py - moves other apps' windows using the macOS Accessibility API

Used by the restlessness system to let miku physically interact with windows
when she's been ignored for too long.

Levels:
  3 (grabby) - wiggle: shake a window in place a few pixels
  4 (feral)  - toss: slide a window toward / off a screen edge

Requires Accessibility permission in:
  System Settings > Privacy & Security > Accessibility

If permission is denied or AX is unavailable, all functions are no-ops.

Implementation note:
  AXUIElementCreateApplication / AXUIElementCopyAttributeValue are available
  via pyobjc-framework-ApplicationServices, but AXValueCreate (needed to build
  a settable CGPoint value) is not properly bridged.  We load ApplicationServices
  via ctypes for that one call; everything else uses pyobjc.
"""

from __future__ import annotations
import ctypes
import random
import time
from typing import Any

from PyQt6.QtCore import QRect

# ── optional imports ──────────────────────────────────────────────────────────

_AX_AVAILABLE = False
_AXLib = None

try:
    from ApplicationServices import (
        AXIsProcessTrustedWithOptions,
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementSetAttributeValue,
        kAXWindowsAttribute,
        kAXPositionAttribute,
        kAXMinimizedAttribute,
        kAXRoleAttribute,
    )

    # Load the C library for AXValueCreate (kAXValueCGPointType = 1)
    _AXLib = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )

    class _CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    _AXLib.AXValueCreate.restype  = ctypes.c_void_p
    _AXLib.AXValueCreate.argtypes = [ctypes.c_uint32, ctypes.c_void_p]

    _kAXValueCGPointType = 1  # from AXValue.h

    _AX_AVAILABLE = True
except Exception:
    pass  # graceful degradation


# ── permission helpers ────────────────────────────────────────────────────────

def is_available() -> bool:
    return _AX_AVAILABLE


def is_trusted() -> bool:
    """Return True if we have Accessibility permission (no prompt)."""
    if not _AX_AVAILABLE:
        return False
    try:
        return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": False}))
    except Exception:
        return False


def request_trust():
    """Show the macOS Accessibility permission prompt."""
    if not _AX_AVAILABLE:
        return
    try:
        AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    except Exception:
        pass


# ── AX helpers ────────────────────────────────────────────────────────────────

def _ax_get(element: Any, attribute: str):
    """Return (value, error_code) from AXUIElementCopyAttributeValue."""
    try:
        err, val = AXUIElementCopyAttributeValue(element, attribute, None)
        return val, err
    except Exception:
        return None, -1


_ax_set_pos_errors = 0

# Try to get AXValueCreate via pyobjc (preferred on modern macOS — ctypes bridge is broken on Sequoia)
_USE_PYOBJC_AXVALUE = False
_CGPointMake = None
_AXValueCreate_pyobjc = None
try:
    from Quartz import CGPointMake as _CGPointMake
    from ApplicationServices import AXValueCreate as _AXValueCreate_pyobjc
    _USE_PYOBJC_AXVALUE = True
    print("[claudemeji] AX: using pyobjc AXValue path (CGPointMake + AXValueCreate)")
except ImportError as e:
    print(f"[claudemeji] AX: pyobjc AXValue not available ({e}), using ctypes fallback")


def _ax_set_position(element: Any, x: float, y: float) -> bool:
    """Move an AX window element to (x, y). Returns True on success."""
    global _ax_set_pos_errors

    # Method 1: pure pyobjc (works on macOS Sequoia where ctypes bridge is broken)
    if _USE_PYOBJC_AXVALUE:
        try:
            pt = _CGPointMake(x, y)
            val = _AXValueCreate_pyobjc(1, pt)  # 1 = kAXValueTypeCGPoint
            if val is None:
                if _ax_set_pos_errors < 3:
                    print(f"[claudemeji] _ax_set_position: pyobjc AXValueCreate returned None")
                _ax_set_pos_errors += 1
                return False
            err = AXUIElementSetAttributeValue(element, kAXPositionAttribute, val)
            if err != 0 and _ax_set_pos_errors < 3:
                print(f"[claudemeji] _ax_set_position (pyobjc): err={err} for ({x}, {y})")
            if err != 0:
                _ax_set_pos_errors += 1
            return err == 0
        except Exception as e:
            if _ax_set_pos_errors < 3:
                print(f"[claudemeji] _ax_set_position (pyobjc): exception: {e}")
            _ax_set_pos_errors += 1
            # fall through to ctypes method

    # Method 2: ctypes fallback
    if _AXLib is None:
        return False
    try:
        pt  = _CGPoint(x, y)
        val = _AXLib.AXValueCreate(_kAXValueCGPointType, ctypes.byref(pt))
        if not val:
            return False
        err = AXUIElementSetAttributeValue(element, kAXPositionAttribute,
                                           ctypes.c_void_p(val))
        if err != 0 and _ax_set_pos_errors < 3:
            print(f"[claudemeji] _ax_set_position (ctypes): err={err} for ({x}, {y})")
        if err != 0:
            _ax_set_pos_errors += 1
        return err == 0
    except Exception as e:
        if _ax_set_pos_errors < 3:
            print(f"[claudemeji] _ax_set_position (ctypes): exception: {e}")
        _ax_set_pos_errors += 1
        return False


def _find_ax_window(pid: int, rect: QRect | None = None, tolerance: int = 40) -> Any | None:
    """
    Find the AX window element for a given pid.

    If rect is provided, tries to match by position (with tolerance).
    Falls back to the first AX window for that pid if position matching fails.
    """
    if not _AX_AVAILABLE:
        return None
    try:
        app_el = AXUIElementCreateApplication(pid)
        windows, err = _ax_get(app_el, kAXWindowsAttribute)
        if err != 0 or not windows:
            print(f"[claudemeji] AX: no windows for pid {pid} (err={err})")
            return None

        # filter to real windows (skip menubars, etc.)
        real_windows = []
        for win in windows:
            role, rerr = _ax_get(win, kAXRoleAttribute)
            if rerr == 0 and role == "AXWindow":
                real_windows.append(win)

        if not real_windows:
            # fall back to all windows if role filtering found nothing
            real_windows = list(windows)

        # try position matching first
        if rect is not None:
            for win in real_windows:
                pos_val, err2 = _ax_get(win, kAXPositionAttribute)
                if err2 != 0 or pos_val is None:
                    continue
                try:
                    pt = _CGPoint()
                    ok = _AXLib.AXValueGetValue(
                        ctypes.c_void_p(id(pos_val)),
                        _kAXValueCGPointType,
                        ctypes.byref(pt),
                    )
                    if ok:
                        wx, wy = pt.x, pt.y
                        if (abs(wx - rect.x()) <= tolerance and
                                abs(wy - rect.y()) <= tolerance):
                            return win
                except Exception:
                    continue

        # fallback: just return the first real window for this pid
        if real_windows:
            return real_windows[0]
        return None
    except Exception as e:
        print(f"[claudemeji] AX: error finding window for pid {pid}: {e}")
        return None


# ── setup AXValueGetValue (needed above) ─────────────────────────────────────

if _AXLib is not None:
    try:
        _AXLib.AXValueGetValue.restype  = ctypes.c_bool
        _AXLib.AXValueGetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p
        ]
    except Exception:
        pass


# ── public wrangling API ──────────────────────────────────────────────────────

def move_window_by(pid: int, rect: QRect, dx: float, dy: float) -> bool:
    """
    Move a window by (dx, dy) pixels from its current position.
    Used by sprite weight-pulling. Returns True if successful.
    """
    if not is_trusted():
        return False
    win = _find_ax_window(pid, rect)
    if win is None:
        return False
    return _ax_set_position(win, float(rect.x()) + dx, float(rect.y()) + dy)


def move_window_to(pid: int, target_x: float, target_y: float) -> bool:
    """
    Move a window to an absolute position. Uses pid-only matching (no position
    matching needed) since we know exactly where we want the window.
    Used by push/carry where the window tracks the sprite in real-time.
    Note: trust check is done by caller (_ax_threaded), not here.
    """
    win = _find_ax_window(pid, rect=None)
    if win is None:
        print(f"[claudemeji] move_window_to: no AX window for pid {pid}")
        return False
    return _ax_set_position(win, target_x, target_y)


def wiggle_window(pid: int, rect: QRect, amplitude: int = 12, shakes: int = 4) -> bool:
    """
    Shake a window horizontally a few pixels — level 3 (grabby).
    Synchronous: runs the shake instantly (called from a QTimer so it's non-blocking
    for the UI thread, but each wiggle step happens in sequence here).
    Returns True if the window was found and moved.
    """
    if not is_trusted():
        return False
    win = _find_ax_window(pid, rect)
    if win is None:
        return False

    ox, oy = float(rect.x()), float(rect.y())
    offsets = []
    for i in range(shakes):
        sign = 1 if i % 2 == 0 else -1
        offsets.append(sign * amplitude)
    offsets.append(0)  # return to origin

    for dx in offsets:
        _ax_set_position(win, ox + dx, oy)
        time.sleep(0.05)

    return True


def _minimize_ax_window(win) -> bool:
    """Minimize an AX window element. Returns True on success."""
    try:
        from Foundation import NSNumber
        err = AXUIElementSetAttributeValue(win, kAXMinimizedAttribute,
                                           NSNumber.numberWithBool_(True))
        return err == 0
    except Exception:
        return False


def minimize_window(pid: int, rect: QRect) -> bool:
    """
    Minimize a window via the Accessibility API.
    Used by window throw (the window is 'tossed away' = minimized).
    """
    if not is_trusted():
        return False
    win = _find_ax_window(pid, rect)
    return _minimize_ax_window(win) if win else False


def throw_and_minimize(pid: int, rect: QRect, screen_rect: QRect,
                       direction: str = "up") -> bool:
    """
    Throw a window in a parabolic arc, then minimize it.
    Faithful to original shimemiku ThrowIE: vx=32, vy=-10, gravity=0.5
    (scaled for real-time steps rather than per-frame ticks).
    """
    if not is_trusted():
        return False
    win = _find_ax_window(pid, rect)
    if win is None:
        return False

    ox, oy = float(rect.x()), float(rect.y())
    # shimemiku-inspired parabolic arc (gentler than original — windows are bigger than IE)
    vx = 8.0 if direction == "right" else -8.0 if direction == "left" else 0.0
    vy = -8.0  # initial upward velocity
    gravity = 0.5
    steps = 25
    step_s = 0.025

    cx, cy = ox, oy
    for i in range(steps):
        cx += vx
        vy += gravity
        cy += vy
        # tumble wobble (decays over time)
        wobble = 6 * ((-1) ** i) * max(0, 1.0 - i / steps)
        _ax_set_position(win, cx + wobble, cy)
        time.sleep(step_s)

    return _minimize_ax_window(win)


def toss_window_up(pid: int, rect: QRect) -> bool:
    """
    Toss a window upward — it bounces up and settles back down.
    Used by side-toss: miku grabs the side and flicks it up.
    No minimize — just a playful shove.
    """
    if not is_trusted():
        return False
    win = _find_ax_window(pid, rect)
    if win is None:
        return False

    ox, oy = float(rect.x()), float(rect.y())
    # bounce: up 80-180px, slight horizontal wobble, then back down
    toss_height = random.uniform(80, 180)
    steps = 20
    step_s = 0.022

    for i in range(steps):
        t = i / steps
        # parabolic arc: up then back down to original position
        arc_y = -toss_height * 4 * t * (1 - t)
        wobble_x = random.uniform(-3, 3) * (1 - t)  # wobble decays
        _ax_set_position(win, ox + wobble_x, oy + arc_y)
        time.sleep(step_s)

    # settle back to original position
    _ax_set_position(win, ox, oy)
    return True


def toss_window(pid: int, rect: QRect, screen_rect: QRect,
                direction: str | None = None) -> bool:
    """
    Slide a window toward a screen edge, then release it — level 4 (feral).
    direction: "left" | "right" | "up" | "down" | None (random)
    Returns True if the window was found and moved.
    """
    if not is_trusted():
        return False
    win = _find_ax_window(pid, rect)
    if win is None:
        return False

    if direction is None:
        direction = random.choice(["left", "right", "right", "down"])  # bias rightward

    ox, oy = float(rect.x()), float(rect.y())
    steps  = 20
    step_s = 0.02

    if direction == "right":
        tx = float(screen_rect.right() - rect.width() // 2)
        ty = oy
    elif direction == "left":
        tx = float(screen_rect.left() - rect.width() // 2)
        ty = oy
    elif direction == "up":
        tx = ox
        ty = float(screen_rect.top() - rect.height() + 40)
    else:  # down
        tx = ox
        ty = float(screen_rect.bottom() - rect.height() // 2)

    for i in range(steps + 1):
        t = i / steps
        # ease-in: slow start, fast finish (miku is pushing hard)
        t_eased = t * t
        nx = ox + (tx - ox) * t_eased
        ny = oy + (ty - oy) * t_eased
        _ax_set_position(win, nx, ny)
        time.sleep(step_s)

    return True
