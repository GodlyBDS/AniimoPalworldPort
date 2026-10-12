# Document formats (manifest, objects, catalog, creature)

## Manifest (`aniimo-pal/manifest`, schema 1.0.0)

Produced by `aniimo-pal scan`. JSON Schema: `src/aniimo_pal/schemas/manifest.schema.json`.

```jsonc
{
  "schema": "aniimo-pal/manifest",
  "schema_version": "1.0.0",
  "generator": {"name": "aniimo-pal", "version": "0.2.0"},
  "created_utc": "2026-10-12T09:00:00+00:00",
  "duration_seconds": 1.234,
  "source": {"root_label": "Aniimo", "root_path": null, "host_platform": "Windows"},
  "settings": {"hash_algorithms": ["sha256"], "follow_symlinks": false, "include_hidden": false,
               "exclude": [], "probe_bytes": 65536, "high_entropy_threshold": 7.5},
  "summary": {"file_count": 18, "total_bytes": 9123, "skipped_count": 0, "error_count": 0,
              "warning_count": 2, "supported_count": 9, "encrypted_count": 1,
              "by_kind": {"unity_bundle": 3}, "by_category": {"unity_container": 6},
              "bytes_by_category": {"unity_container": 4096}, "unity_versions": {"2021.3.15f1": 4}},
  "files": [
    {
      "path": "Game_Data/StreamingAssets/creatures/creature_a.bundle",   // relative POSIX path
      "size": 512,
      "mtime_utc": "2026-10-12T08:59:00+00:00",                          // omitted with --no-mtime
      "hashes": {"sha256": "…"},
      "kind": "unity_bundle",            // see `aniimo-pal formats`
      "category": "unity_container",
      "supported": true,                 // a pipeline route exists AND not encryption-flagged
      "route": "adapter:unitypy|assetripper",
      "confidence": "high",              // high | medium | low
      "entropy": 5.12,                   // bits/byte over the probe
      "details": {"signature": "UnityFS", "format_version": 8, "unity_version": "2021.3.15f1",
                  "flags_hex": "0x40", "flag_layout": "post-2020.3.34", "encryption_flag": false, "…": "…"},
      "warnings": [],
      "error": null,                     // set when the file could not be read
      "relations": [{"type": "resource_of", "path": "…"}]   // optional
    }
  ],
  "skipped": [{"path": ".git/", "reason": "hidden"}]
}
```

Guarantees checked by `aniimo-pal validate`:
- Matches the JSON Schema (when `jsonschema` is installed; basic checks otherwise).
- Paths are unique, sorted, relative and POSIX, with no `..`.
- Every readable file has exactly the configured hash algorithms.
- The `summary` equals a recomputation from `files` and `skipped`.

`details` fields by kind:
- `unity_bundle`: `signature`, `signature_offset`, `format_version`,
  `player_version`, `unity_version`, `declared_size`, block-info sizes and
  compression, `flags`, `flags_hex`, `flag_layout`
  (`pre-2020.3.34` | `post-2020.3.34` | `ambiguous`), `encryption_flag`
  (`true` | `false` | `null` = ambiguous).
- `unity_serialized`: `format_version`, `metadata_size`, `data_offset`, `endianness`, `unity_version`.
- `unity_yaml`: `class_ids_in_probe` (Unity class ID → name and count).
- `unity_meta`: `guid`. Textures: `width`/`height` (+ `fourcc`, `mip_count`, …). `model_fbx`: `encoding`, `fbx_version`.

## Object index (`aniimo-pal/objects`, schema 0.2.0)

Produced by `aniimo-pal inventory`. One record per Unity object that the
creature pipeline uses (GameObject, Transform, renderers, Mesh, Material,
Texture2D, Shader, AnimationClip, Animator/controllers, Avatar, MonoBehaviour,
TextAsset, AssetBundle; `--all-objects` records every class):

