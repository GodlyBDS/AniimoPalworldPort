# Architecture

## 1. Goal and the end-state experience

The goal is a **creature porting studio**: the user picks an Aniimo, and the tool
does the rest:

```
catalog ─► extract ─► associate ─► convert ─► inspect/correct ─► configure ─► variants ─► validate ─► build mod ─► test in game
```

The user ports **one creature at a time**. Everything they decide is saved and
reused, so the next creature costs less work than the last. Replacing an existing
Pal is an acceptable early milestone. The real target is **new, independent Pals**
whose Normal, Shiny and Prismana variants all belong to one species.

Design rules that every module follows:

1. **Modular stages.** Extraction, identification, conversion, inspection, variant
   management and Palworld integration are separate packages. They pass files and
   JSON documents to each other, not in-memory objects. Any stage can be re-run,
   replaced or driven from a GUI.
2. **No hard-coded creature or asset layout.** Asset association is a ruleset plus
   evidence. It is never a fixed path list.
3. **Inputs are read-only. Outputs go to a workspace.** Nothing is ever written
   into the game folder or the repository.
4. **Content-addressed references.** Assets are referenced by `sha256` plus path,
   so saved work survives re-scans and game updates. A changed hash forces
   re-confirmation.
5. **Wrap mature tools** (AssetRipper, UnityPy, Blender, Unreal) through adapters.
   Do not re-implement parsers.
6. **No protection bypass.** Encrypted or obfuscated content is detected, reported
   and skipped (`legal-and-safety.md`).

## 2. Package map

Status: ✅ implemented and tested · 🟡 interface/discovery only · ⬜ planned

```
src/aniimo_pal/
  cli.py                  ✅ argparse CLI (future GUI calls the same functions)
  config.py               ✅ TOML config, strict keys, defaults = dataclasses
  log.py                  ✅ console + JSON-lines logging
  scanner/                ✅ milestone 1
    unity_headers.py         UnityFS/legacy/SerializedFile header parsing, encryption flag
    formats.py               format registry (kind, category, pipeline route) + detection
    scanner.py               read-only walk, single-pass hash + probe, relations
  manifest/               ✅ milestone 1
    model.py, io.py          schema id/version, summary, atomic write, validation
    diff.py, verify.py       game-update diff, on-disk verification
  schemas/                ✅ JSON Schema for manifests
  creature/               ✅ (data model only) per-creature project + variant roll reference
  adapters/               🟡 discovery for UnityPy, AssetRipper, Blender, Unreal Editor
  catalog/                ⬜ creature catalog built from container object listings
  identify/               ⬜ asset association engine (rules + evidence + confirmations)
  convert/                ⬜ mesh/texture/material/skeleton/animation stages
    blender_scripts/         ⬜ run inside Blender (GLB to FBX, axis/scale, bone rename, bake)
  unreal/                 ⬜ UE 5.1 Python import script(s) (run inside the editor)
  modkit/                 ⬜ Palworld mod scaffold, PalSchema JSON, packaging metadata
  studio/                 ⬜ GUI shell (guided workflow) + 3D preview
```

## 3. Data flow and documents

```
game install (read-only)
   │  aniimo-pal scan
   ▼
manifest.json ─────────────┐  (file inventory: kind, hashes, Unity version, flags)
   │  catalog (UnityPy object listing / AssetRipper export index)
   ▼                       │
catalog.json               │  (candidate creatures: names, prefabs, meshes, skeletons, anim sets)
   │  user selects one     │
   ▼                       ▼
creatures/<id>/creature.json   (species, asset refs + association state, variants, integration)
   │  extract (adapter) → convert (Blender) → inspect/correct (studio + Blender)
   ▼
creatures/<id>/build/…       (FBX, PNG/TGA, material and bone-mapping JSON)
   │  unreal import (UE 5.1 + modding kit)
   ▼
mod project → cooked pak(s) + PalSchema JSON → test in Palworld
```

All JSON documents carry `schema` and `schema_version` fields. Readers refuse
documents with an unknown `schema`. Minor version bumps only add fields.

### Workspace layout (outside the repository, git-ignored if placed inside)

```
<workspace>/
  manifests/<game-version>.json        scanner output
  catalog/<game-version>.json          creature catalog (milestone 3)
  rules/                               reusable association rules, bone maps, material maps
  creatures/<id>/creature.json         one species = one project, resumable
  creatures/<id>/extract/              tool output (AssetRipper/UnityPy), never edited
  creatures/<id>/build/                converted, user-corrected assets
  packs/<pack-name>.json               multi-creature mod pack definitions (later)
```

