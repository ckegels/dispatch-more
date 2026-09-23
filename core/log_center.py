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
# Where a plugin's lines come from. Two, because a plugin can write either way:
#
# - `plugins.<key>`, the logger the loader hands each plugin in its context (see
#   apps.plugins.loader._build_context). Every plugin that logs the way the examples show
#   logs through this one.
# - `_dispatcharr_plugin_<key>`, which is what the loader calls a plugin's own module, so
#   it is what `logging.getLogger(__name__)` inside a plugin comes out as.
PLUGIN_LOGGER = "plugins"
PLUGIN_MODULE = "_dispatcharr_plugin_"


def _topic(label, loggers, words=()):
    """One part of Dispatcharr: what to call it, what wrote it, what it sounds like."""
    return {"label": label, "loggers": tuple(loggers), "words": tuple(words)}


# The parts people look for. Each says which loggers it is -- every line carries the name
# of the logger that wrote it, since that is what the format says ("{asctime} {levelname}
# {name} {message}") -- and which words give it away on the lines that carry no logger at
# all: uWSGI's own, a traceback, a celery banner.
#
# The loggers are what to add to when something new is written: a part nobody can pick out
# here is a part nobody can read the logs of.
TOPICS = {
    "proxy": _topic(
        "Channels playing (the proxy)",
        ("live_proxy", "apps.proxy", "ts_proxy", "vod_proxy"),
        ("stream_manager", "Channel ", "channel "),
    ),
    "overlap": _topic(
        "Channel Switch Overlap",
        ("apps.proxy.live_proxy.probation",),
        ("probation", "Overlap", "overlap", "skipped channel"),
    ),
    "media_servers": _topic(
        "Media servers (Plex, Jellyfin)",
        ("apps.proxy.live_proxy.media_server", "apps.proxy.live_proxy.media_servers"),
        ("Plex", "Jellyfin", "plex", "jellyfin", "media server"),
    ),
    "stream_check": _topic(
        "Stream Check",
        ("apps.channels.stream_check",),
        ("Stream Check", "stream_check"),
    ),
    # The Channel Manager's four tabs, which had nowhere of their own at all
    "channel_manager": _topic(
        "Channel Manager (Lineup, Guides, Logos, Layout)",
        (
            "apps.channels.channel_manager",
            "apps.channels.guide_manager",
            "apps.channels.guide_layout",
            "apps.channels.logo_library",
            "apps.channels.known_channels",
            "apps.channels.epg_matching",
        ),
        ("Channel Manager", "Guide Layout", "Guides read", "Guides:", "Find Logos"),
    ),
    "recordings": _topic(
        "Recordings (DVR)",
        ("apps.channels.recordings", "apps.channels.dvr", "apps.timeshift"),
        ("Recording ", "recording ", "DVR"),
    ),
    "m3u": _topic("Playlists (M3U)", ("apps.m3u",), ("M3U", "m3u")),
    "epg": _topic("Guides (EPG)", ("apps.epg",), ("EPG", "epg", "XMLTV", "xmltv")),
    "vod": _topic("VOD", ("apps.vod", "vod_proxy"), ("VOD",)),
    "output": _topic(
        "What players are given (M3U, EPG, HDHomeRun)",
        ("apps.output", "apps.hdhr"),
        ("HDHomeRun", "hdhr", "lineup.json"),
    ),
    "plugins": _topic(
        "Plugins",
        ("apps.plugins", PLUGIN_LOGGER, PLUGIN_MODULE),
        ("plugin",),
    ),
    "accounts": _topic(
        "Users and logins",
        ("apps.accounts", "django.security", "apps.api"),
        ("authentication", "Unauthorized", "forbidden"),
    ),
    "backups": _topic("Backup and restore", ("apps.backups", "core.tasks"), ("backup", "Backup")),
    "database": _topic(
        "Database and requests",
        ("django.db", "django.request", "django.geventpool", "django.channels"),
        ("OperationalError", "IntegrityError", "deadlock"),
    ),
    "celery": _topic("Background tasks", ("celery",), ("Task ", "task ")),
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
# How many lines are read at most. "Everything" over a journal a year old is gigabytes,
# and it was all read into one string in the web worker before anything was cut -- with
# Follow on, every five seconds. Both numbers are far more than anybody reads; what they
# do is stop one runaway log taking the server with it.
PAGE_LINES = 200_000
DOWNLOAD_LINES = 2_000_000

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
# A line Dispatcharr wrote itself, in the shape its formatter gives every one of them:
# "2026-09-19 07:21:38,343 INFO apps.channels.stream_check Stream Check: batch ended".
# Reading it apart is what lets a line be found by the part of Dispatcharr that wrote it
# rather than by whether the words happen to appear in it -- and it is the only way to tell
# one plugin's lines from another's.
_WRITTEN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+"
    r"(TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+(\S+)\s(.*)$",
    re.DOTALL,
)


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