```jsonc
{"id": "cab-foxlet:47",            // <serialized-file name, lowercased>:<path id>
 "cab": "cab-foxlet", "path_id": 47, "container": "StreamingAssets/foxlet.bundle",
 "class": "Material", "class_id": 21, "name": "Foxlet_Mat",
 "asset_path": null,                // AssetBundle container path, e.g. assets/creatures/foxlet/foxlet.prefab
 "refs": [{"field": "m_SavedProperties.m_TexEnvs[0][1].m_Texture", "target": "cab-foxlet:45"}],
 "info": {"texture_slots": ["_MainTex", "_BumpMap"], "colors": {}, "floats": {}},
 "error": null}
```
`info` per class: Texture2D size/format (`streamed` is `null` in the
inventory: pixel data is not read); Mesh vertex/submesh counts and bone-name
hashes; AnimationClip binding path hashes, sample rate, length and **events**;
Avatar bone paths; Transform local TRS; TextAsset size and content kind.
`containers[]` lists each file with its hash, serialized files (CAB names,
externals), `class_counts` (every class, recorded or not) and `read`:
`deep` (objects recorded), `survey` (table of contents only) or `none` (error).
`skipped[]` lists encryption-flagged or unsupported files that were not opened.

**Strategies.** `targeted` (default): every container is *surveyed* (type
table, dependencies, asset paths and TextAssets, no object decoding); only
containers holding a `SkinnedMeshRenderer`, plus the containers their objects
reference (followed for up to 8 rounds), are read in detail. Surveyed
containers contribute their TextAssets (data-table discovery) and class
counts. `--full` reads every container in detail (needed for
`include_static_meshes` catalogs). Texture pixels and mesh vertex buffers are
never decoded during the inventory.

**Speed and resume.** Files are checked against the manifest by size and
modification time (`--verify-hashes` re-hashes, `--no-verify` skips the check).
Containers are read by parallel worker processes (`--workers`, default CPU
count − 1, at most 8), and each finished container is cached in
`inventory-cache/` next to the output, keyed by its content hash. An
interrupted or repeated run continues from the cache. Progress lines
`[done/total] phase: file (elapsed, ETA)` are printed as it goes.

## Catalog (`aniimo-pal/catalog`, schema 0.1.0)

