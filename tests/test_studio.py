"""Studio: portable layout, containment, downloads, installers, steps, preview job, GUI smoke test."""

import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aniimo_pal.config import load_config
from aniimo_pal.studio import downloads, setup, steps
from aniimo_pal.studio.layout import StudioLayout


@pytest.fixture
def layout(tmp_path):
    lay = StudioLayout.for_root(tmp_path / "MyStudio")
    lay.root.mkdir()
    lay.ensure()
    return lay


def test_layout_everything_inside_the_folder(layout):
    for p in (layout.data, layout.venv, layout.tools, layout.workspace, layout.tmp, layout.cache, layout.home,
              layout.logs, layout.settings_file, layout.toolkit_config, layout.venv_python):
        assert layout.contains(p), p
    env = layout.child_env({"PATH": "/usr/bin"})
    for key in ("TMP", "TEMP", "TMPDIR", "PIP_CACHE_DIR", "PYTHONUSERBASE", "BLENDER_USER_RESOURCES",
                "BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "DOTNET_BUNDLE_EXTRACT_BASE_DIR", "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        assert layout.contains(env[key]), key
    home_key = ("APPDATA", "LOCALAPPDATA") if sys.platform == "win32" else ("HOME",)
    assert all(layout.contains(env[k]) for k in home_key)
    assert env["PATH"] == "/usr/bin" and env["PYTHONNOUSERSITE"] == "1"
    assert not layout.contains(layout.root.parent / "elsewhere")


def test_settings_and_toolkit_config(layout):
    assert layout.load_settings() == {}
    layout.save_settings({"game_folder": "C:\\Games\\Aniimo"})
    assert layout.load_settings() == {"game_folder": "C:\\Games\\Aniimo"}
    cfg = load_config(layout.write_toolkit_config({"assetripper": "C:\\x\\AssetRipper.GUI.Free.exe", "blender": ""}))
    assert cfg.tools.assetripper == "C:/x/AssetRipper.GUI.Free.exe"
    assert layout.contains(cfg.logging.file)


def _zip(path: Path, entries: dict[str, bytes], mode: int | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            if mode:
                info.external_attr = mode << 16
            zf.writestr(info, data)
    return path


def test_safe_extract_rejects_path_traversal(tmp_path):
    bad = _zip(tmp_path / "bad.zip", {"../escape.txt": b"x"})
    with pytest.raises(downloads.DownloadError, match="escapes"):
        downloads.extract(bad, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()
    tar_path = tmp_path / "bad.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo("/abs/evil")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(downloads.DownloadError):
        downloads.extract(tar_path, tmp_path / "out2")
    with pytest.raises(downloads.DownloadError, match="unsupported"):
        downloads.extract(tmp_path / "x.rar", tmp_path / "out3")


def test_extract_keeps_executable_bit(tmp_path):
    z = _zip(tmp_path / "ok.zip", {"AssetRipper/AssetRipper.GUI.Free": b"#!/bin/sh\n"}, mode=0o755)
    downloads.extract(z, tmp_path / "out")
    exe = tmp_path / "out" / "AssetRipper" / "AssetRipper.GUI.Free"
    assert exe.is_file()
    if sys.platform != "win32":
        assert os.access(exe, os.X_OK)


def test_remove_tree_only_inside(tmp_path, layout):
    with pytest.raises(downloads.DownloadError):
        downloads.remove_tree(tmp_path, layout.data)


class _FlakyServer:
    """Serves a payload; the first full request is cut off halfway (like a dropped connection)."""

    def __init__(self, payload: bytes, support_range: bool = True, always_cut: bool = False):
        self.payload, self.requests = payload, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                outer.requests.append(rng)
                if rng and support_range:
                    start = int(rng.split("=")[1].rstrip("-"))
                    body = outer.payload[start:]
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.payload)))
                self.end_headers()
                cut = always_cut or len(outer.requests) == 1
                self.wfile.write(outer.payload[: len(outer.payload) // 2] if cut else outer.payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/file.zip"

    def close(self):
        self.httpd.shutdown()


def test_download_resumes_after_truncation(tmp_path):
    payload = os.urandom(300_000)
    srv = _FlakyServer(payload)
    try:
        seen = []
        out = downloads.download(srv.url, tmp_path / "f.zip", progress=lambda d, t: seen.append((d, t)))
    finally:
        srv.close()
    assert out.read_bytes() == payload
    assert srv.requests[0] is None and srv.requests[1] == "bytes=150000-"
    assert seen[-1] == (len(payload), len(payload))
    assert not (tmp_path / "f.zip.part").exists()


def test_download_gives_up_and_leaves_no_partial_file(tmp_path):
    srv = _FlakyServer(os.urandom(10_000), support_range=False, always_cut=True)
    try:
        with pytest.raises(downloads.DownloadError, match="incomplete"):
            downloads.download(srv.url, tmp_path / "f.zip", attempts=2)
    finally:
        srv.close()
    assert not list(tmp_path.iterdir())


def test_pick_assetripper_asset():
    release = {"assets": [{"name": n, "browser_download_url": f"https://x/{n}"} for n in (
        "AssetRipper_win_x64.zip", "AssetRipper_win_arm64.zip", "AssetRipper_linux_x64.zip",
        "AssetRipper_mac_arm64.zip", "AssetRipper_mac_x64.zip", "source.tar.gz")]}
    pick = setup.pick_assetripper_asset
    assert pick(release, setup.PlatformInfo("windows", "x64"))["name"] == "AssetRipper_win_x64.zip"
    assert pick(release, setup.PlatformInfo("linux", "x64"))["name"] == "AssetRipper_linux_x64.zip"
    assert pick(release, setup.PlatformInfo("macos", "arm64"))["name"] == "AssetRipper_mac_arm64.zip"
    with pytest.raises(downloads.DownloadError):
        pick({"assets": []}, setup.PlatformInfo("linux", "arm64"))


def test_pick_blender_build_picks_newest_patch():
    html = ('<a href="blender-5.2.0-windows-x64.zip">x</a> <a href="blender-5.2.2-windows-x64.zip">x</a> '
            '<a href="blender-5.2.10-windows-x64.zip">x</a> <a href="blender-5.2.2-linux-x64.tar.xz">x</a> '
            '<a href="blender-5.2.2-windows-arm64.zip">x</a>')
    assert setup.pick_blender_build(html, setup.PlatformInfo("windows", "x64")) == "blender-5.2.10-windows-x64.zip"
    assert setup.pick_blender_build(html, setup.PlatformInfo("linux", "x64")) == "blender-5.2.2-linux-x64.tar.xz"
    with pytest.raises(downloads.DownloadError, match="macOS"):
        setup.pick_blender_build(html, setup.PlatformInfo("macos", "arm64"))
    with pytest.raises(downloads.DownloadError):
        setup.pick_blender_build("", setup.PlatformInfo("linux", "x64"))


def test_install_assetripper_from_zip_and_checks(layout, tmp_path):
    assert setup.check_assetripper(layout).ok is False
    z = _zip(tmp_path / "AssetRipper_linux_x64.zip", {"AssetRipper.GUI.Free": b"bin", "AssetRipper.GUI.Free.exe": b"bin",
                                                      "libcapstone.so": b"lib"}, mode=0o755)
    exe = setup.install_assetripper(layout, lambda _m: None, zip_path=z, plat=setup.PlatformInfo("linux", "x64"))
    assert layout.contains(exe) and setup.check_assetripper(layout).ok
    assert setup.tool_paths(layout)["assetripper"] == str(exe)
    bad = _zip(tmp_path / "other.zip", {"readme.txt": b"no exe"})
    with pytest.raises(downloads.DownloadError, match="does not contain"):
        setup.install_assetripper(layout, lambda _m: None, zip_path=bad)


def test_run_checks_shape(layout):
    keys = [c.key for c in setup.run_checks(layout)]
    assert keys == ["python", "python_env", "assetripper", "blender", "disk"]
    blender = next(c for c in setup.run_checks(layout, {"blender_external": "/nope/blender"}) if c.key == "blender")
    assert blender.ok is False


def test_validate_game_folder(layout, tmp_path):
    game = tmp_path / "Aniimo"
    (game / "Aniimo_Data").mkdir(parents=True)
    ok, msg = steps.validate_game_folder(layout, game)
    assert ok and "Aniimo_Data" in msg
    assert steps.validate_game_folder(layout, "")[0] is False
    assert steps.validate_game_folder(layout, tmp_path / "missing")[0] is False
    assert steps.validate_game_folder(layout, layout.workspace)[0] is False  # inside the Studio data
    assert steps.validate_game_folder(layout, tmp_path)[0] is False  # contains the Studio itself
    plain = tmp_path / "Plain"
    plain.mkdir()
    assert steps.validate_game_folder(layout, plain)[0] is True


def test_step_commands_write_only_into_workspace(layout, tmp_path):
    r = steps.StepRunner(layout, str(tmp_path / "Aniimo"), {"assetripper": "/t/ar", "blender": "/t/blender"})
    cfile = r.ws.creature_file("aniimo_foxlet")
    cmds = [r.cmd_scan(), r.cmd_inventory(), r.cmd_catalog(), r.cmd_create("foxlet"), r.cmd_extract(cfile),
            r.cmd_assetripper(cfile, "primary"), r.cmd_convert(cfile), r.cmd_preview(cfile)]
    for cmd in cmds:
        assert cmd[:5] == [str(layout.venv_python), "-m", "aniimo_pal", "--config", str(layout.toolkit_config)]
        for i, arg in enumerate(cmd):
            if arg in ("-o", "--output"):
                assert layout.contains(cmd[i + 1])
    assert "--replace" in r.cmd_assetripper(cfile, "project")
    assert r.cmd_convert(cfile)[-2:] == ["--blender", "/t/blender"]


def test_step_runner_streams_output(layout, monkeypatch):
    r = steps.StepRunner(layout, "/game", {})
    lines = []
    code = r.run([sys.executable, "-c", "import os, sys; print('cwd-ok' if os.getcwd() == sys.argv[3] else 'bad'); "
                  "print(os.environ['TMP'])", "x", "y", str(layout.workspace)], lines.append)
    assert code == 0 and "cwd-ok" in lines and str(layout.tmp) in lines


def test_step_runner_reports_progress_lines(layout):
    seen = []
    r = steps.StepRunner(layout, "/game", {}, on_progress=lambda d, t, m: seen.append((d, t, m)))
    code = r.run([sys.executable, "-c", "print('[1/3] survey: a.bundle  (elapsed 0.0 min, ETA 0.1 min)'); print('done')",
                  "x", "y", "z"], lambda _line: None)
    assert code == 0 and seen == [(1, 3, "survey: a.bundle  (elapsed 0.0 min, ETA 0.1 min)")]


def test_step_runner_stop_kills_the_whole_process_tree(layout, tmp_path):
    import time

    pidfile = tmp_path / "child.pid"
    # The step starts a child process (like a worker pool or Blender) and both run "forever".
    script = ("import subprocess, sys, time; "
              "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              f"open({str(pidfile)!r}, 'w').write(str(c.pid)); print('started', flush=True); time.sleep(60)")
    r = steps.StepRunner(layout, "/game", {})
    lines, outcome = [], {}

    def job():
        try:
            r.run([sys.executable, "-c", script, "x", "y"], lines.append)
        except steps.StepStopped:
            outcome["stopped"] = True

    t = threading.Thread(target=job)
    t.start()
    deadline = time.monotonic() + 20
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert r.stop() is True
    t.join(20)
    assert not t.is_alive() and outcome == {"stopped": True} and "(stopped by user)" in lines
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child)
    with pytest.raises(steps.StepStopped):  # a stopped runner starts nothing new
        r.run([sys.executable, "-c", "print(1)", "x", "y"], lines.append)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a reaped-later zombie counts as gone
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def test_unknown_files_breakdown():
    manifest = {"files": [
        {"path": "Data/Packs/a.pak", "kind": "unknown_high_entropy"},
        {"path": "Data/Packs/b.pak", "kind": "unknown_high_entropy"},
        {"path": "Data/Video/intro", "kind": "unknown_high_entropy"},
        {"path": "c.bin", "kind": "unknown_high_entropy"},
        {"path": "Data/x.bundle", "kind": "unity_bundle"},
    ]}
    br = steps.unknown_files_breakdown(manifest)
    assert br["extensions"] == [(".pak", 2), ("(no extension)", 1), (".bin", 1)]
    assert br["folders"] == [("Data/Packs", 2), ("Data/Video", 1), ("(top level)", 1)]


def test_mods_information_is_complete():
    from aniimo_pal.studio import mods

    for m in mods.IN_GAME_MODS + mods.FOR_BUILDING_MODS:
        assert m["name"] and m["why"] and m["install"]
        assert m["url"] is None or m["url"].startswith("https://")
    names = " ".join(m["name"] for m in mods.IN_GAME_MODS)
    assert "UE4SS" in names and "PalSchema" in names


def test_start_studio_check_mode(tmp_path):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, str(root / "start_studio.py"), "--check"], capture_output=True, text=True,
                          cwd=tmp_path, timeout=120)
    assert "Python 3.11+ for the Studio" in proc.stdout and "AssetRipper" in proc.stdout


