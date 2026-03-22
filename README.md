# claudemeji

a desktop mascot that reacts to claude code in real time. she walks on your windows, climbs walls, chases your cursor, and throws your windows when you ignore her.

macOS only. powered by PyQt6 + claude code hooks.

## install

```bash
curl -fsSL https://raw.githubusercontent.com/mermachine/claudemeji/main/install.sh | bash
```

this clones the repo, installs dependencies, wires up claude code hooks, and configures the bundled shimemiku sprite pack. grant Accessibility permission (System Settings > Privacy & Security > Accessibility) for window interactions.

## how it works

claude code hooks write per-session events to `~/.claudemeji/events/`. the **conductor** watches that directory and spawns one miku per active session — each with independent physics, animation, and restlessness. sub-mikus spawn in-process when claude uses Agent/Task tools.

## what she does

| | |
|---|---|
| **reacts to tools** | different animations for bash, read, write, think, plan |
| **walks on windows** | treats your window title bars as platforms |
| **climbs walls** | screen edges and window sides, with ceiling crawling |
| **gets restless** | escalates from calm → fidgety → climby → grabby → feral |
| **chases your cursor** | at restlessness 2+, she starts following you |
| **pushes/peeks/throws windows** | at high restlessness, she interacts with your windows |
| **carries windows** | jumps to a window corner and walks off with it |
| **reacts to drag** | calm when grabbed at low restlessness, angry when feral |
| **z-orders correctly** | goes behind windows above the one she's standing on |

## running manually

```bash
# conductor mode (default) — manages all sessions
/usr/bin/python3 -u -m claudemeji.main

# single session
/usr/bin/python3 -u -m claudemeji.main --session SESSION_ID

# solo mode (no events, just wanders)
/usr/bin/python3 -u -m claudemeji.main --solo
```

## configuring animations

```bash
# GUI editor for sprite pack config
python3 -m claudemeji.animator
```

config lives at `~/.claudemeji/config.toml`. sprite packs are individual PNGs referenced by filename. use `[action_aliases]` to map action names if your pack doesn't have all animations — unmapped actions fall back to `sit_idle`.

## sprite pack

ships with **shimemiku** (Hatsune Miku) by [canarypop](https://kilakila.jp/shimeji/). 61 sprites covering standing, walking, running, sitting, climbing, falling, dragging, and all the window interaction poses.