Produced by `aniimo-pal catalog build`. `species[]` entries contain: `key`,
`suggested_id`, `display_name`, `grouping_evidence`, `prefabs[]` (object,
name, asset path, `variant` + `variant_provenance`/`variant_evidence`),
`skeleton` (bones with path, parent, skinned flag and local TRS; `skins[]` with each
skinned renderer's mesh and ordered joint paths),
`assets[]` (object, container, sha256, role, variant, slot, evidence,
provenance), `animations[]` (object, `linked_via` controller|binding-match,
`binding_overlap` [matched, total], `role_guess` (inferred), `event_count`),
and `warnings[]`. The `rules` used are embedded for reproducibility.
`summary` adds `ambiguous_clip_count` (unlinked clips whose bindings fit more
than `max_binding_match_species` skeletons, e.g. shared humanoid rigs, and are
therefore not assigned) and `diagnostics` (renderers found, with mesh, with
GameObject, in a hierarchy, root GameObjects, and the serialized files holding
meshes missing from the object list) to explain small or empty catalogs.

## Creature project (`aniimo-pal/creature`, schema 0.3.0, may change)

0.3.0 replaces the mutually exclusive `variants` with the identity model
(architecture.md §6). 0.1/0.2 files are migrated automatically on load:

```jsonc
"variants": {
  "regions": {"forms": [{"id": "snow", "name": "Snow", "biomes": ["<palworld biome id>"], "provenance": "verified",
                         "allow_prismana": true, "appearances": null}],
              "inheritance": null},
  "forms": {"normal": {"enabled": true}, "prismana": {"enabled": true}},
  "shiny": {"enabled": true, "appearances": [{"id": "shiny_01", "name": "Shiny 1", "weight": 1.0}, "… 12 total"]},
  "rarity": {"wild": {"prismana_probability": 0.0002, "shiny_probability": 0.002},
             "egg":  {"prismana_probability": 0.0,    "shiny_probability": 0.002},
             "allow_guaranteed": false, "pity": null,
             "policy": {"min_shiny_to_prismana_ratio": 2.0, "max_shiny_probability": 0.01,
                        "expected_shiny_appearances": 12}}
},
"evolution": {"line": "jawling", "stage": 2, "previous": ["aniimo_jawling"], "next": ["aniimo_infergon"],
              "provenance": "inferred"}
```
Assets carry `region` (`any`, `base` or a regional id), `form` (`shared`,
`normal`, `prismana` or `unclassified`) and `appearance` (`any`, `standard`,
`shiny` or a Shiny id) in place of 0.2's single `variant`.

0.2.0 added (all optional, 0.1.x files remain valid): `prefabs`, `skeleton`,
`notes`, `history`; per-asset `class`, `name`, `slot`, `provenance`
(verified|inferred|user), `extracted`, `assetripper`; the `unclassified`
asset variant (must be resolved before `mapped`); the reserved `combat`
section (see architecture.md §9); and `source_profile` / `translation` /
`profile_overrides` (on regional forms, the Prismana form and Shiny) for stats, elements and work (architecture.md
§10). Example:

```jsonc
"source_profile": {"system": {"id": "aniimo", "version": "0.1.0"},
  "stats": {"hp": {"value": 80, "provenance": "verified", "source": "cab-x:12 m_Hp"}},
  "elements": [{"id": "earth", "provenance": "verified"}, {"id": "wind", "provenance": "verified"}],
  "work": [{"id": "earth_job", "level": 2, "provenance": "verified"}],
  "tiers": {"job_level": {"value": 2, "meaning": null, "provenance": "verified"}}},
"translation": {"target_system": {"id": "palworld", "version": "0.1.0"},
  "profile": {"id": "faithful", "rules_version": null},
  "stats": {"hp": {"value": 75, "auto_value": 75, "from": ["hp"], "method": "user", "overridden": true}},
  "elements": [{"id": "ground", "from": ["earth"], "method": "user"}],
  "unmapped_elements": ["wind"],
  "work": [{"id": "mining", "level": 2, "from": ["earth_job"], "method": "user"}]}
``` Status order:
`draft → mapped → extracted → converted → configured → validated → exported`.
`mapped` requires every non-rejected association to be confirmed.

Created with `aniimo-pal creature init <workspace> --id <id> --name <name>`.

```jsonc
{
  "schema": "aniimo-pal/creature",
  "schema_version": "0.1.0",
  "id": "aniimo_example",                 // ^[a-z][a-z0-9_]{2,47}$
  "display_name": "Example",
  "status": "draft",                      // draft→extracted→mapped→converted→configured→validated→exported
  "source": {"manifest_sha256": null, "game_version": null},
  "assets": [
    {"ref": {"path": "…/creature_a.bundle", "sha256": "…", "object": "optional object path/name in container"},
     "role": "mesh",                      // mesh|skeleton|texture|material|animation|icon|audio|other
     "region": "any", "form": "shared", "appearance": "any",   // identity scope (0.3.0)
     "association": "proposed",           // proposed|confirmed|rejected (confirmed requires sha256)
     "evidence": ["dependency:prefab", "name-stem"]}
  ],
  "variants": "(0.3.0 identity model, see above)",
  "integration": {"mode": "replace", "replace_target": null, "notes": ""},
  "combat": {
    "profiles": ["ai_companion"],
    "abilities": [{"id": "ult", "kind": "ultimate", "provenance": "inferred",
                   "phases": [{"clip": "cab-x:12", "label": "windup"}, {"clip": "cab-x:13", "label": "active"}],
                   "effects": {"vfx": ["cab-x:40"]},
                   "parameters": {"cooldown_s": {"value": 30, "provenance": "user"}},
                   "palworld_mapping": {"target": "partner_skill", "status": "proposed"}}],
    "ultimate": {"ability": "ult", "target": "partner_skill"}
  }
}
```

Variant rules enforced by `aniimo-pal creature validate`: Normal is always
enabled; an enabled rare variant needs a probability in (0, 1]; a disabled one
must have probability 0; combined rare probability ≤ 1; assets can't be assigned
to a disabled variant.
