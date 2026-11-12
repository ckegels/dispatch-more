"""A provider account refusing every connection is tried last until it sends data (refusing.py).

Measured on 2026-11-12: both TiviBridge accounts refused every stream while their playlists
refreshed, and each channel start spent 12 seconds asking them before the account that works.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.channels.models import Channel, ChannelStream, Stream
from apps.proxy.live_proxy import probation, refusing
from apps.proxy.live_proxy.tests.test_probation import FakeRedis, _make_account, _reset_in_use_cache


class RefusingTests(TestCase):
    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.bridge, self.bridge_profile = _make_account("TiviBridge", probation_enabled=True)
        self.works, self.works_profile = _make_account("Digitalizard", probation_enabled=True)
        self.hd = Stream.objects.create(name="BBC 2 HD", url="http://t.example/1.ts", m3u_account=self.bridge)
        self.fhd = Stream.objects.create(name="BBC 2 FHD", url="http://t.example/2.ts", m3u_account=self.bridge)
        self.good = Stream.objects.create(name="BBC 2", url="http://d.example/1.ts", m3u_account=self.works)
        patcher = patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_streams_refused_mark_the_account(self):
        self.assertFalse(refusing.note_refused(self.redis, self.hd.id))
        self.assertEqual(refusing.marked_accounts(self.redis), set())
        self.assertTrue(refusing.note_refused(self.redis, self.fhd.id))
        self.assertEqual(refusing.marked_accounts(self.redis), {self.bridge.id})
        # ...and once marked, every refusal gives the stream up at once
        self.assertTrue(refusing.note_refused(self.redis, self.hd.id))
        events = [e for e in probation.recent_events(self.redis) if e["account"] == "TiviBridge"]
        self.assertEqual([e["action"] for e in events], ["provider refusing every connection"])

    def test_one_dead_stream_does_not_mark_its_account(self):
        for _ in range(5):
            self.assertFalse(refusing.note_refused(self.redis, self.hd.id))
        self.assertEqual(refusing.marked_accounts(self.redis), set())

    def test_data_clears_the_mark_and_the_count(self):
        refusing.note_refused(self.redis, self.hd.id)
        refusing.note_refused(self.redis, self.fhd.id)
        refusing.note_served(self.redis, self.fhd.id)
        self.assertEqual(refusing.marked_accounts(self.redis), set())
        # The count started again: one more refused stream is not two
        self.assertFalse(refusing.note_refused(self.redis, self.hd.id))
        self.assertIn("provider sends again", [e["action"] for e in probation.recent_events(self.redis)])

    def test_data_from_another_account_clears_nothing(self):
        refusing.note_refused(self.redis, self.hd.id)
        refusing.note_refused(self.redis, self.fhd.id)
        refusing.note_served(self.redis, self.good.id)
        self.assertEqual(refusing.marked_accounts(self.redis), {self.bridge.id})

    def test_a_mark_ends_by_itself(self):
        self.redis.hset(refusing.MARKS_KEY, self.bridge.id, 1.0)
        self.assertEqual(refusing.marked_accounts(self.redis), set())

    def test_accounts_without_the_overlap_are_left_as_stock(self):
        self.bridge.custom_properties = {"probation_enabled": False}
        self.bridge.save()
        for stream in (self.hd, self.fhd, self.hd):
            self.assertFalse(refusing.note_refused(self.redis, stream.id))
        self.assertEqual(refusing.marked_accounts(self.redis), set())
        with patch.object(probation, "in_use", return_value=False):
            self.assertFalse(refusing.note_refused(self.redis, self.hd.id))
            refusing.note_served(self.redis, self.hd.id)
        self.assertEqual((self.redis.hashes, self.redis.sets), ({}, {}))

    def test_data_costs_one_lookup_when_nothing_was_refused(self):
        with patch.object(probation, "stream_account") as lookup:
            refusing.note_served(self.redis, self.good.id)
        lookup.assert_not_called()


class LastTests(TestCase):
    def setUp(self):
        self.redis = FakeRedis()

    def order(self, items, marked):
        with patch.object(refusing, "marked_accounts", return_value=marked):
            return refusing.last(self.redis, items, lambda s: s.account, lambda s: s.custom)

    def test_marked_streams_go_after_the_others_and_before_the_fallback(self):
        streams = [
            SimpleNamespace(name=n, account=a, custom=c)
            for n, a, c in [("tb2 HD", 12, False), ("tb HD", 14, False), ("dz FHD", 15, False),
                            ("dz HD", 15, False), ("Could Not Dispatch", 1, True)]
        ]
        self.assertEqual(
            [s.name for s in self.order(streams, {12, 14})],
            ["dz FHD", "dz HD", "tb2 HD", "tb HD", "Could Not Dispatch"],
        )

    def test_nothing_marked_is_the_same_list(self):
        streams = [SimpleNamespace(account=12, custom=False)]
        self.assertIs(self.order(streams, set()), streams)

    def test_a_custom_stream_in_the_middle_keeps_its_place(self):
        streams = [
            SimpleNamespace(name=n, account=a, custom=c)
            for n, a, c in [("tb", 12, False), ("slate", 1, True), ("dz", 15, False)]
        ]
        self.assertEqual([s.name for s in self.order(streams, {12})], ["slate", "dz", "tb"])


@patch("apps.channels.models.Channel._pick_channel_to_preempt", return_value=None)
class GetStreamTests(TestCase):
    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.bridge, _ = _make_account("TiviBridge", probation_enabled=True)
        self.works, self.works_profile = _make_account("Digitalizard", probation_enabled=True)
        self.refused = Stream.objects.create(name="BBC 2 HD", url="http://t.example/1.ts", m3u_account=self.bridge)
        self.good = Stream.objects.create(name="BBC 2", url="http://d.example/1.ts", m3u_account=self.works)
        self.channel = Channel.objects.create(channel_number=901, name="BBC 2")
        ChannelStream.objects.create(channel=self.channel, stream=self.refused, order=0)
        ChannelStream.objects.create(channel=self.channel, stream=self.good, order=1)
        for target in ("apps.channels.models.RedisClient.get_client", "core.utils.RedisClient.get_client"):
            patcher = patch(target, return_value=self.redis)
            patcher.start()
            self.addCleanup(patcher.stop)
        stream_profile = MagicMock()
        stream_profile.is_redirect.return_value = False
        patcher = patch.object(Channel, "get_stream_profile", return_value=stream_profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_channel_starts_on_the_account_that_works(self, _preempt):
        self.redis.hset(refusing.MARKS_KEY, self.bridge.id, 9e12)
        stream_id, profile_id, error, _reserved = self.channel.get_stream()
        self.assertEqual((stream_id, profile_id, error), (self.good.id, self.works_profile.id, None))

    def test_unmarked_it_is_the_channels_own_order(self, _preempt):
        stream_id, _profile_id, _error, _reserved = self.channel.get_stream()
        self.assertEqual(stream_id, self.refused.id)

    def test_a_marked_account_still_plays_when_the_others_are_full(self, _preempt):
        from apps.m3u.connection_pool import profile_connections_key

        self.redis.hset(refusing.MARKS_KEY, self.bridge.id, 9e12)
        self.redis.set(profile_connections_key(self.works_profile.id), 1)
        stream_id, _profile_id, _error, _reserved = self.channel.get_stream()
        self.assertEqual(stream_id, self.refused.id)


class StreamManagerTests(TestCase):
    """A refused connection to a marked account gives the stream up at once."""

    def _run(self, refused_by_account):
        from apps.proxy.live_proxy.input.manager import StreamManager
        from apps.proxy.live_proxy.tests.test_health_reconnect import _make_manager

        sm = _make_manager(max_retries=3)
        attempts = []
        sleeps = []

        def fake_establish():
            attempts.append(1)
            sm.connected = True
            return True

        def fake_process():
            sm.connected = False

        def fake_try_next(**kwargs):
            sm.running = False
            return False

        with patch.object(StreamManager, "_monitor_health"), \
                patch.object(StreamManager, "_ensure_owner_or_stop", return_value=True), \
                patch.object(StreamManager, "_close_all_connections"), \
                patch.object(StreamManager, "_try_next_stream", side_effect=fake_try_next), \
                patch("apps.proxy.live_proxy.input.manager.close_old_connections"), \
                patch.object(sm, "_establish_http_connection", side_effect=fake_establish), \
                patch.object(sm, "_process_stream_data", side_effect=fake_process), \
                patch.object(sm, "_sleep_interruptible", side_effect=lambda s: sleeps.append(s) or True), \
                patch("apps.proxy.live_proxy.input.manager.gevent.sleep", side_effect=sleeps.append), \
                patch("apps.proxy.live_proxy.input.manager.log_system_event"), \
                patch.object(probation, "refusal_retry_delay", side_effect=lambda _s, f, _r: 1.5 * f), \
                patch.object(refusing, "note_refused", return_value=refused_by_account) as noted:
            sm.run()
        return attempts, sleeps, noted

    def test_one_attempt_and_no_wait(self):
        attempts, sleeps, noted = self._run(refused_by_account=True)
        self.assertEqual((len(attempts), sleeps), (1, []))
        noted.assert_called_once()

    def test_otherwise_the_attempts_and_waits_are_as_before(self):
        attempts, sleeps, _noted = self._run(refused_by_account=False)
        self.assertEqual((len(attempts), sleeps), (3, [1.5, 3.0]))
