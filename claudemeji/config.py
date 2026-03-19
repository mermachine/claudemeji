"""
config.py - loads config.toml and resolves sprite pack + action mappings

Compound state config format:

  [actions.react_good]
  files = ["shime22.png", "shime18.png"]   # base (fallback)
  fps = 10
  loop = false

  [actions.react_good.postures.sitting]    # override when sitting
  files = ["shime28.png"]
  fps = 8
  loop = false

  [actions.drag]
  files = ["shime48.png"]                  # base drag (calm)

  [actions.drag.contexts.r2]               # drag at restlessness 2 (annoyed)
  files = ["shime7.png", "shime8.png"]
  fps = 6

  [actions.drag.contexts.r4]               # drag at restlessness 4 (furious)
  files = ["shime5.png", "shime6.png"]
  fps = 8
"""

from __future__ import annotations
import os
import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

from claudemeji.sprite import ActionDef

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.claudemeji/config.toml")


@dataclass
class PackConfig:
    path: str
    img_dir: str = "img"
    sheet: str = ""
    frame_width: int = 128
    frame_height: int = 128

    @property
    def img_dir_path(self) -> str:
        base = os.path.expanduser(self.path)
        if self.img_dir:
            return os.path.join(base, self.img_dir)
        return base

    @property
    def sheet_path(self) -> str:
        return os.path.join(os.path.expanduser(self.path), self.sheet)

    @property
    def is_file_based(self) -> bool:
        return not bool(self.sheet)


@dataclass
class PhysicsConfig:
    window_pull_distance: int = 0  # how far (px) sprite weight pulls windows down (0 = disabled)
    default_facing: str = "left"   # which direction sprites face natively ("left" or "right")


@dataclass
class Config:
    pack: PackConfig
    actions: dict[str, ActionDef] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    physics: PhysicsConfig = field(default_factory=PhysicsConfig)

    def resolve_action(self, name: str) -> str:
        """Return the canonical action name to use (handles aliases, fallback)."""
        if name in self.actions:
            return name
        if name in self.aliases:
            resolved = self.aliases[name]
            if resolved in self.actions:
                return resolved
        return "sit_idle"


def _base_fields(d: dict) -> dict:
    """Extract the ActionDef fields shared by both full actions and sub-variants."""
    return dict(
        frames=d.get("frames"),
        files=d.get("files"),
        fps=d.get("fps", 8),
        loop=d.get("loop", True),
        flip=d.get("flip", False),
        intro_files=d.get("intro_files"),
        outro_files=d.get("outro_files"),
        walk_speed=d.get("walk_speed", 0.0),
        offset_y=d.get("offset_y", 0),
    )


def _parse_sub_dict(section: dict) -> dict[str, ActionDef]:
    """Parse a dict of name → sub-definition (postures, contexts, previous, variants)."""
    return {name: ActionDef(**_base_fields(d)) for name, d in section.items()}


def _parse_action_def(adef: dict) -> ActionDef:
    """Parse a single action definition dict into an ActionDef, including variants."""
    return ActionDef(
        **_base_fields(adef),
        postures=_parse_sub_dict(adef.get("postures", {})),
        contexts=_parse_sub_dict(adef.get("contexts", {})),
        previous=_parse_sub_dict(adef.get("previous", {})),
        variants=list(_parse_sub_dict(adef.get("variants", {})).values()),
        min_restlessness=adef.get("min_restlessness", 0),
        idle_tier=adef.get("idle_tier", False),
    )


def load(path: str = DEFAULT_CONFIG_PATH) -> Config:
    if tomllib is None:
        raise ImportError(
            "Python 3.11+ required for built-in tomllib, or: pip install tomli"
        )

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No config found at {path}. "
            "Copy config.example.toml to ~/.claudemeji/config.toml to get started."
        )

    with open(path, "rb") as f:
        data = tomllib.load(f)

    pack_data = data.get("sprite_pack", {})
    pack = PackConfig(
        path=os.path.expanduser(pack_data.get("path", ".")),
        img_dir=pack_data.get("img_dir", ""),
        sheet=pack_data.get("sheet", ""),
        frame_width=pack_data.get("frame_width", 128),
        frame_height=pack_data.get("frame_height", 128),
    )

    actions: dict[str, ActionDef] = {}
    for name, adef in data.get("actions", {}).items():
        actions[name] = _parse_action_def(adef)

    if "sit_idle" not in actions:
        actions["sit_idle"] = ActionDef(files=["shime1.png"], fps=1, loop=True)

    aliases = data.get("action_aliases", {})

    physics_data = data.get("physics", {})
    physics = PhysicsConfig(
        window_pull_distance=physics_data.get("window_pull_distance", 0),
        default_facing=physics_data.get("default_facing", "left"),
    )

    return Config(pack=pack, actions=actions, aliases=aliases, physics=physics)
