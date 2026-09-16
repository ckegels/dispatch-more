"""An HDHomeRun whose tuner count comes from the address (apps.proxy.live_proxy.hdhr_tuner_views).

Dispatcharr counts a custom stream per channel as a tuner, so a setup with a fallback stream
on every channel advertises hundreds of them while its providers allow two. These endpoints
say the number instead of working it out.
"""

import json

from django.test import TestCase

from apps.channels.models import Channel, ChannelProfile, Stream
from apps.m3u.models import M3UAccount, M3UAccountProfile


class TunerCountTests(TestCase):
    def setUp(self):
        self.account = M3UAccount.objects.create(
            name="TiviBridge", account_type="XC", server_url="http://a.example", is_active=True
        )
        M3UAccountProfile.objects.filter(m3u_account=self.account).update(max_streams=1)
        self.profile = ChannelProfile.objects.create(name="austria")
        channel = Channel.objects.create(channel_number=1, name="ORF 1")
        # The fallback stream per channel that inflates Dispatcharr's own count
        for number in range(5):
            Stream.objects.create(
                name=f"custom {number}",
                url=f"http://a.example/{number}.ts",
                is_custom=True,
                m3u_account=self.account,
            )
        self.channel = channel

    def _discover(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)

    def test_the_count_comes_from_the_address(self):
        from apps.m3u.utils import calculate_tuner_count

        calculated = calculate_tuner_count(minimum=1, unlimited_default=10)
        self.assertGreater(calculated, 2)  # the custom streams are counted by Dispatcharr

        data = self._discover("/proxy/hdhr/austria/tuners/2/discover.json")

        self.assertEqual(data["TunerCount"], 2)
        # And it keeps the media server on this address, not on the counting one
        self.assertTrue(data["BaseURL"].endswith("/proxy/hdhr/austria/tuners/2"))
        self.assertTrue(data["LineupURL"].endswith("/proxy/hdhr/austria/tuners/2/lineup.json"))
        # Its own device, so it cannot collide with the same profile added the usual way
        self.assertTrue(data["DeviceID"].endswith("-t2"))
        self.assertIn("2 tuners", data["FriendlyName"])

    def test_dispatcharr_still_counts_for_itself(self):
        """The stock endpoint is untouched: this only adds another way in."""
        from apps.m3u.utils import calculate_tuner_count

        data = self._discover("/hdhr/austria/discover.json")
        self.assertEqual(
            data["TunerCount"], calculate_tuner_count(minimum=1, unlimited_default=10)
        )

    def test_an_output_profile_still_fits_in_the_address(self):
        from core.models import OutputProfile

        output = OutputProfile.objects.create(name="Remux", is_active=True)
        data = self._discover(
            f"/proxy/hdhr/austria/output_profile/{output.id}/tuners/3/discover.json"
        )
        self.assertEqual(data["TunerCount"], 3)
        self.assertIn(f"/output_profile/{output.id}/tuners/3", data["BaseURL"])

    def test_a_silly_number_is_brought_back_into_range(self):
        self.assertEqual(
            self._discover("/proxy/hdhr/austria/tuners/9999/discover.json")["TunerCount"], 64
        )

    def test_the_lineup_is_dispatcharrs_own(self):
        ours = self.client.get("/proxy/hdhr/austria/tuners/2/lineup.json")
        theirs = self.client.get("/hdhr/austria/lineup.json")
        self.assertEqual(ours.status_code, 200)
        self.assertEqual(json.loads(ours.content), json.loads(theirs.content))

        status = self.client.get("/proxy/hdhr/austria/tuners/2/lineup_status.json")
        self.assertEqual(status.status_code, 200)

    def test_an_unknown_document_is_not_found(self):
        self.assertEqual(
            self.client.get("/proxy/hdhr/austria/tuners/2/something.json").status_code, 404
        )
