"""
Diagnostics -> Logs: every log Dispatcharr writes, wherever it writes it, in one place.

Stock Dispatcharr's own log pages show the files its log collector writes -- which only runs
in Docker. On Linux or an LXC nothing writes them, the page stays empty, and the logs are in
the systemd journal, one per service, reachable only with journalctl over SSH. This reads
either: the journal of every dispatcharr* service where there is systemd, the collector's
files where there are those.

Lines are kept as records: a Python traceback belongs to the line that raised it, so asking
for errors shows each error whole. Records can be narrowed by service, time, level, a topic
(the loggers of one part of Dispatcharr) and text, followed as they come, and downloaded --
the whole log for what is chosen, or a bundle of everything from the last day to send on.
"""

import io
import json
import logging
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

# How far back, by name; none sets no start
SINCE = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440, "7d": 10080, "all": None}
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# The parts of Dispatcharr people look for, by the loggers and words their lines carry
TOPICS = {
    "stream_check": ("Stream Check", "stream_check"),
    "proxy": ("live_proxy", "ts_proxy", "proxy.", "stream_manager", "Channel ", "channel "),
    "overlap": ("probation", "Overlap", "overlap", "skipped channel"),
    "m3u": ("apps.m3u", "M3U", "m3u"),
    "epg": ("apps.epg", "EPG", "epg"),
    "media_servers": ("media_server", "Plex", "Jellyfin", "plex", "jellyfin"),
    "vod": ("vod_proxy", "apps.vod", "VOD"),
    "celery": ("celery", "Task ", "task "),
}
# What each dispatcharr* service is, for people rather than systemd
SERVICE_NAMES = {
    "dispatcharr": "Web server (uWSGI): the proxy, the page, the API",
    "dispatcharr-celery": "Background tasks (Celery): refreshes, Stream Check",
    "dispatcharr-celerybeat": "Scheduler (Celery beat)",
    "dispatcharr-daphne": "Live updates (websockets)",
}
# At most this many records in the page; the download has all of them
PAGE_RECORDS = 3000
# A download of everything, capped so a runaway log cannot fill the server's memory
DOWNLOAD_BYTES = 200 * 1024 * 1024

