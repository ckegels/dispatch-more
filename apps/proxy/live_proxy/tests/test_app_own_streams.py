"""What arrTV can play: its decoding limit, a stream of its own, and failover within it."""

from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.proxy.live_proxy import app_devices, app_own_streams, probation
from apps.proxy.live_proxy.redis_keys import RedisKeys
from apps.proxy.live_proxy.tests.test_probation import FakeRedis

CHROMECAST = "app|1|chromecast-0001"


def _reset(test):
    app_devices._HELD.update(at=0.0, value=None)
    test.addCleanup(app_devices._HELD.update, at=0.0, value=None)


class DecodingLimitTests(TestCase):
    def setUp(self):
        _reset(self)

    def said(self, value):
        request = RequestFactory().get("/proxy/ts/stream/x", HTTP_X_DISPATCH_MAX_VIDEO=value)
        return app_devices.declared_max_quality(request)

    def test_read_only_with_devices_on(self):
        self.assertEqual(self.said("1080"), "")
        app_devices.save_settings({"devices": True})
        self.assertEqual(self.said("1080"), "FHD")

    def test_a_height_or_a_quality(self):
        app_devices.save_settings({"devices": True})
        self.assertEqual(self.said("2160"), "")  # 4K plays: no limit
        self.assertEqual(self.said("1080p"), "FHD")
        self.assertEqual(self.said("720"), "HD")
        self.assertEqual(self.said("576"), "SD")
        self.assertEqual(self.said("hd"), "HD")
        self.assertEqual(self.said("lots"), "")

    def test_it_is_the_strictest_limit_that_counts(self):
        app_devices.save_settings({"devices": True, "home_networks": "192.168.2.0/24", "outside_max_quality": "HD"})
        at_home = probation.Viewer("192.168.2.40", 1, app="okhttp", server_device=CHROMECAST, max_quality="FHD")
        away = probation.Viewer("192.168.65.3", 1, app="okhttp", server_device=CHROMECAST, max_quality="FHD")
        self.assertEqual(app_devices.quality_limit_for(at_home), "FHD")
        self.assertEqual(app_devices.quality_limit_for(away), "HD")


class OwnStreamTests(TestCase):
    """RTL ZWEI plays 4K for somebody at home; the Chromecast HD cannot decode 4K."""

    def setUp(self):
        from apps.channels.models import Channel, ChannelStream, Stream
        from apps.m3u.models import M3UAccount, M3UAccountProfile

        _reset(self)
        app_devices.save_settings({"devices": True, "own_stream": True})
        self.one = M3UAccount.objects.create(name="TiviBridge", account_type="XC", server_url="http://a")
        self.two = M3UAccount.objects.create(name="Digitalizard.com", account_type="XC", server_url="http://b")
        for account in (self.one, self.two):
            if not account.profiles.exists():
                M3UAccountProfile.objects.create(m3u_account=account, name="default", is_default=True, max_streams=1)
        self.channel = Channel.objects.create(name="┃DE┃ RTL ZWEI", channel_number=2)
        made = [
            ("┃DE┃ RTL ZWEI 4K", self.one), ("┃DE┃ RTL ZWEI FHD", self.one),
            ("DE| RTL ZWEI FHD", self.two), ("DE| RTL ZWEI HD", self.two),
        ]
        self.streams = {
            # A playlist gives every stream its hash; a stream of its own is run by it
            name: Stream.objects.create(name=name, url=f"http://x/{i}", m3u_account=account, stream_hash=f"hash{i}")
            for i, (name, account) in enumerate(made)
        }
        self.fallback = Stream.objects.create(name="Could Not Dispatch", url="http://local/f", is_custom=True)
        for position, stream in enumerate(list(self.streams.values()) + [self.fallback]):
            ChannelStream.objects.create(channel=self.channel, stream=stream, order=position)
        self.redis = FakeRedis()
        self.playing("┃DE┃ RTL ZWEI 4K")
        self.free = {self.one.id: True, self.two.id: True}
        capacity = mock.patch(
            "apps.m3u.connection_pool.pool_has_capacity_for_profile",
            side_effect=lambda profile, redis_client, viewer=None: self.free[profile.m3u_account_id],
        )
        capacity.start()
        self.addCleanup(capacity.stop)
        events = mock.patch("apps.proxy.live_proxy.health.record_event")
        events.start()
        self.addCleanup(events.stop)

    def playing(self, name):
        stream = self.streams[name]
        self.redis.hset(RedisKeys.channel_metadata(str(self.channel.uuid)), "stream_id", stream.id)
        self.redis.set(f"stream_profile:{stream.id}", 1)

    def chromecast(self, **given):
        return probation.Viewer(
            "192.168.2.50", 1, app="AerioTV/-arr.", server_device=CHROMECAST, **{"max_quality": "FHD", **given}
        )

    def own(self, viewer=None):
        found = app_own_streams.own_stream_for(self.redis, viewer or self.chromecast(), self.channel)
        return found.name if found else None

    def test_another_providers_stream_it_can_play(self):
        # The FHD of the account the channel is on would do too; another provider's first
        self.assertEqual(self.own(), "DE| RTL ZWEI FHD")
        # And it is remembered, so what the device says about the channel reaches it
        stream = self.streams["DE| RTL ZWEI FHD"]
        self.redis.hset(f"live:channel:{stream.stream_hash}:metadata", "state", "active")
        self.assertEqual(
            app_own_streams.session_for(self.redis, self.chromecast(), str(self.channel.uuid)), stream.stream_hash
        )
        # A reconnect goes back to the same stream
        self.assertEqual(self.own(), "DE| RTL ZWEI FHD")

    def test_one_already_running_costs_nothing_and_comes_first(self):
        self.redis.set(f"stream_profile:{self.streams['┃DE┃ RTL ZWEI FHD'].id}", 1)
        self.assertEqual(self.own(), "┃DE┃ RTL ZWEI FHD")

    def test_only_where_a_connection_is_free(self):
        self.free[self.two.id] = False
        self.assertEqual(self.own(), "┃DE┃ RTL ZWEI FHD")
        self.free[self.one.id] = False
        self.assertIsNone(self.own())

    def test_joins_what_it_can_play(self):
        self.playing("DE| RTL ZWEI HD")
        self.assertIsNone(self.own())

    def test_a_channel_nobody_plays_it_starts_itself(self):
        self.redis.delete(RedisKeys.channel_metadata(str(self.channel.uuid)))
        self.assertIsNone(self.own())

    def test_only_for_arrtv_with_a_limit_and_the_switch_on(self):
        self.assertIsNone(self.own(probation.Viewer("192.168.2.50", 1, app="TiviMate")))
        self.assertIsNone(self.own(self.chromecast(max_quality=None)))
        app_devices.save_settings({"own_stream": False})
        self.assertIsNone(self.own())


