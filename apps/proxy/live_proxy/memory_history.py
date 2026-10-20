"""
Reading the memory recorder's history back (Diagnostics -> Memory -> History).

The recorder (memory_recorder.py) writes one JSON line per sample to /data/memory-history, a file
per UTC day, gzipped once the day is over. This reads a time window of it, condenses it to at
most POINTS points for the chart (keeping each bucket's worst values, since peaks are what
matter), and finds what deserves a mark on it: reboots, gaps in the record (the server or the
recorder stopped), web workers that ended, sudden jumps, a slow Redis, processes stuck in `D`.
A torn last line (a crash while writing) is skipped, never an error.
"""

import gzip
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone

from .memory_recorder import DIR, FLAG, INTERVAL, KEEP_DAYS

POINTS = 720
DAY_FILE = re.compile(r"^(\d{4}-\d\d-\d\d)\.jsonl(\.gz)?$")
JUMP_MB = 1024
JUMP_WINDOW = 300
SLOW_REDIS_MS = 500
STUCK_MB = 500
WEB = "Web worker (uWSGI)"


def folder():
    return os.environ.get("MEMORY_HISTORY_DIR") or DIR


def is_on():
    return os.path.exists(os.path.join(folder(), FLAG))


def switch(on):
    path = folder()
    os.makedirs(path, exist_ok=True)
    flag = os.path.join(path, FLAG)
    if on:
        open(flag, "a").close()
    elif os.path.exists(flag):
        os.unlink(flag)
    return is_on()


def _days():
    try:
        names = os.listdir(folder())
    except OSError:
        return []
    return sorted((m.group(1), name) for name in names if (m := DAY_FILE.match(name)))


def records(since, until):
    """Every sample with since <= t <= until, oldest first."""
    first = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")
    last = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%d")
    for day, name in _days():
        if day < first or day > last:
            continue
        path = os.path.join(folder(), name)
        try:
            opener = gzip.open if name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # a line torn by a crash or a power cut
                    t = record.get("t")
                    if isinstance(t, (int, float)) and since <= t <= until:
                        yield record
        except (OSError, EOFError):
            continue  # an unreadable day; the rest still shows


def _mb(proc):
    return proc[4] if proc[4] is not None else proc[3]


def point(record):
    """What the chart shows of one sample."""
    sysinfo = record.get("sys") or {}
    cg = record.get("cg") if isinstance(record.get("cg"), dict) else {}
    procs = record.get("procs") or []
    kinds = {}
    for proc in procs:
        kinds[proc[2]] = kinds.get(proc[2], 0) + _mb(proc)
    total, available = sysinfo.get("total"), sysinfo.get("available")
    swap_used = None
    if sysinfo.get("swap_total") is not None and sysinfo.get("swap_free") is not None:
        swap_used = sysinfo["swap_total"] - sysinfo["swap_free"]
    largest = max(procs, key=_mb) if procs else None
    redis = record.get("redis") or {}
    psi = record.get("psi") if isinstance(record.get("psi"), dict) else {}
    return {
        "t": record["t"],
        "used": (total - available) if total is not None and available is not None else None,
        "total": total,
        "container": cg.get("current"),
        "limit": cg.get("max"),
        "anon": cg.get("anon"),
        "swap": cg.get("swap") if cg.get("swap") is not None else swap_used,
        "dispatcharr": sum(kinds.values()),
        "kinds": kinds,
        "largest": [largest[2], _mb(largest), largest[0]] if largest else None,
        "redis_ms": None if "error" in redis else redis.get("ms"),
        "redis_error": redis.get("error"),
        "pressure": (psi.get("full") or [None])[0],
        "stuck": sum(1 for p in procs if p[6] == "D"),
    }


def _worst(points):
    """One chart point for a bucket: the latest time, and each value at its worst."""
    out = dict(points[-1])
    for key in ("used", "container", "anon", "swap", "dispatcharr", "redis_ms", "pressure", "stuck"):
        values = [p[key] for p in points if p.get(key) is not None]
        out[key] = max(values) if values else None
    kinds = {}
    for p in points:
        for kind, mb in (p.get("kinds") or {}).items():
            kinds[kind] = max(kinds.get(kind, 0), mb)
    out["kinds"] = kinds
    largest = [p["largest"] for p in points if p.get("largest")]
    out["largest"] = max(largest, key=lambda x: x[1]) if largest else None
    out["redis_error"] = next((p["redis_error"] for p in points if p.get("redis_error")), None)
    return out


