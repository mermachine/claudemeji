#!/bin/bash
# claudemeji-hook.sh - handles all claude code hook event types
# receives JSON on stdin, writes one event line to ~/.claudemeji/events/SESSION_ID.jsonl

EVENTS_DIR="$HOME/.claudemeji/events"
mkdir -p "$EVENTS_DIR"

INPUT=$(cat)
TS=$(date +%s)

# session identity + context from payload
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "default"')
EVENTS_FILE="$EVENTS_DIR/$SESSION_ID.jsonl"

# event type: from env var (set by claude code before running hook)
EVENT="${CLAUDE_HOOK_EVENT:-unknown}"

case "$EVENT" in
  PreToolUse)
    echo "$INPUT" | jq -c "{event_type: \"tool_start\", tool_name: .tool_name, tool_use_id: .tool_use_id, session_id: .session_id, cwd: .cwd, ts: $TS}" >> "$EVENTS_FILE"
    ;;
  PostToolUse)
    echo "$INPUT" | jq -c "{event_type: \"tool_end\", tool_name: .tool_name, tool_use_id: .tool_use_id, exit_code: (.exit_code // 0), session_id: .session_id, ts: $TS}" >> "$EVENTS_FILE"
    ;;
  SessionStart)
    echo "$INPUT" | jq -c "{event_type: \"session_start\", session_id: .session_id, cwd: .cwd, ts: $TS}" >> "$EVENTS_FILE"
    # auto-launch: if ~/.claudemeji/launch.sh exists and is executable, run it with the session id
    LAUNCHER="$HOME/.claudemeji/launch.sh"
    if [ -x "$LAUNCHER" ]; then
      "$LAUNCHER" "$SESSION_ID" &
    fi
    ;;
  Stop)
    # Stop fires on every response turn — NOT session end.
    # In conductor mode we don't want this to kill miku.
    # Just record it as a pause, not a session_stop.
    echo "$INPUT" | jq -c "{event_type: \"stop\", session_id: .session_id, ts: $TS}" >> "$EVENTS_FILE"
    ;;
  SessionEnd)
    echo "$INPUT" | jq -c "{event_type: \"session_stop\", session_id: .session_id, ts: $TS}" >> "$EVENTS_FILE"
    # kill claudemeji for this session if a pid file exists
    PID_FILE="$HOME/.claudemeji/pids/$SESSION_ID.pid"
    if [ -f "$PID_FILE" ]; then
      kill "$(cat "$PID_FILE")" 2>/dev/null
      rm -f "$PID_FILE"
    fi
    ;;
  SubagentStop)
    echo "$INPUT" | jq -c "{event_type: \"subagent_stop\", session_id: .session_id, ts: $TS}" >> "$EVENTS_FILE"
    ;;
  Notification)
    echo "$INPUT" | jq -c "{event_type: \"notification\", session_id: .session_id, ts: $TS, raw: .}" >> "$EVENTS_FILE"
    ;;
  UserPromptSubmit)
    echo "$INPUT" | jq -c "{event_type: \"notification\", session_id: .session_id, ts: $TS}" >> "$EVENTS_FILE"
    ;;
esac
