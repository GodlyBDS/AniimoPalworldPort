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
  convert/                🟡 M4: .anim parser, skin assembly, Blender job (rigged FBX + retargeted clips) ✅;
                             materials/textures in FBX, Palworld-skeleton retarget, blend shapes ⬜
  unreal/                 ⬜ M5: UE 5.1 Python import script(s), run inside the editor
  modkit/                 ⬜ M5/M6: Palworld mod scaffold, PalSchema JSON, packaging metadata
  combat/                 ⬜ M7: combat discovery + translation templates (schema already reserved)
  systems.py              ✅ versioned game-system vocabularies (bundled Aniimo/Palworld defs, unverified)
  creature/profile.py     ✅ source_profile / translation schema, variant inheritance, warnings
  translate/              ⬜ M8: stat, element and work translation engine + balancing presets
  studio/                 ⬜ M9: GUI shell (guided workflow, 3D preview, combat + stats/work panels)
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

The studio also gets a **Stats & Work Suitability** panel (§10). It shows
original vs translated values per variant, element mappings, editable work
categories and levels, balancing presets, reusable profiles, reset-to-auto, and
warnings for invalid or unusually strong configurations. It also gets a
**Combat Mapping** panel (§9): it lists discovered
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

## 6. Identity, variants, rarity, regional forms and persistence

### 6.1 Identity model (five independent properties)
Every individual creature has an identity built from properties that combine
freely. These are never separate, mutually exclusive creature definitions:

| Property | Values | Decided by |
|----------|--------|-----------|
| Species | the creature project (`id`) | Palworld's spawn rules (step 1) |
| Evolution stage | `evolution.stage`, one project per stage, linked by `previous`/`next` | source data |
| Regional form | `base` or a regional form id (e.g. `snow`) | the spawn **biome** (step 2), not a roll |
| Special form | `normal` or `prismana` | independent **form roll** (step 3) |
| Appearance | `standard` or one of the Shiny appearances (12 per species by the brief) | independent **Shiny roll** (step 4) + equal-weight pick (step 5) |

Example: a Snow regional form can also be Prismana with Shiny appearance 7,
if the source data provides those assets.

### 6.2 Spawn/hatch selection order (reference: `creature/variants.py`)
1. Palworld selects the species.
2. `region_for_biome` → regional form (`base` if no regional form claims the biome).
3. Prismana roll: `u_form < p_prismana`.
4. Independent Shiny roll: `u_shiny < p_shiny`.
5. If Shiny: pick an appearance by weight (equal by default).
6. Resolve assets for the identity and spawn **exactly one** individual.

Rare outcomes change that one individual and never add copies.
`select_individual` / `roll_individual` are the specification and test oracle
for the in-game implementation.

### 6.3 Rarity rules
- **P(Prismana Shiny) = p_prismana × p_shiny.** It is computed, never
  configured, so it is always rarer than either alone. Example from the brief:
  0.1 % × 1 % = 0.001 % ≈ 1 in 100,000.
- Separate `wild` and `egg` probabilities. Prismana is primarily wild: the egg
  default is 0, and an egg chance above the wild chance is an error.
- Validation errors: Prismana not rarer than Shiny in a context; a probability
  of 1 without explicit `allow_guaranteed`; a configured Prismana-Shiny
  probability; enabled variants with zero chance.
- Policy warnings (per-creature `variants.rarity.policy`, documented defaults):
  Shiny above 1 % per roll is not "genuinely rare"; Prismana less than 2× rarer
  than Shiny; Prismana possible from eggs; a Shiny appearance count other than
  12; unequal appearance weights; any explicit pity/guarantee setting.
- **No pity system and no guaranteed rare outcomes** unless the user enables
  them explicitly. The reference roller implements neither.
- Defaults written by `creature init --from-catalog` are placeholders, not
  balancing decisions: wild Shiny 1/500 and Prismana 1/5,000 (Prismana Shiny
  1 in 2.5 million); egg Shiny 1/500 and Prismana 0.
- `aniimo-pal creature roll` prints exact odds ("1 in N") per context and
  region, next to a simulation.

### 6.4 Regional forms
- Declared in `variants.regions.forms[]`: `id`, `name`, target-game `biomes`,
  `provenance` (**verified or user only**, so regional forms are never
  invented), optional `allow_prismana`, an optional `appearances` subset
  (which Shiny appearances exist for that region), and optional
  `profile_overrides`/`combat_overrides` (stats, elements, abilities, work).
- One biome maps to at most one regional form. Assets carry a `region` scope
  (`any`, `base` or a regional form id).
- Breeding inheritance is configured by `variants.regions.inheritance.mode`
  (`parent`, `biome` or `configurable`). Semantics are defined in milestone 6.
- Still to build: catalog detection of regional prefabs from real data
  (milestone 3 extension, rule-driven), per-region conversion (milestone 4),
  Palworld biome ids and spawner placement (milestone 6), and the editor panel
  (milestone 9).