def test_preview_job_picks_variant_textures(tmp_path):
    from aniimo_pal.convert.preview import preview_job, variant_label

    cdir = tmp_path / "creature"
    (cdir / "convert").mkdir(parents=True)
    (cdir / "convert" / "m.fbx").write_bytes(b"fbx")
    mats = cdir / "extract" / "unitypy" / "materials"
    mats.mkdir(parents=True)
    for name, tex in (("normal", "textures/normal/A.png"), ("shiny", "textures/normal/shiny_01/S.png"),
                      ("prismana", "textures/prismana/P.png")):
        (mats / f"{name}.json").write_text(json.dumps({"textures": {"_MainTex": {"file": tex}}}))
    asset = lambda name, form, app: {"role": "material", "association": "confirmed", "form": form,  # noqa: E731
                                      "appearance": app, "extracted": f"extract/unitypy/materials/{name}.json"}
    creature = {"assets": [{"role": "mesh", "converted": "convert/m.fbx"},
                           asset("normal", "normal", "standard"), asset("shiny", "normal", "shiny_01"),
                           asset("prismana", "prismana", "standard")],
                "prefabs": [{"form": "prismana", "appearance": "standard"}, {"form": "normal", "appearance": "shiny_01"},
                            {"form": "normal", "appearance": "standard"}, {"form": "unclassified", "appearance": "standard"}]}
    job = preview_job(creature, cdir)
    assert [v["label"] for v in job["variants"]] == ["Normal", "Shiny 1", "Prismana"]
    assert [Path(v["texture"]).name for v in job["variants"]] == ["A.png", "S.png", "P.png"]
    assert variant_label("prismana", "shiny_07") == "Prismana Shiny 7"
    creature["assets"][0]["converted"] = None
    from aniimo_pal.errors import ToolkitError

    with pytest.raises(ToolkitError, match="convert"):
        preview_job(creature, cdir)


def test_gui_smoke(layout):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    from aniimo_pal.studio.app import StudioApp

    app = StudioApp(root, layout)
    try:
        assert {c.key for c in app.checks} >= {"python_env", "assetripper", "blender"}
        assert str(app.nb.tab(app.tab_scan, "state")) == "disabled"  # gated until setup + game folder are done
        app.game_var.set(str(layout.root.parent))  # contains the Studio -> rejected
        app.accept_game()
        assert "game_folder" not in app.settings
        assert app.stop_button.instate(["disabled"]) == (not app.busy)  # enabled only while a task runs
    finally:
        app.on_close()
