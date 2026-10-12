import tomllib
from dataclasses import asdict
from pathlib import Path

import pytest

from aniimo_pal.config import Config, load_config
from aniimo_pal.errors import ConfigError

REPO = Path(__file__).resolve().parents[1]


def _norm(d):
    return {k: (_norm(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


def test_default_toml_matches_builtin_defaults():
    with open(REPO / "config" / "default.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data == _norm(asdict(Config()))


def test_example_local_config_loads():
    cfg = load_config(REPO / "config" / "local.example.toml")
    assert cfg.scan.workers == 8


@pytest.mark.parametrize("toml,message", [
    ("[nope]\n", "unknown config section"),
    ("[scan]\nworkerz = 1\n", "unknown config key"),
    ("[scan]\nworkers = \"4\"\n", "must be an integer"),
    ("[scan]\nworkers = 0\n", "between 1 and 64"),
    ("[scan]\nfollow_symlinks = 1\n", "must be a boolean"),
    ("[scan]\nhash_algorithms = []\n", "at least one"),
    ("[scan]\nhash_algorithms = [\"sha256\", \"sha256\"]\n", "duplicates"),
    ("[scan]\nhash_algorithms = [\"shake_128\"]\n", "variable-length"),
    ("[scan]\nexclude = [1]\n", "list of strings"),
    ("[scan]\nhigh_entropy_threshold = 9\n", "(0, 8]"),
    ("[logging]\nlevel = \"LOUD\"\n", "logging.level"),
    ("scan = 3\n", "must be a table"),
    ("this is not toml", "invalid TOML"),
])
def test_invalid_configs(tmp_path, toml, message):
    p = tmp_path / "c.toml"
    p.write_text(toml)
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace("]", r"\]")):
        load_config(p)


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")


def test_partial_override_keeps_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[scan]\nworkers = 2\nhigh_entropy_threshold = 7\n")
    cfg = load_config(p)
    assert cfg.scan.workers == 2
    assert cfg.scan.high_entropy_threshold == 7.0
    assert cfg.scan.hash_algorithms == ("sha256",)
