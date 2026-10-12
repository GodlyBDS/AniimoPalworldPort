"""File-format recognition from a byte probe (the first N bytes) plus the file name.

Recognition is signature-first; extensions are only used as hints when a format
has no magic number (e.g. ``.resS``, ``.tga``) or to flag suspicious mismatches.
Each format is registered in ``FORMATS`` with the pipeline route that will
handle it in later milestones, so the manifest doubles as a work list.
"""

from __future__ import annotations

import codecs
import json
import math
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from aniimo_pal.scanner import unity_headers as uh


@dataclass(frozen=True)
class FormatInfo:
    kind: str
    category: str
    description: str
    # Planned handler: adapter/stage name, or None if the pipeline ignores it.
    route: str | None = None


_FORMAT_LIST = [
    # Unity containers and project files
    FormatInfo("unity_bundle", "unity_container", "UnityFS AssetBundle (format 6+)", "adapter:unitypy|assetripper"),
    FormatInfo("unity_bundle_legacy", "unity_container", "UnityWeb/UnityRaw/UnityArchive legacy bundle", "adapter:assetripper"),
    FormatInfo("unity_serialized", "unity_container", "Unity SerializedFile (.assets, level*, CAB-*)", "adapter:unitypy|assetripper"),
    FormatInfo("unity_resource", "unity_container", "Unity streamed resource data (.resS/.resource)", "adapter:unitypy|assetripper"),
    FormatInfo("unity_yaml", "unity_project", "Unity text-serialised asset (scene/prefab/material/anim)", "adapter:unity-yaml"),
    FormatInfo("unity_meta", "unity_project", "Unity .meta sidecar (asset GUID)", None),
    FormatInfo("unity_package", "unity_project", ".unitypackage (gzip tar)", "adapter:assetripper"),
    # Code / runtime metadata (inventoried, never processed)
    FormatInfo("il2cpp_metadata", "code", "IL2CPP global-metadata.dat", None),
    FormatInfo("pe_binary", "code", "Windows PE executable/DLL", None),
    FormatInfo("elf_binary", "code", "ELF executable/shared object", None),
    # Interchange models
    FormatInfo("model_fbx", "model", "Autodesk FBX (binary or ASCII)", "stage:blender"),
    FormatInfo("model_glb", "model", "glTF 2.0 binary (.glb)", "stage:blender"),
    FormatInfo("model_gltf", "model", "glTF 2.0 JSON (.gltf)", "stage:blender"),
    FormatInfo("model_obj", "model", "Wavefront OBJ", "stage:blender"),
    # Images / textures
    FormatInfo("image_png", "texture", "PNG image", "stage:texture"),
    FormatInfo("image_jpeg", "texture", "JPEG image", "stage:texture"),
    FormatInfo("image_tga", "texture", "Truevision TGA (by extension)", "stage:texture"),
    FormatInfo("image_exr", "texture", "OpenEXR image", "stage:texture"),
    FormatInfo("image_hdr", "texture", "Radiance HDR image", "stage:texture"),
    FormatInfo("texture_dds", "texture", "DirectDraw Surface", "stage:texture"),
    FormatInfo("texture_ktx", "texture", "Khronos KTX 1.1", "stage:texture"),
    FormatInfo("texture_ktx2", "texture", "Khronos KTX 2.0", "stage:texture"),
    FormatInfo("texture_astc", "texture", "ASTC compressed texture", "stage:texture"),
    FormatInfo("texture_pvr", "texture", "PowerVR PVR v3 texture", "stage:texture"),
    # Audio / video (inventoried only; out of scope for the 3D pipeline)
    FormatInfo("audio_fsb5", "audio", "FMOD sample bank (FSB5)", None),
    FormatInfo("audio_fmod_bank", "audio", "FMOD Studio bank (RIFF/FEV)", None),
    FormatInfo("audio_wwise_bank", "audio", "Wwise SoundBank (BKHD)", None),
    FormatInfo("audio_wwise_pck", "audio", "Wwise file package (AKPK)", None),
    FormatInfo("audio_wav", "audio", "RIFF WAVE audio", None),
    FormatInfo("audio_ogg", "audio", "Ogg container", None),
    FormatInfo("video_usm", "video", "CRI USM video", None),
    FormatInfo("video_mp4", "video", "ISO BMFF / MP4", None),
    # Generic
    FormatInfo("archive_zip", "archive", "ZIP archive", None),
    FormatInfo("archive_gzip", "archive", "gzip stream", None),
    FormatInfo("archive_7z", "archive", "7-Zip archive", None),
    FormatInfo("json", "data", "JSON document", None),
    FormatInfo("text", "data", "Plain text (UTF-8)", None),
    FormatInfo("empty", "other", "Zero-byte file", None),
    FormatInfo("unknown_binary", "other", "Unrecognised binary", None),
    FormatInfo("unknown_high_entropy", "other", "Unrecognised high-entropy data (compressed/encrypted/packed)", None),
]

