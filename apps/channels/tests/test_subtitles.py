"""The Subtitles tab's list: what each channel's streams carry, and the language it speaks."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.channels import subtitles
from apps.channels.models import Channel, ChannelGroup, ChannelStream, Stream
from apps.m3u.models import M3UAccount


class SubtitlesTabTests(TestCase):
    def setUp(self):
        group = ChannelGroup.objects.create(name="┃NL┃ NEDERLAND")
        provider = M3UAccount.objects.create(name="Provider", server_url="http://x.invalid/list.m3u")
        self.npo = Channel.objects.create(name="┃NL┃ NPO 1", channel_number=1, channel_group=group)
        self.vrt = Channel.objects.create(name="┃BE┃ VRT 1", channel_number=2, channel_group=group)
        self.nbc = Channel.objects.create(name="NBC 56", channel_number=3, channel_group=group)
        teletext = Stream.objects.create(name="NPO 1 HD", m3u_account=provider, url="http://x.invalid/1", stream_stats={
            "subtitles": [{"kind": "teletext", "lang": "dut", "hearing_impaired": False}],
            "audio_languages": ["dut"], "subtitles_checked_at": "2026-10-01T20:00:00+00:00"})
        bare = Stream.objects.create(name="NPO 1 SD", m3u_account=provider, url="http://x.invalid/2",
                                     stream_stats={"subtitles": [], "audio_languages": []})
        none = Stream.objects.create(name="VRT 1 HD", m3u_account=provider, url="http://x.invalid/3",
                                     stream_stats={"subtitles": [], "audio_languages": []})
        unchecked = Stream.objects.create(name="NBC 56", m3u_account=provider, url="http://x.invalid/4", stream_stats={"resolution": "1280x720"})
        fallback = Stream.objects.create(name="Could Not Dispatch", url="http://x.invalid/5", is_custom=True)
        for order, (channel, stream) in enumerate([(self.npo, bare), (self.npo, teletext), (self.vrt, none),
                                                   (self.nbc, unchecked), (self.nbc, fallback)]):
            ChannelStream.objects.create(channel=channel, stream=stream, order=order)

    def row(self, channel):
        return next(r for r in subtitles.rows() if r["id"] == channel.id)

    def test_what_each_channel_carries(self):
        npo = self.row(self.npo)
        self.assertEqual((npo["state"], npo["subtitles"]),
                         ("found", [{"kind": "teletext", "lang": "dut", "hearing_impaired": False}]))
        self.assertEqual(len(npo["streams"]), 2, "each stream's own result, the fallback left out")
        self.assertEqual(self.row(self.vrt)["state"], "none")
        self.assertEqual(self.row(self.nbc)["state"], "unchecked")
        counts = subtitles.summary(subtitles.rows())
        self.assertEqual((counts["found"], counts["none"], counts["unchecked"], counts["teletext"]), (1, 1, 1, 1))

    def test_the_language_spoken(self):
        self.assertEqual((self.row(self.npo)["spoken"], self.row(self.npo)["spoken_from"]), ("dut", "audio"))
        self.assertEqual((self.row(self.vrt)["spoken"], self.row(self.vrt)["spoken_from"]), ("dut", "country"))
        subtitles.set_spoken(self.vrt.id, "fra")
        self.assertEqual((self.row(self.vrt)["spoken"], self.row(self.vrt)["spoken_from"]), ("fra", "set"))
        subtitles.set_spoken(self.vrt.id, "")
        self.assertEqual(self.row(self.vrt)["spoken_from"], "country")

    def test_the_page(self):
        admin = get_user_model().objects.create_user(username="admin", password="x", user_level=10)
        client = APIClient()
        client.force_authenticate(admin)
        page = client.get("/api/channels/subtitles/").json()
        self.assertEqual(page["summary"]["channels"], 3)
        page = client.put("/api/channels/subtitles/", {"channel": self.nbc.id, "spoken": "eng"}, format="json").json()
        self.assertEqual(next(r for r in page["rows"] if r["id"] == self.nbc.id)["spoken"], "eng")
