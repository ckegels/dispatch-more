"""
Server rewind (fork/pause-resume.md §4.3): the server keeps a recording of a watched channel, so
a TV can pause for as long as it likes, and rewind, without keeping anything itself -- the TVs'
own disks are what ended long pauses (a Shield with 1 GB free, 2026-10-02).

While a TV watches a channel it tells the server so every ~20 s (`watch`, with where it paused
when it is paused). The first such call starts a recorder for the channel in the worker that
received it: `ffmpeg -c copy` reading the channel from this server's own proxy -- one more client
of the channel the TV already plays, no provider connection of its own (User-Agent
`DispatchMore-Rewind/1`, passed by Force Close like the caption worker) -- writing 6-second
segments with their wall times into `<REWIND_DIR>/<channel uuid>/`. A Redis lease makes it one
recorder per channel across workers; a supervisor loop keeps the lease, trims, keeps the budget
and stops the recorder once no TV has asked for a while.

A TV plays `/proxy/ts/rewind/<uuid>/index.m3u8`: an HLS event playlist made from what is on disk
(segments of a restarted recorder after an #EXT-X-DISCONTINUITY), with #EXT-X-PROGRAM-DATE-TIME
so the player knows the wall time of every moment.

Kept: the last `rewind_minutes` (60) always; while a TV is paused, everything from its pause on,
up to `rewind_max_pause_minutes` (240); all channels together within `rewind_budget_gb` (20, and
never more than the disk's free space allows) -- over it, the oldest minutes nobody paused at go
first. Off (`rewind` false): nothing is recorded, the API says so.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

USER_AGENT = "DispatchMore-Rewind/1"
SEGMENT_SECONDS = 6
# A TV that has not said it is watching for this long is gone
VIEWER_TTL = 45
# The recorder's lease, refreshed by its supervisor every LOOP seconds
LEASE_TTL = 30
LOOP = 5
# A recording nobody watches is kept this long (a flip back finds it), then deleted
FILES_GRACE = 5 * 60
# Free space never used, whatever the budget
DISK_MARGIN_BYTES = 5 * 1024 ** 3
# A recorder that keeps failing is not restarted forever
MAX_RESTARTS = 20

_recorders = {}  # uuid -> Recorder, the ones this process runs
_lock = threading.Lock()


# ── Settings and places ───────────────────────────────────────────────────────

def settings():
    from .app_devices import load_settings

    values = load_settings()
    return {
        "enabled": bool(values.get("rewind")),
        "minutes": int(values.get("rewind_minutes") or 60),
        "max_pause_minutes": int(values.get("rewind_max_pause_minutes") or 240),
        "budget_gb": float(values.get("rewind_budget_gb") or 20),
    }


def rewind_dir():
    return os.environ.get("DISPATCHARR_REWIND_DIR", "/data/rewind")


def channel_dir(uuid):
    return os.path.join(rewind_dir(), str(uuid))


def stream_base():
    return os.environ.get("DISPATCHARR_INTERNAL_URL", "http://127.0.0.1:9191").rstrip("/")


def _redis():
    from core.utils import RedisClient

    return RedisClient.get_client()


def _viewers_key(uuid):
    return f"rewind:viewers:{uuid}"


def _owner_key(uuid):
    return f"rewind:owner:{uuid}"


def _worker_token():
    return f"{os.getpid()}"


# ── TVs watching ──────────────────────────────────────────────────────────────

def watch(uuid, viewer, paused_at_ms=None, redis_client=None):
    """A TV watches `uuid` (and, paused, is at `paused_at_ms`); starts the recorder when needed.
    Returns the window (see `window`)."""
    redis_client = redis_client or _redis()
    conf = settings()
    if not conf["enabled"]:
        return {"enabled": False}
    entry = {"until": time.time() + VIEWER_TTL}
    if paused_at_ms:
        entry["paused_at"] = int(paused_at_ms)
    redis_client.hset(_viewers_key(uuid), str(viewer), json.dumps(entry))
    redis_client.expire(_viewers_key(uuid), VIEWER_TTL * 4)
    ensure_recorder(uuid, redis_client)
    return window(uuid)


def leave(uuid, viewer, redis_client=None):
    redis_client = redis_client or _redis()
    redis_client.hdel(_viewers_key(uuid), str(viewer))


def viewers(uuid, redis_client=None, now=None):
    """{viewer: entry} of the TVs still watching."""
    redis_client = redis_client or _redis()
    now = now or time.time()
    found = {}
    for key, raw in (redis_client.hgetall(_viewers_key(uuid)) or {}).items():
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if float(entry.get("until") or 0) > now:
            found[key.decode() if isinstance(key, bytes) else key] = entry
    return found


def keep_from_ms(conf, watching, now=None):
    """The oldest moment to keep: the last `minutes`, or a paused TV's position, within the
    maximum pause."""
    now_ms = int((now or time.time()) * 1000)
    keep = now_ms - conf["minutes"] * 60_000
    paused = [int(e["paused_at"]) for e in watching.values() if e.get("paused_at")]
    if paused:
        keep = min(keep, min(paused) - 30_000)
    return max(keep, now_ms - conf["max_pause_minutes"] * 60_000)


# ── The recorder ──────────────────────────────────────────────────────────────

def ensure_recorder(uuid, redis_client=None):
    """Starts a recorder in this process unless one runs (here or in another worker)."""
    redis_client = redis_client or _redis()
    with _lock:
        recorder = _recorders.get(str(uuid))
        if recorder is not None and recorder.alive():
            return recorder
        if not redis_client.set(_owner_key(uuid), _worker_token(), nx=True, ex=LEASE_TTL):
            return None  # another worker records it
        recorder = Recorder(str(uuid), redis_client)
        _recorders[str(uuid)] = recorder
        recorder.start()
        return recorder


def _set_parent_death_signal():
    """The recorder dies with its worker (Linux), so no ffmpeg outlives it unsupervised."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGTERM)
    except Exception:
        pass


