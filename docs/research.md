# Research: existing tools and workflows

Researched 2026-10-12. Each claim is marked **[verified]** (read in source code or
official docs), **[source]** (read in community docs, not tested) or
**[unverified]** (plausible but not checked). Nothing below was run against real
Aniimo files, Unreal Engine or Palworld in this milestone (see `roadmap.md`).

## 1. Unity asset inspection and extraction

| Tool | License | Status | Role in this toolkit |
|------|---------|--------|----------------------|
| [AssetRipper](https://github.com/AssetRipper/AssetRipper) | GPL-3.0 | Active. Supports Unity 3.5.0 to 6000.4.x per README **[verified]** | Primary bulk extractor (bundles to glTF/PNG, or a full Unity project). Run as an **external process**, never linked. |
| [UnityPy](https://github.com/K0lb3/UnityPy) | MIT | Active, `pip install UnityPy`, Python ≥ 3.8 **[verified]** | In-process inventory: object types, names, dependencies; texture export (`Texture2D.image`), mesh export to OBJ (`Mesh.export()`) **[verified, README]** |
| [AssetStudio](https://github.com/Perfare/AssetStudio) (Perfare) | MIT | **Archived 2023-01-21**, read-only **[verified]** | Reference only. Too old for current Unity versions. |
| uTinyRipper | MIT | Development suspended **[source]** | Not used. |
| AssetRipper-CLI (MeikoMei16) | — | Third-party wrapper, built from source **[source]** | Not used. We call the official headless mode directly. |

### AssetRipper headless interface **[verified from source, master @ beeb50e, 2026-10-08]**
- Arguments (Ookii.CommandLine, POSIX style): `--port <int>` (0 = random),
  `--headless` (no browser launch), `--log <bool>`, `--log-path <path>`,
  `--local-web-file <path>`. Defined in `Source/AssetRipper.GUI.Web/Arguments.cs`.
- HTTP routes (`WebApplicationLauncher.cs`): `POST /LoadFile`, `POST /LoadFolder`,
  `POST /Export/UnityProject`, `POST /Export/PrimaryContent`, `POST /Reset`.
  Load and export routes read the form field `Path` (`Pages/Commands.cs`).
- Models are exported as glTF/GLB (`AssetRipper.Export.Modules.Models/Glb*.cs`).
- **Not yet tested:** running the binary, export timing, and how it reports completion.
  Milestone 2 has to poll for completion or inspect the output directory.

### UnityPy facts used by the scanner **[verified from source]**
- UnityFS header (big-endian): signature, `u32` format version, player version
  string, engine version string, `i64` size, `u32` compressed and uncompressed
  block-info sizes, `u32` flags. Format ≥ 7 aligns to 16 bytes.
- `ArchiveFlags`: compression mask `0x3F`, `0x40` blocks and directory combined,
  `0x80` block info at end, `0x100` old web plugin compatibility. On engines before
  2020.3.34, 2021.3.2 and 2022.1.1, `0x200` = encryption. On newer engines,
  `0x200` = block-info padding and encryption is `0x400`/`0x1000` (mask `0x1400`).
- SerializedFile header: `u32` metadata size, file size, version, data offset;
  v ≥ 9 adds an endian byte and 3 reserved bytes; v ≥ 22 adds `u32` metadata size,
  `i64` file size, `i64` data offset and `i64` unknown. The Unity version string
  follows for v ≥ 7.
- UnityPy can decrypt UnityCN bundles when given a game's key
  (`set_assetbundle_decrypt_key`). **This toolkit never calls it** (see
  `legal-and-safety.md`).

The test suite cross-checks our header parser against UnityPy 1.25.4 on
synthetic bundles and SerializedFiles (`tests/test_unitypy_crosscheck.py`).

### Aniimo specifics **[unverified]**
- Developer: Pawprint Studio (FunPlus-backed). Released on PC, PS5 and Xbox on
  2026-09-16, with mobile following **[source: press coverage]**.
- **The engine was not confirmed by any public source we found.** The project brief
  says it is a customised Unity. The scanner is built to work whatever the answer:
  it reports what each file actually is, including unknown or high-entropy files,
  and never assumes a layout.
- Customised Unity games often use non-standard containers (prefixed headers,
  custom encryption, UnityCN). The scanner flags all of these and the toolkit stops
  there.

## 2. Palworld modding

| Item | Facts | Confidence |
|------|-------|-----------|
| Engine | Unreal Engine 5.1 (community guides say 5.1.1) | [source] |
| Modding kit | [localcc/PalworldModdingKit](https://github.com/localcc/PalworldModdingKit): community UE project (`Pal.uproject`) for "blueprint mods referencing game functions", including custom pals. Not an official Pocketpair SDK. | [verified, repo page] |
| Kit toolchain | UE 5.1, .NET 6, VS 2022 + MSVC v143 14.38, Wwise 2021.1.11. "Do not let Unreal upgrade the project." | [source: palmods.gg] |
| Pak mods | Resource paks go in `Pal/Content/Paks/~mods/` (legacy/manual). Keep the `.pak` + `.ucas` + `.utoc` together. | [source] |
| LogicMods | `Pal/Content/Paks/LogicMods/`. Needs UE4SS and BPModLoaderMod. Mod content lives in `Content/Mods/<Name>/` with a `ModActor` blueprint, a Primary Asset Label with **Always Cook** and a **unique non-zero chunk ID**. The pak name must match the mod folder. | [source] |
| UE4SS | Since Palworld 0.4.1.5 the Palworld-specific RE-UE4SS build ([Okaetsu fork, `experimental-palworld`](https://github.com/Okaetsu/RE-UE4SS/releases/tag/experimental-palworld)) is needed. | [source] |
| PalSchema | UE4SS mod that edits DataTables and Blueprints from `.json`/`.jsonc`, including **adding new DataTable rows**. This is the likely route for new Pals without binary DataTable editing. | [source] |
| Docs hubs | pwmodding.wiki (community wiki), palmods.gg guides | [source] |

Open questions for the integration milestones (need FModel or the modding kit and
a Palworld install):
- Which DataTables and blueprints a **new** Pal needs: character parameters,
  icons, Paldeck, drops, spawner placement, breeding. Get the exact names from the
  current game data. Do not guess.
- Whether spawn groups pick one weighted entry per individual. If they do, rare
  variants can use native weights. If not, a spawn hook must swap the variant.
- How a captured Pal's identity is saved (CharacterID string or something else).
  This decides how variants persist (`architecture.md` §6).

## 3. Unreal Engine 5.1 import workflow

`unreal.AssetImportTask` (UE 5.1) **[verified, Epic Python API docs]**:
`filename`, `destination_path`, `destination_name`, `automated` (no dialogs),
`replace_existing`, `replace_existing_settings`, `save`, `options`, `factory`,
`async_`, `imported_object_paths` (results; `result` is deprecated).

`unreal.FbxImportUI` (UE 5.1) **[verified]**: `import_mesh`, `import_as_skeletal`,
`skeleton` (leave None to create one; **required for animation-only import**),
`import_animations`, `override_animation_name`, `create_physics_asset`,
`import_materials`, `import_textures`, `mesh_type_to_import`,
`automated_import_should_detect_type`, `skeletal_mesh_import_data`,
`anim_sequence_import_data`, `texture_import_data`.

Planned import order per creature: skeletal mesh first (creates the skeleton),
then animations against that skeleton, then textures, then material instances.
Run it with `UnrealEditor-Cmd <project>.uproject -run=pythonscript -script=...`
**[unverified for UE 5.1 + Palworld kit, test locally]**.

FBX is the planned hand-off format to UE 5.1. glTF import in 5.1 depends on the
Interchange/glTF importer plugins **[unverified]**, so Blender converts GLB to FBX.

## 4. Blender as the conversion and editing host

- Headless: `blender -b --factory-startup --python script.py -- args` **[verified, standard Blender CLI]**.
- Main jobs: import AssetRipper GLB, fix axes and units (Unity: Y-up, left-handed,
  metres; UE: Z-up, left-handed, centimetres), rename and retarget bones, bake
  animations, export FBX with settings UE 5.1 accepts.
- Blender is GPL. Our scripts run inside it as separate files.

## Sources
- AssetRipper: https://github.com/AssetRipper/AssetRipper (source read at commit beeb50e)
- UnityPy: https://github.com/K0lb3/UnityPy (`files/BundleFile.py`, `files/SerializedFile.py`, `enums/BundleFile.py`)
- AssetStudio: https://github.com/Perfare/AssetStudio
- PalworldModdingKit: https://github.com/localcc/PalworldModdingKit
- palmods.gg guides: https://www.palmods.gg/guides/modding/modding-kit, https://www.palmods.gg/guides/modding/pak-mods
- Palworld modding guide (community): https://github.com/AidenM6901/PalworldModdingGuide
- UE 5.1 Python API: https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/AssetImportTask?application_version=5.1 and `.../class/FbxImportUI?application_version=5.1`
- Aniimo press: https://80.lv/articles/new-free-to-play-pok-mon-rival-aniimo-launches-today-on-pc-and-consoles, https://www.invenglobal.com/articles/25919/aniimo-selects-chuu-as-official-model-launching-sept-16-on-pc-and-console
- PalSchema description (third-party mirror): https://vgtimes.ru/games/palworld/files/96142-palschema-0.6.0.html
- Palworld RE-UE4SS build: https://github.com/Okaetsu/RE-UE4SS/releases/tag/experimental-palworld
