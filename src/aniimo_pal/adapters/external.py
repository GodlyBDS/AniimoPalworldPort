"""Discovery for Blender and Unreal Editor (used by later pipeline stages)."""

from __future__ import annotations

from aniimo_pal.adapters.base import ToolAdapter, ToolStatus, find_executable


class BlenderAdapter(ToolAdapter):
    name = "Blender"
    license = "GPL-2.0-or-later"
    homepage = "https://www.blender.org/"
    handles = ("model_fbx", "model_glb", "model_gltf", "model_obj")

    def __init__(self, configured_path: str = "") -> None:
        self.configured_path = configured_path

    def status(self) -> ToolStatus:
        location = find_executable(self.configured_path, ("blender", "blender.exe"))
        return ToolStatus(self.name, location is not None, location=location, license=self.license,
                          homepage=self.homepage,
                          notes="runs headless as: blender -b --factory-startup --python <script> -- <args>")


class UnrealEditorAdapter(ToolAdapter):
    name = "Unreal Editor 5.1"
    license = "Unreal Engine EULA"
    homepage = "https://www.unrealengine.com/"
    handles = ()

    def __init__(self, configured_path: str = "") -> None:
        self.configured_path = configured_path

    def status(self) -> ToolStatus:
        location = find_executable(self.configured_path, ("UnrealEditor-Cmd", "UnrealEditor-Cmd.exe",
                                                           "UnrealEditor", "UnrealEditor.exe"))
        return ToolStatus(self.name, location is not None, location=location, license=self.license,
                          homepage=self.homepage,
                          notes="Palworld targets UE 5.1.x; other engine versions will not produce loadable paks")
