# AniimoPalworldToolkit

A modular Python toolchain for porting **legally usable** creature assets from
Aniimo (reported to be a customised Unity engine) into **Palworld**
(Unreal Engine 5.1) mods, one creature at a time.

The long-term goal is a "creature porting studio": pick an Aniimo, extract,
auto-map, convert, inspect and fix, configure Normal/Shiny/Prismana variants,
validate, and export a Palworld mod. See [docs/architecture.md](docs/architecture.md).

> **Status: milestone 1.** The read-only scanner, manifests, CLI, config,
> logging, the per-creature project format and tests are working. Extraction,
> conversion, Unreal import and Palworld integration are **not implemented yet**.
> Nothing has been tested against real Aniimo files, Unreal Engine or Palworld.
> See [docs/roadmap.md](docs/roadmap.md) for exactly what is verified.

## What works today

| Feature | Command |
|---------|---------|
| Read-only inventory of a directory: SHA-256 (or other) hashes, format detection, Unity version, bundle compression and encryption flags, sidecar relations, JSON manifest | `aniimo-pal scan` |
| Manifest summary, schema + consistency validation | `aniimo-pal summary`, `aniimo-pal validate` |
| Compare manifests across game updates (added/removed/modified/moved) | `aniimo-pal diff` |
| Re-verify files on disk against a manifest | `aniimo-pal verify` |
| List recognised formats and their planned pipeline route | `aniimo-pal formats` |
| Detect external tools (UnityPy, AssetRipper, Blender, Unreal Editor) | `aniimo-pal doctor` |
| Per-creature project files with variant rules; simulate rare-variant rates | `aniimo-pal creature init/validate/roll` |

## Install

Requires Python ≥ 3.11. The core has **no runtime dependencies**.

```bash
pip install -e ".[dev]"          # + pytest, jsonschema
pip install -e ".[unitypy]"      # optional: UnityPy (used from milestone 2; enables cross-check tests)
```

## Quick start

```bash
# 1. Inventory a game install (never modified). Output must be OUTSIDE the scanned folder.
aniimo-pal scan "D:/Games/Aniimo" -o workspace/manifests/aniimo.json --label "Aniimo 1.0"

# 2. Review what was found, including warnings about encrypted/unknown containers
aniimo-pal summary workspace/manifests/aniimo.json --warnings
aniimo-pal validate workspace/manifests/aniimo.json

# 3. Start a creature project (one species per project; resumable)
aniimo-pal creature init workspace --id aniimo_example --name "Example"
aniimo-pal creature roll workspace/creatures/aniimo_example/creature.json --n 1000000
```

Global options: `--config FILE` (TOML, see [config/default.toml](config/default.toml)),
`--log-level`, `--log-file` (JSON lines). Exit codes: `0` ok, `1` check failed,
`2` usage/config error.

## Recognised formats

Unity: UnityFS bundles (with encryption-flag detection), legacy
UnityWeb/UnityRaw, SerializedFiles (`.assets`, `level*`, `CAB-*`), `.resS`/
`.resource`, text YAML assets, `.meta`, `.unitypackage`. Also: IL2CPP metadata,
PE/ELF binaries, FBX, glTF/GLB, OBJ, PNG, JPEG, TGA, EXR, HDR, DDS, KTX/KTX2,
ASTC, PVR, FSB5, FMOD/Wwise banks, WAV, Ogg, USM, MP4, zip/gzip/7z, JSON, text.
Unrecognised files are classified by entropy. Run `aniimo-pal formats` for the
full table.

## Repository layout

```
config/            default.toml (documented defaults), local.example.toml
docs/              research, architecture, manifest formats, legal/safety, roadmap
src/aniimo_pal/    scanner/, manifest/, creature/, adapters/, cli.py, config.py, log.py, schemas/
tests/             unit tests; synthetic.py generates all test assets at runtime
```

## Legal and safety

Use only assets you have the right to use. The toolkit **never** decrypts,
de-obfuscates or bypasses protection. It reports encrypted or unknown containers
and stops there. No game files belong in this repository; `.gitignore` and
`tests/test_repo_hygiene.py` enforce this. See
[docs/legal-and-safety.md](docs/legal-and-safety.md).

## Development

```bash
python -m pytest           # all tests (UnityPy cross-checks skip if UnityPy is absent)
ruff check src tests
```
