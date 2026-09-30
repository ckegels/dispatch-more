"""Show Groups against Dispatcharr's own models: the groups and their settings, the minute's
pass, channels always in a group, taking the plugin's group over, and the tab's API."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone as tz
from unittest import mock

from django.test import TestCase

from apps.channels.models import (
    Channel, ChannelGroup, ChannelProfile, ChannelProfileMembership, ChannelStream, Stream,
)
from apps.channels.show_groups import live, store, themes
from apps.channels.show_groups import plan as plans
from apps.epg.models import EPGData, EPGSource, ProgramData

NOW = datetime(2026, 9, 22, 18, 0, tzinfo=tz.utc)


class FakeRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        return 1 if self.data.pop(key, None) is not None else 0


class Base(TestCase):
    """Two source channels with a guide each, a fake Redis, no viewers."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.dict(os.environ, {"SHOW_GROUPS_DIR": folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.redis = FakeRedis()
        self.watching = {}
        for patch in (
            mock.patch.object(live, "_redis", lambda: self.redis),
            mock.patch.object(live, "viewers", lambda uuid: self.watching.get(str(uuid), 0)),
            mock.patch("apps.output.streaming_chunk_cache.invalidate_epg_chunk_cache", lambda: None),
            mock.patch.object(live, "announce", lambda *a: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        self.source = EPGSource.objects.create(name="Guide", source_type="xmltv")
        self.public = ChannelGroup.objects.create(name="Public")
        self.pbs_guide = EPGData.objects.create(tvg_id="pbs", name="pbs", epg_source=self.source)
        self.tlc_guide = EPGData.objects.create(tvg_id="tlc", name="tlc", epg_source=self.source)
        self.pbs = Channel.objects.create(name="PBS 13", channel_number=513,
                                          epg_data=self.pbs_guide, channel_group=self.public)
        self.tlc = Channel.objects.create(name="TLC", channel_number=40,
                                          epg_data=self.tlc_guide, channel_group=self.public)
        self.stream = Stream.objects.create(name="PBS 13 HD", url="http://example.invalid/1")
        ChannelStream.objects.create(channel=self.pbs, stream=self.stream, order=0)

    def airs(self, guide, title, start_minutes, length, categories):
        start = NOW + timedelta(minutes=start_minutes)
        ProgramData.objects.create(epg=guide, title=title, start_time=start,
                                   end_time=start + timedelta(minutes=length),
                                   custom_properties={"categories": list(categories)})

    def switch_on(self, *group_ids, **changes):
        groups = themes.load_groups()
        for group in groups:
            if group["id"] in group_ids:
                group["on"] = True
                group.update(changes.get(group["id"], {}))
        themes.save({"live": True}, groups)

    def run_at(self, minutes):
        settings, groups = themes.load_settings(), themes.load_groups()
        own = live.own_copy_ids(live.load_state())
        plan = plans.compute(settings, groups, NOW, exclude_ids=own)
        return live.tick(settings, groups, plan, now=NOW + timedelta(minutes=minutes))

    def members(self, name):
        return sorted(ChannelProfileMembership.objects.filter(
            channel_profile__name="Show Groups", channel__channel_group__name=name, enabled=True)
            .values_list("channel__name", flat=True))


class Settings(Base):
    def test_everything_starts_off(self):
        self.assertFalse(themes.load_settings()["live"])
        self.assertFalse(any(g["on"] for g in themes.load_groups()))
        self.assertEqual([g["id"] for g in themes.load_groups()][:3], ["cooking", "travel", "movies"])

    def test_a_ready_made_group_as_it_comes_is_not_stored(self):
        groups = themes.load_groups()
        groups[1]["on"] = True
        themes.save(None, groups)
        from core.models import CoreSettings

        stored = CoreSettings.objects.get(key=themes.SETTINGS_KEY).value["groups"]
        self.assertEqual([g["id"] for g in stored], ["travel"])

    def test_a_group_of_your_own_is_added_and_kept(self):
        groups = themes.load_groups()
        groups.append({"id": themes.new_id("Formula 1", {g["id"] for g in groups}),
                       "name": "Formula 1", "category_words": "formula 1, f1", "on": True})
        _, groups = themes.save(None, groups)
        mine = groups[-1]
        self.assertEqual((mine["id"], mine["name"], mine["preset"]), ("my-formula-1", "Formula 1", False))

    def test_two_groups_cannot_share_a_name(self):
        groups = themes.load_groups()
        groups.append({"id": "my-cooking", "name": "cooking"})
        with self.assertRaises(ValueError):
            themes.save(None, groups)


class Live(Base):
    def test_off_touches_nothing(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", 10, 30, ["Travel"])
        self.assertEqual(self.run_at(0), ([], [], []))
        self.assertFalse(ChannelProfile.objects.filter(name="Show Groups").exists())

    def test_each_group_holds_what_is_on(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", 10, 30, ["Travel"])
        self.airs(self.tlc_guide, "Cake Boss", 10, 30, ["Cooking"])
        self.switch_on("travel", "cooking")
        joined, _, _ = self.run_at(0)
        self.assertEqual(sorted(joined), ["PBS 13", "TLC"])
        self.assertEqual(self.members("Travel"), ["PBS 13"])
        self.assertEqual(self.members("Cooking"), ["TLC"])
        copy = Channel.objects.get(channel_group__name="Travel")
        self.assertGreaterEqual(copy.channel_number, 20000)
        self.assertFalse(copy.hidden_from_output)
        self.assertEqual(list(copy.streams.values_list("id", flat=True)), [self.stream.id])

    def test_a_copy_leaves_when_its_show_is_over_but_not_under_a_viewer(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", 10, 30, ["Travel"])
        self.switch_on("travel")
        self.run_at(0)
        copy = Channel.objects.get(channel_group__name="Travel")
        self.watching[str(copy.uuid)] = 1
        self.assertEqual(self.run_at(120)[2], ["PBS 13"], "held for its viewer")
        self.watching.clear()
        self.assertEqual(self.run_at(125)[2], ["PBS 13"], "and for the grace after")
        self.assertEqual(self.run_at(140)[1], ["PBS 13"])
        copy.refresh_from_db()
        self.assertTrue(copy.hidden_from_output)

    def test_a_channel_always_in_the_group(self):
        self.switch_on("cooking", cooking={"permanent": [self.tlc.id]})
        self.run_at(0)
        self.assertEqual(self.members("Cooking"), ["TLC"])
        self.assertEqual(self.run_at(600), ([], [], []), "whatever is on, it stays")
        self.assertIn("always in the group", "\n".join(live.activity()))

    def test_a_group_switched_off_is_cleared_away(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", 10, 30, ["Travel"])
        self.switch_on("travel")
        self.run_at(0)
        groups = themes.load_groups()
        groups[1]["on"] = False
        themes.save(None, groups)
        self.run_at(1)
        self.assertFalse(ChannelGroup.objects.filter(name="Travel").exists())
        self.assertFalse(Channel.objects.filter(name="PBS 13").exclude(id=self.pbs.id).exists())

    def test_live_off_keeps_the_copies_and_their_numbers(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", 10, 30, ["Travel"])
        self.switch_on("travel")
        self.run_at(0)
        themes.save({"live": False})
        self.run_at(1)
        self.assertEqual(self.members("Travel"), [])
        self.assertTrue(Channel.objects.filter(channel_group__name="Travel").exists())

    def test_a_name_that_is_already_your_group_is_refused_the_others_go_on(self):
        ChannelGroup.objects.create(name="Movies")
        self.airs(self.pbs_guide, "Casablanca", 10, 100, ["Movie"])
        self.airs(self.tlc_guide, "Cake Boss", 10, 30, ["Cooking"])
        self.switch_on("movies", "cooking")
        joined, _, _ = self.run_at(0)
        self.assertEqual(joined, ["TLC"])
        self.assertIn("already have a channel group", live.load_state()["refused"]["movies"])

    def test_remove_everything(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", 10, 30, ["Travel"])
        self.switch_on("travel")
        self.run_at(0)
        with self.assertRaises(live.Refused):
            live.remove_all(themes.load_settings())
        themes.save({"live": False})
        deleted, kept, removed = live.remove_all(themes.load_settings())
        self.assertEqual((deleted, kept), (["PBS 13"], []))
        self.assertFalse(ChannelGroup.objects.filter(name="Travel").exists())
        self.assertFalse(ChannelProfile.objects.filter(name="Show Groups").exists())


class TakeOver(Base):
    """The plugin's Cooking group becomes Show Groups' Cooking group, copies and all."""

    def setUp(self):
        super().setUp()
        from apps.plugins.models import PluginConfig

        self.config = PluginConfig.objects.create(
            key="show_groups", name="Show Groups", enabled=True,
            settings={"live": True, "group_name": "Cooking", "always": "Cake Boss",
                      "category_words": "cooking, food", "linger": 20})
        group = ChannelGroup.objects.create(name="Cooking")
        profile = ChannelProfile(name="Show Groups")
        profile._start_empty = True
        profile.save()
        self.copy = Channel.objects.create(name="TLC", channel_number=20000, channel_group=group)
        ChannelProfileMembership.objects.create(channel_profile=profile, channel=self.copy, enabled=True)
        store.write_text("live.json", json.dumps({
            "group_id": group.id, "profile_id": profile.id,
            "copies": {str(self.tlc.id): {"id": self.copy.id, "uuid": str(self.copy.uuid)}},
            "watched": {}}))

    def test_live_is_refused_while_the_plugin_runs(self):
        self.switch_on("cooking")
        with self.assertRaises(live.Refused):
            self.run_at(0)

    def test_a_record_without_the_profile_still_shows_who_is_in(self):
        record = json.loads(open(store.path_of("live.json")).read())
        del record["profile_id"]
        store.write_text("live.json", json.dumps(record))
        live.take_over()
        self.assertEqual(live.load_state()["profile_id"], ChannelProfile.objects.get(name="Show Groups").id)
        from apps.channels.show_groups_views import _page

        cooking = next(g for g in _page()["groups"] if g["id"] == "cooking")
        self.assertEqual([m["name"] for m in cooking["in_group"]], ["TLC"], "before the first minute too")

    def test_taken_over(self):
        taken = live.take_over()
        self.assertEqual(taken, {"group": "cooking", "copies": 1})
        self.config.refresh_from_db()
        self.assertFalse(self.config.enabled)
        cooking = next(g for g in themes.load_groups() if g["id"] == "cooking")
        self.assertTrue(cooking["on"])
        self.assertEqual((cooking["always"], cooking["category_words"]), ("Cake Boss", "cooking, food"))
        self.assertEqual(themes.load_settings()["linger"], 20)
        self.assertFalse(os.path.exists(store.path_of("live.json")))
        # The next minute works with the same copy: no new one, and it leaves as nothing is on
        self.run_at(0)
        self.assertEqual(list(Channel.objects.filter(channel_group__name="Cooking")), [self.copy])
        self.assertEqual(self.members("Cooking"), [])


class Api(Base):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        admin = get_user_model().objects.create_user(username="admin", password="x", user_level=10)
        self.client = APIClient()
        self.client.force_authenticate(admin)

    def test_the_page_and_saving(self):
        page = self.client.get("/api/channels/show-groups/").json()
        self.assertEqual(len(page["groups"]), len(themes.PRESETS))
        self.assertIsNone(page["plugin"])
        groups = page["groups"]
        groups[0]["on"] = True
        saved = self.client.put("/api/channels/show-groups/", {"groups": groups, "settings": {"live": True}},
                                format="json").json()
        self.assertTrue(saved["settings"]["live"])
        self.assertTrue(saved["groups"][0]["on"])

    def test_update_now_and_what_is_in_the_group(self):
        self.airs(self.pbs_guide, "Rick Steves' Europe", -10, 30, ["Travel"])
        self.switch_on("travel")
        with mock.patch("apps.channels.show_groups_views.timezone.now", return_value=NOW), \
                mock.patch("apps.channels.show_groups.live.timezone.now", return_value=NOW), \
                mock.patch("apps.channels.show_groups.plan.timezone.now", return_value=NOW):
            page = self.client.post("/api/channels/show-groups/run/", {"action": "update"},
                                    format="json").json()
        travel = next(g for g in page["groups"] if g["id"] == "travel")
        self.assertEqual([m["name"] for m in travel["in_group"]], ["PBS 13"])
        self.assertEqual(travel["in_group"][0]["showing"]["title"], "Rick Steves' Europe")
        self.assertEqual(travel["titles"][0]["title"], "Rick Steves' Europe")

    def test_a_refusal_is_said(self):
        answer = self.client.post("/api/channels/show-groups/run/", {"action": "take_over"}, format="json")
        self.assertEqual(answer.status_code, 409)


IPTV_ORG = [
    {"id": "24Kitchen.us", "name": "24Kitchen", "country": "US", "categories": ["cooking"]},
    {"id": "FoodNetwork.uk", "name": "Food Network", "country": "UK", "categories": ["cooking"]},
    {"id": "FoodNetwork.ca", "name": "Food Network", "country": "CA", "categories": ["cooking"]},
    {"id": "Travelxp.in", "name": "Travelxp", "country": "IN", "categories": ["travel"]},
    {"id": "BonGusto.de", "name": "BonGusto", "country": "DE", "categories": []},
    {"id": "BBCFood.uk", "name": "BBC Food", "country": "UK", "categories": ["cooking"],
     "closed": "2008-12-26"},
    {"id": "NatGeo.nl", "name": "National Geographic", "alt_names": ["Nat Geo"], "country": "NL",
     "categories": ["documentary"]},
]


class WholeChannels(Api):
    """Channels iptv-org files under a group's kind, offered to keep in the group."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        from apps.channels.show_groups import kinds

        cache.delete(kinds.INDEX_KEY)
        self.addCleanup(cache.delete, kinds.INDEX_KEY)
        patcher = mock.patch("apps.channels.logo_library._get_json", return_value=IPTV_ORG)
        self.download = patcher.start()
        self.addCleanup(patcher.stop)
        for number, name in ((1, "┃BE┃ 24KITCHEN"), (2, "┃UK┃ FOOD NETWORK HD"), (3, "┃DE┃ BON GUSTO"),
                             (4, "┃UK┃ BBC FOOD"), (5, "┃NL┃ NAT GEO"), (6, "┃UK┃ BBC ONE")):
            Channel.objects.create(name=name, channel_number=number, channel_group=self.public)

    def kinds(self, group):
        return self.client.get(f"/api/channels/show-groups/kinds/?group={group}").json()

    def test_cooking_channels_in_their_own_country_first_else_where_listed(self):
        found = self.kinds("cooking")
        self.assertEqual([c["name"] for c in found["channels"]], ["┃BE┃ 24KITCHEN", "┃UK┃ FOOD NETWORK HD"])
        self.assertEqual(found["channels"][0]["listed_as"], "24Kitchen (US)", "only listed in the US")
        self.assertEqual(found["channels"][1]["listed_as"], "Food Network (GB)", "its own country's")
        self.assertEqual(found["kinds"], ["cooking"])

    def test_by_another_name_and_by_tvg_id(self):
        self.assertEqual([c["name"] for c in self.kinds("documentaries")["channels"]], ["┃NL┃ NAT GEO"])
        Channel.objects.filter(name="┃UK┃ BBC ONE").update(tvg_id="Travelxp.in")
        self.assertEqual([c["name"] for c in self.kinds("travel")["channels"]], ["┃UK┃ BBC ONE"])

    def test_downloaded_once(self):
        self.kinds("cooking")
        self.kinds("travel")
        self.assertEqual(self.download.call_count, 1)

    def test_kept_in_the_group_and_said_so(self):
        groups = themes.load_groups()
        kitchen = Channel.objects.get(name="┃BE┃ 24KITCHEN")
        groups[0]["permanent"] = [kitchen.id]
        themes.save(None, groups)
        found = self.kinds("cooking")["channels"]
        self.assertEqual([c["always"] for c in found], [True, False])

    def test_a_group_of_your_own_starts_with_no_kinds(self):
        groups = themes.load_groups() + [{"id": "my-f1", "name": "F1", "category_words": "f1"}]
        themes.save(None, groups)
        self.assertEqual(self.kinds("my-f1")["channels"], [])
        groups[-1]["channel_kinds"] = ["cooking"]
        themes.save(None, groups)
        self.assertEqual(len(self.kinds("my-f1")["channels"]), 2)

    def test_abroad_only_by_its_own_name_and_not_when_home_has_one(self):
        from django.core.cache import cache

        from apps.channels.show_groups import kinds

        more = IPTV_ORG + [
            {"id": "CanThoTV2.vn", "name": "Can Tho TV 2", "alt_names": ["HGTV"], "country": "VN",
             "categories": ["science"]},
            {"id": "TV8.it", "name": "TV8", "country": "IT", "categories": []},
            {"id": "TV8.md", "name": "TV8", "country": "MD", "categories": ["science"]},
        ]
        cache.delete(kinds.INDEX_KEY)
        self.download.return_value = more
        Channel.objects.create(name="┃UK┃ HGTV", channel_number=7, channel_group=self.public)
        Channel.objects.create(name="┃IT┃ TV 8", channel_number=8, channel_group=self.public)
        groups = themes.load_groups()
        next(g for g in groups if g["id"] == "science")["on"] = True
        themes.save(None, groups)
        self.assertEqual(self.kinds("science")["channels"], [])


class Comparison(Api):
    """The tab's Shows: every show in the guide next to what the databases say."""

    def setUp(self):
        super().setUp()
        self.airs(self.pbs_guide, "Cake Boss", 10, 30, ["Entertainment"])
        self.airs(self.tlc_guide, "Chopped", 10, 30, ["Cooking"])
        self.airs(self.tlc_guide, "Mystery Show", 40, 30, [])
        store.merge_lookups({"cake boss": {"tvmaze": {"name": "Cake Boss", "genres": ["Reality", "Food"],
                                                      "asked": "2026-09-22T17:00:00+00:00"}},
                             "chopped": {"tvmaze": {"name": "Chopped", "genres": ["Drama"],
                                                    "asked": "2026-09-22T17:00:00+00:00"}}})
        self.switch_on("cooking")
        live.work_out(themes.load_settings(), themes.load_groups(), NOW)

    def shows(self, **query):
        from urllib.parse import urlencode

        return self.client.get("/api/channels/show-groups/shows/?" + urlencode(query)).json()

    def test_every_show_with_what_each_says(self):
        found = self.shows()
        self.assertEqual(found["count"], 3)
        boss = next(s for s in found["shows"] if s["title"] == "Cake Boss")
        self.assertEqual(boss["guide"], ["Entertainment"])
        self.assertTrue(boss["vague"])
        self.assertEqual(boss["sources"]["tvmaze"]["genres"], ["Reality", "Food"])
        self.assertIsNone(boss["sources"]["wikidata"], "not asked yet")
        self.assertEqual([t["group"] for t in boss["takes"]], ["cooking"], "past the vague guide")

    def test_where_they_disagree(self):
        found = self.shows(only="disagree")
        self.assertEqual([s["title"] for s in found["shows"]], ["Cake Boss", "Chopped"])
        chopped = found["shows"][1]
        self.assertEqual(chopped["disagree"], [{"group": "cooking", "guides": True, "databases": False}])

    def test_the_vague_ones_are_queued_for_the_databases(self):
        queued = [written for _, written, _ in plans.load()["unknown"]]
        self.assertIn("Cake Boss", queued)
        self.assertIn("Mystery Show", queued)
        self.assertNotIn("Chopped", queued)
        self.assertEqual([s["title"] for s in self.shows(only="unknown")["shows"]], ["Mystery Show"])

    def test_ask_now(self):
        with mock.patch("apps.channels.show_groups.lookups.ask",
                        side_effect=lambda source, title, settings: {"name": title, "genres": ["Mystery"]}
                        if source == "tvmaze" else None):
            found = self.client.post("/api/channels/show-groups/shows/", {"title": "Mystery Show"},
                                     format="json").json()
        mystery = found["shows"][0]
        self.assertEqual(mystery["sources"]["tvmaze"]["genres"], ["Mystery"])
        self.assertEqual(mystery["sources"]["wikidata"], {"name": "", "genres": []}, "asked, did not know")
        self.assertEqual(self.redis.get(live.REBUILD_KEY), "1", "the plan is made again")


class ServiceKeys(Api):
    """Settings → Service keys: one place for the keys every feature uses."""

    def test_saved_and_read(self):
        page = self.client.get("/api/channels/service-keys/").json()
        self.assertEqual([s["id"] for s in page["services"]], ["tmdb", "tvdb", "trakt", "omdb"])
        self.assertEqual(page["values"]["tvdb_key"], "")
        saved = self.client.put("/api/channels/service-keys/", {"values": {"tvdb_key": " abc ", "nope": 1}},
                                format="json").json()
        self.assertEqual(saved["values"]["tvdb_key"], "abc")
        self.assertNotIn("nope", saved["values"])

    def test_a_tmdb_key_from_show_groups_is_taken_over(self):
        from core.models import CoreSettings

        CoreSettings.objects.update_or_create(key=themes.SETTINGS_KEY, defaults={
            "name": "Show Groups", "value": {"settings": {"tmdb_key": "old"}}})
        from apps.channels import service_keys

        self.assertEqual(service_keys.load()["tmdb_key"], "old")

    def test_the_test_button(self):
        from apps.channels import service_keys
        from apps.channels.show_groups import lookups

        self.assertFalse(self.client.post("/api/channels/service-keys/test/", {"service": "tvdb"},
                                          format="json").json()["ok"])
        service_keys.save({"tvdb_key": "k"})
        with mock.patch.object(lookups, "ask", return_value={"name": "MasterChef", "genres": ["Food"]}):
            answer = self.client.post("/api/channels/service-keys/test/", {"service": "tvdb"}, format="json").json()
        self.assertTrue(answer["ok"])
        with mock.patch.object(lookups, "ask", side_effect=lookups.Unavailable("TheTVDB refused the key")):
            answer = self.client.post("/api/channels/service-keys/test/", {"service": "tvdb"}, format="json").json()
        self.assertEqual(answer, {"ok": False, "message": "TheTVDB refused the key"})

    def test_a_refused_key_records_nothing(self):
        from apps.channels import service_keys
        from apps.channels.show_groups import lookups

        service_keys.save({"tvdb_key": "wrong"})
        plans.save({"unknown": [[3, "Cake Boss", "cake boss"], [2, "Chopped", "chopped"]]})

        def ask(source, title, settings):
            if source == "tvdb":
                raise lookups.Unavailable("refused")
            return {"name": title, "genres": ["Food"]}

        with mock.patch.object(lookups, "ask", side_effect=ask) as asked:
            live.look_up(themes.load_settings(), budget=30, now=NOW)
        answers = store.load_lookups()
        self.assertNotIn("tvdb", answers["cake boss"], "not recorded as unknown")
        self.assertEqual(answers["chopped"]["tvmaze"]["genres"], ["Food"])
        self.assertEqual([c.args[0] for c in asked.call_args_list].count("tvdb"), 1, "not asked again this pass")

    def test_the_old_does_not_know_answers_are_asked_again_once(self):
        from apps.channels.show_groups import lookups

        store.merge_lookups({"cake boss": {"tvmaze": {"asked": "2026-09-20T00:00:00+00:00"},
                                           "wikidata": {"name": "Cake Boss", "genres": ["Food"],
                                                        "asked": "2026-09-20T00:00:00+00:00"}}})
        plans.save({"unknown": [[3, "Cake Boss", "cake boss"]]})
        with mock.patch.object(lookups, "ask", return_value={"name": "Cake Boss", "genres": ["Reality"]}):
            live.look_up(themes.load_settings(), budget=30, now=NOW)
        answers = store.load_lookups()["cake boss"]
        self.assertEqual(answers["tvmaze"]["genres"], ["Reality"], "asked again")
        self.assertEqual(answers["wikidata"]["genres"], ["Food"], "a real answer is kept")
        store.merge_lookups({"chopped": {"tvmaze": {"asked": NOW.isoformat()}}})
        self.assertEqual(store.forget_empty_answers_once(), 0, "only once")


class Queue(Base):
    def test_fillers_and_a_channels_own_name_are_not_asked_about(self):
        omroep = Channel.objects.create(name="┃NL┃ OMROEP TILBURG HD", channel_number=9,
                                        epg_data=EPGData.objects.create(tvg_id="ot", name="ot", epg_source=self.source),
                                        channel_group=self.public)
        self.airs(omroep.epg_data, "Omroep Tilburg", 0, 60, [])
        self.airs(self.pbs_guide, "Paid Programming", 0, 30, [])
        self.airs(self.pbs_guide, "Mystery Show", 40, 30, [])
        self.switch_on("cooking")
        made = plans.compute(themes.load_settings(), themes.load_groups(), NOW)
        self.assertEqual([written for _, written, _ in made["unknown"]], ["Mystery Show"])
