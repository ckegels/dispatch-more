"""arrTV's "Wrong guide? Choose another" (apps.proxy.live_proxy.app_guides).

Somebody watching a channel picks the guide that matches the picture from a list of the
guides that could be this channel, each with what is on it now. The design is
fork/arrTV-guide-choice.md; these check its rules one by one.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import guide_manager
from apps.epg.models import EPGData, EPGSource, ProgramData
from apps.proxy.live_proxy import app_devices, app_guides
from apps.proxy.live_proxy.tests.test_probation import FakeRedis


def no_background_jobs(test):
    """Every Celery task queued by what a test touches (Dispatcharr's own signals included)
    goes nowhere: there is no broker here, and a queued task waits on one for twenty seconds."""
    patcher = mock.patch("celery.app.task.Task.apply_async")
    patcher.start()
    test.addCleanup(patcher.stop)

URL = "/api/core/app-guide/"
DEVICE = "3f2a9c1e-0b7d-4e21-9a55-0f1c2d3e4f50"


class GuideChoiceTests(TestCase):
    def setUp(self):
        from apps.channels.models import Channel, ChannelStream, Stream

        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        no_background_jobs(self)
        # Nothing queued for real: the preload and reads are Celery tasks
        for target in (
            "apps.channels.tasks.read_guide_programmes.delay",
            "apps.channels.tasks.preload_guide_choices.delay",
            "apps.epg.tasks.parse_programs_for_tvg_id.delay",
        ):
            patcher = mock.patch(target)
            setattr(self, target.split(".")[-2], patcher.start())
            self.addCleanup(patcher.stop)
        self.redis = FakeRedis()
        patcher = mock.patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.at = EPGSource.objects.create(name="EPGShare AT", source_type="xmltv")
        self.de = EPGSource.objects.create(name="EPGShare DE", source_type="xmltv")
        self.dummy = EPGSource.objects.create(name="Dummy", source_type="dummy")
        self.wrong = EPGData.objects.create(tvg_id="ORF1.wrong", name="ORF1 wrong", epg_source=self.at)
        self.right = EPGData.objects.create(tvg_id="ORF1HD.de", name="ORF 1 HD", epg_source=self.de)
        self.empty = EPGData.objects.create(tvg_id="ORF1.at", name="ORF1.at", epg_source=self.at)
        self.made_up = EPGData.objects.create(tvg_id="ORF1.dummy", name="ORF 1", epg_source=self.dummy)
        moment = timezone.now()
        self.airing(self.wrong, "Bundesland heute")
        self.airing(self.right, "Zeit im Bild")
        ProgramData.objects.create(
            epg=self.right, title="Wetter",
            start_time=moment + timedelta(minutes=20), end_time=moment + timedelta(minutes=30),
        )
        self.airing(self.made_up, "ORF 1")

        self.channel = Channel.objects.create(name="┃AT┃ ORF 1", channel_number=101, epg_data=self.wrong)
        # Every test channel ends in the fallback stream, kept last
        stream = Stream.objects.create(name="┃AT┃ ORF 1 FHD", url="http://x/1")
        self.fallback = Stream.objects.create(name="Could Not Dispatch", url="http://local/f", is_custom=True)
        ChannelStream.objects.create(channel=self.channel, stream=stream, order=0)
        ChannelStream.objects.create(channel=self.channel, stream=self.fallback, order=1)

        # The Guides tab's matcher, as it would answer: current first, then by score
        self.matched = [
            {"id": self.wrong.id, "score": 70},
            {"id": self.right.id, "score": 91},
            {"id": self.empty.id, "score": 88},
            {"id": self.made_up.id, "score": 99},
        ]
        patcher = mock.patch(
            "apps.channels.channel_manager.guide_candidates",
            side_effect=lambda *a, **k: [dict(e) for e in self.matched],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.alice = User.objects.create_user(username="alice", password="x", user_level=1)
        app_devices.save_settings({"guide_choice": True})
        self.preload_guide_choices.reset_mock()

    def airing(self, guide, title):
        moment = timezone.now()
        ProgramData.objects.create(
            epg=guide, title=title,
            start_time=moment - timedelta(minutes=10), end_time=moment + timedelta(minutes=20),
        )

    def api(self, user=None, device=True):
        client = APIClient()
        client.force_authenticate(user=user or self.alice)
        if device:
            client.credentials(HTTP_X_DISPATCH_DEVICE=DEVICE, HTTP_X_DISPATCH_DEVICE_NAME="Living room SHIELD")
        return client

    def ask(self, user=None):
        return self.api(user).get(URL, {"channel": str(self.channel.uuid)})

    def choose(self, epg, user=None, device=True, **extra):
        return self.api(user, device).post(
            URL, {"channel": str(self.channel.uuid), "epg_id": epg.id}, format="json", **extra
        )

    # -- the switch -----------------------------------------------------------------------

    def test_off_both_endpoints_refuse_and_the_capabilities_say_so(self):
        app_devices.save_settings({"guide_choice": False})
        self.assertEqual(self.ask().status_code, 403)
        self.assertEqual(self.choose(self.right).status_code, 403)
        self.assertFalse(app_devices.capabilities()["guide_choice"])
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.wrong.id)

    def test_on_the_capabilities_say_where(self):
        capabilities = app_devices.capabilities()
        self.assertTrue(capabilities["guide_choice"])
        self.assertEqual(capabilities["guide_choice_url"], URL)

    def test_switching_on_starts_the_preload_and_off_stops_keeping(self):
        app_devices.save_settings({"guide_choice": False})
        app_guides.note_read({self.empty.id: 3})
        self.assertEqual(app_guides.kept_ids(), set(), "off keeps nothing")
        app_devices.save_settings({"guide_choice": True})
        self.preload_guide_choices.assert_called_once()
        app_guides.note_read({self.empty.id: 3})
        self.assertEqual(app_guides.kept_ids(), {self.empty.id})
        app_devices.save_settings({"guide_choice": False})
        self.assertEqual(app_guides.kept_ids(), set())
        self.assertEqual(app_guides._record().get("ids"), {}, "the record is emptied")

    # -- the list -------------------------------------------------------------------------

    def test_only_guides_with_something_on_now_best_first_and_no_dummy(self):
        answer = self.ask().json()
        self.assertEqual([g["epg_id"] for g in answer["guides"]], [self.right.id])
        guide = answer["guides"][0]
        self.assertEqual(guide["now"]["title"], "Zeit im Bild")
        self.assertEqual(guide["next"]["title"], "Wetter")
        self.assertEqual(guide["source"], {"id": self.de.id, "name": "EPGShare DE"})
        self.assertEqual(guide["score"], 91)

    def test_the_current_guide_is_shown_apart_and_never_offered(self):
        answer = self.ask().json()
        self.assertEqual(answer["current"]["epg_id"], self.wrong.id)
        self.assertEqual(answer["current"]["now"]["title"], "Bundesland heute")
        self.assertNotIn(self.wrong.id, [g["epg_id"] for g in answer["guides"]])
        self.assertEqual(answer["channel"]["number"], 101)

    def test_a_current_guide_with_nothing_on_is_still_shown(self):
        ProgramData.objects.filter(epg=self.wrong).delete()
        current = self.ask().json()["current"]
        self.assertEqual(current["epg_id"], self.wrong.id)
        self.assertIsNone(current["now"])

    def test_a_channel_on_no_guide_has_no_current(self):
        self.channel.epg_data = None
        self.channel.save()
        self.assertIsNone(self.ask().json()["current"])

    def test_at_most_twenty_in_all_by_score(self):
        more = []
        for i in range(30):
            guide = EPGData.objects.create(tvg_id=f"x{i}", name=f"X {i}", epg_source=self.de)
            self.airing(guide, f"Show {i}")
            more.append({"id": guide.id, "score": 50 - i})
        self.matched = self.matched + more
        guides = self.ask().json()["guides"]
        self.assertEqual(len(guides), app_guides.LIST_MOST)
        self.assertEqual(guides[0]["epg_id"], self.right.id)
        scores = [g["score"] for g in guides]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_with_little_found_it_widens_a_step_at_a_time(self):
        """A list of maybes rather than none: any confidence, then without the matching
        settings' limits, then a plain search on the channel's name."""
        wide = EPGData.objects.create(tvg_id="ORF1.other", name="ORF Eins", epg_source=self.de)
        found = EPGData.objects.create(tvg_id="orf.search", name="ORF Search", epg_source=self.de)
        self.airing(wide, "Wide show")
        self.airing(found, "Search show")
        calls = []

        def answer(*args, **kwargs):
            calls.append(kwargs)
            if kwargs.get("search"):
                return [{"id": found.id, "score": None}]
            if kwargs.get("wide"):
                return [{"id": wide.id, "score": 30}]
            return [{"id": self.wrong.id, "score": 70}]

        with mock.patch("apps.channels.channel_manager.guide_candidates", side_effect=answer):
            guides = self.ask().json()["guides"]
        self.assertEqual([g["epg_id"] for g in guides], [wide.id, found.id])
        self.assertEqual(calls[0]["min_score"], app_guides.ANY_SCORE)
        self.assertFalse(calls[0]["wide"])
        self.assertTrue(calls[1]["wide"])
        self.assertEqual(calls[2]["search"], "orf 1")
        self.assertEqual(calls[3]["search"], "orf", "then its longest word alone")

    def test_once_the_list_is_full_it_widens_no_further(self):
        more = []
        for i in range(25):
            guide = EPGData.objects.create(tvg_id=f"y{i}", name=f"Y {i}", epg_source=self.de)
            self.airing(guide, f"Show {i}")
            more.append({"id": guide.id, "score": 90 - i})
        with mock.patch("apps.channels.channel_manager.guide_candidates", return_value=more) as matcher:
            guides = self.ask().json()["guides"]
        self.assertEqual(len(guides), app_guides.LIST_MOST)
        self.assertEqual(matcher.call_count, 1)

    def test_load_more_gives_the_next_ones_down_until_there_are_none(self):
        more = []
        for i in range(45):
            guide = EPGData.objects.create(tvg_id=f"z{i}", name=f"Z {i}", epg_source=self.de)
            self.airing(guide, f"Show {i}")
            more.append({"id": guide.id, "score": 80 - i})
        self.matched = more
        client = self.api()
        first = client.get(URL, {"channel": str(self.channel.uuid)}).json()
        self.assertEqual(len(first["guides"]), app_guides.LIST_MOST)
        self.assertTrue(first["more"])
        shown = [g["epg_id"] for g in first["guides"]]
        second = client.get(URL, {"channel": str(self.channel.uuid), "shown": ",".join(map(str, shown))}).json()
        self.assertEqual([g["epg_id"] for g in second["guides"]], [e["id"] for e in more[20:40]])
        shown += [g["epg_id"] for g in second["guides"]]
        third = client.get(URL, {"channel": str(self.channel.uuid), "shown": ",".join(map(str, shown))}).json()
        self.assertEqual([g["epg_id"] for g in third["guides"]], [e["id"] for e in more[40:]])
        self.assertFalse(third["more"])

    def test_each_page_looks_deeper(self):
        depths = []

        def answer(*args, **kwargs):
            depths.append(kwargs.get("limit"))
            return []

        with mock.patch("apps.channels.channel_manager.guide_candidates", side_effect=answer):
            self.api().get(URL, {"channel": str(self.channel.uuid), "shown": ",".join(str(i) for i in range(1, 41))})
        self.assertEqual(depths[0], app_guides.LOOK_AT + 80 + 1)

    def test_only_the_chosen_sources_are_offered(self):
        other = EPGData.objects.create(tvg_id="ORF1.at2", name="ORF 1 AT", epg_source=self.at)
        self.airing(other, "Zeit im Bild")
        self.matched = self.matched + [{"id": other.id, "score": 60}]
        app_devices.save_settings({"guide_choice_sources": [self.at.id]})
        self.assertEqual([g["epg_id"] for g in self.ask().json()["guides"]], [other.id])

    def test_guides_never_read_are_read_and_the_answer_says_it_is_reading(self):
        answer = self.ask().json()
        self.assertTrue(answer["reading"])
        self.read_guide_programmes.assert_called_once()
        by_source = self.read_guide_programmes.call_args[0][0]
        self.assertEqual(by_source, {str(self.at.id): [self.empty.id]})
        self.assertEqual(self.read_guide_programmes.call_args[1]["record"], "app-guide")

    def test_asking_again_while_they_are_read_does_not_read_them_twice(self):
        self.ask()
        self.ask()
        self.assertEqual(self.read_guide_programmes.call_count, 1)

    def test_once_read_and_found_empty_it_is_not_read_again(self):
        app_guides.note_read({self.empty.id: 0}, how="asked")
        self.redis.delete(app_guides.READING_KEY.format(channel=self.channel.id))
        self.assertFalse(self.ask().json()["reading"])
        self.read_guide_programmes.assert_not_called()

    def test_a_login_that_cannot_see_the_channel_gets_nothing(self):
        self.channel.user_level = 10
        self.channel.save()
        self.assertEqual(self.ask().status_code, 404)
        self.assertEqual(self.choose(self.right).status_code, 404)
        self.assertEqual(self.ask(self.admin).status_code, 200)

    def test_a_channel_outside_the_logins_profiles_is_not_there_either(self):
        from apps.channels.models import ChannelProfile

        from apps.channels.models import ChannelProfileMembership

        profile = ChannelProfile.objects.create(name="Kids")
        # A new profile takes in the channels there are; this one is switched off in it
        ChannelProfileMembership.objects.update_or_create(
            channel_profile=profile, channel=self.channel, defaults={"enabled": False}
        )
        self.alice.channel_profiles.add(profile)
        self.assertEqual(self.ask().status_code, 404)
        ChannelProfileMembership.objects.filter(channel_profile=profile).update(enabled=True)
        self.assertEqual(self.ask().status_code, 200)

    # -- choosing -------------------------------------------------------------------------

    def test_choosing_puts_the_channel_on_the_guide_and_says_what_is_on(self):
        from apps.channels.models import ChannelStream

        answer = self.choose(self.right)
        self.assertEqual(answer.status_code, 200)
        body = answer.json()
        self.assertEqual(body["guide"]["epg_id"], self.right.id)
        self.assertEqual(body["guide"]["now"]["title"], "Zeit im Bild")
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.right.id)
        # Its streams untouched, the fallback still last
        order = list(ChannelStream.objects.filter(channel=self.channel).order_by("order").values_list("stream_id", flat=True))
        self.assertEqual(order[-1], self.fallback.id)
        self.assertEqual(len(order), 2)

    def test_the_apps_are_told_at_once_that_the_guide_changed(self):
        with mock.patch("core.utils.send_websocket_update") as sent:
            self.assertEqual(self.choose(self.right).status_code, 200)
        sent.assert_called_once()
        group, kind, data = sent.call_args[0]
        self.assertEqual((group, kind, data["type"], data["guide"]), ("updates", "update", "channels_changed", True))
        self.assertEqual(data["channels"], [str(self.channel.uuid)])

    def test_with_every_arrtv_switch_off_nothing_is_sent(self):
        from apps.channels import guide_manager as gm

        app_devices.save_settings({"guide_choice": False, "devices": False})
        with mock.patch("core.utils.send_websocket_update") as sent:
            gm.apply({str(self.channel.id): self.right.id})
        sent.assert_not_called()

    def test_it_is_saved_the_way_dispatcharrs_signal_watches(self):
        from apps.channels.models import Channel

        with mock.patch.object(Channel, "save", autospec=True, side_effect=Channel.save) as saving:
            self.choose(self.right)
        self.assertIn("epg_data", saving.call_args.kwargs.get("update_fields") or [])

    def test_the_choice_is_written_down_with_who_made_it(self):
        self.choose(self.right, REMOTE_ADDR="192.168.2.40")
        entry = guide_manager.load_chosen()[str(self.channel.id)]
        self.assertEqual(entry["epg"], self.right.id)
        self.assertEqual(entry["was"], self.wrong.id)
        by = entry["by"]
        self.assertEqual(by["via"], "arrTV")
        self.assertEqual((by["user_id"], by["username"]), (self.alice.id, "alice"))
        self.assertEqual((by["device"], by["device_name"]), (DEVICE, "Living room SHIELD"))
        self.assertEqual(by["ip"], "192.168.2.40")

    def test_without_a_device_it_still_works_and_says_so(self):
        self.assertEqual(self.choose(self.right, device=False).status_code, 200)
        by = guide_manager.load_chosen()[str(self.channel.id)]["by"]
        self.assertIsNone(by["device"])
        self.assertIsNone(by["device_name"])

    def test_a_guide_with_nothing_on_is_refused_and_nothing_changes(self):
        answer = self.choose(self.empty)
        self.assertEqual(answer.status_code, 409)
        self.assertIn("nothing on", answer.json()["error"])
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.wrong.id)

    def test_a_dummy_guide_is_refused(self):
        self.assertEqual(self.choose(self.made_up).status_code, 404)

    def test_the_guide_it_is_on_counts_as_this_one_is_right(self):
        self.assertEqual(self.choose(self.wrong).status_code, 200)
        self.assertTrue(guide_manager.settled(self.channel.id, self.wrong.id))

    def test_the_guides_tab_suggests_nothing_for_it_afterwards(self):
        self.choose(self.right)
        self.assertTrue(guide_manager.settled(self.channel.id, self.right.id))

    # -- the admin's view -----------------------------------------------------------------

    def test_the_changes_are_listed_and_one_can_be_put_back(self):
        self.choose(self.right)
        admin = self.api(self.admin)
        changes = admin.get("/api/core/arrtv/guide-changes/").json()["changes"]
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual((change["channel_name"], change["guide_name"], change["was_name"]),
                         ("┃AT┃ ORF 1", "ORF 1 HD", "ORF1 wrong"))
        self.assertEqual(change["by"]["username"], "alice")

        gone = admin.delete(f"/api/core/arrtv/guide-changes/?channel={self.channel.id}")
        self.assertEqual(gone.status_code, 200)
        self.assertEqual(gone.json()["changes"], [])
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.wrong.id)
        self.assertNotIn(str(self.channel.id), guide_manager.load_chosen())

    def test_confirming_a_change_keeps_what_it_can_be_put_back_to(self):
        """
        Alice moves the channel off the wrong guide, Bob says "this one is right". That used
        to write the guide over what it replaced, so Put back left it where it was.
        """
        self.choose(self.right)
        bob = User.objects.create_user(username="bob", password="x", user_level=1)
        self.assertEqual(self.choose(self.right, user=bob).status_code, 200)
        entry = guide_manager.load_chosen()[str(self.channel.id)]
        self.assertEqual((entry["was"], entry["by"]["username"]), (self.wrong.id, "alice"))
        self.assertEqual([c["username"] for c in entry["confirmed"]], ["bob"])
        self.assertEqual(app_guides.changes()[0]["confirmed"][0]["username"], "bob")

        self.api(self.admin).delete(f"/api/core/arrtv/guide-changes/?channel={self.channel.id}")
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.wrong.id)

    def test_two_changes_are_put_back_one_at_a_time(self):
        third = EPGData.objects.create(tvg_id="ORF1.third", name="ORF 1 third", epg_source=self.de)
        self.airing(third, "Zeit im Bild 2")
        self.choose(self.right)
        self.choose(third)
        change = app_guides.changes()[0]
        self.assertEqual((change["was"], change["earlier"]), (self.right.id, 1))

        self.api(self.admin).delete(f"/api/core/arrtv/guide-changes/?channel={self.channel.id}")
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.right.id)
        # The change before it is on the list again, and goes back in its turn
        change = app_guides.changes()[0]
        self.assertEqual((change["epg"], change["was"], change["earlier"]), (self.right.id, self.wrong.id, 0))
        self.api(self.admin).delete(f"/api/core/arrtv/guide-changes/?channel={self.channel.id}")
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.wrong.id)
        self.assertEqual(app_guides.changes(), [])

    def test_a_change_made_elsewhere_since_is_not_undone(self):
        from apps.channels.models import Channel

        self.choose(self.right)
        Channel.objects.filter(id=self.channel.id).update(epg_data=self.empty)
        self.assertFalse(app_guides.changes()[0]["in_force"])
        refused = self.api(self.admin).delete(f"/api/core/arrtv/guide-changes/?channel={self.channel.id}")
        self.assertEqual(refused.status_code, 409)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.empty.id)

    def test_keeping_a_change_takes_it_off_the_list_and_leaves_the_guide(self):
        self.choose(self.right)
        kept = self.api(self.admin).post(
            "/api/core/arrtv/guide-changes/", {"channel": self.channel.id, "action": "keep"}, format="json"
        )
        self.assertEqual((kept.status_code, kept.json()["changes"]), (200, []))
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.epg_data_id, self.right.id)
        self.assertTrue(guide_manager.settled(self.channel.id, self.right.id))
        self.assertEqual(self.api(self.alice).post(
            "/api/core/arrtv/guide-changes/", {"channel": self.channel.id, "action": "keep"}, format="json"
        ).status_code, 403)

    def test_a_guides_tab_choice_is_not_an_arrtv_change(self):
        guide_manager.apply({self.channel.id: self.right.id})
        self.assertEqual(app_guides.changes(), [])
        self.assertEqual(self.api(self.admin).delete(
            f"/api/core/arrtv/guide-changes/?channel={self.channel.id}").status_code, 404)

    def test_only_an_admin_sees_the_changes(self):
        self.assertEqual(self.api().get("/api/core/arrtv/guide-changes/").status_code, 403)


class KeepingTests(TestCase):
    """What arrTV reads is kept through the refresh's clean-up, and only while switched on."""

    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        no_background_jobs(self)
        patcher = mock.patch("apps.channels.tasks.preload_guide_choices.delay")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("core.utils.RedisClient.get_client", return_value=FakeRedis())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = EPGSource.objects.create(name="xmltvfr.fr", source_type="xmltv")
        self.kept = EPGData.objects.create(tvg_id="TF1.fr", name="TF1", epg_source=self.source)
        self.other = EPGData.objects.create(tvg_id="M6.fr", name="M6", epg_source=self.source)
        moment = timezone.now()
        for guide in (self.kept, self.other):
            ProgramData.objects.create(
                epg=guide, title="Le journal",
                start_time=moment - timedelta(minutes=10), end_time=moment + timedelta(minutes=20),
            )

    def _refresh_cleans_up(self):
        from apps.epg.tasks import _delete_orphaned_epg_programs

        _delete_orphaned_epg_programs(self.source)
        return set(ProgramData.objects.values_list("epg__name", flat=True))

    def test_off_the_clean_up_is_stock(self):
        app_guides.note_read({self.kept.id: 5})
        self.assertEqual(self._refresh_cleans_up(), set())

    def test_on_what_arrtv_read_is_kept(self):
        app_devices.save_settings({"guide_choice": True})
        app_guides.note_read({self.kept.id: 5})
        self.assertEqual(self._refresh_cleans_up(), {"TF1"})

    def test_a_read_that_found_nothing_keeps_nothing(self):
        app_devices.save_settings({"guide_choice": True})
        app_guides.note_read({self.kept.id: 0})
        self.assertEqual(self._refresh_cleans_up(), set())

    def test_the_reading_task_writes_to_arrtvs_record_not_the_guides_tabs(self):
        import os
        import tempfile

        from apps.channels import channel_manager
        from apps.channels.tasks import read_guide_programmes

        handle = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False)
        handle.write(
            '<?xml version="1.0"?><tv><programme channel="TF1.fr" start="20260920080000 +0000" '
            'stop="20260920090000 +0000"><title>Le journal</title></programme></tv>'
        )
        handle.close()
        self.addCleanup(os.remove, handle.name)
        self.source.file_path = handle.name
        self.source.save()
        with mock.patch.object(channel_manager, "say_reading") as saying:
            read_guide_programmes({str(self.source.id): [self.kept.id]}, record="app-guide", how="asked")
        saying.assert_not_called()
        self.assertEqual(channel_manager.reads(), {}, "the Guides tab's record is untouched")
        self.assertEqual(app_guides._record()["ids"][str(self.kept.id)]["found"], 1)
        self.assertEqual(app_guides._record()["ids"][str(self.kept.id)]["how"], "asked")


