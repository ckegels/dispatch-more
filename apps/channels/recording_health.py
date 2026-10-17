"""A recording's health, checked when it ends (Dispatch More; DVR settings, off by default).

The user (2026-10-17): "after a show is recorded it needs a health check". On their server a
recording of Saturday Night Live was marked completed, and its sound was missing for 21 of its
44 minutes: 15 minutes in, the provider dropped the channel, failover moved it to another
stream, and that one stalled for 11 s every 20-40 s until the end -- each stall a hole in the
file. Nothing said so; the recording looked like any other.

The check reads the finished file with ffprobe:

- its streams: a recording without a picture or without sound is no recording;
- its length, against the scheduled one (start to end);
- the timestamp of every packet, picture and sound apart: a step of more than GAP_SECONDS
  forward, or back more than BACK_SECONDS, is a hole (or a jump), and how much is missing where.

It reads the file once, at disk speed, decoding nothing. The result is kept with the recording
(custom_properties.health) and shown on the DVR page: "ok", "damaged" (something missing) or
"failed" (no picture, unreadable, or not half as long as it should be), with what is wrong in
plain words ("minor" when only a few seconds are missing). It runs before Comskip: cutting commercials out of a file would look like holes.
"""

import json
import logging
import subprocess
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# A packet more than this after the one before it on the same stream: a hole
GAP_SECONDS = 1.0
# A packet this far before the one before it: the timestamps jumped back
BACK_SECONDS = 0.5
# Missing more than this on picture or sound -- and more than DAMAGED_SHARE of the recording -- is
# damage; less is "minor": a reconnect's few seconds in a two-hour match leave it watchable
DAMAGED_MISSING_SECONDS = 30.0
DAMAGED_SHARE = 0.01
# Missing more than this at all is worth a mention ("minor")
MINOR_MISSING_SECONDS = 3.0
# Shorter than scheduled by more than this (and more than SHORT_SHARE of it): damaged
SHORT_SECONDS = 60.0
SHORT_SHARE = 0.05
# Not even this share of the scheduled length: failed
FAILED_SHARE = 0.5
# The problems listed, at most, so a badly broken recording does not fill the page
MAX_PROBLEMS = 6
FFPROBE_TIMEOUT = 30 * 60


def clock(seconds):
    """1:02:03, or 2:03 under an hour."""
    seconds = int(round(max(0.0, seconds)))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _span(seconds):
    """'7 s', '3 min 20 s', '21 min'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    m, s = divmod(seconds, 60)
    return f"{m} min {s} s" if s and m < 10 else f"{m} min"


class _Stream:
    def __init__(self, kind):
        self.kind = kind
        self.last = None
        self.first = None
        self.packets = 0
        self.gaps = 0
        self.missing = 0.0
        self.largest = 0.0
        self.first_gap_at = None
        self.jumps_back = 0

    def packet(self, pts):
        self.packets += 1
        if self.first is None:
            self.first = pts
        if self.last is not None:
            step = pts - self.last
            if step > GAP_SECONDS:
                self.gaps += 1
                self.missing += step
                self.largest = max(self.largest, step)
                if self.first_gap_at is None:
                    self.first_gap_at = self.last
            elif step < -BACK_SECONDS:
                self.jumps_back += 1
                if self.first_gap_at is None:
                    self.first_gap_at = self.last
        self.last = pts

    def summary(self):
        return {
            "packets": self.packets,
            "gaps": self.gaps,
            "missing_secs": round(self.missing, 1),
            "largest_secs": round(self.largest, 1),
            "first_gap_at_secs": None if self.first_gap_at is None else round(self.first_gap_at, 1),
            "jumps_back": self.jumps_back,
        }


def analyse(streams, packets, duration_secs, expected_secs):
    """
    The health of a recording, from what ffprobe said: streams (its codec_type list), packets
    ((stream index, pts seconds) in file order) and the file's duration; expected_secs is the
    scheduled length (None when not known).
    """
    kinds = {i: s.get("codec_type") for i, s in enumerate(streams)}
    video_index = next((i for i, k in kinds.items() if k == "video"), None)
    audio_index = next((i for i, k in kinds.items() if k == "audio"), None)
    video, audio = _Stream("video"), _Stream("audio")
    for index, pts in packets:
        if index == video_index:
            video.packet(pts)
        elif index == audio_index:
            audio.packet(pts)

    problems = []
    status = "ok"

    def worse(to):
        nonlocal status
        order = ("ok", "minor", "damaged", "failed")
        if order.index(to) > order.index(status):
            status = to

    if video_index is None or video.packets == 0:
        problems.append("No picture in the file")
        worse("failed")
    if audio_index is None or audio.packets == 0:
        problems.append("No sound in the file")
        worse("damaged")
    for stream, what in ((video, "Picture"), (audio, "Sound")):
        if stream.gaps:
            share = f" of {_span(duration_secs)}" if duration_secs else ""
            problems.append(
                f"{what} missing for {_span(stream.missing)}{share}"
                f" ({stream.gaps} {'hole' if stream.gaps == 1 else 'holes'}, the first at {clock(stream.first_gap_at)},"
                f" the longest {_span(stream.largest)})"
            )
            share_missing = stream.missing / duration_secs if duration_secs else 1.0
            if stream.missing > DAMAGED_MISSING_SECONDS and share_missing > DAMAGED_SHARE:
                worse("damaged")
            elif stream.missing > MINOR_MISSING_SECONDS:
                worse("minor")
        if stream.jumps_back:
            problems.append(
                f"{what} timestamps jump back {stream.jumps_back} {'time' if stream.jumps_back == 1 else 'times'}"
                f", the first at {clock(stream.first_gap_at)}"
            )
            worse("damaged")
    if expected_secs and duration_secs is not None:
        short = expected_secs - duration_secs
        if duration_secs < expected_secs * FAILED_SHARE:
            problems.append(f"Only {_span(duration_secs)} of the {_span(expected_secs)} scheduled")
            worse("failed")
        elif short > SHORT_SECONDS and short > expected_secs * SHORT_SHARE:
            problems.append(f"{_span(short)} shorter than the {_span(expected_secs)} scheduled")
            worse("damaged")

    return {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "duration_secs": None if duration_secs is None else round(duration_secs, 1),
        "expected_secs": None if expected_secs is None else round(expected_secs, 1),
        "video": video.summary(),
        "audio": audio.summary(),
        "problems": problems[:MAX_PROBLEMS],
    }


def _probe_streams(path):
    """(streams, duration seconds) of the file, or raises."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type",
         "-of", "json", path],
        capture_output=True, text=True, timeout=120, check=True,
    ).stdout
    data = json.loads(out or "{}")
    streams = sorted(data.get("streams") or [], key=lambda s: s.get("index", 0))
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return streams, duration


