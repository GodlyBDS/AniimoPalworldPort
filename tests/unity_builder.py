"""Build structurally real Unity SerializedFiles/bundles for tests (requires UnityPy).

Object data is serialised with UnityPy's own type-tree writer using UnityPy's
bundled type definitions for the chosen Unity version, so the objects parse
exactly like objects from a real build. All content (names, meshes, pixels)
is synthetic.

File layout written here (SerializedFile v22, little-endian metadata, no
embedded type trees) follows UnityPy's ``SerializedFile`` reader.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

import synthetic
from UnityPy.helpers import TypeTreeHelper
from UnityPy.helpers.Tpk import get_typetree_node
from UnityPy.helpers.UnityVersion import UnityVersion
from UnityPy.streams import EndianBinaryWriter

_INT_TYPES = {"SInt8", "UInt8", "SInt16", "UInt16", "SInt32", "UInt32", "SInt64", "UInt64", "int",
              "unsigned int", "short", "unsigned short", "long long", "unsigned long long", "char",
              "FileSize", "Type*"}


def default_value(node) -> Any:
    """Zero/empty value for a type-tree node, in the shape UnityPy's writer expects."""
    t = node.m_Type
    if t in _INT_TYPES:
        return 0
    if t in ("float", "double"):
        return 0.0
    if t == "bool":
        return False
    if t == "string":
        return ""
    if t == "TypelessData":
        return b""
    if t == "pair":
        return (default_value(node.m_Children[0]), default_value(node.m_Children[1]))
    if node.m_Children and node.m_Children[0].m_Type == "Array":
        return []
    return {c.m_Name: default_value(c) for c in node.m_Children}


def pptr(path_id: int = 0, file_id: int = 0) -> dict[str, int]:
    return {"m_FileID": file_id, "m_PathID": path_id}


@dataclass
class _Obj:
    path_id: int
    class_id: int
    data: dict[str, Any]


@dataclass
class SerializedBuilder:
    unity_version: str = "2021.3.15f1"
    target_platform: int = 19  # StandaloneWindows64
    externals: list[str] = field(default_factory=list)  # e.g. "archive:/CAB-other/CAB-other"
    _objects: list[_Obj] = field(default_factory=list)
    _next_id: int = 1

    def __post_init__(self):
        self._version = UnityVersion.from_str(self.unity_version)

    def node(self, class_id: int):
        return get_typetree_node(class_id, self._version)

    def add(self, class_id: int, **fields: Any) -> int:
        data = default_value(self.node(class_id))
        for key, value in fields.items():
            if key not in data:
                raise KeyError(f"class {class_id} has no field {key!r}")
            if isinstance(value, dict) and isinstance(data[key], dict):
                merged = dict(data[key])
                merged.update(value)
                value = merged
            data[key] = value
        path_id = self._next_id
        self._next_id += 1
        self._objects.append(_Obj(path_id, class_id, data))
        return path_id

    def get(self, path_id: int) -> dict[str, Any]:
        return next(o.data for o in self._objects if o.path_id == path_id)

    def build(self) -> bytes:
        class_ids = sorted({o.class_id for o in self._objects})
        type_index = {cid: i for i, cid in enumerate(class_ids)}

        data = EndianBinaryWriter(endian="<")
        entries = []
        for obj in self._objects:
            w = EndianBinaryWriter(endian="<")
            TypeTreeHelper.write_typetree(obj.data, self.node(obj.class_id), w, None)
            blob = w.bytes
            entries.append((obj.path_id, data.Position, len(blob), type_index[obj.class_id]))
            data.write(blob)
            data.align_stream(8)

        meta = EndianBinaryWriter(endian="<")
        meta.write_string_to_null(self.unity_version)
        meta.write_int(self.target_platform)
        meta.write_boolean(False)  # no embedded type trees -> UnityPy uses its bundled definitions
        meta.write_int(len(class_ids))
        for cid in class_ids:
            meta.write_int(cid)
            meta.write_boolean(False)       # is_stripped_type
            meta.write_short(-1)            # script_type_index
            if cid == 114:
                meta.write_bytes(bytes(16))  # script_id
            meta.write_bytes(bytes(16))     # old_type_hash
        meta.write_int(len(entries))
        header_len = 48
        for path_id, offset, size, tidx in entries:
            while (header_len + meta.Position) % 4:
                meta.write_bytes(b"\x00")
            meta.write_long(path_id)
            meta.write_long(offset)
            meta.write_u_int(size)
            meta.write_int(tidx)
        meta.write_int(0)  # script types
        meta.write_int(len(self.externals))
        for path in self.externals:
            meta.write_string_to_null("")      # temp_empty
            meta.write_bytes(bytes(16))        # guid
            meta.write_int(0)                  # type
            meta.write_string_to_null(path)
        meta.write_int(0)  # ref types (v >= 20)
        meta.write_string_to_null("")  # user information

        metadata = meta.bytes
        data_offset = header_len + len(metadata)
        data_offset += (16 - data_offset % 16) % 16
        total = data_offset + len(data.bytes)
        header = struct.pack(">IIII", 0, 0, 22, 0) + b"\x00\x00\x00\x00"
        header += struct.pack(">IqqQ", len(metadata), total, data_offset, 0)
        out = header + metadata
        out += b"\x00" * (data_offset - len(out))
        return out + data.bytes


