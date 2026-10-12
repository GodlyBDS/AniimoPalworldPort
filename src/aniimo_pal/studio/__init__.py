"""Aniimo -> Palworld Studio: a portable, guided GUI around the toolkit.

Everything the Studio installs or produces lives inside one folder (the folder
that contains the launcher): ``studio-data/`` holds the Python environment,
AssetRipper, Blender, the workspace, temp files, caches and logs. Nothing is
written anywhere else (the selected game folder is only read).

This package uses only the Python standard library, so the GUI can start before
anything is installed.
"""