### 6.4b Cosmetic skins (gacha skins) and special palettes

Aniimo also has cosmetic **skins** (a gacha system). Unlike Shiny looks (colour
palettes only, same mesh) a skin can change the model: Irisalis's skins add
wings. Skins are therefore a separate dimension, **not part of the rarity
roll**:

* `variants.skins.entries[]`: every skin found in the source data or entered by
  the user (`id`, `name`, `source` e.g. gacha/event, `meshes`, `materials`,
  `prefabs`, `provenance`, `enabled`). Nothing is invented.
* `variants.skins.policy`: the user's decision per creature: `base_only`,
  `all`, or `selected` (only entries with `enabled: true`, e.g. the four special
  skins). `null` means undecided: no skin is exported and validation warns.
* How a skin is obtained in Palworld (cosmetic choice, separate Pal, spawn
  variant) is decided with spawning (milestone 6).
* `aniimo-pal creature skins FILE` lists and edits them.

Umbral (galaxy-purple palette, a 13th look besides the 12 Shiny palettes) and
Dazzling/Shadow are recorded in `data/systems/aniimo.toml` as user-provided
facts; their rarity rules are pending (see the open questions there).

### 6.5 Override layering
Stats and combat resolve as: base → regional-form overrides → Prismana
overrides → Shiny overrides, each needing `verified` or `user` provenance.
Shiny is cosmetic unless the data says otherwise. Rarity, stats/work and
combat stay in separate sections and never leak into each other.

### 6.6 Persistence (in-game requirement, unverified)
The full identity (species, stage, region, form, appearance) must survive
capture, Palbox storage, save/load, breeding and evolution. A captured Snow
Pal stays Snow in any biome. Implementation options, to be verified in
milestone 6 against how Palworld saves a Pal:

| Option | Persistence | Cost / risk |
|--------|-------------|-------------|
| A. One character row per (region, form) with appearance as a material variant | Region and form persist via the CharacterID; the appearance index still needs storage | Few rows; needs one stored index |
| B. One character row per full identity | Everything persists via the CharacterID | Up to regions × 2 × 13 rows per species; Paldeck/breeding fragmentation |
| C. One row + identity fields in custom save data | Needs save hooks | Fragile across game updates |

Note: vanilla Palworld has no evolution mechanic **[unverified]**, so Aniimo
evolution lines need their own design. The identity must carry across a stage
change whenever the next stage has assets for it.

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


## 10. Stats, element types and work suitability translation (core requirement; milestone 8)

Goal: a ported Aniimo keeps its **identity** in Palworld, so a fast, fragile
attacker stays one and a tank does not become a damage dealer. This means
translating *relative* strengths, elemental identity and work specialisation,
not copying numbers between two unrelated scales.

### 10.1 Independent layers (each in its own place in `creature.json`)

| Layer | Where | Notes |
|-------|-------|-------|
| Original Aniimo stats, elements, work/job levels, tiers, stage, role | `source_profile` | Read from game data (provenance `verified`) or entered by the user. Never rewritten by translation. |
| Palworld stats, elements, work suitability | `translation` | Each value records `from` (source ids), `method` (rule id or `user`), `auto_value`, `overridden` |
| Balancing preset + rules version | `translation.profile` | `faithful`, `vanilla_balanced` or `custom` |
| Variant-specific differences | `variants.regions.forms[].profile_overrides`, `variants.forms.prismana.profile_overrides`, `variants.shiny.profile_overrides` | `verified`/`user` provenance only; Normal is the base |
| Spawn rarity | `variants.rarity` (wild/egg) | Rejected inside profile overrides |
| Combat | `combat` | Rejected inside profile overrides |

The vocabularies (element ids, stat ids, work categories, level ranges) come
from **versioned game-system definitions** (`aniimo_pal/data/systems/*.toml`,
`aniimo-pal systems`). Validation rejects anything not in the target
vocabulary, so **unsupported element types are never invented**. A creature
records the system id and version it used, so updated definitions or rules do
not invalidate earlier imports. Supply confirmed definitions with
`creature validate --source-system/--target-system`.

### 10.2 Implemented now (milestone-2 scope)
- Schema and validation for `source_profile`, `translation` and
  `profile_overrides`. Every value carries provenance. Each source element must
  be either mapped or listed in `translation.unmapped_elements`. Work levels
  are checked against the target system's range. Rarity and combat keys are
  rejected inside stat overrides.
- `effective_profile(creature, variant)`: Normal = base; Shiny and Prismana
  inherit it plus any verified/user overrides.
- Warnings (non-fatal): unverified system definitions; translated stats without
  any verified source stat; tiers with no documented meaning; rare variants
  whose work suitability is all at the maximum level.
