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
        # Look back and movies, as stock lists them by login (get_user_active_connections)
        self.connections = []
        patcher = patch(
            "apps.proxy.utils.get_user_active_connections",
            side_effect=lambda user_id: [c for c in self.connections if c.pop("_user", user_id) == user_id],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertTrue(any("starts a stream; closing 1 stream(s) of kids · Living room SHIELD" in line for line in logs.output))
        # Closed whole, since the living room was its only viewer: its slot is free at once
        release.assert_called_once()
        stop_client.assert_not_called()
        # Its client no longer counts against the user's own stream limit, asked right after
        self.assertEqual(self.redis.hgetall(RedisKeys.client_metadata("orf1", "c1")), {})
        self.assertEqual(self._replaced(LIVING_ROOM)["name"], "Bedroom TV")

    def test_the_replaced_device_is_told_at_once_on_the_socket(self, stop_client, release, _spawn):
        """Not only when it has played what it had buffered, seconds later."""
        self._watching("orf1", "c1", LIVING_ROOM)
        with patch("core.utils.send_websocket_update") as send:
            self._ask(BEDROOM)
        send.assert_called_once_with("updates", "update", {
            "type": "device_replaced", "user_id": self.kids.id,
            "device": LIVING_ROOM, "by": "Bedroom TV",
            # Which device took over, so the replaced one can offer to join it (app_links)
            "by_device": BEDROOM,
        })

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
        self.assertEqual(answer["by_device"], BEDROOM)
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

    def test_joining_the_same_channel_is_allowed_it_costs_no_provider_connection(self, stop_client, release, _spawn):
        """The user: "it's the same channel of course that is allowed, it's using the same provider"."""
        self._watching("france2", "c1", LIVING_ROOM)
        self.assertIsNone(self._ask(BEDROOM, channel="france2"))
        stop_client.assert_not_called()
        release.assert_not_called()
        self.assertIsNone(self._replaced(LIVING_ROOM))

    def test_any_channel_already_playing_costs_nothing_even_for_a_replaced_device(self, stop_client, release, _spawn):
        self._watching("orf1", "c1", LIVING_ROOM)
        self._ask(BEDROOM, channel="orf2")
        self._watching("orf2", "c2", BEDROOM)
        # The living room was replaced, but the bedroom's own channel is playing: joining it
        # opens nothing, so it is let in
        self.assertIsNone(self._ask(LIVING_ROOM, channel="orf2"))
        # Its old channel was closed for the bedroom: going back to it is a connection again
        self.assertEqual(self._ask(LIVING_ROOM, channel="orf1")["by"], "Bedroom TV")

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

    # ── Look back and movies: they hold a provider connection too ──

    def _session(self, kind, media, client, device):
        """A look back session ("timeshift") or a movie ("vod") the device streams under."""
        self.connections.append({"media_id": media, "client_id": client, "connected_at": 1.0, "type": kind})
        app_one_device.note_client(self.redis, client, self._key(device))

    def _ask_session(self, device, client, take_over=False):
        headers = {"HTTP_X_DISPATCH_DEVICE": device}
        if take_over:
            headers["HTTP_X_DISPATCH_TAKE_OVER"] = "1"
        request = RequestFactory().get("/proxy/catchup/x", REMOTE_ADDR="192.168.65.3", **headers)
        return app_one_device.check_request(self.redis, request, self.kids, client)

    @patch("apps.proxy.utils.stop_timeshift_client")
    def test_a_look_back_on_the_first_device_is_closed_like_a_channel(self, stop_timeshift, stop_client, release, _spawn):
        self._session("timeshift", "42_20261016T2000", "sess-living", LIVING_ROOM)
        self.assertIsNone(self._ask(BEDROOM))
        stop_timeshift.assert_called_once_with(self.redis, "42_20261016T2000", "sess-living")
        self.assertEqual(self._replaced(LIVING_ROOM)["name"], "Bedroom TV")

    def test_a_movie_on_the_first_device_is_closed_like_a_channel(self, stop_client, release, _spawn):
        from apps.proxy.vod_proxy.multi_worker_connection_manager import get_vod_client_stop_key

        self._session("vod", "content-uuid", "vod_1_1", LIVING_ROOM)
        self.assertIsNone(self._ask(BEDROOM))
        self.assertEqual(self.redis.get(get_vod_client_stop_key("vod_1_1")), "true")

    def test_starting_a_look_back_takes_over_and_a_replaced_look_back_is_refused(self, stop_client, release, _spawn):
        self._watching("orf1", "c1", LIVING_ROOM)
        self.assertIsNone(self._ask_session(BEDROOM, "sess-bedroom"))
        release.assert_called_once()
        # The living room's own look back is refused until it takes over, and Watch here works
        self.assertEqual(self._ask_session(LIVING_ROOM, "sess-living")["by"], "Bedroom TV")
        self._session("timeshift", "42_x", "sess-bedroom", BEDROOM)
        with patch("apps.proxy.utils.stop_timeshift_client") as stop_timeshift:
            self.assertIsNone(self._ask_session(LIVING_ROOM, "sess-living", take_over=True))
        stop_timeshift.assert_called_once_with(self.redis, "42_x", "sess-bedroom")

    def test_a_session_of_a_player_that_never_said_its_device_is_left_alone(self, stop_client, release, _spawn):
        self.connections.append({"media_id": "m", "client_id": "unknown", "connected_at": 1.0, "type": "vod"})
        self.assertIsNone(self._ask(BEDROOM))
        self.assertIsNone(self.redis.get("vod_proxy:client:unknown:stop"))

    def test_a_request_that_does_not_say_the_name_keeps_the_one_given(self, stop_client, release, _spawn):
        self._ask_session(BEDROOM, "sess-bedroom")
        self.assertEqual(app_one_device.plain_name(self.redis, self._key(BEDROOM)), "Bedroom TV")

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


class OneDeviceSocketTests(SimpleTestCase):
    def test_only_that_login_receives_it(self):
        from dispatcharr.consumers import user_may_receive_update

        message = {"type": "device_replaced", "user_id": 7, "device": LIVING_ROOM, "by": "Bedroom TV"}
        self.assertTrue(user_may_receive_update(MagicMock(id=7, user_level=0), message))
        self.assertFalse(user_may_receive_update(MagicMock(id=8, user_level=10), message))
        self.assertFalse(user_may_receive_update(None, message))
