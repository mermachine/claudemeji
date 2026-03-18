"""windows.py - query visible macOS windows for platform physics

Uses pyobjc-framework-Quartz (optional dependency).
If not installed, returns [] and physics falls back to screen-floor-only.

Install: pip install pyobjc-framework-Quartz
"""
from __future__ import annotations
from dataclasses import dataclass, field
from PyQt6.QtCore import QRect

try:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

MIN_WINDOW_WIDTH  = 100
MIN_WINDOW_HEIGHT = 50


@dataclass
class WindowInfo:
    """Visible on-screen window info from Quartz, used for both physics and wrangling."""
    pid:   int
    rect:  QRect
    title: str = ""
    name:  str = ""   # owner app name
    window_number: int = 0   # system-wide CGWindowID, used for z-ordering
    z_index: int = 0         # position in front-to-back order (0 = frontmost)


def is_available() -> bool:
    return _AVAILABLE


def get_window_infos(own_pid: int | None = None) -> list[WindowInfo]:
    """
    Return WindowInfo for each visible on-screen application window.
    Excludes our own process and non-normal windows.
    Returns [] if Quartz is not available.
    """
    if not _AVAILABLE:
        return []

    windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )
    if not windows:
        return []

    infos = []
    z_idx = 0
    for win in windows:
        if win.get("kCGWindowLayer", 999) != 0:
            continue
        pid = win.get("kCGWindowOwnerPID", 0)
        if own_pid is not None and pid == own_pid:
            continue
        bounds = win.get("kCGWindowBounds")
        if not bounds:
            continue
        x = int(bounds["X"])
        y = int(bounds["Y"])
        w = int(bounds["Width"])
        h = int(bounds["Height"])
        if w < MIN_WINDOW_WIDTH or h < MIN_WINDOW_HEIGHT:
            continue
        infos.append(WindowInfo(
            pid=pid,
            rect=QRect(x, y, w, h),
            title=win.get("kCGWindowName", "") or "",
            name=win.get("kCGWindowOwnerName", "") or "",
            window_number=int(win.get("kCGWindowNumber", 0)),
            z_index=z_idx,
        ))
        z_idx += 1

    return infos


def get_window_rects(own_pid: int | None = None) -> list[QRect]:
    """Convenience wrapper returning just QRects (for physics platform code)."""
    return [info.rect for info in get_window_infos(own_pid)]


def get_platform_tuples(own_pid: int | None = None) -> list[tuple]:
    """Return (QRect, pid, window_number, z_index) tuples for physics.
    Ordered front-to-back (z_index 0 = frontmost)."""
    return [(info.rect, info.pid, info.window_number, info.z_index)
            for info in get_window_infos(own_pid)]
