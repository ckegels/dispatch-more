"""Recognising the same channel across providers and qualities (apps.channels.channel_manager).

The names are shaped like real ones: a country box in front, a quality at the end, the same
channel written three ways by three providers. The setup is shaped like a real one too:
channels in a country group, each with a custom fallback stream on the end that shows a
"could not play this" screen, which is the thing most easily broken by getting this wrong.
"""

from datetime import timedelta
from unittest.mock import patch

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
from apps.epg.models import EPGData, EPGSource
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

    def test_the_mark_a_provider_puts_on_a_recording_is_not_part_of_the_name(self):
        # It says the provider is recording that stream, not which channel it is
        self.assertEqual(
            channel_manager.clean_name("┃NL┃ NPO 1 ⏺ʳᵉᶜ", settings()), "┃NL┃ NPO 1"
        )
        # ...even written up against the name, where a whole-word guard would miss it
        self.assertEqual(
            channel_manager.clean_name("┃NL┃ NPO 1⏺ʳᵉᶜ", settings()), "┃NL┃ NPO 1"
        )

    def test_a_bare_word_to_ignore_is_still_only_taken_whole(self):
        self.assertEqual(
            channel_manager.clean_name("┃NL┃ DRAWING RAW", settings()), "┃NL┃ DRAWING"
        )

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
        row = self._row(channel_manager.build_plan(settings(match_tvg_id=True)), f"ch:{self.orf1.id}")
        self.assertEqual(row["adds"], 1)

    def test_a_shared_tvg_id_is_not_trusted_unless_asked(self):
        """
        Providers give one tvg-id to channels that are not the same. On a real setup a
        Krone stream carried Euronews' and was put on Euronews, and every CBS station and
        its East and West feeds were made one. DispatcharrUtils never looks at it.
        """
        euronews = Channel.objects.create(name="┃AT┃ EURONEWS", channel_number=5, channel_group=self.austria, tvg_id="euronews.at")
        self._stream("┃AT┃ KRONE TV", self.b, tvg_id="euronews.at")
        row = self._row(channel_manager.build_plan(settings()), f"ch:{euronews.id}")
        self.assertEqual(row["adds"], 0)

    def test_stations_of_one_network_are_not_made_one(self):
        cbs = Channel.objects.create(name="┃US┃ CBS", channel_number=6, channel_group=self.austria, tvg_id="CBS.us")
        for name in ("┃US┃ CBS EAST", "┃US┃ CBS WEST", "┃US┃ CBS (WCBS) NEW YORK", "┃US┃ CBS 4K SPORTS"):
            self._stream(name, self.b, tvg_id="CBS.us")
        self._stream("┃US┃ CBS HD", self.b, tvg_id="CBS.us")
        row = self._row(channel_manager.build_plan(settings()), f"ch:{cbs.id}")
        self.assertEqual([s["name"] for s in row["streams"] if s["added"]], ["┃US┃ CBS HD"])

    def test_only_a_quality_at_the_end_is_taken_off(self):
        """As DispatcharrUtils' normalizer and the group merge do. Several in a row go."""
        clean = lambda name: channel_manager.clean_name(name, settings())
        self.assertEqual(clean("┃AT┃ ORF 1 FHD HEVC"), "┃AT┃ ORF 1")
        self.assertEqual(clean("┃AT┃ ORF 1 (1080p)"), "┃AT┃ ORF 1")
        self.assertEqual(clean("┃US┃ CBS 4K SPORTS"), "┃US┃ CBS 4K SPORTS")
        self.assertEqual(clean("┃UK┃ SKY HD"), "┃UK┃ SKY")
        # A name that is nothing but a quality is kept, rather than matching everything
        self.assertEqual(clean("HD"), "HD")

    def test_several_channels_of_one_name_each_get_it_as_the_group_merge_did(self):
        """
        Dispatcharr's own sync makes a channel per stream, so a setup has plenty of channels
        with one name. The group merge people ran gave each of them every stream of that
        name, and so does this -- with the combining of duplicates turned off, which is
        what that parity now means: on, the two are offered as one channel instead.
        """
        twin = self._channel("┃AT┃ ORF 1 HD", 2, self.austria)
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        plan = channel_manager.build_plan(settings(combine_duplicates=False))

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
        self.assertEqual(
            result, {"created": 0, "updated": 0, "streams_added": 0, "combined": 0}
        )

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


class HandOrderTests(_Setup):
    """Streams put in another order on the page, so the Channels page is not needed for it."""

    def test_the_order_given_is_applied(self):
        fhd = self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = f"ch:{self.orf1.id}"
        order = [fhd.id, self.existing.id, self.fallback.id]
        channel_manager.apply_plan(settings(), [key], {key: order})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1 FHD", "┃AT┃ ORF 1", "could not dispatch"])

    def test_a_channel_with_nothing_else_to_change_is_applied_for_its_order(self):
        second = self._stream("┃AT┃ ORF 1 HD", self.b)
        ChannelStream.objects.filter(channel=self.orf1).delete()
        self._attach(self.orf1, [self.existing, second, self.fallback])
        key = f"ch:{self.orf1.id}"
        self.assertEqual(self._row(channel_manager.build_plan(settings()), key)["status"], "unchanged")

        result = channel_manager.apply_plan(settings(), [key], {key: [second.id, self.existing.id, self.fallback.id]})
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1 HD", "┃AT┃ ORF 1", "could not dispatch"])

    def test_the_fallback_stays_last_whatever_order_is_given(self):
        fhd = self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], {key: [self.fallback.id, fhd.id, self.existing.id]})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1 FHD", "┃AT┃ ORF 1", "could not dispatch"])

    def test_an_order_of_other_streams_than_there_are_now_is_ignored(self):
        """The streams changed since the page was looked at: the plan's own order stands."""
        fhd = self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], {key: [fhd.id, self.fallback.id]})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "┃AT┃ ORF 1 FHD", "could not dispatch"])


