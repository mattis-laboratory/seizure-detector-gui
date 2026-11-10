#!/bin/bash
# One-time setup on macOS: creates a private Python environment (.venv) next to
# this folder and installs the app into it. Double-click this file in Finder.
# If macOS refuses to open it ("cannot be opened because it is from an
# unidentified developer"), right-click -> Open, once.
set -e
cd "$(dirname "$0")/.."
echo "Seizure Marker setup in: $(pwd)"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then PY="$cand"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.10 or newer was not found."
  echo "Install it from https://www.python.org/downloads/macos/ (or: brew install python), then run this again."
  read -r -p "Press Return to close." _
  exit 1
fi
echo "Using $($PY --version) at $(command -v "$PY")"

"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .

# A git clone has no quarantine flag; a downloaded zip does. Clear it from our own launchers.
xattr -dr com.apple.quarantine "Seizure Marker.app" launchers 2>/dev/null || true
chmod +x "Seizure Marker.app/Contents/MacOS/launch" launchers/*.command 2>/dev/null || true

echo
echo "Done. Start the app with 'Seizure Marker.app' or launchers/'Seizure Marker.command'."
read -r -p "Press Return to close." _
