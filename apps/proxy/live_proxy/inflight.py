"""
What each web worker and Celery worker is doing, for the memory recorder (memory_recorder.py).

On 2026-10-20 a web worker sat at 6 GB and nothing could say which requests had been running in
it: by then it answered nothing, and the request log only lists requests that finished. Every
process now keeps its requests (and Celery tasks) in flight in memory, and a background greenlet
writes them every WRITE_EVERY seconds to a small file of its own in /dev/shm
(<pid>.json: the oldest first, with how long each has been running, and the paths finished since
the last write). The recorder copies them into its history.

A worker that is stuck stops writing: its file's age is then the sign, and its last contents
are what it was doing when it stopped. Streams count as in flight until the stream ends.

Does nothing unless the memory recorder is switched on (its flag file), checked every
FLAG_EVERY seconds; switched off, the middleware costs one attribute read per request.
"""

import json
import os
import re
import threading
import time

from .memory_recorder import DIR as HISTORY_DIR, FLAG, INFLIGHT_DIR

WRITE_EVERY = 5
FLAG_EVERY = 30
SHOWN = 40

_ID = re.compile(r"/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+)(?=/|$)")


def short_path(path):
    """/proxy/ts/stream/<uuid> and /api/x/12/ counted as one path each."""
    return _ID.sub("/<id>", path or "")[:120]


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.done = {}
        self.kind = "web"
        self.enabled = False
        self.checked = 0.0
        self.writer = None
        self.writer_pid = None


_state = _State()


def enabled():
    now = time.monotonic()
    if now - _state.checked > FLAG_EVERY:
        _state.checked = now
        _state.enabled = os.path.exists(os.path.join(os.environ.get("MEMORY_HISTORY_DIR") or HISTORY_DIR, FLAG))
    return _state.enabled


def _write():
    folder = os.environ.get("MEMORY_INFLIGHT_DIR") or INFLIGHT_DIR
    now = time.time()
    with _state.lock:
        active = sorted(_state.active.values(), key=lambda r: r[3])
        done = sorted(_state.done.items(), key=lambda kv: -kv[1][0])[:20]
        _state.done = {}
    record = {
        "at": int(now),
        "kind": _state.kind,
        "n": len(active),
        "req": [[m, p, c, round(now - t0, 1)] for m, p, c, t0 in active[:SHOWN]],
        "done": {path: [count, round(longest, 1)] for path, (count, longest) in done},
    }
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, f"{os.getpid()}.json")
    temporary = f"{target}.tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(record, fh, separators=(",", ":"))
    os.replace(temporary, target)


def _writer_loop():
    while True:
        time.sleep(WRITE_EVERY)
        if not enabled():
            continue
        try:
            _write()
        except OSError:
            pass


def _ensure_writer(kind):
    _state.kind = kind
    if _state.writer is not None and _state.writer.is_alive() and _state.writer_pid == os.getpid():
        return
    with _state.lock:
        if _state.writer is not None and _state.writer.is_alive() and _state.writer_pid == os.getpid():
            return
        # A forked child (uWSGI workers, Celery children) starts its own writer
        _state.active = {}
        _state.done = {}
        _state.writer_pid = os.getpid()
        _state.writer = threading.Thread(target=_writer_loop, name="inflight-writer", daemon=True)
        _state.writer.start()



def begin(key, method, path, client):
    _ensure_writer(_state.kind)
    with _state.lock:
        _state.active[key] = (method, path, client, time.time())


def end(key):
    with _state.lock:
        entry = _state.active.pop(key, None)
        if entry is None:
            return
        took = time.time() - entry[3]
        path = short_path(entry[1])
        count, longest = _state.done.get(path, (0, 0.0))
        _state.done[path] = (count + 1, max(longest, took))


def _client(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", ""))[:45]


class InflightMiddleware:
    """Keeps this worker's requests in flight (see the module docstring)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not enabled():
            return self.get_response(request)
        key = id(request)
        begin(key, request.method, request.path[:200], _client(request))
        try:
            response = self.get_response(request)
        except BaseException:
            end(key)
            raise
        if getattr(response, "streaming", False):
            response.streaming_content = _until_done(response.streaming_content, key)
        else:
            end(key)
        return response


def _until_done(content, key):
    """A stream stays in flight until its last chunk went out (or the client left)."""
    try:
        yield from content
    finally:
        end(key)


def task_started(task_id=None, task=None, **_):
    if not enabled():
        return
    _ensure_writer("celery")
    name = getattr(task, "name", None) or "task"
    begin(f"task:{task_id}", "TASK", name, "")


def task_finished(task_id=None, **_):
    end(f"task:{task_id}")


def connect_celery():
    """Called from the app config: Celery's task signals feed the same record."""
    try:
        from celery.signals import task_postrun, task_prerun
    except ImportError:
        return
    task_prerun.connect(task_started, weak=False, dispatch_uid="dispatch-more-inflight-start")
    task_postrun.connect(task_finished, weak=False, dispatch_uid="dispatch-more-inflight-end")
