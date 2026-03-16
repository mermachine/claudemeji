"""
platform_utils.py - platform-specific window tweaks

macOS needs native NSWindow calls for:
  - true always-on-top (NSFloatingWindowLevel)
  - disabling the drop shadow on transparent windows

Must be called after the window is fully shown (use a QTimer.singleShot delay).
"""

from __future__ import annotations
import sys


def apply_macos_window_fixes(widget):
    """No-op on non-macOS."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes

        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")

        # set up the messenger with a generic signature; we'll cast per-call
        send = objc.objc_msgSend

        def sel(name: str) -> ctypes.c_void_p:
            objc.sel_registerName.restype = ctypes.c_void_p
            return objc.sel_registerName(name.encode())

        # get NSView from Qt winId
        nsview = ctypes.c_void_p(int(widget.winId()))

        # NSView -> NSWindow
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        nswindow = ctypes.c_void_p(send(nsview, sel("window")))

        if not nswindow:
            print("[claudemeji] macOS: could not get NSWindow")
            return

        # NSFloatingWindowLevel = 3
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(nswindow, sel("setLevel:"), 3)

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

        print("[claudemeji] macOS: window level, collection behavior, activation policy set")

    except Exception as e:
        print(f"[claudemeji] macOS window fixes failed: {e}")
