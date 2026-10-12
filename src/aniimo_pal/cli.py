"""Command-line interface: ``aniimo-pal <command>``.

Exit codes: 0 success, 1 check failed (validation errors, verify mismatch,
diff with ``--fail-on-change``), 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from aniimo_pal import TOOL_NAME, __version__
from aniimo_pal.config import Config, load_config
from aniimo_pal.errors import ToolkitError
from aniimo_pal.log import setup_logging

EXIT_OK, EXIT_CHECK_FAILED, EXIT_USAGE = 0, 1, 2


def _print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def _print_summary(manifest: dict[str, Any]) -> None:
    s = manifest["summary"]
    print(f"root:       {manifest['source']['root_label']}")
    print(f"files:      {s['file_count']} ({_fmt_bytes(s['total_bytes'])}), skipped {s['skipped_count']}")
    print(f"supported:  {s['supported_count']}  encrypted-flagged: {s['encrypted_count']}  "
          f"warnings: {s['warning_count']}  errors: {s['error_count']}")
    if s.get("unity_versions"):
        print("unity:      " + ", ".join(f"{v} x{n}" for v, n in s["unity_versions"].items()))
    if s["by_kind"]:
        print("by kind:")
        width = max(len(k) for k in s["by_kind"])
        for kind, count in sorted(s["by_kind"].items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {kind:<{width}}  {count}")


# --- commands ---------------------------------------------------------------

def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.manifest.io import check_output_location, write_manifest
    from aniimo_pal.scanner import ScanOptions, scan

    scan_cfg = config.scan
    overrides: dict[str, Any] = {}
    if args.hash:
        overrides["hash_algorithms"] = tuple(a.strip().lower() for part in args.hash
                                             for a in part.split(",") if a.strip())
    if args.workers is not None:
        overrides["workers"] = args.workers
    if args.follow_symlinks:
        overrides["follow_symlinks"] = True
    if args.include_hidden:
        overrides["include_hidden"] = True
    if args.exclude:
        overrides["exclude"] = scan_cfg.exclude + tuple(args.exclude)
    if args.no_mtime:
        overrides["record_mtime"] = False
    scan_cfg = replace(scan_cfg, **overrides)
    scan_cfg.validate()

    root = Path(args.root)
    if not root.is_dir():
        raise ToolkitError(f"scan root is not a directory: {root}")
    out = Path(args.output)
    check_output_location(out, [root])  # fail before doing any work
    manifest = scan(root, scan_cfg, ScanOptions(
        root_label=args.label,
        store_absolute_root=args.store_absolute_root or config.manifest.store_absolute_root,
    ))
    write_manifest(manifest, out, indent=config.manifest.indent, read_only_roots=[root])
    if not args.quiet:
        _print_summary(manifest)
        print(f"manifest:   {out}")
    return EXIT_OK


def cmd_summary(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.manifest.io import read_manifest

    manifest = read_manifest(args.manifest)
    if args.json:
        _print_json(manifest["summary"])
    else:
        _print_summary(manifest)
        if args.warnings:
            for f in manifest["files"]:
                for w in f.get("warnings", []):
                    print(f"WARN  {f['path']}: {w}")
                if f.get("error"):
                    print(f"ERROR {f['path']}: {f['error']}")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.manifest.io import read_manifest, validate_manifest

    errors, used_schema = validate_manifest(read_manifest(args.manifest))
    mode = "JSON Schema + semantic checks" if used_schema else "basic + semantic checks (jsonschema not installed)"
    if errors:
        for e in errors:
            print(f"invalid: {e}")
        print(f"{len(errors)} error(s) [{mode}]")
        return EXIT_CHECK_FAILED
    print(f"valid [{mode}]")
    return EXIT_OK


def cmd_diff(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.manifest.diff import diff_manifests
    from aniimo_pal.manifest.io import read_manifest

    result = diff_manifests(read_manifest(args.old), read_manifest(args.new))
    if args.json:
        _print_json(result)
    else:
        for p in result["added"]:
            print(f"+ {p}")
        for p in result["removed"]:
            print(f"- {p}")
        for m in result["modified"]:
            print(f"M {m['path']} ({m['old_size']} -> {m['new_size']} bytes)")
        for m in result["moved"]:
            print(f"R {m['from']} -> {m['to']}")
        for k in result["kind_changed"]:
            print(f"K {k['path']} ({k['old_kind']} -> {k['new_kind']})")
        print(f"added {len(result['added'])}, removed {len(result['removed'])}, "
              f"modified {len(result['modified'])}, moved {len(result['moved'])}, "
              f"unchanged {result['unchanged_count']}")
    if args.fail_on_change and not result["identical"]:
        return EXIT_CHECK_FAILED
    return EXIT_OK


def cmd_verify(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.manifest.io import read_manifest
    from aniimo_pal.manifest.verify import verify_manifest

    result = verify_manifest(read_manifest(args.manifest), args.root, check_extra=not args.no_extra)
    if args.json:
        _print_json(result)
    else:
        for key in ("missing", "changed", "extra"):
            for p in result[key]:
                print(f"{key.upper():8} {p}")
        for u in result["unreadable"]:
            print(f"UNREADABLE {u['path']}: {u['error']}")
        print(f"ok {len(result['ok'])}, missing {len(result['missing'])}, changed {len(result['changed'])}, "
              f"unreadable {len(result['unreadable'])}, extra {len(result['extra'])} "
              f"[{result['hash_algorithm']}]")
    return EXIT_OK if result["passed"] else EXIT_CHECK_FAILED


def cmd_formats(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.scanner.formats import FORMATS

    rows = [{"kind": f.kind, "category": f.category, "route": f.route, "description": f.description}
            for f in FORMATS.values()]
    if args.json:
        _print_json(rows)
    else:
        for r in rows:
            print(f"{r['kind']:<22} {r['category']:<16} {r['route'] or '-':<30} {r['description']}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.adapters import all_adapters

    statuses = [a.status().to_dict() for a in all_adapters(config.tools)]
    report = {
        "toolkit": {"name": TOOL_NAME, "version": __version__},
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "tools": statuses,
    }
    if args.json:
        _print_json(report)
    else:
        print(f"{TOOL_NAME} {__version__} on Python {report['python']} ({report['platform']})")
        for s in statuses:
            mark = "found  " if s["available"] else "missing"
            extra = f" {s['version']}" if s["version"] else ""
            where = f" at {s['location']}" if s["location"] else ""
            print(f"  [{mark}] {s['name']}{extra}{where}  ({s['license']})")
            if s["notes"]:
                print(f"            {s['notes']}")
        print("Missing tools only matter for later pipeline stages; scanning needs none of them.")
    return EXIT_OK


def cmd_systems(args: argparse.Namespace, config: Config) -> int:
    from dataclasses import asdict

    from aniimo_pal.systems import bundled_system, load_system

    sysdef = load_system(args.system) if args.system.endswith(".toml") else bundled_system(args.system)
    if args.json:
        _print_json(asdict(sysdef))
        return EXIT_OK
    print(f"{sysdef.id} {sysdef.version} (game {sysdef.game_version}) status={sysdef.status}")
    print("elements: " + ", ".join(f"{k} ({v['name']})" for k, v in sysdef.elements.items()))
    print("stats:    " + ", ".join(f"{k} ({v['name']})" for k, v in sysdef.stats.items()))
    print("work:     " + (", ".join(sysdef.work_categories) or "(not defined yet)")
          + f"  levels {sysdef.work_level_min}-{sysdef.work_level_max}")
    for src in sysdef.sources:
        print(f"source:   {src}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    _print_json(config.to_dict())
    return EXIT_OK


def cmd_creature_init(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import creature_path, new_creature, save_creature

    if args.from_catalog:
        from aniimo_pal.creature.workflow import creature_from_catalog, pending

        if not args.species:
            raise ToolkitError("--species is required with --from-catalog")
        rarity = {"wild": {"shiny_probability": args.wild_shiny, "prismana_probability": args.wild_prismana},
                  "egg": {"shiny_probability": args.egg_shiny, "prismana_probability": args.egg_prismana}}
        data = creature_from_catalog(
            _read_json(args.from_catalog), args.species, creature_id=args.id, display_name=args.name,
            rarity=rarity, auto_confirm_verified=not args.propose_all)
    else:
        if not args.id or not args.name:
            raise ToolkitError("--id and --name are required (or use --from-catalog/--species)")
        data = new_creature(args.id, args.name)
    path = creature_path(args.workspace, data["id"])
    if path.exists():
        raise ToolkitError(f"creature project already exists: {path} (projects are never overwritten)")
    save_creature(data, path)
    print(f"created {path}")
    if args.from_catalog:
        print(f"assets: {len(data['assets'])}, awaiting review: {len(pending(data))}, status: {data['status']}")
    return EXIT_OK


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ToolkitError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ToolkitError(f"{path} is not valid JSON: {exc}") from exc


def _parse_indices(text: str | None) -> list[int]:
    if not text:
        return []
    out = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def cmd_creature_review(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import load_creature
    from aniimo_pal.creature.workflow import pending

    c = load_creature(args.file)
    todo = set(pending(c))
    print(f"{c['id']} ({c['display_name']}) status={c['status']}  pending={len(todo)}")
    for p in c.get("prefabs", []):
        print(f"  prefab {p['name']!s:<32} {p.get('variant', '?'):<22} {', '.join(p.get('variant_evidence') or [])}")
    print(f"  {'#':>3} {'role':<10} {'form':<12} {'appearance':<10} {'assoc':<10} {'prov':<9} {'name':<32} evidence")
    for i, a in enumerate(c["assets"]):
        if args.pending and i not in todo:
            continue
        mark = "*" if i in todo else " "
        extra = f" role_guess={a['role_guess']}" if a.get("role_guess") else ""
        print(f" {mark}{i:>3} {a['role']:<10} {a.get('form', 'shared'):<12} {a.get('appearance', 'any'):<10} "
              f"{a['association']:<10} {a.get('provenance', 'user'):<9} {str(a.get('name'))[:32]:<32} "
              f"{', '.join(a.get('evidence') or [])}{extra}")
    for note in c.get("notes", []):
        print(f"  note: {note}")
    return EXIT_OK


def cmd_creature_associate(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import load_creature, save_creature
    from aniimo_pal.creature.workflow import pending, set_associations

    c = load_creature(args.file)
    def pairs(items):
        out = {}
        for item in items or []:
            idx, _, name = item.partition("=")
            out[int(idx)] = name
        return out

    set_associations(c, confirm=_parse_indices(args.confirm), reject=_parse_indices(args.reject),
                     forms=pairs(args.form), appearances=pairs(args.appearance), confirm_all=args.confirm_all)
    save_creature(c, args.file)
    print(f"saved {args.file}: status={c['status']}, pending={len(pending(c))}")
    return EXIT_OK


def cmd_creature_extract(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import load_creature, save_creature
    from aniimo_pal.extract import extract_creature

    path = Path(args.file)
    c = load_creature(path)
    report = extract_creature(c, path.parent, args.root, include_proposed=args.include_proposed)
    save_creature(c, path)
    s = report["summary"]
    print(f"extracted ok={s['ok']} skipped={s['skipped']} errors={s['errors']} -> {path.parent / 'extract' / 'unitypy'}")
    for r in report["outputs"]:
        if r["status"] == "error":
            print(f"ERROR asset {r['index']}: {r['error']}")
    return EXIT_OK if s["errors"] == 0 else EXIT_CHECK_FAILED


def cmd_creature_assetripper(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.adapters.assetripper import AssetRipperAdapter
    from aniimo_pal.creature.model import load_creature, save_creature
    from aniimo_pal.extract import assetripper_export_creature

    exe = AssetRipperAdapter(args.assetripper or config.tools.assetripper).locate()
    if exe is None:
        raise ToolkitError("AssetRipper not found: pass --assetripper PATH or set [tools].assetripper in the config")
    path = Path(args.file)
    c = load_creature(path)
    if c.get("status") == "draft" and not args.include_proposed:
        raise ToolkitError("creature is still 'draft': review associations first, or pass --include-proposed")
    report = assetripper_export_creature(c, path.parent, args.root, exe, mode=args.mode,
                                         include_proposed=args.include_proposed, replace=args.replace)
    save_creature(c, path)
    s = report["summary"]
    print(f"AssetRipper {args.mode}: files {s['files_written']}, assets matched {s['assets_matched']}, "
          f"unmatched {s['assets_unmatched']}, prefabs matched {s['prefabs_matched']}")
    unmatched = [m for m in report["assets"] if not m["files"]]
    for m in unmatched[:40]:
        print(f"  unmatched asset {m['index']}: {m['role']} {m['name']}")
    if len(unmatched) > 40:
        print(f"  ... and {len(unmatched) - 40} more unmatched assets")
    sample = report.get("unassigned_files_sample", [])
    if unmatched and sample:
        print("  exported files not matched to any asset (those sharing a name part with this creature first):")
        for f in sample[:40]:
            print(f"    {f}")
        print("  exported file types: " + ", ".join(f"{e} {n}" for e, n in report.get("extension_counts", {}).items()))
    if s["files_written"] == 0:
        print("  AssetRipper wrote no files. Its log: " + " | ".join(report.get("log_tail") or []))
    return EXIT_OK


def cmd_creature_convert(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.convert.pipeline import convert_creature
    from aniimo_pal.creature.model import load_creature, save_creature

    path = Path(args.file)
    c = load_creature(path)
    bone_map = _read_json(args.bone_map) if args.bone_map else None
    blender = args.blender or config.tools.blender or None
    report = convert_creature(c, path.parent, blender=blender, bone_map=bone_map,
                              include_proposed=args.include_proposed)
    save_creature(c, path)
    s = report["summary"]
    print(f"converted meshes {s['meshes_ok']}, clips {s['clips']}, errors {s['errors']} -> {path.parent / 'convert'}")
    for m in report["meshes"]:
        if m["status"] != "ok":
            print(f"  {m['status'].upper()} {m['mesh']}: {m.get('error')}")
        else:
            print(f"  ok {m['mesh']}: {m['fbx']} ({len(m['clips'])} clips)")
    for e in report["clip_errors"]:
        print(f"  clip error {e['name']}: {e['error']}")
    return EXIT_OK if s["errors"] == 0 else EXIT_CHECK_FAILED


def cmd_creature_preview(args: argparse.Namespace, config: Config) -> int:
    import subprocess

    from aniimo_pal.convert.preview import build_preview
    from aniimo_pal.creature.model import load_creature

    path = Path(args.file)
    blender = args.blender or config.tools.blender or None
    report = build_preview(load_creature(path), path.parent, blender=blender)
    print(f"preview scene: {report['blend']}")
    print("variants: " + ", ".join(v["label"] for v in report["variants"]))
    print("animations: " + ", ".join(f"{t['action']} (frames {t['start']}-{t['end']})" for t in report["timeline"]))
    if report.get("render"):
        print(f"preview image: {report['render']}")
    elif report.get("render_error"):
        print(f"(no preview image: {report['render_error']})")
    if args.open:
        if not blender:
            raise ToolkitError("--open needs a Blender executable (--blender or config [tools].blender)")
        subprocess.Popen([blender, report["blend"]])
    return EXIT_OK


def cmd_inventory(args: argparse.Namespace, config: Config) -> int:
    import time

    from aniimo_pal.inventory import build_inventory, default_workers, summary_doc, summary_path
    from aniimo_pal.manifest.io import check_output_location, read_manifest, write_json_atomic

    out = Path(args.output)
    check_output_location(out, [Path(args.root)])
    cache = Path(args.cache) if args.cache else out.parent / "inventory-cache"
    check_output_location(cache, [Path(args.root)])
    workers = args.workers or default_workers()
    started = time.monotonic()

    def progress(done: int, total: int, rel: str) -> None:
        elapsed = time.monotonic() - started
        eta = elapsed / done * (total - done) if done else 0
        print(f"[{done}/{total}] {rel}  (elapsed {elapsed / 60:.1f} min, ETA {eta / 60:.1f} min)", flush=True)

    verify = "none" if args.no_verify else ("hash" if args.verify_hashes else "quick")
    manifest = read_manifest(args.manifest)
    print(f"Listing Unity objects with {workers} worker(s); finished containers are cached in {cache}", flush=True)
    doc = build_inventory(manifest, args.root, include=args.include or (), verify=verify, workers=workers,
                          cache_dir=cache, all_objects=args.all_objects, progress=progress,
                          strategy="full" if args.full else "targeted")
    write_json_atomic(doc, out, indent=0 if doc["summary"]["object_count"] > 100_000 else config.manifest.indent)
    write_json_atomic(summary_doc(doc), summary_path(out), indent=config.manifest.indent)
    s = doc["summary"]
    print(f"containers {s['container_count']} (errors {s['container_errors']}, with skinned meshes "
          f"{s['containers_with_skinned_meshes']}, deep-read {s['containers_deep_read']}, from cache "
          f"{s['containers_from_cache']}, skipped {s['skipped_count']}), objects recorded {s['object_count']} "
          f"(errors {s['object_errors']}), unresolved refs {s['unresolved_refs']}, {s['seconds'] / 60:.1f} min")
    for cls, n in sorted(s["by_class"].items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        print(f"  {cls:<28} {n}")
    errors = [c for c in doc["containers"] if c["status"] != "ok"][:10]
    for c in errors:
        print(f"  ERROR {c['path']}: {c['error']}")
    print(f"objects:    {out}")
    return EXIT_OK


def cmd_objects_find(args: argparse.Namespace, config: Config) -> int:
    import re

    doc = _read_json(args.objects)
    try:
        name_re = re.compile(args.name, re.IGNORECASE) if args.name else None
    except re.error as exc:
        raise ToolkitError(f"invalid --name regex: {exc}") from exc
    classes = {c.lower() for c in args.cls or []}
    hits = []
    for o in doc.get("objects", []):
        if classes and o["class"].lower() not in classes:
            continue
        hay = " ".join(filter(None, [o.get("name"), o.get("asset_path")]))
        if name_re and not name_re.search(hay):
            continue
        hits.append(o)
    if args.json:
        _print_json(hits[: args.limit])
    else:
        for o in hits[: args.limit]:
            info = o.get("info") or {}
            extra = f" {info.get('content')} {info.get('size')}B" if o["class"] == "TextAsset" else ""
            print(f"{o['id']:<28} {o['class']:<20} {str(o.get('name'))[:40]:<40} {o['container']}{extra}")
        print(f"{len(hits)} match(es)" + (f", showing {args.limit}" if len(hits) > args.limit else ""))
    return EXIT_OK


def cmd_catalog_build(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.catalog import build_catalog, load_rules
    from aniimo_pal.catalog.names import load_names
    from aniimo_pal.manifest.io import write_json_atomic

    names = load_names(args.names) if args.names else None
    doc = build_catalog(_read_json(args.objects), load_rules(args.rules), names)
    write_json_atomic(doc, args.output, indent=config.manifest.indent)
    s = doc["summary"]
    print(f"species {s['species_count']} from {s['prefab_count']} prefabs; unassigned clips {s['unassigned_clip_count']} "
          f"(of which fit too many skeletons to assign: {s['ambiguous_clip_count']})")
    d = s["diagnostics"]
    print(f"renderers {d['renderers']}: with mesh {d['renderers_with_mesh']}, with GameObject "
          f"{d['renderers_with_gameobject']}, in a Transform hierarchy {d['renderers_in_hierarchy']}; "
          f"root GameObjects {d['root_gameobjects']}")
    for m in d["missing_mesh_files"]:
        print(f"  meshes not in the object list: {m['count']} in {m['cab']} ({m['container'] or 'container not read'})")
    print(f"catalog:    {args.output}")
    return EXIT_OK


def cmd_catalog_list(args: argparse.Namespace, config: Config) -> int:
    doc = _read_json(args.catalog)
    if args.json:
        _print_json([{"key": s["key"], "display_name": s["display_name"],
                      "variants": sorted({p["variant"] for p in s["prefabs"]}),
                      "assets": len(s["assets"]), "animations": len(s["animations"]),
                      "warnings": len(s["warnings"])} for s in doc["species"]])
        return EXIT_OK
    print(f"{'key':<28} {'name':<24} {'variants':<28} {'assets':>6} {'anims':>6} {'warn':>5}")
    for s in doc["species"]:
        variants = ",".join(sorted({p["variant"] for p in s["prefabs"]}))
        print(f"{s['key']:<28} {s['display_name'][:24]:<24} {variants:<28} {len(s['assets']):>6} "
              f"{len(s['animations']):>6} {len(s['warnings']):>5}")
    return EXIT_OK


def cmd_catalog_show(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.workflow import find_species

    _print_json(find_species(_read_json(args.catalog), args.species))
    return EXIT_OK


def cmd_creature_validate(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import load_creature, validate_creature
    from aniimo_pal.creature.profile import profile_warnings
    from aniimo_pal.systems import bundled_system, load_system

    src = load_system(args.source_system) if args.source_system else bundled_system("aniimo")
    dst = load_system(args.target_system) if args.target_system else bundled_system("palworld")
    data = load_creature(args.file)
    errors = validate_creature(data, src, dst)
    for e in errors:
        print(f"invalid: {e}")
    if isinstance(data, dict):
        for w in profile_warnings(data, src, dst):
            print(f"warning: {w}")
    print(f"{len(errors)} error(s)" if errors else "valid")
    return EXIT_CHECK_FAILED if errors else EXIT_OK


def cmd_creature_roll(args: argparse.Namespace, config: Config) -> int:
    import random
    from collections import Counter

    from aniimo_pal.creature.model import load_creature, validate_creature
    from aniimo_pal.creature.variants import CONTEXTS, one_in, outcome_probabilities, rarity_warnings, select_individual

    data = load_creature(args.file)
    errors = validate_creature(data)
    if errors:
        raise ToolkitError("invalid creature project: " + "; ".join(errors))
    if args.n < 1:
        raise ToolkitError("--n must be at least 1")
    contexts = CONTEXTS if args.context == "both" else (args.context,)
    from aniimo_pal.creature.variants import region_for_biome

    region = region_for_biome(data["variants"], args.biome)
    print(f"{data['id']}: biome={args.biome or '-'} -> regional form {region!r}; one individual per event")
    for ctx in contexts:
        exact = outcome_probabilities(data["variants"], ctx, region)
        rng = random.Random(args.seed)
        counts: Counter[str] = Counter()
        apps: Counter[str] = Counter()
        for _ in range(args.n):
            ind = select_individual(data, biome=args.biome, context=ctx, rng=rng)
            shiny = ind["appearance"] != "standard"
            counts[f"{ind['form']}{'_shiny' if shiny else ''}"] += 1
            if shiny:
                apps[ind["appearance"]] += 1
        print(f"[{ctx}] {args.n:,} simulated events (seed {args.seed})")
        for key, label in (("normal", "Normal"), ("normal_shiny", "Shiny"), ("prismana", "Prismana"),
                           ("prismana_shiny", "Prismana Shiny")):
            p = exact[key]
            print(f"  {label:<15} exact {p:.8f} ({one_in(p):>18})   observed {counts[key]:>9,}")
        if exact["per_appearance"]:
            spread = ", ".join(f"{k}:{apps[k]}" for k in sorted(exact["per_appearance"]))
            print(f"  shiny appearances observed: {spread}")
    for w in rarity_warnings(data["variants"]):
        print(f"warning: {w}")
    return EXIT_OK


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="AniimoPalworldToolkit: read-only asset inventory for Unity -> Palworld (UE 5.1) pipelines.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", help="TOML config file (overrides built-in defaults)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="override logging.level")
    parser.add_argument("--log-file", help="write JSON-lines log to this file")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("scan", help="inventory a directory and write a JSON manifest (read-only)")
    p.add_argument("root", help="directory to scan (never modified)")
    p.add_argument("-o", "--output", required=True, help="manifest path (must be outside ROOT)")
    p.add_argument("--hash", action="append", metavar="ALGO[,ALGO]", help="hash algorithm(s), e.g. sha256,md5")
    p.add_argument("--workers", type=int, help="parallel workers")
    p.add_argument("--exclude", action="append", metavar="GLOB", help="exclude relative paths matching GLOB")
    p.add_argument("--follow-symlinks", action="store_true")
    p.add_argument("--include-hidden", action="store_true")
    p.add_argument("--no-mtime", action="store_true", help="omit modification times")
    p.add_argument("--label", help="root label stored in the manifest (default: directory name)")
    p.add_argument("--store-absolute-root", action="store_true", help="store the absolute root path")
    p.add_argument("-q", "--quiet", action="store_true", help="do not print the summary")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("summary", help="print a manifest summary")
    p.add_argument("manifest")
    p.add_argument("--warnings", action="store_true", help="also list per-file warnings and errors")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("validate", help="validate a manifest against the schema")
    p.add_argument("manifest")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("diff", help="compare two manifests")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fail-on-change", action="store_true", help="exit 1 if the manifests differ")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("verify", help="re-hash files under ROOT and compare with a manifest")
    p.add_argument("manifest")
    p.add_argument("root")
    p.add_argument("--no-extra", action="store_true", help="do not report files missing from the manifest")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("formats", help="list recognised file formats and their pipeline routes")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_formats)

    p = sub.add_parser("doctor", help="report which external tools are available")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="print the effective configuration as JSON")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("systems", help="show a game-system definition (elements, stats, work categories)")
    p.add_argument("system", nargs="?", default="palworld", help="bundled id (aniimo|palworld) or a TOML path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_systems)

    p = sub.add_parser("inventory", help="list objects inside supported Unity containers (UnityPy, read-only)")
    p.add_argument("manifest")
    p.add_argument("root", help="the directory the manifest was scanned from")
    p.add_argument("-o", "--output", required=True, help="objects.json path (outside ROOT)")
    p.add_argument("--include", action="append", metavar="GLOB", help="only containers whose path matches")
    p.add_argument("--no-verify", action="store_true", help="skip checking files against the manifest")
    p.add_argument("--verify-hashes", action="store_true",
                   help="re-hash every file (slow on large installs; default checks size + modification time)")
    p.add_argument("--workers", type=int, help="parallel worker processes (default: CPU cores - 1, max 8)")
    p.add_argument("--cache", help="per-container cache folder for resuming (default: next to the output)")
    p.add_argument("--all-objects", action="store_true", help="record every object, not only relevant classes")
    p.add_argument("--full", action="store_true",
                   help="deep-read every container (slow); default reads only creature bundles and their dependencies")
    p.set_defaults(func=cmd_inventory)

    p = sub.add_parser("objects", help="search an object index (e.g. find stat/element/work data tables)")
    p.add_argument("objects", help="objects.json from `inventory`")
    p.add_argument("--class", dest="cls", action="append", metavar="CLASS",
                   help="filter by Unity class (repeatable), e.g. TextAsset, MonoBehaviour")
    p.add_argument("--name", metavar="REGEX", help="case-insensitive regex on object name / asset path")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_objects_find)

    catalog = sub.add_parser("catalog", help="creature catalog built from an object inventory")
    catsub = catalog.add_subparsers(dest="catalog_command", required=True, metavar="ACTION")
    p = catsub.add_parser("build", help="find creatures and associate their assets")
    p.add_argument("objects", help="objects.json from `inventory`")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--rules", help="catalog rules TOML (default: built-in, see config/catalog_rules.toml)")
    p.add_argument("--names", help="your names for game IDs (TOML: [names] \"10184\" = \"...\"; [forms.\"10184\"])")
    p.set_defaults(func=cmd_catalog_build)
    p = catsub.add_parser("list", help="list species in a catalog")
    p.add_argument("catalog")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_catalog_list)
    p = catsub.add_parser("show", help="print one species as JSON")
    p.add_argument("catalog")
    p.add_argument("species")
    p.set_defaults(func=cmd_catalog_show)

    creature = sub.add_parser("creature", help="per-creature project files (one Aniimo species each)")
    csub = creature.add_subparsers(dest="creature_command", required=True, metavar="ACTION")
    p = csub.add_parser("init", help="create WORKSPACE/creatures/<id>/creature.json")
    p.add_argument("workspace", help="workspace directory (outside the repository and game folder)")
    p.add_argument("--id", help="species id, e.g. aniimo_example (default from catalog)")
    p.add_argument("--name", help="display name (default from catalog)")
    p.add_argument("--from-catalog", metavar="CATALOG", help="prefill from a catalog species")
    p.add_argument("--species", help="catalog species key (see `catalog list`)")
    p.add_argument("--wild-shiny", type=float, default=0.002, help="wild Shiny roll chance (placeholder default)")
    p.add_argument("--wild-prismana", type=float, default=0.0002, help="wild Prismana roll chance (placeholder default)")
    p.add_argument("--egg-shiny", type=float, default=0.002, help="egg Shiny roll chance (placeholder default)")
    p.add_argument("--egg-prismana", type=float, default=0.0, help="egg Prismana roll chance (default 0: wild-only)")
    p.add_argument("--propose-all", action="store_true",
                   help="do not auto-confirm associations backed by explicit game-data references")
    p.set_defaults(func=cmd_creature_init)
    p = csub.add_parser("review", help="list assets and associations awaiting a decision")
    p.add_argument("file")
    p.add_argument("--pending", action="store_true", help="only show items needing a decision")
    p.set_defaults(func=cmd_creature_review)
    p = csub.add_parser("associate", help="confirm/reject associations or set variants")
    p.add_argument("file")
    p.add_argument("--confirm", metavar="I,J,K-L", help="asset indices to confirm")
    p.add_argument("--reject", metavar="I,J,K-L", help="asset indices to reject")
    p.add_argument("--form", action="append", metavar="I=FORM", help="set an asset's form (shared|normal|prismana)")
    p.add_argument("--appearance", action="append", metavar="I=APPEARANCE",
                   help="set an asset's appearance (any|standard|shiny|shiny_NN)")
    p.add_argument("--confirm-all", action="store_true", help="confirm every remaining proposed association")
    p.set_defaults(func=cmd_creature_associate)
    p = csub.add_parser("extract", help="export confirmed assets with UnityPy into the creature folder")
    p.add_argument("file")
    p.add_argument("--root", required=True, help="game install root that the manifest was scanned from")
    p.add_argument("--include-proposed", action="store_true", help="also export proposed (unreviewed) assets")
    p.set_defaults(func=cmd_creature_extract)
    p = csub.add_parser("export-assetripper", help="export the creature's containers with AssetRipper (headless)")
    p.add_argument("file")
    p.add_argument("--root", required=True, help="game install root that the manifest was scanned from")
    p.add_argument("--mode", choices=["primary", "project"], default="primary",
                   help="primary = GLB/PNG; project = Unity project files incl. .anim clips")
    p.add_argument("--assetripper", help="path to AssetRipper.GUI.Free (default: config [tools] or PATH)")
    p.add_argument("--include-proposed", action="store_true")
    p.add_argument("--replace", action="store_true", help="delete and redo a previous export of this mode")
    p.set_defaults(func=cmd_creature_assetripper)
    p = csub.add_parser("convert", help="build rigged FBX (with retargeted clips) for Unreal Engine 5.1 via Blender")
    p.add_argument("file")
    p.add_argument("--blender", help="Blender executable (default: config [tools].blender, else the bpy module)")
    p.add_argument("--bone-map", metavar="JSON", help="rename bones: {\"old\": \"new\"}")
    p.add_argument("--include-proposed", action="store_true")
    p.set_defaults(func=cmd_creature_convert)
    p = csub.add_parser("preview", help="build a Blender scene with every variant and every animation")
    p.add_argument("file")
    p.add_argument("--blender", help="Blender executable (default: config [tools].blender, else the bpy module)")
    p.add_argument("--open", action="store_true", help="open the scene in Blender afterwards")
    p.set_defaults(func=cmd_creature_preview)
    p = csub.add_parser("validate", help="validate a creature.json")
    p.add_argument("file")
    p.add_argument("--source-system", metavar="TOML", help="Aniimo system definition (default: bundled, unverified)")
    p.add_argument("--target-system", metavar="TOML", help="Palworld system definition (default: bundled, unverified)")
    p.set_defaults(func=cmd_creature_validate)
    p = csub.add_parser("roll", help="exact odds + simulation of form/Shiny rolls (wild and egg)")
    p.add_argument("file")
    p.add_argument("--context", choices=["wild", "egg", "both"], default="both")
    p.add_argument("--biome", help="target-game biome id (selects the regional form)")
    p.add_argument("--n", type=int, default=100000, help="number of simulated spawn events")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_creature_roll)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        setup_logging(args.log_level or config.logging.level, args.log_file or config.logging.file or None)
        return args.func(args, config)
    except ToolkitError as exc:
        print(f"{TOOL_NAME}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (NotADirectoryError, PermissionError) as exc:
        print(f"{TOOL_NAME}: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:
        # Output piped into e.g. `head`; exit quietly like standard Unix tools.
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
