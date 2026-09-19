#!/bin/bash
# Build, test and (with --publish) publish one release of Dispatch More.
#
#   fork/patcher/release.sh v99              # build and test only
#   fork/patcher/release.sh v99 --publish    # and make the GitHub release (needs `gh auth login`)
set -euo pipefail
RELEASE="${1:?usage: release.sh <release, e.g. v99> [--publish]}"
REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"
[ -z "$(git status --porcelain)" ] || { echo "Commit first: a release is built from a commit."; exit 1; }

BASE="$(cat fork/patcher/BASE)"
REPOSITORY="$(gh repo view --json url --jq .url 2>/dev/null || git remote get-url origin 2>/dev/null || true)"
BASE="$BASE" REPOSITORY="$REPOSITORY" bash fork/patcher/build-overlay.sh "$RELEASE"
DISPATCHARR="$(git show "$BASE:version.py" | sed -n "s/^__version__ = '\(.*\)'.*/\1/p")"
ARCHIVE="fork/patcher/out/dispatch-more-$RELEASE-dispatcharr-$DISPATCHARR.tar.gz"
BASE="$BASE" bash fork/patcher/test-patcher.sh "$ARCHIVE"

if [ "${2:-}" = "--publish" ]; then
  gh release create "$RELEASE" "$ARCHIVE" \
    --target "$(git rev-parse HEAD)" \
    --title "Dispatch More $RELEASE (for Dispatcharr $DISPATCHARR)" \
    --notes "An unofficial, modified build of Dispatcharr $DISPATCHARR — see the README for installing, uninstalling, and where to report problems (not the official Dispatcharr GitHub or Discord).

Changes since the last release: see the commits on feature/probation-slots."
  echo "Published $RELEASE."
fi
