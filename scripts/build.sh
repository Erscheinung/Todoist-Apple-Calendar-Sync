#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$root_dir/.build/package"
rm -rf "$build_dir"
mkdir -p "$build_dir"
# Build Linux/CPython 3.12 wheels even when the deploy command is run on macOS.
python3 -m pip install --disable-pip-version-check \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --target "$build_dir" -r "$root_dir/requirements.txt"
cp "$root_dir/src/handler.py" "$root_dir/src/google_calendar.py" "$root_dir/src/week_digest.py" "$root_dir/src/ics_calendar.py" "$build_dir/"
(
  cd "$build_dir"
  rm -f ../lambda.zip
  zip -qr ../lambda.zip .
)
