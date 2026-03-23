"""
config.py - loads global config + per-pack config from ~/.claudemeji/

Directory layout:
  ~/.claudemeji/
    config.toml                 # global: active_pack, physics overrides
    packs/
      shimemiku/
        config.toml             # pack-specific: sprite_pack path, actions, aliases
      pokemon/
        config.toml

Global config.toml:
  active_pack = "shimemiku"

  [physics]
  window_pull_distance = 40
  default_facing = "left"

Pack config.toml (same action format as before):
  [sprite_pack]
  path = "/path/to/img"

  [actions.stand]
  files = ["shime1.png"]
  ...
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

CONFIG_DIR = os.path.expanduser("~/.claudemeji")
GLOBAL_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")
PACKS_DIR = os.path.join(CONFIG_DIR, "packs")


@dataclass
class PackConfig:
    name: str           # pack directory name (e.g. "shimemiku")
    path: str           # resolved img directory path

    @property
    def img_dir_path(self) -> str:
        return os.path.expanduser(self.path)

    @property
    def config_path(self) -> str:
        """Path to this pack's config.toml."""
        return os.path.join(PACKS_DIR, self.name, "config.toml")


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
        files=d.get("files"),
        fps=d.get("fps", 8),
        loop=d.get("loop", True),
        intro_files=d.get("intro_files"),
        outro_files=d.get("outro_files"),
        walk_speed=d.get("walk_speed", 0.0),
        offset_y=d.get("offset_y", 0),
    )


def _parse_sub_dict(section: dict) -> dict[str, ActionDef]:
    """Parse a dict of name → sub-definition (postures, contexts, variants)."""
    return {name: ActionDef(**_base_fields(d)) for name, d in section.items()}


def _parse_action_def(adef: dict) -> ActionDef:
    """Parse a single action definition dict into an ActionDef, including variants."""
    return ActionDef(
        **_base_fields(adef),
        postures=_parse_sub_dict(adef.get("postures", {})),
        contexts=_parse_sub_dict(adef.get("contexts", {})),
        variants=list(_parse_sub_dict(adef.get("variants", {})).values()),
        min_restlessness=adef.get("min_restlessness", 0),
        idle_tier=adef.get("idle_tier", False),
    )


def _load_toml(path: str) -> dict:
    """Load a TOML file and return the parsed dict."""
    if tomllib is None:
        raise ImportError(
            "Python 3.11+ required for built-in tomllib, or: pip install tomli"
        )
    with open(path, "rb") as f:
        return tomllib.load(f)


def _parse_pack_data(pack_name: str, data: dict) -> tuple[PackConfig, dict[str, ActionDef], dict[str, str]]:
    """Parse pack-level config data (sprite_pack, actions, aliases)."""
    pack_data = data.get("sprite_pack", {})
    pack = PackConfig(
        name=pack_name,
        path=os.path.expanduser(pack_data.get("path", ".")),
    )

    actions: dict[str, ActionDef] = {}
    for name, adef in data.get("actions", {}).items():
        actions[name] = _parse_action_def(adef)

    if "sit_idle" not in actions:
        actions["sit_idle"] = ActionDef(files=["shime1.png"], fps=1, loop=True)

    aliases = data.get("action_aliases", {})
    return pack, actions, aliases


def _parse_physics(data: dict) -> PhysicsConfig:
    """Parse physics config from global config data."""
    physics_data = data.get("physics", {})
    return PhysicsConfig(
        window_pull_distance=physics_data.get("window_pull_distance", 0),
        default_facing=physics_data.get("default_facing", "left"),
    )


def available_packs() -> list[str]:
    """List installed pack names (directories under ~/.claudemeji/packs/)."""
    if not os.path.isdir(PACKS_DIR):
        return []
    return sorted(
        d for d in os.listdir(PACKS_DIR)
        if os.path.isfile(os.path.join(PACKS_DIR, d, "config.toml"))
    )


def load(path: str | None = None) -> Config:
    """Load config from the global config + active pack.

    If `path` is given (via CLAUDEMEJI_CONFIG env var), load that as a
    standalone pack config (useful for development / the animator).
    """
    if tomllib is None:
        raise ImportError(
            "Python 3.11+ required for built-in tomllib, or: pip install tomli"
        )

    # direct path override — treat as a standalone pack config (dev/animator mode)
    if path:
        data = _load_toml(path)
        pack, actions, aliases = _parse_pack_data("custom", data)
        physics = _parse_physics(data)
        return Config(pack=pack, actions=actions, aliases=aliases, physics=physics)

    # --- normal load: global config → active pack ---

    if not os.path.exists(GLOBAL_CONFIG_PATH):
        raise FileNotFoundError(
            f"No config found at {GLOBAL_CONFIG_PATH}. "
            "Run the installer or create ~/.claudemeji/config.toml with: active_pack = \"shimemiku\""
        )

    global_data = _load_toml(GLOBAL_CONFIG_PATH)
    active_pack = global_data.get("active_pack", "shimemiku")

    pack_config_path = os.path.join(PACKS_DIR, active_pack, "config.toml")
    if not os.path.exists(pack_config_path):
        raise FileNotFoundError(
            f"Pack '{active_pack}' not found at {pack_config_path}. "
            f"Available packs: {', '.join(available_packs()) or '(none)'}"
        )

    pack_data = _load_toml(pack_config_path)
    pack, actions, aliases = _parse_pack_data(active_pack, pack_data)

    # physics: pack can define defaults, global config overrides
    physics = _parse_physics(pack_data)
    global_physics = global_data.get("physics", {})
    if "window_pull_distance" in global_physics:
        physics.window_pull_distance = global_physics["window_pull_distance"]
    if "default_facing" in global_physics:
        physics.default_facing = global_physics["default_facing"]

    return Config(pack=pack, actions=actions, aliases=aliases, physics=physics)
