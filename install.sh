#!/bin/bash
# claudemeji installer
# clones the repo, installs dependencies, sets up hooks, and launches the conductor
set -euo pipefail

REPO_URL="https://github.com/mermachine/claudemeji.git"
INSTALL_DIR="$HOME/.local/share/claudemeji"
CONFIG_DIR="$HOME/.claudemeji"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
HOOK_SCRIPT="$INSTALL_DIR/hooks/claudemeji-hook.sh"

# --- colors ---
bold='\033[1m'
dim='\033[2m'
green='\033[32m'
yellow='\033[33m'
red='\033[31m'
reset='\033[0m'

info()  { echo -e "${bold}${green}>>>${reset} $*"; }
warn()  { echo -e "${bold}${yellow}>>>${reset} $*"; }
error() { echo -e "${bold}${red}>>>${reset} $*"; exit 1; }

# --- preflight ---

info "claudemeji installer"
echo ""

# macOS check
if [[ "$(uname)" != "Darwin" ]]; then
    error "claudemeji is macOS-only (needs Quartz, Accessibility API, PyQt6)"
fi

# python check — need system python or one with PyQt6 support
PYTHON=""
for candidate in /usr/bin/python3 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
[[ -z "$PYTHON" ]] && error "python3 not found"

# jq check (needed by hook script)
if ! command -v jq &>/dev/null; then
    warn "jq not found — installing via homebrew"
    if command -v brew &>/dev/null; then
        brew install jq
    else
        error "jq is required but not installed. Install it with: brew install jq"
    fi
fi

# claude code check
if [[ ! -d "$HOME/.claude" ]]; then
    warn "~/.claude not found — is Claude Code installed?"
    echo "  claudemeji needs Claude Code hooks to react to tool calls."
    echo "  Install Claude Code first, then re-run this script."
    exit 1
fi

# --- clone or update ---

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "updating existing install at $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
else
    info "cloning claudemeji to $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# --- python dependencies ---

info "installing python dependencies"
"$PYTHON" -m pip install --user --quiet \
    PyQt6 \
    pyobjc-framework-Quartz \
    pyobjc-framework-ApplicationServices \
    2>&1 | grep -v "already satisfied" || true

# tomli only needed for python < 3.11
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")
if [[ "$PY_MINOR" -lt 11 ]]; then
    "$PYTHON" -m pip install --user --quiet tomli 2>&1 | grep -v "already satisfied" || true
fi

# --- config ---

mkdir -p "$CONFIG_DIR/events" "$CONFIG_DIR/pids" "$CONFIG_DIR/packs"

# --- pack config ---

PACK_DIR="$CONFIG_DIR/packs/shimemiku"
if [[ ! -f "$PACK_DIR/config.toml" ]]; then
    info "installing shimemiku pack config"
    mkdir -p "$PACK_DIR"
    # copy pack config, rewriting the sprite path to the install location
    sed "s|path = .*|path = \"$INSTALL_DIR/assets/shimemiku/shimemiku/img\"|" \
        "$INSTALL_DIR/assets/shimemiku/config.toml" > "$PACK_DIR/config.toml"
else
    info "shimemiku pack config already exists"
fi

# --- global config ---

if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    info "creating global config at $CONFIG_DIR/config.toml"
    cat > "$CONFIG_DIR/config.toml" <<TOML
# claudemeji global config
# switch packs by changing active_pack to any directory name under ~/.claudemeji/packs/
active_pack = "shimemiku"

[physics]
window_pull_distance = 40
# default_facing = "left"
TOML
else
    info "global config already exists at $CONFIG_DIR/config.toml"
fi

# --- hooks ---

chmod +x "$HOOK_SCRIPT"

# install hooks into claude settings.json
info "installing Claude Code hooks"

if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
    # create fresh settings with just our hooks
    cat > "$CLAUDE_SETTINGS" <<JSON
{
  "hooks": {}
}
JSON
fi

# use python to merge hooks into existing settings (jq can't handle this cleanly)
"$PYTHON" -c "
import json, sys

settings_path = '$CLAUDE_SETTINGS'
hook_script = '$HOOK_SCRIPT'

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault('hooks', {})

# hook entries we need
entries = {
    'PreToolUse':      {'matcher': '*'},
    'PostToolUse':     {'matcher': '*'},
    'Stop':            {},
    'SessionStart':    {},
    'SubagentStop':    {},
    'Notification':    {},
    'UserPromptSubmit': {},
}

