"""Configuration model and TOML loader.

The dataclass defaults below are the single source of truth; ``config/default.toml``
documents them and a unit test keeps the two in sync. User files only need to
contain the keys they override. Unknown sections or keys are rejected so typos
surface immediately instead of being silently ignored.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from aniimo_pal.errors import ConfigError

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class ScanConfig:
    hash_algorithms: tuple[str, ...] = ("sha256",)
    follow_symlinks: bool = False
    include_hidden: bool = False
    exclude: tuple[str, ...] = ()
    workers: int = 4
    probe_bytes: int = 65536
    high_entropy_threshold: float = 7.5
    record_mtime: bool = True

    def validate(self) -> None:
        if not self.hash_algorithms:
            raise ConfigError("scan.hash_algorithms must list at least one algorithm")
        for algo in self.hash_algorithms:
            if algo not in hashlib.algorithms_guaranteed:
                raise ConfigError(
                    f"scan.hash_algorithms: unsupported algorithm {algo!r}; "
                    f"choose from {sorted(hashlib.algorithms_guaranteed)}"
                )
            if algo.startswith("shake_"):
                raise ConfigError(f"scan.hash_algorithms: variable-length {algo!r} is not supported")
        if len(set(self.hash_algorithms)) != len(self.hash_algorithms):
            raise ConfigError("scan.hash_algorithms contains duplicates")
        if not 1 <= self.workers <= 64:
            raise ConfigError("scan.workers must be between 1 and 64")
        if not 64 <= self.probe_bytes <= 16 * 1024 * 1024:
            raise ConfigError("scan.probe_bytes must be between 64 and 16 MiB")
        if not 0.0 < self.high_entropy_threshold <= 8.0:
            raise ConfigError("scan.high_entropy_threshold must be in (0, 8]")


@dataclass(frozen=True)
class ManifestConfig:
    store_absolute_root: bool = False
    indent: int = 2

    def validate(self) -> None:
        if not 0 <= self.indent <= 8:
            raise ConfigError("manifest.indent must be between 0 and 8")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = ""

    def validate(self) -> None:
        if self.level.upper() not in LOG_LEVELS:
            raise ConfigError(f"logging.level must be one of {LOG_LEVELS}")


@dataclass(frozen=True)
class ToolsConfig:
    assetripper: str = ""
    blender: str = ""
    unreal_editor: str = ""

    def validate(self) -> None:
        pass


@dataclass(frozen=True)
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    manifest: ManifestConfig = field(default_factory=ManifestConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)

    def validate(self) -> "Config":
        for f in fields(self):
            getattr(self, f.name).validate()
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    """Coerce a TOML value to the type of the dataclass default, or raise."""
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be a boolean")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string")
        return value
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} must be a list of strings")
        return tuple(value)
    raise ConfigError(f"{where}: unsupported type")  # pragma: no cover


def merge(base: Config, data: dict[str, Any]) -> Config:
    """Return ``base`` with values from a parsed TOML mapping applied."""
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a table")
    updates: dict[str, Any] = {}
    section_names = {f.name for f in fields(base)}
    for section, values in data.items():
        if section not in section_names:
            raise ConfigError(f"unknown config section [{section}]")
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        current = getattr(base, section)
        known = {f.name: getattr(current, f.name) for f in fields(current)}
        changes = {}
        for key, value in values.items():
            if key not in known:
                raise ConfigError(f"unknown config key {section}.{key}")
            changes[key] = _coerce(section, key, value, known[key])
        updates[section] = replace(current, **changes)
    return replace(base, **updates)


def load_config(path: str | Path | None = None) -> Config:
    """Load built-in defaults, optionally overridden by a TOML file."""
    config = Config()
    if path is not None:
        p = Path(path)
        try:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {p}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
        config = merge(config, data)
    return config.validate()
