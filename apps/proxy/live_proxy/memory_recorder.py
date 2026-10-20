"""
The memory recorder (Diagnostics -> Memory -> History): a sample every INTERVAL seconds of where
memory goes, kept on disk for KEEP_DAYS, so that when the server fills up again what led up to it
can be read afterwards -- also after a reboot or a crash.

Why (2026-10-20): the user's LXC froze at 8 GB for an hour. By the time anyone looked, a web
worker held 6 GB and nothing said how it got there: the Memory tab shows only the present, and
the journal had been lost to the freeze. Measuring afterwards found the cause (fork/HANDOVER.md
§5.4c, "8 GB and a frozen container"); next time the record should already be there.

Built to keep writing while everything else is stuck:
- a process of its own (a systemd service on Linux installs, started by the Docker entrypoint),
  never a thread in a web worker, which is what freezes;
- plain Python reading /proc and /sys only: no Django, no database, no Redis client library, so
  nothing it depends on can be the thing that is stuck (Redis is asked over a raw socket with a
  2 s timeout, and a slow answer is itself recorded);
- PSS is read only from processes that are not in uninterruptible sleep (reading the memory map
  of a process stuck in reclaim can block the reader too);
- every line is written, flushed and fsync'd on its own, so a crash loses at most the line being
  written, and the reader skips a torn last line.

Each sample is one JSON line in /data/memory-history/YYYY-MM-DD.jsonl (UTC days); days before
today are gzipped. Off unless the flag file `on` exists in that folder (the tab's switch): off,
the process only checks the flag every INTERVAL seconds.

Run: python3 memory_recorder.py [--dir /data/memory-history] [--once]
"""

import gzip
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone

DIR = os.environ.get("MEMORY_HISTORY_DIR") or "/data/memory-history"
INFLIGHT_DIR = os.environ.get("MEMORY_INFLIGHT_DIR") or "/dev/shm/dispatch-more-inflight"
FLAG = "on"
INTERVAL = 30
# Sample faster for a while when something looks wrong (see _alarming)
FAST_INTERVAL = 5
FAST_FOR = 120
KEEP_DAYS = 14
KEEP_BYTES = 500 * 1024 * 1024
OTHERS = 5
REDIS = (os.environ.get("REDIS_HOST") or "127.0.0.1", int(os.environ.get("REDIS_PORT") or 6379))

# What to call a process, by its command line. memory.py's list, kept here as well so this file
# runs with nothing from the app imported.
KINDS = (
    ("memory_recorder.py", "Memory recorder"),
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
    ("captions/worker.py", "Caption worker"),
)
CLOCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r", **({} if binary else {"errors": "replace"})) as fh:
            return fh.read()
    except OSError:
        return None


def _kv_kb(text):
    """'Name:   123 kB' lines -> {name: MB}."""
    out = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) // 1024 if len(parts) > 1 and parts[1] == "kB" else int(parts[0])
    return out


def system():
    m = _kv_kb(_read("/proc/meminfo"))
    out = {
        "total": m.get("MemTotal"), "available": m.get("MemAvailable"),
        "swap_total": m.get("SwapTotal"), "swap_free": m.get("SwapFree"),
    }
    load = (_read("/proc/loadavg") or "").split()
    if len(load) >= 3:
        out["load"] = [float(x) for x in load[:3]]
    up = (_read("/proc/uptime") or "").split()
    if up:
        out["uptime"] = int(float(up[0]))
    return out


