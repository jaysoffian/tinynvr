"""Configuration loading and saving for NVR."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("NVR_CONFIG", "config.yaml"))


@dataclass
class StorageConfig:
    path: str = "./recordings"
    retention_days: int = 7
    segment_seconds: int = 300


@dataclass
class CameraConfig:
    url: str = ""
    enabled: bool = True


@dataclass
class Config:
    storage: StorageConfig = field(default_factory=StorageConfig)
    cameras: dict[str, CameraConfig] = field(default_factory=dict)


def _parse_camera(data: dict[str, Any]) -> CameraConfig:
    return CameraConfig(
        url=data.get("url", ""),
        enabled=data.get("enabled", True),
    )


def _parse_storage(data: dict[str, Any]) -> StorageConfig:
    return StorageConfig(
        path=data.get("path", "./recordings"),
        retention_days=data.get("retention_days", 7),
        segment_seconds=data.get("segment_seconds", 300),
    )


def load_config(path: Path | None = None) -> Config:
    """Load configuration from YAML file."""
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        logger.warning("Config file %s not found, using defaults", config_path)
        return Config()

    raw = yaml.safe_load(config_path.read_text())
    if not raw:
        return Config()

    storage = _parse_storage(raw.get("storage", {}))
    cameras = {
        name: _parse_camera(cam_data)
        for name, cam_data in raw.get("cameras", {}).items()
    }
    return Config(storage=storage, cameras=cameras)


def config_to_dict(config: Config) -> dict[str, Any]:
    """Convert config dataclass to a plain dict suitable for YAML/JSON."""
    return {
        "storage": {
            "path": config.storage.path,
            "retention_days": config.storage.retention_days,
            "segment_seconds": config.storage.segment_seconds,
        },
        "cameras": {
            name: {
                "url": cam.url,
                "enabled": cam.enabled,
            }
            for name, cam in config.cameras.items()
        },
    }


def save_config(config: Config, path: Path | None = None) -> None:
    """Write configuration back to YAML file."""
    config_path = path or CONFIG_PATH
    data = config_to_dict(config)
    config_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    logger.info("Config saved to %s", config_path)