class PreloadTests(TestCase):
    def setUp(self):
        from apps.channels.models import Channel

        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        no_background_jobs(self)
        self.queued = mock.patch("apps.channels.tasks.preload_guide_choices.delay").start()
        self.reads = mock.patch("apps.channels.tasks.read_guide_programmes.delay").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch("core.utils.RedisClient.get_client", return_value=FakeRedis()).start()
        self.source = EPGSource.objects.create(name="EPGShare DE", source_type="xmltv")
        self.guides = [
            EPGData.objects.create(tvg_id=f"g{i}", name=f"G {i}", epg_source=self.source) for i in range(4)
        ]
        self.used = Channel.objects.create(name="One", channel_number=1, epg_data=self.guides[0])
        Channel.objects.create(name="Two", channel_number=2)
        mock.patch(
            "apps.channels.channel_manager.guide_candidates",
            side_effect=lambda *a, **k: [{"id": g.id, "score": 90 - i} for i, g in enumerate(self.guides)],
        ).start()
        app_devices.save_settings({"guide_choice": True})
        # Switching it on started one; each test starts from none running
        app_guides.end_preload()
        self.queued.reset_mock()

    def test_batches_queue_the_next_then_read_what_is_not_in_use_in_one_go(self):
        from apps.channels import tasks

        with mock.patch.object(tasks, "PRELOAD_BATCH_CHANNELS", 1):
            tasks.preload_guide_choices(0)
            self.queued.assert_called_with(1, mock.ANY)
            wanted = self.queued.call_args[0][1]
            self.reads.assert_not_called()
            tasks.preload_guide_choices(1, wanted)
        self.reads.assert_called_once()
        by_source = self.reads.call_args[0][0]
        # The guide channel One is on is Dispatcharr's to read; the other three are read here
        self.assertEqual(sorted(by_source[str(self.source.id)]), sorted(g.id for g in self.guides[1:]))
        self.assertTrue(app_guides._record()["preloaded_at"])

    def test_it_stops_between_batches_when_switched_off(self):
        from apps.channels import tasks

        app_devices.save_settings({"guide_choice": False})
        self.assertEqual(tasks.preload_guide_choices(0), "Switched off")
        self.reads.assert_not_called()

    def test_a_full_preload_forgets_what_it_no_longer_wants_but_not_what_a_viewer_asked_for(self):
        stale = EPGData.objects.create(tvg_id="old", name="Old", epg_source=self.source)
        asked = EPGData.objects.create(tvg_id="asked", name="Asked", epg_source=self.source)
        app_guides.note_read({stale.id: 3}, how="preload")
        app_guides.note_read({asked.id: 3}, how="asked")
        app_guides.finish_preload({g.id for g in self.guides})
        ids = app_guides._record()["ids"]
        self.assertNotIn(str(stale.id), ids)
        self.assertIn(str(asked.id), ids)

    def test_after_a_refresh_only_that_sources_kept_guides_are_read_again(self):
        other = EPGSource.objects.create(name="Elsewhere", source_type="xmltv")
        elsewhere = EPGData.objects.create(tvg_id="e", name="E", epg_source=other)
        app_guides.finish_preload(set())  # a preload just ran
        app_guides.note_read({self.guides[1].id: 3, elsewhere.id: 3})
        self.reads.reset_mock()
        app_guides.after_refresh(self.source.id)
        self.reads.assert_called_once()
        self.assertEqual(list(self.reads.call_args[0][0]), [str(self.source.id)])

    def test_a_day_on_a_refresh_redoes_the_whole_preload(self):
        self.queued.reset_mock()
        app_guides.after_refresh(self.source.id)
        self.queued.assert_called_once()

    def test_only_one_preload_at_a_time(self):
        """
        The daily refresh refreshes every source, and each one's refresh used to start a
        whole preload of its own: several chains at once, each reading every source's file.
        """
        other = EPGSource.objects.create(name="Elsewhere", source_type="xmltv")
        self.queued.reset_mock()
        app_guides.after_refresh(self.source.id)
        app_guides.after_refresh(other.id)
        self.assertFalse(app_guides.start_preload(), "Load now while one runs")
        self.queued.assert_called_once()
        # Once it is done, the next may start
        app_guides.finish_preload(set())
        self.assertTrue(app_guides.start_preload())

    def test_a_preload_that_died_part_way_is_not_loading_for_a_day(self):
        app_guides.start_preload()
        self.assertEqual(app_guides.preload_state()["state"], "working")
        # The services restarted: no batch holds it any more, and it runs out
        app_guides._redis().delete(app_guides.PRELOAD_LOCK_KEY)
        self.assertEqual(app_guides.preload_state()["state"], "stopped")
        self.assertTrue(app_guides.start_preload())

    def test_switched_off_part_way_it_lets_go(self):
        from apps.channels import tasks

        app_guides.start_preload()
        app_devices.save_settings({"guide_choice": False})
        tasks.preload_guide_choices(0)
        self.assertFalse(app_guides.preload_running())
        self.assertEqual(app_guides.preload_state()["state"], "stopped")

    def test_a_guide_a_viewer_asked_for_long_ago_is_let_go(self):
        """
        Every list ever opened used to add guides that were kept, and read again at every
        refresh, for good. Re-reading after a refresh does not count as being asked for.
        """
        from datetime import timedelta

        from django.utils import timezone

        from apps.channels.settings_rows import change_row

        lately = EPGData.objects.create(tvg_id="lately", name="Lately", epg_source=self.source)
        long_ago = EPGData.objects.create(tvg_id="long", name="Long ago", epg_source=self.source)
        app_guides.note_read({lately.id: 3, long_ago.id: 3}, how="asked")
        then = (timezone.now() - timedelta(days=app_guides.ASKED_KEPT_DAYS + 1)).isoformat(timespec="seconds")

        def age(kept):
            kept["ids"][str(long_ago.id)]["asked_at"] = then

        change_row(app_guides.KEPT_KEY, "arrTV guides kept", age)
        # A refresh reads both again: that is not a viewer asking
        app_guides.note_read({lately.id: 3, long_ago.id: 3}, how="preload")
        self.assertEqual(app_guides._record()["ids"][str(long_ago.id)]["asked_at"], then)
        app_guides.finish_preload(set())
        ids = app_guides._record()["ids"]
        self.assertIn(str(lately.id), ids)
        self.assertNotIn(str(long_ago.id), ids)


class WideMatchingTests(TestCase):
    """What "wide" means to the Guides tab's matcher: none of its settings' limits."""

    def test_wide_drops_the_sources_pattern_and_country(self):
        from apps.channels import channel_manager

        settings = {**channel_manager.MATCHING_DEFAULTS, "sources": [1], "tvg_id_like": ".de",
                    "country_must_agree": True}
        with mock.patch("apps.channels.channel_manager.load_matching", return_value=settings):
            _, narrow = channel_manager._guides_in_reach()
            _, wide = channel_manager._guides_in_reach(wide=True)
        self.assertEqual(narrow["sources"], [1])
        self.assertEqual((wide["sources"], wide["tvg_id_like"], wide["country_must_agree"]), ([], "", False))
