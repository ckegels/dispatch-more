"""Recognising the same channel across providers and qualities (apps.channels.channel_manager).

The names are shaped like real ones: a country box in front, a quality at the end, the same
channel written three ways by three providers. The setup is shaped like a real one too:
channels in a country group, each with a custom fallback stream on the end that shows a
"could not play this" screen, which is the thing most easily broken by getting this wrong.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import channel_manager
from apps.channels.models import (
    Channel,
    ChannelGroup,
    ChannelProfile,
    ChannelProfileMembership,
    ChannelStream,
    Stream,
)
from apps.epg.models import EPGData
from apps.m3u.models import M3UAccount


def settings(**overrides):
    return {**channel_manager.DEFAULTS, **overrides}


class RecognitionTests(TestCase):
    def same(self, a, b, **overrides):
        s = settings(**overrides)
        return channel_manager.channel_key(a, s) == channel_manager.channel_key(b, s)

    def test_the_quality_and_the_stream_words_come_off(self):
        for name in ("┃AT┃ ORF 1 HD", "┃AT┃ ORF 1 FHD", "┃AT┃ ORF 1 4K HEVC", "┃AT┃ ORF 1 VIP",
                     "┃AT┃ ORF 1 (1080p)", "┃at┃ orf 1"):
            self.assertTrue(self.same(name, "┃AT┃ ORF 1"), name)

    def test_by_default_the_rest_of_the_name_counts_as_written(self):
        """
        As DispatcharrUtils and the group merge people ran by hand: the country box, the
        spacing and the punctuation are part of the name. Loosening that merged channels
        those tools kept apart, and each merge of two channels was a conflict.
        """
        self.assertFalse(self.same("┃AT┃ ORF 1", "┃DE┃ ORF 1"))
        self.assertFalse(self.same("┃AT┃ ORF 1", "┃AT┃ ORF-1"))
        self.assertFalse(self.same("┃BE┃ Eén", "┃BE┃ Een"))

    def test_loose_matching_is_there_to_be_asked_for(self):
        self.assertTrue(self.same("┃AT┃ ORF 1", "┃AT┃ ORF-1", name_matching="loose"))
        self.assertTrue(self.same("┃BE┃ Eén", "┃BE┃ Een", name_matching="loose"))

    def test_quality_is_a_whole_word_not_part_of_one(self):
        """"HD" in the middle of a word is the word: SHD Sport is not SH plus a quality."""
        self.assertFalse(self.same("SHD Sport", "S Sport"))

    def test_a_number_on_its_own_is_part_of_the_name(self):
        """Counted as a quality, "Channel 480" and "Channel 720" both became "Channel"."""
        self.assertFalse(self.same("Channel 480", "Channel 720"))
        self.assertTrue(self.same("ORF 1 720p", "ORF 1"))

    def test_rules_and_aliases(self):
        overrides = dict(
            regex_rules=[[r"^AT:\s*", "┃AT┃ "]],
            aliases={"┃BE┃ National Geographic": ["┃BE┃ NGC", "┃BE┃ Nat Geo"]},
        )
        self.assertTrue(self.same("AT: ORF 1", "┃AT┃ ORF 1", **overrides))
        self.assertTrue(self.same("┃BE┃ NGC HD", "┃BE┃ National Geographic", **overrides))

    def test_a_rule_that_does_not_work_is_skipped_not_fatal(self):
        self.assertTrue(self.same("ORF 1", "ORF 1 HD", regex_rules=[["(unclosed", ""]]))

    def test_the_picture_a_probe_measured_outranks_what_the_name_claims(self):
        self.assertEqual(channel_manager.quality_of("ORF 1 4K", {"resolution": "1280x720"})[0], "HD")
        self.assertEqual(channel_manager.quality_of("ORF 1 FHD")[0], "FHD")

    def test_a_stream_that_says_nothing_sits_between_hd_and_sd(self):
        """Usually one or the other; last would bury a good stream."""
        _, unknown, _ = channel_manager.quality_of("ORF 1")
        _, hd, _ = channel_manager.quality_of("ORF 1 HD")
        _, sd, _ = channel_manager.quality_of("ORF 1 SD")
        self.assertTrue(hd < unknown < sd)


class _Setup(TestCase):
    def setUp(self):
        self.a = M3UAccount.objects.create(name="Provider A", account_type="XC", server_url="http://a", is_active=True)
        self.b = M3UAccount.objects.create(name="Provider B", account_type="XC", server_url="http://b", is_active=True)
        self.austria = ChannelGroup.objects.create(name="┃AT┃ AUSTRIA")
        self.germany = ChannelGroup.objects.create(name="┃DE┃ GERMANY")
        self.fallback = Stream.objects.create(
            name="could not dispatch", url="http://local/fallback", is_custom=True
        )
        self.orf1 = self._channel("┃AT┃ ORF 1", 1, self.austria)
        # What it has now: one stream, then the fallback
        self.existing = self._stream("┃AT┃ ORF 1", self.a)
        self._attach(self.orf1, [self.existing, self.fallback])

    def _stream(self, name, account, group=None, **extra):
        return Stream.objects.create(
            name=name, url=f"http://{account.name}/{name}", m3u_account=account,
            channel_group=group or self.austria, **extra,
        )

    def _channel(self, name, number, group, **extra):
        return Channel.objects.create(name=name, channel_number=number, channel_group=group, **extra)

    def _attach(self, channel, streams):
        for order, stream in enumerate(streams):
            ChannelStream.objects.create(channel=channel, stream=stream, order=order)

    def _order(self, channel):
        return list(
            ChannelStream.objects.filter(channel=channel).order_by("order").values_list("stream__name", flat=True)
        )

    def _row(self, plan, key):
        return next(r for r in plan["rows"] if r["key"] == key)


class MergeTests(_Setup):
    def test_every_copy_of_a_channel_is_found_across_providers(self):
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        self._stream("┃AT┃ ORF 1 HD", self.a)
        self._stream("┃AT┃ ORF 2 HD", self.a)  # another channel

        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")

        self.assertEqual(row["status"], "merge")
        self.assertEqual(row["adds"], 2)
        names = [s["name"] for s in row["streams"]]
        self.assertNotIn("┃AT┃ ORF 2 HD", names)

    def test_what_is_added_goes_in_before_the_fallback(self):
        """After it, it would never be tried before the screen saying nothing works."""
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        channel_manager.apply_plan(settings(), [f"ch:{self.orf1.id}"])

        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "┃AT┃ ORF 1 FHD", "could not dispatch"])

    def test_the_fallback_is_never_removed(self):
        """Out of scope, because custom streams are skipped, and so not decided about."""
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        channel_manager.apply_plan(settings(replace_streams=True, reorder_existing=True), [f"ch:{self.orf1.id}"])
        self.assertEqual(self._order(self.orf1)[-1], "could not dispatch")

    def test_the_best_picture_comes_first_when_asked_to_reorder(self):
        self._stream("┃AT┃ ORF 1 SD", self.b)
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        channel_manager.apply_plan(settings(reorder_existing=True), [f"ch:{self.orf1.id}"])

        self.assertEqual(
            self._order(self.orf1),
            ["┃AT┃ ORF 1 FHD", "┃AT┃ ORF 1", "┃AT┃ ORF 1 SD", "could not dispatch"],
        )

    def test_the_preferred_provider_comes_first_when_that_is_the_order(self):
        self._stream("┃AT┃ ORF 1 FHD", self.a)
        self._stream("┃AT┃ ORF 1 HD", self.b)
        row = self._row(
            channel_manager.build_plan(settings(order="provider", accounts=[self.b.id, self.a.id])),
            f"ch:{self.orf1.id}",
        )
        added = [s["name"] for s in row["streams"] if s["added"]]
        self.assertEqual(added[0], "┃AT┃ ORF 1 HD")

    def test_a_stream_from_an_account_not_chosen_is_left_where_it_is(self):
        """It was not part of the question, so nothing is decided about it."""
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        row = self._row(
            channel_manager.build_plan(settings(accounts=[self.b.id], replace_streams=True)),
            f"ch:{self.orf1.id}",
        )
        kept = [s for s in row["streams"] if s["name"] == "┃AT┃ ORF 1"]
        self.assertTrue(kept and not kept[0]["removed"])

    def test_another_countrys_channel_of_the_same_name_is_not_merged(self):
        self._stream("┃DE┃ ORF 1 HD", self.b, group=self.germany)
        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")
        self.assertEqual(row["status"], "unchanged")

    def test_a_stream_that_names_no_country_can_still_be_matched(self):
        """Not saying is not a different country."""
        other = ChannelGroup.objects.create(name="Unsorted")
        self._stream("ORF 1 HD", self.b, group=other)
        row = self._row(channel_manager.build_plan(settings(name_matching="loose")), f"ch:{self.orf1.id}")
        self.assertEqual(row["adds"], 1)

    def test_tvg_id_is_trusted_over_the_name(self):
        self.orf1.tvg_id = "ORF1.at"
        self.orf1.save()
        self._stream("┃AT┃ ORF EINS", self.b, tvg_id="ORF1.at")
        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")
        self.assertEqual(row["adds"], 1)

    def test_several_channels_of_one_name_each_get_it_as_the_group_merge_did(self):
        """
        Dispatcharr's own sync makes a channel per stream, so a setup has plenty of channels
        with one name. The group merge people ran gave each of them every stream of that
        name, and so does this, by default.
        """
        twin = self._channel("┃AT┃ ORF 1 HD", 2, self.austria)
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        plan = channel_manager.build_plan(settings())

        self.assertFalse(any(r["status"] == "conflict" for r in plan["rows"]))
        for channel in (self.orf1, twin):
            row = self._row(plan, f"ch:{channel.id}")
            self.assertIn("┃AT┃ ORF 1 FHD", [s["name"] for s in row["streams"] if s["added"]])

    def test_or_shown_as_a_conflict_and_left_alone_when_asked(self):
        self._channel("┃AT┃ ORF 1 HD", 2, self.austria)
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        plan = channel_manager.build_plan(settings(several_matches="conflict"))

        conflict = next(r for r in plan["rows"] if r["status"] == "conflict")
        self.assertEqual(len(conflict["candidates"]), 2)
        # And applying it does nothing
        result = channel_manager.apply_plan(settings(several_matches="conflict"), [conflict["key"]])
        self.assertEqual(result, {"created": 0, "updated": 0, "streams_added": 0})

    def test_what_the_group_merge_did_is_what_this_does(self):
        """
        Taken from the group merge people ran by hand: for each channel in the group, every
        stream of the same name with its quality taken off, from any provider.
        """
        self._stream("┃AT┃ ORF 1 FHD", self.b, group=self.germany)  # another group, same name
        self._stream("┃AT┃ ORF 1 UHD", self.a)
        self._stream("┃AT┃ ORF 2", self.a)
        plan = channel_manager.build_plan(settings(channel_groups=[self.austria.id]))
        row = self._row(plan, f"ch:{self.orf1.id}")
        self.assertEqual(
            sorted(s["name"] for s in row["streams"] if s["added"]),
            ["┃AT┃ ORF 1 FHD", "┃AT┃ ORF 1 UHD"],
        )

    def test_only_the_chosen_group_is_touched(self):
        """The group-scoped merge people were running by hand."""
        orf_de = self._channel("┃DE┃ ORF 1", 5, self.germany)
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        plan = channel_manager.build_plan(settings(channel_groups=[self.austria.id]))
        self.assertFalse(any(r["key"] == f"ch:{orf_de.id}" for r in plan["rows"]))

    def test_nothing_is_written_by_looking(self):
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        before = self._order(self.orf1)
        channel_manager.build_plan(settings(create_new=True, reorder_existing=True, replace_streams=True))
        self.assertEqual(self._order(self.orf1), before)
        self.assertEqual(Channel.objects.count(), 1)


class ReplaceTests(_Setup):
    def test_a_stream_that_is_not_this_channel_is_removed_when_asked(self):
        wrong = self._stream("┃AT┃ PULS 4", self.a)
        ChannelStream.objects.create(channel=self.orf1, stream=wrong, order=5)

        row = self._row(channel_manager.build_plan(settings(replace_streams=True)), f"ch:{self.orf1.id}")
        self.assertEqual([s["name"] for s in row["streams"] if s["removed"]], ["┃AT┃ PULS 4"])

        channel_manager.apply_plan(settings(replace_streams=True), [f"ch:{self.orf1.id}"])
        self.assertNotIn("┃AT┃ PULS 4", self._order(self.orf1))

    def test_but_never_by_default(self):
        wrong = self._stream("┃AT┃ PULS 4", self.a)
        ChannelStream.objects.create(channel=self.orf1, stream=wrong, order=5)
        channel_manager.apply_plan(settings(), [f"ch:{self.orf1.id}"])
        self.assertIn("┃AT┃ PULS 4", self._order(self.orf1))

    def test_a_channel_is_never_emptied(self):
        """With nothing of its own left, it keeps what it has."""
        lonely = self._channel("┃AT┃ Lonely", 9, self.austria)
        wrong = self._stream("┃AT┃ Something Else", self.a)
        self._attach(lonely, [wrong])
        row = self._row(channel_manager.build_plan(settings(replace_streams=True)), f"ch:{lonely.id}")
        self.assertFalse(any(s["removed"] for s in row["streams"]))


class NewChannelTests(_Setup):
    def setUp(self):
        super().setUp()
        self.profile = ChannelProfile.objects.create(name="Everything")
        EPGData.objects.create(tvg_id="PULS4.at", name="PULS 4")

    def test_new_channels_are_only_made_when_asked(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        plan = channel_manager.build_plan(settings())
        self.assertFalse(any(r["status"] == "new" for r in plan["rows"]))

    def test_a_channel_no_channel_has_is_made_with_every_copy(self):
        self._stream("┃AT┃ PULS 4 HD", self.a, tvg_id="PULS4.at")
        self._stream("┃AT┃ PULS 4 FHD", self.b)
        plan = channel_manager.build_plan(settings(create_new=True))

        (row,) = [r for r in plan["rows"] if r["status"] == "new"]
        self.assertEqual(row["channel"]["name"], "┃AT┃ PULS 4")
        self.assertEqual(row["adds"], 2)
        self.assertEqual(row["streams"][0]["quality"], "FHD")
        self.assertEqual(row["channel"]["epg"]["how"], "tvg-id")
        # Numbered after the highest there is
        self.assertEqual(row["channel"]["number"], 2)

        channel_manager.apply_plan(settings(create_new=True), [row["key"]])
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertEqual(made.epg_data.tvg_id, "PULS4.at")
        self.assertEqual(self._order(made), ["┃AT┃ PULS 4 FHD", "┃AT┃ PULS 4 HD"])
        # Into every profile, as Dispatcharr does unless told otherwise
        self.assertTrue(ChannelProfileMembership.objects.filter(channel=made, channel_profile=self.profile).exists())

    def test_the_guide_is_found_by_name_when_no_stream_carries_a_tvg_id(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        (row,) = [r for r in channel_manager.build_plan(settings(create_new=True))["rows"] if r["status"] == "new"]
        self.assertEqual(row["channel"]["epg"]["how"], "name")

    def test_the_country_box_can_be_left_off_the_name(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        (row,) = [
            r for r in channel_manager.build_plan(settings(create_new=True, keep_country_prefix=False))["rows"]
            if r["status"] == "new"
        ]
        self.assertEqual(row["channel"]["name"], "PULS 4")

    def test_a_channel_with_too_few_copies_is_not_made(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        plan = channel_manager.build_plan(settings(create_new=True, min_streams_new=2))
        self.assertFalse(any(r["status"] == "new" for r in plan["rows"]))

    def test_no_profile_when_asked_for_none(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        (row,) = [r for r in channel_manager.build_plan(settings(create_new=True))["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(settings(create_new=True, profiles="none"), [row["key"]])
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertFalse(ChannelProfileMembership.objects.filter(channel=made).exists())


class ViewTests(_Setup):
    def setUp(self):
        super().setUp()
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )

    def test_the_options_list_what_the_levers_choose_between(self):
        data = self.client_api.get("/api/channels/channel-manager/").json()
        self.assertTrue({"Provider A", "Provider B"} <= {a["name"] for a in data["accounts"]})
        self.assertIn("┃AT┃ AUSTRIA", [g["name"] for g in data["channel_groups"]])
        self.assertEqual(data["defaults"]["create_new"], False)

    def test_preview_and_apply(self):
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        plan = self.client_api.post(
            "/api/channels/channel-manager/preview/", {"settings": {}}, format="json"
        ).json()
        self.assertEqual(plan["summary"]["merge"], 1)

        response = self.client_api.post(
            "/api/channels/channel-manager/apply/",
            {"settings": {}, "keys": [f"ch:{self.orf1.id}"]},
            format="json",
        )
        self.assertEqual(response.json()["updated"], 1)
        self.assertIn("┃AT┃ ORF 1 FHD", self._order(self.orf1))

    def test_the_levers_are_kept(self):
        self.client_api.put(
            "/api/channels/channel-manager/settings/", {"settings": {"order": "provider"}}, format="json"
        )
        self.assertEqual(channel_manager.load_settings()["order"], "provider")

    def test_nothing_chosen_is_refused(self):
        response = self.client_api.post(
            "/api/channels/channel-manager/apply/", {"keys": []}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(user=User.objects.create_user(username="v", password="x", user_level=0))
        self.assertEqual(viewer.get("/api/channels/channel-manager/").status_code, 403)
