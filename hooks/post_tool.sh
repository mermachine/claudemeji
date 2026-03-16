#!/bin/bash
# Claude Code PostToolUse hook - fires after each tool call
# exit_code reflects whether the tool succeeded

EVENTS_FILE="$HOME/.claudemeji/events.jsonl"
mkdir -p "$(dirname "$EVENTS_FILE")"

INPUT=$(cat)

TOOL=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')
# PostToolUse passes exit code in the JSON; 0 = success
EXIT_CODE=$(echo "$INPUT" | jq -r '.exit_code // 0')

echo "{\"event_type\":\"tool_end\",\"tool_name\":\"$TOOL\",\"exit_code\":$EXIT_CODE,\"ts\":$(date +%s)}" >> "$EVENTS_FILE"
