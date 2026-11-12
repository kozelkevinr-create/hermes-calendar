#!/usr/bin/env bash
# Validate this plugin on a remote Hermes host without installing it.
#
# Hermes loads a user plugin by importing ``<plugin dir>/__init__.py`` as a
# package, so the capability probe fails for absolute internal imports even
# when every local test passes. CI cannot catch that class of bug (Hermes is
# not installed there); this script reproduces it against a real installation.
#
# The worktree is tarred over ssh into a fresh temporary directory, validated
# with ``hermes plugins validate --json``, and the temporary directory is always
# removed. Nothing is copied into the host's ``~/.hermes`` plugin directories.
#
# Usage:
#   scripts/validate_remote.sh
#
# Environment:
#   HERMES_VALIDATE_HOST  ssh host alias (default: docker-host)
#   HERMES_VALIDATE_BIN   remote validator path (default: ~/.local/bin/hermes)
#
# Exit status is non-zero when the validator reports ``"ok": false`` or cannot
# run. The raw JSON stays on stdout so it can be pasted into a report.

set -euo pipefail

HOST="${HERMES_VALIDATE_HOST:-docker-host}"
REMOTE_BIN="${HERMES_VALIDATE_BIN:-~/.local/bin/hermes}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_DIR=""

cleanup() {
    if [[ -n "${REMOTE_DIR}" ]]; then
        ssh "${HOST}" "rm -rf -- '${REMOTE_DIR}'" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

echo "validate-remote: host=${HOST} repo=${REPO_ROOT}" >&2

if ! ssh "${HOST}" "test -x ${REMOTE_BIN}"; then
    echo "validate-remote: ${REMOTE_BIN} is not executable on ${HOST}" >&2
    exit 1
fi

REMOTE_DIR="$(ssh "${HOST}" 'mktemp -d "${TMPDIR:-/tmp}/hermes-calendar-validate.XXXXXX"')"
echo "validate-remote: staging in ${HOST}:${REMOTE_DIR}" >&2

tar -czf - \
    --exclude=.git \
    --exclude=.github \
    --exclude=docs \
    --exclude=tests \
    --exclude=.venv \
    --exclude=./build \
    --exclude=./dist \
    --exclude='*.egg-info' \
    --exclude=__pycache__ \
    --exclude='*.pyc' \
    --exclude=.pytest_cache \
    --exclude=.mypy_cache \
    --exclude=.ruff_cache \
    -C "${REPO_ROOT}" . |
    ssh "${HOST}" "tar -xzf - -C '${REMOTE_DIR}'"

set +e
JSON="$(ssh "${HOST}" "${REMOTE_BIN} plugins validate --json '${REMOTE_DIR}'")"
VALIDATOR_STATUS=$?
set -e

printf '%s\n' "${JSON}"

if [[ -z "${JSON}" ]]; then
    echo "validate-remote: validator produced no output (exit ${VALIDATOR_STATUS})" >&2
    exit 1
fi

if ! printf '%s' "${JSON}" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as exc:
    print(f"validate-remote: validator output is not JSON: {exc}", file=sys.stderr)
    raise SystemExit(1)

if payload.get("ok") is not True:
    failures = [
        check.get("detail") or check.get("name", "unknown check")
        for check in payload.get("checks", [])
        if check.get("ok") is not True
    ]
    print(
        "validate-remote: hermes plugins validate reported ok=false"
        + (": " + "; ".join(failures) if failures else ""),
        file=sys.stderr,
    )
    raise SystemExit(1)
'; then
    exit 1
fi

echo "validate-remote: ok" >&2
