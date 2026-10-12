"""Stand-in for AssetRipper.GUI.Free's headless HTTP interface (tests only).

Mirrors the observed behaviour of the real program (see
src/aniimo_pal/adapters/assetripper.py): POST commands require
form-urlencoded bodies (415 otherwise), answer 302 immediately, and report
completion via stdout marker lines a moment later.

Exports write ``Assets/Loaded/<input basename>.glb`` for each loaded file plus
any relative paths listed in the FAKE_AR_FILES environment variable.
Set FAKE_AR_FAIL_STARTUP=1 to exit immediately with an error.
"""

import argparse
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

LOADED: list[str] = []


def emit(line: str, delay: float = 0.3) -> None:
    def later():
        time.sleep(delay)
        print(line, flush=True)
    threading.Thread(target=later, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/" else 404)
        self.end_headers()
        self.wfile.write(b"fake assetripper")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        if self.path != "/Reset" and self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
            self.send_response(415)
            self.end_headers()
            return
        paths = parse_qs(body).get("Path", [])
        if self.path in ("/LoadFile", "/LoadFolder"):
            LOADED.extend(paths)
            emit("Processing : Finished processing assets")
        elif self.path in ("/Export/PrimaryContent", "/Export/UnityProject"):
            out = Path(paths[0])
            for src in LOADED:
                target = out / "Assets" / "Loaded" / (Path(src).stem + ".glb")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"glTF")
            for rel in filter(None, os.environ.get("FAKE_AR_FILES", "").split(",")):
                target = out / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fake")
            emit("Export : Finished exporting primary content." if self.path.endswith("PrimaryContent")
                 else "Export : Finished post-export")
        elif self.path == "/Reset":
            LOADED.clear()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()


def main() -> int:
    if os.environ.get("FAKE_AR_FAIL_STARTUP"):
        print("fatal: simulated startup failure", flush=True)
        return 3
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log-path")
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Now listening on: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
