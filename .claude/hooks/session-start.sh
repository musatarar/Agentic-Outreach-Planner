#!/bin/bash
# SessionStart hook for Claude Code on the web: reproduce the CLAUDE.md clean-clone
# setup (3.12 venv + dev deps + .env + frontend deps) before the session starts.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"
ROOT="$(pwd)"

# The container's default python3 can predate the repo's 3.12 floor.
if ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info[:2] < (3, 12))' 2>/dev/null; then
  rm -rf .venv
  python3.12 -m venv .venv
fi
.venv/bin/pip install -q -r requirements-dev.txt

# settings.py refuses to boot without DJANGO_SECRET_KEY; the example ships a dev value.
[ -f .env ] || cp .env.example .env

npm install --prefix frontend --no-audit --no-fund

# Venv binaries (python, ruff, mypy, coverage) resolve without activation.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$ROOT/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
