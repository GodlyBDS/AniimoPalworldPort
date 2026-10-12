"""Palworld-side requirements shown in the Studio (information only; nothing is installed into Palworld).

Status: from community documentation (see docs/research.md), not verified in
game by this project. These are needed once the Palworld import milestones
(5-6) exist. Extraction and conversion do not need them.
"""

from __future__ import annotations

IN_GAME_MODS = [
    {
        "name": "UE4SS for Palworld (Okaetsu's RE-UE4SS fork, 'experimental-palworld')",
        "why": "Runs Lua/Blueprint mods in Palworld. PalSchema and Blueprint (Logic) mods need it. "
               "Required for new Pals; replacement-only resource paks do not need it.",
        "install": "Extract the release into <Palworld>/Pal/Binaries/Win64 (remove old UE4SS files first), "
                   "then start the game once and check that the UE4SS console appears.",
        "url": "https://github.com/Okaetsu/RE-UE4SS/releases/tag/experimental-palworld",
        "notes": "Community guides report the generic UE4SS release no longer works with current Palworld.",
    },
    {
        "name": "PalSchema",
        "why": "Adds and edits Palworld data (new Pal rows, stats, spawns, drops) from JSON files. "
               "This is how ported Aniimo become real new Pals.",
        "install": "Put the PalSchema folder into UE4SS's Mods folder. PalSchema content mods go into "
                   "PalSchema's own 'mods' sub-folder.",
        "url": "https://github.com/Okaetsu/PalSchema/releases/latest",
        "notes": "PalSchema must match your UE4SS version.",
    },
    {
        "name": "BPModLoaderMod (ships with UE4SS)",
        "why": "Loads Blueprint 'LogicMods' (.pak files in <Palworld>/Pal/Content/Paks/LogicMods).",
        "install": "Included in UE4SS; make sure it is enabled in UE4SS's Mods/mods.txt.",
        "url": None,
        "notes": "",
    },
]

FOR_BUILDING_MODS = [
    {
        "name": "Palworld Modding Kit (community Unreal project)",
        "why": "Unreal Engine project used to cook the converted creature into a Palworld mod (milestone 5).",
        "install": "Follow the kit's own setup guide (UE 5.1, Visual Studio 2022, .NET 6, Wwise 2021.1.11).",
        "url": "https://github.com/localcc/PalworldModdingKit",
        "notes": "Not needed until the Unreal import step exists in the Studio.",
    },
    {
        "name": "Unreal Engine 5.1",
        "why": "Palworld is built on UE 5.1; mods must be cooked with the same engine version.",
        "install": "Install UE 5.1 through the Epic Games Launcher.",
        "url": "https://www.unrealengine.com/download",
        "notes": "Large download; only needed for the import milestone.",
    },
]
