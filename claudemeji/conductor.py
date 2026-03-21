"""
conductor.py - MikuManager: orchestrates multiple MikuSlots

One process, many Miku instances. The conductor owns the shared resources
(platform refresh, AX worker) and routes events from MultiHookWatcher
to the appropriate per-session MikuSlot.
"""

from __future__ import annotations

import os
import random

from PyQt6.QtCore import QObject, QTimer

from claudemeji.config import Config
from claudemeji.slot import MikuSlot
from claudemeji.windows import get_platform_tuples, is_available as windows_available


class MikuManager(QObject):
    """Manages multiple MikuSlots, one per Claude Code session.

    Shared responsibilities:
      - platform refresh (2s timer, pushed to all slots)
      - slot lifecycle (create on session_start, destroy on session_stop)
      - position staggering so multiple mikus don't stack
    """

    def __init__(self, config: Config | None, ax_threaded, parent=None):
        super().__init__(parent)
        self._slots: dict[str, MikuSlot] = {}
        self._config = config
        self._ax_threaded = ax_threaded
        self._slot_counter = 0

        # protect parent process from window interactions
        self._parent_pid = os.getppid()

        # shared platform refresh
        self._platform_timer = QTimer(self)
        self._platform_timer.setInterval(2000)
        self._platform_timer.timeout.connect(self._refresh_platforms)
        self._platform_timer.start()
        self._refresh_platforms()

    # --- public API ---

    def on_session_event(self, session_id: str, event: dict):
        """Route an event to the right slot, creating on session_start."""
        etype = event.get("event_type", "")

        # create slot on first event from any session — not just session_start.
        # the multi_watcher already filters out stale files (>5min old), so any
        # event we receive is from a live session worth tracking.
        if session_id not in self._slots:
            if etype == "session_stop":
                return  # don't create a slot just to immediately destroy it
            self._create_slot(session_id)

        slot = self._slots.get(session_id)
        if not slot:
            return

        # dispatch synthetic events to dedicated handlers
        if etype == "_idle":
            slot.handle_idle()
        elif etype == "_wait":
            slot.handle_wait_triggered()
        elif etype == "_wait_cleared":
            slot.handle_wait_cleared()
        else:
            slot.handle_event(event)

        # tear down on explicit session stop
        if etype == "session_stop":
            self._destroy_slot(session_id)

    @property
    def slots(self) -> dict[str, MikuSlot]:
        return self._slots

    def destroy_all(self):
        """Tear down all slots (e.g. on app quit)."""
        for sid in list(self._slots.keys()):
            self._destroy_slot(sid)

    # --- internal ---

    def _create_slot(self, session_id: str):
        """Spawn a new Miku for this session, offset so they don't stack."""
        if session_id in self._slots:
            return

        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        screen = app.primaryScreen().availableGeometry()

        # stagger position: each new slot shifts 60px left
        self._slot_counter += 1
        base_x = screen.width() - 148
        offset = (self._slot_counter - 1) * 60
        init_x = base_x - offset

        # wrap around if we go off-screen
        if init_x < 100:
            init_x = base_x - random.randint(0, 200)

        slot = MikuSlot(
            session_id=session_id,
            config=self._config,
            ax_threaded=self._ax_threaded,
            entry_action="stand",
            init_x=init_x,
            init_y=screen.height() - 148,
        )
        self._slots[session_id] = slot
        print(f"[claudemeji:conductor] created slot for session {session_id[:12]}... "
              f"({len(self._slots)} active)")

        # give the new slot current platforms immediately
        self._refresh_platforms()

    def _destroy_slot(self, session_id: str):
        """Tear down a session's Miku."""
        slot = self._slots.pop(session_id, None)
        if slot:
            slot.destroy()
            print(f"[claudemeji:conductor] destroyed slot for session {session_id[:12]}... "
                  f"({len(self._slots)} active)")

    def _refresh_platforms(self):
        """Shared platform query — push to all slots."""
        if not windows_available():
            platforms = []
        else:
            try:
                platforms = get_platform_tuples(own_pid=os.getpid())
                # filter: exclude parent process (don't toss our own terminal)
                platforms = [p for p in platforms if p[1] != self._parent_pid]
            except Exception:
                platforms = []

        for slot in self._slots.values():
            # per-slot screen filtering (in case slots are on different screens)
            screen = slot.physics._screen_rect()
            filtered = [p for p in platforms if p[0].intersects(screen)]
            slot.update_platforms(filtered)
