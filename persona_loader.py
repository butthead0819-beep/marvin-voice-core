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
