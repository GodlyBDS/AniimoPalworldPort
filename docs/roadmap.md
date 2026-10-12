# Roadmap, verification status and blockers

## Milestone status

| # | Milestone | Status |
|---|-----------|--------|
| 1 | Repository architecture, read-only scanner, manifests, CLI, config, logging, tests, docs | **Done** |
| 1b | Creature project model (`creature.json`) + rare-variant reference semantics | **Done** |
| 2 | Tool adapters: UnityPy object inventory, AssetRipper headless export, UnityPy extraction (PNG/OBJ/JSON) | **Done** (synthetic data; real AssetRipper build exercised) |
| 3 | Creature catalog + association engine (dependency graph, variants, skeleton, animation linking, evidence/provenance, review/confirm workflow, reusable rules) | **Done** (synthetic data) |
| 3b | **Identity model** (species, stage, regional form, Normal/Prismana, standard/12 Shiny) with independent form + Shiny rolls, wild/egg rarity, computed Prismana-Shiny odds, biome-driven regional forms (creature schema 0.3.0, auto-migration) | **Done** (reference semantics + validation; regional detection from real data pending) |
| 4 | Conversion via Blender: GLB→FBX, axis/scale, skin check, bone rename/retarget, Unity `.anim`→FBX animation, texture/material maps; first editor (Blender add-on) | **In progress**. Done: `.anim` parsing, skin assembly (works around AssetRipper's missing skinned-prefab export), Blender job with verified clip retargeting, `creature convert`. Next: materials/textures in the FBX, Palworld-skeleton retarget for replace mode, blend shapes/LODs, Blender add-on editor |
| 5 | UE 5.1 Python import script + Palworld mod scaffold; **replace mode** for one test creature | Planned |
| 6 | **New-Pal mode**: data rows (PalSchema), biome spawners for regional forms, form/appearance identity persistence (capture, storage, save/load, breeding inheritance), in-game rarity matching the reference roller | Planned, needs research |
| 7 | **Combat translation + in-game testing**: combat discovery (events, controller states, skill data), reusable combat templates, **Ultimate → Partner Skill** framework, AI usage rules, combat validation | Planned, starts with a research spike (architecture §9) |
| 8 | **Stats, element & work-suitability translation**: data-table discovery on real data, verified system definitions, normalisation + balancing presets (Faithful / Vanilla Balanced / Custom), element and work mapping tables, variant-specific verified differences | Planned; schema, vocabularies and validation already in place (architecture §10) |
| 9 | Studio GUI (guided workflow, 3D preview, association review, variant, combat, stats and work panels, validation report) | **Started early**: portable Studio with first-run installer, game-folder selection, scan/catalog, creature workflow, asset review, variant + animation 3D preview (Blender scene + still), Palworld mods page ([studio.md](studio.md)). Combat/stats/regional panels still to come |
| 10 | Mod packs, compatibility checks, distributable build | Planned |

Regional forms are added step by step: catalog detection when real data is
available (rule keywords plus verified evidence only, never invented),
per-region conversion in M4, biome mapping and spawning in M6, and the editor
panel in M9. The 12 Shiny appearances are likewise only enabled for appearances
that exist in the source data. Unofficial appearances are never generated or
presented as authentic.

Combat translation (M7) only starts once extraction and conversion
(M4–M6) work reliably on real creatures. Its data shape is already reserved and
validated in `creature.json` so earlier stages can record combat facts (e.g.
animation events) without migrations.

## What has actually been verified

| Claim | How |
|-------|-----|
| Scanner identifies every listed format; never modifies input | Unit tests on synthetic bytes; before/after snapshots |
| UnityFS / SerializedFile header parsing matches UnityPy | `tests/test_unitypy_crosscheck.py`, UnityPy 1.25.4 |
| Synthetic test bundles are real Unity data | Objects serialised with UnityPy's own type-tree writer and Unity 2021.3 type definitions; UnityPy and AssetRipper both load them |
| Object inventory: classes, names, container paths, references (incl. cross-bundle), clip events, avatar bone paths | `tests/test_inventory.py` |
| Encrypted-flag bundles are never opened; changed files are refused | inventory + extract tests |
| Catalog: species grouping, Normal/Shiny/Prismana detection, asset roles and variant attribution, skeleton, controller clips, CRC32 binding-hash clip matching, thresholds, unclassified handling | `tests/test_catalog.py` |
| Creature workflow: from catalog, auto-confirm verified, review/associate, status transitions | `tests/test_workflow.py` |
| UnityPy extraction: correct PNG pixels per variant, OBJ geometry, material → texture file links, clip events, skeleton JSON | `tests/test_workflow.py` |
| AssetRipper adapter protocol (launch, form encoding, completion markers, timeouts, startup failures) | Fake server in CI (`tests/fake_assetripper.py`) **and** a real AssetRipper build (master @ beeb50e) via `ASSETRIPPER_PATH=… pytest -k real` |
| Independent form/Shiny rolls, exact combined odds, wild vs egg contexts, equal-weight appearances, regional restrictions, biome → regional form, no extra copies, policy errors/warnings, 0.1/0.2 → 0.3 migration | `tests/test_rarity.py`, `tests/test_creature.py` |
| Catalog detects Prismana and Shiny independently (Prismana Shiny prefabs), numbers Shiny appearances, scopes assets by form/appearance | `tests/test_catalog.py` |
| Combat section schema (multi-phase abilities, Ultimate → Partner Skill default, provenance) | `tests/test_creature.py` |
| `.anim` parsing of real AssetRipper output; Hermite evaluation | `tests/test_convert.py` (fixture produced by real AssetRipper from a synthetic compiled clip) |
| Skin assembly: joint order, glTF conversion, inverse bind matrices, bind-pose source, vertices on bones | `tests/test_skin_assembly.py` |
| Clip retargeting through FBX export + re-import matches Unity-space math (≤ 0.1 mm) | `tests/test_convert.py` (bpy 5.2.2) |
| Studio: first-run install into a fresh folder (Python env, portable Blender 5.2.2, AssetRipper from zip), full creature workflow incl. 3D preview with real AssetRipper + real Blender, containment check (nothing written to home/temp/caches outside the folder), GUI under Xvfb | Linux, manual run + `tests/test_studio.py`; Windows/macOS untested |
| Full chain scan → inventory → catalog → creature → extract → AssetRipper (primary + project) → convert | `tests/test_convert_pipeline.py` with `ASSETRIPPER_PATH` (real build) |
| Stats/elements/work schema: vocabulary checks against system definitions, unmapped elements handled explicitly, work-level ranges, variant inheritance and overrides, rarity/combat kept out | `tests/test_profile.py` |
| TextAsset description + `objects` search (data-table discovery) | `tests/test_inventory.py` |
| Targeted inventory (survey all, deep-read creature bundles + dependency closure) gives the same creature objects as a full read; cache resume; parallel workers (spawn) agree; quick size/mtime check | `tests/test_inventory.py`; 420-bundle synthetic install: 2.4 s targeted vs 6.1 s full, cached re-run < 1 s |
| Catalog scales to a real-size object list: 3,000 rigged prefabs + 20,000 clips in ~9 s (clips grouped by binding set, indexed once); clips fitting too many skeletons counted as ambiguous; diagnostics for empty catalogs | `tests/test_catalog.py`; synthetic benchmark |
| Studio first-run install (Python env, AssetRipper, Blender 5.2.2), scan and parallel object list on **Windows** with a real Aniimo install (2,165 containers, 2.36 M objects, 4.5 min) | User's machine (log shared, no game files) |
| Catalog on the real install: before ID-aware naming, all `P_Parmon_<id>` prefabs merged into one species and effects/items were listed; now IDs separate species, name groups can be chosen, codenames are guessed from file names | Unit tests with Aniimo-style names; **re-run on the real install pending** |
| Game-ID forms (`1018403` = `10184` form 03) grouped with their creature, left unclassified; user names file | `tests/test_catalog.py`, `tests/test_studio.py`; ID scheme observed in the real install's prefab names |
| Toolkit-written mesh GLB (UnityPy → glTF with JOINTS_0/WEIGHTS_0) identical to AssetRipper's mesh GLB triangle by triangle (positions, joints, weights, winding); conversion no longer needs AssetRipper for meshes | `tests/test_convert_pipeline.py` (real AssetRipper + bpy) |
| **First real creature converted: Chirpi (10181)**: rigged FBX + preview, user confirmed it looks correct (eyes were missing: fixed below, re-test pending) | User's machine, Windows |
| One combined model per creature (body, eyes, ... on one skeleton, one skin per part); single-bone and weightless parts (eyes) attached to their renderer; per-part textures in the preview; placeholder meshes shared by many prefabs no longer merge species | `tests/test_convert_pipeline.py` (bpy), `tests/test_catalog.py` |
| Placeholder renderers (`SkinDummyMesh`, real meshes attached at runtime: Grizbo 10503, Sparki 10331, 1001202) resolved to the real mesh by renderer name; survey records every mesh name | `tests/test_catalog.py`; **real-data check pending** |
| Chirpi (10181) with eyes: confirmed correct by the user. Tromber (10184): mesh distorted (strips, detached legs), suspected bind poses (game keeps them in separate `…_BindPose_…` assets) | **Open** |
| First real conversion attempt (Tromber 10184, Chirpi 10181): AssetRipper 2.0 exported the real meshes and clips only as `.json`/`.asset` dumps (no `.glb`, no `.anim`) | **Open**: real AssetRipper (local test with Aniimo-style names, suffixes and container paths) names files after the object name incl. suffix under the asset path, which matches; the real export still differs. Export now logs exported files sharing the creature's name parts, plus file-type counts; build-suffix-tolerant matching added |
| Studio Stop button kills the step's whole process tree; progress lines drive the status bar | `tests/test_studio.py` (Linux; Windows `taskkill /T` path untested) |

## Not verified (no real files or tools available)

- **Any real Aniimo file.** Its container format, encryption, Unity version,
  naming conventions and prefab structure are unknown. The catalog's
  heuristics are tuned on synthetic data only and will need rule adjustments.
- Skinned-mesh fidelity through AssetRipper (the synthetic mesh has no
  weights), real animation curves, compressed or streamed textures (the
  synthetic textures are uncompressed RGBA32). Inventory time on a real
  32 GB install with LZ4-compressed bundles (synthetic bundles are
  uncompressed) is not measured yet.
- Whether Aniimo's creatures actually use `SkinnedMeshRenderer` (the targeted
  inventory's trigger). If a real install reports 0 containers with skinned
  meshes, run the inventory with `--full` and share the class counts.
- Blender conversion runs with the `bpy` module on synthetic data. A standalone
  Blender install and real Aniimo meshes are untested. Unreal import, cooking,
  packaging, UE4SS/PalSchema and in-game behaviour: **nothing has been run.**
- Palworld facts behind new Pals, spawning, save persistence and combat (see
  `architecture.md` §6 and §9.4).
- Both games' stat, element and work systems. The bundled system definitions
  come from fan and community wikis and are marked `unverified`. Several facts
  conflict, including Palworld's work-level range and the meaning of Aniimo's
  "tiers" (architecture §10.4).

## Blockers

| Blocker | Unblocks | Who can resolve |
|---------|----------|-----------------|
| Rights to use specific Aniimo assets | Any public release of ported creatures | Rights holder / user |
| Aniimo install for local scanning | Real-data tuning of inventory/catalog rules | User, locally |
| Encrypted or custom containers (if found) | Extraction of those files. **The toolkit will not bypass them** | Rights holder |
| Blender (4.x/5.x) on the user's machine, or `pip install bpy` on a matching Python | Running `creature convert` locally | User |
| Windows + UE 5.1 + PalworldModdingKit toolchain | Milestones 5–7 | User |
| Palworld + Palworld RE-UE4SS + PalSchema | In-game testing (M5–M7) | User |

## What to run locally now (please share the outputs, not game files)

```bash
pip install -e ".[unitypy]"
aniimo-pal scan "<Aniimo dir>" -o workspace/manifests/aniimo.json --label "Aniimo <version>"
aniimo-pal summary workspace/manifests/aniimo.json --json
aniimo-pal inventory workspace/manifests/aniimo.json "<Aniimo dir>" -o workspace/objects.json
aniimo-pal catalog build workspace/objects.json -o workspace/catalog.json
aniimo-pal catalog list workspace/catalog.json
```

To find stat, element and job data, also run:
`aniimo-pal objects workspace/objects.json --class TextAsset --class MonoBehaviour --name "stat|attr|element|job|work|monster|pet"`
and share the list of names (not contents).

Then pick one species and run `creature init --from-catalog`, `creature review`,
`creature extract` and, with AssetRipper installed, `creature export-assetripper`.
Report:
- the encrypted, unknown and error counts from `summary`;
- the inventory summary line;
- how many species `catalog list` finds and whether names and variants look right;
- `creature review` output for one creature;
- whether its extracted PNGs, OBJ and AssetRipper GLBs look correct.

The inventory surveys every bundle and reads only bundles with creature meshes
(plus what they reference) in detail, in parallel, with a resume cache. Its
summary line ("containers ... with skinned meshes N, deep-read M") is the most
useful thing to share.