# Every line is a record of its own, but for the lines of a Python traceback, which belong to
# the record that reported it
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\):?$")
# What goes on a traceback after its first line: indented frames and code, the chained-error
# sentences, carets, and the exception it ends with
_CONTINUES = re.compile(
    r"^(\s|During handling of the above exception|The above exception was the direct cause|"
    r"[\^~]+\s*$|[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning)\b(:.*)?$)"
)
# A journal line: "2026-09-19T09:21:38+0200 host unit[pid]: message"
_JOURNAL = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2}) \S+ ([^:\[]+)(?:\[\d+\])?: ?(.*)$")
_LEVEL = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b")


# ── Where the logs are ───────────────────────────────────────────────────────


def _systemd_units():
    """The dispatcharr* services on this machine, or none where there is no systemd."""
    if not (os.path.isdir("/run/systemd/system") and shutil.which("journalctl") and shutil.which("systemctl")):
        return []
    try:
        listed = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "dispatcharr*"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    units = []
    for line in listed.splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit.endswith(".service") and re.fullmatch(r"[A-Za-z0-9@._-]+", unit):
            units.append(unit[: -len(".service")])
    return sorted(set(units))


def _collector_files():
    """The collector's live log files (Docker), newest last."""
    from dispatcharr.log_collector import is_log_family_name

    base = getattr(settings, "LOG_FILE_DIR", "") or ""
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return sorted(name for name in names if is_log_family_name(name) and not re.search(r"\.\d+$", name))


def sources():
    """What can be read here: [{"id", "label", "kind"}]."""
    found = []
    for unit in _systemd_units():
        found.append({"id": f"journal:{unit}", "label": SERVICE_NAMES.get(unit, unit), "kind": "journal", "unit": unit})
    for name in _collector_files():
        role = name[len("dispatcharr.log"):].lstrip("-") or "all"
        found.append({"id": f"file:{name}", "label": f"Log collector ({role})", "kind": "file", "file": name})
    return found


def journal_readable():
    """Whether the web app may read the journal (it needs root, or the systemd-journal group)."""
    if not shutil.which("journalctl"):
        return False
    try:
        done = subprocess.run(["journalctl", "-n", "1", "--no-pager", "-q"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and "No journal files were opened" not in done.stderr and "insufficient permissions" not in done.stderr.lower()


# ── Reading ──────────────────────────────────────────────────────────────────


def _raw_journal(units, since_minutes, limit=None):
    command = ["journalctl", "--no-pager", "-o", "short-iso", "-q"]
    for unit in units:
        command += ["-u", unit]
    if since_minutes:
        command += ["--since", f"-{since_minutes}min"]
    if limit:
        command += ["-n", str(limit)]
    done = subprocess.run(command, capture_output=True, timeout=120)
    return done.stdout.decode("utf-8", "replace")


def _raw_files(names, since_minutes):
    """The collector's files, rotated ones first, cut at the time asked for."""
    base = settings.LOG_FILE_DIR
    lines = []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes) if since_minutes else None
    for name in names:
        family = sorted(
            (n for n in os.listdir(base) if n == name or re.fullmatch(re.escape(name) + r"\.\d+", n)),
            key=lambda n: -int(n.rsplit(".", 1)[1]) if re.search(r"\.\d+$", n) else 0,
        )
        for part in family:
            try:
                with open(os.path.join(base, part), "rb") as handle:
                    text = handle.read().decode("utf-8", "replace")
            except OSError:
                continue
            for line in text.splitlines():
                if cutoff:
                    stamp = _stamp(line)
                    if stamp and stamp < cutoff:
                        continue
                lines.append(line)
    return "\n".join(lines)


def _stamp(line):
    found = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", line)
    if not found:
        return None
    try:
        return datetime.fromisoformat(f"{found.group(1)}T{found.group(2)}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def records(text):
    """
    The lines as records: [{"time", "service", "level", "text"}], a traceback's lines kept
    with the record that raised it.
    """
    found = []
    for line in text.splitlines():
        if not line.strip():
            continue
        service, message, time_ = "", line, ""
        journal = _JOURNAL.match(line)
        if journal:
            time_, service, message = journal.group(1), journal.group(2).strip(), journal.group(3)
        same_service = bool(found) and found[-1]["service"] == service
        if _TRACEBACK.match(message) and same_service and found[-1]["level"] in ("ERROR", "CRITICAL"):
            # A traceback belongs to the ERROR line that reported it
            found[-1]["text"] += "\n" + message
            continue
        if _TRACEBACK.match(message):
            # After anything else it is an error of its own, or asking for errors would lose it
            found.append({"time": time_, "service": service, "level": "ERROR", "text": message})
            continue
        if same_service and _CONTINUES.match(message):
            # The rest of a traceback: its frames, the code lines, and the exception at its end
            found[-1]["text"] += "\n" + message
            continue
        level = _LEVEL.search(message[:120])
        found.append({
            "time": time_ or (message[:19] if _stamp(message) else ""),
            "service": service,
            "level": {"WARN": "WARNING"}.get(level.group(1), level.group(1)) if level else "",
            "text": message,
        })
    return found


def _keep(record, level, topic, text):
    if level and level != "ALL":
        wanted = LEVELS.index(level)
        have = LEVELS.index(record["level"]) if record["level"] in LEVELS else -1
        # A record without a level (a plain line from a service) is shown only when all are
        if have < wanted:
            return False
    if topic and topic in TOPICS and not any(word in record["text"] for word in TOPICS[topic]):
        return False
    if text and text.lower() not in record["text"].lower():
        return False
    return True


def _raw(source_ids, since):
    available = {s["id"]: s for s in sources()}
    chosen = [available[i] for i in source_ids if i in available] or list(available.values())
    minutes = SINCE.get(since, 60)
    units = [s["unit"] for s in chosen if s["kind"] == "journal"]
    files = [s["file"] for s in chosen if s["kind"] == "file"]
    parts = []
    if units:
        parts.append(_raw_journal(units, minutes))
    if files:
        parts.append(_raw_files(files, minutes))
    return "\n".join(parts)


def read(source_ids=(), since="1h", level="ALL", topic="", text=""):
    """The records chosen, newest last, at most PAGE_RECORDS of them."""
    found = [r for r in records(_raw(source_ids, since)) if _keep(r, level, topic, text)]
    return {
        "records": found[-PAGE_RECORDS:],
        "total": len(found),
        "cut": len(found) > PAGE_RECORDS,
    }


def download(source_ids=(), since="all", level="ALL", topic="", text=""):
    """The whole log for what is chosen, as text: every record, not only the page's."""
    if level in ("", "ALL") and not topic and not text:
        data = _raw(source_ids, since).encode("utf-8", "replace")
    else:
        data = "\n".join(
            r["text"] if not r["time"] or r["text"].startswith(r["time"][:10]) else f"{r['time']} {r['service']}: {r['text']}"
            for r in records(_raw(source_ids, since)) if _keep(r, level, topic, text)
        ).encode("utf-8", "replace")
    return data[-DOWNLOAD_BYTES:]


def bundle():
    """
    Everything someone helping would ask for, in one zip: every log of the last day, which
    Dispatcharr and which build, where the logs came from.
    """
    import version

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources():
            name = source.get("unit") or source.get("file")
            archive.writestr(f"logs/{name}.log", download([source["id"]], since="24h"))
        archive.writestr("about.json", json.dumps({
            "dispatcharr": version.__version__,
            "build": getattr(version, "__build__", None),
            "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": [s["label"] for s in sources()],
            "journal_readable": journal_readable(),
        }, indent=1))
    return buffer.getvalue()
