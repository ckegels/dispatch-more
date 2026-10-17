"""arrTV devices of one login watching together (apps.proxy.live_proxy.app_links)."""

import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import app_devices, app_links, app_one_device, probation
from apps.proxy.live_proxy.redis_keys import RedisKeys
from apps.proxy.live_proxy.tests.test_probation import FakeRedis

SHIELD = "shield-living-room"
PIXEL = "pixel-9-phone"


class WatchTogetherTests(TestCase):
    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        self.redis = FakeRedis()
        self.user = User.objects.create_user(username="admin", password="x")
        app_devices.save_settings({"devices": True, "link_devices": True})
        for device, name in ((SHIELD, "SHIELD"), (PIXEL, "Pixel 9")):
            self.redis.hset(app_devices.NAMES_KEY, self._key(device), json.dumps({"name": name, "user": "admin"}))
        patcher = patch("core.utils.send_websocket_update")
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)

    def _key(self, device):
        return app_devices.device_key(self.user.id, device)

    def _viewer(self, device, multiview=None):
        return probation.Viewer("192.168.65.3", user_id=self.user.id, app="arrTV",
                                server_device=self._key(device), multiview=multiview)

    def _watching(self, channel, client, device):
        self.redis.hset(RedisKeys.channel_metadata(channel), "state", "active")
        self.redis.sadd(RedisKeys.clients(channel), client)
        self.redis.hset(RedisKeys.client_metadata(channel, client), mapping={
            "ip_address": "192.168.65.3", "user_id": str(self.user.id), "server_device": self._key(device),
        })

    def test_the_other_device_watching_a_channel_is_offered(self):
        self._watching("france2", "c1", SHIELD)
        self.assertEqual(app_links.offers(self.redis, self.user.id, self._key(PIXEL)), [
            {"device": SHIELD, "name": "SHIELD", "channel": "france2", "channel_name": ""},
        ])
        # Not itself, and not a multiview of several channels at once
        self.assertEqual(app_links.offers(self.redis, self.user.id, self._key(SHIELD)), [])
        self._watching("tf1", "c2", SHIELD)
        self.assertEqual(app_links.offers(self.redis, self.user.id, self._key(PIXEL)), [])

    def test_join_links_both_on_the_others_channel(self):
        self._watching("france2", "c1", SHIELD)
        joined = app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        self.assertEqual(joined["channel"], "france2")
        self.assertEqual(app_links.linked_with(self.redis, self.user.id, self._key(PIXEL)), {self._key(SHIELD)})
        self.assertEqual(app_links.channel_of(self.redis, self.user.id), "france2")
        self.assertIsNone(app_links.join(self.redis, self.user.id, self._key(PIXEL), "not-watching"))
        # The device joined hears it: it is linked too, and reports its position from now on
        self.sent.assert_any_call("updates", "update", {
            "type": "link_joined", "user_id": self.user.id, "by": PIXEL, "devices": [SHIELD],
        })

    def test_a_channel_change_is_followed_by_the_other(self):
        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        self.sent.reset_mock()  # the join's own message
        request = RequestFactory().get("/proxy/ts/stream/tf1")
        self.assertTrue(app_links.mirror(self.redis, request, self._viewer(SHIELD), "tf1"))
        self.sent.assert_called_once_with("updates", "update", {
            "type": "linked_channel", "user_id": self.user.id, "channel": "tf1",
            "by": SHIELD, "devices": [PIXEL],
        })
        # The follower's own request for that channel tells nobody: no echo
        self.assertFalse(app_links.mirror(self.redis, request, self._viewer(PIXEL), "tf1"))
        self.assertEqual(self.sent.call_count, 1)

    def test_nothing_is_mirrored_unlinked_from_a_tile_or_switched_off(self):
        request = RequestFactory().get("/")
        self.assertFalse(app_links.mirror(self.redis, request, self._viewer(SHIELD), "tf1"))
        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        self.sent.reset_mock()  # the join's own message
        self.assertFalse(app_links.mirror(self.redis, request, self._viewer(SHIELD, multiview="mv"), "tf1"))
        app_devices.save_settings({"link_devices": False})
        self.assertFalse(app_links.mirror(self.redis, request, self._viewer(SHIELD), "tf1"))
        self.sent.assert_not_called()

    def test_leaving_ends_a_group_of_two(self):
        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        app_links.leave(self.redis, self.user.id, self._key(PIXEL))
        self.assertEqual(app_links.members(self.redis, self.user.id), set())
        self.assertIsNone(app_links.channel_of(self.redis, self.user.id))
        # The one left is told, and plays on
        self.sent.assert_called_with("updates", "update", {
            "type": "link_ended", "user_id": self.user.id, "devices": [SHIELD],
        })
        # Leaving again (the app leaves at every exit) says nothing more
        self.sent.reset_mock()
        app_links.leave(self.redis, self.user.id, self._key(PIXEL))
        self.sent.assert_not_called()

    @patch("apps.proxy.utils.get_user_active_connections", return_value=[])
    @patch("apps.proxy.live_proxy.probation.gevent.spawn", side_effect=lambda fn, *args: None)
    @patch("apps.proxy.live_proxy.probation._release_channel_slot_now")
    def test_devices_watching_together_never_replace_each_other(self, release, _spawn, _conns):
        """On a login held to one device: the follower's tune is a new channel for a moment."""
        app_devices.save_settings({"one_device_users": str(self.user.id)})
        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        request = RequestFactory().get("/proxy/ts/stream/tf1")
        self.assertIsNone(app_one_device.check(self.redis, request, self._viewer(PIXEL), ("tf1", "tf1")))
        release.assert_not_called()

    def test_a_look_back_leaves_watching_together(self):
        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        request = RequestFactory().get("/proxy/catchup/x", HTTP_X_DISPATCH_DEVICE=PIXEL, REMOTE_ADDR="192.168.65.3")
        with patch("apps.proxy.utils.get_user_active_connections", return_value=[]):
            app_one_device.check_request(self.redis, request, self.user, "sess-1")
        self.assertEqual(app_links.members(self.redis, self.user.id), set())

    def test_the_api_offers_joins_and_leaves_for_the_device_that_asks(self):
        self._watching("france2", "c1", SHIELD)
        client = APIClient()
        client.force_authenticate(user=self.user)
        with patch("core.utils.RedisClient.get_client", return_value=self.redis):
            answer = client.get("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL).json()
            self.assertFalse(answer["linked"])
            self.assertEqual(answer["offers"][0]["device"], SHIELD)
            answer = client.post("/api/core/app-link/", {"join": SHIELD}, format="json",
                                 HTTP_X_DISPATCH_DEVICE=PIXEL).json()
            self.assertEqual(answer["channel"], "france2")
            self.assertTrue(client.get("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL).json()["linked"])
            client.delete("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL)
            self.assertFalse(client.get("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL).json()["linked"])
            # Not without saying which device, and not while switched off
            self.assertEqual(client.get("/api/core/app-link/").status_code, 400)
            app_devices.save_settings({"link_devices": False})
            self.assertEqual(client.get("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL).status_code, 403)

    def test_the_capabilities_say_where_to_ask(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        answer = client.get("/api/core/capabilities/").json()
        self.assertTrue(answer["link_devices"])
        self.assertEqual(answer["link_url"], "/api/core/app-link/")


class WatchTogetherSocketTests(SimpleTestCase):
    def test_only_that_login_hears_a_channel_change(self):
        from dispatcharr.consumers import user_may_receive_update

        message = {"type": "linked_channel", "user_id": 1, "channel": "tf1", "by": SHIELD, "devices": [PIXEL]}
        self.assertTrue(user_may_receive_update(MagicMock(id=1, user_level=0), message))
        self.assertFalse(user_may_receive_update(MagicMock(id=2, user_level=10), message))


class InStepTests(TestCase):
    """Linked devices report the PTS they show; each hears the others' on the same channel."""

    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        self.redis = FakeRedis()
        self.user = User.objects.create_user(username="admin", password="x")
        app_devices.save_settings({"devices": True, "link_devices": True})
        self.redis.hset(app_devices.NAMES_KEY, self._key(SHIELD), json.dumps({"name": "SHIELD"}))
        patcher = patch("core.utils.send_websocket_update")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _key(self, device):
        return app_devices.device_key(self.user.id, device)

    def _watching(self, channel, client, device):
        self.redis.hset(RedisKeys.channel_metadata(channel), "state", "active")
        self.redis.sadd(RedisKeys.clients(channel), client)
        self.redis.hset(RedisKeys.client_metadata(channel, client), mapping={
            "ip_address": "192.168.65.3", "user_id": str(self.user.id), "server_device": self._key(device),
        })

    def test_each_hears_the_others_fresh_position_on_the_same_channel(self):
        import time as _time

        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        self._watching("france2", "c2", PIXEL)
        now = int(_time.time() * 1000)
        # The first to report hears nobody yet
        self.assertEqual(app_links.report_position(self.redis, self.user.id, self._key(SHIELD), {"pts": 900000, "at": now}), [])
        heard = app_links.report_position(self.redis, self.user.id, self._key(PIXEL), {"pts": 3600000, "at": now})
        self.assertEqual(heard, [{"device": SHIELD, "name": "SHIELD", "pts": 900000, "at": now}])

    def test_a_device_on_another_stream_or_not_linked_compares_with_nobody(self):
        import time as _time

        now = int(_time.time() * 1000)
        self._watching("france2", "c1", SHIELD)
        self.assertEqual(app_links.report_position(self.redis, self.user.id, self._key(PIXEL), {"pts": 1, "at": now}), [])
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
        app_links.report_position(self.redis, self.user.id, self._key(SHIELD), {"pts": 1, "at": now})
        # The phone on a stream of its own (another channel id): other timestamps
        self._watching("own-stream-hash", "c2", PIXEL)
        self.assertEqual(app_links.report_position(self.redis, self.user.id, self._key(PIXEL), {"pts": 2, "at": now}), [])

    def test_the_api_answers_the_others_positions(self):
        import time as _time

        self._watching("france2", "c1", SHIELD)
        client = APIClient()
        client.force_authenticate(user=self.user)
        with patch("core.utils.RedisClient.get_client", return_value=self.redis):
            client.post("/api/core/app-link/", {"join": SHIELD}, format="json", HTTP_X_DISPATCH_DEVICE=PIXEL)
            self._watching("france2", "c2", PIXEL)
            now = int(_time.time() * 1000)
            client.post("/api/core/app-link/", {"position": {"pts": 5, "at": now}}, format="json", HTTP_X_DISPATCH_DEVICE=SHIELD)
            answer = client.post("/api/core/app-link/", {"position": {"pts": 9, "at": now}}, format="json",
                                 HTTP_X_DISPATCH_DEVICE=PIXEL).json()
        self.assertEqual(answer["others"][0]["pts"], 5)


class WatchTogetherWithPeopleTests(TestCase):
    """
    Other users in the house (v266): X's SHIELD is joined by Y's Pixel. Asked first, X leads,
    Y changing channel asks X, and either can end it.
    """

    def setUp(self):
        from apps.channels.models import Channel

        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        self.redis = FakeRedis()
        self.x = User.objects.create_user(username="x", password="x", user_level=10)
        self.y = User.objects.create_user(username="y", password="x", user_level=1, first_name="Yves")
        app_devices.save_settings({
            "devices": True, "link_devices": True, "home_networks": "192.168.2.0/24",
            "link_users": f"{self.x.id},{self.y.id}",
        })
        self.tf1 = str(Channel.objects.create(name="TF1", channel_number=1, user_level=0).uuid)
        self.france2 = str(Channel.objects.create(name="France 2", channel_number=2, user_level=0).uuid)
        # A channel only higher levels may watch
        self.adult = str(Channel.objects.create(name="Late", channel_number=3, user_level=5).uuid)
        self.redis.hset(app_devices.NAMES_KEY, self.shield, json.dumps({"name": "SHIELD", "user": "x"}))
        self.redis.hset(app_devices.NAMES_KEY, self.pixel, json.dumps({"name": "Pixel 9", "user": "y"}))
        patcher = patch("core.utils.send_websocket_update")
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def shield(self):
        return app_devices.device_key(self.x.id, SHIELD)

    @property
    def pixel(self):
        return app_devices.device_key(self.y.id, PIXEL)

    def _watching(self, channel, client, device_key, user, ip="192.168.65.3"):
        self.redis.hset(RedisKeys.channel_metadata(channel), "state", "active")
        self.redis.sadd(RedisKeys.clients(channel), client)
        self.redis.hset(RedisKeys.client_metadata(channel, client), mapping={
            "ip_address": ip, "user_id": str(user.id), "server_device": device_key,
        })

    def _viewer(self, device_key, user):
        return probation.Viewer("192.168.65.3", user_id=user.id, app="arrTV", server_device=device_key)

    def _messages(self, kind):
        return [c.args[2] for c in self.sent.call_args_list if c.args[2]["type"] == kind]

    def _linked(self):
        self._watching(self.tf1, "c1", self.shield, self.x)
        app_links.join(self.redis, self.y.id, self.pixel, SHIELD, "192.168.65.3")
        app_links.answer(self.redis, self.x.id, self.shield, PIXEL, True)
        self._watching(self.tf1, "c2", self.pixel, self.y)
        self.sent.reset_mock()

    def test_another_persons_device_in_the_same_house_is_offered_with_who_it_is(self):
        self._watching(self.tf1, "c1", self.shield, self.x)
        offers = app_links.offers(self.redis, self.y.id, self.pixel, "192.168.65.3")
        self.assertEqual(offers, [{
            "device": SHIELD, "name": "SHIELD", "channel": self.tf1, "channel_name": "TF1",
            "user_id": str(self.x.id), "user": "x",
        }])
        # Another house (another address over the VPN): nothing
        self.assertEqual(app_links.offers(self.redis, self.y.id, self.pixel, "192.168.65.9"), [])

    def test_the_home_networks_are_one_house(self):
        self._watching(self.tf1, "c1", self.shield, self.x, ip="192.168.2.10")
        self.assertEqual(len(app_links.offers(self.redis, self.y.id, self.pixel, "192.168.2.44")), 1)

    def test_only_the_users_chosen_and_only_a_channel_the_one_joining_may_watch(self):
        self._watching(self.adult, "c1", self.shield, self.x)
        self.assertEqual(app_links.offers(self.redis, self.y.id, self.pixel, "192.168.65.3"), [])
        self._watching(self.tf1, "c1", self.shield, self.x)
        self.redis.delete(RedisKeys.client_metadata(self.adult, "c1"))
        app_devices.save_settings({"link_users": str(self.x.id)})
        self.assertEqual(app_links.offers(self.redis, self.y.id, self.pixel, "192.168.65.3"), [])

    def test_joining_asks_the_other_first_and_allowed_links_the_two(self):
        self._watching(self.tf1, "c1", self.shield, self.x)
        asked = app_links.join(self.redis, self.y.id, self.pixel, SHIELD, "192.168.65.3")
        self.assertTrue(asked["pending"])
        self.assertEqual(app_links.linked_with(self.redis, self.y.id, self.pixel), set())
        (request,) = self._messages("link_request")
        self.assertEqual(request["devices"], [SHIELD])
        self.assertEqual(request["by"], PIXEL)
        self.assertEqual(request["user"], "Yves")
        self.assertEqual(sorted(request["user_ids"]), sorted([self.x.id, self.y.id]))
        self.assertTrue(app_links.answer(self.redis, self.x.id, self.shield, PIXEL, True))
        self.assertEqual(app_links.linked_with(self.redis, self.y.id, self.pixel), {self.shield})
        (accepted,) = self._messages("link_accepted")
        self.assertEqual((accepted["devices"], accepted["channel"]), ([PIXEL], self.tf1))
        # The one joined leads
        self.assertTrue(app_links.leads(self.redis, self.shield))
        self.assertFalse(app_links.leads(self.redis, self.pixel))
        # An answer once more, or to nobody, is no answer
        self.assertFalse(app_links.answer(self.redis, self.x.id, self.shield, PIXEL, True))

    def test_declined_the_one_asking_is_told_and_nothing_is_linked(self):
        self._watching(self.tf1, "c1", self.shield, self.x)
        app_links.join(self.redis, self.y.id, self.pixel, SHIELD, "192.168.65.3")
        app_links.answer(self.redis, self.x.id, self.shield, PIXEL, False)
        (declined,) = self._messages("link_declined")
        self.assertEqual(declined["devices"], [PIXEL])
        self.assertEqual(app_links.linked_with(self.redis, self.y.id, self.pixel), set())

    def test_the_leader_changing_channel_asks_the_other_too(self):
        # The user: "lets do the 10 seconds ask in both directions"
        self._linked()
        request = RequestFactory().get("/")
        self.assertTrue(app_links.mirror(self.redis, request, self._viewer(self.shield, self.x), self.france2))
        self.assertEqual(self._messages("linked_channel"), [])
        (asked,) = self._messages("link_follow_request")
        self.assertEqual((asked["devices"], asked["channel"], asked["by"]), ([PIXEL], self.france2, SHIELD))
        self.assertIn(self.y.id, asked["user_ids"])
        # The group is on the leader's channel; the other following asks nobody
        self.assertEqual(app_links.channel_of(self.redis, str(self.x.id)), self.france2)
        self.sent.reset_mock()
        self.assertFalse(app_links.mirror(self.redis, request, self._viewer(self.pixel, self.y), self.france2))
        self.sent.assert_not_called()

    def test_the_other_staying_when_the_leader_changed_leaves_and_the_leader_hears_why(self):
        self._linked()
        app_links.mirror(self.redis, RequestFactory().get("/"), self._viewer(self.shield, self.x), self.france2)
        y_client = APIClient()
        y_client.force_authenticate(user=self.y)
        with patch("core.utils.RedisClient.get_client", return_value=self.redis):
            y_client.post("/api/core/app-link/", {"stay": SHIELD}, format="json", HTTP_X_DISPATCH_DEVICE=PIXEL)
        self.assertEqual(app_links.linked_with(self.redis, self.x.id, self.shield), set())
        told = [m for m in self._messages("link_ended") if m["devices"] == [SHIELD]]
        self.assertEqual((told[-1]["reason"], told[-1]["name"]), ("stayed", "Pixel 9"))

    def test_ones_own_devices_still_follow_the_leader_at_once(self):
        x_tv = app_devices.device_key(self.x.id, "bedroom-tv")
        self.redis.hset(app_devices.NAMES_KEY, x_tv, json.dumps({"name": "Bedroom", "user": "x"}))
        self._linked()
        self._watching(self.tf1, "c3", x_tv, self.x)
        app_links.join(self.redis, self.x.id, x_tv, SHIELD)
        self.sent.reset_mock()
        app_links.mirror(self.redis, RequestFactory().get("/"), self._viewer(self.shield, self.x), self.france2)
        (follow,) = self._messages("linked_channel")
        self.assertEqual(follow["devices"], ["bedroom-tv"])
        (asked,) = self._messages("link_follow_request")
        self.assertEqual(asked["devices"], [PIXEL])

    def test_the_one_who_joined_changing_channel_asks_and_staying_takes_him_out(self):
        self._linked()
        request = RequestFactory().get("/")
        self.assertTrue(app_links.mirror(self.redis, request, self._viewer(self.pixel, self.y), self.france2))
        (asked,) = self._messages("link_follow_request")
        self.assertEqual((asked["devices"], asked["channel"], asked["by"]), ([SHIELD], self.france2, PIXEL))
        self.assertEqual(asked["channel_name"], "France 2")
        # The group stays where the leader is until the leader follows
        self.assertEqual(app_links.channel_of(self.redis, str(self.x.id)), self.tf1)
        self.assertTrue(app_links.remove(self.redis, self.x.id, self.shield, PIXEL, reason="stayed"))
        self.assertEqual(app_links.linked_with(self.redis, self.y.id, self.pixel), set())
        told = [m for m in self._messages("link_ended") if m["devices"] == [PIXEL]]
        self.assertEqual(told[-1]["reason"], "stayed")

    def test_following_is_the_leader_tuning_the_channel(self):
        self._linked()
        request = RequestFactory().get("/")
        app_links.mirror(self.redis, request, self._viewer(self.pixel, self.y), self.france2)
        self.sent.reset_mock()
        app_links.mirror(self.redis, request, self._viewer(self.shield, self.x), self.france2)
        self.assertEqual(app_links.channel_of(self.redis, str(self.x.id)), self.france2)
        self.assertEqual(app_links.linked_with(self.redis, self.y.id, self.pixel), {self.shield})

    def test_the_leader_going_where_the_other_may_not_takes_him_out(self):
        self._linked()
        app_links.mirror(self.redis, RequestFactory().get("/"), self._viewer(self.shield, self.x), self.adult)
        self.assertEqual(app_links.linked_with(self.redis, self.y.id, self.pixel), set())
        told = [m for m in self._messages("link_ended") if m["devices"] == [PIXEL]]
        self.assertEqual(told[-1]["reason"], "not_allowed")

    def test_the_leader_disconnects_the_other_but_not_the_other_way_round(self):
        self._linked()
        self.assertFalse(app_links.remove(self.redis, self.y.id, self.pixel, SHIELD))
        self.assertTrue(app_links.remove(self.redis, self.x.id, self.shield, PIXEL))
        self.assertEqual(app_links.linked_with(self.redis, self.x.id, self.shield), set())

    def test_the_state_says_who_leads_and_who_is_in_it(self):
        self._linked()
        mine = app_links.state(self.redis, self.x.id, self.shield, "192.168.65.3")
        self.assertTrue(mine["linked"] and mine["leads"])
        self.assertEqual(mine["together"], [{"device": PIXEL, "name": "Pixel 9", "user": "Yves", "leads": False}])
        theirs = app_links.state(self.redis, self.y.id, self.pixel, "192.168.65.3")
        self.assertFalse(theirs["leads"])
        self.assertEqual(theirs["channel"], self.tf1)

    def test_the_api_asks_answers_and_disconnects(self):
        self._watching(self.tf1, "c1", self.shield, self.x)
        x_client, y_client = APIClient(), APIClient()
        x_client.force_authenticate(user=self.x)
        y_client.force_authenticate(user=self.y)
        with patch("core.utils.RedisClient.get_client", return_value=self.redis):
            answer = y_client.post("/api/core/app-link/", {"join": SHIELD}, format="json",
                                   HTTP_X_DISPATCH_DEVICE=PIXEL, REMOTE_ADDR="192.168.65.3")
            self.assertEqual(answer.status_code, 202)
            self.assertTrue(answer.json()["pending"])
            x_client.post("/api/core/app-link/", {"answer": PIXEL, "allow": True}, format="json",
                          HTTP_X_DISPATCH_DEVICE=SHIELD, REMOTE_ADDR="192.168.65.3")
            self.assertTrue(y_client.get("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL,
                                         REMOTE_ADDR="192.168.65.3").json()["linked"])
            x_client.delete(f"/api/core/app-link/?device={PIXEL}", HTTP_X_DISPATCH_DEVICE=SHIELD)
            self.assertFalse(y_client.get("/api/core/app-link/", HTTP_X_DISPATCH_DEVICE=PIXEL,
                                          REMOTE_ADDR="192.168.65.3").json()["linked"])

    def test_every_login_of_the_group_hears_it_and_nobody_else(self):
        from dispatcharr.consumers import user_may_receive_update

        message = {"type": "link_request", "user_id": 1, "user_ids": [1, 2], "devices": [SHIELD]}
        self.assertTrue(user_may_receive_update(MagicMock(id=2), message))
        self.assertFalse(user_may_receive_update(MagicMock(id=3), message))
