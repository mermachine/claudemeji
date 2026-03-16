"""
restlessness.py - tracks how long claude has been ignored and escalates chaos

Restlessness levels (0–4):
  0  calm    - normal wandering
  1  fidgety - faster/more erratic movement, higher climb chance
  2  climby  - seeks walls and windows to climb, stays up longer
  3  grabby  - starts wiggling windows (requires AX permission)
  4  feral   - throws windows toward screen edges

Level escalates when:
  - Claude Code session has been idle for RESTLESS_INTERVAL_S per level tick
  - AND the user is not in a "productive" app (coding, terminal, finder)

Level decreases when:
  - user grabs the shimeji     → -1
  - any claude code event      → reset to 0

Apps that count as "productive" (won't trigger escalation even while idle):
  terminals, editors, VS Code, Cursor, Finder, Xcode, etc.
  Notably NOT in the list: browsers, Discord, Slack, games.
"""

from __future__ import annotations
import time
from PyQt6.QtCore import QObject, QTimer, pyqtSignal


# ── defaults (overridable via config) ────────────────────────────────────────

RESTLESS_INTERVAL_S       = 5 * 60   # seconds of idleness per level tick (5 min)
RESTLESS_CHECK_INTERVAL_MS = 15_000  # how often to evaluate (15s)
RESTLESS_MAX_LEVEL        = 4

# Wrangle trigger: at level ≥ 3, wrangle a window this often (seconds)
WRANGLE_INTERVAL_S = {
    3: 45.0,   # level 3: wiggle every ~45s
    4: 20.0,   # level 4: toss every ~20s
}

# Bundle IDs / prefixes considered "you're coding, calm down"
PRODUCTIVE_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.apple.finder",
    "com.apple.Terminal",
    "com.googlecode.iterm2",
    "net.kovidgoyal.kitty",
    "org.alacritty",
    "io.alacritty",
    "com.microsoft.VSCode",
    "com.todesktop.230313mzl4w4u92",  # Cursor
    "com.anthropic.claudefordesktop",
    "com.apple.systempreferences",
    "com.apple.SystemPreferences",
    "com.apple.ActivityMonitor",
    "com.apple.Console",
    "com.apple.dt.Xcode",
    "com.sublimetext.3",
    "com.sublimetext.4",
    "com.panic.Nova",
    "org.vim.MacVim",
    # JetBrains — also matched by prefix below
    "com.jetbrains.intellij",
    "com.jetbrains.pycharm",
    "com.jetbrains.webstorm",
    "com.jetbrains.goland",
    "com.jetbrains.clion",
})

PRODUCTIVE_PREFIXES: tuple[str, ...] = (
    "com.jetbrains.",
    "com.apple.dt.",
)


# ── app detection ─────────────────────────────────────────────────────────────

def _frontmost_bundle_id() -> str | None:
    """Return the bundle ID of the frontmost app, or None if unavailable."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return app.bundleIdentifier()
    except Exception:
        return None


def in_productive_app() -> bool:
    """
    Return True if the user is currently in a coding / productive app.
    When True, restlessness won't escalate even if Claude is idle.
    Returns True on any error (safe default: don't escalate when uncertain).
    """
    bid = _frontmost_bundle_id()
    if bid is None:
        return True  # can't tell → assume productive
    if bid in PRODUCTIVE_BUNDLE_IDS:
        return True
    for prefix in PRODUCTIVE_PREFIXES:
        if bid.startswith(prefix):
            return True
    return False


# ── restlessness engine ───────────────────────────────────────────────────────

class RestlessnessEngine(QObject):
    """
    Tracks idleness and escalates shimeji chaos when the user is ignoring Claude.

    Signals:
      level_changed(int)  - restlessness level changed (0–4)
      wrangle_window(int) - time to mess with a window; arg is current level
    """

    level_changed  = pyqtSignal(int)
    wrangle_window = pyqtSignal(int)

    def __init__(
        self,
        interval_s: float = RESTLESS_INTERVAL_S,
        parent=None,
    ):
        super().__init__(parent)
        self._interval_s       = interval_s
        self._level            = 0
        self._last_event_time  = time.monotonic()
        self._last_wrangle_time = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(RESTLESS_CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()

    @property
    def level(self) -> int:
        return self._level

    # ── external notifications ────────────────────────────────────────────────

    def notify_event(self):
        """Call on every incoming claude code hook event."""
        self._last_event_time = time.monotonic()
        if self._level > 0:
            self._set_level(0)

    def notify_grabbed(self):
        """User grabbed the shimeji — dial back the chaos by one level."""
        if self._level > 0:
            self._set_level(self._level - 1)
            print(f"[claudemeji] grabbed — restlessness → {self._level}")

    # ── internals ─────────────────────────────────────────────────────────────

    def _set_level(self, level: int):
        level = max(0, min(RESTLESS_MAX_LEVEL, level))
        if level != self._level:
            self._level = level
            print(f"[claudemeji] restlessness level → {level}")
            self.level_changed.emit(level)

    def _check(self):
        now     = time.monotonic()
        idle_s  = now - self._last_event_time

        # how many level ticks has this idleness earned?
        earned = min(RESTLESS_MAX_LEVEL, int(idle_s / self._interval_s))

        if earned > self._level:
            # only escalate if the user isn't actively coding
            if not in_productive_app():
                self._set_level(earned)
            # if they're in a productive app, don't escalate — they're just focused

        # at level ≥ 3, periodically emit wrangle_window
        if self._level >= 3:
            interval = WRANGLE_INTERVAL_S.get(self._level, 30.0)
            if now - self._last_wrangle_time >= interval:
                self._last_wrangle_time = now
                self.wrangle_window.emit(self._level)
