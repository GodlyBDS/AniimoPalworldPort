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


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    _print_json(config.to_dict())
    return EXIT_OK


def cmd_creature_init(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import creature_path, new_creature, save_creature

    path = creature_path(args.workspace, args.id)
    if path.exists():
        raise ToolkitError(f"creature project already exists: {path} (projects are never overwritten)")
    save_creature(new_creature(args.id, args.name), path)
    print(f"created {path}")
    return EXIT_OK


def cmd_creature_validate(args: argparse.Namespace, config: Config) -> int:
    from aniimo_pal.creature.model import load_creature, validate_creature

    errors = validate_creature(load_creature(args.file))
    for e in errors:
        print(f"invalid: {e}")
    print(f"{len(errors)} error(s)" if errors else "valid")
    return EXIT_CHECK_FAILED if errors else EXIT_OK


def cmd_creature_roll(args: argparse.Namespace, config: Config) -> int:
    import random
    from collections import Counter

    from aniimo_pal.creature.model import load_creature, validate_creature
    from aniimo_pal.creature.variants import roll_variant, variant_probabilities

    data = load_creature(args.file)
    errors = validate_creature(data)
    if errors:
        raise ToolkitError("invalid creature project: " + "; ".join(errors))
    if args.n < 1:
        raise ToolkitError("--n must be at least 1")
    rng = random.Random(args.seed)
    counts = Counter(roll_variant(data["variants"], rng=rng) for _ in range(args.n))
    probs = variant_probabilities(data["variants"])
    print(f"{args.n} simulated spawn events (seed {args.seed}); one individual per event:")
    for name in ("normal", "shiny", "prismana"):
        print(f"  {name:<9} {counts[name]:>9}  observed {counts[name] / args.n:.6f}  configured {probs[name]:.6f}")
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

    creature = sub.add_parser("creature", help="per-creature project files (one Aniimo species each)")
    csub = creature.add_subparsers(dest="creature_command", required=True, metavar="ACTION")
    p = csub.add_parser("init", help="create WORKSPACE/creatures/<id>/creature.json")
    p.add_argument("workspace", help="workspace directory (outside the repository and game folder)")
    p.add_argument("--id", required=True, help="species id, e.g. aniimo_example")
    p.add_argument("--name", required=True, help="display name")
    p.set_defaults(func=cmd_creature_init)
    p = csub.add_parser("validate", help="validate a creature.json")
    p.add_argument("file")
    p.set_defaults(func=cmd_creature_validate)
    p = csub.add_parser("roll", help="simulate variant selection to check configured rarity")
    p.add_argument("file")
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
