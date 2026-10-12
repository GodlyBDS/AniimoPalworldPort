# Aniimo → Palworld Studio (guided GUI)

The Studio is the easy way to use the toolkit. You need one folder and one
launcher, and you never have to type commands.

## Start

1. Put the toolkit in a new, empty folder (e.g. `Desktop\AniimoStudio`).
2. **Windows:** double-click `Start-Studio.bat`.
   **Linux/macOS:** run `./start-studio.sh`.
3. The **Setup** tab opens on first run. Click **Install everything missing**.

The launcher uses an installed Python 3.11+ that includes tkinter. If there is
none, it downloads a portable Python (python-build-standalone, ~40 MB) into the
folder.

## What gets installed, and where

Everything goes into `studio-data/` inside the Studio folder:

| Item | Location | Source |
|------|----------|--------|
| Python environment (UnityPy, PyYAML, numpy, jsonschema; the toolkit is used straight from `src/` via a `.pth` file, so updating the files needs no reinstall) | `studio-data/python-env/` | PyPI |
| AssetRipper | `studio-data/tools/assetripper/` | latest GitHub release (or a release .zip you downloaded: *Install AssetRipper from a downloaded .zip*) |
| Blender 5.2 LTS (portable) | `studio-data/tools/blender/` | download.blender.org (Windows .zip / Linux .tar.xz). On macOS install Blender yourself and use *Select an installed Blender* |
| Portable Python (only if needed) | `studio-data/python/` | python-build-standalone |
| Your results | `studio-data/workspace/` | — |
| Temp files, caches, tool settings, logs | `studio-data/tmp`, `cache`, `home`, `logs` | — |

Every tool the Studio starts gets its temp, cache and user-config locations
pointed into `studio-data/` (TEMP/TMP, pip cache, Blender user folders,
.NET/AppData/HOME). To remove the Studio completely, delete the folder.

The game folder you select is **only read**. The Studio refuses a game folder
that overlaps its own data folder.

**Windows: keep the Studio folder path short** (e.g. `C:\AniimoStudio`). Windows
limits paths to 260 characters; the installer avoids pip's deep build folders
and unzips Blender with long-path support, but tools inside may still create
deep paths. A GitHub zip download unpacks into a long folder name
(`AniimoPalworldPort-claude-…`); rename it.

## Workflow (tabs)

1. **Setup:** status of every requirement, with one-click install.
2. **Game folder:** select the Aniimo install (read-only).
3. **Scan & catalog:** file inventory → Unity object list → creature
   catalog, with a summary (Unity versions, encrypted files, and where the
   unrecognised files are, by extension and folder). The Unity object list
   surveys every bundle quickly and reads only the bundles with creature
   meshes (plus what they use) in detail, in parallel; the status bar shows
   progress and the estimated time left.
4. **Creatures:**
   - *Choose creature groups…*: every rigged model is grouped by the start
     of its name (e.g. `p_parmon`, `m_monster`, `eff_parmon`, `m_item`); pick
     the groups that are creatures. Groups whose names carry ID numbers are
     suggested. The choice is saved as `workspace/catalog_rules.toml` and the
     list is rebuilt. Search filters by name, ID and codename guess;
   - *Set names…*: type the creature's in-game name and name its numbered
     forms (form 03 = "Highland Form", …). Stored in
     `workspace/creature_names.toml` as your own entries; the toolkit ships
     no creature names;
   - pick a creature → *Create project* → review assets (confirm/reject
     proposals). Steps can also run before the review: the Studio asks and
     then uses the proposed links (they stay marked as proposed);
   - a step that needs earlier ones (the preview needs the converted model,
     conversion needs the AssetRipper exports) offers to run them first;
   - *Run all steps*: extract (models with skin weights, textures) →
     AssetRipper animation export → convert to FBX → build 3D preview.
     Missing animations do not stop the model conversion (they are listed
     as warnings). All parts of a creature (body, eyes, ...) end up in one
     FBX on one skeleton, and the preview shows each part with its own
     texture;
   - the **3D preview** tab shows every variant side by side (Normal, each
     Shiny appearance, Prismana) with its real extracted texture;
   - *Open 3D preview in Blender* opens the same scene in Blender. Every
     animation (idle, walk, attack, ultimate, …) is on the timeline, one after
     another, with a named marker for each. Press Space to play; orbit and
     inspect freely.
5. **Palworld mods:** what Palworld needs once creatures can be imported
   (UE4SS for Palworld, PalSchema, BPModLoaderMod), plus the creator-side
   modding kit and Unreal Engine 5.1. Information only: the Studio never
   changes your Palworld installation.
6. **Log:** everything the tools printed (also saved in `studio-data/logs/`).

The **Stop** button (bottom right) stops the running step together with every
program it started (worker processes, AssetRipper, Blender). Finished work is
kept: the object list caches each finished bundle, so running the step again
continues where it stopped.

## What has been tested

- **Linux (this project's CI/dev machine):** first-run install of the Python
  environment and portable Blender 5.2.2 into a fresh folder; AssetRipper
  installed from a release-style zip; the complete creature workflow (scan →
  … → convert → preview with rendered still) on a synthetic Unity game folder,
  using the real AssetRipper and real Blender executables; GUI screenshots
  under a virtual display.
- **Containment:** after setup and a full run, nothing was written outside the
  Studio folder in the home, temp or cache locations checked.
- **Tests:** download resume after dropped connections, archive path-traversal
  protection, installer asset selection, command building, GUI smoke test.

**Not tested yet:**
- Windows (`Start-Studio.bat`, the PowerShell portable-Python download, the
  Windows Blender zip) and macOS.
- Downloading AssetRipper from GitHub. The dev environment blocks GitHub
  release downloads; the asset-selection logic is unit-tested and the zip
  install path was run for real.
- Real Aniimo files.

Please report what happens on your machine.
