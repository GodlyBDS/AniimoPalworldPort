"""Object inventory: list the objects inside the Unity containers of a manifest.

Input: a scan manifest + the scanned root. Output: an ``aniimo-pal/objects``
document (``objects.json``) with one record per relevant Unity object and its
outgoing references. The catalog and association engine work only from this
document, so they never need to touch game files again.

Designed for real installs (thousands of bundles, tens of GB):

* Only manifest entries that are ``supported`` (encryption-flagged bundles are
  excluded) and of kind ``unity_bundle``/``unity_serialized`` are opened.
* Each file is first checked against the manifest. ``quick`` (default) compares
  size and modification time; ``hash`` re-hashes the whole file; ``none`` skips
  the check. The extraction stage always re-verifies the sha256 of what it uses.
* Containers are processed in parallel worker processes.
* Each finished container is cached (keyed by its content hash) in
  ``cache_dir``. An interrupted run resumes where it stopped, and a game update
  only re-processes changed bundles.
* Only classes the catalog and data discovery use are recorded (every object is
  still counted per class); ``all_objects=True`` records everything.
* Strategy ``targeted`` (default): first a fast **survey** of every container
  (type table, dependencies, asset paths, TextAsset names; no object decoding),
  then a **deep** read only of containers holding skinned meshes (creatures and
  characters) plus, round by round, the containers their references point to
  (materials, textures, animations). Scenery, UI, audio and level bundles are
  surveyed but not decoded. Strategy ``full`` deep-reads every container.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import multiprocessing
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.adapters.unitypy_adapter import UnityPyAdapter, unitypy_version
from aniimo_pal.errors import ToolkitError
from aniimo_pal.log import get_logger

log = get_logger("inventory")

SCHEMA_ID = "aniimo-pal/objects"
SCHEMA_VERSION = "0.2.0"
INVENTORY_KINDS = ("unity_bundle", "unity_serialized")
CACHE_FORMAT = "inv-4"  # bump when per-container records change
VERIFY_MODES = ("quick", "hash", "none")


def _file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _verify(path: Path, entry: dict[str, Any], algo: str, mode: str) -> None:
    if mode == "none":
        return
    st = path.stat()
    if st.st_size != entry.get("size"):
        raise ToolkitError("file size changed since the manifest was written; re-scan first")
    if mode == "quick":
        recorded = entry.get("mtime_utc")
        if recorded:
            now = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            if now != recorded:
                raise ToolkitError("file modification time changed since the manifest was written; re-scan first")
        return
    if _file_hash(path, algo) != entry["hashes"].get(algo):
        raise ToolkitError("file changed since the manifest was written; re-scan first")


def _survey_container(path: str, _unused: bool = False) -> dict[str, Any]:
    """Worker: fast table of contents of one container (runs in a separate process)."""
    started = time.monotonic()
    res = UnityPyAdapter().survey_file(path)
    res["seconds"] = round(time.monotonic() - started, 3)
    return res


def _inventory_container(path: str, all_objects: bool) -> dict[str, Any]:
    """Worker: inventory one container (runs in a separate process)."""
    started = time.monotonic()
    counts: dict[str, int] = {}
    files, objects = [], []
    for inv in UnityPyAdapter().inventory_file(path, all_objects=all_objects, class_counts=counts):
        files.append({"cab": inv.cab, "unity_version": inv.unity_version, "externals": inv.externals,
                      "object_count": len(inv.objects)})
        objects.extend(inv.objects)
    return {"serialized_files": files, "objects": objects, "class_counts": counts,
            "seconds": round(time.monotonic() - started, 3)}


def _inventory_seed_container(path: str, all_objects: bool) -> dict[str, Any]:
    """Worker: like :func:`_inventory_container`, also decoding components (MonoBehaviours) with references.

    Used for containers holding skinned renderers (creature prefabs): a component there may be what assigns
    meshes or materials at runtime.
    """
    started = time.monotonic()
    counts: dict[str, int] = {}
    files, objects = [], []
    for inv in UnityPyAdapter().inventory_file(path, all_objects=all_objects, class_counts=counts,
                                               parse_behaviours=True):
        files.append({"cab": inv.cab, "unity_version": inv.unity_version, "externals": inv.externals,
                      "object_count": len(inv.objects)})
        objects.extend(inv.objects)
    return {"serialized_files": files, "objects": objects, "class_counts": counts,
            "seconds": round(time.monotonic() - started, 3)}


def _parse_behaviour_objects(path: str, all_objects: bool, object_ids: list[str]) -> dict[str, Any]:
    """Worker: decode the given MonoBehaviours of one container with their references.

    These are data assets that components on creature prefabs point at (e.g. a renderer's platform data
    holding its mesh and materials); other containers' objects are not touched.
    """
    from aniimo_pal.adapters.unitypy_adapter import cab_key

    started = time.monotonic()
    wanted = set(object_ids)
    adapter = UnityPyAdapter()
    env = adapter.load(path)
    node_cache: dict = {}
    objects = []
    for sf in adapter.serialized_files(env):
        cab = cab_key(sf.name)
        externals = [cab_key(e.path) for e in sf.externals]
        for path_id, obj in sf.objects.items():
            if f"{cab}:{path_id}" in wanted:
                try:
                    cls = obj.type.name
                except Exception:
                    continue
                if cls == "MonoBehaviour":
                    objects.append(adapter._object_record(obj, cab, externals, cls, node_cache, True))
    return {"objects": objects, "seconds": round(time.monotonic() - started, 3)}


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


STRATEGIES = ("targeted", "full")
DEEP_TRIGGER_CLASSES = ("SkinnedMeshRenderer",)


def build_inventory(manifest: dict[str, Any], root: str | Path, *, include: Iterable[str] = (),
                    verify: str | bool = "quick", workers: int = 1, cache_dir: str | Path | None = None,
                    all_objects: bool = False, strategy: str = "targeted",
                    progress: Callable[[int, int, str], None] | None = None,
                    verify_hashes: bool | None = None) -> dict[str, Any]:
    if verify_hashes is not None:  # backwards compatibility
        verify = "hash" if verify_hashes else "none"
    if isinstance(verify, bool):
        verify = "hash" if verify else "none"
    if verify not in VERIFY_MODES:
        raise ToolkitError(f"verify must be one of {VERIFY_MODES}")
    if strategy not in STRATEGIES:
        raise ToolkitError(f"strategy must be one of {STRATEGIES}")
    started = time.monotonic()
    root_path = Path(root).resolve()
    algo = manifest["settings"]["hash_algorithms"][0]
    patterns = tuple(include)
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    deep_tag = f"{CACHE_FORMAT}-{'all' if all_objects else 'relevant'}-{unitypy_version()}"
    survey_tag = f"survey-6-{unitypy_version()}"

    candidates, skipped = [], []
    for entry in manifest["files"]:
        if entry.get("kind") not in INVENTORY_KINDS:
            continue
        if patterns and not any(fnmatch.fnmatchcase(entry["path"], p) for p in patterns):
            continue
        if not entry.get("supported"):
            details = entry.get("details", {})
            reason = ("encryption flag set" if details.get("encryption_flag") else
                      "bundle signature behind a prefix" if details.get("signature_offset") else "not supported")
            skipped.append({"path": entry["path"], "reason": reason})
            continue
        candidates.append(entry)
    by_rel = {e["path"]: e for e in candidates}

    errors: dict[str, str] = {}
    for entry in candidates:
        try:
            _verify(root_path / entry["path"], entry, algo, verify)
        except (ToolkitError, OSError) as exc:
            errors[entry["path"]] = f"{type(exc).__name__}: {exc}"[:500]
    ok_entries = [e for e in candidates if e["path"] not in errors]

    def run_phase(entries: list[dict[str, Any]], fn, tag: str, label: str,
                  extra: dict[str, list[str]] | None = None) -> dict[str, dict[str, Any]]:
        """Run ``fn`` per container (cached, parallel); returns {rel: result}.

        ``extra``: per-container argument passed to ``fn`` (and part of the cache key).
        """
        out: dict[str, dict[str, Any]] = {}
        todo, done = [], 0
        for entry in entries:
            digest = entry["hashes"].get(algo)
            if extra is not None:
                arg_key = hashlib.sha1("\n".join(sorted(extra[entry["path"]])).encode()).hexdigest()[:12]
                digest = f"{digest}-{arg_key}" if digest else None
            cf = cache / f"{tag}-{digest}.json" if cache and digest else None
            if cf and cf.is_file():
                try:
                    out[entry["path"]] = dict(json.loads(cf.read_text(encoding="utf-8")), cached=True)
                    done += 1
                    continue
                except (OSError, json.JSONDecodeError):
                    pass
            todo.append((entry, cf))
        if progress and done:
            progress(done, len(entries), f"{label}: {done} from cache")

        def finish(entry, cf, res):
            nonlocal done
            out[entry["path"]] = res
            if cf and "error" not in res:
                tmp = cf.with_suffix(".tmp")
                tmp.write_text(json.dumps(res, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, cf)
            done += 1
            if progress:
                progress(done, len(entries), f"{label}: {entry['path']}")

        if workers <= 1 or len(todo) <= 1:
            for entry, cf in todo:
                try:
                    args = (extra[entry["path"]],) if extra is not None else ()
                    res = fn(str(root_path / entry["path"]), all_objects, *args)
                except Exception as exc:  # one bad container must not stop the inventory
                    log.warning("%s failed for %s: %s", label, entry["path"], exc)
                    res = {"error": f"{type(exc).__name__}: {exc}"[:500]}
                finish(entry, cf, res)
        else:
            # "spawn" everywhere: what Windows uses, and safe with threads in the parent (e.g. the Studio).
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                futures = {pool.submit(fn, str(root_path / e["path"]), all_objects,
                                       *((extra[e["path"]],) if extra is not None else ())): (e, cf)
                           for e, cf in todo}
                for fut in as_completed(futures):
                    entry, cf = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        log.warning("%s failed for %s: %s", label, entry["path"], exc)
                        res = {"error": f"{type(exc).__name__}: {exc}"[:500]}
                    finish(entry, cf, res)
        return out

    surveys: dict[str, dict[str, Any]] = {}
    deep: dict[str, dict[str, Any]] = {}
    behaviour_assets: dict[str, dict[str, Any]] = {}  # id -> decoded MonoBehaviour data asset
    rounds = 0
    # The survey also yields the asset path index (AssetBundle m_Container): needed in both strategies.
    surveys = run_phase(ok_entries, _survey_container, survey_tag, "survey")
    asset_index = _asset_index(surveys)
    if strategy == "full":
        deep = run_phase(ok_entries, _inventory_container, deep_tag, "deep read")
    else:
        cab_owner: dict[str, str] = {}
        for rel, res in surveys.items():
            for sf in res.get("serialized_files", []):
                cab_owner.setdefault(sf["cab"], rel)
        wanted = {rel for rel, res in surveys.items()
                  if any((res.get("class_counts") or {}).get(c, 0) for c in DEEP_TRIGGER_CLASSES)}
        known_cabs: set[str] = set()
        parsed_behaviours: set[str] = set()
        behaviour_hops = 0
        new_behaviours: list[dict[str, Any]] = []
        while (wanted or new_behaviours) and rounds < 12:
            rounds += 1
            new: dict[str, dict[str, Any]] = {}
            batch = [by_rel[r] for r in sorted(wanted) if r not in deep]
            if batch:
                if rounds == 1:  # creature prefab containers: components decoded too
                    new = run_phase(batch, _inventory_seed_container, deep_tag + "-components",
                                    f"deep read (round {rounds})")
                else:
                    new = run_phase(batch, _inventory_container, deep_tag, f"deep read (round {rounds})")
                deep.update(new)
                for res in new.values():
                    for sf in res.get("serialized_files", []):
                        known_cabs.add(sf["cab"])
                    for obj in res.get("objects", []):
                        if obj["class"] == "MonoBehaviour" and obj["refs"]:
                            parsed_behaviours.add(obj["id"])
                            if rounds == 1:
                                new_behaviours.append(obj)
            # Data assets that components on creature prefabs point at (two hops at most), decoded with refs.
            found: list[dict[str, Any]] = []
            if new_behaviours and behaviour_hops < 2:
                behaviour_hops += 1
                targets: dict[str, set[str]] = defaultdict(set)
                for obj in new_behaviours:
                    for ref in obj["refs"]:
                        cab = ref["target"].rsplit(":", 1)[0]
                        if ref["field"] not in ("m_GameObject", "m_Script") and ref["target"] not in parsed_behaviours \
                                and cab in cab_owner:
                            targets[cab_owner[cab]].add(ref["target"])
                extra = {rel: sorted(ids) for rel, ids in targets.items()}
                got = run_phase([by_rel[r] for r in sorted(extra) if r in by_rel], _parse_behaviour_objects,
                                deep_tag + "-assets", f"component data (hop {behaviour_hops})", extra=extra)
                for rel, res in got.items():
                    for obj in res.get("objects", []):
                        obj["container"] = rel
                        parsed_behaviours.add(obj["id"])
                        behaviour_assets[obj["id"]] = obj
                        found.append(obj)
            new_behaviours = found
            # Follow references into containers not read yet (shared materials, textures, animations).
            wanted = set()
            for obj in found:  # assets named by path strings (e.g. PlatformData mesh and material paths)
                for _field, value in (obj.get("info") or {}).get("asset_path_strings") or []:
                    for copy in asset_index.get(value.replace("\\", "/").lower(), []):
                        if copy["container"] not in deep:
                            wanted.add(copy["container"])
            for obj in [o for res in new.values() for o in res.get("objects", [])] + found:
                for ref in obj["refs"]:
                    cab = ref["target"].rsplit(":", 1)[0]
                    if cab not in known_cabs and cab in cab_owner and cab_owner[cab] not in deep:
                        wanted.add(cab_owner[cab])

    containers, objects = [], []
    class_counts: dict[str, int] = defaultdict(int)
    for entry in candidates:
        rel = entry["path"]
        res = deep.get(rel) or surveys.get(rel) or {}
        survey = surveys.get(rel) or {}
        error = errors.get(rel) or res.get("error")
        mode = "deep" if rel in deep else ("survey" if rel in surveys else "none")
        record = {"path": rel, "sha256": entry["hashes"].get("sha256"), "hash": {algo: entry["hashes"].get(algo)},
                  "kind": entry["kind"], "status": "error" if error else "ok", "error": error, "read": mode,
                  "serialized_files": res.get("serialized_files", []), "cached": bool(res.get("cached")),
                  "seconds": res.get("seconds"), "class_counts": (survey or res).get("class_counts", {}),
                  "asset_paths": len(survey.get("asset_paths", []))}
        containers.append(record)
        if rel in deep:
            for obj in deep[rel].get("objects", []):
                obj["container"] = rel
                objects.append(obj)
        else:
            # Survey-only containers still expose TextAssets (data-table discovery) and mesh names
            # (real meshes behind placeholder renderers).
            for cab, path_id, name, *rest in survey.get("text_assets", []):
                objects.append({"id": f"{cab}:{path_id}", "cab": cab, "path_id": path_id, "class": "TextAsset",
                                "class_id": 49, "name": name, "asset_path": None, "refs": [],
                                "info": rest[0] if rest else {},
                                "error": None, "container": rel})
            for cls, class_id, key in (("Mesh", 43, "meshes"), ("MonoBehaviour", 114, "scriptables")):
                for cab, path_id, name, *rest in survey.get(key, []):
                    objects.append({"id": f"{cab}:{path_id}", "cab": cab, "path_id": path_id, "class": cls,
                                    "class_id": class_id, "name": name, "asset_path": None, "refs": [],
                                    "info": rest[0] if rest else {"surveyed": True}, "error": None,
                                    "container": rel})
        for cls, n in record["class_counts"].items():
            class_counts[cls] += n
    if behaviour_assets:
        # A decoded data asset replaces the record of the same object in the container it was decoded from.
        # Other copies of that serialized file (e.g. a CacheBundleFiles download) keep their own records; the
        # object index picks the most completely read copy and reports copies that disagree.
        where = {(o["id"], o.get("container")): i for i, o in enumerate(objects) if o["id"] in behaviour_assets}
        for oid, obj in behaviour_assets.items():
            i = where.get((oid, obj.get("container")))
            if i is None:
                objects.append(obj)
            else:
                objects[i] = obj

    cab_owner_all: dict[str, str] = {}
    duplicate_cabs = set()
    for c in containers:
        for sf in c["serialized_files"]:
            if sf["cab"] in cab_owner_all and cab_owner_all[sf["cab"]] != c["path"]:
                duplicate_cabs.add(sf["cab"])
            cab_owner_all.setdefault(sf["cab"], c["path"])

    known_ids = {o["id"] for o in objects}
    unresolved = sum(1 for o in objects for r in o["refs"] if r["target"] not in known_ids)
    objects.sort(key=lambda o: (o["cab"], o["path_id"]))
    by_class: dict[str, int] = defaultdict(int)
    for o in objects:
        by_class[o["class"]] += 1

    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": {"name": "UnityPy", "version": unitypy_version()},
        "manifest": {"root_label": manifest["source"]["root_label"], "created_utc": manifest.get("created_utc")},
        "settings": {"verify": verify, "workers": workers, "all_objects": all_objects, "strategy": strategy},
        "summary": {
            "container_count": len(containers),
            "container_errors": sum(1 for c in containers if c["status"] != "ok"),
            "containers_deep_read": sum(1 for c in containers if c["read"] == "deep"),
            "containers_with_skinned_meshes": sum(1 for c in containers
                                                  if c["class_counts"].get("SkinnedMeshRenderer", 0)),
            "containers_from_cache": sum(1 for c in containers if c["cached"]),
            "dependency_rounds": rounds,
            "skipped_count": len(skipped),
            "object_count": len(objects),
            "object_errors": sum(1 for o in objects if o.get("error")),
            "unresolved_refs": unresolved,
            "duplicate_cabs": sorted(duplicate_cabs),
            "by_class": dict(sorted(by_class.items())),
            "all_classes_seen": dict(sorted(class_counts.items())),
            "seconds": round(time.monotonic() - started, 1),
        },
        "containers": containers,
        "skipped": skipped,
        # Asset path (lower case, as in AssetBundle m_Container) -> copies [{object, container, meshes}].
        "asset_index": asset_index,
        "objects": objects,
    }


def _asset_index(surveys: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rel, res in sorted(surveys.items()):
        for path, main, meshes in res.get("container_entries") or []:
            index[path.replace("\\", "/").lower()].append({"object": main, "container": rel, "meshes": meshes})
    return dict(index)


def summary_path(objects_path: str | Path) -> Path:
    """Small sidecar next to objects.json, so tools can show the summary without loading every object."""
    p = Path(objects_path)
    return p.with_name(p.stem + ".summary.json")


def summary_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: doc.get(k) for k in ("schema_version", "generator", "created_utc", "manifest", "settings", "summary")} | \
        {"schema": SCHEMA_ID + "-summary"}


def _completeness(rec: dict[str, Any]) -> tuple[int, int, int, int]:
    info = rec.get("info") or {}
    return (0 if info.get("surveyed") else 1, 0 if rec.get("error") else 1, len(info), len(rec.get("refs") or []))


def _differences(a: dict[str, Any], b: dict[str, Any], limit: int = 12) -> list[str]:
    """Dotted paths (two levels below info) where two records of one object differ."""
    out = [k for k in ("class", "name", "refs") if a.get(k) != b.get(k)]
    ia, ib = a.get("info") or {}, b.get("info") or {}
    for k in sorted(set(ia) | set(ib)):
        va, vb = ia.get(k), ib.get(k)
        if va == vb:
            continue
        if isinstance(va, dict) and isinstance(vb, dict):
            out += [f"info.{k}.{s}" for s in sorted(set(va) | set(vb)) if va.get(s) != vb.get(s)]
        else:
            out.append(f"info.{k}")
    return out[:limit]


def _content_key(rec: dict[str, Any]) -> str:
    return json.dumps([rec.get("class"), rec.get("name"), rec.get("refs"), rec.get("info")], sort_keys=True,
                      default=str)


class ObjectIndex:
    """Lookup helpers over an ``aniimo-pal/objects`` document."""

    def __init__(self, doc: dict[str, Any]):
        if doc.get("schema") != SCHEMA_ID:
            raise ToolkitError(f"not an {SCHEMA_ID} document")
        self.doc = doc
        # The same serialized file (CAB) can sit in several containers, e.g. a StreamingAssets bundle and its
        # downloaded copy under CacheBundleFiles: one object id, several records. One canonical record per id:
        # the most completely read one (decoded beats surveyed, then more facts and references); among equally
        # complete copies the first by container path. Copies that were both decoded but disagree are reported
        # in ``conflicts`` instead of guessing which is current.
        copies: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for o in doc["objects"]:
            copies[o["id"]].append(o)
        self.by_id: dict[str, dict[str, Any]] = {}
        self.copies: dict[str, list[dict[str, Any]]] = {}
        self.conflicts: list[dict[str, Any]] = []
        for oid, recs in copies.items():
            if len(recs) == 1:
                self.by_id[oid] = recs[0]
                continue
            recs = sorted(recs, key=lambda r: (tuple(-x for x in _completeness(r)), r.get("container") or ""))
            self.by_id[oid] = recs[0]
            self.copies[oid] = recs
            full = [r for r in recs if _completeness(r) == _completeness(recs[0])]
            if len({_content_key(r) for r in full}) > 1:
                self.conflicts.append({"object": oid, "class": recs[0].get("class"), "name": recs[0].get("name"),
                                       "containers": [r.get("container") for r in full],
                                       "chosen": recs[0].get("container"),
                                       "differs_in": _differences(full[0], full[1])})
        self.by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.referrers: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for o in self.by_id.values():
            self.by_class[o["class"]].append(o)
            for r in o["refs"]:
                self.referrers[r["target"]].append((o["id"], r["field"]))
        self.container_hash = {c["path"]: c.get("sha256") for c in doc["containers"]}

    def get(self, object_id: str | None) -> dict[str, Any] | None:
        return self.by_id.get(object_id) if object_id else None

    def refs(self, obj: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
        """Resolved targets of references whose field path starts with ``prefix``."""
        out = []
        for r in obj["refs"]:
            if r["field"] == prefix or r["field"].startswith(prefix + "[") or r["field"].startswith(prefix + "."):
                target = self.by_id.get(r["target"])
                if target is not None:
                    out.append(target)
        return out

    def ref(self, obj: dict[str, Any], field: str) -> dict[str, Any] | None:
        found = self.refs(obj, field)
        return found[0] if found else None
