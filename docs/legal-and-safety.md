# Legal and safety boundaries

This toolkit is for moving assets you are **legally allowed to use**: your own
creations, assets licensed for this use, or assets whose rights holder permits it
(e.g. a published modding policy or written permission). Aniimo and Palworld
assets are owned by their publishers. Being *able* to extract a file does not
grant the right to redistribute it. Check the current terms of both games before
publishing any mod.

## What the toolkit does

- Reads files in a game install you provide. **Read-only**: files are opened `rb`;
  the scanner refuses to write output inside the scanned tree; symlinks are not
  followed by default (no escaping the root).
- Detects formats and **reports** protection markers: the UnityFS encryption flag
  (UnityCN-style), prefixed or offset bundle signatures, Unity-named files with no
  recognisable structure, and high-entropy unknown data.
- Marks such files `supported: false`. Later pipeline stages skip them.

## What the toolkit will not do

- No decryption, key extraction or key use (UnityPy's
  `set_assetbundle_decrypt_key` is never called), no de-obfuscation, no stripping
  of prefixes or custom headers, and no circumventing DRM, anti-cheat or integrity
  checks.
- No memory dumping of running games.
- No distribution of game files. The repository holds code, docs and synthetic
  test data generated at test time.

If a creature's assets are only available in protected form, it is out of scope.
The right path is permission or an official export route from the rights holder.

## Keeping game data out of Git

- `.gitignore` excludes Unity containers, extracted models/textures, Unreal
  outputs, and local `input/`, `workspace/`, `extracted/`, `output/` and
  `manifests/` folders.
- `tests/test_repo_hygiene.py` runs the toolkit's own detector on every tracked
  file and fails CI if a Unity container, extracted model/texture, audio bank,
  executable or high-entropy blob (or any file > 2 MiB) is committed.
- Manifests and creature projects contain file names and hashes from the game.
  Keep them in your workspace, not in public repos. Manifests do not store the
  absolute scan path unless you pass `--store-absolute-root`.
