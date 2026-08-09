from __future__ import annotations

from pathlib import Path

import yaml

_PERSONAS_DIR = Path(__file__).parent / "personas"


def load_axes() -> dict:
    with open(_PERSONAS_DIR / "axes.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_character_presets() -> dict:
    presets = {}
    characters_dir = _PERSONAS_DIR / "characters"
    for path in sorted(characters_dir.glob("*.yaml")):
        with open(path, "r", encoding="utf-8") as f:
            presets[path.stem] = yaml.safe_load(f)
    return presets


def _load_yaml(relative_path: str):
    with open(_PERSONAS_DIR / relative_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_persona_behavior_map() -> dict:
    return _load_yaml("moods/persona_behavior_map.yaml")


def load_player_tone_map() -> dict:
    return _load_yaml("player_tone.yaml")


def load_catchphrases() -> list:
    return _load_yaml("moods/catchphrases.yaml")


def load_relationship_tone_map() -> dict:
    return _load_yaml("moods/relationship_tone.yaml")
