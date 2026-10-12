"""UnityPy adapter (https://github.com/K0lb3/UnityPy, MIT).

Read-only use of UnityPy:

* ``inventory_file``: list every object in a bundle / SerializedFile with its
  class, name, container (asset) path, outgoing references (PPtrs) and a few
  class-specific facts used by the catalog (texture size, mesh bone hashes,
  animation binding hashes, avatar bone paths, ...).
* ``open_objects``: re-open a container and hand back parsed objects by path ID
  for extraction (textures to PNG, meshes to OBJ).

UnityPy only reads the files (``open(path, "rb")``). UnityPy's
``set_assetbundle_decrypt_key`` is intentionally never called, and callers must
not pass encryption-flagged files (the inventory stage filters them using the
manifest).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import ntpath
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from aniimo_pal.adapters.base import ToolAdapter, ToolStatus
from aniimo_pal.errors import ToolkitError

# Classes parsed fully so their references and details can be recorded. Others
# only have their name peeked (cheap), which matters for multi-GB installs.
PARSE_CLASSES = {
    "GameObject", "Transform", "RectTransform", "MeshFilter", "MeshRenderer", "SkinnedMeshRenderer",
    "Material", "Texture2D", "Mesh", "AnimationClip", "Animator", "AnimatorController",
    "AnimatorOverrideController", "Avatar", "Animation", "AssetBundle", "Sprite", "MonoBehaviour",
    "PrefabInstance", "Prefab", "TextAsset",
}
# Classes whose references are not walked (no useful PPtrs, large payloads).
NO_REF_WALK = {"Texture2D", "Mesh", "AnimationClip", "Avatar", "AssetBundle", "TextAsset"}


class UnityPyUnavailable(ToolkitError):
    """UnityPy is not installed."""


def _require_unitypy():
    try:
        import UnityPy  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise UnityPyUnavailable(
            "UnityPy is required for this command: pip install 'aniimo-palworld-toolkit[unitypy]'"
        ) from exc
    import UnityPy

    return UnityPy


def unitypy_version() -> str | None:
    try:
        return importlib.metadata.version("UnityPy")
    except importlib.metadata.PackageNotFoundError:
        return None


def cab_key(name: str) -> str:
    """Normalise a serialized-file name or external path to a lookup key.

    ``archive:/CAB-abc/CAB-abc`` -> ``cab-abc``; ``Library/unity default resources``
    -> ``unity default resources``; ``sharedassets0.assets`` -> itself.
    """
    return ntpath.basename(name.replace("\\", "/").rstrip("/")).lower()


def object_id(cab: str, path_id: int) -> str:
    return f"{cab}:{path_id}"


def _is_pptr(value: Any) -> bool:
    return isinstance(value, dict) and "m_PathID" in value and "m_FileID" in value and len(value) == 2


def iter_pptrs(value: Any, path: str = "") -> Iterator[tuple[str, int, int]]:
    """Yield (field_path, file_id, path_id) for every non-null PPtr in a parsed dict."""
    if _is_pptr(value):
        if value["m_PathID"]:
            yield path, int(value["m_FileID"]), int(value["m_PathID"])
        return
    if isinstance(value, dict):
        for key, sub in value.items():
            yield from iter_pptrs(sub, f"{path}.{key}" if path else str(key))
    elif isinstance(value, tuple):  # map entries / pairs, e.g. (slot name, texture env)
        for i, sub in enumerate(value):
            yield from iter_pptrs(sub, f"{path}[{i}]")
    elif isinstance(value, list):
        if value and not isinstance(value[0], (dict, list, tuple)):
            return  # plain numeric/byte arrays: nothing to find, skip quickly
        for i, sub in enumerate(value):
            yield from iter_pptrs(sub, f"{path}[{i}]")


def _class_info(cls: str, d: dict[str, Any]) -> dict[str, Any]:
    """Small, JSON-friendly facts per class (never large payloads)."""
    info: dict[str, Any] = {}
    if cls == "Texture2D":
        stream = d.get("m_StreamData") or {}
        info.update(width=d.get("m_Width"), height=d.get("m_Height"), format=d.get("m_TextureFormat"),
                    mip_count=d.get("m_MipCount"), streamed=bool(stream.get("path")))
    elif cls == "Mesh":
        vdata = d.get("m_VertexData") or {}
        info.update(vertex_count=vdata.get("m_VertexCount"), submesh_count=len(d.get("m_SubMeshes") or []),
                    bone_name_hashes=sorted({int(h) for h in d.get("m_BoneNameHashes") or []}),
                    root_bone_name_hash=d.get("m_RootBoneNameHash"))
    elif cls == "AnimationClip":
        bindings = (d.get("m_ClipBindingConstant") or {}).get("genericBindings") or []
        muscle = d.get("m_MuscleClip") or {}
        start, stop = muscle.get("m_StartTime"), muscle.get("m_StopTime")
        info.update(binding_path_hashes=sorted({int(b["path"]) for b in bindings if b.get("path")}),
                    sample_rate=d.get("m_SampleRate"), legacy=d.get("m_Legacy"),
                    length=(stop - start) if isinstance(start, (int, float)) and isinstance(stop, (int, float)) else None,
                    # Animation events are verified source data (e.g. hit frames, VFX/SFX triggers).
                    events=[{"time": e.get("time"), "function": e.get("functionName"), "data": e.get("data"),
                             "int": e.get("intParameter"), "float": e.get("floatParameter")}
                            for e in d.get("m_Events") or []][:200])
    elif cls == "Avatar":
        info["bone_paths"] = sorted({str(p) for _h, p in d.get("m_TOS") or [] if p})
    elif cls == "Material":
        props = d.get("m_SavedProperties") or {}
        info["texture_slots"] = [slot for slot, env in props.get("m_TexEnvs") or []
                                 if (env.get("m_Texture") or {}).get("m_PathID")]
        info["colors"] = {name: c for name, c in props.get("m_Colors") or []}
        info["floats"] = {name: f for name, f in props.get("m_Floats") or []}
    elif cls == "GameObject":
        info["active"] = d.get("m_IsActive")
    elif cls in ("Transform", "RectTransform"):
        info.update(position=d.get("m_LocalPosition"), rotation=d.get("m_LocalRotation"), scale=d.get("m_LocalScale"))
    elif cls == "TextAsset":
        info.update(_text_asset_info(d.get("m_Script")))
    elif cls == "AssetBundle":
        info.update(bundle_name=d.get("m_AssetBundleName"), dependencies=list(d.get("m_Dependencies") or []))
    return info


def _text_asset_info(script: Any) -> dict[str, Any]:
    """Size and a coarse content guess for a TextAsset (data tables often live in these)."""
    if isinstance(script, str):
        raw = script.encode("utf-8", "surrogateescape")
    elif isinstance(script, (bytes, bytearray)):
        raw = bytes(script)
    else:
        return {"size": None, "content": "unknown"}
    head = raw[:4096]
    try:
        text = head.decode("utf-8")
        is_text = "\x00" not in text
    except UnicodeDecodeError:
        is_text = False
    if not is_text:
        content = "binary"
    else:
        stripped = text.lstrip("\ufeff \t\r\n")
        if stripped.startswith(("{", "[")):
            content = "json"
        elif stripped.startswith("<"):
            content = "xml"
        elif "\n" in stripped and stripped.split("\n", 1)[0].count(",") >= 2:
            content = "csv"
        else:
            content = "text"
    return {"size": len(raw), "content": content}


@dataclass
class SerializedInventory:
    cab: str
    unity_version: str
    externals: list[str]
    objects: list[dict[str, Any]] = field(default_factory=list)


class UnityPyAdapter(ToolAdapter):
    name = "UnityPy"
    license = "MIT"
    homepage = "https://github.com/K0lb3/UnityPy"
    handles = ("unity_bundle", "unity_serialized", "unity_resource")

    def status(self) -> ToolStatus:
        if importlib.util.find_spec("UnityPy") is None:
            return ToolStatus(self.name, False, license=self.license, homepage=self.homepage,
                              notes="optional; install with: pip install 'aniimo-palworld-toolkit[unitypy]'")
        return ToolStatus(self.name, True, location="python package", version=unitypy_version(),
                          license=self.license, homepage=self.homepage)

    # -- loading -------------------------------------------------------------

    @staticmethod
    def load(path: str | Path):
        UnityPy = _require_unitypy()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return UnityPy.load(str(path))

    @staticmethod
    def serialized_files(env) -> Iterator[Any]:
        from UnityPy.files import SerializedFile

        stack = list(env.files.values())
        while stack:
            f = stack.pop(0)
            if isinstance(f, SerializedFile):
                yield f
            elif hasattr(f, "files") and isinstance(f.files, dict):
                stack.extend(f.files.values())

    # -- inventory -----------------------------------------------------------

    def inventory_file(self, path: str | Path) -> list[SerializedInventory]:
        """Inventory every SerializedFile inside ``path`` (bundle or .assets)."""
        env = self.load(path)
        results = []
        for sf in self.serialized_files(env):
            cab = cab_key(sf.name)
            externals = [cab_key(e.path) for e in sf.externals]
            inv = SerializedInventory(cab=cab, unity_version=str(sf.unity_version), externals=externals)
            for obj in sf.objects.values():
                inv.objects.append(self._object_record(obj, cab, externals))
            inv.objects.sort(key=lambda o: o["path_id"])
            results.append(inv)
        return results

    @staticmethod
    def _object_record(obj, cab: str, externals: list[str]) -> dict[str, Any]:
        try:
            cls = obj.type.name
        except Exception:  # unknown class id
            cls = f"Class{obj.class_id}"
        rec: dict[str, Any] = {"id": object_id(cab, obj.path_id), "cab": cab, "path_id": obj.path_id,
                               "class": cls, "class_id": obj.class_id, "name": None, "asset_path": None,
                               "refs": [], "info": {}, "error": None}
        try:
            rec["asset_path"] = obj.container
        except Exception:
            pass
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if cls in PARSE_CLASSES:
                    d = obj.parse_as_dict(check_read=False)
                    rec["name"] = d.get("m_Name") or None
                    rec["info"] = _class_info(cls, d)
                    if cls == "MonoBehaviour" and isinstance(d.get("m_Script"), dict):
                        rec["info"]["script"] = d["m_Script"].get("m_PathID")
                    if cls not in NO_REF_WALK:
                        for field_path, file_id, path_id in iter_pptrs(d):
                            if file_id == 0:
                                target_cab = cab
                            elif 0 < file_id <= len(externals):
                                target_cab = externals[file_id - 1]
                            else:
                                continue
                            rec["refs"].append({"field": field_path, "target": object_id(target_cab, path_id)})
                else:
                    rec["name"] = obj.peek_name() or None
        except Exception as exc:  # malformed / unsupported object: record and continue
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return rec

    # -- extraction helpers --------------------------------------------------

    def open_objects(self, path: str | Path) -> dict[str, Any]:
        """Return {object_id: ObjectReader} for every object in the container."""
        env = self.load(path)
        out = {}
        for sf in self.serialized_files(env):
            cab = cab_key(sf.name)
            for obj in sf.objects.values():
                out[object_id(cab, obj.path_id)] = obj
        out["__env__"] = env  # keep the environment alive while readers are used
        return out
