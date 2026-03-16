# claudemeji

a desktop mascot that watches what claude code is doing and reacts with animations.
powered by PyQt6. works on macOS and Windows (linux untested but probably fine).

## how it works

1. **claude code hooks** write events to `~/.claudemeji/events.jsonl` as JSON lines
2. **claudemeji** watches that file and maps tool calls to animation states
3. your shimeji sprite pack plays the matching animation

## setup

### 1. install

```bash
pip install PyQt6
# python 3.10 or earlier also needs:
pip install tomli
```

### 2. configure a sprite pack

```bash
mkdir -p ~/.claudemeji
cp config.example.toml ~/.claudemeji/config.toml
# edit config.toml to point at your sprite pack and define frame ranges
```

### 3. install the hooks

add to your claude code settings (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "/path/to/claudemeji/hooks/pre_tool.sh"}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "/path/to/claudemeji/hooks/post_tool.sh"}]
      }
    ]
  }
}
```

### 4. run

```bash
python -m claudemeji.main
```

## action set

| action | trigger |
|---|---|
| `sit_idle` | between events, idle timeout |
| `think` | Agent/Task tool, processing |
| `read` | Read, Grep, Glob, WebSearch |
| `type` | Edit, Write |
| `run` | Bash |
| `wait` | long-running process |
| `react_good` | successful tool completion |
| `react_bad` | error, denied tool call |
| `walk_left` / `walk_right` | physics / movement |
| `fall` | physics |
| `climb` | physics (optional) |
| `drag` | mouse interaction |

## using existing shimeji packs

shimeji packs aren't required to have all our actions. use `[action_aliases]` in your
config to map our canonical names to whatever animation fits. any unmapped action
falls back to `sit_idle`.

## sprite pack format

we expect a single PNG spritesheet (vertical strip by default).
set `frame_width` and `frame_height` in the config, then define frame indices
per action. grids work too - set appropriate dimensions and the frame indices
will be read left-to-right, top-to-bottom.
