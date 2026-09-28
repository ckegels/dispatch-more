"""Remembered streams (apps.channels.pairings): which provider stream is which channel.

What a stream is on a channel for is decided once; a provider renaming it must not undo
that, and taking it off by hand must.
"""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import channel_manager, pairings
from apps.channels.models import Channel, ChannelGroup, ChannelStream, Stream
from apps.m3u.models import M3UAccount


class PairingTests(TestCase):
    def setUp(self):
        self.a = M3UAccount.objects.create(name="TiviBridge", account_type="XC", server_url="http://a", is_active=True)
        self.b = M3UAccount.objects.create(name="Digitalizard", account_type="XC", server_url="http://b", is_active=True)
        self.group = ChannelGroup.objects.create(name="┃BE┃ BELGIUM")
        self.fallback = Stream.objects.create(name="could not dispatch", url="http://local/f", is_custom=True)
        self.vrt = Channel.objects.create(name="┃BE┃ VRT 1", channel_number=1, channel_group=self.group)
        self.tivi = self.stream("┃BE┃ VRT 1 FHD", self.a, 101)
        self.digi = self.stream("BE| VRT 1 HD", self.b, 5501, tvg_id="vrt1.be")
        for order, stream in enumerate([self.tivi, self.digi, self.fallback]):
            ChannelStream.objects.create(channel=self.vrt, stream=stream, order=order)

    def stream(self, name, account, sid=None, **extra):
        return Stream.objects.create(
            name=name, url=f"http://{account.name}/{sid or name}", m3u_account=account,
            channel_group=self.group, stream_id=sid, **extra,
        )

    def order(self, channel):
        return list(
            ChannelStream.objects.filter(channel=channel).order_by("order").values_list("stream__name", flat=True)
        )

    def remembered(self):
        return pairings.load()["channels"].get(str(self.vrt.id), [])

    def test_everything_already_matched_is_written_down_but_not_the_fallback(self):
        counts = pairings.sync()
        self.assertEqual(counts["added"], 2)
        entries = {e["stream"]: e for e in self.remembered()}
        self.assertEqual(set(entries), {self.tivi.id, self.digi.id})
        self.assertEqual(entries[self.digi.id]["sid"], 5501)
        self.assertEqual(entries[self.digi.id]["tvg"], "vrt1.be")
        self.assertEqual(entries[self.digi.id]["account"], self.b.id)

    def test_writing_it_down_twice_adds_nothing(self):
        pairings.sync()
        self.assertEqual(pairings.sync()["added"], 0)
        self.assertEqual(len(self.remembered()), 2)

    def test_a_renamed_stream_goes_back_where_the_old_one_was(self):
        pairings.sync()
        # The provider renames it: the hash makes a new stream, the old one goes stale
        renamed = self.stream("BE| VRT 1 FHD", self.b, 5501, tvg_id="vrt1.be")
        Stream.objects.filter(id=self.digi.id).update(is_stale=True)
        self.assertEqual(pairings.after_refresh(self.b.id), 1)
        self.assertEqual(self.order(self.vrt), ["┃BE┃ VRT 1 FHD", "BE| VRT 1 FHD", "could not dispatch"])
        self.assertIn(renamed.id, {e["stream"] for e in self.remembered()})

    def test_one_deleted_outright_is_found_by_its_number_and_goes_before_the_fallback(self):
        pairings.sync()
        self.digi.delete()
        self.stream("BE| VRT 1 FHD", self.b, 5501)
        self.assertEqual(pairings.after_refresh(self.b.id), 1)
        self.assertEqual(self.order(self.vrt), ["┃BE┃ VRT 1 FHD", "BE| VRT 1 FHD", "could not dispatch"])

    def test_without_a_number_the_same_name_finds_it(self):
        self.digi.stream_id = None
        self.digi.save()
        pairings.sync()
        self.digi.delete()
        self.stream("BE| VRT 1 HD", self.b)
        self.assertEqual(pairings.after_refresh(self.b.id), 1)

    def test_nothing_is_guessed_when_the_number_is_gone(self):
        pairings.sync()
        self.digi.delete()
        self.stream("BE| VRT 2 HD", self.b, 5502)
        self.assertEqual(pairings.after_refresh(self.b.id), 0)
        self.assertEqual(self.order(self.vrt), ["┃BE┃ VRT 1 FHD", "could not dispatch"])
        lost = [e for e in self.remembered() if e["account"] == self.b.id]
        self.assertTrue(lost and lost[0].get("lost_at"), "kept, and looked for again next time")

    def test_one_taken_off_by_hand_is_forgotten_not_put_back(self):
        pairings.sync()
        ChannelStream.objects.filter(channel=self.vrt, stream=self.digi).delete()
        self.assertEqual(pairings.sync()["forgotten"], 1)
        self.assertNotIn(self.digi.id, {e["stream"] for e in self.remembered()})
        self.assertEqual(pairings.after_refresh(self.b.id), 0)
        self.assertNotIn("BE| VRT 1 HD", self.order(self.vrt))

    def test_one_stream_check_parked_is_left_to_stream_check(self):
        pairings.sync()
        ChannelStream.objects.filter(channel=self.vrt, stream=self.digi).delete()
        with patch("apps.channels.stream_check.parked_ids", return_value={self.digi.id}):
            self.assertEqual(pairings.sync()["forgotten"], 0)
        self.assertIn(self.digi.id, {e["stream"] for e in self.remembered()})

    def test_switched_off_nothing_is_put_back(self):
        pairings.sync()
        channel_manager.save_settings({**channel_manager.load_settings(), "remember_pairings": False})
        self.digi.delete()
        self.stream("BE| VRT 1 FHD", self.b, 5501)
        self.assertEqual(pairings.after_refresh(self.b.id), 0)

    def test_the_first_refresh_writes_down_what_is_matched(self):
        self.assertEqual(pairings.after_refresh(self.a.id), 0)
        self.assertEqual(len(self.remembered()), 2)

    def test_the_settings_page_can_save_and_count(self):
        admin = User.objects.create_user(username="admin", password="x", user_level=10)
        client = APIClient()
        client.force_authenticate(user=admin)
        url = "/api/channels/channel-manager/pairings/"
        self.assertEqual(client.get(url).json()["streams"], 0)
        answer = client.post(url, {"action": "save"}, format="json").json()
        self.assertEqual((answer["streams"], answer["channels"]), (2, 1))
        self.assertEqual(client.post(url, {"action": "forget"}, format="json").json()["streams"], 0)