def _event(events, kind, t, text, **extra):
    # The same thing again within 10 minutes is the same event, made longer
    for event in reversed(events[-8:]):
        if event["kind"] == kind and t - event.get("until", event["t"]) <= 600:
            event["until"] = t
            event["count"] = event.get("count", 1) + 1
            return
    events.append({"kind": kind, "t": t, "text": text, **extra})


def events_of(samples):
    events = []
    before = None
    recent = []
    for record in samples:
        t = record["t"]
        now = point(record)
        if before is not None:
            gap = t - before["t"]
            every = before.get("every") or INTERVAL
            if record.get("boot") and before.get("boot") and record["boot"] != before["boot"]:
                events.append({"kind": "reboot", "t": t, "since": before["t"],
                               "text": f"The server restarted (no record for {gap // 60} min before it)"})
            elif gap > max(3 * every, 120):
                events.append({"kind": "gap", "t": t, "since": before["t"],
                               "text": f"No record for {gap // 60} min {gap % 60} s: the server or the recorder stopped"})
            elif record.get("start"):
                # Started again within the same boot without a long gap: the recorder itself was
                # restarted (a crash, an update) or switched off and on
                events.append({"kind": "start", "t": t, "since": before["t"],
                               "text": "The recorder started again (restarted, updated or switched back on)"})
            old = {p[0]: p for p in before.get("procs") or [] if p[2] == WEB}
            new = {p[0] for p in record.get("procs") or [] if p[2] == WEB}
            for pid, proc in old.items():
                if pid not in new and record.get("boot") == before.get("boot"):
                    events.append({"kind": "worker", "t": t, "pid": pid,
                                   "text": f"Web worker {pid} ended, holding {_mb(proc)} MB"})
        elif record.get("start"):
            events.append({"kind": "start", "t": t, "text": "Recording started"})
        recent = [p for p in recent if t - p["t"] <= JUMP_WINDOW] + [now]
        level = now.get("container") or now.get("used")
        lows = [p.get("container") or p.get("used") for p in recent if (p.get("container") or p.get("used")) is not None]
        if level is not None and lows and level - min(lows) >= JUMP_MB:
            _event(events, "jump", t, f"Memory rose {level - min(lows)} MB within {JUMP_WINDOW // 60} min")
        if now.get("redis_error"):
            _event(events, "redis", t, f"Redis did not answer ({now['redis_error']})")
        elif (now.get("redis_ms") or 0) >= SLOW_REDIS_MS:
            _event(events, "redis", t, f"Redis took {now['redis_ms']:.0f} ms to answer")
        stuck = [p for p in record.get("procs") or [] if p[6] == "D" and _mb(p) >= STUCK_MB]
        if stuck:
            p = max(stuck, key=_mb)
            _event(events, "stuck", t, f"{p[2]} {p[0]} stuck waiting (state D) at {_mb(p)} MB")
        before = record
    return events


def overview(hours):
    """The chart's points and events for the last `hours`, and the recorder's state."""
    until = time.time()
    since = until - hours * 3600
    samples = list(records(since, until))
    if len(samples) > POINTS:
        width = (until - since) / POINTS
        buckets = {}
        for record in samples:
            buckets.setdefault(int((record["t"] - since) // width), []).append(point(record))
        points = [_worst(bucket) for _, bucket in sorted(buckets.items())]
    else:
        points = [point(r) for r in samples]
    days = _days()
    size = 0
    for _, name in days:
        try:
            size += os.path.getsize(os.path.join(folder(), name))
        except OSError:
            pass
    last = samples[-1]["t"] if samples else None
    return {
        "on": is_on(),
        "folder": folder(),
        "last_sample": last,
        "last_sample_age": int(until - last) if last else None,
        "every": INTERVAL,
        "keep_days": KEEP_DAYS,
        "days": [d for d, _ in days],
        "bytes": size,
        "samples": len(samples),
        "points": points,
        "events": events_of(samples),
    }


def at(t, window=600):
    """The full sample closest to `t` (within `window` seconds), with the previous one's time."""
    best = None
    for record in records(t - window, t + window):
        if best is None or abs(record["t"] - t) < abs(best["t"] - t):
            best = record
    return best


def archive(hours=None):
    """The raw files (all, or the days touching the last `hours`) as a zip, for a report."""
    first = None
    if hours:
        first = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for day, name in _days():
            if first and day < first:
                continue
            zf.write(os.path.join(folder(), name), arcname=name)
    return buffer.getvalue()
