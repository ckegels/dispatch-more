"""One arrTV device at a time, for the logins the admin chose (apps.proxy.live_proxy.app_one_device)."""

import json
from unittest import mock
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import app_devices, app_one_device, probation
from apps.proxy.live_proxy.redis_keys import RedisKeys
from apps.proxy.live_proxy.tests.test_probation import FakeRedis

LIVING_ROOM = "shield-living-room"
BEDROOM = "chromecast-bedroom"


@patch("apps.proxy.live_proxy.probation.gevent.spawn", side_effect=lambda fn, *args: None)
@patch("apps.proxy.live_proxy.probation._release_channel_slot_now")
@patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_client")
class OneDeviceTests(TestCase):
    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        self.redis = FakeRedis()
        self.kids = User.objects.create_user(username="kids", password="x")
        self.friend = User.objects.create_user(username="friend", password="x")
        app_devices.save_settings({"devices": True, "one_device_users": str(self.kids.id)})
        self._name(LIVING_ROOM, "Living room SHIELD")
        self._name(BEDROOM, "Bedroom TV")

    # ── helpers ──

    def _key(self, device, user=None):
        return app_devices.device_key((user or self.kids).id, device)

    def _name(self, device, name, user=None):
        self.redis.hset(app_devices.NAMES_KEY, self._key(device, user), json.dumps({"name": name, "user": "kids"}))

    def _viewer(self, device, user=None):
        user = user or self.kids
        return probation.Viewer("192.168.65.3", user_id=user.id, app="arrTV", server_device=self._key(device, user))

    def _watching(self, channel, client, device=None, user=None, user_agent="arrTV"):
        """A client on a live channel: an arrTV device's, or (device None) any other player's."""
        user = user or self.kids
        self.redis.hset(RedisKeys.channel_metadata(channel), "state", "active")
        self.redis.sadd(RedisKeys.clients(channel), client)
        meta = {"ip_address": "192.168.65.3", "user_id": str(user.id), "user_agent": user_agent, "connected_at": "1"}
        if device:
            meta["server_device"] = self._key(device, user)
        self.redis.hset(RedisKeys.client_metadata(channel, client), mapping=meta)

    def _ask(self, device, channel="new-channel", take_over=False, user=None):
        headers = {"HTTP_X_DISPATCH_TAKE_OVER": "1"} if take_over else {}
        request = RequestFactory().get(f"/proxy/ts/stream/{channel}", **headers)
        return app_one_device.check(self.redis, request, self._viewer(device, user), (channel, channel))

    def _replaced(self, device, user=None):
        raw = self.redis.get(app_one_device._replaced_key((user or self.kids).id, self._key(device, user)))
        return json.loads(raw) if raw else None

    # ── tests ──

    def test_a_second_device_closes_the_first_ones_channel_and_it_is_told_who(self, stop_client, release, _spawn):
        self._watching("orf1", "c1", LIVING_ROOM)
        with self.assertLogs("live_proxy", level="INFO") as logs:
            self.assertIsNone(self._ask(BEDROOM))
        self.assertTrue(any("closing 1 stream(s) of kids · Living room SHIELD" in line for line in logs.output))
        # Closed whole, since the living room was its only viewer: its slot is free at once
        release.assert_called_once()
        stop_client.assert_not_called()
        # Its client no longer counts against the user's own stream limit, asked right after
        self.assertEqual(self.redis.hgetall(RedisKeys.client_metadata("orf1", "c1")), {})
        self.assertEqual(self._replaced(LIVING_ROOM)["name"], "Bedroom TV")

    def test_the_replaced_device_is_refused_when_it_reconnects_rather_than_taking_it_back(self, stop_client, release, _spawn):
        """Otherwise the two would take the stream from each other at every reconnect."""
        self._watching("orf1", "c1", LIVING_ROOM)
        self._ask(BEDROOM, channel="orf2")
        self._watching("orf2", "c2", BEDROOM)
        release.reset_mock()
        answer = self._ask(LIVING_ROOM, channel="orf1")
        self.assertEqual(answer["by"], "Bedroom TV")
        self.assertTrue(answer["device_replaced"])
        self.assertEqual(answer["error"], "Playing on another device: Bedroom TV")
        # The bedroom keeps watching
        release.assert_not_called()
        stop_client.assert_not_called()
        self.assertIsNone(self._replaced(BEDROOM))

    def test_watch_here_takes_it_back(self, stop_client, release, _spawn):
        self._watching("orf1", "c1", LIVING_ROOM)
        self._ask(BEDROOM, channel="orf2")
        self._watching("orf2", "c2", BEDROOM)
        release.reset_mock()
        self.assertIsNone(self._ask(LIVING_ROOM, channel="orf1", take_over=True))
        release.assert_called_once()
        self.assertIsNone(self._replaced(LIVING_ROOM))
        self.assertEqual(self._replaced(BEDROOM)["name"], "Living room SHIELD")

    def test_refused_while_the_new_devices_channel_is_still_starting(self, stop_client, release, _spawn):
        """
        The living room reconnects within a second of being closed; the bedroom is no client
        of its channel until that channel has started. Without this the living room would
        slip back in, and both would play.
        """
        self._watching("orf1", "c1", LIVING_ROOM)
        self._ask(BEDROOM, channel="orf2")
        self.assertEqual(self._ask(LIVING_ROOM, channel="orf1")["by"], "Bedroom TV")

    def test_once_the_other_device_stopped_watching_the_replaced_one_plays_again(self, stop_client, release, _spawn):
        """A TV left on is not refused for a day after the other one went off."""
        self._watching("orf1", "c1", LIVING_ROOM)
        with patch("apps.proxy.live_proxy.app_one_device.time.time", return_value=1_000.0):
            self._ask(BEDROOM, channel="orf2")
        self.assertIsNone(self._ask(LIVING_ROOM, channel="orf1"))
        self.assertIsNone(self._replaced(LIVING_ROOM))

    def test_the_same_channel_goes_on_running_for_the_new_device(self, stop_client, release, _spawn):
        """Closing the channel whole would answer the new device "Channel is stopping"."""
        self._watching("orf1", "c1", LIVING_ROOM)
        self.assertIsNone(self._ask(BEDROOM, channel="orf1"))
        stop_client.assert_called_once_with("orf1", "c1")
        release.assert_not_called()

    def test_a_channel_someone_else_or_a_recording_is_on_keeps_running(self, stop_client, release, _spawn):
        self._watching("orf1", "c1", LIVING_ROOM)
        self._watching("orf1", "rec", user=self.friend, user_agent="Dispatcharr-DVR/1.0")
        self.assertIsNone(self._ask(BEDROOM))
        stop_client.assert_called_once_with("orf1", "c1")
        release.assert_not_called()

    def test_every_tile_of_the_first_devices_multiview_is_closed(self, stop_client, release, _spawn):
        self._watching("orf1", "t1", LIVING_ROOM)
        self._watching("orf2", "t2", LIVING_ROOM)
        self._ask(BEDROOM)
        self.assertEqual(release.call_count, 2)

    def test_nothing_else_is_ever_closed(self, stop_client, release, _spawn):
        # Another login's device, a player of the same login that did not say which device it
        # is, and the device's own other channel (Force Close's business, not this)
        self._watching("theirs", "c1", LIVING_ROOM, user=self.friend)
        self._watching("vlc", "c2", user_agent="VLC/3.0")
        self._watching("mine", "c3", BEDROOM)
        self.assertIsNone(self._ask(BEDROOM))
        stop_client.assert_not_called()
        release.assert_not_called()

    def test_a_login_not_chosen_or_the_devices_not_recognised_is_stock(self, stop_client, release, _spawn):
        self._watching("theirs", "c1", LIVING_ROOM, user=self.friend)
        self.assertIsNone(self._ask(BEDROOM, user=self.friend))
        app_devices.save_settings({"devices": False})
        self._watching("orf1", "c2", LIVING_ROOM)
        self.assertIsNone(self._ask(BEDROOM))
        stop_client.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.redis.strings, {})

    def test_a_device_that_never_said_its_name_is_another_device(self, stop_client, release, _spawn):
        self.redis.hdel(app_devices.NAMES_KEY, self._key(BEDROOM))
        self._watching("orf1", "c1", LIVING_ROOM)
        self._ask(BEDROOM, channel="orf2")
        self._watching("orf2", "c2", BEDROOM)
        self.assertEqual(self._ask(LIVING_ROOM)["by"], "another device")

    def test_a_failure_never_costs_the_new_device_its_stream(self, stop_client, release, _spawn):
        broken = MagicMock()
        broken.scan_iter.side_effect = RuntimeError("redis went away")
        request = RequestFactory().get("/proxy/ts/stream/x")
        with self.assertLogs("live_proxy", level="WARNING"):
            self.assertIsNone(app_one_device.check(broken, request, self._viewer(BEDROOM), ("x", "x")))


