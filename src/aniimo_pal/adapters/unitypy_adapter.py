"""UnityPy adapter (https://github.com/K0lb3/UnityPy, MIT).

Planned use (milestone 2): read-only object inventory of unencrypted bundles and
SerializedFiles (``UnityPy.load(path)``, iterate ``env.objects``), texture
export via ``Texture2D.image`` and mesh export via ``Mesh.export()`` (OBJ).
UnityPy's ``set_assetbundle_decrypt_key`` is intentionally never called.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util

from aniimo_pal.adapters.base import ToolAdapter, ToolStatus


class UnityPyAdapter(ToolAdapter):
    name = "UnityPy"
    license = "MIT"
    homepage = "https://github.com/K0lb3/UnityPy"
    handles = ("unity_bundle", "unity_serialized", "unity_resource")

    def status(self) -> ToolStatus:
        if importlib.util.find_spec("UnityPy") is None:
            return ToolStatus(self.name, False, license=self.license, homepage=self.homepage,
                              notes="optional; install with: pip install 'aniimo-palworld-toolkit[unitypy]'")
        try:
            version = importlib.metadata.version("UnityPy")
        except importlib.metadata.PackageNotFoundError:
            version = None
        return ToolStatus(self.name, True, location="python package", version=version,
                          license=self.license, homepage=self.homepage)
