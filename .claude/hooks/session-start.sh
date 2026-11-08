#!/bin/bash
# SessionStart hook: installs Python dependencies so `pytest` and `ruff check`
# work immediately in a Claude Code on the web session.
#
# Runs synchronously, so the session starts with the environment already usable
# and can never race an in-flight install.
set -euo pipefail

# Developer machines manage their own virtualenvs; only provision the ephemeral
# remote container.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-"$(dirname "$0")/../.."}"

echo "[session-start] Python $(python3 --version 2>&1 | cut -d' ' -f2) — installing dependencies"

# requirements.txt is a superset of requirements-engine.txt (same pins, plus
# Streamlit), so this one install covers both the dashboard and the headless
# engine. requirements-dev.txt adds pytest + ruff.
# `install` rather than a locked sync: the container image is cached after this
# hook completes, so repeat sessions reuse the result.
python3 -m pip install --quiet --disable-pip-version-check --root-user-action=ignore \
  --upgrade-strategy only-if-needed \
  -r requirements.txt \
  -r requirements-dev.txt

# engine.py and scripts/run_scan.py are imported from the repo root rather than
# installed as a package, so the test suite needs the root on the import path.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONPATH=\"${CLAUDE_PROJECT_DIR:-$PWD}\"" >> "$CLAUDE_ENV_FILE"
fi

echo "[session-start] Ready — 'pytest -q' and 'ruff check .' are available."