FORMATS: dict[str, FormatInfo] = {f.kind: f for f in _FORMAT_LIST}

# Extensions that conventionally hold Unity containers. Used only to flag files
# whose bytes do NOT match any known signature.
UNITY_CONTAINER_EXTENSIONS = {".assets", ".bundle", ".unity3d", ".ab", ".assetbundle"}


@dataclass
class Detection:
    kind: str
    confidence: str = "high"
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def info(self) -> FormatInfo:
        return FORMATS[self.kind]


def shannon_entropy(data: bytes) -> float:
    """Shannon entropy in bits per byte (0.0 for empty input)."""
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def _ext(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".ress"):
        return ".ress"
    dot = lower.rfind(".")
    return lower[dot:] if dot > 0 else ""


def _decode_text(head: bytes) -> str | None:
    """Decode the probe as UTF-8 text (tolerating a split final character)."""
    if b"\x00" in head:
        return None
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return None
    if text.startswith("﻿"):
        text = text[1:]
    # Reject control-heavy data that happens to be valid UTF-8.
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\r\n\f")
    if text and controls / len(text) > 0.01:
        return None
    return text


_UNITY_YAML_DOC = re.compile(r"^--- !u!(\d+) &(-?\d+)", re.MULTILINE)
_META_GUID = re.compile(r"^guid: ([0-9a-f]{32})\s*$", re.MULTILINE)

# Common Unity class IDs (https://docs.unity3d.com/Manual/ClassIDReference.html).
UNITY_CLASS_NAMES = {
    1: "GameObject", 4: "Transform", 21: "Material", 23: "MeshRenderer", 28: "Texture2D",
    33: "MeshFilter", 43: "Mesh", 48: "Shader", 74: "AnimationClip", 83: "AudioClip",
    90: "Avatar", 91: "AnimatorController", 95: "Animator", 111: "Animation",
    114: "MonoBehaviour", 115: "MonoScript", 137: "SkinnedMeshRenderer",
    213: "Sprite", 224: "RectTransform", 1001: "PrefabInstance",
}


def _detect_text(text: str, ext: str, size: int) -> Detection | None:
    stripped = text.lstrip()
    if text.startswith("%YAML") and "!u!" in text[:512]:
        ids = [int(m.group(1)) for m in _UNITY_YAML_DOC.finditer(text)]
        counts = Counter(ids)
        return Detection(
            "unity_yaml",
            details={
                "class_ids_in_probe": {
                    str(cid): {"name": UNITY_CLASS_NAMES.get(cid, "unknown"), "count": n}
                    for cid, n in sorted(counts.items())
                }
            },
        )
    if stripped.startswith("fileFormatVersion:"):
        m = _META_GUID.search(text)
        return Detection("unity_meta", details={"guid": m.group(1)} if m else {},
                         confidence="high" if m else "medium")
    if stripped.startswith("; FBX"):
        return Detection("model_fbx", details={"encoding": "ascii"})
    if stripped.startswith(("{", "[")):
        details: dict[str, Any] = {}
        parsed = None
        if len(text.encode("utf-8")) == size:  # whole file is in the probe
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        if ext == ".gltf" or (isinstance(parsed, dict) and isinstance(parsed.get("asset"), dict)
                              and "version" in parsed["asset"]):
            if isinstance(parsed, dict) and isinstance(parsed.get("asset"), dict):
                details["gltf_version"] = str(parsed["asset"].get("version"))
            return Detection("model_gltf", details=details,
                             confidence="high" if details else "medium")
        return Detection("json", confidence="high" if parsed is not None else "medium")
    if ext == ".obj" and re.search(r"^(v|vn|vt|f) ", text, re.MULTILINE):
        return Detection("model_obj")
    if text.startswith("#?RADIANCE") or text.startswith("#?RGBE"):
        return Detection("image_hdr")
    return Detection("text", confidence="medium")


