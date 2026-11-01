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
# The fork, from the "origin" remote -- never the official repository, which is "upstream" and
# has no push address. Every gh command below names it outright rather than letting gh guess.
ORIGIN="$(git remote get-url origin 2>/dev/null || true)"
SLUG_REPO="$(echo "$ORIGIN" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
case "$SLUG_REPO" in
  Dispatcharr/*|"") [ "${2:-}" = "--publish" ] && { echo "The origin remote is not the fork ($ORIGIN): not publishing."; exit 1; } ;;
esac
REPOSITORY="${ORIGIN:+https://github.com/$SLUG_REPO}"
BASE="$BASE" REPOSITORY="$REPOSITORY" bash fork/patcher/build-overlay.sh "$RELEASE"
DISPATCHARR="$(git show "$BASE:version.py" | sed -n "s/^__version__ = '\(.*\)'.*/\1/p")"
ARCHIVE="fork/patcher/out/dispatch-more-$RELEASE-dispatcharr-$DISPATCHARR.tar.gz"
# Updating across Dispatcharr versions is tested too, whenever there is something to test it
# with: a newer Dispatcharr than this release's (its tag, from `git fetch upstream --tags`), and
# the newest release built here for an older one (fork/patcher/out)
UPDATED_TO="$(git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//' \
  | { cat; echo "$DISPATCHARR"; } | sort -V | sed -n "/^$DISPATCHARR\$/,\$p" | grep -vx "$DISPATCHARR" | tail -1 || true)"
PREVIOUS_ARCHIVE="$(ls fork/patcher/out/dispatch-more-v*-dispatcharr-*.tar.gz 2>/dev/null \
  | grep -v -- "-dispatcharr-$DISPATCHARR.tar.gz" | sort -V | tail -1 || true)"
BASE="$BASE" UPDATED_TO="$UPDATED_TO" PREVIOUS_ARCHIVE="$PREVIOUS_ARCHIVE" bash fork/patcher/test-patcher.sh "$ARCHIVE"

if [ "${2:-}" = "--publish" ]; then
  # The one-command installer rides along as a download of its own, so
  # releases/latest/download/quick-install.sh always gives the newest
  cp fork/patcher/quick-install.sh fork/patcher/out/quick-install.sh
  gh release create "$RELEASE" "$ARCHIVE" fork/patcher/out/quick-install.sh --repo "$SLUG_REPO" \
    --target "$(git rev-parse HEAD)" \
    --title "Dispatch More $RELEASE (for Dispatcharr $DISPATCHARR)" \
    --notes "An unofficial, modified build of Dispatcharr $DISPATCHARR — see the README for installing, uninstalling, and where to report problems (not the official Dispatcharr GitHub or Discord).

Changes since the last release: see the commits on feature/probation-slots."
  echo "Published $RELEASE."
fi
