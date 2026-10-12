"""Creature catalog: find creature prefabs in an object index and associate their assets.

Association is graph-first, inference-second, and every association records its
evidence and provenance:

* ``verified``: an explicit reference in the game data (prefab hierarchy ->
  SkinnedMeshRenderer -> Mesh/Materials -> Textures; Animator -> Avatar /
  AnimatorController -> AnimationClips).
* ``inferred``: derived by rules: animation clips with no controller link that
  animate this skeleton (CRC32 bone-path hashes match), variant labels from
  name keywords, clip roles from name keywords.

Nothing here assumes a particular game's folder layout or naming scheme; all
keywords and thresholds come from ``CatalogRules``.
"""

from __future__ import annotations

import re
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.catalog.rules import CatalogRules, match_keyword, name_group, name_selected, tokens
from aniimo_pal.creature.variants import BASE_REGION, STANDARD, variant_key
from aniimo_pal.inventory import ObjectIndex

SCHEMA_ID = "aniimo-pal/catalog"
SCHEMA_VERSION = "0.1.0"

RENDERER_CLASSES = ("SkinnedMeshRenderer",)
STATIC_RENDERER_CLASSES = ("MeshRenderer",)
TRANSFORM_CLASSES = ("Transform", "RectTransform")
_BONE_FIELD = re.compile(r"^m_Bones\[(\d+)\]$")


def path_hash(path: str) -> int:
    """Unity binding path hash: CRC32 of the UTF-8 transform path."""
    return zlib.crc32(path.encode("utf-8"))


