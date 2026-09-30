"""
Where Dispatcharr's memory goes: every process it runs (the web workers, the Celery workers
and their children, beat, daphne, Redis, PostgreSQL, nginx, ffmpeg for streams), how much each
really holds, and whether it has the language model's libraries (PyTorch) loaded.

Asked for on 2026-09-30, when the user's server showed Dispatcharr at nearly 4 GB and memory
at 100 %. Nothing here changes anything; it reads /proc through psutil.

PSS, not RSS, is what adds up: the web workers are forked from one master and share most of
their pages, so their RSS counted together says several times what they take. PSS divides
each shared page among the processes sharing it. Where PSS cannot be read (a process of
another user, PostgreSQL on a binary install), RSS is shown and marked.
"""

import os

KINDS = (
    # (what the command line holds, what to call it)
    ("celery -A dispatcharr beat", "Celery beat (the scheduler)"),
    ("-Q dvr", "Celery DVR worker (recordings)"),
    ("celery -A dispatcharr worker", "Celery worker (background tasks)"),
    ("daphne", "Daphne (websockets)"),
    ("redis-server", "Redis"),
    ("postgres", "PostgreSQL"),
    ("nginx", "nginx"),
    ("ffmpeg", "ffmpeg (a stream)"),
    ("streamlink", "Streamlink (a stream)"),
    ("yt-dlp", "yt-dlp (a stream)"),
    ("uwsgi", "Web worker (uWSGI)"),
    ("gunicorn", "Web worker"),
    ("manage.py", "Django command"),
)

TORCH_MARKERS = ("libtorch", "torch/lib")


def _kind(cmdline, name):
    text = " ".join(cmdline) if cmdline else name
    for marker, label in KINDS:
        if marker in text:
            return label
    return None


def _has_torch(pid):
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="ignore") as fh:
            return any(marker in line for line in fh for marker in TORCH_MARKERS)
    except OSError:
        return None


def snapshot():
    """Every Dispatcharr process with its memory, biggest first, and the totals per kind."""
    import psutil

    rows = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
        info = proc.info
        kind = _kind(info.get("cmdline") or [], info.get("name") or "")
        if kind is None:
            continue
        try:
            memory = proc.memory_full_info()
            pss = getattr(memory, "pss", None)
            rss = memory.rss
            exact = pss is not None
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            try:
                rss = proc.memory_info().rss
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            pss, exact = None, False
        # A Celery child and the parent it was forked from: the parent's line says which
        if kind.startswith("Celery worker") and info.get("ppid"):
            try:
                parent = psutil.Process(info["ppid"])
                if "celery" in " ".join(parent.cmdline()):
                    kind = "Celery worker child (runs one task at a time)"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        if kind.startswith("Web worker") and info.get("ppid"):
            try:
                if "uwsgi" not in " ".join(psutil.Process(info["ppid"]).cmdline()):
                    kind = "uWSGI master"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        rows.append({
            "pid": info["pid"], "kind": kind,
            "mb": round((pss if exact else rss) / 1048576, 1),
            "rss_mb": round(rss / 1048576, 1), "exact": exact,
            "torch": _has_torch(info["pid"]),
            "command": " ".join(info.get("cmdline") or [])[:160],
            "this": info["pid"] == os.getpid(),
        })
    rows.sort(key=lambda r: -r["mb"])
    totals = {}
    for row in rows:
        total = totals.setdefault(row["kind"], {"kind": row["kind"], "processes": 0, "mb": 0.0,
                                               "torch": 0})
        total["processes"] += 1
        total["mb"] = round(total["mb"] + row["mb"], 1)
        total["torch"] += bool(row["torch"])
    system = psutil.virtual_memory()
    return {
        "processes": rows,
        "kinds": sorted(totals.values(), key=lambda t: -t["mb"]),
        "total_mb": round(sum(r["mb"] for r in rows), 1),
        "system": {"total_mb": round(system.total / 1048576), "used_mb": round(system.used / 1048576),
                   "available_mb": round(system.available / 1048576), "percent": system.percent},
    }
