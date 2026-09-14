"""Tests for device IDs in M3U stream links (Channel Switch Overlap)."""

import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.channels.models import ChannelGroup
from apps.m3u.models import M3UAccount
from apps.output.tests.test_views import OutputEndpointTestMixin, _response_text
from apps.proxy.live_proxy import probation


class M3UDeviceIdTests(OutputEndpointTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        group = ChannelGroup.objects.create(name=f"Device Group {uuid4().hex[:8]}")
        self.profile = self._create_isolated_profile("device")
        for number in (1.0, 2.0):
            self._add_channel_to_profile(
                self.profile, group, channel_number=number, name=f"Channel {number}"
            )

    def _enable_overlap(self):
        M3UAccount.objects.create(
            name=f"overlap-{uuid4().hex[:8]}",
            account_type="XC",
            username="u",
            password="p",
            max_streams=1,
            custom_properties={"probation_enabled": True},
        )

    def _stream_links(self, content):
        return [line for line in content.splitlines() if line.startswith("http")]

    def _device_ids(self, content):
        ids = set()
        for link in self._stream_links(content):
            ids.update(parse_qs(urlparse(link).query).get(probation.DEVICE_ID_PARAM, [None]))
        return ids

    def _get_m3u(self, user_agent=None, **params):
        url = reverse("output:m3u_endpoint", kwargs={"profile_name": self.profile.name})
        headers = {"HTTP_USER_AGENT": user_agent} if user_agent else {}
        response = self.client.get(url, params, **headers)
        self.assertEqual(response.status_code, 200)
        return _response_text(response)

    def test_no_device_id_without_overlap_accounts(self):
        content = self._get_m3u()

        self.assertEqual(len(self._stream_links(content)), 2)
        self.assertEqual(self._device_ids(content), {None})
        self.assertNotIn(probation.DEVICE_ID_PLACEHOLDER, content)

    def test_each_download_gets_its_own_device_id(self):
        self._enable_overlap()

        first = self._get_m3u()
        # Served from the 2-second playlist cache, still a new device ID
        second = self._get_m3u()

        first_ids = self._device_ids(first)
        second_ids = self._device_ids(second)
        self.assertEqual(len(first_ids), 1)
        self.assertEqual(len(second_ids), 1)
        self.assertNotEqual(first_ids, second_ids)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{12}", i) for i in first_ids | second_ids))
        self.assertNotIn(probation.DEVICE_ID_PLACEHOLDER, first + second)

    def test_playlist_unchanged_while_overlap_is_disabled(self):
        # Same links as stock Dispatcharr, even when a device ID is requested
        for content in (self._get_m3u(), self._get_m3u(device_id="living-room")):
            links = self._stream_links(content)
            self.assertEqual(len(links), 2)
            self.assertTrue(all(re.fullmatch(r"http://testserver/proxy/ts/stream/[0-9a-f-]+", link) for link in links), links)

    def test_media_servers_get_no_device_id(self):
        self._enable_overlap()

        for user_agent in ("Jellyfin-Server/10.10.7", "Emby/4.8.10.0", "PlexMediaServer/1.41.0.8992"):
            content = self._get_m3u(user_agent=user_agent)
            links = self._stream_links(content)
            self.assertEqual(len(links), 2, user_agent)
            self.assertTrue(
                all(re.fullmatch(r"http://testserver/proxy/ts/stream/[0-9a-f-]+", link) for link in links),
                (user_agent, links),
            )

    def test_media_server_and_player_sharing_the_playlist_cache(self):
        self._enable_overlap()

        player = self._get_m3u(user_agent="TiviMate/5.1.6")
        # Within the 2-second cache: same cached content, different result per requester
        jellyfin = self._get_m3u(user_agent="Jellyfin-Server/10.10.7")

        self.assertNotIn(None, self._device_ids(player))
        self.assertEqual(self._device_ids(jellyfin), {None})

    def test_media_server_keeps_other_link_parameters(self):
        self._enable_overlap()

        content = self._get_m3u(user_agent="Emby/4.8.10.0", output_format="mpegts")

        for link in self._stream_links(content):
            self.assertEqual(parse_qs(urlparse(link).query), {"output_format": ["mpegts"]})

    def test_requested_device_id_is_kept(self):
        self._enable_overlap()

        content = self._get_m3u(device_id="living-room")

        self.assertEqual(self._device_ids(content), {"living-room"})

    def test_invalid_requested_device_id_is_replaced(self):
        self._enable_overlap()

        content = self._get_m3u(device_id="bad id!")

        (device_id,) = self._device_ids(content)
        self.assertRegex(device_id, r"^[0-9a-f]{12}$")

    def test_xtream_playlist_links_get_device_id(self):
        self._enable_overlap()
        user = User.objects.create_user(
            username=f"xc-{uuid4().hex[:8]}",
            password="pass",
            user_level=10,
            custom_properties={"xc_password": "xcpass"},
        )

        response = self.client.get(
            "/get.php", {"username": user.username, "password": "xcpass", "type": "m3u_plus"}
        )
        self.assertEqual(response.status_code, 200)
        content = _response_text(response)

        links = self._stream_links(content)
        self.assertTrue(links)
        self.assertTrue(all(f"/live/{user.username}/xcpass/" in link for link in links))
        self.assertEqual(len(self._device_ids(content)), 1)
        self.assertNotIn(None, self._device_ids(content))