class OneDeviceSettingsTests(TestCase):
    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)

    def test_the_logins_are_kept_as_ids(self):
        saved = app_devices.save_settings({"one_device_users": ["3", "7", "x"]})
        self.assertEqual(saved["one_device_users"], "3,7")
        self.assertEqual(app_one_device.chosen_users(), {"3", "7"})
        self.assertEqual(app_devices.save_settings({"one_device_users": ""})["one_device_users"], "")

    def test_the_capabilities_say_so_only_with_devices_recognised(self):
        tv = User.objects.create_user(username="tv", password="x")
        client = APIClient()
        client.force_authenticate(user=tv)
        app_devices.save_settings({"one_device_users": str(tv.id)})
        answer = client.get("/api/core/capabilities/").json()
        self.assertFalse(answer["one_device"])
        self.assertEqual(answer["headers"]["take_over"], "X-Dispatch-Take-Over")
        app_devices.save_settings({"devices": True})
        self.assertTrue(client.get("/api/core/capabilities/").json()["one_device"])

    def test_take_over_by_header_or_query_parameter(self):
        self.assertTrue(app_devices.declared_take_over(RequestFactory().get("/", HTTP_X_DISPATCH_TAKE_OVER="1")))
        self.assertTrue(app_devices.declared_take_over(RequestFactory().get("/", {"dm_take_over": "true"})))
        self.assertFalse(app_devices.declared_take_over(RequestFactory().get("/")))