## 4. Automatic asset association (milestone 3+)

Unity bundles hold explicit references (GameObject → SkinnedMeshRenderer → Mesh,
Materials → Textures, Animator → AnimatorController → AnimationClips, Avatar →
skeleton). Association is therefore **graph-based first and name-based second**:

1. **Dependency graph** from UnityPy/AssetRipper (PathID references, bundle
   dependencies). A creature's root prefab pulls in its subgraph.
2. **Evidence scoring** for edges the graph lacks: shared name stems, a shared
   bundle or directory, matching bone sets between a mesh and an animation, and
   texture-slot naming (`_albedo`, `_n`, `_mask`).
3. **Variant detection**: material/texture sets that differ while the mesh and
   skeleton stay the same are proposed as Shiny/Prismana candidates. The user
   confirms them; the tool never decides on its own.
4. Each proposal is stored in `creature.json` as `association: proposed` with an
   `evidence` list. The user confirms or rejects it in the studio. Confirmed
   patterns are saved to `rules/` and reused for the next creature.

## 5. Intermediate editor (studio)

Requirements: preview the mesh, play animations, show the skeleton, check
material/texture slots, edit the bone mapping to the target Palworld skeleton
(replace mode) or validate a new skeleton (new_pal mode), assign animations to
Palworld roles (idle, walk, run, attack, …), and switch between variants.

Recommended approach (to confirm in milestone 4):
- **Blender add-on as the deep editor.** Rigs, weights, retargeting, the action
  editor and FBX export already exist and run headless for automation.
- **Studio shell for the guided workflow.** A local app (Python backend + web UI
  with a glTF viewer such as three.js) handles catalog browsing, association
  confirmation, variant setup and validation, and opens Blender for heavy edits.
  The shell only reads and writes the JSON documents above, so CLI and GUI stay
  interchangeable.

## 6. Variants, rare spawning and persistence

Requirements: Normal/Shiny/Prismana are variants of one species. Shiny and
Prismana probabilities are configured independently. Exactly one variant comes
out of each spawn event (a rare variant **replaces** the normal individual), and
the variant survives capture, storage and save/load.

Implemented now (`creature/variants.py`): the **reference semantics**. One
uniform draw `u` is partitioned into `[0, p_prismana)` → Prismana,
`[p_prismana, p_prismana + p_shiny)` → Shiny, otherwise Normal. Validation
rejects a combined probability above 1. Tests check exclusivity and
frequencies. `aniimo-pal creature roll` lets users check their configured rarity.
The in-game implementation must match this function and is tested against it.

In-game implementation options (to verify against real game data in milestone 6):

| Option | Spawn exclusivity | Persistence | Risk |
|--------|-------------------|-------------|------|
| **A. Separate character row per variant** (`<id>`, `<id>_shiny`, `<id>_prismana`) sharing skeleton/animations, different materials | Native if a spawn group picks one weighted entry per individual; otherwise a per-spawn hook picks the row using the reference roll | Free: the save already stores the character ID | Paldeck, breeding and condenser treat variants as separate entries unless linked |
| B. One row + a variant flag on the individual | Needs a spawn hook | Needs save-data hooks: fragile across game updates | High |

Option A is the working assumption because persistence then needs no custom save
code. It is **unverified** until we confirm how Palworld stores a captured Pal's
identity.

## 7. Palworld integration modes

- `replace` (early milestone): swap the visuals of an existing Pal. The Aniimo
  mesh must be bound to (or retargeted onto) the target Pal's skeleton so the
  existing animation blueprint works. Packaged as a resource pak.
- `new_pal` (final goal): new character rows (PalSchema JSON where possible), a
  new skeletal mesh, skeleton and animations, an animation blueprint derived
  from a comparable Pal, icon, Paldeck entry and spawner entries. Packaged as a
  LogicMod or resource pak with a unique chunk ID.

## 8. Community readiness (later)

- Mod packs: `packs/<name>.json` lists creature IDs + versions and builds one mod.
  Compatibility checks look for duplicate IDs, chunk-ID clashes and asset-path
  overrides.
- Distribution of the **tool** only: users run it on their own legally obtained
  game install. Creature projects and builds hold no game data until the user
  runs extraction locally.
- Packaging: `pip install` today; a frozen desktop build (e.g. PyInstaller) when
  the studio exists.
