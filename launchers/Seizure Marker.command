#!/bin/bash
# Double-click launcher (macOS). Run launchers/setup_mac.command once first.
cd "$(dirname "$0")/.."
if [ ! -x ".venv/bin/python" ]; then
  echo "Not set up yet: double-click launchers/setup_mac.command first."
  read -r -p "Press Return to close." _
  exit 1
fi
unset MPLBACKEND
exec .venv/bin/python -m seizure_marker "$@"
