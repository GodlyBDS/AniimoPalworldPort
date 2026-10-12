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

## Object index (`aniimo-pal/objects`, schema 0.1.0)

Produced by `aniimo-pal inventory`. One record per Unity object:

```jsonc
{"id": "cab-foxlet:47",            // <serialized-file name, lowercased>:<path id>
 "cab": "cab-foxlet", "path_id": 47, "container": "StreamingAssets/foxlet.bundle",
 "class": "Material", "class_id": 21, "name": "Foxlet_Mat",
 "asset_path": null,                // AssetBundle container path, e.g. assets/creatures/foxlet/foxlet.prefab
 "refs": [{"field": "m_SavedProperties.m_TexEnvs[0][1].m_Texture", "target": "cab-foxlet:45"}],
 "info": {"texture_slots": ["_MainTex", "_BumpMap"], "colors": {}, "floats": {}},
 "error": null}
```
`info` per class: Texture2D size/format/streamed; Mesh vertex/submesh counts
and bone-name hashes; AnimationClip binding path hashes, sample rate, length
and **events**; Avatar bone paths; Transform local TRS. `containers[]` lists
each opened file with its hash and serialized files (CAB names, externals).
`skipped[]` lists encryption-flagged or unsupported files that were not opened.

## Catalog (`aniimo-pal/catalog`, schema 0.1.0)

Produced by `aniimo-pal catalog build`. `species[]` entries contain: `key`,
`suggested_id`, `display_name`, `grouping_evidence`, `prefabs[]` (object,
name, asset path, `variant` + `variant_provenance`/`variant_evidence`),
`skeleton` (bones with path, parent, skinned flag and local TRS),
`assets[]` (object, container, sha256, role, variant, slot, evidence,
provenance), `animations[]` (object, `linked_via` controller|binding-match,
`binding_overlap` [matched, total], `role_guess` (inferred), `event_count`),
and `warnings[]`. The `rules` used are embedded for reproducibility.

## Creature project (`aniimo-pal/creature`, schema 0.2.0, may change)

0.2.0 adds (all optional, 0.1.x files remain valid): `prefabs`, `skeleton`,
`notes`, `history`; per-asset `class`, `name`, `slot`, `provenance`
(verified|inferred|user), `extracted`, `assetripper`; the `unclassified`
asset variant (must be resolved before `mapped`); the reserved `combat`
section (see architecture.md §9); and `source_profile` / `translation` /
`variants.*.profile_overrides` for stats, elements and work (architecture.md
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
     "variant": "shared",                 // shared|normal|shiny|prismana
     "association": "proposed",           // proposed|confirmed|rejected (confirmed requires sha256)
     "evidence": ["dependency:prefab", "name-stem"]}
  ],
  "variants": {
    "normal":   {"enabled": true},
    "shiny":    {"enabled": true, "spawn_probability": 0.002},
    "prismana": {"enabled": true, "spawn_probability": 0.0005}
  },
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