class OneDeviceStreamRequestTests(SimpleTestCase):
    """The refusal is the stream request's answer, before the user's own limit is asked."""

    @mock.patch("apps.proxy.live_proxy.views.check_user_stream_limits")
    @mock.patch("apps.proxy.live_proxy.app_one_device.check")
    @mock.patch("apps.proxy.live_proxy.views.probation.skipped_while_surfing", return_value=False)
    @mock.patch("apps.proxy.live_proxy.views.timing.start")
    @mock.patch("apps.proxy.live_proxy.app_own_streams.own_stream_for", return_value=None)
    @mock.patch("apps.proxy.live_proxy.views.get_stream_object")
    @mock.patch("apps.proxy.live_proxy.views.network_access_allowed", return_value=True)
    @mock.patch("apps.proxy.live_proxy.views.ProxyServer")
    def test_a_replaced_device_gets_409_with_who_took_over(
        self, proxy_cls, _ok, get_object, _own, _timing, _surf, check, limits
    ):
        from apps.proxy.live_proxy.views import stream_ts

        channel = mock.MagicMock(uuid="orf1", name="ORF 1")
        channel.get_stream_profile.return_value.is_redirect.return_value = False
        get_object.return_value = channel
        proxy_cls.get_instance.return_value = mock.MagicMock()
        check.return_value = app_one_device.refusal("Bedroom TV")
        request = RequestFactory().get("/proxy/ts/stream/orf1")
        request.user = mock.MagicMock(is_authenticated=True, stream_limit=1)
        response = stream_ts(request, "orf1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)["by"], "Bedroom TV")
        limits.assert_not_called()
