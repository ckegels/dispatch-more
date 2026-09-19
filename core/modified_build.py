"""
What the page knows about this being a modified build, and the way back to stock.

The installer (fork/patcher/install.sh) leaves a record in the Dispatcharr folder: which
release, for which Dispatcharr, how it was installed, where the originals were kept, and where
a request to uninstall is to be left. Uninstalling needs root -- the files belong to the
installation and the services are systemd's -- which the web app does not have. So the page
only leaves the request: on Linux a root-owned watcher the installer set up (a systemd path
unit) sees it and runs the uninstaller; in Docker the next start of the container does.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

RECORD_NAME = ".fork-install.json"


def record_path():
    return Path(settings.BASE_DIR) / RECORD_NAME


def status():
    import version

    record = {}
    try:
        record = json.loads(record_path().read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read the install record: {e}")
    request = Path(record["uninstall_request"]) if record.get("uninstall_request") else None
    return {
        "build": getattr(version, "__build__", None),
        "dispatcharr_version": version.__version__,
        "installed": bool(record),
        "record": {
            key: record.get(key)
            for key in ("release", "for_dispatcharr", "layout", "installed_at", "repository")
        },
        "uninstall_requested": bool(request and request.exists()),
    }


def request_uninstall(user=""):
    """Leave the request for the uninstaller. Raises ValueError when there is no way to."""
    record = json.loads(record_path().read_text()) if record_path().exists() else {}
    target = record.get("uninstall_request")
    if not target:
        raise ValueError(
            "This build was not put here by the installer, so the page cannot take it out. "
            "Run the uninstall script by hand."
        )
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps({
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "by": user,
        }))
    except OSError as e:
        raise ValueError(f"Could not leave the request at {target}: {e}")
    logger.warning(f"Uninstall of the modified build requested by {user or 'an admin'}")
    return record.get("layout", "")
