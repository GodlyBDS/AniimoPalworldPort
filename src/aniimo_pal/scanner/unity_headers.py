"""Minimal, read-only parsers for Unity container headers.

Only *headers* are parsed: enough to identify a file, report its Unity version,
compression and flags, and decide which external tool should handle it. Payload
decompression and object parsing are deliberately left to mature tools
(UnityPy, AssetRipper) via adapters.

Layouts follow UnityPy's ``files/BundleFile.py`` / ``files/SerializedFile.py``
and ``enums/BundleFile.py`` (MIT). All header integers are big-endian.

Encrypted bundles are *detected and reported only*. This module never attempts
decryption, key recovery or any other protection bypass.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Any

BUNDLE_SIGNATURES = ("UnityFS", "UnityWeb", "UnityRaw", "UnityArchive")

COMPRESSION_NAMES = {0: "none", 1: "lzma", 2: "lz4", 3: "lz4hc", 4: "lzham"}

# ArchiveFlags (UnityPy enums/BundleFile.py)
FLAG_COMPRESSION_MASK = 0x3F
FLAG_BLOCKS_AND_DIRECTORY_COMBINED = 0x40
FLAG_BLOCKS_INFO_AT_END = 0x80
FLAG_OLD_WEB_PLUGIN_COMPAT = 0x100
# Older engines: encryption bit is 0x200.
FLAG_OLD_ENCRYPTION = 0x200
# Newer engines: 0x200 means "padding at start"; encryption is 0x400 or 0x1000.
FLAG_NEW_BLOCK_INFO_PADDING = 0x200
FLAG_NEW_ENCRYPTION = 0x1400

_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,3})\.(\d{1,3})(?:([a-z])(\d+))?")
_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")


def parse_unity_version(text: str) -> tuple[int, int, int] | None:
    """Parse '2021.3.15f1' (or '2021.3.15f1c1', '5.6.7') into (2021, 3, 15)."""
    m = _VERSION_RE.match(text or "")
    if not m:
        return None
    version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None if version == (0, 0, 0) else version


def uses_old_archive_flags(version: tuple[int, int, int]) -> bool:
    """Mirror of UnityPy's threshold for the pre-2020.3.34 ArchiveFlags layout."""
    return (
        version < (2020,)
        or (version[0] == 2020 and version < (2020, 3, 34))
        or (version[0] == 2021 and version < (2021, 3, 2))
        or (version[0] == 2022 and version < (2022, 1, 1))
    )


def _read_cstring(data: bytes, offset: int, limit: int = 64) -> tuple[str, int] | None:
    """Read a NUL-terminated printable ASCII string. Returns (text, next_offset)."""
    end = data.find(b"\x00", offset, offset + limit + 1)
    if end < 0:
        return None
    raw = data[offset:end]
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _PRINTABLE.match(text):
        return None
    return text, end + 1


@dataclass
class HeaderResult:
    """Outcome of a header parse: structured details plus human-readable warnings."""

    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    confidence: str = "high"


def find_bundle_signature(data: bytes) -> tuple[str, int] | None:
    """Locate a Unity bundle signature (with its NUL) in ``data``.

    Returns (signature, offset). Offset 0 is the normal case; a non-zero offset
    means the file has a prefix in front of the bundle (custom container or
    obfuscation), which is reported but not stripped.
    """
    best: tuple[str, int] | None = None
    for sig in BUNDLE_SIGNATURES:
        pos = data.find(sig.encode("ascii") + b"\x00")
        if pos >= 0 and (best is None or pos < best[1]):
            best = (sig, pos)
    return best