- Discovery aid: the inventory records TextAsset size and content type (json,
  csv, xml, text, binary). `aniimo-pal objects` searches the object index by
  class and name, to find stat, element and work tables in real data.

### 10.3 Translation engine (milestone 8, after real data is available)
Planned approach, written down now so it stays explainable:
- **Normalise** each source stat to a percentile within the source population
  (all catalogued Aniimo at the same stage), so values are relative.
- **Faithful:** map percentiles onto the target stat's distribution across
  Palworld's species, using documented monotonic curves.
- **Vanilla balanced:** match the creature to reference Pals with a similar
  role and profile, and take their value bands.
- **Custom:** user-editable curves and caps.
- Elements: a versioned mapping table supporting one-to-one, many-to-one and
  explicit "unmapped" entries, with gameplay-based (not visual) rationale per
  entry. Visual colour is never evidence.
- Work: Aniimo job categories map to Palworld work categories via a table.
  Levels convert through a configurable table, only after the meaning of
  Aniimo's levels is verified. Unrelated categories are never added.
- Every output keeps `auto_value`, so the studio can reset to defaults after
  manual edits.

**None of these formulas are implemented.** Both games' systems must be
confirmed from data first (see 10.4).

### 10.4 Unknowns that need real game data
- Where Aniimo stores species stats, elements and job data: TextAsset tables,
  ScriptableObjects (MonoBehaviour) or server-side only. Use `objects --class
  TextAsset/MonoBehaviour`.
- Aniimo stat semantics and scaling with level/stage. Guides list HP, ATK, P.DEF,
  M.DEF, BREAK and REGEN, and say progression changed after the beta.
- **The meaning of "tiers 1–4" and the Prismana link.** Guides describe Homeland
  *job levels* 1–4 (another lists stations at Lv 1–3). They also report that
  Prismana forms **do not change stats** but often change element (and gain
  Light). So "tier 4 = Prismana" is unconfirmed, and Prismana should differ
  through verified element/skill overrides, not stat boosts.
- The Aniimo job list: guides mention 13 jobs (nine element jobs + four general)
  without consistent names.
- Palworld work-level range for the target version: wiki.gg says natural 1–8
  and maximum 10; other guides say 1–4. Also Palworld's internal stat fields
  and how DataTable rows encode elements and work (inspect
  `DT_PalMonsterParameter` with FModel or the modding kit).
- Element gameplay: Aniimo uses 1.6×/0.625× multipliers with no immunities and
  self-resistance; Palworld uses 2×/0.5× plus a 20 % same-element bonus. Wind
  and Light have no Palworld element; Neutral and Dragon have no Aniimo
  element.

## 11. Conversion pipeline (milestone 4)

`aniimo-pal creature convert` turns a creature's confirmed assets into a rigged
FBX for Unreal Engine 5.1. Each step writes inspectable files under
`<creature>/convert/`.

1. **Clips:** AssetRipper's Unity-project export rebuilds editor curves from a
   build's compiled clips as `.anim` YAML. `convert/unity_anim.py` parses them
   into `aniimo-pal/clip` JSON in **Unity local space** (positions, quaternions,
   euler and scale keys with Hermite tangents, float curves, events). It has a
   reference evaluator.
2. **Skin assembly** (`convert/skin_assembly.py`): AssetRipper does not export
   skinned prefabs as rigs (research §1). The mesh GLB (weights, no skin) is
   combined with the catalog skeleton (`skeleton.bones` local TRS), the
   renderer's joint order (`skeleton.skins[].bones`, i.e. `m_Bones`) and the
   mesh's real bind poses (`*.skin.json` from the UnityPy extract). Joints and
   inverse bind matrices use AssetRipper's coordinate conversion, so the mesh
   and rig line up.
3. **Blender job** (`convert/blender_scripts/convert_model.py`, standalone):
   - imports the rigged glTF;
   - fits the Unity→armature basis from rest-pose bone positions (the expected
     AssetRipper basis first, a least-squares fit as fallback; residual
     reported);
   - retargets each clip with P = A·U_anim·U_rest⁻¹·A⁻¹·B_rest, which does not
     depend on how the importer orients bones;
   - exports FBX with UE-oriented settings: unit scale to centimetres, no leaf
     bones, armature named `Armature`, every action as its own take, no
     keyframe simplification;
   - writes a report: skinning coverage, bounds sanity check, per-clip error,
     warnings.

Verification so far (all on synthetic data):
- FBX export + re-import reproduces animated bone positions computed
  independently in Unity space (≤ 0.1 mm).
- Rigged vertices sit on their bones.
- The whole CLI chain runs against a real AssetRipper build.

Not covered yet: real Aniimo meshes (multiple skinned meshes per prefab,
compressed or streamed vertex data, blend shapes, LODs), materials and textures
inside the FBX, retargeting onto a Palworld skeleton (replace mode), and Unreal
import.