class Recorder:
    def __init__(self, uuid, redis_client):
        self.uuid = uuid
        self.redis = redis_client
        self.dir = channel_dir(uuid)
        self.process = None
        self.part = 0
        self.restarts = 0
        self.stop_event = threading.Event()
        self.idle_since = None

    def alive(self):
        return not self.stop_event.is_set()

    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        existing = [int(m.group(1)) for m in (re.match(r"part-(\d+)\.m3u8$", n) for n in os.listdir(self.dir)) if m]
        self.part = max(existing, default=0)
        threading.Thread(target=self._supervise, daemon=True, name=f"rewind-{self.uuid}").start()

    def command(self):
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "fatal",
            "-user_agent", USER_AGENT,
            "-i", f"{stream_base()}/proxy/ts/stream/{self.uuid}",
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_list_size", "0",
            "-hls_flags", "program_date_time+temp_file+independent_segments",
            # Numbered: named by the second they were written, the burst a recorder starts with
            # (the proxy's few seconds of buffer at once) overwrote each other
            "-hls_segment_filename", os.path.join(self.dir, f"p{self.part}-%06d.ts"),
            os.path.join(self.dir, f"part-{self.part}.m3u8"),
        ]

    def _spawn(self):
        self.part += 1
        try:
            # To a file, not a pipe: over hours a pipe nobody reads fills and stalls ffmpeg
            self.log = open(os.path.join(self.dir, "recorder.log"), "wb")
            self.process = subprocess.Popen(
                self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=self.log, preexec_fn=_set_parent_death_signal,
            )
            logger.info(f"Rewind: recording channel {self.uuid} (part {self.part})")
        except OSError as e:
            logger.warning(f"Rewind: could not start ffmpeg for {self.uuid}: {e}")
            self.process = None

    def _kill(self):
        if getattr(self, "log", None) is not None:
            try:
                self.log.close()
            except OSError:
                pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def _supervise(self):
        try:
            while not self.stop_event.is_set():
                conf = settings()
                watching = viewers(self.uuid, self.redis)
                if not conf["enabled"]:
                    break
                if watching:
                    self.idle_since = None
                    if self.process is None or self.process.poll() is not None:
                        if self.process is not None:
                            err = _tail(os.path.join(self.dir, "recorder.log"))
                            logger.info(f"Rewind: recorder of {self.uuid} ended ({err.strip() or 'no message'}); restarting")
                            self.restarts += 1
                        if self.restarts > MAX_RESTARTS:
                            logger.warning(f"Rewind: recorder of {self.uuid} keeps failing; giving up")
                            break
                        self._spawn()
                else:
                    # Nobody watches: stop recording at once (it holds the channel open, and with
                    # it a provider connection); the files stay a while for a flip back
                    self._kill()
                    self.idle_since = self.idle_since or time.time()
                    if time.time() - self.idle_since > FILES_GRACE:
                        shutil.rmtree(self.dir, ignore_errors=True)
                        break
                trim(self.uuid, keep_from_ms(conf, watching))
                enforce_budget(conf)
                self.redis.set(_owner_key(self.uuid), _worker_token(), ex=LEASE_TTL)
                self.stop_event.wait(LOOP)
        except Exception:
            logger.exception(f"Rewind: supervisor of {self.uuid} failed")
        finally:
            self._kill()
            self.stop_event.set()
            with _lock:
                if _recorders.get(self.uuid) is self:
                    del _recorders[self.uuid]
            try:
                if self.redis.get(_owner_key(self.uuid)) in (_worker_token(), _worker_token().encode()):
                    self.redis.delete(_owner_key(self.uuid))
            except Exception:
                pass


def _tail(path, size=300):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - size))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ── What is on disk ───────────────────────────────────────────────────────────

_SEGMENT = re.compile(r"^p(\d+)-(\d+)\.ts$")


