"""
Put a release of the modified build over Dispatcharr, or take it off again. Called by
install.sh and uninstall.sh, which find Dispatcharr's own Python, work out how it is installed,
and restart it; this does the files.

Installing:
  - refuses a Dispatcharr it was not built for (the manifest says which), and leaves it as it is;
  - refuses when a file it would replace or remove is not the stock one -- something else has
    changed it, and overwriting that would lose it -- unless told to go ahead anyway;
  - keeps every original in the state folder before touching anything, and the stock
    frontend whole;
  - an older release already on is taken off first, so an upgrade starts from stock;
  - leaves a record in the Dispatcharr folder for the page (core/modified_build.py): which
    release, how installed, and where a request to uninstall is to be left.

Uninstalling puts back exactly what was there, removes what was added, and forgets the record.

Exit codes: 0 done, 10 nothing to do, 3 wrong Dispatcharr version, 4 files not stock.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

RECORD = ".fork-install.json"
NOTHING_TO_DO, WRONG_VERSION, NOT_STOCK = 10, 3, 4


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dispatcharr_version(app):
    try:
        text = open(os.path.join(app, "version.py")).read()
    except OSError:
        return None
    found = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
    return found.group(1) if found else None


def owner_of(path):
    info = os.stat(path)
    return info.st_uid, info.st_gid


def give_to(path, owner):
    """Written files belong to whoever owns the installation, as they did before."""
    if os.geteuid() != 0:
        return
    for root, dirs, files in os.walk(path) if os.path.isdir(path) else [(os.path.dirname(path), [], [os.path.basename(path)])]:
        for name in dirs + files:
            try:
                os.lchown(os.path.join(root, name), *owner)
            except OSError:
                pass
    try:
        os.lchown(path, *owner)
    except OSError:
        pass


def copy_file(source, target, owner):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)
    give_to(target, owner)


def load_record(app, state):
    for path in (os.path.join(state, "record.json"), os.path.join(app, RECORD)):
        try:
            return json.load(open(path))
        except (OSError, ValueError):
            continue
    return None


def install(app, state, package, layout, force=False):
    manifest = json.load(open(os.path.join(package, "manifest.json")))
    name, release = manifest["name"], manifest["release"]
    version = dispatcharr_version(app)
    if version is None:
        print(f"No Dispatcharr found at {app} (no version.py).")
        return WRONG_VERSION
    if version != manifest["for_dispatcharr"]:
        print(
            f"{name} {release} is for Dispatcharr {manifest['for_dispatcharr']}, and this is "
            f"{version}. Nothing was changed. A release for {version} is needed."
        )
        return WRONG_VERSION

    here = None
    try:
        here = json.load(open(os.path.join(app, RECORD)))
    except (OSError, ValueError):
        pass
    if here and here.get("release") == release and not force:
        print(f"{name} {release} is already installed.")
        return NOTHING_TO_DO
    if here:
        print(f"{name} {here.get('release')} is installed: taking it off first.")
        uninstall(app, state, quiet=True)

    # Before anything changes: is every file to be replaced or removed the stock one?
    problems = []
    for entry in manifest["files"]:
        target = os.path.join(app, entry["path"])
        if entry["action"] in ("replace", "remove"):
            if not os.path.exists(target):
                problems.append(f"missing: {entry['path']}")
            elif sha256(target) != entry["stock_sha256"]:
                problems.append(f"not stock: {entry['path']}")
        elif os.path.exists(target) and sha256(target) != entry["sha256"]:
            problems.append(f"already there, and different: {entry['path']}")
    if problems and not force:
        print(f"Not installed: {len(problems)} file(s) are not as stock Dispatcharr {version} has them.")
        for problem in problems[:30]:
            print(f"  {problem}")
        print("Something else has changed them. Put stock back first, or run again with --force to overwrite.")
        return NOT_STOCK

    owner = owner_of(app)
    backup = os.path.join(state, "backup")
    shutil.rmtree(backup, ignore_errors=True)
    os.makedirs(os.path.join(backup, "files"), exist_ok=True)

    # The originals, then the release over them
    for entry in manifest["files"]:
        target = os.path.join(app, entry["path"])
        if entry["action"] in ("replace", "remove") and os.path.exists(target):
            os.makedirs(os.path.dirname(os.path.join(backup, "files", entry["path"])), exist_ok=True)
            shutil.copy2(target, os.path.join(backup, "files", entry["path"]))
    dist = os.path.join(app, "frontend", "dist")
    if os.path.isdir(dist):
        shutil.copytree(dist, os.path.join(backup, "frontend-dist"), symlinks=True)
    else:
        # There was no built frontend: uninstalling takes ours away rather than keeping it
        open(os.path.join(backup, "no-frontend-dist"), "w").close()

    for entry in manifest["files"]:
        target = os.path.join(app, entry["path"])
        if entry["action"] == "remove":
            if os.path.exists(target):
                os.remove(target)
        else:
            copy_file(os.path.join(package, "files", entry["path"]), target, owner)
    shutil.rmtree(dist, ignore_errors=True)
    shutil.copytree(os.path.join(package, "frontend-dist"), dist, symlinks=True)
    give_to(dist, owner)

    # What uninstalling needs, kept where the page's request and the watcher can find it
    for script in ("install.sh", "uninstall.sh", "docker-entrypoint.sh", "patch.py", "manifest.json"):
        source, kept = os.path.join(package, script), os.path.join(state, script)
        # In Docker the release is unpacked in the state folder itself: nothing to copy then
        if os.path.exists(source) and os.path.abspath(source) != os.path.abspath(kept):
            shutil.copy2(source, kept)
    requests = os.path.join(state, "requests")
    os.makedirs(requests, exist_ok=True)
    # The web app runs as the installation's owner: it may leave its request here
    give_to(requests, owner)
    record = {
        "name": name,
        "release": release,
        "for_dispatcharr": version,
        "layout": layout,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository": manifest.get("repository", ""),
        "built_from": manifest.get("built_from", ""),
        "state": state,
        "uninstall_request": os.path.join(requests, "uninstall"),
    }
    json.dump(record, open(os.path.join(state, "record.json"), "w"), indent=1)
    json.dump(record, open(os.path.join(app, RECORD), "w"), indent=1)
    give_to(os.path.join(app, RECORD), owner)
    counts = {a: sum(1 for f in manifest["files"] if f["action"] == a) for a in ("replace", "add", "remove")}
    print(
        f"{name} {release} installed over Dispatcharr {version}: {counts['replace']} files replaced, "
        f"{counts['add']} added, {counts['remove']} removed, and the frontend. Originals kept in {backup}."
    )
    return 0


def uninstall(app, state, quiet=False):
    record = load_record(app, state)
    manifest_path = os.path.join(state, "manifest.json")
    if not record or not os.path.exists(manifest_path):
        if not quiet:
            print("Nothing to uninstall: no record of an installation here.")
        return NOTHING_TO_DO
    manifest = json.load(open(manifest_path))
    backup = os.path.join(state, "backup")
    owner = owner_of(app)

    for entry in manifest["files"]:
        target = os.path.join(app, entry["path"])
        original = os.path.join(backup, "files", entry["path"])
        if entry["action"] in ("replace", "remove"):
            if os.path.exists(original):
                copy_file(original, target, owner)
        elif os.path.exists(target):
            os.remove(target)
            # Folders the release made, left empty now, go too
            folder = os.path.dirname(target)
            while folder.startswith(app + os.sep) and folder != app and not os.listdir(folder):
                os.rmdir(folder)
                folder = os.path.dirname(folder)
    dist = os.path.join(app, "frontend", "dist")
    if os.path.isdir(os.path.join(backup, "frontend-dist")):
        shutil.rmtree(dist, ignore_errors=True)
        shutil.copytree(os.path.join(backup, "frontend-dist"), dist, symlinks=True)
        give_to(dist, owner)
    elif os.path.exists(os.path.join(backup, "no-frontend-dist")):
        shutil.rmtree(dist, ignore_errors=True)

    for path in (os.path.join(app, RECORD), os.path.join(state, "record.json"),
                 os.path.join(state, "requests", "uninstall")):
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(backup, ignore_errors=True)
    if not quiet:
        print(f"{record.get('name', 'The modified build')} {record.get('release', '')} uninstalled: "
              f"stock Dispatcharr {dispatcharr_version(app)} is back.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--app", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--package", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--layout", default="systemd")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    app, state = os.path.abspath(args.app), os.path.abspath(args.state)
    os.makedirs(state, exist_ok=True)
    if args.action == "install":
        return install(app, state, os.path.abspath(args.package), args.layout, args.force)
    return uninstall(app, state)


if __name__ == "__main__":
    sys.exit(main())
