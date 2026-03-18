"""
platform_utils.py - platform-specific window tweaks

macOS needs native NSWindow calls for:
  - true always-on-top (NSFloatingWindowLevel)
  - disabling the drop shadow on transparent windows
  - z-ordering relative to other app windows (for behind-window effect)

Must be called after the window is fully shown (use a QTimer.singleShot delay).
"""

from __future__ import annotations
import sys

# cached ctypes handles (initialized once)
_objc = None
_send = None


def _init_objc():
    """Lazily initialize ctypes objc bridge. Returns (objc, send, sel_fn)."""
    global _objc, _send
    if _objc is not None:
        return _objc, _send, _sel

    import ctypes
    _objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    _send = _objc.objc_msgSend
    return _objc, _send, _sel


def _sel(name: str):
    import ctypes
    _objc.sel_registerName.restype = ctypes.c_void_p
    return _objc.sel_registerName(name.encode())


def _get_nswindow(widget):
    """Get NSWindow pointer from a QWidget. Returns ctypes.c_void_p or None."""
    import ctypes
    objc, send, sel = _init_objc()

    nsview = ctypes.c_void_p(int(widget.winId()))
    send.restype = ctypes.c_void_p
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    nswindow = ctypes.c_void_p(send(nsview, sel("window")))
    return nswindow if nswindow else None


def apply_macos_window_fixes(widget):
    """No-op on non-macOS."""
    global _current_level
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        objc, send, sel = _init_objc()

        nswindow = _get_nswindow(widget)
        if not nswindow:
            print("[claudemeji] macOS: could not get NSWindow")
            return

        # NSFloatingWindowLevel = 3
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(nswindow, sel("setLevel:"), _NSFloatingWindowLevel)
        _current_level = _NSFloatingWindowLevel

        # setHasShadow: NO
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        send(nswindow, sel("setHasShadow:"), False)

        # collection behavior: CanJoinAllSpaces | Stationary | IgnoresCycle
        # prevents hiding during app switch / Mission Control / Exposé
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        send(nswindow, sel("setCollectionBehavior:"), 1 | 16 | 64)

        # set app activation policy to Accessory so macOS doesn't hide our
        # windows when another app is focused (1 = NSApplicationActivationPolicyAccessory)
        objc.objc_getClass.restype = ctypes.c_void_p
        NSApp_class = ctypes.c_void_p(objc.objc_getClass(b"NSApplication"))
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        nsapp = ctypes.c_void_p(send(NSApp_class, sel("sharedApplication")))
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(nsapp, sel("setActivationPolicy:"), 1)

    except Exception as e:
        print(f"[claudemeji] macOS window fixes failed: {e}")


# --- z-ordering ---

# NSWindowOrderingMode
_NSWindowAbove = 1
_NSWindowBelow = -1

# NSWindowLevel
_NSNormalWindowLevel = 0
_NSFloatingWindowLevel = 3

# track current state to avoid redundant calls
_current_level = _NSFloatingWindowLevel


def set_window_floating(widget):
    """Raise sprite window to NSFloatingWindowLevel (above all normal windows)."""
    global _current_level
    if sys.platform != "darwin":
        return
    if _current_level == _NSFloatingWindowLevel:
        return  # already floating
    try:
        import ctypes
        _init_objc()
        nswindow = _get_nswindow(widget)
        if not nswindow:
            return

        send = _send
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(nswindow, _sel("setLevel:"), _NSFloatingWindowLevel)

        # orderFront: to ensure we're at the top of our level
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        send(nswindow, _sel("orderFront:"), None)

        _current_level = _NSFloatingWindowLevel
    except Exception as e:
        print(f"[claudemeji] set_window_floating failed: {e}")


def set_window_above(widget, target_window_number: int):
    """Lower sprite to NSNormalWindowLevel and order just above a specific window.
    target_window_number is a system-wide CGWindowID from CGWindowListCopyWindowInfo.

    This makes miku appear at the same z-depth as the window she's interacting with,
    so she goes behind any windows that are stacked above it."""
    global _current_level
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        _init_objc()
        nswindow = _get_nswindow(widget)
        if not nswindow:
            return

        send = _send

        # drop to normal window level
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(nswindow, _sel("setLevel:"), _NSNormalWindowLevel)

        # ensure shadow stays off and collection behavior persists at new level
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        send(nswindow, _sel("setHasShadow:"), False)
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        send(nswindow, _sel("setCollectionBehavior:"), 1 | 16 | 64)

        # order above the target window
        # orderWindow:relativeTo: takes (NSWindowOrderingMode, windowNumber)
        # windowNumber is system-wide, works cross-app
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_long, ctypes.c_long]
        send(nswindow, _sel("orderWindow:relativeTo:"),
             _NSWindowAbove, target_window_number)

        _current_level = _NSNormalWindowLevel
    except Exception as e:
        print(f"[claudemeji] set_window_above({target_window_number}) failed: {e}")


def is_floating() -> bool:
    """Check if sprite is currently at floating level."""
    return _current_level == _NSFloatingWindowLevel
