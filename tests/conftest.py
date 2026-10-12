import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import synthetic  # noqa: E402


@pytest.fixture
def game_tree(tmp_path):
    root = tmp_path / "game"
    root.mkdir()
    files = synthetic.build_game_tree(root)
    return root, files


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return d
