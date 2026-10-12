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

Status: ✅ implemented and tested · 🟡 partial · ⬜ planned

```
src/aniimo_pal/
  cli.py                  ✅ argparse CLI (the future GUI calls the same functions)
  config.py, log.py       ✅ TOML config with strict keys; console + JSON-lines logging
  scanner/                ✅ M1: read-only walk, hashes, format detection, Unity headers, encryption flags
  manifest/               ✅ M1: manifest schema, validation, diff, verify; shared atomic JSON writer
  adapters/
    unitypy_adapter.py    ✅ M2: object inventory (classes, names, container paths, PPtr refs, class facts)
    assetripper.py        ✅ M2: headless session (launch, load, export primary/project, completion markers)
    external.py           🟡 Blender / Unreal Editor discovery only
  inventory.py            ✅ M2: manifest -> objects.json (hash re-check, encrypted files never opened)
  catalog/                ✅ M3: dependency graph -> species, variants, skeleton, animations, evidence
  creature/               ✅ creature.json model, workflow (from catalog, review, associate), variants, combat schema
  extract.py              ✅ M2: UnityPy export (PNG/OBJ/JSON) + AssetRipper export mapped back to assets
  convert/                ⬜ M4: Blender scripts (GLB->FBX, axis/scale, bone rename/retarget, .anim->FBX animation)
  unreal/                 ⬜ M5: UE 5.1 Python import script(s), run inside the editor
  modkit/                 ⬜ M5/M6: Palworld mod scaffold, PalSchema JSON, packaging metadata
  combat/                 ⬜ M7: combat discovery + translation templates (schema already reserved)
  studio/                 ⬜ M8: GUI shell (guided workflow, 3D preview, combat mapping)
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

## 4. Automatic asset association (implemented in milestone 2/3)

Unity data holds explicit references: GameObject → components (Transform,
SkinnedMeshRenderer, Animator), SkinnedMeshRenderer → Mesh, Materials and bone
Transforms, Material → Textures, Animator → Avatar and AnimatorController →
AnimationClips. Association is therefore **graph-first and inference-second**.
Every association records `evidence` and `provenance`:

| Step | How | Provenance |
|------|-----|-----------|
| Object index | `inventory` lists every object and its PPtr references, resolving cross-bundle references through each file's externals (CAB names) | — |
| Creature candidates | Root GameObjects whose hierarchy contains a SkinnedMeshRenderer (MeshRenderer optional via rules) | verified |
| Species grouping | Prefabs sharing a mesh object, or (fallback) identical skinned bone paths + the same name stem | verified / inferred |
| Assets | Meshes, materials, textures (with slot names), Avatar and controller reached through references | verified |
| Variant labels | Rule keywords in the prefab name, asset path or material name; extra keyword-less prefabs become `unclassified` | inferred |
| Variant attribution | Assets used only by one variant's prefab belong to that variant, otherwise `shared` | verified graph + inferred label |
| Skeleton | Transform hierarchy under the prefab root, with local TRS and a skinned flag per bone | verified |
| Animations | Controller-linked clips (verified). Unlinked clips are matched when enough of their bound transform-path CRC32 hashes exist in the skeleton (`min_binding_matches`, `min_binding_ratio`) | verified / inferred |
| Clip roles | Name keywords (idle/walk/attack/ultimate/…) | inferred, advisory only |

`creature init --from-catalog` auto-confirms **verified** associations, unless
`--propose-all` is passed. Everything inferred stays `proposed` until the user
confirms or rejects it (`creature review` / `creature associate`; later in the
studio). Keywords and thresholds live in a rules TOML
(`config/catalog_rules.toml`, copy to `<workspace>/rules/`), so a ruleset tuned
on one creature is reused for the next.

## 5. Intermediate editor (studio)

Requirements: preview the mesh, play animations, show the skeleton, check
material/texture slots, edit the bone mapping to the target Palworld skeleton
(replace mode) or validate a new skeleton (new_pal mode), assign animations to
Palworld roles (idle, walk, run, attack, …), and switch between variants.

The studio also gets a **Combat Mapping** panel (§9): it lists discovered
attacks and the Ultimate, previews clips and their events, lets the user assign
clips to Palworld skill slots and the Partner Skill, and sets parameters,
effects, AI rules and the interaction profile. It shows provenance for every
value.

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

## 9. Combat translation (core requirement; milestone 7)

Aniimo lets the player transform into a creature and use its attacks and
Ultimate. Palworld drives Pals through AI, active skills, partner skills and
(for some Pals) mounted combat. The toolkit must **translate** a creature's
combat identity into Palworld mechanics, not just import attack animations.

### 9.1 Data model (reserved now in `creature.json` → `combat`)
- `profiles`: integration profiles from `ai_companion`, `partner_skill`,
  `mounted` and `player_ability`. A creature is never made rideable by default.
- `abilities[]`: `id`, `kind` (basic, special, ultimate, movement, passive,
  transformation, support), `provenance`, ordered `phases[]` (each references
  one of the creature's animation assets, so **multi-stage** sequences are
  native), `effects` (vfx/sfx/projectiles, as asset references), `parameters`
  (each value carries its own provenance), and `palworld_mapping`
  (target + review status).
- `ultimate`: `{ability, target}` with **`target: "partner_skill"` by default**.
- Variants inherit the base combat config. Per-variant `combat_overrides` need
  `verified` or `user` provenance.
- Validation is structural only for now (`aniimo-pal creature validate`).
  Missing facts stay absent and are never invented.

### 9.2 Discovery (verified vs inferred)
- **Verified today:** AnimationClip events (time, function name, parameters)
  are captured in the inventory and in extracted clip JSON. They are the
  primary source for hit frames and VFX/SFX trigger timing.
- **Planned:** controller state-machine structure (state names, transitions →
  combo order), MonoBehaviour skill/config data (needs type trees or script
  metadata; only where the game data is readable), and projectile/VFX prefabs
  referenced from skill data. Each fact is tagged `verified`. Name-based
  guesses (clip `role_guess`) stay `inferred` and advisory.

### 9.3 Ultimate → Partner Skill framework (design decision)
The Aniimo Ultimate becomes the creature's **unique Palworld Partner Skill**,
preserving its cinematic identity:
- The original multi-phase clip sequence plays in order (windup → active →
  recovery …). It is never swapped for a generic Palworld animation unless
  technically unavoidable, and if it is, that is recorded as a limitation.
- Damage, VFX, SFX and projectile spawns are synchronised to the clip's own
  animation events (mapped to Unreal anim notifies at import).
- Transformation-specific camera, targeting, movement and input are adapted
  to Partner Skill activation (player key press). AI-usage rules and
  cooldowns are configurable.
- Implemented as reusable **templates** (melee, ranged, projectile, AoE,
  charge, multi-hit combo, buff/debuff, heal/support, movement, ultimate)
  parameterised per creature. No per-creature code.

### 9.4 What must be verified before implementation (milestone 7 research)
From community sources **[source, not tested]**: active-skill data lives in
`DT_WazaDataTable_Common` (cooldown field `CoolTime`), and partner skills in
`DT_PartnerSkill` / `DT_PartnerSkillParameter`. Partner skills are passive
or active; active ones are triggered by the Partner Skill key and gated by
technology. Internal IDs can change between game patches.

**Unknown:**
- whether a new Pal can get a *new* partner skill (not a reused one) through
  PalSchema rows alone, or whether Blueprint/C++ work in the modding kit is
  needed;
- how Pal attack montages and notifies are driven;
- whether custom AI usage rules are possible.

Milestone 7 starts with a spike: one creature, one basic attack, one
multi-phase partner skill, tested in game. Static validation (references,
cooldown ranges, missing clips or effects) is kept separate from in-game
verification in every report.