# for SessionEnd, the hook event env var is 'SessionEnd' not 'Stop'
session_end_entry = {}

for event, extra in entries.items():
    hook_cmd = f'CLAUDE_HOOK_EVENT={event} {hook_script}'
    hook_obj = {'type': 'command', 'command': hook_cmd, 'timeout': 5}

    if event not in hooks:
        hooks[event] = []

    # hooks[event] can be a list of matcher groups or a list of hook objects
    event_hooks = hooks[event]

    # check if our hook is already installed (in any nesting level)
    already = False
    for item in event_hooks:
        if isinstance(item, dict):
            # could be a matcher group with 'hooks' list, or a direct hook
            sub_hooks = item.get('hooks', [item])
            for h in sub_hooks:
                if isinstance(h, dict) and 'claudemeji-hook.sh' in h.get('command', ''):
                    already = True
                    break
        if already:
            break

    if not already:
        if extra.get('matcher'):
            # needs a matcher group
            existing_group = None
            for item in event_hooks:
                if isinstance(item, dict) and item.get('matcher') == extra['matcher']:
                    existing_group = item
                    break
            if existing_group:
                existing_group.setdefault('hooks', []).append(hook_obj)
            else:
                event_hooks.append({'matcher': extra['matcher'], 'hooks': [hook_obj]})
        else:
            # simple hook list (or matcher-less group)
            # find existing group without matcher, or create one
            existing_group = None
            for item in event_hooks:
                if isinstance(item, dict) and 'hooks' in item and 'matcher' not in item:
                    existing_group = item
                    break
            if existing_group:
                existing_group['hooks'].append(hook_obj)
            else:
                event_hooks.append({'hooks': [hook_obj]})

# add SessionEnd separately (not in the loop since the env var is different)
se_cmd = f'CLAUDE_HOOK_EVENT=SessionEnd {hook_script}'
se_obj = {'type': 'command', 'command': se_cmd, 'timeout': 5}
if 'SessionEnd' not in hooks:
    hooks['SessionEnd'] = []
se_hooks = hooks['SessionEnd']
se_already = any(
    'claudemeji-hook.sh' in h.get('command', '')
    for item in se_hooks
    for h in (item.get('hooks', [item]) if isinstance(item, dict) else [])
)
if not se_already:
    existing_group = None
    for item in se_hooks:
        if isinstance(item, dict) and 'hooks' in item and 'matcher' not in item:
            existing_group = item
            break
    if existing_group:
        existing_group['hooks'].append(se_obj)
    else:
        se_hooks.append({'hooks': [se_obj]})

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  hooks installed successfully')
"

# --- launch script ---

LAUNCHER="$CONFIG_DIR/launch.sh"
if [[ ! -f "$LAUNCHER" ]]; then
    info "creating launch script at $LAUNCHER"
    cat > "$LAUNCHER" <<LAUNCH
#!/bin/bash
# auto-launched by claudemeji-hook.sh on SessionStart
# starts the conductor if it isn't already running
cd "$INSTALL_DIR"
exec $PYTHON -u -m claudemeji.main &
LAUNCH
    chmod +x "$LAUNCHER"
else
    info "launch script already exists at $LAUNCHER"
fi

# --- done ---

echo ""
info "installation complete!"
echo ""
echo -e "  ${bold}to start claudemeji:${reset}"
echo "    cd $INSTALL_DIR && $PYTHON -u -m claudemeji.main"
echo ""
echo -e "  ${bold}to configure animations:${reset}"
echo "    cd $INSTALL_DIR && $PYTHON -m claudemeji.animator"
echo ""
echo -e "  ${bold}note:${reset} claudemeji will auto-launch on new Claude Code sessions."
echo "  for window interactions (push, throw, carry), grant Accessibility"
echo "  permission in: System Settings > Privacy & Security > Accessibility"
echo ""

# offer to start now
read -p "start claudemeji now? [Y/n] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    info "launching conductor..."
    cd "$INSTALL_DIR"
    nohup "$PYTHON" -u -m claudemeji.main > /tmp/claudemeji.log 2>&1 &
    echo -e "  running (pid $!, log at /tmp/claudemeji.log)"
fi
