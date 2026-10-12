"""Aniimo -> Palworld Studio: guided tkinter GUI.

Tabs follow the workflow: Setup -> Game folder -> Scan & catalog -> Creatures,
plus a Palworld mods page and the log. Long operations run in a background
thread and stream their output into the log; the window stays responsive.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable

from aniimo_pal.studio import mods, setup, steps
from aniimo_pal.studio.layout import StudioLayout

TITLE = "Aniimo → Palworld Studio"
OK, MISSING, WARN = "✔", "✖", "!"


STOPPED = "stopped by user"


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening a folder for the user
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class StudioApp:
    def __init__(self, tk_root: tk.Tk, layout: StudioLayout):
        self.tk = tk_root
        self.layout = layout
        layout.ensure()
        self.settings = layout.load_settings()
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.current_task = ""
        self.current_runner: steps.StepRunner | None = None
        self.action_buttons: list[ttk.Button] = []
        self.checks: list[setup.CheckResult] = []
        self.log_file = open(layout.logs / "studio.log", "a", encoding="utf-8")  # noqa: SIM115

        tk_root.title(TITLE)
        tk_root.geometry("1100x720")
        tk_root.minsize(900, 600)
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 14, "bold"))
        style.configure("Accent.TButton", font=("TkDefaultFont", 10, "bold"))

        header = ttk.Frame(tk_root, padding=(12, 10, 12, 0))
        header.pack(fill="x")
        ttk.Label(header, text=TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"Everything is stored in: {layout.root}", foreground="#555").pack(side="right")

        status = ttk.Frame(tk_root, padding=(12, 0, 12, 8))
        status.pack(side="bottom", fill="x")  # packed before the tabs so it is never squeezed out
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.stop_button = ttk.Button(status, text="Stop", command=self.stop_task)
        self.stop_button.pack(side="right", padx=(6, 0))
        self.stop_button.state(["disabled"])
        self.progress = ttk.Progressbar(status, length=260, mode="determinate")
        self.progress.pack(side="right")

        self.nb = ttk.Notebook(tk_root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=8)
        self.tab_setup = self._build_setup_tab()
        self.tab_game = self._build_game_tab()
        self.tab_scan = self._build_scan_tab()
        self.tab_creatures = self._build_creatures_tab()
        self.tab_mods = self._build_mods_tab()
        self.tab_log = self._build_log_tab()

        tk_root.protocol("WM_DELETE_WINDOW", self.on_close)
        tk_root.after(100, self._drain_events)
        self.refresh_checks(first_run=True)

    # ------------------------------------------------------------------ helpers
    def log(self, text: str) -> None:
        """Thread-safe log."""
        self.events.put(("log", text))

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.log_file.write(text + "\n")
        self.log_file.flush()

    def _progress(self, done: int, total: int | None) -> None:
        self.events.put(("progress", (done, total)))

    def _count_progress(self, done: int, total: int, text: str) -> None:
        """Thread-safe progress of a long step (e.g. '[120/2158] survey: ... ETA 9.2 min')."""
        self.events.put(("count", (done, total, text)))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    done, total = payload
                    if total:
                        self.progress.configure(mode="determinate", maximum=total, value=done)
                        self.status_var.set(f"{self.current_task}: {done / 1024 ** 2:.0f} / {total / 1024 ** 2:.0f} MB")
                elif kind == "count":
                    done, total, text = payload
                    if total:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=total, value=done)
                        eta = text.rsplit("ETA ", 1)[-1].rstrip(")") if "ETA " in text else ""
                        stage = text.split(":", 1)[0]
                        self.status_var.set(f"{self.current_task}: {stage} {done} / {total}"
                                            + (f", about {eta} left" if eta and done < total else ""))
                elif kind == "done":
                    callback, error = payload
                    self._task_finished(callback, error)
        except queue.Empty:
            pass
        self.tk.after(100, self._drain_events)

    def button(self, parent, text, command, accent=False) -> ttk.Button:
        b = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        self.action_buttons.append(b)
        return b

    def run_task(self, title: str, fn: Callable[[], None], on_done: Callable[[], None] | None = None) -> None:
        if self.busy:
            messagebox.showinfo(TITLE, "Please wait: another task is still running.")
            return
        self.busy = True
        self.current_task = title
        for b in self.action_buttons:
            b.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.status_var.set(f"{title} ...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.log(f"=== {title} ===")

        def worker():
            error = None
            try:
                fn()
            except steps.StepStopped:
                error = STOPPED
            except steps.StepFailed as exc:  # the reason is already in the log, no traceback needed
                error = str(exc)
            except Exception as exc:  # report every failure in the GUI
                error = f"{type(exc).__name__}: {exc}"
                self.log(traceback.format_exc().rstrip())
            self.events.put(("done", (on_done, error)))

        threading.Thread(target=worker, daemon=True).start()

    def _task_finished(self, callback, error) -> None:
        self.busy = False
        self.current_runner = None
        self.stop_button.state(["disabled"])
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        for b in self.action_buttons:
            b.state(["!disabled"])
        if error == STOPPED:
            self.status_var.set(f"{self.current_task}: stopped")
            self.log("Stopped. Finished parts are kept; running the step again continues where it stopped.")
            callback = None
        elif error:
            self.status_var.set(f"{self.current_task} failed")
            self.log(f"FAILED: {error}")
            messagebox.showerror(TITLE, f"{self.current_task} failed:\n\n{error}\n\nSee the Log tab for details.")
        else:
            self.status_var.set(f"{self.current_task}: done")
        if callback:
            callback()
        self.refresh_gating()

    def stop_task(self) -> None:
        r = self.current_runner
        if not self.busy:
            return
        if r is None:
            messagebox.showinfo(TITLE, "This task cannot be stopped midway. Please wait for it to finish.")
            return
        self.status_var.set(f"{self.current_task}: stopping ...")
        self.stop_button.state(["disabled"])
        threading.Thread(target=r.stop, daemon=True).start()

    def save_settings(self) -> None:
        self.layout.save_settings(self.settings)

    # ------------------------------------------------------------------ Setup tab
    def _build_setup_tab(self) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="1  Setup")
        ttk.Label(f, wraplength=1000, justify="left", text=(
            "First-run setup. The Studio needs a Python environment with the toolkit, AssetRipper (exports Unity "
            "assets) and Blender (converts models and animations). Everything is installed inside "
            f"{self.layout.data}. Nothing is installed anywhere else on your PC.")).pack(anchor="w")
        cols = ("status", "item", "detail", "location")
        self.setup_tree = ttk.Treeview(f, columns=cols, show="headings", height=7, selectmode="browse")
        for col, width, label in (("status", 60, ""), ("item", 330, "Requirement"), ("detail", 330, "Status"),
                                  ("location", 300, "Location")):
            self.setup_tree.heading(col, text=label)
            self.setup_tree.column(col, width=width, anchor="w")
        self.setup_tree.pack(fill="x", pady=10)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.button(row, "Install everything missing", self.install_missing, accent=True).pack(side="left")
        self.button(row, "Install selected", self.install_selected).pack(side="left", padx=6)
        self.button(row, "Re-check", lambda: self.refresh_checks()).pack(side="left")
        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=8)
        self.button(row2, "Install AssetRipper from a downloaded .zip ...", self.install_assetripper_zip).pack(side="left")
        self.button(row2, "Select an installed Blender ...", self.select_blender).pack(side="left", padx=6)
        self.button(row2, "Open download page of selected", self.open_selected_help).pack(side="left")
        self.setup_summary = ttk.Label(f, text="", wraplength=1000, justify="left")
        self.setup_summary.pack(anchor="w", pady=8)
        return f

    def refresh_checks(self, first_run: bool = False) -> None:
        self.checks = setup.run_checks(self.layout, self.settings)
        self.setup_tree.delete(*self.setup_tree.get_children())
        for c in self.checks:
            mark = OK if c.ok else (MISSING if c.required else WARN)
            self.setup_tree.insert("", "end", iid=c.key, values=(mark, c.title, c.detail, c.location or ""))
        missing = [c for c in self.checks if c.required and not c.ok]
        if missing:
            self.setup_summary.configure(text="Missing: " + ", ".join(c.title for c in missing)
                                         + ". Click 'Install everything missing'.")
        else:
            self.setup_summary.configure(text="Setup complete. Continue with tab 2 (Game folder).")
        self.layout.write_toolkit_config(setup.tool_paths(self.layout, self.settings))
        self.refresh_gating()
        if first_run and missing:
            self.nb.select(self.tab_setup)
            self.log("First run: some requirements are missing. Install them from the Setup tab.")

    def setup_ok(self, key: str) -> bool:
        return any(c.key == key and c.ok for c in self.checks)

    def refresh_gating(self) -> None:
        env_ok = self.setup_ok("python_env")
        game_ok = bool(self.settings.get("game_folder")) and steps.validate_game_folder(
            self.layout, self.settings.get("game_folder", ""))[0]
        self.nb.tab(self.tab_game, state="normal" if env_ok else "disabled")
        self.nb.tab(self.tab_scan, state="normal" if env_ok and game_ok else "disabled")
        self.nb.tab(self.tab_creatures, state="normal" if env_ok and game_ok else "disabled")

    def _installer(self, key: str, **kw) -> Callable[[], None]:
        if key == "python_env":
            return lambda: setup.install_python_env(self.layout, self.log)
        if key == "assetripper":
            return lambda: setup.install_assetripper(self.layout, self.log, self._progress, **kw)
        if key == "blender":
            return lambda: setup.install_blender(self.layout, self.log, self._progress)
        raise ValueError(key)

    def install_missing(self) -> None:
        todo = [c for c in self.checks if c.required and not c.ok and c.can_install]
        if not todo:
            manual = [c for c in self.checks if c.required and not c.ok]
            if manual:
                messagebox.showinfo(TITLE, "These need a manual step:\n\n" + "\n".join(
                    f"• {c.title}: {c.detail}" for c in manual))
            else:
                messagebox.showinfo(TITLE, "Everything is already installed.")
            return
        if not messagebox.askyesno(TITLE, "Install now?\n\n" + "\n".join(f"• {c.title}" for c in todo)
                                   + f"\n\nDownloads may total over 500 MB. Everything goes into:\n{self.layout.data}"):
            return
        installers = [(c.title, self._installer(c.key)) for c in todo]

        def job():
            for title, fn in installers:
                self.log(f"--- {title} ---")
                fn()
        self.run_task("Installing requirements", job, lambda: self.refresh_checks())

    def install_selected(self) -> None:
        sel = self.setup_tree.selection()
        if not sel:
            messagebox.showinfo(TITLE, "Select a row first.")
            return
        check = next(c for c in self.checks if c.key == sel[0])
        if not check.can_install:
            messagebox.showinfo(TITLE, f"{check.title} cannot be installed automatically: {check.detail}")
            return
        self.run_task(f"Installing {check.title}", self._installer(check.key), lambda: self.refresh_checks())

    def install_assetripper_zip(self) -> None:
        path = filedialog.askopenfilename(title="Select the AssetRipper release .zip",
                                          filetypes=[("Zip archives", "*.zip")])
        if path:
            self.run_task("Installing AssetRipper from zip", self._installer("assetripper", zip_path=Path(path)),
                          lambda: self.refresh_checks())

    def select_blender(self) -> None:
        path = filedialog.askopenfilename(title="Select the Blender executable (blender.exe / Blender.app/Contents/MacOS/Blender)")
        if path:
            self.settings["blender_external"] = path
            self.save_settings()
            self.refresh_checks()

    def open_selected_help(self) -> None:
        sel = self.setup_tree.selection()
        check = next((c for c in self.checks if sel and c.key == sel[0]), None)
        if check and check.help_url:
            webbrowser.open(check.help_url)

    # ------------------------------------------------------------------ Game folder tab
    def _build_game_tab(self) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="2  Game folder")
        ttk.Label(f, wraplength=1000, justify="left", text=(
            "Select the folder where Aniimo is installed (the folder containing the game's executable and its data "
            "folders). The Studio only READS from it; it never changes, moves or deletes game files. All results "
            f"are written to {self.layout.workspace}.")).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=12)
        self.game_var = tk.StringVar(value=self.settings.get("game_folder", ""))
        ttk.Entry(row, textvariable=self.game_var, width=90).pack(side="left", fill="x", expand=True)
        self.button(row, "Browse ...", self.browse_game).pack(side="left", padx=6)
        self.button(row, "Use this folder", self.accept_game, accent=True).pack(side="left")
        self.game_status = ttk.Label(f, text="", wraplength=1000)
        self.game_status.pack(anchor="w")
        ttk.Label(f, wraplength=1000, justify="left", foreground="#7a4b00", text=(
            "Only use game files you are allowed to use. Encrypted or protected files are reported and skipped: "
            "the Studio never decrypts or bypasses protection.")).pack(anchor="w", pady=16)
        return f

    def browse_game(self) -> None:
        path = filedialog.askdirectory(title="Select the Aniimo install folder")
        if path:
            self.game_var.set(path)
            self.accept_game()

    def accept_game(self) -> None:
        ok, msg = steps.validate_game_folder(self.layout, self.game_var.get())
        self.game_status.configure(text=("✔ " if ok else "✖ ") + msg, foreground="#1b6e1b" if ok else "#a00")
        if ok:
            self.settings["game_folder"] = str(Path(self.game_var.get()).resolve())
            self.save_settings()
            self.refresh_gating()

    # ------------------------------------------------------------------ Scan tab
    def _build_scan_tab(self) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="3  Scan & catalog")
        ttk.Label(f, wraplength=1000, justify="left", text=(
            "Step 1 lists every file and checks its format. Step 2 lists the objects inside readable Unity files. "
            "Step 3 finds the creatures and their models, textures, skeletons and animations. Large installs can "
            "take a while.")).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=10)
        self.button(row, "Run all three steps", lambda: self.run_scan(("scan", "inventory", "catalog")),
                    accent=True).pack(side="left")
        self.button(row, "1. Scan files", lambda: self.run_scan(("scan",))).pack(side="left", padx=6)
        self.button(row, "2. List Unity objects", lambda: self.run_scan(("inventory",))).pack(side="left")
        self.button(row, "3. Find creatures", lambda: self.run_scan(("catalog",))).pack(side="left", padx=6)
        self.button(row, "Open workspace folder", lambda: open_path(self.layout.workspace)).pack(side="right")
        self.scan_text = ScrolledText(f, height=22, wrap="word", state="disabled")
        self.scan_text.pack(fill="both", expand=True)
        self.show_scan_summary()
        return f

    def runner(self) -> steps.StepRunner:
        """A runner for the next task; the Stop button stops whatever it is running."""
        r = steps.StepRunner(self.layout, self.settings.get("game_folder", ""),
                             setup.tool_paths(self.layout, self.settings), on_progress=self._count_progress)
        if not self.busy:  # never replace the runner of a task that is still running
            self.current_runner = r
        return r

    def run_scan(self, which: tuple[str, ...]) -> None:
        r = self.runner()
        cmds = {"scan": r.cmd_scan, "inventory": r.cmd_inventory, "catalog": r.cmd_catalog}

        def job():
            for name in which:
                if r.run(cmds[name](), self.log) != 0:
                    raise RuntimeError(f"step '{name}' did not complete; see the log")
        self.run_task("Scanning", job, lambda: (self.show_scan_summary(), self.refresh_species()))

    def show_scan_summary(self) -> None:
        ws = steps.Workspace(self.layout)
        lines = []
        m = steps.read_json(ws.manifest)
        if m:
            s = m["summary"]
            lines += [f"Files scanned: {s['file_count']} ({s['total_bytes'] / 1024 ** 3:.2f} GB)",
                      "Unity containers by kind: " + ", ".join(f"{k} {v}" for k, v in s["by_kind"].items()
                                                                if k.startswith("unity")),
                      f"Supported for extraction: {s['supported_count']}",
                      f"Encryption-flagged (skipped, never decrypted): {s['encrypted_count']}",
                      f"Unity versions: {', '.join(s.get('unity_versions', {})) or 'unknown'}", ""]
            prefixed = sum(1 for f in m["files"] if (f.get("details") or {}).get("signature_offset"))
            if prefixed:
                lines.insert(-1, f"Unity bundles behind a prefix (custom container or obfuscation; skipped, "
                                 f"never stripped): {prefixed}")
            unknown = s["by_kind"].get("unknown_high_entropy", 0)
            if unknown:
                lines.append(f"Note: {unknown} files are unrecognised high-entropy data (custom or protected formats, "
                             "or compressed data the scanner does not know). They are left alone.")
                br = steps.unknown_files_breakdown(m)
                lines.append("  by extension: " + ", ".join(f"{e} {n}" for e, n in br["extensions"]))
                lines.append("  by folder:    " + ", ".join(f"{d} {n}" for d, n in br["folders"]))
                lines.append("")
        o = steps.read_objects_summary(ws.objects)
        if o is not None and "summary" not in o:
            lines.append("Unity objects listed (made by an older version: run '2. List Unity objects' again to see "
                         "its summary; finished bundles are reused).")
        elif o:
            so = o["summary"]
            lines.append(f"Unity objects listed: {so['object_count']} in {so['container_count']} containers "
                         f"({so['container_errors']} container errors)")
            if "containers_deep_read" in so:
                lines.append(f"  containers with creature meshes: {so['containers_with_skinned_meshes']}, read in "
                             f"detail (incl. their dependencies): {so['containers_deep_read']}, "
                             f"took {so['seconds'] / 60:.1f} min")
        c = steps.read_json(ws.catalog)
        if c:
            cs = c["summary"]
            lines.append(f"Creatures found: {cs['species_count']} ({cs.get('game_id_count', '?')} distinct creature IDs, "
                         f"from {cs['prefab_count']} prefabs; {cs.get('prefabs_filtered_out', 0)} other rigged "
                         "models such as effects, items and NPCs not shown)")
            if steps.is_older(ws.catalog, ws.objects):
                lines.append("  This creature list is older than the object list: click '3. Find creatures'.")
            d = cs.get("diagnostics")
            if d and not cs["species_count"]:
                lines.append(f"  Why none: {d['renderers']} creature mesh renderers; with their mesh found "
                             f"{d['renderers_with_mesh']}, attached to a GameObject {d['renderers_with_gameobject']}, "
                             f"in a bone/transform hierarchy {d['renderers_in_hierarchy']}; root GameObjects "
                             f"{d['root_gameobjects']}. Please share these numbers.")
                for m in d["missing_mesh_files"][:5]:
                    lines.append(f"    meshes missing from the object list: {m['count']} in "
                                 f"{m['container'] or m['cab'] + ' (not read)'}")
        if not lines:
            lines = ["Nothing scanned yet. Click 'Run all three steps'."]
        self.scan_text.configure(state="normal")
        self.scan_text.delete("1.0", "end")
        self.scan_text.insert("end", "\n".join(lines))
        self.scan_text.configure(state="disabled")

    # ------------------------------------------------------------------ Creatures tab
    def _build_creatures_tab(self) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="4  Creatures")
        paned = ttk.PanedWindow(f, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        ttk.Label(left, text="Creatures found in the game").pack(anchor="w")
        top = ttk.Frame(left)
        top.pack(fill="x", pady=(4, 0))
        ttk.Label(top, text="Search:").pack(side="left")
        self.species_filter = tk.StringVar()
        self.species_filter.trace_add("write", lambda *_a: self.refresh_species())
        ttk.Entry(top, textvariable=self.species_filter, width=18).pack(side="left", padx=4, fill="x", expand=True)
        self.button(top, "Choose creature groups…", self.choose_groups).pack(side="right")
        self.species_count = ttk.Label(left, text="", foreground="#555")
        self.species_count.pack(anchor="w")
        tree_box = ttk.Frame(left)
        tree_box.pack(fill="both", expand=True, pady=4)
        cols = ("name", "codename", "variants", "project")
        self.species_tree = ttk.Treeview(tree_box, columns=cols, show="headings", selectmode="browse")
        for col, w, label in (("name", 150, "Name"), ("codename", 100, "Codename (guess)"),
                              ("variants", 120, "Forms / appearances"), ("project", 70, "Project")):
            self.species_tree.heading(col, text=label)
            self.species_tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(tree_box, orient="vertical", command=self.species_tree.yview)
        self.species_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.species_tree.pack(side="left", fill="both", expand=True)
        self.species_tree.bind("<<TreeviewSelect>>", lambda _e: self.show_creature())

        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        paned.add(right, weight=2)
        # Buttons first, so long creature details can never push them out of the window.
        row = ttk.Frame(right)
        row.pack(fill="x")
        self.button(row, "Create project", self.create_project, accent=True).pack(side="left")
        self.button(row, "Run all steps", lambda: self.creature_steps(steps.CREATURE_STEPS),
                    accent=True).pack(side="left", padx=6)
        self.button(row, "Open creature folder", self.open_creature_folder).pack(side="right")
        self.button(row, "Set names…", self.set_names).pack(side="right", padx=6)
        row2 = ttk.Frame(right)
        row2.pack(fill="x", pady=(6, 0))
        self.button(row2, "Extract", lambda: self.creature_steps(("extract",))).pack(side="left")
        self.button(row2, "Export animations (AssetRipper)",
                    lambda: self.creature_steps(("project",))).pack(side="left", padx=6)
        self.button(row2, "Convert to FBX (Blender)", lambda: self.creature_steps(("convert",))).pack(side="left")
        row_p = ttk.Frame(right)
        row_p.pack(fill="x", pady=(6, 0))
        self.button(row_p, "Build 3D preview", lambda: self.creature_steps(("preview",))).pack(side="left")
        self.button(row_p, "Open 3D preview in Blender", self.open_preview, accent=True).pack(side="left", padx=6)
        self.creature_info = ScrolledText(right, height=9, wrap="word", state="disabled")
        self.creature_info.pack(fill="x", pady=(8, 0))
        self._set_creature_info("Select a creature on the left.")
        sub = ttk.Notebook(right)
        sub.pack(fill="both", expand=True, pady=(8, 0))
        self.creature_sub = sub
        pv = ttk.Frame(sub, padding=6)
        sub.add(pv, text="3D preview (all variants + animations)")
        self.preview_label = ttk.Label(pv, wraplength=620, justify="left")
        self.preview_label.pack(anchor="center", expand=True)
        self.preview_image = None
        av = ttk.Frame(sub, padding=6)
        sub.add(av, text="Assets (★ = needs your decision)")
        row3 = ttk.Frame(av)
        row3.pack(fill="x")
        self.button(row3, "Confirm selected", lambda: self.decide("confirm")).pack(side="left")
        self.button(row3, "Reject selected", lambda: self.decide("reject")).pack(side="left", padx=6)
        self.button(row3, "Confirm all proposed", lambda: self.decide("all")).pack(side="left")
        acols = ("idx", "role", "form", "appearance", "assoc", "prov", "name")
        abox = ttk.Frame(av)
        abox.pack(fill="both", expand=True, pady=4)
        self.asset_tree = ttk.Treeview(abox, columns=acols, show="headings", selectmode="extended", height=12)
        for col, w in (("idx", 50), ("role", 80), ("form", 80), ("appearance", 90), ("assoc", 85), ("prov", 70),
                       ("name", 220)):
            self.asset_tree.heading(col, text=col)
            self.asset_tree.column(col, width=w, anchor="w")
        asb = ttk.Scrollbar(abox, orient="vertical", command=self.asset_tree.yview)
        self.asset_tree.configure(yscrollcommand=asb.set)
        asb.pack(side="right", fill="y")
        self.asset_tree.pack(side="left", fill="both", expand=True)
        self._catalog_cache: tuple[float, dict | None] = (-1.0, None)
        self.refresh_species()
        return f

    def _set_creature_info(self, text: str) -> None:
        self.creature_info.configure(state="normal")
        self.creature_info.delete("1.0", "end")
        self.creature_info.insert("end", text)
        self.creature_info.configure(state="disabled")

    def load_catalog(self) -> dict | None:
        """The catalog, re-read only when the file changed (it can be large)."""
        path = steps.Workspace(self.layout).catalog
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if self._catalog_cache[0] != mtime:
            self._catalog_cache = (mtime, steps.read_json(path))
        return self._catalog_cache[1]

    def species_entry(self, key: str | None) -> dict | None:
        cat = self.load_catalog()
        return next((s for s in (cat or {}).get("species", []) if s["key"] == key), None) if key else None

    def selected_species(self) -> str | None:
        sel = self.species_tree.selection()
        return sel[0] if sel else None

    def refresh_species(self) -> None:
        ws = steps.Workspace(self.layout)
        cat = self.load_catalog()
        projects = ws.creatures()
        keep = self.selected_species()
        self.species_tree.delete(*self.species_tree.get_children())
        if not cat:
            self.species_count.configure(text="")
            return
        needle = self.species_filter.get().strip().lower()
        shown = 0
        for sp in cat["species"]:
            code = (sp.get("codename") or {}).get("value") or ""
            if needle and needle not in f"{sp['key']} {sp['display_name']} {code}".lower():
                continue
            variants = sorted({p.get("variant", "") for p in sp["prefabs"]})
            project = projects.get(sp["suggested_id"])
            status = (steps.read_json(project) or {}).get("status", "?") if project else "—"
            self.species_tree.insert("", "end", iid=sp["key"],
                                     values=(sp["display_name"], code, ", ".join(variants), status))
            shown += 1
        s = cat["summary"]
        hidden = s.get("prefabs_filtered_out", 0)
        self.species_count.configure(text=f"{shown} of {len(cat['species'])} shown "
                                     f"({s.get('game_id_count', '?')} creature IDs)"
                                     + (f"; {hidden} rigged prefabs hidden by your group choice" if hidden else
                                        "; all rigged models are listed (effects, items, … too): "
                                        "use 'Choose creature groups…'" if s.get("name_groups") else ""))
        if keep and self.species_tree.exists(keep):
            self.species_tree.selection_set(keep)

    def current_creature_file(self) -> Path | None:
        sp = self.species_entry(self.selected_species())
        if sp is None:
            return None
        path = steps.Workspace(self.layout).creature_file(sp["suggested_id"])
        return path if path.is_file() else None

    def show_creature(self) -> None:
        sp = self.species_entry(self.selected_species())
        self.asset_tree.delete(*self.asset_tree.get_children())
        if sp is None:
            return
        info = [f"{sp['display_name']}  ({len(sp['assets'])} assets, {len(sp['animations'])} animations, "
                f"{len(sp['skeleton']['bones'])} bones)"]
        if sp.get("game_id"):
            forms = sp.get("game_forms") or []
            named = {p.get("game_form_index"): p.get("game_form_name") for p in sp["prefabs"]}
            info.append(f"Game ID {sp['game_id']}" + (f" (internal name {sp['internal_name']})"
                                                       if sp.get("internal_name") not in (None, sp["display_name"]) else "")
                        + ("  ·  forms in the files: " + ", ".join(f"{i}" + (f" = {named[i]}" if named.get(i) else "")
                                                                  for i in forms) if forms else ""))
        code = sp.get("codename")
        if code:
            info.append(f"Codename (guess from file names, ID {code['id']}): {code['value']}  ·  {code['evidence']}")
        info += [f"⚠ {w}" for w in sp["warnings"][:4]]
        if len(sp["warnings"]) > 4:
            info.append(f"⚠ … and {len(sp['warnings']) - 4} more warnings")
        path = self.current_creature_file()
        if path:
            from aniimo_pal.creature.model import load_creature
            from aniimo_pal.creature.variants import CONTEXTS, one_in, outcome_probabilities
            from aniimo_pal.creature.workflow import pending

            c = load_creature(path)
            todo = set(pending(c))
            info.append(f"Project status: {c['status']}  ·  decisions needed: {len(todo)}")
            done = [steps.STEP_LABELS[s] for s in steps.CREATURE_STEPS if steps.step_done(path.parent, s)]
            info.append("Steps done: " + (", ".join(done) if done else "none yet"))
            for ctx in CONTEXTS:
                p = outcome_probabilities(c["variants"], ctx)
                info.append(f"{ctx}: Shiny {one_in(p['any_shiny'])}, Prismana {one_in(p['any_prismana'])}, "
                            f"Prismana Shiny {one_in(p['prismana_shiny'])}")
            for i, a in enumerate(c["assets"]):
                mark = "★" if i in todo else ""
                self.asset_tree.insert("", "end", iid=str(i), values=(f"{mark}{i}", a["role"], a.get("form"),
                                                                      a.get("appearance"), a["association"],
                                                                      a.get("provenance", ""), a.get("name")))
            anims = [a for a in c["assets"] if a["role"] == "animation" and a["association"] != "rejected"]
            if anims:
                info.append(f"Animations ({len(anims)}): " + ", ".join(
                    a.get("name", "?") + (f" ({a['role_guess']})" if a.get("role_guess") else "") for a in anims))
        else:
            info.append("No project yet: click 'Create project'.")
        self._set_creature_info("\n".join(info))
        self.show_preview_image(path)

    def set_names(self) -> None:
        """Give the selected creature (game ID) and its numbered forms the names you know from the game."""
        sp = self.species_entry(self.selected_species())
        if sp is None or not sp.get("game_id"):
            messagebox.showinfo(TITLE, "Select a creature with a game ID number first.")
            return
        from aniimo_pal.catalog.names import apply_names, load_names, save_names

        ws = steps.Workspace(self.layout)
        gid = sp["game_id"]
        current = load_names(ws.names)
        win = tk.Toplevel(self.tk)
        win.title(f"Names for game ID {gid}")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, wraplength=460, justify="left", text=(
            f"Internal name: {sp.get('internal_name') or sp['display_name']}. Type the names you see in the game. "
            "Form numbers come from the files (e.g. 1018403 = form 03); what a number means differs per "
            "creature, so name each one (e.g. 'Highland Form' or 'Prismana'). Leave a field empty to clear it.")
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frm, text="Creature name").grid(row=1, column=0, sticky="w")
        name_var = tk.StringVar(value=current["names"].get(gid, ""))
        ttk.Entry(frm, textvariable=name_var, width=32).grid(row=1, column=1, sticky="we", pady=2)
        form_vars = {}
        for r, idx in enumerate(sp.get("game_forms") or [], start=2):
            ttk.Label(frm, text=f"Form {idx}").grid(row=r, column=0, sticky="w")
            form_vars[idx] = tk.StringVar(value=current["forms"].get(gid, {}).get(idx, ""))
            ttk.Entry(frm, textvariable=form_vars[idx], width=32).grid(row=r, column=1, sticky="we", pady=2)

        def save() -> None:
            try:
                data = save_names(ws.names, gid, name_var.get(), {i: v.get() for i, v in form_vars.items()})
                cat = self.load_catalog()
                if cat is not None:
                    apply_names(cat, data)
                    from aniimo_pal.manifest.io import write_json_atomic

                    write_json_atomic(cat, ws.catalog, indent=None)
                    self._catalog_cache = (ws.catalog.stat().st_mtime, cat)
            except Exception as exc:  # report, never crash the GUI
                messagebox.showerror(TITLE, str(exc), parent=win)
                return
            win.destroy()
            self.refresh_species()
            self.show_creature()

        bar = ttk.Frame(frm)
        bar.grid(row=99, column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Button(bar, text="Save", style="Accent.TButton", command=save).pack(side="left")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right")
        self.names_dialog = win

    def choose_groups(self) -> None:
        """Pick which name groups (p_parmon, m_monster, …) are creatures; rebuilds the creature list."""
        cat = self.load_catalog()
        groups = (cat or {}).get("summary", {}).get("name_groups")
        if not groups:
            messagebox.showinfo(TITLE, "Run '3. Find creatures' first (Scan & catalog tab).")
            return
        ws = steps.Workspace(self.layout)
        chosen_before = ws.catalog_rules.is_file()
        win = tk.Toplevel(self.tk)
        win.title("Choose creature groups")
        win.geometry("820x560")
        ttk.Label(win, wraplength=780, justify="left", padding=8, text=(
            "Every rigged model in the game is grouped by the start of its name. Select the groups that are "
            "creatures (Ctrl/Shift-click for several) and click 'Use selected groups'. Groups whose names "
            "carry an ID number are pre-selected as a suggestion the first time. Your choice is saved in the "
            "workspace and the creature list is rebuilt (this reads the object list again: about a minute "
            "for a large game).")).pack(fill="x")
        box = ttk.Frame(win, padding=(8, 0))
        box.pack(fill="both", expand=True)
        cols = ("prefix", "prefabs", "with_id", "examples")
        tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="extended")
        for col, w, label in (("prefix", 170, "Name group"), ("prefabs", 70, "Models"),
                              ("with_id", 80, "With ID"), ("examples", 460, "Examples")):
            tree.heading(col, text=label)
            tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        for g in groups:
            tree.insert("", "end", iid=g["prefix"],
                        values=(g["prefix"], g["prefabs"], g["with_id"], ", ".join(g["examples"])))
        filtered = chosen_before or not all(g["selected"] for g in groups)  # own choice or the Aniimo preset
        tree.selection_set([g["prefix"] for g in groups if (g["selected"] if filtered else g["with_id"] > 0)])
        bar = ttk.Frame(win, padding=8)
        bar.pack(fill="x")

        def apply(prefixes: list[str]) -> None:
            from aniimo_pal.catalog.rules import update_rules_file

            try:
                update_rules_file(ws.catalog_rules, include_name_prefixes=prefixes)
            except Exception as exc:  # report, never crash the GUI
                messagebox.showerror(TITLE, str(exc), parent=win)
                return
            win.destroy()
            self.run_scan(("catalog",))

        ttk.Button(bar, text="Use selected groups", style="Accent.TButton",
                   command=lambda: apply(list(tree.selection()))).pack(side="left")
        ttk.Button(bar, text="Show all groups", command=lambda: apply([])).pack(side="left", padx=6)
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right")
        self.group_dialog = win

    def create_project(self) -> None:
        key = self.selected_species()
        if not key:
            messagebox.showinfo(TITLE, "Select a creature first.")
            return
        if self.current_creature_file():
            messagebox.showinfo(TITLE, "This creature already has a project.")
            return
        r = self.runner()
        self.run_task("Creating project", lambda: self._check(r.run(r.cmd_create(key), self.log)),
                      lambda: (self.refresh_species(), self.species_tree.selection_set(key), self.show_creature()))

    @staticmethod
    def _check(code: int) -> None:
        if code != 0:
            raise steps.StepFailed("the step did not complete; the reason is shown in the Log tab")

    def decide(self, action: str) -> None:
        path = self.current_creature_file()
        if not path:
            return
        from aniimo_pal.creature.model import load_creature, save_creature
        from aniimo_pal.creature.workflow import set_associations

        c = load_creature(path)
        idx = [int(i) for i in self.asset_tree.selection()]
        try:
            if action == "confirm":
                set_associations(c, confirm=idx)
            elif action == "reject":
                set_associations(c, reject=idx)
            else:
                set_associations(c, confirm_all=True)
            save_creature(c, path)
        except Exception as exc:
            messagebox.showerror(TITLE, str(exc))
        self.show_creature()
        self.refresh_species()
        key = self.selected_species()
        if key:
            self.species_tree.selection_set(key)

    def creature_steps(self, which: tuple[str, ...]) -> None:
        path = self.current_creature_file()
        if not path:
            messagebox.showinfo(TITLE, "Create a project for this creature first.")
            return
        full = steps.with_prerequisites(path.parent, which)
        if full != which:
            extra = [steps.STEP_LABELS[s] for s in full if s not in which]
            if not messagebox.askyesno(TITLE, "This needs earlier steps that have not run yet: "
                                       f"{', '.join(extra)}.\n\nRun them first?"):
                return
            which = full
        include_proposed = False
        if any(s in which for s in ("extract", "primary", "project", "convert")):
            from aniimo_pal.creature.model import load_creature
            from aniimo_pal.creature.workflow import pending

            c = load_creature(path)
            open_items = len(pending(c))
            if c.get("status") == "draft" and open_items:
                if not messagebox.askyesno(TITLE, (
                        f"{open_items} asset links found automatically are still waiting for your review "
                        "(Assets tab, marked ★).\n\nYes: continue now and use the proposed links too "
                        "(they stay marked as proposed; you can review them later).\nNo: review them first.")):
                    self.creature_sub.select(1)
                    return
                include_proposed = True
        needs = {"primary": "assetripper", "project": "assetripper", "convert": "blender", "preview": "blender"}
        for step in which:
            tool = needs.get(step)
            if tool and not self.setup_ok(tool):
                messagebox.showwarning(TITLE, f"{tool.capitalize()} is not installed yet (Setup tab).")
                return
        r = self.runner()
        ip = include_proposed
        builders = {"extract": lambda: r.cmd_extract(path, ip),
                    "primary": lambda: r.cmd_assetripper(path, "primary", ip),
                    "project": lambda: r.cmd_assetripper(path, "project", ip),
                    "convert": lambda: r.cmd_convert(path, ip), "preview": lambda: r.cmd_preview(path)}

        def job():
            for step in which:
                self._check(r.run(builders[step](), self.log))
        key = self.selected_species()
        self.run_task("Processing creature", job,
                      lambda: (self.refresh_species(), key and self.species_tree.selection_set(key), self.show_creature()))

    def show_preview_image(self, creature_file: Path | None) -> None:
        png = creature_file.parent / "preview" / "preview.png" if creature_file else None
        if png and png.is_file():
            try:
                img = tk.PhotoImage(file=str(png))
                factor = max(1, img.width() // 620)
                self.preview_image = img.subsample(factor, factor) if factor > 1 else img
                self.preview_label.configure(image=self.preview_image, text="")
                return
            except tk.TclError:
                pass
        self.preview_image = None
        self.preview_label.configure(image="", text="No preview yet: convert the creature, then click 'Build 3D preview'."
                                     if creature_file else "")

    def open_preview(self) -> None:
        path = self.current_creature_file()
        blend = path.parent / "preview" / "preview.blend" if path else None
        if not blend or not blend.is_file():
            messagebox.showinfo(TITLE, "Build the 3D preview first (convert the creature, then 'Build 3D preview').")
            return
        blender = setup.tool_paths(self.layout, self.settings).get("blender")
        if not blender:
            messagebox.showwarning(TITLE, "Blender is not installed yet (Setup tab).")
            return
        self.log(f"Opening {blend} in Blender (press Space to play; markers show each animation) ...")
        subprocess.Popen([blender, str(blend)], env=self.layout.child_env(), cwd=str(self.layout.tmp))

    def open_creature_folder(self) -> None:
        path = self.current_creature_file()
        open_path(path.parent if path else self.layout.workspace)

    # ------------------------------------------------------------------ Mods tab
    def _build_mods_tab(self) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="Palworld mods")
        canvas = tk.Canvas(f, highlightthickness=0)
        scroll = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(inner, wraplength=980, justify="left", text=(
            "These Palworld mods are needed once a converted creature is imported into Palworld (the Unreal import "
            "and Palworld integration milestones). Extraction and conversion do not need them. They are installed "
            "into your Palworld game folder by you; the Studio does not change your Palworld installation. "
            "Information is from community documentation and has not yet been verified in game by this project.")
        ).pack(anchor="w", pady=(0, 10))
        for heading, items in (("Required in Palworld (players and testers)", mods.IN_GAME_MODS),
                               ("Required to build the mod (creators only)", mods.FOR_BUILDING_MODS)):
            ttk.Label(inner, text=heading, style="Title.TLabel").pack(anchor="w", pady=(8, 4))
            for m in items:
                box = ttk.LabelFrame(inner, text=m["name"], padding=8)
                box.pack(fill="x", pady=4)
                ttk.Label(box, text=f"Why: {m['why']}", wraplength=940, justify="left").pack(anchor="w")
                ttk.Label(box, text=f"Install: {m['install']}", wraplength=940, justify="left").pack(anchor="w")
                if m["notes"]:
                    ttk.Label(box, text=f"Note: {m['notes']}", wraplength=940, justify="left",
                              foreground="#7a4b00").pack(anchor="w")
                if m["url"]:
                    ttk.Button(box, text="Open download page", command=lambda u=m["url"]: webbrowser.open(u)
                               ).pack(anchor="w", pady=(4, 0))
        return f

    # ------------------------------------------------------------------ Log tab
    def _build_log_tab(self) -> ttk.Frame:
        f = ttk.Frame(self.nb, padding=12)
        self.nb.add(f, text="Log")
        self.log_text = ScrolledText(f, wrap="none", state="disabled", font=("TkFixedFont", 9))
        self.log_text.pack(fill="both", expand=True)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Open logs folder", command=lambda: open_path(self.layout.logs)).pack(side="right")
        return f

    def on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(TITLE, "A task is still running. Stop it and quit?"):
                return
            if self.current_runner:
                self.current_runner.stop()
        self.log_file.close()
        self.tk.destroy()


def main(root_folder: str | Path) -> int:
    layout = StudioLayout.for_root(root_folder)
    tk_root = tk.Tk()
    StudioApp(tk_root, layout)
    tk_root.mainloop()
    return 0