def plugins():
    """
    The plugins installed, for the picker: [{"key", "name"}].

    From the table rather than the loader, so asking costs one query and never goes near
    the plugins folder: this is a list for a menu, not a reason to load anything.
    """
    try:
        from apps.plugins.models import PluginConfig

        return [
            {"key": one.key, "name": one.name or one.key}
            for one in PluginConfig.objects.order_by("name", "key")
        ]
    except Exception as e:
        logger.debug(f"Could not list the plugins: {e}")
        return []


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


def _raw_files(names, since_minutes, limit=None):
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
    # The newest, where there is a cap: the oldest of a runaway log is not what anybody
    # opened this page for
    return "\n".join(lines[-limit:] if limit else lines)


def _stamp(line):
    """
    When a line was written, from the start of it.

    The collector writes the server's own time with nothing to say so, and reading it as
    UTC put every line hours out on any machine that is not on UTC -- so "the last fifteen
    minutes" of a file quietly kept the wrong quarter of an hour, or none at all.
    """
    found = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(Z|[+-]\d{2}:?\d{2})?", line)
    if not found:
        return None
    try:
        when = datetime.fromisoformat(f"{found.group(1)}T{found.group(2)}")
    except ValueError:
        return None
    offset = found.group(3)
    if offset:
        # It said which time it is in, so it is taken at its word
        text = "+00:00" if offset == "Z" else (offset if ":" in offset else f"{offset[:3]}:{offset[3:]}")
        try:
            return when.replace(tzinfo=datetime.fromisoformat(f"2000-01-01T00:00:00{text}").tzinfo)
        except ValueError:
            return when.replace(tzinfo=timezone.utc)
    return when.astimezone() if when.tzinfo is None else when


