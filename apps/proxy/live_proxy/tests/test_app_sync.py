"""arrTV's start-up check (apps.proxy.live_proxy.app_sync).

The app compares these stamps with the ones it saved and downloads only what changed, so the
rules that matter are: a stamp moves whenever something the app shows changes, stays put when
nothing did (or the change is invisible to that login), and the guide links hold exactly the
guides that login's channels use. The plan is fork/guide-loading-plan.md.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.epg.models import EPGData, EPGSource, ProgramData
from apps.proxy.live_proxy import app_sync

SYNC = "/api/core/app-sync/"
LINKS = "/api/core/app-guide-links/"


class AppSyncTests(TestCase):
    def setUp(self):
        from apps.channels.models import Channel, ChannelGroup

        # No broker here: anything a save queues goes nowhere instead of waiting on one
        patcher = mock.patch("celery.app.task.Task.apply_async")
        patcher.start()
        self.addCleanup(patcher.stop)
        app_sync._CACHE.clear()
        self.addCleanup(app_sync._CACHE.clear)

        self.admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.alice = User.objects.create_user(username="alice", password="x", user_level=1)
        self.source = EPGSource.objects.create(name="EPGShare DE", source_type="xmltv")
        self.other_source = EPGSource.objects.create(name="EPGShare FR", source_type="xmltv")
        self.ard = EPGData.objects.create(tvg_id="DasErste.de", name="Das Erste", epg_source=self.source)
        self.zdf = EPGData.objects.create(tvg_id="ZDF.de", name="ZDF", epg_source=self.source)
        # A guide no channel uses: the reason /api/epg/epgdata/ was too big for a TV
        self.unused = EPGData.objects.create(tvg_id="TF1.fr", name="TF1", epg_source=self.other_source)
        self.group = ChannelGroup.objects.create(name="Germany")
        self.channel = Channel.objects.create(
            name="Das Erste", channel_number=1, channel_group=self.group, epg_data=self.ard,
        )
        self.second = Channel.objects.create(
            name="ZDF", channel_number=2, channel_group=self.group, epg_data=self.zdf,
        )
        now = timezone.now()
        ProgramData.objects.create(
            epg=self.ard, title="Tagesschau", start_time=now, end_time=now + timedelta(minutes=15),
        )

    def sync(self, user=None):
        app_sync._CACHE.clear()
        client = APIClient()
        client.force_authenticate(user=user or self.alice)
        response = client.get(SYNC)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def links(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.alice)
        response = client.get(LINKS)
        self.assertEqual(response.status_code, 200)
        return response.json()

    # -- the answer ---------------------------------------------------------------------

    def test_a_login_is_needed(self):
        self.assertEqual(APIClient().get(SYNC).status_code, 401)
        self.assertEqual(APIClient().get(LINKS).status_code, 401)

    def test_capabilities_say_where_to_ask(self):
        client = APIClient()
        client.force_authenticate(user=self.alice)
        capabilities = client.get("/api/core/capabilities/").json()
        self.assertEqual(capabilities["sync_url"], SYNC)
        self.assertEqual(capabilities["guide_links_url"], LINKS)

    def test_nothing_changed_gives_the_same_stamps(self):
        first, second = self.sync(), self.sync()
        for key in ("lineup", "guide_links", "guides"):
            self.assertEqual(first[key], second[key], key)
        self.assertEqual(first["channels"], 2)
        self.assertEqual(first["programmes"], 1)
        self.assertEqual(first["guide_links_url"], LINKS)

    def test_a_second_ask_within_seconds_is_answered_from_memory(self):
        client = APIClient()
        client.force_authenticate(user=self.alice)
        client.get(SYNC)
        with mock.patch.object(app_sync, "_lineup", side_effect=AssertionError("read again")):
            self.assertEqual(client.get(SYNC).status_code, 200)

    # -- lineup -------------------------------------------------------------------------

    def test_the_lineup_stamp_moves_with_what_the_app_shows(self):
        from apps.channels.models import Channel, ChannelOverride

        before = self.sync()["lineup"]
        Channel.objects.filter(id=self.channel.id).update(name="Das Erste HD")  # no auto_now here
        renamed = self.sync()["lineup"]
        self.assertNotEqual(before, renamed)
        ChannelOverride.objects.create(channel=self.channel, channel_number=101)
        self.assertNotEqual(renamed, self.sync()["lineup"])
        added = Channel.objects.create(name="3sat", channel_number=3, channel_group=self.group)
        with_new = self.sync()["lineup"]
        Channel.objects.filter(id=added.id).update(hidden_from_output=True)
        self.assertNotEqual(with_new, self.sync()["lineup"])

    def test_a_group_renamed_moves_the_lineup(self):
        before = self.sync()["lineup"]
        self.group.name = "Deutschland"
        self.group.save()
        self.assertNotEqual(before, self.sync()["lineup"])

    def test_streams_changing_behind_a_channel_do_not_move_the_lineup(self):
        # Stream Check reorders streams all day; the app does not show them, so it must not
        # download the lineup again because of it
        from apps.channels.models import ChannelStream, Stream

        before = self.sync()["lineup"]
        stream = Stream.objects.create(name="Das Erste FHD", url="http://x/1")
        ChannelStream.objects.create(channel=self.channel, stream=stream, order=0)
        self.assertEqual(before, self.sync()["lineup"])

    def test_a_channel_above_the_logins_level_is_not_in_its_lineup(self):
        from apps.channels.models import Channel

        before = self.sync()
        secret = Channel.objects.create(name="Admin only", channel_number=9, user_level=10)
        after = self.sync()
        self.assertEqual(before["lineup"], after["lineup"])
        self.assertEqual(after["channels"], 2)
        self.assertEqual(self.sync(self.admin)["channels"], 3)
        Channel.objects.filter(id=secret.id).update(name="Still admin only")
        self.assertEqual(before["lineup"], self.sync()["lineup"])

    def test_the_logins_channel_profiles_count(self):
        from apps.channels.models import ChannelProfile, ChannelProfileMembership

        before = self.sync()
        profile = ChannelProfile.objects.create(name="Kids")
        ChannelProfileMembership.objects.update_or_create(
            channel_profile=profile, channel=self.channel, defaults={"enabled": True}
        )
        ChannelProfileMembership.objects.update_or_create(
            channel_profile=profile, channel=self.second, defaults={"enabled": False}
        )
        self.alice.channel_profiles.add(profile)
        after = self.sync()
        self.assertNotEqual(before["lineup"], after["lineup"])
        self.assertEqual(after["channels"], 1)

    def test_admins_profiles_count_too_since_the_app_filters_by_them_itself(self):
        from apps.channels.models import ChannelProfile, ChannelProfileMembership

        before = self.sync(self.admin)["lineup"]
        profile = ChannelProfile.objects.create(name="Living room")
        ChannelProfileMembership.objects.update_or_create(
            channel_profile=profile, channel=self.channel, defaults={"enabled": True}
        )
        self.admin.channel_profiles.add(profile)
        self.assertNotEqual(before, self.sync(self.admin)["lineup"])

    # -- guide links ----------------------------------------------------------------------

    def test_guide_links_are_only_the_guides_the_channels_use(self):
        links = self.links()
        self.assertEqual(
            links,
            [
                {"id": self.ard.id, "tvg_id": "DasErste.de", "epg_source": self.source.id, "source_type": "xmltv"},
                {"id": self.zdf.id, "tvg_id": "ZDF.de", "epg_source": self.source.id, "source_type": "xmltv"},
            ],
        )

    def test_a_guide_put_on_by_an_override_is_a_link_too(self):
        from apps.channels.models import ChannelOverride

        before = self.sync()["guide_links"]
        ChannelOverride.objects.create(channel=self.second, epg_data=self.unused)
        self.assertIn(self.unused.id, [g["id"] for g in self.links()])
        self.assertNotEqual(before, self.sync()["guide_links"])

    def test_changing_a_channels_guide_moves_the_links_stamp(self):
        from apps.channels.models import Channel

        before = self.sync()["guide_links"]
        Channel.objects.filter(id=self.second.id).update(epg_data=self.unused)
        self.assertNotEqual(before, self.sync()["guide_links"])

    def test_a_guides_tvg_id_changing_moves_the_links_stamp(self):
        before = self.sync()["guide_links"]
        EPGData.objects.filter(id=self.zdf.id).update(tvg_id="ZDF.HD.de")
        self.assertNotEqual(before, self.sync()["guide_links"])

    def test_an_unused_guide_changing_moves_nothing(self):
        before = self.sync()
        EPGData.objects.filter(id=self.unused.id).update(tvg_id="TF1.HD.fr")
        after = self.sync()
        self.assertEqual(before["guide_links"], after["guide_links"])
        self.assertEqual(before["lineup"], after["lineup"])

    # -- guides -----------------------------------------------------------------------------

    def test_a_source_refresh_moves_the_guides_stamp(self):
        before = self.sync()
        EPGSource.objects.filter(id=self.other_source.id).update(updated_at=timezone.now())
        after = self.sync()
        self.assertNotEqual(before["guides"], after["guides"])
        self.assertEqual(before["lineup"], after["lineup"])
        refreshed = {s["id"]: s["refreshed"] for s in after["sources"]}
        self.assertIsNotNone(refreshed[self.other_source.id])

    def test_new_programmes_for_the_channels_move_the_guides_stamp(self):
        before = self.sync()["guides"]
        now = timezone.now()
        ProgramData.objects.create(
            epg=self.zdf, title="heute", start_time=now, end_time=now + timedelta(minutes=20),
        )
        self.assertNotEqual(before, self.sync()["guides"])

    def test_programmes_of_a_guide_nobody_uses_do_not_count(self):
        before = self.sync()
        now = timezone.now()
        ProgramData.objects.create(
            epg=self.unused, title="Journal", start_time=now, end_time=now + timedelta(minutes=30),
        )
        after = self.sync()
        self.assertEqual(before["guides"], after["guides"])
        self.assertEqual(after["programmes"], 1)

    def test_sources_never_give_away_their_address(self):
        # An XMLTV URL often holds a provider login; the stamp covers it, the answer does not
        EPGSource.objects.filter(id=self.source.id).update(url="http://user:secret@provider/xmltv.php")
        self.assertNotIn("secret", str(self.sync()))
