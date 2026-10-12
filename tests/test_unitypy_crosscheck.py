"""Cross-check our header parser against UnityPy on the same synthetic bytes.

Skipped when UnityPy is not installed. This validates that the synthetic
fixtures are structurally real Unity containers (UnityPy can open them) and
that the toolkit reads the same header values UnityPy does.
"""

import pytest
import synthetic

from aniimo_pal.scanner.formats import detect

UnityPy = pytest.importorskip("UnityPy")


@pytest.mark.parametrize("engine", ["2021.3.15f1", "2019.4.40f1", "2022.3.10f1"])
def test_bundle_header_agrees_with_unitypy(engine):
    data = synthetic.unityfs_bundle(engine_version=engine)
    env = UnityPy.load(data)
    (bundle,) = env.files.values()
    ours = detect(data, len(data), "x.bundle").details
    assert ours["signature"] == bundle.signature
    assert ours["format_version"] == bundle.version
    assert ours["unity_version"] == bundle.version_engine
    assert ours["flags"] == int(bundle.dataflags)
    old_layout = type(bundle.dataflags).__name__ == "ArchiveFlagsOld"
    assert (ours["flag_layout"] == "pre-2020.3.34") is old_layout
    assert list(bundle.files) == ["CAB-synthetic.resS"]


@pytest.mark.parametrize("version", [17, 21, 22])
def test_serialized_header_agrees_with_unitypy(version):
    data = synthetic.serialized_file(version=version)
    env = UnityPy.load(data)
    (sf,) = env.files.values()
    ours = detect(data, len(data), "sharedassets0.assets")
    assert ours.kind == "unity_serialized"
    assert ours.details["format_version"] == sf.header.version
    assert ours.details["unity_version"] == sf.unity_version
    assert ours.details["data_offset"] == sf.header.data_offset
