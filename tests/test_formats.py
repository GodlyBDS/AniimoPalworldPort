import pytest
import synthetic

from aniimo_pal.scanner.formats import FORMATS, detect, shannon_entropy


def _detect(data, name="file.bin"):
    return detect(data[:65536], len(data), name)


@pytest.mark.parametrize("data,name,kind", [
    (synthetic.unityfs_bundle(), "data.unity3d", "unity_bundle"),
    (synthetic.legacy_bundle(), "old.unity3d", "unity_bundle_legacy"),
    (synthetic.serialized_file(version=22), "sharedassets0.assets", "unity_serialized"),
    (synthetic.serialized_file(version=17), "level0", "unity_serialized"),
    (bytes(range(256)), "sharedassets0.assets.resS", "unity_resource"),
    (bytes(range(256)), "audio.resource", "unity_resource"),
    (synthetic.UNITY_PREFAB_YAML.encode(), "Creature.prefab", "unity_yaml"),
    (synthetic.UNITY_META.encode(), "Creature.prefab.meta", "unity_meta"),
    (b"\x1f\x8b\x08\x00" + bytes(32), "pack.unitypackage", "unity_package"),
    (b"\x1f\x8b\x08\x00" + bytes(32), "x.gz", "archive_gzip"),
    (synthetic.il2cpp_metadata(), "global-metadata.dat", "il2cpp_metadata"),
    (b"MZ" + bytes(62), "GameAssembly.dll", "pe_binary"),
    (b"\x7fELF" + bytes(60), "libunity.so", "elf_binary"),
    (synthetic.fbx_binary_header(), "a.fbx", "model_fbx"),
    (b"; FBX 7.4.0 project file\n", "a.fbx", "model_fbx"),
    (synthetic.glb(), "a.glb", "model_glb"),
    (b'{"asset": {"version": "2.0"}, "meshes": []}', "a.gltf", "model_gltf"),
    (b"o Cube\nv 0 0 0\nv 1 0 0\nf 1 2 1\n", "a.obj", "model_obj"),
    (synthetic.png(), "a.png", "image_png"),
    (b"\xff\xd8\xff\xe0" + bytes(32), "a.jpg", "image_jpeg"),
    (bytes(18) + bytes(range(200)), "a.tga", "image_tga"),
    (b"\x76\x2f\x31\x01" + bytes(32), "a.exr", "image_exr"),
    (b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n", "a.hdr", "image_hdr"),
    (synthetic.dds(), "a.dds", "texture_dds"),
    (b"\xabKTX 11\xbb\r\n\x1a\n\x01\x02\x03\x04" + bytes(64), "a.ktx", "texture_ktx"),
    (synthetic.ktx2(), "a.ktx2", "texture_ktx2"),
    (b"\x13\xab\xa1\x5c\x04\x04\x01\x40\x00\x00\x20\x00\x00\x01\x00\x00" + bytes(16), "a.astc", "texture_astc"),
    (b"PVR\x03" + bytes(60), "a.pvr", "texture_pvr"),
    (b"FSB5" + bytes(60), "a.fsb", "audio_fsb5"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(32), "a.wav", "audio_wav"),
    (b"RIFF\x00\x00\x00\x00FEV FMT " + bytes(32), "a.bank", "audio_fmod_bank"),
    (b"BKHD" + bytes(60), "a.bnk", "audio_wwise_bank"),
    (b"AKPK" + bytes(60), "a.pck", "audio_wwise_pck"),
    (b"OggS" + bytes(60), "a.ogg", "audio_ogg"),
    (b"CRID" + bytes(60), "a.usm", "video_usm"),
    (b"\x00\x00\x00\x18ftypmp42" + bytes(32), "a.mp4", "video_mp4"),
    (b"PK\x03\x04" + bytes(60), "a.zip", "archive_zip"),
    (b"7z\xbc\xaf\x27\x1c" + bytes(60), "a.7z", "archive_7z"),
    (b'{"a": [1, 2]}', "a.json", "json"),
    (b"hello world\n", "readme.txt", "text"),
    (b"", "empty.dat", "empty"),
    (bytes(1024), "zeros.bin", "unknown_binary"),
    (synthetic.high_entropy(), "blob.bin", "unknown_high_entropy"),
])
def test_detect_kind(data, name, kind):
    det = _detect(data, name)
    assert det.kind == kind
    assert det.kind in FORMATS


def test_every_format_has_route_or_explicit_none():
    for info in FORMATS.values():
        assert info.route is None or info.route.split(":")[0] in {"adapter", "stage"}


def test_png_dimensions():
    det = _detect(synthetic.png(width=7, height=3), "x.png")
    assert det.details["width"] == 7 and det.details["height"] == 3


def test_dds_details():
    det = _detect(synthetic.dds(width=128, height=64, fourcc=b"DXT5"), "x.dds")
    assert det.details == {"width": 128, "height": 64, "mip_count": 1, "fourcc": "DXT5"}


def test_unity_yaml_class_ids():
    det = _detect(synthetic.UNITY_PREFAB_YAML.encode(), "x.prefab")
    ids = det.details["class_ids_in_probe"]
    assert ids["1"]["name"] == "GameObject"
    assert ids["137"]["name"] == "SkinnedMeshRenderer"


def test_meta_guid():
    det = _detect(synthetic.UNITY_META.encode(), "x.meta")
    assert det.details["guid"] == "0123456789abcdef0123456789abcdef"


def test_unity_extension_without_signature_is_flagged():
    det = _detect(synthetic.high_entropy(), "creature.bundle")
    assert det.kind == "unknown_high_entropy"
    assert any("suggests a Unity container" in w for w in det.warnings)


def test_prefixed_bundle_detected_with_warning():
    data = b"\x13\x37" * 40 + synthetic.unityfs_bundle()
    det = _detect(data, "creature.bundle")
    assert det.kind == "unity_bundle"
    assert det.details["signature_offset"] == 80
    assert det.confidence == "medium"


def test_glb_length_mismatch_warns():
    data = synthetic.glb() + b"junk"
    det = _detect(data, "x.glb")
    assert det.confidence == "medium" and det.warnings


def test_entropy_bounds():
    assert shannon_entropy(b"") == 0.0
    assert shannon_entropy(b"aaaa") == 0.0
    assert shannon_entropy(bytes(range(256))) == pytest.approx(8.0)
