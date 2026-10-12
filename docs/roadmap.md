# Roadmap, verification status and blockers

## Milestone status

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Repository architecture, read-only scanner, manifests, CLI, config, logging, tests, docs | **Done** (this release) |
| 1b | Creature project model (`creature.json`) + rare-variant reference semantics | **Done** (data model and tests only) |
| 2 | Adapters that run tools: UnityPy object inventory, AssetRipper headless export to the workspace | Planned |
| 3 | Creature catalog + association engine (dependency graph + evidence, proposals, confirmations, reusable rules) | Planned |
| 4 | Conversion stages via Blender (GLB→FBX, axis/scale, bone rename/retarget, animation bake, texture/material maps) + first editor (Blender add-on) | Planned |
| 5 | UE 5.1 Python import script + Palworld mod project scaffold; **replace mode** for one test creature | Planned |
| 6 | **New-Pal mode**: data rows (PalSchema), spawners, variants as separate rows, persistence checks | Planned, needs research |
| 7 | Studio GUI shell (guided workflow, 3D preview, validation report) | Planned |
| 8 | Mod packs, compatibility checks, distributable build | Planned |

## What has actually been verified

| Claim | How |
|-------|-----|
| Scanner identifies every format in `aniimo-pal formats` | Unit tests on synthetic bytes (`tests/test_formats.py`) |
| UnityFS / SerializedFile header parsing matches UnityPy | `tests/test_unitypy_crosscheck.py`, UnityPy 1.25.4, synthetic files that UnityPy opens |
| Encryption flag interpretation by engine version | Unit tests built from UnityPy's flag definitions and version thresholds |
| Scanner never modifies the input tree | Byte, size and mtime snapshot before/after (`test_scan_is_read_only`) |
| Output refused inside scanned tree | CLI and API tests |
| Manifest schema, semantic validation, diff, verify | Unit tests |
| Variant roll is mutually exclusive and matches configured rates | 200k-sample statistical test (5σ bound) |
| Tested on Linux, Python 3.13 | Local run only. A CI matrix for 3.11–3.13 on Linux and Windows is configured (`.github/workflows/ci.yml`) but has not run yet; Windows is untested |

## Not verified (no files or tools available in the development environment)

- **Any real Aniimo file.** No game data was available. Whether Aniimo uses
  standard UnityFS bundles, UnityCN encryption, custom containers or something
  else is **unknown**. The engine itself was not confirmed by a public source.
- AssetRipper headless run (interface read from source only).
- Blender scripting, FBX round-trips, Unreal import, cooking, pak packaging,
  UE4SS/PalSchema behaviour and in-game loading. **None of these were run.**
- Palworld data-table names for new Pals, spawn-group selection semantics and
  how captured Pals are saved. These decide the variant design (`architecture.md` §6).

## Blockers

| Blocker | Unblocks | Who can resolve |
|---------|----------|-----------------|
| Rights to use specific Aniimo assets | Any public release of ported creatures | Rights holder / user |
| Aniimo install for local scanning | Knowing the real container format (milestone 2 scope) | User, locally |
| Encrypted or custom containers (if the scan finds them) | Extraction of those files. **The toolkit will not bypass them** | Rights holder (official export or permission) |
| Windows machine with UE 5.1 + PalworldModdingKit toolchain (VS 2022 / MSVC 14.38, .NET 6, Wwise 2021.1.11) | Milestones 5–6 | User, locally |
| Palworld install + Palworld RE-UE4SS build + PalSchema | In-game testing | User, locally |

## Local tests needed (please run and share results)

1. **Scan your Aniimo install** (read-only):
   ```
   aniimo-pal scan "<Aniimo install dir>" -o workspace/manifests/aniimo.json --label "Aniimo <version>"
   aniimo-pal summary workspace/manifests/aniimo.json --warnings > workspace/scan-report.txt
   ```
   Share `summary --json` output and a few lines from the warnings report. Do
   **not** share game files. The key questions: how many `unity_bundle` and
   `unity_serialized` files exist, what `encrypted_count` is, how many files are
   `unknown_*`, and which Unity version is reported.
2. `aniimo-pal doctor` on the machine that will run the pipeline.
3. After a game patch: re-scan, then `aniimo-pal diff old.json new.json`.
4. Milestone 5 prerequisite: confirm the untouched PalworldModdingKit project
   opens and cooks on your machine (per the kit's own instructions).