class _Hierarchy:
    """GameObject/Transform hierarchy helpers over an ObjectIndex."""

    def __init__(self, idx: ObjectIndex):
        self.idx = idx
        self.go_transform: dict[str, dict[str, Any]] = {}
        self.transform_go: dict[str, dict[str, Any]] = {}
        for cls in TRANSFORM_CLASSES:
            for tr in idx.by_class.get(cls, []):
                go = idx.ref(tr, "m_GameObject")
                if go is not None:
                    self.go_transform[go["id"]] = tr
                    self.transform_go[tr["id"]] = go

    def father(self, tr: dict[str, Any]) -> dict[str, Any] | None:
        return self.idx.ref(tr, "m_Father")

    def children(self, tr: dict[str, Any]) -> list[dict[str, Any]]:
        return self.idx.refs(tr, "m_Children")

    def components(self, go: dict[str, Any]) -> list[dict[str, Any]]:
        return self.idx.refs(go, "m_Component")

    def roots(self) -> list[dict[str, Any]]:
        return [go for go_id, tr in self.go_transform.items()
                if self.father(tr) is None and (go := self.idx.get(go_id)) is not None]

    def subtree(self, root_go: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
        """[(GameObject, path relative to root)] in depth-first order; root path is ''."""
        out = []
        root_tr = self.go_transform.get(root_go["id"])
        if root_tr is None:
            return [(root_go, "")]
        stack = [(root_tr, "")]
        seen = set()
        while stack:
            tr, path = stack.pop()
            if tr["id"] in seen:
                continue
            seen.add(tr["id"])
            go = self.transform_go.get(tr["id"])
            if go is not None:
                out.append((go, path))
            for child in reversed(self.children(tr)):
                child_go = self.transform_go.get(child["id"])
                name = (child_go or {}).get("name") or ""
                stack.append((child, f"{path}/{name}" if path else name))
        return out


def _ref_entry(idx: ObjectIndex, obj: dict[str, Any]) -> dict[str, Any]:
    return {"object": obj["id"], "class": obj["class"], "name": obj.get("name"),
            "container": obj.get("container"), "sha256": idx.container_hash.get(obj.get("container"))}


def _prefab_candidate(idx: ObjectIndex, h: _Hierarchy, root: dict[str, Any], rules: CatalogRules) -> dict[str, Any] | None:
    renderer_classes = RENDERER_CLASSES + (STATIC_RENDERER_CLASSES if rules.include_static_meshes else ())
    nodes = h.subtree(root)
    renderers, animators, legacy_anims, behaviours = [], [], [], []
    path_of_transform: dict[str, str] = {}
    local_trs: dict[str, dict[str, Any]] = {}
    own_active = {path: (go.get("info") or {}).get("active") is not False for go, path in nodes}
    for go, path in nodes:
        tr = h.go_transform.get(go["id"])
        if tr is not None:
            path_of_transform[tr["id"]] = path
            info = tr.get("info") or {}
            local_trs[path] = {"position": info.get("position"), "rotation": info.get("rotation"),
                               "scale": info.get("scale")}
        for comp in h.components(go):
            if comp["class"] in renderer_classes:
                renderers.append((go, comp))
            elif comp["class"] == "Animator":
                animators.append(comp)
            elif comp["class"] == "Animation":
                legacy_anims.append(comp)
            elif comp["class"] == "MonoBehaviour" and comp["refs"]:
                behaviours.append(comp)
    if not renderers:
        return None

    meshes: dict[str, dict[str, Any]] = {}
    skins: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    bones: dict[str, str] = {}
    for go, r in renderers:
        mesh = idx.ref(r, "m_Mesh")
        if mesh is None and r["class"] == "MeshRenderer":
            mf = next((c for c in h.components(go) if c["class"] == "MeshFilter"), None)
            mesh = idx.ref(mf, "m_Mesh") if mf else None
        if mesh is not None:
            meshes[mesh["id"]] = mesh
        for m in idx.refs(r, "m_Materials"):
            if m["id"] not in {x["id"] for x in materials}:
                materials.append(m)
        for b in idx.refs(r, "m_Bones"):
            if b["id"] in path_of_transform:
                bones[b["id"]] = path_of_transform[b["id"]]
        if r["class"] == "SkinnedMeshRenderer":
            # mesh None: no mesh reference at all (the game assigns it at runtime); resolved by name later.
            # Joint order matters: mesh blend indices refer to positions in m_Bones.
            ordered: dict[int, str | None] = {}
            for ref in r["refs"]:
                m = _BONE_FIELD.match(ref["field"])
                if m:
                    ordered[int(m.group(1))] = path_of_transform.get(ref["target"])
            root_bone = idx.ref(r, "m_RootBone")
            chain = _material_chain(idx, r, go, behaviours)
            for mid in chain["materials"]:  # materials assigned through a component belong to the prefab too
                if mid not in {x["id"] for x in materials}:
                    materials.append(idx.by_id[mid])
            skins.append({"renderer": r["id"], "mesh": mesh["id"] if mesh else None,
                          "mesh_name": mesh.get("name") if mesh else None,
                          "renderer_path": path_of_transform.get((h.go_transform.get(go["id"]) or {}).get("id")),
                          "bones": [ordered.get(i) for i in range(max(ordered) + 1)] if ordered else [],
                          "root_bone": path_of_transform.get(root_bone["id"]) if root_bone else None,
                          "materials": chain["materials"],
                          "materials_source": chain["source"], "materials_missing": chain["missing"],
                          "component_mesh": chain["mesh"], "path_assignments": chain["path_assignments"],
                          # Hidden in the prefab (this object or a parent inactive): an alternative state the
                          # game switches on at runtime, e.g. Grizbo's "Rampage" fur.
                          "active": _active_in_prefab(own_active, path_of_transform.get(
                              (h.go_transform.get(go["id"]) or {}).get("id")))})
    if not meshes and not skins:
        return None
    _mark_part_states(skins)

    controllers, avatars, clips = [], [], []
    for a in animators:
        av = idx.ref(a, "m_Avatar")
        if av is not None:
            avatars.append(av)
        ctrl = idx.ref(a, "m_Controller")
        if ctrl is not None:
            controllers.append(ctrl)
    for ctrl in list(controllers):
        if ctrl["class"] == "AnimatorOverrideController":
            base = idx.ref(ctrl, "m_Controller")
            if base is not None:
                controllers.append(base)
            clips.extend(idx.refs(ctrl, "m_Clips"))
        clips.extend(idx.refs(ctrl, "m_AnimationClips"))
    for la in legacy_anims:
        clips.extend(idx.refs(la, "m_Animations"))
    clips = [c for c in clips if c["class"] == "AnimationClip"]

    # Skeleton: every transform path under the root (bones are a subset).
    skeleton_paths = sorted(p for p in path_of_transform.values() if p)
    return {
        "root": root, "nodes": nodes, "meshes": list(meshes.values()), "materials": materials,
        "bone_paths": sorted(set(bones.values())), "skeleton_paths": skeleton_paths,
        "controllers": controllers, "avatars": avatars, "clips": clips, "local_trs": local_trs, "skins": skins,
    }


def _element_key(field: str) -> str:
    """``m_Parts[3].materials[0]`` -> ``m_Parts[3]``: the list element a component field belongs to."""
    head, sep, _tail = field.rpartition("].")
    return head + "]" if sep else ""


def _data_asset_parts(idx: ObjectIndex, refs: list[dict[str, Any]], depth: int = 2
                      ) -> tuple[list[str], list[str], str] | None:
    """Materials and the mesh held by data assets a component references (not other components).

    Returns (material ids, [mesh id], evidence path) or None. When an asset lists several meshes (e.g. per
    platform or LOD), the one under a field naming "default" wins, else the first; its materials come from the
    same list element.
    """
    for ref in refs:
        target = idx.get(ref["target"])
        if not target or target["class"] != "MonoBehaviour" or any(r["field"] == "m_GameObject"
                                                                  for r in target["refs"]):
            continue  # not a decoded data asset (components sit on GameObjects)
        name = target.get("name") or ref["target"]
        by_path = _path_configs(idx, target)
        if by_path:
            best = by_path[0]
            mats = [m["object"] for m in best["materials"] if m["object"] in idx.by_id]
            return (mats, [best["mesh"]["object"]] if best["mesh"] and best["mesh"]["object"] in idx.by_id else [],
                    f"{ref['field']}:{name}.{best['group']}", by_path)
        inner = [r for r in target["refs"] if r["field"] != "m_Script"]
        meshes = [r for r in inner if (idx.get(r["target"]) or {}).get("class") == "Mesh"]
        mats = [r for r in inner if (idx.get(r["target"]) or {}).get("class") == "Material"]
        if meshes or mats:
            mesh = next((m for m in meshes if "default" in m["field"].lower()), meshes[0] if meshes else None)
            if mesh is not None:
                key = _element_key(mesh["field"])
                same = [r for r in mats if key and _element_key(r["field"]) == key]
                mats = same or mats
            return ([r["target"] for r in mats], [mesh["target"]] if mesh else [],
                    f"{ref['field']}:{name}" + (f".{mesh['field']}" if mesh else ""), None)
        if depth > 1:
            deeper = _data_asset_parts(idx, inner, depth - 1)
            if deeper:
                return deeper[0], deeper[1], f"{ref['field']}:{name} -> {deeper[2]}", deeper[3]
    return None


_MATERIAL_FIELD = re.compile(r"^(?P<group>.*?)\.?(?P<list>[^.\[]*material[^.\[]*)\[(?P<k>\d+)\]", re.IGNORECASE)
_MESH_FIELD = re.compile(r"^(?P<group>.*?)\.?(?P<field>[^.\[]*mesh[^.\[]*)(\.|$)", re.IGNORECASE)
_MOBILE_WORDS = ("mobile", "android", "ios", "low")


def resolve_asset_path(idx: ObjectIndex, value: str, want: str | None = None) -> dict[str, Any]:
    """An asset path string -> object, exactly through the AssetBundle container paths (case-insensitive).

    ``want="Mesh"``: a model file's path resolves to its single mesh (from the asset's preload objects).
    Several bundles listing the same path (original and patch): the newest patch bundle (xptNN_) is used and
    the choice is reported.
    """
    copies = (idx.doc.get("asset_index") or {}).get(value.replace("\\", "/").lower())
    out: dict[str, Any] = {"path": value, "object": None}
    if not copies:
        out["status"] = "path not listed by any scanned bundle"
        return out
    copy = max(copies, key=lambda c: (_patch_level(c["container"]), c["container"]))
    if len({c["object"] for c in copies}) > 1:
        out["note"] = f"{len(copies)} bundles list this path; newest patch bundle used: {copy['container']}"
    obj = idx.get(copy["object"])
    cls = (obj or {}).get("class")
    if want == "Mesh" and cls != "Mesh":
        meshes = copy.get("meshes") or []
        if len(meshes) == 1:
            out.update(object=meshes[0], status=f"resolved (the model file's only mesh; main asset {cls or '?'})")
        else:
            out["status"] = f"model file has {len(meshes)} meshes; which one is not named"
            out["candidates"] = meshes[:8]
        return out
    out.update(object=copy["object"], status="resolved" if obj else "resolved (object not in the object list)",
               container=copy["container"])
    if obj is not None and want and cls != want:
        out["status"] = f"path names a {cls}, not a {want}"
        out["object"] = None
    return out


def _path_configs(idx: ObjectIndex, asset: dict[str, Any]) -> list[dict[str, Any]]:
    """Mesh/material assignments a data asset makes by path strings, grouped per renderer entry, best first.

    Field paths keep platform config, LOD and order, e.g. ``m_DefaultConfig.m_Lods[0].m_Renderers[0].m_MeshPath.m_Path``
    and ``...m_Renderers[0].m_MaterialList[2].m_Path``. Ranking: non-mobile config first (default/PC), then
    lowest LOD, then renderer entry; material order is kept.
    """
    groups: dict[str, dict[str, Any]] = {}
    for field, value in (asset.get("info") or {}).get("asset_path_strings") or []:
        m = _MATERIAL_FIELD.match(field)
        if m:
            g = groups.setdefault(m["group"], {"group": m["group"], "mesh": None, "materials": []})
            g["materials"].append(dict(resolve_asset_path(idx, value, "Material"), index=int(m["k"]), field=field))
            continue
        m = _MESH_FIELD.match(field)
        if m:
            g = groups.setdefault(m["group"], {"group": m["group"], "mesh": None, "materials": []})
            g["mesh"] = dict(resolve_asset_path(idx, value, "Mesh"), field=field)
    out = []
    for g in groups.values():
        g["materials"].sort(key=lambda x: x["index"])
        head = g["group"].split(".", 1)[0].lower()
        nums = [int(n) for n in re.findall(r"\[(\d+)\]", g["group"])]
        g["config"] = g["group"].split(".", 1)[0]
        g["mobile"] = any(w in head for w in _MOBILE_WORDS)
        g["lod"] = nums[0] if nums else 0
        g["rank"] = (g["mobile"], "default" not in head and "pc" not in head, *nums, g["group"])
        out.append(g)
    out.sort(key=lambda g: g.pop("rank"))
    return out


def _cab_owners(idx: ObjectIndex) -> dict[str, str]:
    owners = getattr(idx, "_cab_owner_cache", None)
    if owners is None:
        owners = {sf["cab"]: c["path"] for c in idx.doc.get("containers", []) for sf in c.get("serialized_files", [])}
        idx._cab_owner_cache = owners
    return owners


def _missing_targets(idx: ObjectIndex, refs: list[dict[str, Any]]) -> list[str]:
    owners = _cab_owners(idx)
    out = []
    for ref in refs:
        if ref["target"] in idx.by_id:
            continue
        cab = ref["target"].rsplit(":", 1)[0]
        out.append(f"{ref['target']} (in {owners[cab]}, object not recorded)" if cab in owners
                   else f"{ref['target']} (serialized file {cab} not in any scanned container)")
    return out


def _material_chain(idx: ObjectIndex, r: dict[str, Any], go: dict[str, Any],
                    behaviours: list[dict[str, Any]]) -> dict[str, Any]:
    """Where a renderer's materials come from, with evidence; why they are missing otherwise.

    1. the renderer's own ``m_Materials`` (verified);
    2. a component on the prefab whose list element references this renderer (or its GameObject) together
       with materials / a mesh, e.g. ``m_Parts[3].renderer`` + ``m_Parts[3].materials[0]`` (verified link,
       the component's meaning is inferred from it);
    3. otherwise ``missing`` says which case applies: no material slots at all, or slots pointing at objects
       the inventory did not record (and where they live).
    """
    raw = [ref for ref in r["refs"] if ref["field"] == "m_Materials" or ref["field"].startswith("m_Materials[")]
    resolved = [m["id"] for m in idx.refs(r, "m_Materials") if m["class"] == "Material"]
    out: dict[str, Any] = {"materials": resolved, "source": "renderer" if resolved else None, "missing": None,
                           "mesh": None, "path_assignments": None}
    for mb in behaviours:
        keys = {_element_key(ref["field"]) for ref in mb["refs"] if ref["target"] in (r["id"], go["id"])}
        if not keys:
            continue
        renderer_refs = [ref for ref in mb["refs"]
                         if (idx.get(ref["target"]) or {}).get("class") in RENDERER_CLASSES + STATIC_RENDERER_CLASSES]
        script = idx.ref(mb, "m_Script")
        label = ((script or {}).get("info") or {}).get("class_name") or (script or {}).get("name") or "component"
        for key in sorted(keys):
            if key == "" and len({ref["target"] for ref in renderer_refs}) > 1:
                continue  # top-level fields of a component that lists several renderers: no pairing
            group = [ref for ref in mb["refs"] if _element_key(ref["field"]) == key
                     and ref["field"] not in ("m_GameObject", "m_Script")]
            mats = [ref["target"] for ref in group if (idx.get(ref["target"]) or {}).get("class") == "Material"]
            meshes = [ref["target"] for ref in group if (idx.get(ref["target"]) or {}).get("class") == "Mesh"]
            evidence = f"component:{label}{'.' + key if key else ''}"
            if not mats and not meshes:
                # The component may point at a data asset (e.g. XRender PlatformData) that holds them.
                hop = _data_asset_parts(idx, [ref for ref in group if ref["field"] not in ("m_GameObject",
                                                                                           "m_Script")])
                if hop:
                    mats, meshes, via, configs = hop
                    evidence += f" -> {via}"
                    if configs:
                        out["path_assignments"] = configs
                        bad = [m for c in configs[:1] for m in ([c["mesh"]] if c["mesh"] else []) + c["materials"]
                               if m["object"] not in idx.by_id]
                        if bad:
                            out["missing"] = f"{evidence}: " + "; ".join(f"{b['path']} ({b['status']})"
                                                                       for b in bad[:3])
            if mats and not out["materials"]:
                out.update(materials=mats, source=evidence)
            if len(meshes) == 1 and out["mesh"] is None:
                out["mesh"] = {"object": meshes[0], "evidence": evidence}
            if not mats and not out["materials"]:
                missing = _missing_targets(idx, [ref for ref in group if ref["target"] not in (r["id"], go["id"])])
                if missing:
                    out["missing"] = f"{evidence} references objects not in the object list: {', '.join(missing[:3])}"
    if not out["materials"] and out["missing"] is None:
        if not raw:
            out["missing"] = ("no material assigned in the prefab (slots empty or absent) and no component on the "
                              "prefab references one for this renderer")
        else:
            unresolved = _missing_targets(idx, raw)
            out["missing"] = (f"material slots point at objects the inventory did not record: {', '.join(unresolved[:3])}"
                              if unresolved else "material slots point at objects that are not materials")
    return out


def _look_groups(idx: ObjectIndex, prefabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group prefabs by the set of textures (else materials) their parts use."""
    groups: dict[str, dict[str, Any]] = {}
    for p in prefabs:
        mats = sorted({m for part in p["parts"] for m in part["materials"]})
        textures = sorted({t["id"] for m in mats if (mo := idx.get(m)) for _slot, t in _material_textures(idx, mo)})
        if textures:
            sig = "textures:" + ",".join(textures)
        elif mats:
            sig = "materials:" + ",".join(mats)
        else:
            sig = "no-material-data"
        g = groups.setdefault(sig, {"signature": sig if sig == "no-material-data" else sig.split(":", 1)[0],
                                    "prefabs": [], "textures": [(idx.get(t) or {}).get("name") for t in textures][:8]})
        g["prefabs"].append(p["name"])
    return list(groups.values())


def _mark_part_states(skins: list[dict[str, Any]]) -> None:
    """Tag parts that belong to an alternative look the game switches to (e.g. Grizbo's Rampage).

    Real prefabs keep both looks active and name the parts ``<part>_Default`` / ``<part>_<State>``
    (``M_Parmon_10503_Fur01_Default``, ``..._Fur01_Rampage``, ``..._Fur02_Rampage``). A word is a state when
    it replaces ``Default`` on some part; every part ending in that word gets ``state`` = the word, parts
    ending in ``Default`` get ``default``, all others none (always shown). INFERRED from names.
    """
    names = [(s, tokens((s.get("renderer_path") or "").rsplit("/", 1)[-1])) for s in skins]
    default_bases = {tuple(t[:-1]) for _s, t in names if len(t) > 1 and t[-1] == "default"}
    states = {t[-1] for _s, t in names if len(t) > 1 and t[-1] != "default" and tuple(t[:-1]) in default_bases}
    for s, t in names:
        if len(t) > 1 and (t[-1] == "default" or t[-1] in states):
            s["state"] = t[-1]


def _active_in_prefab(own_active: dict[str, bool], path: str | None) -> bool:
    if path is None:
        return True
    parts = path.split("/") if path else []
    prefixes = [""] + ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return all(own_active.get(p, True) for p in prefixes)


def _material_textures(idx: ObjectIndex, mat: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """[(slot, texture)] using the material's slot order (null textures are already excluded)."""
    slots = (mat.get("info") or {}).get("texture_slots") or []
    textures = [t for t in idx.refs(mat, "m_SavedProperties.m_TexEnvs")]
    return [(slots[i] if i < len(slots) else f"slot{i}", t) for i, t in enumerate(textures)]


def _stem(name: str | None, rules: CatalogRules) -> str:
    variant_words = {w for words in rules.variant_keywords.values() for w in words}
    toks = [t for t in tokens(name) if t not in variant_words and t not in rules.stop_tokens
            and not (t.isdigit() and len(t) < rules.min_id_digits)]
    return "_".join(toks) or "creature"


def _display(name: str | None, rules: CatalogRules) -> str:
    return " ".join(t.capitalize() for t in _stem(name, rules).split("_"))


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def build_catalog(objects_doc: dict[str, Any], rules: CatalogRules | None = None,
                  names: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = rules or CatalogRules()
    idx = ObjectIndex(objects_doc)
    h = _Hierarchy(idx)

    all_candidates = []
    roots = h.roots()
    for root in sorted(roots, key=lambda g: g["id"]):
        cand = _prefab_candidate(idx, h, root, rules)
        if cand is not None:
            all_candidates.append(cand)
    placeholder_stats = _resolve_placeholder_meshes(idx, all_candidates, rules)
    # Prefabs whose only renderers have empty mesh slots and no mesh of that name are not models.
    no_mesh = [c for c in all_candidates if not (c["meshes"] or any(
        u["candidates"] or not u["empty_slot"] for u in c.get("unresolved_parts", [])))]
    all_candidates = [c for c in all_candidates if c not in no_mesh]
    _assign_game_ids(all_candidates + no_mesh, rules)
    model_bundles = _model_bundles(objects_doc, rules)
    for c in all_candidates + no_mesh:
        if not c["game_id"]:  # a model prefab named without its ID: its bundle names it
            c["game_id"] = next((gid for gid, info in model_bundles.items()
                                 if c["root"].get("container") in info["bundles"]), None)
    name_groups = _name_groups(all_candidates, rules)
    selected = {id(c) for c in all_candidates if name_selected(c["root"].get("name"), rules)}
    candidates = [c for c in all_candidates if id(c) in selected]
    # Creature IDs whose model bundle exists but no selected prefab carries the ID: use the model itself.
    covered = {c["game_id"] for c in candidates if c["game_id"]}
    for gid, info in model_bundles.items():
        if gid not in covered:
            for c in all_candidates:
                if id(c) not in selected and c["game_id"] == gid and c["root"].get("container") in info["bundles"]:
                    c["model_only"] = True
                    candidates.append(c)

    # Group prefabs into species: shared mesh (verified), same game ID (forms), else same skeleton + name stem.
    uf = _UnionFind(len(candidates))
    group_evidence: dict[int, set[str]] = defaultdict(set)
    by_base: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(candidates):
        if c["game_id"]:
            by_base[c["game_id"]].append(i)
    for members in by_base.values():
        for a in members:
            for b in members:
                if a < b and _prefixes_compatible(candidates[a]["id_prefix"], candidates[b]["id_prefix"]) \
                        and uf.find(a) != uf.find(b):
                    uf.union(a, b)
                    group_evidence[b].add("same-game-id")
    mesh_owner: dict[str, int] = {}
    mesh_users: dict[str, int] = defaultdict(int)
    for c in candidates:
        for m in c["meshes"]:
            mesh_users[m["id"]] += 1
    for i, c in enumerate(candidates):
        for m in c["meshes"]:
            if mesh_users[m["id"]] > rules.max_shared_mesh_prefabs:
                continue
            if m["id"] in mesh_owner:
                uf.union(i, mesh_owner[m["id"]])
                group_evidence[i].add("shared-mesh")
            else:
                mesh_owner[m["id"]] = i
    by_stem_skel: dict[tuple[str, tuple[str, ...]], int] = {}
    for i, c in enumerate(candidates):
        key = (_stem(c["root"].get("name"), rules), tuple(c["bone_paths"]))
        if key[1] and key in by_stem_skel and uf.find(i) != uf.find(by_stem_skel[key]):
            uf.union(i, by_stem_skel[key])
            group_evidence[i].add("same-skeleton+name-stem")
        by_stem_skel.setdefault(key, i)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(candidates)):
        groups[uf.find(i)].append(i)

    linked_clip_ids = {clip["id"] for c in candidates for clip in c["clips"]}
    orphan_clips = [c for c in idx.by_class.get("AnimationClip", []) if c["id"] not in linked_clip_ids]

    species_list = []
    used_keys: set[str] = set()
    for members in sorted(groups.values(), key=lambda ms: candidates[ms[0]]["root"]["id"]):
        species_list.append(_species(idx, rules, [candidates[i] for i in members],
                                     sorted({e for i in members for e in group_evidence[i]}), used_keys))
    ambiguous = _link_clips_by_binding(idx, rules, species_list, orphan_clips)
    for s in species_list:
        del s["_skel_hashes"]
        anims = s["animations"]
        anims.sort(key=lambda a: (a["name"] or "", a["object"]))
        for a in anims:
            if a["binding_overlap"][1] and a["binding_overlap"][0] < a["binding_overlap"][1]:
                s["warnings"].append(f"clip {a['name']!r} animates {a['binding_overlap'][1] - a['binding_overlap'][0]} "
                                     "paths not present in the skeleton")

    claimed = {a["object"] for s in species_list for a in s["animations"]}
    doc = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"objects_created_utc": objects_doc.get("created_utc"),
                   "root_label": (objects_doc.get("manifest") or {}).get("root_label")},
        "rules": rules.to_dict(),
        "summary": {
            "species_count": len(species_list),
            # Distinct creature IDs (forms and NPC variants of one creature share its ID).
            "game_id_count": len({s["game_id"] for s in species_list if s.get("game_id")}),
            "prefab_count": len(candidates),
            "unassigned_clip_count": sum(1 for c in idx.by_class.get("AnimationClip", []) if c["id"] not in claimed),
            "ambiguous_clip_count": ambiguous,
            "prefabs_filtered_out": len(all_candidates) - len(candidates),
            "placeholder_meshes": placeholder_stats,
            "name_groups": name_groups,
            "id_coverage": _id_coverage(objects_doc, rules, model_bundles, species_list, all_candidates, no_mesh),
            "diagnostics": dict(_diagnostics(idx, h, roots, rules),
                                # One object id, several decoded records that differ (e.g. a StreamingAssets bundle
                                # and a newer CacheBundleFiles download share a CAB name). Not resolved by guessing.
                                duplicate_objects=len(idx.copies), conflicting_copies=len(idx.conflicts),
                                conflict_examples=idx.conflicts[:20]),
        },
        "species": species_list,
    }
    if names is not None:
        from aniimo_pal.catalog.names import apply_names

        apply_names(doc, names)
    return doc


def _model_bundles(objects_doc: dict[str, Any], rules: CatalogRules) -> dict[str, dict[str, Any]]:
    """{creature id: {codename, bundles}} from container names matching ``rules.model_bundle_pattern``."""
    if not rules.model_bundle_pattern:
        return {}
    pattern = re.compile(rules.model_bundle_pattern, re.IGNORECASE)
    out: dict[str, dict[str, Any]] = {}
    for c in objects_doc.get("containers", []):
        m = pattern.search(c["path"].replace("\\", "/").rsplit("/", 1)[-1])
        if m:
            entry = out.setdefault(m.group("id"), {"codename": None, "bundles": []})
            entry["bundles"].append(c["path"])
            if "codename" in pattern.groupindex and m.group("codename"):
                entry["codename"] = m.group("codename")
    return out


def _id_coverage(objects_doc: dict[str, Any], rules: CatalogRules, model_bundles: dict[str, dict[str, Any]],
                 species_list: list[dict[str, Any]], all_candidates: list[dict[str, Any]],
                 no_mesh: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Creature IDs that have a model bundle in the game files, compared with the catalog (with reasons)."""
    if not rules.model_bundle_pattern:
        return None
    containers = {c["path"]: c for c in objects_doc.get("containers", [])}
    in_catalog = {s["game_id"] for s in species_list if s.get("game_id")}
    model_only = sorted(s["game_id"] for s in species_list if s.get("model_only"))
    missing = []
    for gid, info in sorted(model_bundles.items()):
        if gid in in_catalog:
            continue
        filtered = sorted({c["root"].get("name") for c in all_candidates if c["game_id"] == gid})
        meshless = sorted({c["root"].get("name") for c in no_mesh if c["game_id"] == gid})
        records = [containers[b] for b in info["bundles"] if b in containers]
        skinned = sum((r.get("class_counts") or {}).get("SkinnedMeshRenderer", 0) for r in records)
        if filtered:
            reason = f"rigged prefabs with this ID exist but none passed the name filter: {', '.join(filtered[:4])}"
        elif meshless:
            reason = (f"prefab(s) {', '.join(meshless[:4])} found, but their renderers have empty mesh slots and no "
                      "mesh with the renderer's name exists")
        elif not records:
            reason = "model bundle not in the object list (not scanned or not a Unity container)"
        elif not skinned:
            reason = "model bundle has no skinned renderer (no rigged model in it)"
        else:
            reason = "model bundle has skinned renderers, but no prefab root with them was found"
        missing.append({"id": gid, "codename": info["codename"], "bundles": info["bundles"][:3], "reason": reason})
    return {"pattern": rules.model_bundle_pattern, "ids_with_model_bundle": len(model_bundles),
            "in_catalog": len(set(model_bundles) & in_catalog), "model_only": model_only,
            "catalog_ids_without_model_bundle": sorted(in_catalog - set(model_bundles)), "missing": missing}


def _diagnostics(idx: ObjectIndex, h: _Hierarchy, roots: list[dict[str, Any]], rules: CatalogRules) -> dict[str, Any]:
    """Counts that explain an empty or small catalog (which link from renderer to prefab is missing)."""
    renderer_classes = RENDERER_CLASSES + (STATIC_RENDERER_CLASSES if rules.include_static_meshes else ())
    renderers = [r for cls in renderer_classes for r in idx.by_class.get(cls, [])]
    with_mesh = with_go = in_hierarchy = no_mesh_ref = 0
    missing_mesh_targets: dict[str, int] = defaultdict(int)
    for r in renderers:
        mesh_ref = next((ref for ref in r["refs"] if ref["field"] == "m_Mesh"), None)
        if idx.ref(r, "m_Mesh") is not None:
            with_mesh += 1
        elif mesh_ref is not None:
            missing_mesh_targets[mesh_ref["target"].rsplit(":", 1)[0]] += 1
        elif r["class"] == "SkinnedMeshRenderer":
            no_mesh_ref += 1
        go = idx.ref(r, "m_GameObject")
        if go is not None:
            with_go += 1
            if go["id"] in h.go_transform:
                in_hierarchy += 1
    owners = {sf["cab"]: c["path"] for c in idx.doc.get("containers", []) for sf in c.get("serialized_files", [])}
    return {
        "root_gameobjects": len(roots),
        "renderers": len(renderers),
        "renderers_with_mesh": with_mesh,
        # Skinned renderers with an empty mesh slot: the game assigns the mesh at runtime.
        "renderers_without_mesh_reference": no_mesh_ref,
        "renderers_with_gameobject": with_go,
        "renderers_in_hierarchy": in_hierarchy,
        # Where unresolved meshes live: the serialized file and the container that holds it (if any was read).
        "missing_mesh_files": [{"cab": cab, "count": n, "container": owners.get(cab)}
                               for cab, n in sorted(missing_mesh_targets.items(), key=lambda kv: -kv[1])[:10]],
    }


_TRAILING_BUILD_NUMBER = re.compile(r"_-?\d{6,}$")


def _resolve_placeholder_meshes(idx: ObjectIndex, candidates: list[dict[str, Any]],
                                rules: CatalogRules) -> dict[str, Any]:
    """Replace placeholder meshes on renderers with the real mesh, found by the renderer's object name.

    Real Aniimo prefabs often point every SkinnedMeshRenderer at one shared placeholder
    (``SkinDummyMesh``) and attach the real mesh at runtime. The real mesh is named after the renderer's
    GameObject plus a build number (renderer ``M_Parmon_10503_Body01`` -> mesh
    ``M_Parmon_10503_Body01_1597918372``). A match is used only when exactly one mesh name fits;
    it is recorded as inferred (``mesh_source: renderer-name``).
    """
    users: dict[str, int] = defaultdict(int)
    for c in candidates:
        for m in c["meshes"]:
            users[m["id"]] += 1
    placeholders = {mid for mid, n in users.items() if n > rules.max_shared_mesh_prefabs}
    placeholders |= {m["id"] for m in idx.by_class.get("Mesh", []) if "dummy" in tokens(m.get("name"))}
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in idx.by_class.get("Mesh", []):
        if m["id"] in placeholders or not m.get("name"):
            continue
        name = m["name"]
        by_name[_norm_name(name)].append(m)
        stripped = _TRAILING_BUILD_NUMBER.sub("", name)
        if stripped != name:
            by_name[_norm_name(stripped)].append(m)
    resolved = unresolved = 0
    examples: list[str] = []
    for c in candidates:
        if not any(s["mesh"] is None or s["mesh"] in placeholders for s in c["skins"]):
            continue
        meshes = {m["id"]: m for m in c["meshes"] if m["id"] not in placeholders}
        id_tokens = {t for t in tokens(c["root"].get("name")) if t.isdigit() and len(t) >= rules.min_id_digits}
        # The creature ID without a form index (NG_P_Parmon_1018403 -> 10184): its bundles name that ID.
        base_tokens = {t[:-rules.form_index_digits] for t in id_tokens
                       if len(t) - rules.form_index_digits >= rules.min_id_digits} - id_tokens
        skeleton_hashes = _skeleton_hashes(c)
        # Build numbers of real meshes this prefab already uses directly: its other parts share them.
        own_numbers: dict[str, int] = defaultdict(int)
        for m in meshes.values():
            if (mb := _MESH_BUILD_NAME.match(m.get("name") or "")) is not None:
                own_numbers[mb["num"]] += 1
        pending = []
        votes: dict[str, int] = defaultdict(int)
        for s in c["skins"]:
            if s["mesh"] is not None and s["mesh"] not in placeholders:
                continue
            comp = s.get("component_mesh")
            if comp and comp["object"] in idx.by_id:
                real = idx.by_id[comp["object"]]
                s.update(placeholder_mesh=s["mesh"], mesh=real["id"], mesh_name=real.get("name"),
                         mesh_source=comp["evidence"], mesh_provenance="verified-link",
                         mesh_selection={"criterion": "component reference", "candidates": [real.get("name")]})
                meshes[real["id"]] = real
                resolved += 1
                continue
            go_name = (s.get("renderer_path") or c["root"].get("name") or "").rsplit("/", 1)[-1]
            found = by_name.get(_norm_name(go_name), [])
            by_num: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for m in found:
                mb = _MESH_BUILD_NAME.match(m["name"])
                by_num[mb["num"] if mb else ""].append(m)
            for num in by_num:
                votes[num] += 1
            pending.append((s, go_name, found, by_num))
        for s, go_name, found, by_num in pending:
            names = {m["name"] for m in found}
            choice, how, selection = None, "renderer-name", {"criterion": "only one mesh with this name",
                                                             "candidates": sorted(names)}
            if len(names) == 1:
                choice = found
            elif by_num:
                # Same base name, several build numbers (other versions of the model). In order:
                # 1. the mesh's bone hashes fit this renderer (only when some version fits fully),
                # 2. its bundle names this prefab's ID, then the creature ID without the form index,
                # 3. the build number shared with the prefab's directly referenced parts,
                # 4. the newest patch bundle (xptNN_...: a later build of the same asset replaces the old one),
                # 5. the build number most of this prefab's parts can use.
                fits = {num: max(_bone_fit(m, s, skeleton_hashes) for m in group) for num, group in by_num.items()}
                use_fit = 2 in fits.values()
                score = {num: (int(use_fit and fits[num] == 2),
                               max(len(id_tokens & set(tokens(m.get("container")))) for m in group),
                               max(len(base_tokens & set(tokens(m.get("container")))) for m in group),
                               own_numbers.get(num, 0), max(_patch_level(m.get("container")) for m in group),
                               votes[num]) for num, group in by_num.items()}
                ranked = sorted(by_num, key=lambda n: tuple(-x for x in score[n]) + (n,))
                decided_by = None
                if len(ranked) > 1:
                    first, second = score[ranked[0]], score[ranked[1]]
                    decided_by = next((_SCORE_NAMES[i] for i in range(len(first)) if first[i] != second[i]), None)
                selection = {"criterion": decided_by, "candidates": [
                    {"build": n, "names": sorted({m["name"] for m in by_num[n]}),
                     "containers": sorted({m.get("container") or "?" for m in by_num[n]})[:3],
                     **dict(zip(_SCORE_NAMES, score[n], strict=True))} for n in ranked[:4]]}
                if (len(ranked) == 1 or score[ranked[0]] > score[ranked[1]]) and \
                        len({m["name"] for m in by_num[ranked[0]]}) == 1:
                    choice, how = by_num[ranked[0]], "renderer-name+build-number"
            if choice:
                # The same mesh can sit in an old bundle and its patch: take the newest copy.
                real = sorted(choice, key=lambda m: (-_patch_level(m.get("container")), m["id"]))[0]
                s.update(placeholder_mesh=s["mesh"], mesh=real["id"], mesh_name=real["name"],
                         mesh_source=how, mesh_provenance="inferred", mesh_selection=selection,
                         # An empty slot or placeholder only shows the mesh is not in the prefab; which mesh the
                         # game loads is inferred from names, not observed.
                         mesh_uncertainty="runtime assignment not observed; chosen by renderer name"
                                          + (f", decided by {selection['criterion']}" if selection.get("criterion")
                                             and how != "renderer-name" else ""))
                meshes[real["id"]] = real
                resolved += 1
            else:
                s.update(placeholder_mesh=s["mesh"], mesh=None)
                c.setdefault("unresolved_parts", []).append({"renderer": go_name, "candidates": sorted(names)[:8],
                                                             "selection": selection if len(names) > 1 else None,
                                                             "state": s.get("state"), "active": s.get("active"),
                                                             "empty_slot": s["placeholder_mesh"] is None})
                unresolved += 1
                if len(examples) < 10:
                    examples.append(f"{c['root'].get('name')}: renderer {go_name!r} "
                                    f"({'no' if not names else len(names)} mesh name match)")
        c["meshes"] = list(meshes.values())
        c["skins"] = [s for s in c["skins"] if s["mesh"]]
    return {"placeholder_mesh_ids": sorted(placeholders), "renderers_resolved": resolved,
            "renderers_unresolved": unresolved, "unresolved_examples": examples}


_MESH_BUILD_NAME = re.compile(r"^(?P<base>.+)_(?P<num>-?\d{6,})$")
_SCORE_NAMES = ("bone_fit", "bundle_names_prefab_id", "bundle_names_creature_id", "shared_build_number",
                "patch_level", "parts_with_this_build")
_PATCH_PREFIX = re.compile(r"^xpt(\d+)_", re.IGNORECASE)


def _patch_level(container: str | None) -> int:
    """``xpt25_..._ar_char_parm_10321_rose_fbx_...uab`` -> 25 (a patch bundle); plain bundles -> 0."""
    m = _PATCH_PREFIX.match((container or "").replace("\\", "/").rsplit("/", 1)[-1])
    return int(m.group(1)) if m else 0


def _skeleton_hashes(c: dict[str, Any]) -> set[int]:
    """Path hashes of a prefab's transforms in the forms Unity may have hashed (path, name, root-prefixed)."""
    root = c["root"].get("name") or ""
    out = set()
    for p in c["skeleton_paths"]:
        out |= {path_hash(p), path_hash(p.rsplit("/", 1)[-1]), path_hash(f"{root}/{p}")}
    return out


def _bone_fit(mesh: dict[str, Any], skin: dict[str, Any], skeleton_hashes: set[int]) -> int:
    """2: the mesh's bone hashes are all transforms of this prefab (and the count fits the renderer);
    0: they are not; 1: unknown (mesh only surveyed, or no bone data)."""
    hashes = (mesh.get("info") or {}).get("bone_name_hashes")
    if not hashes:
        return 1
    if skin.get("bones") and len(skin["bones"]) < len(hashes):
        return 0
    return 2 if set(hashes) <= skeleton_hashes else 0


def _norm_name(name: str | None) -> str:
    return "_".join(tokens(name))


def _assign_game_ids(candidates: list[dict[str, Any]], rules: CatalogRules) -> None:
    """Set game_id / game_form / id_prefix on each candidate from its name.

    ``P_Parmon_10184`` -> id 10184; ``NG_P_Parmon_1018403`` -> id 10184, form 03 (only when 10184 is also
    the ID of another prefab, so plain long IDs are never split).
    """
    raw = []
    for c in candidates:
        toks = tokens(c["root"].get("name"))
        pos = next((i for i, t in enumerate(toks) if t.isdigit() and len(t) >= rules.min_id_digits), None)
        raw.append(("_".join(toks[:pos]), toks[pos]) if pos is not None else ("", None))
    seen = {gid for _p, gid in raw if gid}
    n = rules.form_index_digits
    for c, (prefix, gid) in zip(candidates, raw, strict=True):
        base, form = gid, None
        if gid and len(gid) - n >= rules.min_id_digits and gid[:-n] in seen:
            base, form = gid[:-n], gid[-n:]
        c["game_id"], c["game_form"], c["id_prefix"] = base, form, prefix


def _prefixes_compatible(a: str, b: str) -> bool:
    """``p_parmon`` and ``ng_p_parmon`` name the same kind of object; ``eff_parmon`` does not."""
    return not a or not b or a == b or a.endswith("_" + b) or b.endswith("_" + a)


def _name_groups(candidates: list[dict[str, Any]], rules: CatalogRules, limit: int = 400) -> list[dict[str, Any]]:
    """Rigged prefabs grouped by name prefix (e.g. p_parmon, eff_parmon, m_item), to choose which are creatures."""
    groups: dict[str, dict[str, Any]] = {}
    for c in candidates:
        name = c["root"].get("name")
        prefix, has_id = name_group(name, rules.min_id_digits)
        g = groups.setdefault(prefix, {"prefix": prefix, "prefabs": 0, "with_id": 0, "examples": [],
                                       "selected": name_selected(name, rules)})
        g["prefabs"] += 1
        g["with_id"] += has_id
        if len(g["examples"]) < 3:
            g["examples"].append(name)
    return sorted(groups.values(), key=lambda g: (-g["prefabs"], g["prefix"]))[:limit]


_GENERIC_NAME_TOKENS = {"tga", "png", "psd", "jpg", "jpeg", "exr", "asset", "mat", "fbx", "prefab", "anim", "controller",
                        "mesh", "tex", "texture", "shader", "lod", "uab", "bundle", "normal", "mask", "albedo",
                        "diffuse", "basecolor", "base", "color", "colour", "emission", "emissive", "metallic",
                        "roughness", "occlusion", "specular", "alpha", "detail", "material", "head", "eye", "eyes",
                        "face", "hair", "tail", "wing", "wings", "set", "skill", "eff", "vfx", "fx", "idle"}


def _codename(rules: CatalogRules, base_name: str | None, texts: list[str]) -> dict[str, Any] | None:
    """Guess an internal codename from file/asset names that put a word right after the species' ID number.

    E.g. ID 10503 and a texture bundle ``aexp_tcpm_10503_sleepybear_tga_0_<hash>.uab`` -> ``sleepybear``
    (pattern seen in a real Aniimo install; only a hint, other games or files may not follow it).
    INFERRED: names are only hints; the user decides.
    """
    ids = [t for t in tokens(base_name) if t.isdigit() and len(t) >= rules.min_id_digits]
    if not ids:
        return None
    skip = _GENERIC_NAME_TOKENS | set(rules.stop_tokens) | {w for ws in rules.variant_keywords.values() for w in ws}
    counts: dict[str, int] = defaultdict(int)
    example: dict[str, str] = {}
    for text in texts:
        toks = tokens(text.rsplit("/", 1)[-1])
        for i, t in enumerate(toks[:-1]):
            nxt = toks[i + 1]
            if t in ids and nxt.isalpha() and len(nxt) >= 3 and nxt not in skip:
                counts[nxt] += 1
                example.setdefault(nxt, text)
    if not counts:
        return None
    best = max(sorted(counts), key=lambda w: counts[w])
    return {"value": best, "provenance": "inferred", "id": ids[0],
            "evidence": f"{counts[best]} name(s) like {example[best].rsplit('/', 1)[-1]!r}",
            "alternatives": sorted((w for w in counts if w != best), key=lambda w: -counts[w])[:3]}


def _link_clips_by_binding(idx: ObjectIndex, rules: CatalogRules, species_list: list[dict[str, Any]],
                           orphan_clips: list[dict[str, Any]]) -> int:
    """Associate unlinked clips with species whose skeleton contains the paths the clip animates.

    Clips are grouped by their exact set of bound paths and indexed once (bone hash -> clip groups), so
    the cost grows with distinct rigs, not with species x clips. A clip that fits more than
    ``rules.max_binding_match_species`` species is left unassigned. Returns the number of such clips.
    """
    groups: dict[frozenset[int], list[dict[str, Any]]] = defaultdict(list)
    for clip in orphan_clips:
        hashes = frozenset((clip.get("info") or {}).get("binding_path_hashes") or [])
        if len(hashes) >= rules.min_binding_matches:
            groups[hashes].append(clip)
    signatures = list(groups)
    by_hash: dict[int, list[int]] = defaultdict(list)
    for i, sig in enumerate(signatures):
        for h in sig:
            by_hash[h].append(i)
    cache: dict[frozenset[int], list[int]] = {}
    matches: dict[int, list[int]] = defaultdict(list)  # signature index -> species indices
    for si, s in enumerate(species_list):
        skel = s["_skel_hashes"]
        if skel not in cache:
            counts: dict[int, int] = defaultdict(int)
            for h in skel:
                for i in by_hash.get(h, ()):
                    counts[i] += 1
            cache[skel] = [i for i, n in counts.items()
                           if n >= rules.min_binding_matches and n / len(signatures[i]) >= rules.min_binding_ratio]
        for i in cache[skel]:
            matches[i].append(si)
    ambiguous = 0
    for i, species_idx in matches.items():
        if len(species_idx) > rules.max_binding_match_species:
            ambiguous += len(groups[signatures[i]])
            continue
        for si in species_idx:
            s = species_list[si]
            have = {a["object"] for a in s["animations"]}
            for clip in groups[signatures[i]]:
                if clip["id"] not in have:
                    s["animations"].append(_anim_entry(idx, clip, rules, s["_skel_hashes"], "binding-match", "inferred"))
    return ambiguous


def _species(idx: ObjectIndex, rules: CatalogRules, members: list[dict[str, Any]], group_evidence: list[str],
             used_keys: set[str]) -> dict[str, Any]:
    warnings: list[str] = []
    # --- identity classification (form and Shiny detected independently) -----
    prismana_words = {"prismana": rules.variant_keywords.get("prismana", [])}
    shiny_words = {"shiny": rules.variant_keywords.get("shiny", [])}
    prefabs = []
    for c in members:
        root = c["root"]
        texts = [("prefab-name", root.get("name"))]
        if root.get("asset_path"):
            texts.append(("asset-path", re.sub(r"\.prefab$", "", root["asset_path"].rsplit("/", 1)[-1])))
        texts += [("material-name", m.get("name")) for m in c["materials"]]
        evidence = []
        hits = {}
        for label, table in (("prismana", prismana_words), ("shiny", shiny_words)):
            for source, text in texts:
                hit = match_keyword(text, table)
                if hit:
                    hits[label] = True
                    evidence.append(f"{source}-keyword:{hit[1]}")
                    break
        form = "prismana" if hits.get("prismana") else "normal"
        game_form = c.get("game_form")
        if game_form and int(game_form) != 0 and not hits:
            # A numbered game form (e.g. 03): its meaning (regional form, Prismana, ...) differs per creature,
            # so it is left for the user to classify instead of guessed.
            form = "unclassified"
            evidence.append(f"game-form-index:{game_form}")
        prefabs.append({"object": root["id"], "name": root.get("name"), "asset_path": root.get("asset_path"),
                        "container": root.get("container"), "region": BASE_REGION,
                        "form": form, "appearance": "shiny" if hits.get("shiny") else STANDARD,
                        "game_form_index": game_form,
                        # Per skinned part: its mesh and the materials this prefab's renderer uses (textures
                        # differ per variant even when the mesh is shared).
                        "parts": [{"mesh": s["mesh"], "mesh_name": s["mesh_name"], "materials": s["materials"],
                                   "materials_source": s.get("materials_source"),
                                   "materials_missing": s.get("materials_missing"), "state": s.get("state"),
                                   "mesh_source": s.get("mesh_source"), "mesh_uncertainty": s.get("mesh_uncertainty")}
                                  for s in c["skins"]],
                        "variant_provenance": "inferred" if hits or game_form else "default",
                        "variant_evidence": evidence, "_cand": c, "_model_only": bool(c.get("model_only"))})

    evidence_groups: list[dict[str, Any]] = []
    for form in ("normal", "prismana"):
        standard = sorted((p for p in prefabs if p["form"] == form and p["appearance"] == STANDARD),
                          key=lambda p: (p["game_form_index"] is not None, len(p["name"] or ""), p["object"]))
        for p in standard[1:]:
            p["form"] = "unclassified"
            p["variant_evidence"].append("additional-prefab-without-distinguishing-keyword")
        if len(standard) > 1:
            warnings.append(f"{len(standard)} prefabs look like the {form} standard appearance; "
                            "extra ones marked 'unclassified'")
        shinies = sorted((p for p in prefabs if p["form"] == form and p["appearance"] == "shiny"),
                         key=lambda p: (p["name"] or "", p["object"]))
        # Which of the (Aniimo: 12) Shiny looks a prefab shows is not in its name; P_/NG_/E_ prefixes may be
        # technical copies of one look. Shiny prefabs keep appearance "shiny" (look unidentified); what the
        # data does show is whether they use the same or different materials/textures.
        for p in shinies:
            p["appearance_identity"] = "unidentified"
            p["variant_evidence"].append("shiny-keyword; which Shiny look is not identified")
        if shinies:
            looks = _look_groups(idx, shinies)
            evidence_groups.append({"form": form, "prefabs": [p["name"] for p in shinies], "groups": looks})
            if len(shinies) == 1:
                verdict = "single Shiny prefab"
            elif any(g["signature"] == "no-material-data" for g in looks):
                verdict = "no material data, so it is unknown whether they differ"
            elif len(looks) == 1:
                verdict = "all use the same materials/textures (one look)"
            else:
                verdict = f"{len(looks)} different material/texture sets"
            warnings.append(f"{len(shinies)} {form} Shiny prefab(s) ({', '.join(p['name'] for p in shinies)}): "
                            f"look not identified; {verdict}")
    for p in prefabs:
        p["variant"] = "unclassified" if p["form"] == "unclassified" else variant_key(p["form"], p["appearance"])

    normals = [p for p in prefabs if p["form"] == "normal" and p["appearance"] == STANDARD]
    if not normals:
        warnings.append("no prefab without a variant keyword; normal variant must be chosen manually")

    normals.sort(key=lambda p: (p["game_form_index"] is not None, len(p["name"] or ""), p["object"]))
    base = normals[0] if normals else prefabs[0]
    game_id = base["_cand"].get("game_id")
    stem = _stem(base["name"], rules)
    key = stem
    n = 2
    while key in used_keys:
        key = f"{stem}_{n}"
        n += 1
    used_keys.add(key)

    # --- assets -------------------------------------------------------------
    assets: dict[str, dict[str, Any]] = {}

    def add(obj: dict[str, Any], role: str, scope: tuple[str, str], evidence: str, provenance: str = "verified",
            slot: str | None = None) -> None:
        entry = assets.get(obj["id"])
        if entry is None:
            entry = dict(_ref_entry(idx, obj), role=role, scopes=set(), evidence=[], provenance=provenance)
            if slot:
                entry["slot"] = slot
            assets[obj["id"]] = entry
        entry["scopes"].add(scope)
        if evidence not in entry["evidence"]:
            entry["evidence"].append(evidence)

    for p in prefabs:
        c, scope = p["_cand"], (p["form"], p["appearance"])
        for m in c["meshes"]:
            add(m, "mesh", scope, "prefab>renderer>mesh")
        for mat in c["materials"]:
            add(mat, "material", scope, "prefab>renderer>material")
            for slot, tex in _material_textures(idx, mat):
                add(tex, "texture", scope, f"material>{slot}", slot=slot)
        for av in c["avatars"]:
            add(av, "skeleton", scope, "prefab>animator>avatar")
        for ctrl in c["controllers"]:
            add(ctrl, "other", scope, "prefab>animator>controller")

    species_forms = {p["form"] for p in prefabs}
    species_apps = {p["appearance"] for p in prefabs}
    asset_list = []
    for entry in assets.values():
        scopes = entry.pop("scopes")
        forms = {f for f, _a in scopes}
        apps = {a for _f, a in scopes}
        entry["region"] = "any"
        entry["form"] = "shared" if (len(forms) > 1 or forms == species_forms) else next(iter(forms))
        if len(apps) == 1 and apps != species_apps:
            entry["appearance"] = next(iter(apps))
        elif STANDARD in apps or apps == species_apps:
            entry["appearance"] = "any"
        else:
            entry["appearance"] = "shiny"
        asset_list.append(entry)
    asset_list.sort(key=lambda a: (a["role"], a["form"], a["appearance"], a["name"] or "", a["object"]))

    # --- skeleton -------------------------------------------------------------
    base_cand = base["_cand"]
    skel_paths = base_cand["skeleton_paths"]
    bone_paths = set(base_cand["bone_paths"])
    local_trs = base_cand["local_trs"]
    skeleton = {
        "root_object": base["object"],
        "root_name": base["name"],
        "bones": [{"path": p, "name": p.rsplit("/", 1)[-1], "parent": p.rsplit("/", 1)[0] if "/" in p else None,
                   "skinned": p in bone_paths, "local": local_trs.get(p)} for p in skel_paths],
        "avatar": base_cand["avatars"][0]["id"] if base_cand["avatars"] else None,
        # Per skinned renderer: mesh + ordered joint paths (blend indices refer to this order).
        "skins": base_cand["skins"],
        # Renderers whose real mesh (behind a runtime placeholder) could not be identified.
        "unresolved_parts": base_cand.get("unresolved_parts", []),
    }
    for p in prefabs:
        bare = [part for part in p["parts"] if not part["materials"]]
        if bare:
            warnings.append(f"{len(bare)} of {len(p['parts'])} part(s) of {p['name']!r} have no material: "
                            f"{bare[0]['materials_missing']}")
    for p in prefabs:
        if p is not base and p["_cand"]["bone_paths"] != base_cand["bone_paths"]:
            warnings.append(f"prefab {p['name']!r} is skinned to different bones than {base['name']!r}")
    skel_hashes = frozenset(path_hash(p) for p in skel_paths)

    # --- animations ------------------------------------------------------------
    animations: dict[str, dict[str, Any]] = {}
    for p in prefabs:
        for clip in p["_cand"]["clips"]:
            animations.setdefault(clip["id"], _anim_entry(idx, clip, rules, skel_hashes, "controller", "verified"))
    # Clips linked only by binding match are added later (_link_clips_by_binding), across all species.
    anim_list = list(animations.values())

    model_only = any(p["_model_only"] for p in prefabs)
    if model_only:
        warnings.append("only the character model was found (its model bundle); no gameplay prefab with this ID "
                        "passed the name filter, so variants and runtime parts may be missing")
    for p in prefabs:
        p.pop("_cand")
        p.pop("_model_only")
    # Codename hints come from bundle file names and asset paths (object names mostly name parts/slots).
    texts = sorted({t for a in asset_list for t in (a.get("container"),) if t}
                   | {t for p in prefabs for t in (p.get("container"), p.get("asset_path")) if t})
    return {
        "key": key,
        "suggested_id": f"aniimo_{key}"[:48],
        "display_name": _display(base["name"], rules),
        "codename": _codename(rules, base["name"], texts),
        "game_id": game_id,
        "game_forms": sorted({p["game_form_index"] for p in prefabs if p["game_form_index"]}),
        "model_only": model_only,
        "grouping_evidence": group_evidence or ["single-prefab"],
        # Shiny prefabs grouped by the textures they actually use (evidence for distinct looks, or not).
        "shiny_look_evidence": evidence_groups,
        "prefabs": prefabs,
        "skeleton": skeleton,
        "assets": asset_list,
        "animations": anim_list,
        "warnings": warnings,
        "_skel_hashes": skel_hashes,
    }


def _anim_entry(idx: ObjectIndex, clip: dict[str, Any], rules: CatalogRules, skel_hashes: set[int],
                linked_via: str, provenance: str) -> dict[str, Any]:
    info = clip.get("info") or {}
    hashes = set(info.get("binding_path_hashes") or [])
    role = match_keyword(clip.get("name"), rules.clip_role_keywords)
    return dict(
        _ref_entry(idx, clip),
        linked_via=linked_via,
        provenance=provenance,
        binding_overlap=[len(hashes & skel_hashes), len(hashes)],
        role_guess=role[0] if role else None,
        role_provenance="inferred:name-keyword" if role else None,
        event_count=len(info.get("events") or []),
        length=info.get("length"),
    )