def records(text):
    """
    The lines as records: [{"time", "service", "level", "logger", "text"}], a traceback's
    lines kept with the record that raised it.

    Where Dispatcharr wrote the line itself, the time, the level and the logger are read
    off it and the text is what is left -- the message. Which means the page can show them
    as columns rather than repeating the date twice in one line, and a search for what
    somebody typed searches the message rather than the furniture around it.
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
            found.append({
                "time": time_, "service": service, "level": "ERROR", "logger": "", "text": message,
            })
            continue
        if same_service and _CONTINUES.match(message):
            # The rest of a traceback: its frames, the code lines, and the exception at its end
            found[-1]["text"] += "\n" + message
            continue
        written = _WRITTEN.match(message)
        if written:
            when, level, logger_name, said = written.groups()
            found.append({
                "time": time_ or when,
                "service": service,
                "level": {"WARN": "WARNING"}.get(level, level),
                "logger": logger_name,
                "text": said,
            })
            continue
        # Something else wrote it: uWSGI, nginx, a celery banner, a bare traceback line
        level = _LEVEL.search(message[:120])
        found.append({
            "time": time_ or (message[:19] if _stamp(message) else ""),
            "service": service,
            "level": {"WARN": "WARNING"}.get(level.group(1), level.group(1)) if level else "",
            "logger": "",
            "text": message,
        })
    return found


def _written_by(record, loggers):
    """Whether this line came from one of these loggers, or a child of one."""
    name = record.get("logger") or ""
    if not name:
        return False
    for one in loggers:
        if name == one or name.startswith(f"{one}."):
            return True
        # A prefix rather than a whole name: what the loader calls a plugin's own module
        if one.endswith("_") and name.startswith(one):
            return True
    return False


def _about(record, topic):
    """Whether a record belongs to one of the parts of Dispatcharr (see TOPICS)."""
    if _written_by(record, topic["loggers"]):
        return True
    if record.get("logger"):
        # It said which logger wrote it and it was not one of these: the words are for the
        # lines that say nothing, not for second-guessing the ones that do
        return False
    return any(word in record["text"] for word in topic["words"])


def from_plugin(record, key):
    """
    Whether this line is one plugin's.

    By the logger, never by the plugin's name appearing somewhere in a line: a plugin
    called "Cooking" would otherwise own every line about a cooking channel.
    """
    if not key:
        return True
    return _written_by(record, (f"{PLUGIN_LOGGER}.{key}", f"{PLUGIN_MODULE}{key}", key))


def _keep(record, level, topic, text, plugin=""):
    if level and level != "ALL":
        wanted = LEVELS.index(level)
        have = LEVELS.index(record["level"]) if record["level"] in LEVELS else -1
        # A record without a level (a plain line from a service) is shown only when all are
        if have < wanted:
            return False
    if topic and topic in TOPICS and not _about(record, TOPICS[topic]):
        return False
    if plugin and not from_plugin(record, plugin):
        return False
    if text:
        wanted = text.lower()
        # The logger is searched as well as the message, so typing "stream_check" finds
        # what it wrote rather than only the lines that say the words
        if wanted not in record["text"].lower() and wanted not in (record.get("logger") or "").lower():
            return False
    return True


def _raw(source_ids, since, limit=PAGE_LINES):
    available = {s["id"]: s for s in sources()}
    chosen = [available[i] for i in source_ids if i in available] or list(available.values())
    minutes = SINCE.get(since, 60)
    units = [s["unit"] for s in chosen if s["kind"] == "journal"]
    files = [s["file"] for s in chosen if s["kind"] == "file"]
    parts = []
    if units:
        # journalctl is asked for the newest lines rather than the whole journal: without
        # a number it hands over everything it has, which on a server that has been up for
        # months is gigabytes into one string
        parts.append(_raw_journal(units, minutes, limit))
    if files:
        parts.append(_raw_files(files, minutes, limit))
    return "\n".join(parts)


def read(source_ids=(), since="1h", level="ALL", topic="", text="", plugin=""):
    """The records chosen, newest last, at most PAGE_RECORDS of them."""
    found = [
        r for r in records(_raw(source_ids, since, PAGE_LINES))
        if _keep(r, level, topic, text, plugin)
    ]
    return {
        "records": found[-PAGE_RECORDS:],
        "total": len(found),
        "cut": len(found) > PAGE_RECORDS,
    }


def as_line(record):
    """One record as a line again, with everything that was read off it put back."""
    parts = [
        record.get("time", ""),
        record.get("service", ""),
        record.get("level", ""),
        record.get("logger", ""),
    ]
    said = " ".join(part for part in parts if part)
    return f"{said}: {record['text']}" if said else record["text"]


def download(source_ids=(), since="all", level="ALL", topic="", text="", plugin=""):
    """The whole log for what is chosen, as text: every record, not only the page's."""
    if level in ("", "ALL") and not topic and not text and not plugin:
        # Nothing was narrowed, so it goes out as it was written
        data = _raw(source_ids, since, DOWNLOAD_LINES).encode("utf-8", "replace")
    else:
        data = "\n".join(
            as_line(r)
            for r in records(_raw(source_ids, since, DOWNLOAD_LINES))
            if _keep(r, level, topic, text, plugin)
        ).encode("utf-8", "replace")
    return data[-DOWNLOAD_BYTES:]


def _plugins_for_the_bundle():
    """The plugins installed, with what they are and whether they are on. No settings."""
    try:
        from apps.plugins.models import PluginConfig

        return [
            {
                "key": one.key,
                "name": one.name,
                "version": one.version,
                "enabled": one.enabled,
            }
            for one in PluginConfig.objects.order_by("name", "key")
        ]
    except Exception as e:
        logger.debug(f"Could not list the plugins for the bundle: {e}")
        return []


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
            # What is installed and whether it is on -- half of what anybody helping asks
            # first. Never a plugin's settings: those are where its keys and passwords are.
            "plugins": _plugins_for_the_bundle(),
        }, indent=1))
    return buffer.getvalue()