def segments(uuid):
    """[(part, wall_ms, duration_s, name)] in order, read from the recorders' own playlists
    (durations and wall times), only the segments still on disk."""
    folder = channel_dir(uuid)
    if not os.path.isdir(folder):
        return []
    present = set(os.listdir(folder))
    found = []
    for name in sorted(present):
        m = re.match(r"part-(\d+)\.m3u8$", name)
        if not m:
            continue
        part = int(m.group(1))
        duration = None
        wall_ms = None
        try:
            with open(os.path.join(folder, name)) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("#EXTINF:"):
                        duration = float(line[8:].split(",")[0])
                    elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                        wall_ms = _parse_wall(line.split(":", 1)[1])
                    elif line and not line.startswith("#"):
                        seg = os.path.basename(line)
                        if seg in present and duration is not None and wall_ms is not None:
                            found.append((part, wall_ms, duration, seg))
                        if wall_ms is not None and duration is not None:
                            wall_ms += int(duration * 1000)
                        duration = None
        except OSError:
            continue
    # Part by part (one recorder at a time, so the parts follow each other), then by time
    found.sort(key=lambda s: (s[0], s[1]))
    return found


def _parse_wall(text):
    text = text.strip()
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def playlist(uuid, segment_url=lambda name: name):
    """The HLS event playlist of what is on disk."""
    found = segments(uuid)
    target = max([int(d + 0.999) for _p, _w, d, _n in found] or [SEGMENT_SECONDS])
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-INDEPENDENT-SEGMENTS",
             f"#EXT-X-MEDIA-SEQUENCE:{_sequence(found)}"]
    previous_part = None
    for part, wall_ms, duration, name in found:
        if previous_part is not None and part != previous_part:
            lines.append("#EXT-X-DISCONTINUITY")
        previous_part = part
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso(wall_ms)}")
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(segment_url(name))
    return "\n".join(lines) + "\n"


def _sequence(found):
    """A media sequence that grows as old segments are trimmed: the first segment's wall time
    in segment lengths, so a player sees trimmed segments leave at the front."""
    return int(found[0][1] / 1000 / SEGMENT_SECONDS) if found else 0


def window(uuid):
    """What a TV can rewind into: {enabled, recording, tail_wall_ms, head_wall_ms, playlist}."""
    found = segments(uuid)
    out = {"enabled": settings()["enabled"], "recording": bool(found),
           "playlist": f"/proxy/ts/rewind/{uuid}/index.m3u8"}
    if found:
        out["tail_wall_ms"] = found[0][1]
        last = found[-1]
        out["head_wall_ms"] = last[1] + int(last[2] * 1000)
    return out


def segment_path(uuid, name):
    if not _SEGMENT.match(name or ""):
        return None
    path = os.path.join(channel_dir(uuid), name)
    return path if os.path.isfile(path) else None


# ── Keeping it small ──────────────────────────────────────────────────────────

def trim(uuid, keep_from):
    """Deletes the segments that end before `keep_from` (wall ms)."""
    for _part, wall_ms, duration, name in segments(uuid):
        if wall_ms + duration * 1000 < keep_from:
            try:
                os.remove(os.path.join(channel_dir(uuid), name))
            except OSError:
                pass


def _dir_size(folder):
    total = 0
    for name in os.listdir(folder):
        try:
            total += os.path.getsize(os.path.join(folder, name))
        except OSError:
            pass
    return total


def budget_bytes(conf):
    base = rewind_dir()
    try:
        free = shutil.disk_usage(base if os.path.isdir(base) else os.path.dirname(base)).free
    except OSError:
        free = 0
    used = sum(_dir_size(os.path.join(base, d)) for d in _channel_dirs())
    return int(min(conf["budget_gb"] * 1024 ** 3, used + max(0, free - DISK_MARGIN_BYTES)))


def _channel_dirs():
    base = rewind_dir()
    if not os.path.isdir(base):
        return []
    return [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]


def enforce_budget(conf, redis_client=None):
    """Over the budget, the oldest segments go -- first of channels nobody is paused on."""
    budget = budget_bytes(conf)
    base = rewind_dir()
    sizes = {d: _dir_size(os.path.join(base, d)) for d in _channel_dirs()}
    total = sum(sizes.values())
    if total <= budget:
        return 0
    redis_client = redis_client or _redis()
    paused = {d for d in sizes if any(e.get("paused_at") for e in viewers(d, redis_client).values())}
    removed = 0
    while total > budget:
        candidates = []
        for d in sizes:
            found = segments(d)
            if len(found) > 1:  # never the segment being written next to
                candidates.append((d in paused, found[0][1], d, found[0][3]))
        if not candidates:
            break
        _paused, _wall, d, name = min(candidates)
        path = os.path.join(base, d, name)
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            break
        total -= size
        sizes[d] -= size
        removed += 1
    if removed:
        logger.info(f"Rewind: over the budget, {removed} old segment(s) removed")
    return removed


def usage():
    """What is recorded now, for the settings page."""
    base = rewind_dir()
    out = []
    for d in _channel_dirs():
        found = segments(d)
        out.append({
            "channel_uuid": d, "bytes": _dir_size(os.path.join(base, d)),
            "minutes": round(sum(s[2] for s in found) / 60, 1),
            # A TV still watches and a recorder holds the lease: the recorder object and its
            # lease outlive ffmpeg by FILES_GRACE, which read as "recording" for five minutes
            # after the last viewer left (2026-10-02)
            "recording": bool(viewers(d)) and (d in _recorders or bool(_redis().get(_owner_key(d)))),
        })
    return out