def parse_bundle_header(data: bytes, file_size: int, offset: int = 0) -> HeaderResult | None:
    """Parse a UnityFS / UnityWeb / UnityRaw / UnityArchive header at ``offset``."""
    sig = _read_cstring(data, offset, 16)
    if sig is None or sig[0] not in BUNDLE_SIGNATURES:
        return None
    signature, pos = sig
    result = HeaderResult()
    d = result.details
    d["signature"] = signature
    d["signature_offset"] = offset
    if offset:
        result.warnings.append(
            f"{signature} signature found at byte offset {offset}, not 0: the file has a "
            "prefix (custom container or obfuscation). Standard tools may refuse it; "
            "the toolkit will not strip or alter it."
        )
        result.confidence = "medium"

    if len(data) < pos + 4:
        result.warnings.append("header truncated before format version")
        result.confidence = "low"
        return result
    (format_version,) = struct.unpack_from(">I", data, pos)
    pos += 4
    d["format_version"] = format_version

    player = _read_cstring(data, pos)
    engine = _read_cstring(data, player[1]) if player else None
    if player is None or engine is None:
        result.warnings.append("unreadable version strings in bundle header")
        result.confidence = "low"
        return result
    d["player_version"], pos = player[0], player[1]
    d["unity_version"], pos = engine[0], engine[1]
    parsed_version = parse_unity_version(engine[0])

    if signature != "UnityFS":
        # Legacy UnityWeb/UnityRaw (Unity <= 5.2). Recognised, not decoded further.
        d["legacy_format"] = True
        return result

    if len(data) < pos + 20:
        result.warnings.append("UnityFS header truncated")
        result.confidence = "low"
        return result
    total_size, comp_info, uncomp_info, flags = struct.unpack_from(">qIII", data, pos)
    d["declared_size"] = total_size
    d["blocks_info_compressed_size"] = comp_info
    d["blocks_info_uncompressed_size"] = uncomp_info
    d["flags"] = flags
    d["flags_hex"] = f"0x{flags:X}"
    compression = flags & FLAG_COMPRESSION_MASK
    d["blocks_info_compression"] = COMPRESSION_NAMES.get(compression, f"unknown({compression})")
    d["blocks_info_at_end"] = bool(flags & FLAG_BLOCKS_INFO_AT_END)
    d["blocks_and_directory_combined"] = bool(flags & FLAG_BLOCKS_AND_DIRECTORY_COMBINED)

    if compression not in COMPRESSION_NAMES:
        result.warnings.append(
            f"unknown block-info compression type {compression}; possibly a modified engine"
        )
        result.confidence = "medium"

    if total_size != file_size - offset:
        result.warnings.append(
            f"declared bundle size {total_size} != actual {file_size - offset} bytes "
            "(truncated, appended data, or modified container)"
        )
        result.confidence = "medium"

    if parsed_version is None:
        # Some games blank the engine version (e.g. '0.0.0'). The meaning of bit
        # 0x200 then depends on the real engine version, so report both readings.
        # True: a new-layout encryption bit is set; None: only 0x200 is set
        # (padding in new layout, encryption in old); False: neither.
        d["flag_layout"] = "ambiguous"
        if flags & FLAG_NEW_ENCRYPTION:
            d["encryption_flag"] = True
        elif flags & FLAG_OLD_ENCRYPTION:
            d["encryption_flag"] = None
        else:
            d["encryption_flag"] = False
        result.warnings.append(
            f"engine version {engine[0]!r} is blank/unparseable; archive flag meaning is ambiguous"
        )
        if d["encryption_flag"] is True:
            result.warnings.append(_ENCRYPTION_WARNING)
        elif d["encryption_flag"] is None:
            result.warnings.append(
                "flag 0x200 is set: either block-info padding (newer engines) or "
                "encryption (older engines); cannot tell without the engine version"
            )
    else:
        old = uses_old_archive_flags(parsed_version)
        d["flag_layout"] = "pre-2020.3.34" if old else "post-2020.3.34"
        if old:
            encrypted = bool(flags & FLAG_OLD_ENCRYPTION)
        else:
            encrypted = bool(flags & FLAG_NEW_ENCRYPTION)
            d["block_info_padding_at_start"] = bool(flags & FLAG_NEW_BLOCK_INFO_PADDING)
        d["encryption_flag"] = encrypted
        if encrypted:
            result.warnings.append(_ENCRYPTION_WARNING)

    known_bits = (
        FLAG_COMPRESSION_MASK
        | FLAG_BLOCKS_AND_DIRECTORY_COMBINED
        | FLAG_BLOCKS_INFO_AT_END
        | FLAG_OLD_WEB_PLUGIN_COMPAT
        | FLAG_NEW_BLOCK_INFO_PADDING
        | FLAG_NEW_ENCRYPTION
    )
    if flags & ~known_bits:
        result.warnings.append(f"unknown archive flag bits set: 0x{flags & ~known_bits:X}")
    return result


_ENCRYPTION_WARNING = (
    "bundle archive flags mark it as encrypted (UnityCN-style AssetBundle encryption). "
    "The toolkit does not decrypt or bypass protection; this file is out of scope "
    "unless you have the rights holder's permission and an authorised export path."
)


def parse_serialized_header(data: bytes, file_size: int) -> HeaderResult | None:
    """Detect a Unity SerializedFile (.assets, level0, CAB-*, globalgamemanagers).

    SerializedFiles have no magic number, so detection is structural: the format
    version must be plausible and the declared file size must equal the real
    size. Returns None if the bytes do not look like a SerializedFile.
    """
    if len(data) < 20 or file_size < 20:
        return None
    metadata_size, size32, version, data_offset = struct.unpack_from(">IIII", data, 0)
    if not 6 <= version <= 50:
        return None
    header_end = 16
    endian = None
    if version >= 9:
        endian_byte = data[16]
        if endian_byte not in (0, 1) or data[17:20] != b"\x00\x00\x00":
            return None
        endian = "big" if endian_byte else "little"
        header_end = 20
    declared_size = size32
    if version >= 22:
        if len(data) < 48:
            return None
        metadata_size, declared_size, data_offset, _unknown = struct.unpack_from(">IqqQ", data, 20)
        header_end = 48
    if declared_size != file_size or data_offset > file_size or metadata_size > file_size:
        return None

    result = HeaderResult()
    d = result.details
    d["format_version"] = version
    d["metadata_size"] = metadata_size
    d["data_offset"] = data_offset
    if endian:
        d["endianness"] = endian
    if version >= 9:
        # Metadata (starting with the Unity version string) follows the header.
        text = _read_cstring(data, header_end)
        if text and parse_unity_version(text[0]):
            d["unity_version"] = text[0]
        elif text and text[0] in ("", "0.0.0"):
            d["unity_version"] = text[0]
            result.warnings.append("Unity version string is blank (stripped by the build)")
            result.confidence = "medium"
        else:
            result.confidence = "medium"
            result.warnings.append("no readable Unity version string after header")
    else:
        # Pre-v9 files keep metadata at the end of the file; not probed.
        result.confidence = "medium"
    return result
