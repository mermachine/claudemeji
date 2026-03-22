"""
state.py - state machine mapping claude code events to animation states

Claude Code hook JSON has a 'tool_name' field for tool calls.
We map those (plus synthetic events like 'idle', 'error') to our action set.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional


# Canonical action names - these are what sprite packs must provide (or map to)
# Direction is NOT part of the action name — facing is handled by SpritePlayer.set_facing()
ACTIONS = [
    # movement (physics)
    "stand",          # neutral standing pose (true default idle)
    "walk",           # horizontal locomotion (flipped programmatically for direction)
    "run",            # fast walk (restless locomotion variant)
    "sprint",         # full dash (high restlessness)
    "crawl",          # deliberate belly crawl movement
    "trip",           # stumble/pratfall during run (one-shot, attention-seeking)
    "fall",
    "jump",           # impulse-based jump toward a target
    "climb",          # wall climbing (flipped for right wall)
    "ceiling",        # ceiling crawl (flipped for direction)
    "hang",           # hanging/dangling on a wall (stationary)
    "hang_ceiling",   # hanging from ceiling (stationary)
    "sit_idle",
    # claude-specific
    "plan",         # planning mode (EnterPlanMode tool)
    "think",        # between tool calls / processing
    "read",         # Read, Grep, Glob tool
    "type",         # Edit, Write tool (also general text output)
    "bash",         # Bash tool executing
    "wait",         # long-running process, nervous energy
    "react_good",   # task success / user approved
    "react_bad",    # error / denied tool call / stop hook
    "drag",         # classic shimeji drag (mouse interaction)
    "subagent",     # parent's split animation when spawning a subagent (Agent/Task tool)
    "spawned",      # subagent entrance animation (jump up from parent, fall down)
    # window interactions (restlessness-gated)
    "window_push",       # pushing/dragging a window
    "window_throw",      # throwing a window (arc + minimize)
    "window_carry",      # walking with a grabbed window
    "window_carry_perch",# perched on window corner before grabbing
    "window_carry_cheer",# celebration after throwing a carried window
]

# Map claude code tool names → our action names
TOOL_TO_ACTION = {
    "Read":       "read",
    "Grep":       "read",
    "Glob":       "read",
    "WebSearch":  "read",
    "WebFetch":   "read",
    "Edit":       "type",
    "Write":      "type",
    "NotebookEdit": "type",
    "Bash":       "bash",
    "Agent":      "subagent",
    "Task":       "subagent",
    "EnterPlanMode": "plan",
    "ExitPlanMode":  "think",
    "_wait":      "wait",   # synthetic: tool taking too long / permission request
    # fallback for anything unrecognized
    "_default":   "think",
}

# Hook event types → actions
EVENT_TO_ACTION = {
    "session_start":   "react_good",
    "session_stop":    "sit_idle",
    "tool_start":      None,  # resolved via TOOL_TO_ACTION
    "tool_end":        None,  # resolved via TOOL_TO_ACTION
    "tool_error":      "react_bad",
    "tool_denied":     "react_bad",
    "subagent_stop":   "react_good",  # subagent returned!
    "idle":            "sit_idle",
    "notification":    "think",
}


@dataclass
class MascotState:
    action: str = "sit_idle"
    tool_name: str | None = None
    # could hold more context later (error message, file being read, etc.)


class StateMachine:
    def __init__(self, on_change: Callable[[MascotState], None]):
        self.state = MascotState()
        self._on_change = on_change

    def handle_event(self, event: dict):
        """
        Process a raw event dict from the hook watcher.
        Expected keys: 'event_type', optionally 'tool_name', 'exit_code', etc.
        """
        event_type = event.get("event_type", "")
        tool_name = event.get("tool_name")

        action = EVENT_TO_ACTION.get(event_type)

        if action is None and tool_name:
            action = TOOL_TO_ACTION.get(tool_name, TOOL_TO_ACTION["_default"])

        exit_code = event.get("exit_code")
        if exit_code is not None and exit_code != 0:
            action = "react_bad"

        if action and action != self.state.action:
            self.state = MascotState(action=action, tool_name=tool_name)
            self._on_change(self.state)

    def set_idle(self):
        if self.state.action != "sit_idle":
            self.state = MascotState(action="sit_idle")
            self._on_change(self.state)
