"""
watcher.py - watches the state file written by claude code hooks

Emits events from the hook log, plus synthetic events:
  - idle_triggered: no events for IDLE_TIMEOUT seconds
  - wait_triggered: tool_start seen but no tool_end within WAIT_TIMEOUT seconds
                    (i.e. a very slow tool — permission prompts are caught earlier)
  - wait_cleared:   tool_end arrived, cancels the wait state
  - permission_requested: Notification hook fired with notification_type=permission_prompt
  - tool_denied:    a pending tool got a bare notification (no permission_prompt) without
                    ever receiving a tool_end — the user denied the permission
"""

from __future__ import annotations
import json
import os
import time
import threading
from PyQt6.QtCore import QObject, pyqtSignal

EVENTS_DIR    = os.path.expanduser("~/.claudemeji/events")
LEGACY_FILE   = os.path.expanduser("~/.claudemeji/events.jsonl")  # pre-session-aware fallback
IDLE_TIMEOUT  = 8.0   # seconds of silence → idle
WAIT_TIMEOUT  = 3.0   # seconds after tool_start without tool_end → waiting


def _find_latest_session_file() -> str:
    """Find the most recently modified session file, or fall back to legacy path."""
    if os.path.isdir(EVENTS_DIR):
        files = [
            os.path.join(EVENTS_DIR, f)
            for f in os.listdir(EVENTS_DIR)
            if f.endswith(".jsonl")
        ]
        if files:
            newest = max(files, key=os.path.getmtime)
            print(f"[claudemeji] auto-selected session: {os.path.basename(newest)}")
            return newest
    return LEGACY_FILE


class HookWatcher(QObject):
    event_received       = pyqtSignal(dict)
    idle_triggered       = pyqtSignal()
    wait_triggered       = pyqtSignal()   # tool taking too long (fallback, 3s)
    wait_cleared         = pyqtSignal()   # tool finished, cancel wait state
    permission_requested = pyqtSignal()   # claude needs user permission (instant)
    tool_denied          = pyqtSignal()   # user denied a pending tool

    def __init__(self, session_id: str | None = None, parent=None):
        super().__init__(parent)
        if session_id:
            self._state_file = os.path.join(EVENTS_DIR, f"{session_id}.jsonl")
        else:
            # auto-discover: find the most recently active session
            self._state_file = _find_latest_session_file()
        self._stop_flag = threading.Event()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True)

    def start(self):
        os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
        if not os.path.exists(self._state_file):
            open(self._state_file, "w").close()
        self._thread.start()

    def stop(self):
        self._stop_flag.set()

    def _watch_loop(self):
        last_event_time = time.monotonic()
        idle_emitted    = False
        pending_tool_start = None   # monotonic time of last tool_start without matching tool_end
        wait_emitted    = False
        # track tool_use_ids awaiting permission (permission_prompt seen, no tool_end yet)
        permission_pending_ids: set[str] = set()

        with open(self._state_file, "r") as f:
            f.seek(0, 2)   # tail from end

            while not self._stop_flag.is_set():
                line = f.readline()
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("event_type", "")
                    tool_use_id = event.get("tool_use_id", "")
                    handled = False  # set True if a specialized signal consumed this event

                    if etype == "tool_start":
                        pending_tool_start = time.monotonic()
                        wait_emitted = False

                    elif etype == "tool_end":
                        if wait_emitted:
                            self.wait_cleared.emit()
                            wait_emitted = False
                        pending_tool_start = None
                        permission_pending_ids.discard(tool_use_id)

                    elif etype == "notification":
                        raw = event.get("raw", {})
                        notif_type = raw.get("notification_type", "") if raw else ""

                        if notif_type == "permission_prompt":
                            # claude is asking for permission — fire wait immediately
                            self.permission_requested.emit()
                            permission_pending_ids.add(tool_use_id if tool_use_id else "_last")
                            wait_emitted = True  # suppress the 3s fallback
                            handled = True
                        elif permission_pending_ids:
                            # bare notification while tools are awaiting permission
                            # = user denied a tool
                            self.tool_denied.emit()
                            permission_pending_ids.clear()
                            wait_emitted = False
                            handled = True
                        else:
                            handled = False

                    # don't emit event_received for notifications already handled
                    # by specialized signals (otherwise state machine stomps with "think")
                    if etype != "notification" or not handled:
                        self.event_received.emit(event)
                    last_event_time = time.monotonic()
                    idle_emitted = False

                else:
                    now = time.monotonic()

                    # wait detection: tool_start with no tool_end for WAIT_TIMEOUT
                    # (fallback for tools that don't trigger a permission prompt)
                    if (pending_tool_start is not None
                            and not wait_emitted
                            and (now - pending_tool_start) >= WAIT_TIMEOUT):
                        self.wait_triggered.emit()
                        wait_emitted = True

                    # idle detection
                    if (now - last_event_time) >= IDLE_TIMEOUT and not idle_emitted:
                        self.idle_triggered.emit()
                        idle_emitted = True

                    time.sleep(0.1)
