"""The "Could Not Dispatch" slate (live_proxy/slate.py): a channel on it looks for a real stream
every so often, goes back to the slate when that does not play, and is stopped after the limit."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from redis import Redis

from apps.proxy.live_proxy import slate

UUID = "slate-test-channel"
CHANNEL = SimpleNamespace(uuid=UUID, name="BE 24KITCHEN")
SLATE_STREAM, SLATE_PROFILE = 900, 90
REAL = [(SimpleNamespace(id=11, name="first"), SimpleNamespace(id=1)),
        (SimpleNamespace(id=12, name="second"), SimpleNamespace(id=2))]


class SlateTests(SimpleTestCase):
    def setUp(self):
        self.redis = Redis()
        self.clear()
        self.addCleanup(self.clear)
        self.patches = [
            mock.patch.object(slate, "settings", return_value=(True, 60, 15 * 60)),
            mock.patch.object(slate, "_current", return_value=(SLATE_STREAM, SLATE_PROFILE)),
            mock.patch.object(slate, "on_slate", return_value=True),
            mock.patch.object(slate, "candidates", return_value=REAL),
            mock.patch("apps.channels.models.Channel.objects.filter",
                       return_value=mock.Mock(first=lambda: CHANNEL)),
            mock.patch.object(slate, "_stop"),
            mock.patch.object(slate.gevent, "spawn"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def clear(self):
        for key in (slate.SINCE_KEY, slate.TRYING_KEY, slate.ENDED_KEY):
            Redis().delete(key.format(channel_uuid=UUID))

    def test_off_is_stock(self):
        with mock.patch.object(slate, "settings", return_value=(False, 60, 900)):
            self.assertEqual(slate.tick(self.redis, UUID, now=1000), "off")
        self.assertFalse(self.redis.exists(slate.SINCE_KEY.format(channel_uuid=UUID)))

    def test_a_real_stream_is_tried_every_retry_seconds_in_turn(self):
        self.assertEqual(slate.tick(self.redis, UUID, now=1000), "landed")
        self.assertEqual(slate.tick(self.redis, UUID, now=1030), "waiting")
        self.assertEqual(slate.tick(self.redis, UUID, now=1061), "trying")
        first = slate.gevent.spawn.call_args.args
        self.assertEqual((first[3].id, first[5]), (11, SLATE_STREAM))
        # One try at a time
        self.assertEqual(slate.tick(self.redis, UUID, now=1122), "trying")
        self.assertEqual(slate.gevent.spawn.call_count, 1)
        self.redis.delete(slate.TRYING_KEY.format(channel_uuid=UUID))
        slate.tick(self.redis, UUID, now=1183)
        self.assertEqual(slate.gevent.spawn.call_args.args[3].id, 12)

    def test_the_limit_stops_the_channel_and_a_quick_return_is_stopped_at_once(self):
        slate.tick(self.redis, UUID, now=1000)
        self.assertEqual(slate.tick(self.redis, UUID, now=1000 + 15 * 60), "stopped")
        slate._stop.assert_called_once_with(UUID)
        # The TV reconnects and the channel ends on the slate again
        self.assertEqual(slate.tick(self.redis, UUID, now=2000), "stopped again")

    def test_no_limit_when_zero(self):
        with mock.patch.object(slate, "settings", return_value=(True, 60, 0)):
            slate.tick(self.redis, UUID, now=1000)
            self.redis.set(slate.TRYING_KEY.format(channel_uuid=UUID), "1")
            self.assertEqual(slate.tick(self.redis, UUID, now=1000 + 86400), "trying")
        slate._stop.assert_not_called()

    def test_off_the_slate_forgets_it(self):
        slate.tick(self.redis, UUID, now=1000)
        with mock.patch.object(slate, "on_slate", return_value=False):
            self.assertEqual(slate.tick(self.redis, UUID, now=1010), "not on the slate")
        self.assertFalse(self.redis.exists(slate.SINCE_KEY.format(channel_uuid=UUID)))

    def test_nothing_free_tries_nothing(self):
        slate.tick(self.redis, UUID, now=1000)
        with mock.patch.object(slate, "candidates", return_value=[]):
            self.assertEqual(slate.tick(self.redis, UUID, now=1061), "nothing free")
        slate.gevent.spawn.assert_not_called()
        self.assertFalse(self.redis.exists(slate.TRYING_KEY.format(channel_uuid=UUID)))


class TryTests(SimpleTestCase):
    def setUp(self):
        self.redis = Redis()
        self.redis.hset(slate.SINCE_KEY.format(channel_uuid=UUID), "since", 1)
        self.addCleanup(self.redis.delete, slate.SINCE_KEY.format(channel_uuid=UUID))

    def test_a_stream_that_plays_ends_the_slate(self):
        with mock.patch("apps.timeshift.priority._switch", return_value=True) as switch, \
             mock.patch("apps.timeshift.priority.verify", return_value=True):
            self.assertTrue(slate.try_stream(self.redis, CHANNEL, *REAL[0], SLATE_STREAM, SLATE_PROFILE))
        switch.assert_called_once()
        self.assertFalse(self.redis.exists(slate.SINCE_KEY.format(channel_uuid=UUID)))

    def test_a_stream_that_does_not_play_goes_back_to_the_slate(self):
        with mock.patch("apps.timeshift.priority._switch", return_value=True) as switch, \
             mock.patch("apps.timeshift.priority.verify", return_value=False), \
             mock.patch.object(slate, "_current", return_value=(11, 1)), \
             mock.patch("apps.channels.models.Stream.objects.filter",
                        return_value=mock.Mock(first=lambda: SimpleNamespace(id=SLATE_STREAM))), \
             mock.patch("apps.m3u.models.M3UAccountProfile.objects.filter",
                        return_value=mock.Mock(first=lambda: SimpleNamespace(id=SLATE_PROFILE))):
            self.assertFalse(slate.try_stream(self.redis, CHANNEL, *REAL[0], SLATE_STREAM, SLATE_PROFILE))
        self.assertEqual(switch.call_args.args[1].id, SLATE_STREAM)
        self.assertTrue(self.redis.exists(slate.SINCE_KEY.format(channel_uuid=UUID)))

    def test_failover_that_moved_on_is_left_alone(self):
        with mock.patch("apps.timeshift.priority._switch", return_value=True) as switch, \
             mock.patch("apps.timeshift.priority.verify", return_value=False), \
             mock.patch.object(slate, "_current", return_value=(SLATE_STREAM, SLATE_PROFILE)):
            self.assertFalse(slate.try_stream(self.redis, CHANNEL, *REAL[0], SLATE_STREAM, SLATE_PROFILE))
        switch.assert_called_once()


class OnSlateTests(SimpleTestCase):
    def channel(self, rows):
        streams = mock.Mock()
        streams.order_by.return_value.values_list.return_value = rows
        return SimpleNamespace(streams=streams)

    def test_the_last_custom_stream_after_real_ones(self):
        self.assertTrue(slate.on_slate(self.channel([(1, False), (9, True)]), 9))
        self.assertFalse(slate.on_slate(self.channel([(1, False), (9, True)]), 1))

    def test_a_channel_of_own_streams_only_is_not_on_a_slate(self):
        self.assertFalse(slate.on_slate(self.channel([(8, True), (9, True)]), 9))
