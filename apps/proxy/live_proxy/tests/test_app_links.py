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

    def test_a_channel_change_is_followed_by_the_other(self):
        self._watching("france2", "c1", SHIELD)
        app_links.join(self.redis, self.user.id, self._key(PIXEL), SHIELD)
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
