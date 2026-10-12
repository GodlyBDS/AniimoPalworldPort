import struct

import pytest
import synthetic

from aniimo_pal.scanner import unity_headers as uh


@pytest.mark.parametrize("text,expected", [
    ("2021.3.15f1", (2021, 3, 15)),
    ("2021.3.15f1c1", (2021, 3, 15)),  # Unity China builds
    ("5.6.7", (5, 6, 7)),
    ("6000.0.23f1", (6000, 0, 23)),
    ("0.0.0", None),
    ("", None),
    ("garbage", None),
])
def test_parse_unity_version(text, expected):
    assert uh.parse_unity_version(text) == expected


@pytest.mark.parametrize("version,old", [
    ((2019, 4, 40), True),
    ((2020, 3, 33), True),
    ((2020, 3, 34), False),
    ((2021, 3, 1), True),
    ((2021, 3, 2), False),
    ((2022, 1, 0), True),
    ((2022, 1, 1), False),
    ((2023, 2, 0), False),
    ((6000, 0, 0), False),
])
def test_archive_flag_layout_thresholds(version, old):
    assert uh.uses_old_archive_flags(version) is old


def test_unityfs_header_fields():
    data = synthetic.unityfs_bundle()
    res = uh.parse_bundle_header(data, len(data))
    d = res.details
    assert d["signature"] == "UnityFS"
    assert d["format_version"] == 8
    assert d["player_version"] == "5.x.x"
    assert d["unity_version"] == "2021.3.15f1"
    assert d["declared_size"] == len(data)
    assert d["blocks_info_compression"] == "none"
    assert d["blocks_and_directory_combined"] is True
    assert d["flag_layout"] == "post-2020.3.34"
    assert d["encryption_flag"] is False
    assert res.warnings == []
    assert res.confidence == "high"


@pytest.mark.parametrize("engine,bit,encrypted", [
    ("2021.3.15f1", 0x400, True),    # new layout, old-style encryption bit
    ("2021.3.15f1", 0x1000, True),   # new layout, newer encryption bit
    ("2021.3.15f1", 0x200, False),   # new layout: 0x200 is block-info padding
    ("2019.4.40f1", 0x200, True),    # old layout: 0x200 is encryption
])
def test_encryption_flag_by_engine_version(engine, bit, encrypted):
    data = synthetic.unityfs_bundle(engine_version=engine, extra_flags=bit)
    res = uh.parse_bundle_header(data, len(data))
    assert res.details["encryption_flag"] is encrypted
    assert any("encrypted" in w for w in res.warnings) is encrypted


def test_blank_engine_version_is_ambiguous():
    data = synthetic.unityfs_bundle(engine_version="0.0.0", extra_flags=0x200)
    res = uh.parse_bundle_header(data, len(data))
    assert res.details["flag_layout"] == "ambiguous"
    assert res.details["encryption_flag"] is None
    assert any("ambiguous" in w for w in res.warnings)


def test_size_mismatch_warns():
    data = synthetic.unityfs_bundle(size_override=999999)
    res = uh.parse_bundle_header(data, len(data))
    assert res.confidence == "medium"
    assert any("declared bundle size" in w for w in res.warnings)


def test_prefixed_bundle_found_and_flagged():
    data = b"\x00" * 32 + synthetic.unityfs_bundle()
    sig, offset = uh.find_bundle_signature(data)
    assert (sig, offset) == ("UnityFS", 32)
    res = uh.parse_bundle_header(data, len(data), offset)
    assert res.details["signature_offset"] == 32
    assert res.details["declared_size"] == len(data) - 32
    assert any("prefix" in w for w in res.warnings)


def test_legacy_bundle():
    data = synthetic.legacy_bundle("UnityRaw")
    res = uh.parse_bundle_header(data, len(data))
    assert res.details["signature"] == "UnityRaw"
    assert res.details["legacy_format"] is True


def test_truncated_bundle_header():
    res = uh.parse_bundle_header(b"UnityFS\x00\x00\x00", 10)
    assert res.confidence == "low"


@pytest.mark.parametrize("version", [9, 17, 21, 22, 23])
def test_serialized_header(version):
    data = synthetic.serialized_file(version=version)
    res = uh.parse_serialized_header(data, len(data))
    assert res is not None
    assert res.details["format_version"] == version
    assert res.details["unity_version"] == "2021.3.15f1"
    assert res.details["endianness"] == "little"


def test_serialized_rejects_wrong_size():
    data = synthetic.serialized_file(version=22)
    assert uh.parse_serialized_header(data, len(data) + 1) is None


@pytest.mark.parametrize("blob", [
    b"",
    b"\x00" * 64,
    struct.pack(">IIII", 0, 64, 999, 0) + bytes(48),   # implausible version
    struct.pack(">IIII", 0, 64, 17, 0) + b"\x05\x00\x00\x00" + bytes(44),  # bad endian byte
])
def test_serialized_rejects_non_unity(blob):
    assert uh.parse_serialized_header(blob, len(blob)) is None