def _magic(head: bytes, size: int, ext: str) -> Detection | None:
    h = head
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        det = Detection("image_png")
        if len(h) >= 24 and h[12:16] == b"IHDR":
            w, hgt = struct.unpack_from(">II", h, 16)
            det.details.update(width=w, height=hgt, bit_depth=h[24] if len(h) > 24 else None,
                               color_type=h[25] if len(h) > 25 else None)
        return det
    if h.startswith(b"\xff\xd8\xff"):
        return Detection("image_jpeg")
    if h.startswith(b"DDS ") and len(h) >= 88:
        height, width = struct.unpack_from("<II", h, 12)
        mips = struct.unpack_from("<I", h, 28)[0]
        fourcc = h[84:88]
        details = {"width": width, "height": height, "mip_count": mips or 1}
        if fourcc.strip(b"\x00"):
            details["fourcc"] = fourcc.decode("latin-1")
        return Detection("texture_dds", details=details)
    if h.startswith(b"\xabKTX 11\xbb\r\n\x1a\n"):
        det = Detection("texture_ktx")
        if len(h) >= 44:
            endian = "<" if h[12:16] == b"\x01\x02\x03\x04" else ">"
            w, hgt = struct.unpack_from(endian + "II", h, 36)
            det.details.update(width=w, height=hgt)
        return det
    if h.startswith(b"\xabKTX 20\xbb\r\n\x1a\n"):
        det = Detection("texture_ktx2")
        if len(h) >= 28:
            vk_format, _type_size, w, hgt = struct.unpack_from("<IIII", h, 12)
            det.details.update(vk_format=vk_format, width=w, height=hgt)
        return det
    if h.startswith(b"\x13\xab\xa1\x5c") and len(h) >= 16:
        bx, by, bz = h[4], h[5], h[6]
        w = h[7] | h[8] << 8 | h[9] << 16
        hgt = h[10] | h[11] << 8 | h[12] << 16
        return Detection("texture_astc", details={"block": f"{bx}x{by}x{bz}", "width": w, "height": hgt})
    if h.startswith(b"PVR\x03"):
        det = Detection("texture_pvr")
        if len(h) >= 32:
            hgt, w = struct.unpack_from("<II", h, 24)
            det.details.update(width=w, height=hgt)
        return det
    if h.startswith(b"\x76\x2f\x31\x01"):
        return Detection("image_exr")
    if h.startswith(b"Kaydara FBX Binary  \x00"):
        det = Detection("model_fbx", details={"encoding": "binary"})
        if len(h) >= 27:
            det.details["fbx_version"] = struct.unpack_from("<I", h, 23)[0]
        return det
    if h.startswith(b"glTF") and len(h) >= 12:
        version, length = struct.unpack_from("<II", h, 4)
        det = Detection("model_glb", details={"gltf_container_version": version, "declared_length": length})
        if length != size:
            det.warnings.append(f"GLB declared length {length} != file size {size}")
            det.confidence = "medium"
        return det
    if h.startswith(b"\xaf\x1b\xb1\xfa"):
        det = Detection("il2cpp_metadata")
        if len(h) >= 8:
            det.details["metadata_version"] = struct.unpack_from("<i", h, 4)[0]
        return det
    if h.startswith(b"MZ"):
        return Detection("pe_binary", confidence="medium")
    if h.startswith(b"\x7fELF"):
        return Detection("elf_binary")
    if h.startswith(b"FSB5"):
        return Detection("audio_fsb5")
    if h.startswith(b"BKHD"):
        return Detection("audio_wwise_bank")
    if h.startswith(b"AKPK"):
        return Detection("audio_wwise_pck")
    if h.startswith(b"RIFF") and len(h) >= 12:
        form = h[8:12]
        if form == b"WAVE":
            return Detection("audio_wav")
        if form == b"FEV ":
            return Detection("audio_fmod_bank")
    if h.startswith(b"OggS"):
        return Detection("audio_ogg")
    if h.startswith(b"CRID"):
        return Detection("video_usm")
    if len(h) >= 12 and h[4:8] == b"ftyp":
        return Detection("video_mp4", details={"brand": h[8:12].decode("latin-1")})
    if h.startswith(b"PK\x03\x04") or h.startswith(b"PK\x05\x06"):
        return Detection("archive_zip")
    if h.startswith(b"\x1f\x8b"):
        if ext == ".unitypackage":
            return Detection("unity_package")
        return Detection("archive_gzip")
    if h.startswith(b"7z\xbc\xaf\x27\x1c"):
        return Detection("archive_7z")
    return None


