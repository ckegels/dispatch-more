#!/bin/bash
# Build one release of the patcher: the overlay for the Dispatcharr version the fork is based
# on, and the scripts that put it on and take it off.
#
#   fork/patcher/build-overlay.sh v99            # -> fork/patcher/out/dispatch-more-v99-dispatcharr-0.31.0.tar.gz
#
# The overlay holds every backend file the fork changes, adds or removes (not tests, not the
# fork's own notes), the frontend already built -- so the server needs neither git nor npm --
# and a manifest with the checksum of each stock file it replaces. The installer checks those
# checksums before changing anything, and refuses a Dispatcharr it was not built for.
set -euo pipefail

RELEASE="${1:?usage: build-overlay.sh <release, e.g. v99>}"
NAME="${NAME:-Dispatch More}"
SLUG="${SLUG:-dispatch-more}"
BASE="${BASE:-bcbb68c4}"
REPOSITORY="${REPOSITORY:-}"
REPO="$(git rev-parse --show-toplevel)"
OUT="${OUT:-$REPO/fork/patcher/out}"
DISPATCHARR="$(git -C "$REPO" show "$BASE:version.py" | sed -n "s/^__version__ = '\(.*\)'.*/\1/p")"
[ -n "$DISPATCHARR" ] || { echo "Could not read the Dispatcharr version at $BASE"; exit 1; }

WORK="$(mktemp -d)"
cleanup() { git -C "$REPO" worktree remove --force "$WORK/src" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

# A clean copy of the commit being released, whatever is lying about in the working tree
git -C "$REPO" worktree add --detach "$WORK/src" HEAD >/dev/null
SRC="$WORK/src"
COMMIT="$(git -C "$SRC" rev-parse --short HEAD)"

# The release says what it is, in the page's version and About
sed -i "s/^__build__ = .*/__build__ = \"$NAME $RELEASE\"/" "$SRC/version.py"

echo "Building the frontend..."
if [ -d "$REPO/frontend/node_modules" ]; then
  ln -s "$REPO/frontend/node_modules" "$SRC/frontend/node_modules"
else
  (cd "$SRC/frontend" && npm ci --no-audit --no-fund)
fi
(cd "$SRC/frontend" && npm run build >/dev/null)

PACKAGE="$WORK/$SLUG"
mkdir -p "$PACKAGE/files"
python3 - "$REPO" "$SRC" "$PACKAGE" "$BASE" <<'PY'
import hashlib, json, os, shutil, subprocess, sys
repo, src, package, base = sys.argv[1:5]

def sha(data):
    return hashlib.sha256(data).hexdigest()

def left_out(path):
    # Not for the server: tests, the fork's own notes and tools, and the frontend's sources
    # (the built frontend is shipped whole instead)
    parts = path.split("/")
    return (
        "tests" in parts or "__tests__" in parts or path.startswith(("fork/", ".github/"))
        or path in ("CLAUDE.md", "README.md") or path.startswith("frontend/")
    )

changes = subprocess.run(
    ["git", "-C", src, "diff", "--name-status", "--no-renames", base, "HEAD"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()
files = []
for line in changes:
    status, path = line.split("\t", 1)
    if left_out(path):
        continue
    entry = {"path": path}
    if status in ("M", "D"):
        stock = subprocess.run(["git", "-C", src, "show", f"{base}:{path}"], capture_output=True, check=True).stdout
        entry["stock_sha256"] = sha(stock)
    if status in ("M", "A"):
        with open(os.path.join(src, path), "rb") as handle:
            data = handle.read()
        entry["sha256"] = sha(data)
        target = os.path.join(package, "files", path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(src, path), target)
    entry["action"] = {"M": "replace", "A": "add", "D": "remove"}[status]
    files.append(entry)

# version.py is stamped for the release: always part of it, as a replaced file
if not any(f["path"] == "version.py" for f in files):
    stock = subprocess.run(["git", "-C", src, "show", f"{base}:version.py"], capture_output=True, check=True).stdout
    with open(os.path.join(src, "version.py"), "rb") as handle:
        data = handle.read()
    shutil.copy2(os.path.join(src, "version.py"), os.path.join(package, "files", "version.py"))
    files.append({"path": "version.py", "action": "replace", "stock_sha256": sha(stock), "sha256": sha(data)})

shutil.copytree(os.path.join(src, "frontend", "dist"), os.path.join(package, "frontend-dist"))
json.dump({"files": files}, open(os.path.join(package, "files.json"), "w"), indent=1)
words = {"replace": "replaced", "add": "added", "remove": "removed"}
print(f"{len(files)} files: " + ", ".join(f"{sum(1 for f in files if f['action'] == a)} {w}" for a, w in words.items()))
PY

python3 - "$PACKAGE" "$NAME" "$SLUG" "$RELEASE" "$DISPATCHARR" "$COMMIT" "$REPOSITORY" <<'PY'
import json, os, sys, datetime
package, name, slug, release, dispatcharr, commit, repository = sys.argv[1:8]
files = json.load(open(os.path.join(package, "files.json")))["files"]
os.remove(os.path.join(package, "files.json"))
json.dump({
    "name": name, "slug": slug, "release": release, "for_dispatcharr": dispatcharr,
    "built_from": commit, "repository": repository,
    "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "files": files,
}, open(os.path.join(package, "manifest.json"), "w"), indent=1)
PY

cp "$REPO/fork/patcher/install.sh" "$REPO/fork/patcher/uninstall.sh" "$REPO/fork/patcher/docker-entrypoint.sh" "$REPO/fork/patcher/patch.py" "$PACKAGE/"
chmod +x "$PACKAGE"/*.sh

mkdir -p "$OUT"
ARCHIVE="$OUT/$SLUG-$RELEASE-dispatcharr-$DISPATCHARR.tar.gz"
tar -czf "$ARCHIVE" -C "$WORK" "$SLUG"
echo "Built $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1)) from $COMMIT, for Dispatcharr $DISPATCHARR"