def _packets(path):
    """(stream index, pts seconds) for every packet with a timestamp, read as ffprobe goes."""
    proc = subprocess.Popen(
        ["ffprobe", "-v", "error", "-show_entries", "packet=stream_index,pts_time",
         "-of", "csv=p=0", path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 16,
    )
    started = time.monotonic()
    try:
        for line in proc.stdout:
            if time.monotonic() - started > FFPROBE_TIMEOUT:
                raise TimeoutError("ffprobe took too long")
            index, _, pts = line.strip().partition(",")
            if not pts or pts == "N/A":
                continue
            try:
                yield int(index), float(pts)
            except ValueError:
                continue
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _expected_secs(recording):
    try:
        return max(0.0, (recording.end_time - recording.start_time).total_seconds())
    except Exception:
        return None


def check_file(path, expected_secs):
    """The health of the file at path (see analyse); "failed" when it cannot be read."""
    try:
        streams, duration = _probe_streams(path)
    except Exception as e:
        return {
            "status": "failed",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "problems": [f"The file could not be read ({type(e).__name__})"],
        }
    return analyse(streams, _packets(path), duration, expected_secs)


def check_recording(recording_id):
    """Check one recording's file and keep the answer with it; never raises."""
    import os

    from .models import Recording

    try:
        recording = Recording.objects.filter(id=recording_id).first()
        if recording is None:
            return None
        cp = recording.custom_properties or {}
        path = cp.get("file_path")
        if not path or not os.path.exists(path):
            health = {
                "status": "failed",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "problems": ["The recording has no file"],
            }
        else:
            health = check_file(path, _expected_secs(recording))
        # Read again: Comskip or the recorder may have written to it meanwhile
        recording.refresh_from_db()
        cp = recording.custom_properties or {}
        cp["health"] = health
        recording.custom_properties = cp
        recording.save(update_fields=["custom_properties"])
        logger.info(
            f"Recording {recording_id} health: {health['status']}"
            + (f" -- {'; '.join(health['problems'])}" if health.get("problems") else "")
        )
        try:
            from core.utils import send_websocket_update

            send_websocket_update("updates", "update", {"type": "recording_updated", "recording_id": recording_id})
        except Exception:
            pass
        return health
    except Exception as e:
        logger.warning(f"Recording {recording_id}: health check failed: {e}", exc_info=True)
        return None