class CombineTests(_Setup):
    """Two channels that are the same channel made one, and the other deleted."""

    def setUp(self):
        super().setUp()
        self.news = ChannelGroup.objects.create(name="┃AT┃ NEWS")
        # The same channel again, in another group, with a stream of its own
        self.twin = self._channel("┃AT┃ ORF 1", 300, self.news)
        self.twins_own = self._stream("┃AT┃ ORF 1 HD", self.b)
        self._attach(self.twin, [self.twins_own])

    def test_it_is_suggested_by_default_but_still_only_a_suggestion(self):
        # On by default: having one channel twice is the thing people come here to fix
        self.assertTrue(channel_manager.DEFAULTS["combine_duplicates"])
        plan = channel_manager.build_plan(settings())
        self.assertTrue(any(r["status"] == "combine" for r in plan["rows"]))
        # ...and nothing is deleted by looking, nor by applying a row that is not it
        self.assertTrue(Channel.objects.filter(id=self.twin.id).exists())

    def test_nothing_is_combined_when_it_is_turned_off(self):
        plan = channel_manager.build_plan(settings(combine_duplicates=False))
        self.assertFalse(any(r["status"] == "combine" for r in plan["rows"]))
        channel_manager.apply_plan(
            settings(combine_duplicates=False), [r["key"] for r in plan["rows"]]
        )
        self.assertTrue(Channel.objects.filter(id=self.twin.id).exists())

    def test_the_two_become_one_row_saying_which_channel_goes(self):
        plan = channel_manager.build_plan(settings())
        (row,) = [r for r in plan["rows"] if r["status"] == "combine"]
        # The one kept is the lowest number in the group its country's channels are in
        self.assertEqual(row["channel"]["id"], self.orf1.id)
        self.assertEqual([c["id"] for c in row["combining"]], [self.twin.id])
        # and neither of them has a row of its own
        self.assertFalse(any(r["key"] == f"ch:{self.twin.id}" for r in plan["rows"]))

    def test_the_streams_of_both_end_up_on_the_one_kept_and_the_other_goes(self):
        levers = settings()
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "combine"]
        answer = channel_manager.apply_plan(levers, [row["key"]])
        self.assertEqual(answer["combined"], 1)
        self.assertFalse(Channel.objects.filter(id=self.twin.id).exists())
        self.assertEqual(
            self._order(self.orf1), ["┃AT┃ ORF 1", "┃AT┃ ORF 1 HD", "could not dispatch"]
        )

    def test_the_fallback_stays_last_when_two_channels_are_made_one(self):
        self._attach(self.twin, [self.fallback])
        levers = settings()
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "combine"]
        channel_manager.apply_plan(levers, [row["key"]])
        self.assertEqual(self._order(self.orf1)[-1], "could not dispatch")

    def test_channels_of_two_different_countries_are_never_combined(self):
        # The whole fork is built on telling these apart; a name they share is not enough
        german = self._channel("┃DE┃ ORF 1", 400, self.germany)
        plan = channel_manager.build_plan(settings())
        (row,) = [r for r in plan["rows"] if r["status"] == "combine"]
        self.assertNotIn(german.id, [c["id"] for c in row["combining"]])
        self.assertNotEqual(row["channel"]["id"], german.id)

    def test_the_group_chosen_on_the_page_decides_which_one_is_kept(self):
        levers = settings()
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "combine"]
        # Asked for the news group, where the twin already is
        channel_manager.apply_plan(levers, [row["key"]], groups={row["key"]: self.news.id})
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.channel_group_id, self.news.id)

    def test_a_set_waved_away_is_not_suggested_again(self):
        levers = settings()
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "combine"]
        channel_manager.ignore(row["key"], name=row["channel"]["name"], kind="combine")
        plan = channel_manager.build_plan(levers)
        self.assertFalse(any(r["status"] == "combine" for r in plan["rows"]))
        # and both channels have their own rows again
        self.assertTrue(any(r["key"] == f"ch:{self.twin.id}" for r in plan["rows"]))


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

    def test_new_channels_are_suggested_unless_turned_off(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        self.assertTrue(any(r["status"] == "new" for r in channel_manager.build_plan(settings())["rows"]))
        plan = channel_manager.build_plan(settings(create_new=False))
        self.assertFalse(any(r["status"] == "new" for r in plan["rows"]))

    def test_only_streams_from_groups_in_use_are_suggested(self):
        """Not every provider stream there is: those from the groups your channels come from."""
        self._stream("┃AT┃ PULS 4 HD", self.a)
        self._stream("┃DE┃ PRO 7 HD", self.a, group=self.germany)
        names = [r["channel"]["name"] for r in channel_manager.build_plan(settings())["rows"] if r["status"] == "new"]
        self.assertEqual(names, ["┃AT┃ PULS 4"])
        names = [
            r["channel"]["name"] for r in channel_manager.build_plan(settings(new_from="all"))["rows"]
            if r["status"] == "new"
        ]
        self.assertEqual(sorted(names), ["┃AT┃ PULS 4", "┃DE┃ PRO 7"])

    def test_suggested_group_and_number_follow_your_channels(self):
        """Streams of a provider group go where your channels from it are, numbered after them."""
        provider_group = ChannelGroup.objects.create(name="AT | AUSTRIA (provider)")
        ChannelStream.objects.filter(channel=self.orf1).delete()
        self._attach(self.orf1, [self.existing, self._stream("┃AT┃ ORF 1 HD", self.b, group=provider_group), self.fallback])
        self._channel("┃DE┃ ARD", 2, self.germany)
        self._channel("┃AT┃ SERVUS", 3, self.austria)
        self._stream("┃AT┃ PULS 4 HD", self.b, group=provider_group)
        (row,) = [r for r in channel_manager.build_plan(settings())["rows"] if r["status"] == "new"]
        self.assertEqual(row["channel"]["group"], "┃AT┃ AUSTRIA")
        self.assertIn("stream group", row["channel"]["group_why"])
        # After the last Austrian channel, 3, on a number nobody has
        self.assertEqual(row["channel"]["number"], 4)

    def test_every_row_says_which_group_it_is_in_so_it_can_be_narrowed_to(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        rows = channel_manager.build_plan(settings())["rows"]
        have = self._row({"rows": rows}, f"ch:{self.orf1.id}")
        self.assertEqual(have["channel"]["group_id"], self.austria.id)
        self.assertEqual(have["before"]["channel"]["group_id"], self.austria.id)
        # and a new channel by the group it is suggested for
        (made,) = [r for r in rows if r["status"] == "new"]
        self.assertIsNotNone(made["channel"]["group_id"])

    def test_a_group_chosen_on_the_page_is_used_and_numbered_there(self):
        self._channel("┃DE┃ ARD", 50, self.germany)
        self._stream("┃AT┃ PULS 4 HD", self.a)
        (row,) = [r for r in channel_manager.build_plan(settings())["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(settings(), [row["key"]], groups={row["key"]: self.germany.id})
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertEqual((made.channel_group_id, made.channel_number), (self.germany.id, 51))

    def test_a_channel_no_channel_has_is_made_with_every_copy(self):
        self._stream("┃AT┃ PULS 4 HD", self.a, tvg_id="PULS4.at")
        self._stream("┃AT┃ PULS 4 FHD", self.b)
        levers = settings(create_new=True, order="quality", epg="tvg_id_then_name")
        plan = channel_manager.build_plan(levers)

        (row,) = [r for r in plan["rows"] if r["status"] == "new"]
        self.assertEqual(row["channel"]["name"], "┃AT┃ PULS 4")
        self.assertEqual(row["adds"], 2)
        self.assertEqual(row["streams"][0]["quality"], "FHD")
        self.assertEqual(row["channel"]["epg"]["how"], "tvg-id")
        # Numbered after the last channel of its group
        self.assertEqual(row["channel"]["number"], 2)

        channel_manager.apply_plan(levers, [row["key"]])
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertEqual(made.epg_data.tvg_id, "PULS4.at")
        # Ending in the fallback the other channels end in
        self.assertEqual(self._order(made), ["┃AT┃ PULS 4 FHD", "┃AT┃ PULS 4 HD", "could not dispatch"])
        # Into every profile, as Dispatcharr does unless told otherwise
        self.assertTrue(ChannelProfileMembership.objects.filter(channel=made, channel_profile=self.profile).exists())

    def test_the_guide_is_found_by_name_when_no_stream_carries_a_tvg_id(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        levers = settings(create_new=True, epg="tvg_id_then_name")
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "new"]
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


class PageChoiceTests(_Setup):
    """What is chosen on the page: streams dropped from a row, and suggestions ignored."""

    def test_a_stream_dropped_on_the_page_is_not_added(self):
        good = self._stream("┃AT┃ ORF 1 FHD", self.b)
        wrong = self._stream("┃AT┃ ORF 1 HD", self.b)
        plan = channel_manager.build_plan(settings())
        channel_manager.apply_plan(settings(), ["ch:%d" % self.orf1.id], drops={"ch:%d" % self.orf1.id: [wrong.id]})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "┃AT┃ ORF 1 FHD", "could not dispatch"])
        self.assertIn(good.id, [s["id"] for s in self._row(plan, "ch:%d" % self.orf1.id)["streams"]])

    def test_a_stream_the_channel_has_can_be_dropped_but_never_the_fallback(self):
        key = "ch:%d" % self.orf1.id
        channel_manager.apply_plan(settings(), [key], drops={key: [self.existing.id, self.fallback.id]})
        # Dropping a stream a channel has takes it off; the fallback stays
        self.assertEqual(self._order(self.orf1), ["could not dispatch"])

    def test_an_ignored_new_channel_is_not_suggested_again(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        (row,) = [r for r in channel_manager.build_plan(settings())["rows"] if r["status"] == "new"]
        channel_manager.ignore(row["key"], name=row["channel"]["name"], kind="new")
        plan = channel_manager.build_plan(settings())
        self.assertFalse(any(r["status"] == "new" for r in plan["rows"]))
        self.assertEqual([i["name"] for i in plan["ignored"]], ["┃AT┃ PULS 4"])
        channel_manager.unignore()
        self.assertTrue(any(r["status"] == "new" for r in channel_manager.build_plan(settings())["rows"]))

    def test_ignoring_a_merge_leaves_only_those_streams_alone(self):
        """A stream the provider adds later is still suggested."""
        first = self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = "ch:%d" % self.orf1.id
        channel_manager.ignore(key, name="┃AT┃ ORF 1", kind="merge", streams=[first.id])
        self.assertEqual(self._row(channel_manager.build_plan(settings()), key)["status"], "unchanged")
        later = self._stream("┃AT┃ ORF 1 HD", self.a)
        row = self._row(channel_manager.build_plan(settings()), key)
        self.assertEqual([s["id"] for s in row["streams"] if s["added"]], [later.id])

    def test_new_channels_take_a_logo_from_the_collections_by_default(self):
        self.assertEqual(channel_manager.DEFAULTS["new_logo"], "collections")
        self._stream("┃AT┃ PULS 4 HD", self.a, logo_url="http://logos/puls4.png")
        levers = settings(new_logo="stream")
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "new"]
        self.assertEqual(row["channel"]["logo_url"], "http://logos/puls4.png")


class JudgingGuidesTests(TestCase):
    """
    How good a match a guide is, and what kind of match it is.

    The comparison used to be on letters alone, over a name that had had the words that
    tell two channels apart taken out of it first. That made a completely different
    station read as a certainty, which is the worst thing a page like this can say.
    """

    def judge(self, name, guide_name, tvg_id="", country="us", mine=""):
        return channel_manager.judge_guide(
            name, country, {"name": guide_name, "tvg_id": tvg_id}, mine
        )

    def test_east_and_west_are_not_the_same_channel(self):
        # Stock takes "east" and "west" off as extraneous, which leaves both as "pbs"
        # and matches them at a hundred per cent
        score, tier, why = self.judge("┃USA┃ PBS EAST", "PBS West", "pbs.west")
        self.assertEqual(score, 0)
        self.assertIn("side", why)

    def test_one_broadcaster_is_not_another_however_alike_the_letters(self):
        """
        The worst of them. "PBS Philadelphia" and "CBS Philadelphia" are ninety-four per
        cent alike letter by letter and are two different television stations, while
        "PBS WHYY Philadelphia" -- the right one -- is forty-one per cent alike and was
        falling below the bar to be offered at all.
        """
        for theirs in ("CBS Philadelphia", "ABC Philadelphia", "NBC Philadelphia"):
            score, _, why = self.judge("┃USA┃ PBS PHILADELPHIA", theirs)
            self.assertEqual(score, 0, theirs)
            self.assertIn("is not", why)

    def test_and_the_right_station_is_found_instead(self):
        score, _, _ = self.judge("┃USA┃ PBS PHILADELPHIA", "PBS WHYY Philadelphia", "whyy.us")
        self.assertGreater(score, 80)

    def test_a_name_is_compared_in_words_not_letters(self):
        # Every word shared, in a longer name: most of both, rather than the little of
        # each other they are letter by letter
        self.assertEqual(channel_manager._alike(["pbs", "philadelphia"], ["pbs", "philadelphia"]), 100)
        self.assertGreaterEqual(
            channel_manager._alike(["pbs", "philadelphia"], ["pbs", "whyy", "philadelphia"]), 75
        )
        # ...and a spelling is still the same word
        self.assertEqual(channel_manager._alike(["dreamworks"], ["dreamwork"]), 100)

    def test_letters_and_digits_stuck_together_are_two_words(self):
        self.assertEqual(channel_manager.guide_words("BBC1"), ["bbc", "1"])
        score, tier, _ = self.judge("┃UK┃ BBC ONE", "BBC1", "bbc1.uk", country="gb")
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))

    def test_ordinary_short_words_are_not_taken_for_broadcasters(self):
        # "Nothing like it" would otherwise offer "like" and "it" and refuse everything
        self.assertEqual(channel_manager._names_in(channel_manager.guide_words("Nothing like it")), set())
        self.assertEqual(channel_manager._names_in(channel_manager.guide_words("PBS")), {"pbs"})

    def test_two_numbers_that_differ_are_two_channels(self):
        self.assertEqual(self.judge("┃USA┃ PBS 12", "PBS 13", "pbs13.us")[0], 0)
        self.assertEqual(self.judge("┃UK┃ SKY SPORTS 1", "Sky Sports 2")[0], 0)

    def test_but_a_number_written_as_a_word_is_that_number(self):
        score, tier, _ = self.judge("┃AT┃ ORF 1", "ORF Eins", "orfeins.at", country="at")
        self.assertEqual(score, 100)
        self.assertEqual(tier, channel_manager.CERTAIN)

    def test_two_call_signs_are_never_the_same_station(self):
        score, _, why = self.judge("┃USA┃ PBS WNET", "PBS KQED", "kqed.us")
        self.assertEqual(score, 0)
        self.assertIn("call sign", why)

    def test_a_tvg_id_that_agrees_with_the_name_is_a_certainty(self):
        score, tier, why = self.judge(
            "┃AT┃ ORF 1", "ORF 1", "orf1.at", country="at", mine="ORF1.at"
        )
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))
        self.assertEqual(why, "its tvg-id and its name")

    def test_but_a_tvg_id_is_a_providers_word_and_not_proof(self):
        """
        Providers write it wrongly -- one id on channels that are not the same, and an
        old id left on a channel that was renamed. This fork has been bitten already, by
        a Krone stream carrying Euronews' id, which is why matching on tvg-id is off by
        default in the Channel Manager.
        """
        score, tier, why = self.judge(
            "┃AT┃ KRONE TV", "Euronews", "euronews.at", country="at", mine="euronews.at"
        )
        self.assertEqual(tier, channel_manager.LIKELY)
        self.assertIn("do not read alike", why)
        # Worth offering, because the very same thing happens when a channel is renamed
        # and keeps its id, and only what is on the guide now tells the two apart
        self.assertGreaterEqual(score, 80)

    def test_and_never_beats_a_name_that_contradicts_it(self):
        for name, theirs, said in (
            ("┃USA┃ PBS 12", "PBS 13", "number"),
            ("┃USA┃ HBO EAST", "HBO West", "side"),
        ):
            score, tier, why = self.judge(name, theirs, "shared.id", mine="shared.id")
            self.assertEqual(score, 0, name)
            self.assertIn("whatever its tvg-id says", why)

    def test_a_name_that_only_reads_alike_is_no_more_than_a_guess(self):
        score, tier, _ = self.judge("┃NL┃ DREAMWORKS", "DreamWorks", "dreamworks.uk", country="nl")
        self.assertEqual(tier, channel_manager.GUESS)
        self.assertLess(score, 80)

    def test_and_the_same_name_in_the_right_country_is_a_certainty(self):
        score, tier, _ = self.judge("┃NL┃ DREAMWORKS", "DreamWorks", "dreamworks.nl", country="nl")
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))

    def test_one_saying_a_number_and_the_other_not_is_not_certain(self):
        _, tier, _ = self.judge("┃USA┃ PBS 12", "PBS", "pbs.us")
        self.assertEqual(tier, channel_manager.GUESS)


