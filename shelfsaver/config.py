"""Application configuration."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    data_dir: Path
    port: int = 8642
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"


def _default_data_dir() -> Path:
    env = os.environ.get("SHELFSAVER_DATA")
    if env:
        return Path(env)
    program_data = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA")
    base = Path(program_data) if program_data else Path.home()
    return base / "ShelfSaver"


def load_config() -> Config:
    cfg = Config(data_dir=_default_data_dir())
    path = cfg.data_dir / "config.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            for key in ("ffmpeg", "ffprobe"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    setattr(cfg, key, value)
    return cfg
