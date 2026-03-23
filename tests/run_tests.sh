#!/bin/bash
# Test runner for mkimage
#
# Usage:
#   ./tests/run_tests.sh                # unit + integration (no root, no remote)
#   ./tests/run_tests.sh --with-root    # include root-requiring tests
#   ./tests/run_tests.sh --windows      # include Windows VM tests
#   ./tests/run_tests.sh --macos        # include macOS tests
#   ./tests/run_tests.sh --all          # everything
#   ./tests/run_tests.sh -- -k "test_build"  # pass extra args to pytest

set -e

cd "$(dirname "$0")/.."

MARKERS="not needs_root and not windows and not macos"

EXTRA_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --with-root) MARKERS="not windows and not macos" ;;
        --windows)   MARKERS="not needs_root and not macos" ;;
        --macos)     MARKERS="not needs_root and not windows" ;;
        --all)       MARKERS="" ;;
        --)          shift; EXTRA_ARGS=("$@"); break ;;
        *)           EXTRA_ARGS+=("$arg") ;;
    esac
done

if [[ -n "$MARKERS" ]]; then
    exec python3 -m pytest tests/ -m "$MARKERS" -v "${EXTRA_ARGS[@]}"
else
    exec python3 -m pytest tests/ -v "${EXTRA_ARGS[@]}"
fi
