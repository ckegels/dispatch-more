"""A stream that keeps stalling is left for the channel's next stream (Dispatch More).

Settings → Proxy, "Leave a Stream That Keeps Stalling" (leave_stalling_streams), off by default.

Stock moves a channel on only when its stream stays silent through three health checks in a
row. A stream that stops for 10-15 s and then comes back resets that count every time, so the
channel stays on it for as long as it keeps doing that. On the user's server (2026-10-17) a
recording of Saturday Night Live failed over to such a stream 15 minutes in: it stalled for
about 11 s every 20-40 s until the end, and the recording lost 21 of its 44 minutes of sound.
TF1 stalled the same way twice in three minutes for a viewer the same day.

With the setting on, the health monitor counts the stalls (each time the stream goes
"unhealthy": no data for the connection timeout, 10 s). STALL_COUNT of them within
STALL_WINDOW_SECONDS, and the channel moves to its next stream, the way a failover does: the
viewers and a recording keep their connection, the order is the failover's own (arrTV's quality
away from home included, see app_devices.failover_order).

Only to a real stream not tried yet: never to the custom fallback ("Could Not Dispatch"), which
is worse than a picture that stops now and then, and never round again -- once every other
stream was tried, the channel stays where it is.
"""

import logging
from collections import deque

logger = logging.getLogger("live_proxy")

SETTING = "leave_stalling_streams"
# This many stalls ...
STALL_COUNT = 3
# ... within this long, and the stream is left
STALL_WINDOW_SECONDS = 5 * 60


def enabled():
    try:
        from apps.proxy.config import TSConfig

        return bool(TSConfig.get_proxy_settings().get(SETTING, False))
    except Exception:
        return False


class StallCounter:
    """The stalls of the stream a channel is on now."""

    def __init__(self, count=STALL_COUNT, window=STALL_WINDOW_SECONDS):
        self.count = count
        self.window = window
        self.stalls = deque()

    def stalled(self, now):
        """A stall began at now: True when that makes the stream one to leave."""
        self.stalls.append(now)
        while self.stalls and now - self.stalls[0] > self.window:
            self.stalls.popleft()
        return len(self.stalls) >= self.count

    def clear(self):
        self.stalls.clear()


def real_alternative(channel_id, current_stream_id, tried_stream_ids):
    """
    Whether the channel has another stream to move to that is not the custom fallback and was
    not tried yet, in the failover's order; the stream id of the first one, or None.
    """
    try:
        from .url_utils import get_alternate_streams

        untried = [
            a for a in get_alternate_streams(channel_id, current_stream_id)
            if a["stream_id"] not in tried_stream_ids
        ]
        real = real_streams(untried)
        return real[0]["stream_id"] if real else None
    except Exception as e:
        logger.debug(f"Stalling stream: could not look for another stream for {channel_id}: {e}")
        return None


def real_streams(entries):
    """The failover's entries ({"stream_id": ...}) that are not the custom fallback, in order."""
    try:
        from apps.channels.models import Stream

        ids = [e["stream_id"] for e in entries]
        real = set(Stream.objects.filter(id__in=ids, is_custom=False).values_list("id", flat=True))
        return [e for e in entries if e["stream_id"] in real]
    except Exception as e:
        logger.debug(f"Stalling stream: could not tell the fallback apart: {e}")
        return []