def bundle(serialized: bytes, cab_name: str, engine_version: str = "2021.3.15f1") -> bytes:
    """Wrap a SerializedFile in an uncompressed UnityFS bundle."""
    return synthetic.unityfs_bundle(engine_version=engine_version, node_name=cab_name, payload=serialized)


# --- creature fixture -----------------------------------------------------------

def crc(path: str) -> int:
    return zlib.crc32(path.encode("utf-8"))


def _rgba(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    return bytes(list(rgb) + [255]) * (width * height)


def add_texture(b: SerializedBuilder, name: str, rgb=(200, 120, 40), size=4) -> int:
    pixels = _rgba(size, size, rgb)
    return b.add(28, m_Name=name, m_Width=size, m_Height=size, m_CompleteImageSize=len(pixels),
                 m_TextureFormat=4, m_MipCount=1, m_ImageCount=1, m_TextureDimension=2, m_IsReadable=True,
                 **{"image data": pixels})


def add_material(b: SerializedBuilder, name: str, textures: dict[str, int | dict]) -> int:
    mat_node = b.node(21)
    props = default_value(next(c for c in mat_node.m_Children if c.m_Name == "m_SavedProperties"))
    props["m_TexEnvs"] = [
        (slot, {"m_Texture": tid if isinstance(tid, dict) else pptr(tid),
                "m_Scale": {"x": 1.0, "y": 1.0}, "m_Offset": {"x": 0.0, "y": 0.0}})
        for slot, tid in textures.items()
    ]
    props["m_Colors"] = [("_Color", {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0})]
    return b.add(21, m_Name=name, m_SavedProperties=props)


def add_triangle_mesh(b: SerializedBuilder, name: str, bone_paths: list[str]) -> int:
    """A one-triangle mesh with position data so UnityPy can export it as OBJ."""
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    mesh_node = b.node(43)
    vdata = default_value(next(c for c in mesh_node.m_Children if c.m_Name == "m_VertexData"))
    channels = [{"stream": 0, "offset": 0, "format": 0, "dimension": 0} for _ in range(14)]
    channels[0] = {"stream": 0, "offset": 0, "format": 0, "dimension": 3}  # position, float32 x3
    vdata.update(m_VertexCount=3, m_Channels=channels, m_DataSize=positions)
    submesh_node = next(c for c in mesh_node.m_Children if c.m_Name == "m_SubMeshes").m_Children[0].m_Children[1]
    sub = default_value(submesh_node)
    sub.update(firstByte=0, indexCount=3, topology=0, baseVertex=0, firstVertex=0, vertexCount=3)
    return b.add(43, m_Name=name, m_SubMeshes=[sub], m_IndexFormat=0,
                 m_IndexBuffer=list(struct.pack("<3H", 0, 1, 2)), m_VertexData=vdata,
                 m_BoneNameHashes=[crc(p) for p in bone_paths], m_RootBoneNameHash=crc(bone_paths[0]) if bone_paths else 0,
                 m_BindPose=[default_value(next(c for c in mesh_node.m_Children
                                                if c.m_Name == "m_BindPose").m_Children[0].m_Children[1])
                             for _ in bone_paths])


def _unity_trs(pos, rot):
    import numpy as np

    x, y, z, w = rot
    m = np.eye(4)
    m[:3, :3] = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m[:3, 3] = pos
    return m


def bone_world_matrices(bone_locals: dict[str, tuple]) -> dict:
    out = {}
    for p in sorted(bone_locals, key=lambda p: p.count("/")):
        m = _unity_trs(*bone_locals[p])
        parent = p.rsplit("/", 1)[0] if "/" in p else None
        out[p] = out[parent] @ m if parent else m
    return out


def add_skinned_mesh(b: SerializedBuilder, name: str, bone_paths: list[str], bone_locals: dict[str, tuple]) -> int:
    """A mesh with one small quad per bone, fully weighted to that bone, plus correct bind poses.

    Vertex layout (stream 0, interleaved): position float3 | blend weights float4 | blend indices uint32x4.
    """
    import numpy as np

    world = bone_world_matrices(bone_locals)
    verts, idx = bytearray(), []
    for j, p in enumerate(bone_paths):
        c = world[p][:3, 3]
        base = len(idx) // 6 * 4
        for dx, dz in ((-0.02, -0.02), (0.02, -0.02), (0.02, 0.02), (-0.02, 0.02)):
            verts += struct.pack("<3f", c[0] + dx, c[1], c[2] + dz)
            verts += struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
            verts += struct.pack("<4I", j, 0, 0, 0)
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    n_verts = 4 * len(bone_paths)
    mesh_node = b.node(43)
    vdata = default_value(next(c for c in mesh_node.m_Children if c.m_Name == "m_VertexData"))
    channels = [{"stream": 0, "offset": 0, "format": 0, "dimension": 0} for _ in range(14)]
    channels[0] = {"stream": 0, "offset": 0, "format": 0, "dimension": 3}    # position
    channels[12] = {"stream": 0, "offset": 12, "format": 0, "dimension": 4}  # blend weights (float)
    channels[13] = {"stream": 0, "offset": 28, "format": 10, "dimension": 4}  # blend indices (uint32)
    vdata.update(m_VertexCount=n_verts, m_Channels=channels, m_DataSize=bytes(verts))
    submesh_node = next(c for c in mesh_node.m_Children if c.m_Name == "m_SubMeshes").m_Children[0].m_Children[1]
    sub = default_value(submesh_node)
    sub.update(firstByte=0, indexCount=len(idx), topology=0, baseVertex=0, firstVertex=0, vertexCount=n_verts)
    bindposes = []
    for p in bone_paths:
        inv = np.linalg.inv(world[p])
        bindposes.append({f"e{r}{c}": float(inv[r, c]) for r in range(4) for c in range(4)})
    return b.add(43, m_Name=name, m_SubMeshes=[sub], m_IndexFormat=0,
                 m_IndexBuffer=list(struct.pack(f"<{len(idx)}H", *idx)), m_VertexData=vdata,
                 m_BoneNameHashes=[crc(p) for p in bone_paths], m_RootBoneNameHash=crc(bone_paths[0]),
                 m_BindPose=bindposes)


def add_clip(b: SerializedBuilder, name: str, bound_paths: list[str], events: list[tuple[float, str]] = ()) -> int:
    clip_node = b.node(74)
    binding = default_value(next(c for c in clip_node.m_Children if c.m_Name == "m_ClipBindingConstant"))
    gb_node = next(c for c in clip_node.m_Children if c.m_Name == "m_ClipBindingConstant")
    gb_item = default_value(gb_node.m_Children[0].m_Children[0].m_Children[1])
    binding["genericBindings"] = [dict(gb_item, path=crc(p), attribute=1, typeID=4) for p in bound_paths]
    ev_node = next(c for c in clip_node.m_Children if c.m_Name == "m_Events").m_Children[0].m_Children[1]
    ev_list = [dict(default_value(ev_node), time=t, functionName=fn) for t, fn in events]
    return b.add(74, m_Name=name, m_SampleRate=30.0, m_ClipBindingConstant=binding, m_Events=ev_list)


@dataclass
class CreatureIds:
    root_go: int
    mesh: int
    materials: list[int]
    textures: list[int]
    clips: list[int]
    bones: list[str]


def add_creature(b: SerializedBuilder, name: str, *, bones: list[str], clips: list[str],
                 variants: dict[str, tuple[int, int, int]] | None = None, mesh: int | None = None,
                 with_controller: bool = True, rgb=(200, 120, 40),
                 extra_textures: dict[str, dict] | None = None,
                 bone_locals: dict[str, tuple] | None = None) -> dict[str, CreatureIds]:
    """Add a skinned creature prefab (and optional colour variants sharing the mesh).

    Hierarchy: <name> (Animator) -> <name>_Body (SkinnedMeshRenderer) and a bone chain
    built from ``bones`` (paths like "Root", "Root/Hips"). Variants are separate root
    prefabs named ``<name>_<Variant>`` that reuse the mesh, skeleton paths and
    controller but use their own material and texture.
    """
    if mesh is None:
        mesh = (add_skinned_mesh(b, f"{name}_Body", bones, bone_locals) if bone_locals
                else add_triangle_mesh(b, f"{name}_Body", bones))
    clip_ids = [add_clip(b, f"{name}_{clip}", bones, events=[(0.25, "OnHit"), (0.5, "PlayVfx")]
                         if "attack" in clip.lower() or "ult" in clip.lower() else ()) for clip in clips]
    controller = b.add(91, m_Name=f"{name}_Controller", m_AnimationClips=[pptr(c) for c in clip_ids]) \
        if with_controller else 0
    avatar = b.add(90, m_Name=f"{name}Avatar", m_TOS=[(crc(p), p) for p in bones])

    def prefab(prefab_name: str, colour) -> CreatureIds:
        albedo = add_texture(b, f"{prefab_name}_Albedo", colour)
        normal = add_texture(b, f"{prefab_name}_Normal", (128, 128, 255))
        mat = add_material(b, f"{prefab_name}_Mat", {"_MainTex": albedo, "_BumpMap": normal, **(extra_textures or {})})

        root_go = b.add(1, m_Name=prefab_name, m_IsActive=True)
        root_tr = b.add(4, m_GameObject=pptr(root_go), m_LocalRotation={"w": 1.0},
                        m_LocalScale={"x": 1.0, "y": 1.0, "z": 1.0})
        animator = b.add(95, m_GameObject=pptr(root_go), m_Avatar=pptr(avatar), m_Controller=pptr(controller),
                         m_Enabled=1)

        body_go = b.add(1, m_Name=f"{prefab_name}_Body", m_IsActive=True)
        body_tr = b.add(4, m_GameObject=pptr(body_go), m_Father=pptr(root_tr), m_LocalRotation={"w": 1.0},
                        m_LocalScale={"x": 1.0, "y": 1.0, "z": 1.0})

        bone_tr: dict[str, int] = {}
        children_of: dict[int, list[int]] = {root_tr: [body_tr]}
        for path in bones:
            parent_path, _, leaf = path.rpartition("/")
            parent_tr = bone_tr[parent_path] if parent_path else root_tr
            go = b.add(1, m_Name=leaf, m_IsActive=True)
            if bone_locals:
                (px, py, pz), (qx, qy, qz, qw) = bone_locals[path]
                tr = b.add(4, m_GameObject=pptr(go), m_Father=pptr(parent_tr),
                           m_LocalRotation={"x": qx, "y": qy, "z": qz, "w": qw},
                           m_LocalPosition={"x": px, "y": py, "z": pz}, m_LocalScale={"x": 1.0, "y": 1.0, "z": 1.0})
            else:
                tr = b.add(4, m_GameObject=pptr(go), m_Father=pptr(parent_tr), m_LocalRotation={"w": 1.0},
                           m_LocalPosition={"x": 0.0, "y": 0.1 * (path.count("/") + 1), "z": 0.0},
                           m_LocalScale={"x": 1.0, "y": 1.0, "z": 1.0})
            b.get(go)["m_Component"] = [{"component": pptr(tr)}]
            bone_tr[path] = tr
            children_of.setdefault(parent_tr, []).append(tr)
        for tr, kids in children_of.items():
            b.get(tr)["m_Children"] = [pptr(k) for k in kids]

        smr = b.add(137, m_GameObject=pptr(body_go), m_Enabled=True, m_Mesh=pptr(mesh), m_Materials=[pptr(mat)],
                    m_Bones=[pptr(bone_tr[p]) for p in bones], m_RootBone=pptr(bone_tr[bones[0]]))
        b.get(root_go)["m_Component"] = [{"component": pptr(root_tr)}, {"component": pptr(animator)}]
        b.get(body_go)["m_Component"] = [{"component": pptr(body_tr)}, {"component": pptr(smr)}]
        return CreatureIds(root_go, mesh, [mat], [albedo, normal], clip_ids, bones)

    out = {"normal": prefab(name, rgb)}
    for variant, colour in (variants or {}).items():
        out[variant] = prefab(f"{name}_{variant.capitalize()}", colour)
    return out


def add_assetbundle(b: SerializedBuilder, name: str, container: dict[str, int]) -> int:
    ab_node = b.node(142)
    info = default_value(next(c for c in ab_node.m_Children if c.m_Name == "m_MainAsset"))
    entries = [(path, dict(info, asset=pptr(pid))) for path, pid in sorted(container.items())]
    return b.add(142, m_Name=name, m_AssetBundleName=name, m_Container=entries,
                 m_PreloadTable=[pptr(pid) for pid in container.values()])


DEFAULT_BONES = ["Root", "Root/Hips", "Root/Hips/Spine", "Root/Hips/Spine/Head", "Root/Hips/Tail"]


def creature_bundle(name: str = "Foxlet", cab: str = "CAB-foxlet", variants=None, clips=("Idle", "Walk", "Attack"),
                    bones=None, with_controller: bool = True) -> tuple[bytes, dict[str, CreatureIds]]:
    b = SerializedBuilder()
    ids = add_creature(b, name, bones=list(bones or DEFAULT_BONES), clips=list(clips),
                       variants=variants, with_controller=with_controller)
    add_assetbundle(b, f"creatures/{name.lower()}", {
        f"assets/creatures/{name.lower()}/{name.lower()}{'' if v == 'normal' else '_' + v}.prefab": c.root_go
        for v, c in ids.items()
    })
    return bundle(b.build(), cab), ids


def add_static_prop(b: SerializedBuilder, name: str) -> int:
    """A non-skinned prefab (MeshFilter + MeshRenderer), e.g. a rock or tree."""
    mesh = add_triangle_mesh(b, f"{name}_Mesh", [])
    mat = add_material(b, f"{name}_Mat", {"_MainTex": add_texture(b, f"{name}_Tex")})
    go = b.add(1, m_Name=name, m_IsActive=True)
    tr = b.add(4, m_GameObject=pptr(go), m_LocalRotation={"w": 1.0})
    mf = b.add(33, m_GameObject=pptr(go), m_Mesh=pptr(mesh))
    mr = b.add(23, m_GameObject=pptr(go), m_Enabled=True, m_Materials=[pptr(mat)])
    b.get(go)["m_Component"] = [{"component": pptr(tr)}, {"component": pptr(mf)}, {"component": pptr(mr)}]
    return go


# Binding attributes for Transform curves in compiled (muscle) clips.
ATTR_POSITION, ATTR_ROTATION, ATTR_SCALE, ATTR_EULER = 1, 2, 3, 4
_ATTR_WIDTH = {ATTR_POSITION: 3, ATTR_ROTATION: 4, ATTR_SCALE: 3, ATTR_EULER: 3}


def add_dense_clip(b: SerializedBuilder, name: str, tracks: list[tuple[str, int, list[tuple[float, ...]]]],
                   sample_rate: float = 30.0, events: list[tuple[float, str]] = ()) -> int:
    """An AnimationClip whose curves live in a compiled DenseClip, like a real build.

    ``tracks``: [(transform path, attribute, [per-frame value tuple])]. Rotation values
    are Unity quaternions (x, y, z, w); positions/scales are (x, y, z).
    """
    clip_node = b.node(74)
    frames = len(tracks[0][2])
    assert all(len(t[2]) == frames for t in tracks)
    samples = []
    for f in range(frames):
        for _path, _attr, values in tracks:
            samples.extend(float(v) for v in values[f])
    curve_count = sum(_ATTR_WIDTH[attr] for _p, attr, _v in tracks)
    muscle = default_value(next(c for c in clip_node.m_Children if c.m_Name == "m_MuscleClip"))
    muscle["m_Clip"]["data"]["m_DenseClip"] = {"m_FrameCount": frames, "m_CurveCount": curve_count,
                                               "m_SampleRate": sample_rate, "m_BeginTime": 0.0,
                                               "m_SampleArray": samples}
    muscle["m_StartTime"] = 0.0
    muscle["m_StopTime"] = (frames - 1) / sample_rate
    muscle["m_StartX"]["q"]["w"] = muscle["m_StopX"]["q"]["w"] = 1.0
    for k in ("m_StartX", "m_StopX"):
        muscle[k]["s"] = {"x": 1.0, "y": 1.0, "z": 1.0}
    muscle["m_IndexArray"] = list(range(curve_count))
    binding = default_value(next(c for c in clip_node.m_Children if c.m_Name == "m_ClipBindingConstant"))
    gb_node = next(c for c in clip_node.m_Children if c.m_Name == "m_ClipBindingConstant")
    gb_item = default_value(gb_node.m_Children[0].m_Children[0].m_Children[1])
    binding["genericBindings"] = [dict(gb_item, path=crc(p), attribute=a, typeID=4) for p, a, _v in tracks]
    ev_node = next(c for c in clip_node.m_Children if c.m_Name == "m_Events").m_Children[0].m_Children[1]
    ev_list = [dict(default_value(ev_node), time=t, functionName=fn) for t, fn in events]
    return b.add(74, m_Name=name, m_SampleRate=sample_rate, m_MuscleClip=muscle, m_MuscleClipSize=1,
                 m_ClipBindingConstant=binding, m_Events=ev_list)