class FailoverOrderTests(TestCase):
    def setUp(self):
        from apps.channels.models import Stream
        from apps.m3u.models import M3UAccount

        _reset(self)
        app_devices.save_settings({"devices": True})
        self.redis = FakeRedis()
        # A stream with no account is a custom one to stock, so these have one
        account = M3UAccount.objects.create(name="TiviBridge", account_type="XC", server_url="http://a")
        names = ["DE| RTL ZWEI 4K", "DE| RTL ZWEI FHD", "DE| RTL ZWEI UHD", "DE| RTL ZWEI HD"]
        self.streams = [
            Stream.objects.create(name=n, url=f"http://x/{i}", m3u_account=account) for i, n in enumerate(names)
        ]
        self.streams.append(Stream.objects.create(name="Could Not Dispatch", url="http://f", is_custom=True))
        self.entries = [{"stream_id": s.id} for s in self.streams]

    def order(self):
        ordered = app_devices.failover_order(self.redis, "rtl2", self.entries, self.streams)
        names = {s.id: s.name for s in self.streams}
        return [names[e["stream_id"]] for e in ordered]

    def test_the_starters_limit_comes_first_the_fallback_last(self):
        viewer = probation.Viewer("192.168.2.50", 1, app="okhttp", server_device=CHROMECAST, max_quality="FHD")
        app_devices.remember_channel_limit(self.redis, "rtl2", viewer)
        self.assertEqual(
            self.order(),
            ["DE| RTL ZWEI FHD", "DE| RTL ZWEI HD", "DE| RTL ZWEI 4K", "DE| RTL ZWEI UHD", "Could Not Dispatch"],
        )

    def test_started_by_anyone_else_it_is_stock(self):
        app_devices.remember_channel_limit(self.redis, "rtl2", probation.Viewer("192.168.2.50", 1, app="okhttp", server_device=CHROMECAST, max_quality="FHD"))
        # The next start is somebody without a limit: the old one goes
        app_devices.remember_channel_limit(self.redis, "rtl2", probation.Viewer("192.168.2.40", 2, app="Plex"))
        self.assertEqual(self.order()[0], "DE| RTL ZWEI 4K")


class StreamRequestTests(SimpleTestCase):
    """The request is for the device's own stream from the moment it is chosen."""

    @mock.patch("apps.proxy.live_proxy.views.ChannelService.is_channel_unavailable_for_new_clients", return_value=True)
    @mock.patch("apps.proxy.live_proxy.views.timing.start")
    @mock.patch("apps.proxy.live_proxy.app_own_streams.own_stream_for")
    @mock.patch("apps.proxy.live_proxy.views.get_stream_object")
    @mock.patch("apps.proxy.live_proxy.views.network_access_allowed", return_value=True)
    @mock.patch("apps.proxy.live_proxy.views.ProxyServer")
    def test_handed_to_the_stream(self, proxy_cls, _ok, get_object, own_for, timing_start, _unavailable):
        from apps.proxy.live_proxy.views import stream_ts

        channel = mock.MagicMock(uuid="rtl2", name="RTL ZWEI")
        channel.get_stream_profile.return_value.is_redirect.return_value = False
        get_object.return_value = channel
        own_for.return_value = mock.MagicMock(stream_hash="hash-of-fhd", id=7)
        proxy_cls.get_instance.return_value = mock.MagicMock()
        request = RequestFactory().get("/proxy/ts/stream/rtl2")
        request.user = mock.MagicMock(is_authenticated=False)
        stream_ts(request, "rtl2")
        self.assertEqual(timing_start.call_args[0][1], "hash-of-fhd")