class GuideChoiceTests(_Setup):
    """The guide picker on a row: what it offers, and what choosing one does."""

    def setUp(self):
        super().setUp()
        self.big = EPGSource.objects.create(name="Everything", source_type="xmltv", priority=1)
        self.local = EPGSource.objects.create(name="Austria", source_type="xmltv", priority=9)
        self.off = EPGSource.objects.create(
            name="Switched off", source_type="xmltv", priority=99, is_active=False
        )

    def test_the_source_you_put_first_wins_a_name_two_sources_carry(self):
        EPGData.objects.create(tvg_id="orf1.generic", name="ORF 1", epg_source=self.big)
        wanted = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        (row,) = [
            r for r in channel_manager.build_plan(settings(epg="tvg_id_then_name"))["rows"]
            if r["key"] == f"ch:{self.orf1.id}"
        ]
        self.assertEqual(row["channel"]["epg"]["id"], wanted.id)

    def test_a_source_switched_off_is_not_offered(self):
        EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.off)
        found = channel_manager.guide_candidates("┃AT┃ ORF 1")
        self.assertEqual(found, [])

    def test_an_exact_tvg_id_comes_first_but_does_not_claim_to_be_certain(self):
        # A provider wrote that id. It goes to the top of the list, and it says what it
        # is worth: with a name that reads nothing like it, it is likely and not certain
        EPGData.objects.create(tvg_id="ORF1.at", name="Nothing like it", epg_source=self.local)
        EPGData.objects.create(tvg_id="other", name="ORF 1", epg_source=self.big)
        found = channel_manager.guide_candidates("┃AT┃ ORF 1", tvg_id="ORF1.at")
        self.assertEqual(found[0]["name"], "Nothing like it")
        self.assertEqual(found[0]["how"], "tvg-id")
        self.assertEqual(found[0]["tier"], channel_manager.LIKELY)
        self.assertIn("do not read alike", found[0]["why"])

    def test_and_a_tvg_id_on_a_name_that_contradicts_it_is_not_offered_at_all(self):
        EPGData.objects.create(tvg_id="pbs.us", name="PBS 13", epg_source=self.local)
        found = channel_manager.guide_candidates("┃USA┃ PBS 12", tvg_id="pbs.us")
        self.assertEqual([one["name"] for one in found], [])

    def test_the_country_box_does_not_drag_the_match_down(self):
        EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        (best,) = channel_manager.guide_candidates("┃AT┃ ORF 1")
        self.assertEqual(best["name"], "ORF 1")
        self.assertEqual(best["source"], "Austria")
        self.assertGreaterEqual(best["score"], 90)

    def test_a_name_nothing_like_it_is_not_offered_as_a_match(self):
        EPGData.objects.create(tvg_id="x.1", name="Sender Eins", epg_source=self.local)
        # The matcher scores everything it sees; only what is alike enough is offered
        self.assertEqual(channel_manager.guide_candidates("┃AT┃ ORF 1"), [])

    def test_but_a_name_that_is_the_same_channel_written_differently_is(self):
        EPGData.objects.create(tvg_id="x.1", name="ORF Eins", epg_source=self.local)
        (found,) = channel_manager.guide_candidates("┃AT┃ ORF 1")
        self.assertEqual(found["name"], "ORF Eins")

    def test_searching_finds_the_words_anywhere_not_the_phrase_as_typed(self):
        """
        Somebody after the Philadelphia PBS station types "pbs philadelphia"; the guide
        calls it "PBS WHYY Philadelphia". Every word is there and the phrase is not.
        """
        wanted = EPGData.objects.create(
            tvg_id="whyy.us", name="PBS WHYY Philadelphia", epg_source=self.local
        )
        EPGData.objects.create(tvg_id="cbs.us", name="CBS Philadelphia", epg_source=self.local)
        found = channel_manager.guide_candidates("┃USA┃ PBS", search="pbs philadelphia")
        self.assertEqual([one["id"] for one in found], [wanted.id])

    def test_and_the_nearest_to_what_was_typed_comes_first(self):
        near = EPGData.objects.create(tvg_id="a.us", name="PBS Kids", epg_source=self.local)
        EPGData.objects.create(
            tvg_id="b.us", name="PBS Kids Something Else Entirely", epg_source=self.local
        )
        found = channel_manager.guide_candidates("┃USA┃ PBS KIDS", search="pbs kids")
        self.assertEqual(found[0]["id"], near.id)

    def test_a_guide_the_matcher_cannot_see_is_still_there_to_search_for(self):
        EPGData.objects.create(tvg_id="x.1", name="Sender Eins", epg_source=self.local)
        (found,) = channel_manager.guide_candidates("┃AT┃ ORF 1", search="Sender")
        self.assertEqual(found["name"], "Sender Eins")
        self.assertEqual(found["how"], "search")

    def test_what_a_guide_holds_is_what_tells_two_of_one_name_apart(self):
        from django.utils import timezone

        from apps.epg.models import ProgramData

        right = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        EPGData.objects.create(tvg_id="orf1.generic", name="ORF 1", epg_source=self.big)
        moment = timezone.now()
        ProgramData.objects.create(
            epg=right, title="Zeit im Bild",
            start_time=moment - timedelta(minutes=10), end_time=moment + timedelta(minutes=20),
        )
        ProgramData.objects.create(
            epg=right, title="Later",
            start_time=moment + timedelta(minutes=20), end_time=moment + timedelta(minutes=50),
        )
        found = {entry["id"]: entry for entry in channel_manager.guide_candidates("┃AT┃ ORF 1")}
        self.assertEqual(found[right.id]["now"], "Zeit im Bild")
        self.assertEqual(found[right.id]["programmes"], 2)
        # The one that is a name and nothing else says so
        other = next(entry for key, entry in found.items() if key != right.id)
        self.assertEqual(other["now"], "")
        self.assertEqual(other["programmes"], 0)

    def test_holding_nothing_and_never_having_been_read_are_not_the_same(self):
        # Dispatcharr reads a guide's programmes only once a channel uses it, so an entry
        # nothing uses and that holds nothing has almost certainly never been read
        never_read = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        really_empty = EPGData.objects.create(tvg_id="orf1.b", name="ORF 1", epg_source=self.big)
        self.orf1.epg_data = really_empty
        self.orf1.save(update_fields=["epg_data"])
        found = {entry["id"]: entry for entry in channel_manager.guide_candidates("┃AT┃ ORF 1")}
        self.assertFalse(found[never_read.id]["in_use"])
        self.assertTrue(found[really_empty.id]["in_use"])

    def test_a_guide_from_another_country_is_not_the_same_channel(self):
        # The matcher never sees the country: normalize_name takes the box off, so both
        # of these are "dreamworks" and score a flat 100 on the name alone
        right = EPGData.objects.create(tvg_id="dreamworks.nl", name="DreamWorks", epg_source=self.big)
        wrong = EPGData.objects.create(tvg_id="dreamworks.uk", name="DreamWorks", epg_source=self.big)
        found = channel_manager.guide_candidates("┃NL┃ DREAMWORKS")
        self.assertEqual(found[0]["id"], right.id)
        self.assertGreater(found[0]["score"], 90)
        theirs = next((one for one in found if one["id"] == wrong.id), None)
        # Either well below the right one, or not worth offering at all
        self.assertTrue(theirs is None or theirs["score"] < found[0]["score"] - 20)

    def test_uk_and_gb_are_the_one_country(self):
        # Playlists say UK, tvg-ids say .uk, and the country's code is gb
        guide = EPGData.objects.create(tvg_id="BBCOne.uk", name="BBC One", epg_source=self.big)
        (best,) = channel_manager.guide_candidates("┃UK┃ BBC ONE")
        self.assertEqual(best["id"], guide.id)
        self.assertGreaterEqual(best["score"], 100)

    def test_a_guide_that_names_no_country_is_judged_on_its_name_alone(self):
        guide = EPGData.objects.create(tvg_id="dreamworks", name="DreamWorks", epg_source=self.big)
        (best,) = channel_manager.guide_candidates("┃NL┃ DREAMWORKS")
        self.assertEqual(best["id"], guide.id)
        self.assertGreaterEqual(best["score"], 90)

    def test_what_is_on_each_guide_is_on_the_plan_for_both_sides(self):
        from django.utils import timezone

        from apps.epg.models import ProgramData

        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        self.orf1.epg_data = guide
        self.orf1.save(update_fields=["epg_data"])
        moment = timezone.now()
        ProgramData.objects.create(
            epg=guide, title="Zeit im Bild",
            start_time=moment - timedelta(minutes=5), end_time=moment + timedelta(minutes=25),
        )
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")
        self.assertEqual(row["before"]["channel"]["epg"]["now"], "Zeit im Bild")
        self.assertEqual(row["before"]["channel"]["epg"]["programmes"], 1)
        self.assertEqual(row["channel"]["epg"]["now"], "Zeit im Bild")

    def test_a_guide_holding_nothing_says_so_on_the_plan(self):
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        self.orf1.epg_data = guide
        self.orf1.save(update_fields=["epg_data"])
        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")
        self.assertEqual(row["before"]["channel"]["epg"]["programmes"], 0)
        self.assertEqual(row["before"]["channel"]["epg"]["now"], "")

    def test_the_guide_a_channel_is_on_is_shown_with_its_source_before_and_after(self):
        # So the two can be read against each other: two entries of one name are told
        # apart by where they come from
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        self.orf1.epg_data = guide
        self.orf1.save(update_fields=["epg_data"])
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")
        self.assertEqual(row["before"]["channel"]["epg"]["name"], "ORF 1")
        self.assertEqual(row["before"]["channel"]["epg"]["source"], "Austria")
        self.assertEqual(row["channel"]["epg"]["source"], "Austria")

    def test_a_guide_belonging_to_no_source_says_so_rather_than_failing(self):
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1")
        self.orf1.epg_data = guide
        self.orf1.save(update_fields=["epg_data"])
        row = self._row(channel_manager.build_plan(settings()), f"ch:{self.orf1.id}")
        self.assertEqual(row["before"]["channel"]["epg"]["source"], "")

    def test_guides_are_read_in_one_pass_of_each_source_s_file(self):
        # Reading one costs a pass of the whole file, so a window's worth goes in one
        a = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        b = EPGData.objects.create(tvg_id="ORF2.at", name="ORF 2", epg_source=self.local)
        c = EPGData.objects.create(tvg_id="x.1", name="X", epg_source=self.big)
        with patch("apps.channels.tasks.read_guide_programmes.delay") as reading:
            answer = channel_manager.load_programmes([a.id, b.id, c.id])
        self.assertEqual(answer, {"queued": True, "reading": 3})
        (asked,) = reading.call_args[0]
        self.assertEqual(sorted(asked[str(self.local.id)]), sorted([a.id, b.id]))
        self.assertEqual(asked[str(self.big.id)], [c.id])

    def test_a_guide_no_channel_uses_is_read_all_the_same(self):
        # Dispatcharr's own task does nothing for an unused guide unless it is forced,
        # and an unused guide is the only kind this is ever asked about
        sd = EPGSource.objects.create(name="Schedules Direct", source_type="schedules_direct")
        guide = EPGData.objects.create(tvg_id="sd.1", name="Some station", epg_source=sd)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay") as asked:
            self.assertEqual(channel_manager.load_programmes([guide.id])["reading"], 1)
        asked.assert_called_once_with(guide.id, force=True)

    def test_the_reading_says_where_it_has_got_to(self):
        from apps.channels.tests.test_guide_manager import FakeRedis

        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        fake = FakeRedis()
        with patch("apps.channels.channel_manager._reading_redis", return_value=fake):
            with patch("apps.channels.tasks.read_guide_programmes.delay"):
                channel_manager.load_programmes([guide.id])
            state = channel_manager.reading_state()
        self.assertTrue(state["reading"])
        self.assertEqual(state["total"], 1)
        self.assertEqual(state["done"], 0)

    def test_a_dummy_guide_has_nothing_to_read(self):
        dummy = EPGSource.objects.create(name="Made up", source_type="dummy")
        guide = EPGData.objects.create(tvg_id="d.1", name="Dummy", epg_source=dummy)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay") as asked:
            with patch("apps.channels.tasks.read_guide_programmes.delay") as reading:
                answer = channel_manager.load_programmes([guide.id])
        self.assertEqual(answer, {"queued": False, "reading": 0})
        asked.assert_not_called()
        reading.assert_not_called()

    def test_reading_a_guide_that_is_gone_says_so_rather_than_failing(self):
        with patch("apps.channels.tasks.read_guide_programmes.delay") as reading:
            self.assertIn("error", channel_manager.load_programmes([9999]))
        reading.assert_not_called()

    def test_the_guide_a_channel_has_is_always_on_the_list(self):
        held = EPGData.objects.create(tvg_id="x.1", name="Sender Eins", epg_source=self.local)
        EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        # Nothing like the name, so the matcher would never offer it
        (first, *rest) = channel_manager.guide_candidates("┃AT┃ ORF 1", current=held.id)
        self.assertEqual(first["id"], held.id)
        self.assertEqual(first["how"], "kept")
        self.assertEqual([entry["name"] for entry in rest], ["ORF 1"])

    def test_and_stays_on_the_list_while_searching_for_another(self):
        held = EPGData.objects.create(tvg_id="x.1", name="Sender Eins", epg_source=self.local)
        EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        found = channel_manager.guide_candidates("┃AT┃ ORF 1", search="ORF", current=held.id)
        self.assertEqual([entry["name"] for entry in found], ["Sender Eins", "ORF 1"])

    def test_a_guide_that_is_gone_is_simply_not_on_it(self):
        found = channel_manager.guide_candidates("┃AT┃ ORF 1", current=9999)
        self.assertEqual(found, [])
        self.assertEqual(channel_manager.guide_candidates("┃AT┃ ORF 1", current="rubbish"), [])

    def test_a_guide_chosen_by_hand_is_put_on_the_channel_with_its_tvg_id(self):
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], epgs={key: guide.id})
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.epg_data_id, guide.id)
        self.assertEqual(self.orf1.tvg_id, "ORF1.at")

    def test_no_guide_is_a_choice_of_its_own(self):
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        self.orf1.epg_data = guide
        self.orf1.save(update_fields=["epg_data"])
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], epgs={key: None})
        self.orf1.refresh_from_db()
        self.assertIsNone(self.orf1.epg_data_id)

    def test_a_guide_gone_since_the_page_was_looked_at_is_not_applied(self):
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], epgs={key: 9999})
        self.orf1.refresh_from_db()
        self.assertIsNone(self.orf1.epg_data_id)

    def test_a_guide_chosen_for_a_new_channel_beats_the_one_matched(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        EPGData.objects.create(tvg_id="PULS4.at", name="PULS 4", epg_source=self.local)
        chosen = EPGData.objects.create(tvg_id="PULS4.de", name="PULS 4 Germany", epg_source=self.big)
        (row,) = [r for r in channel_manager.build_plan(settings())["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(
            settings(epg="tvg_id_then_name"), [row["key"]], epgs={row["key"]: chosen.id}
        )
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertEqual(made.epg_data_id, chosen.id)
        self.assertEqual(made.tvg_id, "PULS4.de")


class NameChoiceTests(_Setup):
    """The name typed on a row: what a new channel is called, or a channel renamed."""

    def test_a_new_channel_takes_the_name_typed_for_it(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        (row,) = [r for r in channel_manager.build_plan(settings())["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(settings(), [row["key"]], names={row["key"]: "┃AT┃ Puls 4"})
        self.assertTrue(Channel.objects.filter(name="┃AT┃ Puls 4").exists())
        self.assertFalse(Channel.objects.filter(name="┃AT┃ PULS 4").exists())

    def test_a_channel_you_have_is_renamed(self):
        self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], names={key: "┃AT┃ ORF Eins"})
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.name, "┃AT┃ ORF Eins")
        # and still gains the stream the row was about
        self.assertIn("┃AT┃ ORF 1 FHD", self._order(self.orf1))

    def test_a_row_with_nothing_else_to_change_is_applied_for_its_name_alone(self):
        key = f"ch:{self.orf1.id}"
        row = self._row(channel_manager.build_plan(settings()), key)
        self.assertEqual(row["status"], "unchanged")
        channel_manager.apply_plan(settings(), [key], names={key: "┃AT┃ ORF Eins"})
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.name, "┃AT┃ ORF Eins")

    def test_a_name_of_nothing_but_spaces_is_no_name_at_all(self):
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], names={key: "   "})
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.name, "┃AT┃ ORF 1")

    def test_a_long_name_is_cut_to_what_the_field_holds(self):
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], names={key: "N" * 400})
        self.orf1.refresh_from_db()
        self.assertEqual(len(self.orf1.name), 255)

    def test_renaming_leaves_the_fallback_where_it_is(self):
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], names={key: "┃AT┃ ORF Eins"})
        self.assertEqual(self._order(self.orf1)[-1], "could not dispatch")


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
        self.assertEqual(data["defaults"]["create_new"], True)

    def test_every_group_is_said_to_be_one_of_four_kinds(self):
        """
        Which groups the picker offers is a lever, so the page is told what each group is
        rather than being handed a list somebody else chose. A group with channels is
        yours; one with nothing in it at all was made by hand, very likely a moment ago on
        this page; one carrying a provider's streams is the provider's, and whether that
        provider is switched on is the difference between the two kinds of those.
        """
        from apps.channels.models import ChannelGroup

        ChannelGroup.objects.create(name="┃AT┃ KIDS")
        theirs = ChannelGroup.objects.create(name="AT | PROVIDER SPORT")
        self._stream("AT | SOME SPORT", self.a, group=theirs)
        switched_off = ChannelGroup.objects.create(name="AT | OLD PROVIDER")
        self.b.is_active = False
        self.b.save(update_fields=["is_active"])
        self._stream("AT | SOMETHING OLD", self.b, group=switched_off)

        kinds = {
            g["name"]: g["kind"]
            for g in self.client_api.get("/api/channels/channel-manager/").json()["channel_groups"]
        }
        self.assertEqual(kinds["┃AT┃ AUSTRIA"], "with_channels")
        self.assertEqual(kinds["┃AT┃ KIDS"], "empty")
        self.assertEqual(kinds["AT | PROVIDER SPORT"], "active_m3u")
        self.assertEqual(kinds["AT | OLD PROVIDER"], "inactive_m3u")

    def test_by_default_only_your_own_groups_are_asked_for(self):
        self.assertEqual(
            channel_manager.DEFAULTS["group_choices"], ["with_channels", "empty"]
        )

    def test_ignoring_through_the_page(self):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        url = "/api/channels/channel-manager/ignore/"
        self.client_api.post(url, {"action": "ignore", "key": "new:at:x", "name": "X", "kind": "new"}, format="json")
        self.assertIn("new:at:x", channel_manager.load_ignored())
        self.client_api.post(url, {"action": "unignore", "key": "new:at:x"}, format="json")
        self.assertEqual(channel_manager.load_ignored(), {})
        self.client_api.post(url, {"action": "ignore", "key": "new:at:y"}, format="json")
        self.client_api.post(url, {"action": "clear"}, format="json")
        self.assertEqual(channel_manager.load_ignored(), {})

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

    def test_the_guides_a_channel_could_be_through_the_page(self):
        source = EPGSource.objects.create(name="Austria", source_type="xmltv", priority=9)
        EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=source)
        found = self.client_api.get(
            "/api/channels/channel-manager/guides/", {"name": "┃AT┃ ORF 1"}
        ).json()["guides"]
        self.assertEqual(found[0]["name"], "ORF 1")
        # A limit that is not a number is not a 500
        self.assertEqual(
            self.client_api.get(
                "/api/channels/channel-manager/guides/", {"name": "┃AT┃ ORF 1", "limit": "lots"}
            ).status_code,
            200,
        )

    def test_reading_one_guide_s_programmes_through_the_page(self):
        source = EPGSource.objects.create(name="Austria", source_type="xmltv", priority=9)
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=source)
        url = "/api/channels/channel-manager/guides/load/"
        with patch("apps.channels.tasks.read_guide_programmes.delay") as reading:
            response = self.client_api.post(url, {"ids": [guide.id]}, format="json")
        self.assertEqual(response.json(), {"queued": True, "reading": 1})
        reading.assert_called_once()
        # Nothing to read is a refusal, not a 500
        self.assertEqual(self.client_api.post(url, {"ids": []}, format="json").status_code, 400)
        self.assertEqual(self.client_api.post(url, {"id": "x"}, format="json").status_code, 400)

    def test_a_name_and_a_guide_set_on_a_row_are_applied_through_the_page(self):
        source = EPGSource.objects.create(name="Austria", source_type="xmltv", priority=9)
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=source)
        key = f"ch:{self.orf1.id}"
        response = self.client_api.post(
            "/api/channels/channel-manager/apply/",
            {"settings": {}, "keys": [key], "names": {key: "┃AT┃ ORF Eins"}, "epgs": {key: guide.id}},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.name, "┃AT┃ ORF Eins")
        self.assertEqual(self.orf1.epg_data_id, guide.id)

    def test_the_levers_are_kept(self):
        self.client_api.put(
            "/api/channels/channel-manager/settings/", {"settings": {"order": "quality"}}, format="json"
        )
        self.assertEqual(channel_manager.load_settings()["order"], "quality")

    def test_levers_saved_under_the_first_defaults_go_back_to_the_new_ones(self):
        """
        The first defaults matched far more loosely, and opening the page saved them, so
        they would have outlived the fix. What was chosen to look at is kept.
        """
        from core.models import CoreSettings

        CoreSettings.objects.update_or_create(
            key=channel_manager.SETTINGS_KEY,
            defaults={"name": "Channel Manager", "value": {
                "match_tvg_id": True, "same_country": True, "order": "quality",
                "channel_groups": [self.austria.id],
            }},
        )
        loaded = channel_manager.load_settings()
        self.assertFalse(loaded["match_tvg_id"])
        self.assertFalse(loaded["same_country"])
        self.assertEqual(loaded["order"], "provider")
        self.assertEqual(loaded["channel_groups"], [self.austria.id])

        # Saved again, they are the person's own and are kept as they are
        channel_manager.save_settings({**loaded, "match_tvg_id": True})
        self.assertTrue(channel_manager.load_settings()["match_tvg_id"])

    def test_nothing_chosen_is_refused(self):
        response = self.client_api.post(
            "/api/channels/channel-manager/apply/", {"keys": []}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(user=User.objects.create_user(username="v", password="x", user_level=0))
        self.assertEqual(viewer.get("/api/channels/channel-manager/").status_code, 403)