def detect(head: bytes, file_size: int, name: str, high_entropy_threshold: float = 7.5) -> Detection:
    """Identify a file from its first bytes (``head``), real size and file name."""
    ext = _ext(name)
    if file_size == 0:
        return Detection("empty")

    # 1. Unity bundle signature at offset 0.
    if head[:7] in (b"UnityFS", b"UnityWe", b"UnityRa", b"UnityAr"):
        parsed = uh.parse_bundle_header(head, file_size, 0)
        if parsed is not None:
            kind = "unity_bundle" if parsed.details.get("signature") == "UnityFS" else "unity_bundle_legacy"
            return Detection(kind, parsed.confidence, parsed.details, parsed.warnings)

    # 2. Formats with magic numbers.
    det = _magic(head, file_size, ext)
    if det is not None:
        return det

    # 3. Unity SerializedFile (structural check, no magic).
    parsed = uh.parse_serialized_header(head, file_size)
    if parsed is not None:
        return Detection("unity_serialized", parsed.confidence, parsed.details, parsed.warnings)

    # 4. Text formats.
    text = _decode_text(head)
    if text is not None:
        det = _detect_text(text, ext, file_size)
        if det is not None:
            return det

    # 5. Bundle signature behind a prefix (custom container / obfuscation).
    found = uh.find_bundle_signature(head)
    if found is not None:
        parsed = uh.parse_bundle_header(head, file_size, found[1])
        if parsed is not None:
            kind = "unity_bundle" if found[0] == "UnityFS" else "unity_bundle_legacy"
            return Detection(kind, parsed.confidence, parsed.details, parsed.warnings)

    # 6. Extension-only formats.
    if ext in (".ress", ".resource"):
        return Detection("unity_resource", confidence="medium",
                         details={"identified_by": "extension"})
    if ext == ".tga":
        return Detection("image_tga", confidence="medium", details={"identified_by": "extension"})

    # 7. Unknown: classify by entropy and flag suspicious Unity extensions.
    entropy = shannon_entropy(head)
    det = Detection("unknown_high_entropy" if entropy >= high_entropy_threshold else "unknown_binary",
                    confidence="low")
    if ext in UNITY_CONTAINER_EXTENSIONS:
        det.warnings.append(
            f"extension {ext!r} suggests a Unity container but no known Unity signature or "
            "header structure was found (custom engine format, encryption or corruption). "
            "Not processed; see docs/legal-and-safety.md."
        )
    return det