def cgroup():
    """The container's own memory (cgroup v2 at the namespace root), when it can be read."""
    base = "/sys/fs/cgroup"
    current = _read(f"{base}/memory.current")
    if current is None:
        return None
    out = {"current": int(current) // 1048576}
    limit = (_read(f"{base}/memory.max") or "").strip()
    out["max"] = int(limit) // 1048576 if limit.isdigit() else None
    swap = (_read(f"{base}/memory.swap.current") or "").strip()
    if swap.isdigit():
        out["swap"] = int(swap) // 1048576
    stat = {}
    for line in (_read(f"{base}/memory.stat") or "").splitlines():
        key, _, value = line.partition(" ")
        if key in ("anon", "file", "shmem", "kernel") and value.strip().isdigit():
            stat[key] = int(value) // 1048576
    out.update(stat)
    events = {}
    for line in (_read(f"{base}/memory.events") or "").splitlines():
        key, _, value = line.partition(" ")
        if key in ("high", "max", "oom", "oom_kill") and value.strip().isdigit():
            events[key] = int(value)
    if events:
        out["events"] = events
    return out


def pressure():
    """Memory pressure (PSI): the share of time tasks waited on memory, over 10 s and 60 s."""
    text = _read("/proc/pressure/memory")
    if not text:
        return None
    out = {}
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        fields = dict(f.split("=") for f in rest.split() if "=" in f)
        out[kind] = [float(fields.get("avg10", 0)), float(fields.get("avg60", 0))]
    return out


def boot_id():
    return (_read("/proc/sys/kernel/random/boot_id") or "").strip()[:8]


def _kind(cmdline):
    for marker, label in KINDS:
        if marker in cmdline:
            return label
    return None


def processes():
    """
    Every Dispatcharr process ([pid, ppid, kind, rss MB, pss MB or null, swap MB, state,
    threads, cpu seconds, started (epoch), command]) and the OTHERS largest of the rest.
    """
    boot = time.time() - float((_read("/proc/uptime") or "0").split()[0] or 0)
    ours, others = [], []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        raw = _read(f"/proc/{pid}/cmdline", binary=True)
        if raw is None:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        status = _read(f"/proc/{pid}/status") or ""
        fields = {}
        for line in status.splitlines():
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        rss = int((fields.get("VmRSS") or "0 kB").split()[0]) // 1024
        if not cmd:
            continue  # kernel threads
        kind = _kind(cmd)
        if kind is None:
            others.append([pid, fields.get("Name", "?"), rss])
            continue
        state = (fields.get("State") or "?")[:1]
        stat = (_read(f"/proc/{pid}/stat") or "").rsplit(")", 1)[-1].split()
        cpu = started = None
        if len(stat) > 20:
            cpu = round((int(stat[11]) + int(stat[12])) / CLOCK, 1)
            started = int(boot + int(stat[19]) / CLOCK)
        pss = None
        if state != "D":
            rollup = _read(f"/proc/{pid}/smaps_rollup")
            match = re.search(r"^Pss:\s+(\d+) kB", rollup or "", re.M)
            if match:
                pss = int(match.group(1)) // 1024
        swap = int((fields.get("VmSwap") or "0 kB").split()[0]) // 1024
        ours.append([
            pid, int(fields.get("PPid") or 0), kind, rss, pss, swap, state,
            int(fields.get("Threads") or 0), cpu, started, cmd[:160],
        ])
    ours.sort(key=lambda p: -(p[4] if p[4] is not None else p[3]))
    others.sort(key=lambda p: -p[2])
    return ours, others[:OTHERS]


def redis():
    """How long Redis takes to answer, and what it says about its memory (raw RESP, 2 s cap)."""
    started = time.monotonic()
    try:
        with socket.create_connection(REDIS, timeout=2) as conn:
            conn.sendall(b"*1\r\n$4\r\nPING\r\n")
            if not conn.recv(64).startswith(b"+PONG"):
                return {"error": "no PONG"}
            ms = round((time.monotonic() - started) * 1000, 1)
            conn.sendall(b"*2\r\n$4\r\nINFO\r\n$6\r\nmemory\r\n")
            data = b""
            while b"\r\n" not in data or len(data) < 64:
                part = conn.recv(65536)
                if not part:
                    break
                data += part
            header, _, rest = data.partition(b"\r\n")
            if header.startswith(b"$"):
                need = int(header[1:])
                while len(rest) < need:
                    part = conn.recv(65536)
                    if not part:
                        break
                    rest += part
            info = dict(
                line.split(":", 1) for line in rest.decode("utf-8", "replace").splitlines() if ":" in line
            )
            used = info.get("used_memory")
            return {"ms": ms, "used": int(used) // 1048576 if used and used.isdigit() else None}
    except (OSError, ValueError) as error:
        return {"error": type(error).__name__, "ms": round((time.monotonic() - started) * 1000, 1)}


def inflight():
    """What each web worker and Celery task was doing: the files apps.proxy.live_proxy.inflight writes."""
    out = {}
    try:
        names = os.listdir(INFLIGHT_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        pid = name[:-5]
        if not os.path.exists(f"/proc/{pid}"):
            try:
                os.unlink(os.path.join(INFLIGHT_DIR, name))  # a process that is gone
            except OSError:
                pass
            continue
        text = _read(os.path.join(INFLIGHT_DIR, name))
        try:
            out[pid] = json.loads(text) if text else None
        except ValueError:
            continue
    return out


def sample():
    """One sample: every part on its own, so one that fails never costs the others."""
    out = {"t": int(time.time()), "boot": boot_id()}
    for key, part in (("sys", system), ("cg", cgroup), ("psi", pressure), ("redis", redis),
                      ("inflight", inflight)):
        try:
            out[key] = part()
        except Exception as error:  # noqa: BLE001 - a recorder that stops is worse than a gap
            out[key] = {"error": f"{type(error).__name__}: {error}"[:200]}
    try:
        out["procs"], out["others"] = processes()
    except Exception as error:  # noqa: BLE001
        out["procs"], out["others"] = [], []
        out["procs_error"] = f"{type(error).__name__}: {error}"[:200]
    return out


def _alarming(now, before):
    """Worth sampling faster: memory jumped, Redis is slow, or tasks wait on memory."""
    if not before:
        return False
    cg, cg0 = now.get("cg") or {}, before.get("cg") or {}
    if isinstance(cg, dict) and isinstance(cg0, dict) and cg.get("current") and cg0.get("current"):
        if cg["current"] - cg0["current"] >= 300:
            return True
    sysnow, sys0 = now.get("sys") or {}, before.get("sys") or {}
    if sysnow.get("available") is not None and sys0.get("available") is not None:
        if sys0["available"] - sysnow["available"] >= 300:
            return True
    redis_now = now.get("redis") or {}
    if "error" in redis_now or (redis_now.get("ms") or 0) >= 500:
        return True
    psi = (now.get("psi") or {}).get("full") if isinstance(now.get("psi"), dict) else None
    if psi and psi[0] >= 5:
        return True
    return any(p[6] == "D" and p[3] >= 500 for p in now.get("procs") or [])


def _day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def write(folder, record):
    """Append one line, flushed to disk before returning."""
    path = os.path.join(folder, _day(record["t"]) + ".jsonl")
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def tidy(folder, today):
    """Gzip finished days, drop days past KEEP_DAYS, and the oldest while over KEEP_BYTES."""
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if name.endswith(".jsonl") and name[:10] < today:
            with open(path, "rb") as src, gzip.open(path + ".gz.tmp", "wb") as dst:
                dst.write(src.read())
            os.replace(path + ".gz.tmp", path + ".gz")
            os.unlink(path)
    days = sorted(n for n in os.listdir(folder) if re.match(r"\d{4}-\d\d-\d\d\.jsonl(\.gz)?$", n))
    cutoff = datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    for name in days:
        if name[:10] < cutoff:
            os.unlink(os.path.join(folder, name))
    days = sorted(n for n in os.listdir(folder) if re.match(r"\d{4}-\d\d-\d\d\.jsonl(\.gz)?$", n))
    while len(days) > 1 and sum(os.path.getsize(os.path.join(folder, n)) for n in days) > KEEP_BYTES:
        os.unlink(os.path.join(folder, days.pop(0)))


def run(folder, once=False):
    os.makedirs(folder, exist_ok=True)
    try:
        os.chmod(folder, 0o755)
    except OSError:
        pass
    before = None
    fast_until = 0.0
    tidied = None
    started = True
    while True:
        began = time.monotonic()
        if os.path.exists(os.path.join(folder, FLAG)):
            record = sample()
            if started:
                # A reboot, a crash of the recorder or its start: the reader marks the boundary
                record["start"] = True
                started = False
            if _alarming(record, before):
                if fast_until < time.monotonic():
                    record["fast"] = True
                fast_until = time.monotonic() + FAST_FOR
            record["every"] = FAST_INTERVAL if fast_until > time.monotonic() else INTERVAL
            try:
                write(folder, record)
                today = _day(record["t"])
                if tidied != today:
                    tidy(folder, today)
                    tidied = today
            except OSError as error:
                print(f"memory recorder: could not write: {error}", file=sys.stderr, flush=True)
            before = record
        else:
            started = True
            before = None
        if once:
            return
        wait = (FAST_INTERVAL if fast_until > time.monotonic() else INTERVAL) - (time.monotonic() - began)
        time.sleep(max(1.0, wait))


if __name__ == "__main__":
    args = sys.argv[1:]
    folder = args[args.index("--dir") + 1] if "--dir" in args else DIR
    run(folder, once="--once" in args)
