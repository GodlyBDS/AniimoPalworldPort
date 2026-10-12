#!/usr/bin/env bash
# Aniimo -> Palworld Studio launcher (Linux/macOS).
# Everything is installed and created inside this folder (studio-data/).
# Use --portable-python to force downloading a portable Python into the folder.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$DIR/studio-data"
PORTABLE="$DATA/python/bin/python3"
PY=""

if [[ -x "$PORTABLE" ]]; then
  PY="$PORTABLE"
elif [[ " $* " != *" --portable-python "* ]]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
fi

if [[ -z "$PY" ]]; then
  echo "Downloading a portable Python (with tkinter) into $DATA ..."
  mkdir -p "$DATA/downloads"
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) triple="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64) triple="aarch64-unknown-linux-gnu" ;;
    Darwin-arm64) triple="aarch64-apple-darwin" ;;
    Darwin-x86_64) triple="x86_64-apple-darwin" ;;
    *) echo "Unsupported platform; install Python 3.11+ with tkinter and retry."; exit 1 ;;
  esac
  url="$(curl -fsSL -H 'User-Agent: AniimoPalworldStudio' \
      https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
    | grep -o "https://[^\"]*cpython-3\.12\.[0-9]*+[0-9]*-${triple}-install_only\.tar\.gz" | head -n1)"
  if [[ -z "$url" ]]; then echo "Could not find a portable Python build; install Python 3.11+ with tkinter."; exit 1; fi
  curl -fL "$url" -o "$DATA/downloads/python.tar.gz"
  tar -xzf "$DATA/downloads/python.tar.gz" -C "$DATA"
  rm -f "$DATA/downloads/python.tar.gz"
  PY="$PORTABLE"
fi

args=()
for a in "$@"; do [[ "$a" == "--portable-python" ]] || args+=("$a"); done
exec "$PY" "$DIR/start_studio.py" "${args[@]+"${args[@]}"}"
