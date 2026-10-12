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
from aniimo_pal.catalog.rules import CatalogRules, match_keyword, tokens
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
    renderers, animators, legacy_anims = [], [], []
    path_of_transform: dict[str, str] = {}
    local_trs: dict[str, dict[str, Any]] = {}
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
        if r["class"] == "SkinnedMeshRenderer" and mesh is not None:
            # Joint order matters: mesh blend indices refer to positions in m_Bones.
            ordered: dict[int, str | None] = {}
            for ref in r["refs"]:
                m = _BONE_FIELD.match(ref["field"])
                if m:
                    ordered[int(m.group(1))] = path_of_transform.get(ref["target"])
            root_bone = idx.ref(r, "m_RootBone")
            skins.append({"renderer": r["id"], "mesh": mesh["id"], "mesh_name": mesh.get("name"),
                          "renderer_path": path_of_transform.get((h.go_transform.get(go["id"]) or {}).get("id")),
                          "bones": [ordered.get(i) for i in range(max(ordered) + 1)] if ordered else [],
                          "root_bone": path_of_transform.get(root_bone["id"]) if root_bone else None})
    if not meshes:
        return None

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


def _material_textures(idx: ObjectIndex, mat: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """[(slot, texture)] using the material's slot order (null textures are already excluded)."""
    slots = (mat.get("info") or {}).get("texture_slots") or []
    textures = [t for t in idx.refs(mat, "m_SavedProperties.m_TexEnvs")]
    return [(slots[i] if i < len(slots) else f"slot{i}", t) for i, t in enumerate(textures)]


def _stem(name: str | None, rules: CatalogRules) -> str:
    variant_words = {w for words in rules.variant_keywords.values() for w in words}
    toks = [t for t in tokens(name) if t not in variant_words and t not in rules.stop_tokens and not t.isdigit()]
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


def build_catalog(objects_doc: dict[str, Any], rules: CatalogRules | None = None) -> dict[str, Any]:
    rules = rules or CatalogRules()
    idx = ObjectIndex(objects_doc)
    h = _Hierarchy(idx)

    candidates = []
    roots = h.roots()
    for root in sorted(roots, key=lambda g: g["id"]):
        cand = _prefab_candidate(idx, h, root, rules)
        if cand is not None:
            candidates.append(cand)

    # Group prefabs into species: shared mesh (verified), else same skeleton + name stem (inferred).
    uf = _UnionFind(len(candidates))
    group_evidence: dict[int, set[str]] = defaultdict(set)
    mesh_owner: dict[str, int] = {}
    for i, c in enumerate(candidates):
        for m in c["meshes"]:
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
                                     sorted({e for i in members for e in group_evidence[i]}),
                                     orphan_clips, used_keys))

    claimed = {a["object"] for s in species_list for a in s["animations"]}
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"objects_created_utc": objects_doc.get("created_utc"),
                   "root_label": (objects_doc.get("manifest") or {}).get("root_label")},
        "rules": rules.to_dict(),
        "summary": {
            "species_count": len(species_list),
            "prefab_count": len(candidates),
            "unassigned_clip_count": sum(1 for c in idx.by_class.get("AnimationClip", []) if c["id"] not in claimed),
            "diagnostics": _diagnostics(idx, h, roots, rules),
        },
        "species": species_list,
    }


def _diagnostics(idx: ObjectIndex, h: _Hierarchy, roots: list[dict[str, Any]], rules: CatalogRules) -> dict[str, Any]:
    """Counts that explain an empty or small catalog (which link from renderer to prefab is missing)."""
    renderer_classes = RENDERER_CLASSES + (STATIC_RENDERER_CLASSES if rules.include_static_meshes else ())
    renderers = [r for cls in renderer_classes for r in idx.by_class.get(cls, [])]
    with_mesh = with_go = in_hierarchy = 0
    missing_mesh_targets: dict[str, int] = defaultdict(int)
    for r in renderers:
        mesh_ref = next((ref for ref in r["refs"] if ref["field"] == "m_Mesh"), None)
        if idx.ref(r, "m_Mesh") is not None:
            with_mesh += 1
        elif mesh_ref is not None:
            missing_mesh_targets[mesh_ref["target"].rsplit(":", 1)[0]] += 1
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
        "renderers_with_gameobject": with_go,
        "renderers_in_hierarchy": in_hierarchy,
        # Where unresolved meshes live: the serialized file and the container that holds it (if any was read).
        "missing_mesh_files": [{"cab": cab, "count": n, "container": owners.get(cab)}
                               for cab, n in sorted(missing_mesh_targets.items(), key=lambda kv: -kv[1])[:10]],
    }


