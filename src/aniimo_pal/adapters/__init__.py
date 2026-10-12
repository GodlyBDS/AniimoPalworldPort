"""Adapters for external tools (UnityPy, AssetRipper, Blender, Unreal Editor)."""

from __future__ import annotations

from aniimo_pal.adapters.assetripper import AssetRipperAdapter
from aniimo_pal.adapters.base import ToolAdapter, ToolStatus
from aniimo_pal.adapters.external import BlenderAdapter, UnrealEditorAdapter
from aniimo_pal.adapters.unitypy_adapter import UnityPyAdapter
from aniimo_pal.config import ToolsConfig


def all_adapters(tools: ToolsConfig) -> list[ToolAdapter]:
    return [
        UnityPyAdapter(),
        AssetRipperAdapter(tools.assetripper),
        BlenderAdapter(tools.blender),
        UnrealEditorAdapter(tools.unreal_editor),
    ]


__all__ = ["AssetRipperAdapter", "BlenderAdapter", "ToolAdapter", "ToolStatus",
           "UnityPyAdapter", "UnrealEditorAdapter", "all_adapters"]
