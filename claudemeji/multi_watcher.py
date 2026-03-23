"""
multi_watcher.py - watches all session event files, dispatches with session_id

Instead of tailing one .jsonl file (like HookWatcher), this watches the entire
~/.claudemeji/events/ directory and creates a per-file watcher for each session.

Emits: event_received(session_id: str, event: dict)
"""

from __future__ import annotations

import os
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from claudemeji.watcher import HookWatcher, EVENTS_DIR

# only watch session files modified in the last 5 minutes —
# avoids spawning watchers for ancient dead sessions on startup
_STALE_THRESHOLD = 300


class MultiHookWatcher(QObject):
    """Watches all session event files, dispatches events tagged with session_id.

    Polls the events directory every 2s for new .jsonl files. Each file gets
    its own HookWatcher that tails from the current end (so we don't replay
    old events from sessions that started before the conductor).
    """

    event_received = pyqtSignal(str, dict)  # (session_id, event)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._watchers: dict[str, HookWatcher] = {}

        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(2000)
        self._scan_timer.timeout.connect(self._check_new_sessions)

    def start(self):
        """Start scanning for session files."""
        os.makedirs(EVENTS_DIR, exist_ok=True)
        self._check_new_sessions()
        self._scan_timer.start()

    def stop(self):
        """Stop all watchers."""
        self._scan_timer.stop()
        for watcher in self._watchers.values():
            watcher.stop()

    def stop_watching(self, session_id: str):
        """Stop and remove the watcher for a specific session."""
        watcher = self._watchers.pop(session_id, None)
        if watcher:
            watcher.stop()

    @property
    def active_sessions(self) -> list[str]:
        return list(self._watchers.keys())

    def _check_new_sessions(self):
        """Scan events dir for new session files (only recent ones)."""
        if not os.path.isdir(EVENTS_DIR):
            return

        try:
            files = os.listdir(EVENTS_DIR)
        except OSError:
            return

        now = time.time()
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            session_id = f[:-6]  # strip .jsonl
            if session_id in self._watchers:
                continue
            # skip stale session files
            path = os.path.join(EVENTS_DIR, f)
            try:
                mtime = os.path.getmtime(path)
                if (now - mtime) > _STALE_THRESHOLD:
                    continue
            except OSError:
                continue
            self._start_watching(session_id)

    def _start_watching(self, session_id: str):
        """Create a HookWatcher for one session file and wire its signals."""
        watcher = HookWatcher(session_id=session_id)

        # relay events with the session_id tag
        watcher.event_received.connect(
            lambda event, sid=session_id: self.event_received.emit(sid, event)
        )

        # idle/wait signals — synthesize events for the conductor
        watcher.idle_triggered.connect(
            lambda sid=session_id: self.event_received.emit(sid, {"event_type": "_idle"})
        )
        watcher.wait_triggered.connect(
            lambda sid=session_id: self.event_received.emit(sid, {"event_type": "_wait"})
        )
        watcher.wait_cleared.connect(
            lambda sid=session_id: self.event_received.emit(sid, {"event_type": "_wait_cleared"})
        )
        watcher.permission_requested.connect(
            lambda sid=session_id: self.event_received.emit(sid, {"event_type": "_permission_requested"})
        )
        watcher.tool_denied.connect(
            lambda sid=session_id: self.event_received.emit(sid, {"event_type": "_tool_denied"})
        )

        self._watchers[session_id] = watcher
        watcher.start()
        print(f"[claudemeji:multi_watcher] watching session {session_id[:12]}...")