def _species(idx: ObjectIndex, rules: CatalogRules, members: list[dict[str, Any]], group_evidence: list[str],
             orphan_clips: list[dict[str, Any]], used_keys: set[str]) -> dict[str, Any]:
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
        prefabs.append({"object": root["id"], "name": root.get("name"), "asset_path": root.get("asset_path"),
                        "container": root.get("container"), "region": BASE_REGION,
                        "form": "prismana" if hits.get("prismana") else "normal",
                        "appearance": "shiny" if hits.get("shiny") else STANDARD,
                        "variant_provenance": "inferred" if hits else "default", "variant_evidence": evidence,
                        "_cand": c})

    for form in ("normal", "prismana"):
        standard = sorted((p for p in prefabs if p["form"] == form and p["appearance"] == STANDARD),
                          key=lambda p: (len(p["name"] or ""), p["object"]))
        for p in standard[1:]:
            p["form"] = "unclassified"
            p["variant_evidence"].append("additional-prefab-without-distinguishing-keyword")
        if len(standard) > 1:
            warnings.append(f"{len(standard)} prefabs look like the {form} standard appearance; "
                            "extra ones marked 'unclassified'")
        shinies = sorted((p for p in prefabs if p["form"] == form and p["appearance"] == "shiny"),
                         key=lambda p: (p["name"] or "", p["object"]))
        for i, p in enumerate(shinies, 1):
            p["appearance"] = f"shiny_{i:02d}"
            p["variant_evidence"].append("appearance-numbered-by-name-order")
        if shinies:
            warnings.append(f"{len(shinies)} {form} Shiny prefab(s) numbered by name order (inferred); "
                            "verify appearance identities against the source data")
    for p in prefabs:
        p["variant"] = "unclassified" if p["form"] == "unclassified" else variant_key(p["form"], p["appearance"])

    normals = [p for p in prefabs if p["form"] == "normal" and p["appearance"] == STANDARD]
    if not normals:
        warnings.append("no prefab without a variant keyword; normal variant must be chosen manually")

    base = normals[0] if normals else prefabs[0]
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
        "bones": [{"path": p, "name": p.rsplit("/", 1)[-1], "parent": p.rsplit("/", 1)[0] if "/" in p else None,
                   "skinned": p in bone_paths, "local": local_trs.get(p)} for p in skel_paths],
        "avatar": base_cand["avatars"][0]["id"] if base_cand["avatars"] else None,
        # Per skinned renderer: mesh + ordered joint paths (blend indices refer to this order).
        "skins": base_cand["skins"],
    }
    for p in prefabs:
        if p is not base and p["_cand"]["bone_paths"] != base_cand["bone_paths"]:
            warnings.append(f"prefab {p['name']!r} is skinned to different bones than {base['name']!r}")
    skel_hashes = {path_hash(p) for p in skel_paths}

    # --- animations ------------------------------------------------------------
    animations: dict[str, dict[str, Any]] = {}
    for p in prefabs:
        for clip in p["_cand"]["clips"]:
            animations.setdefault(clip["id"], _anim_entry(idx, clip, rules, skel_hashes, "controller", "verified"))
    for clip in orphan_clips:
        hashes = set((clip.get("info") or {}).get("binding_path_hashes") or [])
        matched = len(hashes & skel_hashes)
        if hashes and matched >= rules.min_binding_matches and matched / len(hashes) >= rules.min_binding_ratio:
            animations.setdefault(clip["id"], _anim_entry(idx, clip, rules, skel_hashes, "binding-match", "inferred"))
    anim_list = sorted(animations.values(), key=lambda a: (a["name"] or "", a["object"]))
    for a in anim_list:
        if a["binding_overlap"][1] and a["binding_overlap"][0] < a["binding_overlap"][1]:
            warnings.append(f"clip {a['name']!r} animates {a['binding_overlap'][1] - a['binding_overlap'][0]} "
                            "paths not present in the skeleton")

    for p in prefabs:
        p.pop("_cand")
    return {
        "key": key,
        "suggested_id": f"aniimo_{key}"[:48],
        "display_name": _display(base["name"], rules),
        "grouping_evidence": group_evidence or ["single-prefab"],
        "prefabs": prefabs,
        "skeleton": skeleton,
        "assets": asset_list,
        "animations": anim_list,
        "warnings": warnings,
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
