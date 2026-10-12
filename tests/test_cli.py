import json

import pytest

from aniimo_pal.cli import EXIT_CHECK_FAILED, EXIT_OK, EXIT_USAGE, main


def test_scan_validate_summary_verify(game_tree, out_dir, capsys):
    root, files = game_tree
    out = out_dir / "manifest.json"
    assert main(["scan", str(root), "-o", str(out), "--hash", "sha256,md5", "--workers", "2"]) == EXIT_OK
    stdout = capsys.readouterr().out
    assert f"files:      {len(files)}" in stdout
    data = json.loads(out.read_text())
    assert data["settings"]["hash_algorithms"] == ["sha256", "md5"]

    assert main(["validate", str(out)]) == EXIT_OK
    assert "valid" in capsys.readouterr().out

    assert main(["summary", str(out), "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["file_count"] == len(files)

    assert main(["summary", str(out), "--warnings"]) == EXIT_OK
    assert "WARN  Game_Data/StreamingAssets/creatures/locked.bundle" in capsys.readouterr().out

    assert main(["verify", str(out), str(root)]) == EXIT_OK
    (root / "Project/notes.txt").write_text("tampered")
    assert main(["verify", str(out), str(root)]) == EXIT_CHECK_FAILED
    assert "CHANGED  Project/notes.txt" in capsys.readouterr().out


def test_scan_refuses_output_in_root(game_tree, capsys):
    root, _ = game_tree
    assert main(["scan", str(root), "-o", str(root / "m.json")]) == EXIT_USAGE
    assert "refusing to write" in capsys.readouterr().err
    assert not (root / "m.json").exists()


def test_scan_missing_root(tmp_path, capsys):
    assert main(["scan", str(tmp_path / "nope"), "-o", str(tmp_path / "m.json")]) == EXIT_USAGE


def test_scan_bad_hash(game_tree, out_dir, capsys):
    root, _ = game_tree
    assert main(["scan", str(root), "-o", str(out_dir / "m.json"), "--hash", "crc99"]) == EXIT_USAGE
    assert "unsupported algorithm" in capsys.readouterr().err


def test_diff_command(game_tree, out_dir, capsys):
    root, _ = game_tree
    a, b = out_dir / "a.json", out_dir / "b.json"
    main(["scan", str(root), "-o", str(a), "-q"])
    (root / "new.txt").write_text("hi")
    main(["scan", str(root), "-o", str(b), "-q"])
    capsys.readouterr()
    assert main(["diff", str(a), str(b)]) == EXIT_OK
    assert "+ new.txt" in capsys.readouterr().out
    assert main(["diff", str(a), str(b), "--fail-on-change"]) == EXIT_CHECK_FAILED
    assert main(["diff", str(a), str(a), "--fail-on-change", "--json"]) == EXIT_OK


def test_validate_reports_errors(game_tree, out_dir, capsys):
    root, _ = game_tree
    out = out_dir / "m.json"
    main(["scan", str(root), "-o", str(out), "-q"])
    data = json.loads(out.read_text())
    data["summary"]["file_count"] = 0
    out.write_text(json.dumps(data))
    assert main(["validate", str(out)]) == EXIT_CHECK_FAILED


def test_formats_and_doctor_and_config(capsys):
    assert main(["formats", "--json"]) == EXIT_OK
    kinds = {r["kind"] for r in json.loads(capsys.readouterr().out)}
    assert {"unity_bundle", "unity_serialized", "model_fbx"} <= kinds
    assert main(["doctor", "--json"]) == EXIT_OK
    names = [t["name"] for t in json.loads(capsys.readouterr().out)["tools"]]
    assert names == ["UnityPy", "AssetRipper", "Blender", "Unreal Editor 5.1"]
    assert main(["config"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["scan"]["hash_algorithms"] == ["sha256"]


def test_config_file_and_log_file(game_tree, out_dir, tmp_path, capsys):
    root, _ = game_tree
    cfg = tmp_path / "local.toml"
    log_file = out_dir / "log.jsonl"
    cfg.write_text(f'[scan]\nhash_algorithms = ["sha1"]\nexclude = ["Exports/*"]\n'
                   f'[logging]\nlevel = "DEBUG"\nfile = "{log_file.as_posix()}"\n')
    out = out_dir / "m.json"
    assert main(["--config", str(cfg), "scan", str(root), "-o", str(out), "-q"]) == EXIT_OK
    data = json.loads(out.read_text())
    assert data["settings"]["hash_algorithms"] == ["sha1"]
    assert not any(f["path"].startswith("Exports/") for f in data["files"])
    lines = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert any(rec["msg"].startswith("scanning") for rec in lines)


def test_bad_config_is_usage_error(tmp_path, capsys):
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[scan]\nworkerz = 3\n")
    assert main(["--config", str(cfg), "config"]) == EXIT_USAGE
    assert "unknown config key scan.workerz" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
