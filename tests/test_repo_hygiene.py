"""Guard: no game containers or extracted game assets are committed to Git.

Uses the toolkit's own detector on every tracked file.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from aniimo_pal.scanner.formats import detect

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_KINDS = {
    "unity_bundle", "unity_bundle_legacy", "unity_serialized", "unity_resource",
    "unity_package", "il2cpp_metadata", "pe_binary", "elf_binary",
    "model_fbx", "model_glb", "texture_dds", "texture_ktx", "texture_ktx2",
    "texture_astc", "texture_pvr", "audio_fsb5", "audio_fmod_bank",
    "audio_wwise_bank", "audio_wwise_pck", "video_usm", "unknown_high_entropy",
}
MAX_TRACKED_BYTES = 2 * 1024 * 1024


def _tracked_files():
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    return [REPO / p for p in out.decode().split("\0") if p]


def test_no_game_assets_tracked():
    offenders = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(65536)
        kind = detect(head, size, path.name).kind
        if kind in FORBIDDEN_KINDS or size > MAX_TRACKED_BYTES:
            offenders.append(f"{path.relative_to(REPO)} ({kind}, {size} bytes)")
    assert offenders == [], "binary/game assets must not be committed:\n" + "\n".join(offenders)
