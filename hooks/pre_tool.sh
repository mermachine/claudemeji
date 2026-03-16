#!/bin/bash
# Claude Code PreToolUse hook - fires before each tool call
# Receives JSON on stdin, writes an event line to claudemeji's event file

EVENTS_FILE="$HOME/.claudemeji/events.jsonl"
mkdir -p "$(dirname "$EVENTS_FILE")"

# read stdin once
INPUT=$(cat)

TOOL=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')

# write event as a JSON line
echo "{\"event_type\":\"tool_start\",\"tool_name\":\"$TOOL\",\"ts\":$(date +%s)}" >> "$EVENTS_FILE"
