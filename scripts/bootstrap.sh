#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "$0")/.." && pwd -P)
python_bin=${PYTHON_BIN:-}

if [ -z "$python_bin" ]; then
  for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
      "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
      python_bin=$(command -v "$candidate")
      break
    fi
  done
fi

if [ -z "$python_bin" ] || ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Python 3.12 or newer is required; selected interpreter: $python_bin" >&2
  exit 2
fi

export COPYFILE_DISABLE=1
export PYTHONDONTWRITEBYTECODE=1

if [ ! -f "$repo_root/arxiv-monitor-config-phd.json" ]; then
  cp "$repo_root/config/arxiv-monitor-config-phd.example.json" "$repo_root/arxiv-monitor-config-phd.json"
  echo "created local config: $repo_root/arxiv-monitor-config-phd.json"
fi

for required_bin in pdftotext pdftoppm; do
  if ! command -v "$required_bin" >/dev/null 2>&1; then
    echo "missing required system executable: $required_bin" >&2
    exit 2
  fi
done

"$python_bin" -m venv --clear "$repo_root/.venv"
find "$repo_root/.venv" -type f -name '._*' -delete
"$repo_root/.venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --only-binary=:all: \
  --require-hashes \
  -r "$repo_root/requirements.lock"
find "$repo_root/.venv" -type f -name '._*' -delete
"$repo_root/.venv/bin/python" -m pip check
"$repo_root/.venv/bin/python" "$repo_root/scripts/verify_bundle.py"
"$repo_root/.venv/bin/python" "$repo_root/scripts/security_audit.py"
(
  cd "$repo_root"
  "$repo_root/.venv/bin/python" -m unittest discover -s tests -v
)
