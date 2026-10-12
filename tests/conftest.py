import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import synthetic  # noqa: E402

MOSSBUN_BONES = ["Root", "Root/Pelvis", "Root/Pelvis/Chest", "Root/Pelvis/Chest/Head",
                 "Root/Pelvis/Chest/Head/EarL", "Root/Pelvis/Chest/Head/EarR"]


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


@pytest.fixture
def creature_game(tmp_path):
    """A synthetic game with two creature bundles (requires UnityPy).

    * foxlet.bundle: Foxlet + Shiny + Prismana prefabs sharing one mesh; clips via AnimatorController.
    * mossbun.bundle: Mossbun, no controller; its clips are only linkable by bone-path hashes.
    * locked.bundle: encryption-flagged UnityFS header (must be skipped, never opened).
    """
    pytest.importorskip("UnityPy")
    import unity_builder as ub

    root = tmp_path / "game"
    (root / "StreamingAssets").mkdir(parents=True)
    fox, fox_ids = ub.creature_bundle(variants={"shiny": (240, 220, 60), "prismana": (150, 60, 220)})
    (root / "StreamingAssets" / "foxlet.bundle").write_bytes(fox)
    moss, moss_ids = ub.creature_bundle(name="Mossbun", cab="CAB-mossbun", with_controller=False,
                                        clips=("Idle", "Hop", "Ult"), bones=MOSSBUN_BONES)
    (root / "StreamingAssets" / "mossbun.bundle").write_bytes(moss)
    (root / "StreamingAssets" / "locked.bundle").write_bytes(synthetic.unityfs_bundle(extra_flags=0x400))
    return root, {"foxlet": fox_ids, "mossbun": moss_ids}


@pytest.fixture
def pipeline(creature_game, tmp_path):
    """Scan + inventory + catalog for ``creature_game``."""
    from aniimo_pal.catalog import build_catalog
    from aniimo_pal.config import ScanConfig
    from aniimo_pal.inventory import build_inventory
    from aniimo_pal.scanner import scan

    root, ids = creature_game
    manifest = scan(root, ScanConfig())
    objects = build_inventory(manifest, root)
    catalog = build_catalog(objects)
    return {"root": root, "ids": ids, "manifest": manifest, "objects": objects, "catalog": catalog,
            "workspace": tmp_path / "ws"}
