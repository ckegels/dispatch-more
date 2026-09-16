"""Where the time goes while a channel starts (apps.proxy.live_proxy.timing)."""

import time
from unittest.mock import patch

from django.test import TestCase

from apps.proxy.live_proxy import timing
from apps.proxy.live_proxy.constants import TS_SYNC_BYTE

from .test_probation import FakeRedis, _make_account, _reset_in_use_cache


def ts_packet(random_access=False, adaptation=True):
    """One 188-byte transport stream packet, optionally marked as a keyframe."""
    packet = bytearray(188)
    packet[0] = TS_SYNC_BYTE
    packet[1] = 0x01
    packet[2] = 0x00
    # adaptation field + payload, or payload only
    packet[3] = 0x30 if adaptation else 0x10
    if adaptation:
        packet[4] = 1  # adaptation field length
        packet[5] = 0x40 if random_access else 0x00
    return bytes(packet)


class KeyframeTests(TestCase):
    def test_a_keyframe_is_recognised(self):
        self.assertTrue(timing.find_keyframe(ts_packet(random_access=True)))
        self.assertTrue(
            timing.find_keyframe(ts_packet() + ts_packet(random_access=True) + ts_packet())
        )

    def test_data_without_a_keyframe(self):
        self.assertFalse(timing.find_keyframe(ts_packet() * 3))
        self.assertFalse(timing.find_keyframe(ts_packet(adaptation=False)))

    def test_anything_that_is_not_a_transport_stream_is_not_a_keyframe(self):
        # Never raises: a stream the provider sends in another format, or half a packet
        for data in (b"", b"not a transport stream", b"\x47" * 50, None, ts_packet()[:100]):
            self.assertFalse(timing.find_keyframe(data))


class ChannelStartTests(TestCase):
    """The phases are measured and logged once, and kept for the Diagnostics page."""

    UUID = "channel-1"

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        _make_account("timing", probation_enabled=True)

    def _start(self, seconds_ago=1.0):
        timing.start(self.redis, self.UUID, client="TiviMate/5.1.6 (Android 12)")
        # Pretend the request came in a moment ago, so the phases have a measurable length
        self.redis.hset(self.UUID_KEY, "requested", str(time.time() - seconds_ago))

    @property
    def UUID_KEY(self):
        return timing._key(self.UUID)

    def test_the_phases_are_measured_and_logged_once(self):
        self._start(seconds_ago=4.0)
        for phase in ("slot", "provider_connected", "first_byte"):
            timing.mark(self.redis, self.UUID, phase)
        self.assertTrue(timing.mark_keyframe(self.redis, self.UUID, ts_packet(random_access=True)))
        timing.mark(self.redis, self.UUID, "first_byte_out")

        with self.assertLogs("live_proxy", level="INFO") as logs:
            timing.finish(self.redis, self.UUID, "CNN")
        line = logs.output[0]
        self.assertIn("Channel start CNN", line)
        self.assertIn("slot", line)
        self.assertIn("first keyframe", line)
        self.assertIn("first byte to player", line)

        # A second client on the same channel does not log it again
        with patch.object(timing.logger, "info") as info:
            timing.finish(self.redis, self.UUID, "CNN")
            info.assert_not_called()

        (start,) = timing.recent_starts(self.redis)
        self.assertEqual(start["channel"], "CNN")
        self.assertIn("first keyframe=", start["phases"])
        self.assertEqual(start["client"], "TiviMate/5.1.6 (Android 12)")

    def test_the_slowest_phase_is_named(self):
        self._start(seconds_ago=0)
        now = time.time()
        self.redis.hset(self.UUID_KEY, "requested", str(now))
        # A quick connection, then a long wait for the first keyframe
        for phase, offset in (
            ("slot", 0.1),
            ("provider_connected", 0.2),
            ("first_byte", 0.3),
            ("first_keyframe", 3.9),
            ("first_byte_out", 4.0),
        ):
            self.redis.hset(self.UUID_KEY, phase, str(now + offset))

        with self.assertLogs("live_proxy", level="INFO") as logs:
            timing.finish(self.redis, self.UUID, "CNN")

        self.assertIn("slowest: first keyframe", logs.output[0])
        (start,) = timing.recent_starts(self.redis)
        self.assertEqual(start["slowest"], "first keyframe")
        self.assertEqual(start["total"], "4.00")

    def test_a_viewer_joining_a_running_channel_does_not_restart_the_clock(self):
        timing.start(self.redis, self.UUID, client="TiviMate")
        first = self.redis.hgetall(self.UUID_KEY)["requested"]
        timing.start(self.redis, self.UUID, client="Kodi")
        self.assertEqual(self.redis.hgetall(self.UUID_KEY)["requested"], first)

    def test_nothing_is_measured_for_a_channel_that_never_started(self):
        timing.mark(self.redis, self.UUID, "slot")
        self.assertEqual(self.redis.hgetall(self.UUID_KEY), {})
        with patch.object(timing.logger, "info") as info:
            timing.finish(self.redis, self.UUID, "CNN")
            info.assert_not_called()
        self.assertEqual(timing.recent_starts(self.redis), [])

    def test_the_keyframe_scan_stops_once_the_start_is_over(self):
        # No timing hash: the caller is told to stop looking straight away
        self.assertTrue(timing.mark_keyframe(self.redis, "gone", ts_packet()))

    def test_a_broken_redis_never_reaches_the_stream(self):
        class BrokenRedis:
            def __getattr__(self, _name):
                def explode(*_args, **_kwargs):
                    raise RuntimeError("redis is down")

                return explode

        broken = BrokenRedis()
        timing.start(broken, self.UUID)
        timing.mark(broken, self.UUID, "slot")
        self.assertTrue(timing.mark_keyframe(broken, self.UUID, ts_packet()))
        timing.finish(broken, self.UUID, "CNN")
        self.assertEqual(timing.recent_starts(broken), [])

    def test_only_the_last_starts_are_kept(self):
        for number in range(3):
            uuid = f"channel-{number}"
            timing.start(self.redis, uuid, client="TiviMate")
            self.redis.hset(timing._key(uuid), "requested", str(time.time() - 1))
            timing.mark(self.redis, uuid, "first_byte_out")
            timing.finish(self.redis, uuid, f"Channel {number}")

        starts = timing.recent_starts(self.redis)
        self.assertEqual(len(starts), 3)
        # Newest first
        self.assertEqual(starts[0]["channel"], "Channel 2")
