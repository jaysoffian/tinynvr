"""Configuration loading and saving for TinyNVR."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("TINYNVR_CONFIG", "config.yaml"))

_yaml = YAML()
_yaml.preserve_quotes = True

_SEED_CONFIG = """\
storage:
  path: /recordings
  retention_days: 7
  segment_minutes: 1    # 1-60

cameras:
  # example:
  #   url: rtsp://your-camera:554/stream
  #   enabled: true
"""


@dataclass
class StorageConfig:
    path: str = "./recordings"
    retention_days: int = 7
    segment_minutes: int = 1


@dataclass
class CameraConfig:
    url: str = ""
    enabled: bool = True


@dataclass
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    _raw: Any = field(default=None, repr=False)
    _path: Path | None = field(default=None, repr=False)


def _parse_camera(data: dict[str, Any]) -> CameraConfig:
    return CameraConfig(
        url=data.get("url", ""),
        enabled=data.get("enabled", True),
    )


def _parse_storage(data: dict[str, Any]) -> StorageConfig:
    segment_minutes = max(1, min(60, data.get("segment_minutes", 1)))
    raw_value = data.get("segment_minutes", 1)
    if raw_value != segment_minutes:
        logger.warning(
            "segment_minutes=%d out of range 1-60, clamped to %d",
            raw_value,
            segment_minutes,
        )
    return StorageConfig(
        path=data.get("path", "./recordings"),
        retention_days=data.get("retention_days", 7),
        segment_minutes=segment_minutes,
    )


def load_config(path: Path | None = None) -> Config:
    """Load configuration from YAML file."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        logger.warning("Config file %s not found, seeding default config", config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_SEED_CONFIG)
        return load_config(config_path)

    raw = _yaml.load(config_path)
    if not raw:
        return Config()

    storage = _parse_storage(raw.get("storage", {}))
    cameras = {
        name: _parse_camera(cam_data)
        for name, cam_data in raw.get("cameras", {}).items()
    }
    return Config(storage=storage, cameras=cameras, _raw=raw, _path=config_path)


def config_to_dict(config: Config) -> dict[str, Any]:
    """Convert config dataclass to a plain dict suitable for JSON."""
    return {
        "storage": {
            "path": config.storage.path,
            "retention_days": config.storage.retention_days,
            "segment_minutes": config.storage.segment_minutes,
        },
        "cameras": {
            name: {
                "url": cam.url,
                "enabled": cam.enabled,
            }
            for name, cam in config.cameras.items()
        },
    }


def save_config(config: Config) -> None:
    """Write configuration back to YAML, preserving comments and formatting."""
    config_path = config._path or CONFIG_PATH
    raw = config._raw

    if raw is not None:
        # Update the raw ruamel structure in-place to preserve comments
        cameras_raw = raw.get("cameras", {})
        for name, cam in config.cameras.items():
            if name in cameras_raw:
                cameras_raw[name]["enabled"] = cam.enabled
    else:
        raw = config_to_dict(config)

    with config_path.open("w") as f:
        _yaml.dump(raw, f)
    logger.info("Config saved to %s", config_path)
