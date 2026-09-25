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
from apps.channels import channel_manager, known_channels, logo_library
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

    def test_the_country_however_it_is_written_is_there_to_be_asked_for(self):
        """
        The user's third login writes "AT| ATV FHD" where the others write "┃AT┃ ATV HD",
        and matched nothing by name. Off, the name counts as written (DispatcharrUtils).
        """
        self.assertFalse(self.same("AT| ATV FHD", "┃AT┃ ATV HD"))
        for name in ("AT| ATV FHD", "AT | ATV", "AT: ATV", "[AT] ATV", "┃AUT┃ ATV", "at|ATV", "AT - ATV"):
            self.assertTrue(self.same(name, "┃AT┃ ATV HD", country_any_way=True), name)
        self.assertTrue(self.same("US| AMC PLUS HD", "┃USA┃ AMC PLUS", country_any_way=True))
        self.assertTrue(self.same("FR| 20 MINUTES TV FHD", "┃FR┃ 20 MINUTES TV", country_any_way=True))

    def test_a_country_written_any_way_is_still_that_country(self):
        self.assertFalse(self.same("AT| ORF 1", "┃DE┃ ORF 1", country_any_way=True))
        self.assertFalse(self.same("US| AMC", "┃UK┃ AMC", country_any_way=True))

    def test_a_package_or_a_longer_box_is_not_a_country(self):
        """The shape is also how a playlist marks its own packages, and "┃CA EN┃" is not "┃CA FR┃"."""
        self.assertFalse(self.same("GO: CNN", "┃GO┃ CNN", country_any_way=True))
        self.assertFalse(self.same("PPV| CNN", "CNN", country_any_way=True))
        self.assertFalse(self.same("┃CA EN┃ FOOD NETWORK", "┃CA FR┃ FOOD NETWORK", country_any_way=True))
        # A channel whose name only starts like a country code keeps its name
        self.assertEqual(channel_manager._country_one_way("CNN: Live"), "CNN: Live")

    def test_with_loose_matching_as_well(self):
        self.assertTrue(self.same("AT| ORF1 FHD", "┃AT┃ ORF 1", country_any_way=True, name_matching="loose"))

    def test_loose_matching_keeps_east_and_west_apart(self):
        """In brackets they were taken off as decoration, and a West stream went on both feeds."""
        loose = {"name_matching": "loose", "country_any_way": True}
        self.assertFalse(self.same("US| POP [WEST]", "POP HD [EAST]", **loose))
        self.assertTrue(self.same("US| POP [WEST]", "POP HD [WEST]", **loose))

    def test_a_call_sign_is_only_one_written_as_one(self):
        calls = channel_manager.call_signs_of
        self.assertEqual(calls("ABC 10 | ALBANY | WTEN"), {"wten"})
        self.assertEqual(calls("US| ABC 02 (KATU) PORTLAND", "us"), {"katu"})
        self.assertEqual(calls("PBS 56 | CHICAGO | WYIN/WTTW"), {"wyin", "wttw"})
        self.assertEqual(calls("CBS 7 | BEND | KBNZ-LD"), {"kbnz"})
        # A subchannel is a station of its own: WLOX is ABC, WLOX-DT2 is CBS
        self.assertEqual(calls("CBS 13 | GULFPORT | WLOX-DT2"), {"wlox-2"})
        # Four letters in a name, or from a country that gives no call signs, are a word
        self.assertEqual(calls("WILD EARTH"), set())
        self.assertEqual(calls("NAT GEO | WILD"), set())
        self.assertEqual(calls("DE| WELT (WELT)", "de"), set())

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
    def test_a_provider_writing_the_country_its_own_way_is_merged_when_asked(self):
        """The stream goes on the channel you have; its name, and the channel's, stay as written."""
        theirs = self._stream("AT| ORF 1 FHD", self.b, group=ChannelGroup.objects.create(name="EU | AUSTRIA"))
        key = f"ch:{self.orf1.id}"
        self.assertEqual(self._row(channel_manager.build_plan(settings()), key)["status"], "unchanged")

        row = self._row(channel_manager.build_plan(settings(country_any_way=True)), key)
        self.assertEqual(row["status"], "merge")
        self.assertIn("AT| ORF 1 FHD", [s["name"] for s in row["streams"]])
        channel_manager.apply_plan(settings(country_any_way=True), [key])
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", theirs.name, "could not dispatch"])
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.name, "┃AT┃ ORF 1")

    def test_a_local_station_is_found_by_its_call_sign_when_asked(self):
        usa = ChannelGroup.objects.create(name="┃USA┃ ABC NETWORK")
        wten = self._channel("ABC 10 | ALBANY | WTEN", 10, usa)
        start = self._channel("START TV HD (KCBS)", 11, usa)
        cbs2 = self._channel("CBS 2 | LOS ANGELES | KCBS", 12, usa)
        theirs = self._stream("US| ABC 10 (WTEN) ALBANY", self.b, group=usa)
        kcbs = self._stream("US| CBS 02 (KCBS) LOS ANGELES HD", self.b, group=usa)

        plan = channel_manager.build_plan(settings())
        self.assertEqual(self._row(plan, f"ch:{wten.id}")["status"], "unchanged")

        plan = channel_manager.build_plan(settings(match_call_signs=True))
        self.assertIn(theirs.id, [s["id"] for s in self._row(plan, f"ch:{wten.id}")["streams"]])
        # KCBS's own channel, not what KCBS sends on a subchannel under another name
        self.assertIn(kcbs.id, [s["id"] for s in self._row(plan, f"ch:{cbs2.id}")["streams"]])
        self.assertNotIn(kcbs.id, [s["id"] for s in self._row(plan, f"ch:{start.id}")["streams"]])

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

    def test_the_order_the_page_sends_leaves_the_fallback_out(self):
        """
        The page can only move what can be moved, so the order it sends has no fallback in
        it -- and it was compared with the channel's streams fallback and all, found to be
        "other streams", and dropped. A hand order never reached a channel that ends in a
        fallback, which is every channel.
        """
        fhd = self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], {key: [fhd.id, self.existing.id]})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1 FHD", "┃AT┃ ORF 1", "could not dispatch"])

    def test_an_order_of_other_streams_than_there_are_now_is_ignored(self):
        """The streams changed since the page was looked at: the plan's own order stands."""
        fhd = self._stream("┃AT┃ ORF 1 FHD", self.b)
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], {key: [fhd.id, self.fallback.id]})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "┃AT┃ ORF 1 FHD", "could not dispatch"])


class HandAddTests(_Setup):
    """
    A stream found and put on a channel by hand, for the provider the matching did not
    find it on -- a third login writing the name its own way, say.
    """

    def setUp(self):
        super().setUp()
        self.c = M3UAccount.objects.create(name="Provider C", account_type="XC", server_url="http://c", is_active=True)
        self.key = f"ch:{self.orf1.id}"

    def test_a_stream_put_on_by_hand_goes_before_the_fallback(self):
        theirs = self._stream("AT: ORF EINS HD", self.c)
        self.assertEqual(self._row(channel_manager.build_plan(settings()), self.key)["status"], "unchanged")

        result = channel_manager.apply_plan(settings(), [self.key], adds={self.key: [theirs.id]})
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["streams_added"], 1)
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "AT: ORF EINS HD", "could not dispatch"])

    def test_an_order_given_with_it_places_it(self):
        theirs = self._stream("AT: ORF EINS HD", self.c)
        order = [theirs.id, self.existing.id, self.fallback.id]
        channel_manager.apply_plan(settings(), [self.key], {self.key: order}, adds={self.key: [theirs.id]})
        self.assertEqual(self._order(self.orf1), ["AT: ORF EINS HD", "┃AT┃ ORF 1", "could not dispatch"])

    def test_a_fallback_or_a_parked_stream_is_never_put_on(self):
        from apps.channels import stream_check

        other_fallback = Stream.objects.create(name="another fallback", url="http://local/2", is_custom=True)
        parked = self._stream("AT: ORF EINS", self.c)
        with patch.object(stream_check, "parked_ids", return_value={parked.id}):
            result = channel_manager.apply_plan(
                settings(), [self.key], adds={self.key: [other_fallback.id, parked.id]}
            )
        self.assertEqual(result["streams_added"], 0)
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "could not dispatch"])

    def test_nothing_is_put_on_a_row_that_was_not_ticked(self):
        theirs = self._stream("AT: ORF EINS HD", self.c)
        channel_manager.apply_plan(settings(), ["new:nothing"], adds={self.key: [theirs.id]})
        self.assertEqual(self._order(self.orf1), ["┃AT┃ ORF 1", "could not dispatch"])


class StreamSearchTests(_Setup):
    """Finding the stream to put on by hand: every word, anywhere, in any order."""

    def setUp(self):
        super().setUp()
        self.c = M3UAccount.objects.create(name="Provider C", account_type="XC", server_url="http://c", is_active=True)

    def names(self, *args, **kwargs):
        return [s["name"] for s in channel_manager.search_streams(*args, **kwargs)]

    def test_every_word_anywhere_in_any_order(self):
        self._stream("AT: ORF 1 HD", self.c)
        self._stream("┃AT┃ ORF 2", self.b)
        self.assertEqual(self.names("hd orf 1"), ["AT: ORF 1 HD"])
        self.assertEqual(self.names(""), [])

    def test_only_the_providers_asked_for(self):
        self._stream("AT: ORF 1 HD", self.c)
        self._stream("┃AT┃ ORF 1 HD", self.b)
        self.assertEqual(self.names("orf 1", accounts=[self.c.id]), ["AT: ORF 1 HD"])

    def test_no_fallback_no_parked_stream_and_not_what_the_channel_has(self):
        from apps.channels import stream_check

        parked = self._stream("AT: ORF 1 FHD", self.c)
        kept = self._stream("AT: ORF 1 HD", self.c)
        Stream.objects.create(name="ORF 1 could not play", url="http://local/3", is_custom=True)
        with patch.object(stream_check, "parked_ids", return_value={parked.id}):
            found = self.names("orf 1", leave_out=[self.existing.id])
        self.assertEqual(found, [kept.name])

    def test_the_channel_it_is_for_comes_first(self):
        self._stream("AT: ORF 1 SPORT", self.c)
        self._stream("┃AT┃ ORF 1 FHD", self.c)
        self.assertEqual(self.names("orf 1", name="┃AT┃ ORF 1", leave_out=[self.existing.id])[0], "┃AT┃ ORF 1 FHD")

    def test_it_says_where_else_a_stream_is_and_counts_its_provider(self):
        found = channel_manager.search_streams("orf 1")
        mine = next(s for s in found if s["id"] == self.existing.id)
        self.assertEqual([c["name"] for c in mine["channels"]], ["┃AT┃ ORF 1"])
        self.assertEqual(mine["account_id"], self.a.id)
        self.assertIsNone(mine["check"])

    def test_through_the_page(self):
        self._stream("AT: ORF 1 HD", self.c)
        client = APIClient()
        client.force_authenticate(user=User.objects.create_user(username="admin", password="x", user_level=10))
        answer = client.get(
            "/api/channels/channel-manager/streams/",
            {"q": "orf 1", "accounts": f"{self.c.id}", "limit": "nonsense"},
        ).json()
        self.assertEqual([s["name"] for s in answer["streams"]], ["AT: ORF 1 HD"])


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

    def test_a_channel_naming_no_country_never_bridges_two_that_do(self):
        """
        "Two countries named: these are different channels" -- reachable when a rule or an
        alias takes the country box out of the key, which is the only way two countries
        ever share one. A channel naming none used to join both sets, and since the lowest
        number keeps the channel it could end up the keeper of one and the victim of
        another: Austria's ORF 1 and Germany's merged into one, and both deleted.
        """
        # A rule that takes the box off, so all three share a key
        levers = settings(regex_rules=[["┃[A-Za-z]{2,3}┃", ""]])
        german = self._channel("┃DE┃ ORF 1", 500, self.germany)
        self._attach(german, [self._stream("┃DE┃ ORF 1", self.b, group=self.germany)])
        # Truly country-less: its name says none and neither does its group, since
        # country_for falls back to the group
        nowhere = ChannelGroup.objects.create(name="FAVOURITES")
        plain = self._channel("ORF 1", 700, nowhere)
        self._attach(plain, [self._stream("ORF 1", self.b, group=nowhere)])

        aliases = channel_manager._alias_map(levers)
        by_key = {}
        for record in channel_manager._existing_channels(levers, aliases).values():
            if record["key"]:
                by_key.setdefault(record["key"], []).append(record)
        shared = [k for k, recs in by_key.items() if len({r["country"] for r in recs}) > 1]
        self.assertTrue(
            shared, "the rule should put both countries and the plain one under one key"
        )
        sets = channel_manager._duplicate_sets(by_key, False)

        # No channel is in two sets: one that is can be the keeper of one and the victim
        # of another, and applying both deletes the channel that just absorbed the others
        seen = set()
        for records in sets.values():
            for record in records:
                channel_id = record["channel"].id
                self.assertNotIn(
                    channel_id, seen,
                    f"channel {channel_id} is in two sets of duplicates at once",
                )
                seen.add(channel_id)

        # ...and the country-less one is in none of them, since with two countries named
        # there is no telling which it is
        self.assertNotIn(plain.id, seen)

        plan = channel_manager.build_plan(levers)
        channel_manager.apply_plan(levers, [r["key"] for r in plan["rows"]])
        self.assertTrue(Channel.objects.filter(id=plain.id).exists())
        self.assertTrue(
            Channel.objects.filter(id=german.id).exists()
            or Channel.objects.filter(id=self.orf1.id).exists()
        )

    def test_but_two_of_one_country_are_still_combined_beside_them(self):
        # The guard must not stop the case it is there to allow
        german = self._channel("┃DE┃ ORF 1", 500, self.germany)
        self._attach(german, [self._stream("┃DE┃ ORF 1", self.b, group=self.germany)])

        plan = channel_manager.build_plan(settings())
        (row,) = [r for r in plan["rows"] if r["status"] == "combine"]
        self.assertEqual(row["channel"]["id"], self.orf1.id)
        self.assertEqual([c["id"] for c in row["combining"]], [self.twin.id])

    def test_where_channels_live_is_worked_out_once_for_the_whole_plan(self):
        """
        _NewHomes reads every channel there is, plus two more queries. It was built again
        for every row that combines, and once more for the new channels.
        """
        from unittest.mock import patch

        # Several sets to combine, all of them away from the group the combined channel
        # would go to -- so each row has to look up that group's name, which is where the
        # per-row build was
        elsewhere = ChannelGroup.objects.create(name="┃AT┃ ELSEWHERE")
        for n in range(3):
            twin = self._channel(f"┃AT┃ Sender {n}", 400 + n, self.news)
            self._attach(twin, [self._stream(f"┃AT┃ Sender {n}", self.a)])
            other = self._channel(f"┃AT┃ Sender {n}", 600 + n, self.news)
            self._attach(other, [self._stream(f"┃AT┃ Sender {n} HD", self.b)])

        # A group none of them are in, so every row has to look its name up
        levers = settings(target_group=elsewhere.id)
        with patch.object(
            channel_manager, "_NewHomes", wraps=channel_manager._NewHomes
        ) as building:
            plan = channel_manager.build_plan(levers)

        moving = [
            r for r in plan["rows"]
            if r["status"] == "combine" and "group" in r["changes"]
        ]
        self.assertGreater(
            len(moving), 1, "several rows should combine and move, or this proves nothing"
        )
        self.assertEqual(building.call_count, 1)

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


class NewNumberTests(_Setup):
    """Where a new channel's number comes from, and where it must not come from."""

    def test_it_goes_after_its_own_group(self):
        homes = channel_manager._NewHomes()
        # ORF 1 is number 1 in Austria and nothing follows it
        self.assertEqual(homes.number_in(self.austria.id), 2.0)

    def test_but_never_into_the_group_above(self):
        """
        It used to walk up through taken numbers until it found a gap, which on a lineup
        numbered without gaps walks straight into the next group: a new Austrian channel
        landing in the middle of the German block.
        """
        # Austria ends at 2; Germany runs 3 to 6 with one channel missing at 4, which is
        # the hole the old code dropped an Austrian channel into
        self._channel("┃AT┃ ORF 2", 2, self.austria)
        for number in (3, 5, 6):
            self._channel(f"┃DE┃ Das Erste {number}", number, self.germany)

        homes = channel_manager._NewHomes()
        number = homes.number_in(self.austria.id)
        self.assertGreater(number, 6, f"{number} is inside Germany's block (3 to 6)")

        # ...and a second one does not land there either
        self.assertGreater(homes.number_in(self.austria.id), 6)

    def test_and_still_fills_a_gap_where_there_is_one(self):
        self._channel("┃AT┃ ORF 2", 2, self.austria)
        for number in (10, 11):
            self._channel(f"┃DE┃ Das Erste {number}", number, self.germany)
        homes = channel_manager._NewHomes()
        # Room between Austria's last (2) and Germany's first (10)
        self.assertEqual(homes.number_in(self.austria.id), 3.0)


class LeaveAloneTests(_Setup):
    """
    Groups the Lineup is told not to touch. For the ones something else looks after -- a
    plugin's, or one arranged by hand -- where the answer to every suggestion is no.
    """

    def setUp(self):
        super().setUp()
        self.cooking = ChannelGroup.objects.create(name="Cooking")
        self.recipes = self._channel("┃AT┃ PULS 4", 80, self.cooking)
        # A stream that would otherwise be added to it
        self.its_stream = self._stream("┃AT┃ PULS 4 HD", self.a)

    def _rows(self, **overrides):
        plan = channel_manager.build_plan(settings(create_new=True, **overrides))
        return {r["key"]: r for r in plan["rows"]}

    def test_a_channel_in_one_is_not_in_the_plan_at_all(self):
        self.assertIn(f"ch:{self.recipes.id}", self._rows())
        self.assertNotIn(
            f"ch:{self.recipes.id}",
            self._rows(exclude_channel_groups=[self.cooking.id]),
        )

    def test_and_its_streams_are_suggested_as_a_new_channel_instead(self):
        # The stream is not left out with it: it belongs to no channel now, which is what
        # "new" means. What it must not do is quietly land in the group left alone.
        rows = self._rows(exclude_channel_groups=[self.cooking.id])
        new = [r for r in rows.values() if r["status"] == "new"]
        self.assertTrue(new)
        for row in new:
            self.assertNotEqual(row["channel"]["group_id"], self.cooking.id)

    def test_a_group_left_alone_is_never_a_new_channels_home(self):
        # Every Austrian channel of yours is in the group being left alone, so the country
        # would have put a new one there
        self.orf1.channel_group = self.cooking
        self.orf1.save(update_fields=["channel_group"])
        homes = channel_manager._NewHomes(leave_alone=[self.cooking.id])
        group_id, why = homes.group_for(
            {"group_id": self.cooking.id, "name": "┃AT┃ PULS 4"}, "at"
        )
        self.assertNotEqual(group_id, self.cooking.id)
        self.assertIn("left alone", why)


class NumberFromTests(_Setup):
    """
    "Numbers from" on the levers: new channels numbered from a number you give, rather
    than after the last channel of their group.
    """

    def _numbers(self, start):
        for name in ("PULS 4", "ATV", "ServusTV"):
            self._stream(f"┃AT┃ {name} HD", self.a)
        plan = channel_manager.build_plan(settings(create_new=True, number_start=start))
        return sorted(r["channel"]["number"] for r in plan["rows"] if r["status"] == "new")

    def test_the_numbers_start_where_you_said(self):
        self.assertEqual(self._numbers(200), [200.0, 201.0, 202.0])

    def test_but_never_a_number_a_channel_already_has(self):
        """
        It handed them out one after another without looking, so starting at a number the
        lineup already uses gave every new channel a number an existing channel had -- and
        two channels on one number is one channel as far as a media server is concerned.
        The sibling that numbers a channel after its group has always stepped over these.
        """
        self._channel("┃DE┃ Das Erste", 200, self.germany)
        self._channel("┃DE┃ ZDF", 201, self.germany)

        numbers = self._numbers(200)

        self.assertEqual(numbers, [202.0, 203.0, 204.0])
        taken = set(Channel.objects.values_list("channel_number", flat=True))
        self.assertEqual([n for n in numbers if n in taken - set(numbers)], [])


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
        levers = settings(create_new=True, order="quality", epg="tvg_id_then_name", profiles="all")
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
        # Into every profile when asked, as Dispatcharr's own Channels page does with "All"
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


class ProfileTests(_Setup):
    """
    Which channel profiles a channel the Lineup makes joins. Every profile, which is what
    Dispatcharr's Channels page does with "All" selected, put each new channel into every
    profile -- a kids' profile got the sports channels. By default a new channel joins the
    profiles its group is already in: more than ten of the group's channels there.
    """

    def setUp(self):
        super().setUp()
        # Made before the channels, so they do not join every profile on their way in
        self.living_room = ChannelProfile.objects.create(name="Living room")
        self.kids = ChannelProfile.objects.create(name="Kids")
        self.bedroom = ChannelProfile.objects.create(name="Bedroom")
        ChannelProfileMembership.objects.filter(channel=self.orf1).delete()
        self.group = [self._channel(f"┃AT┃ Channel {n}", 100 + n, self.austria) for n in range(12)]
        for channel in self.group:
            ChannelProfileMembership.objects.create(
                channel=channel, channel_profile=self.living_room, enabled=True
            )
        # Eleven there but switched off: a profile the group is hidden in is not one it is in
        for channel in self.group[:11]:
            ChannelProfileMembership.objects.create(
                channel=channel, channel_profile=self.bedroom, enabled=False
            )
        # Ten: not more than ten
        for channel in self.group[:10]:
            ChannelProfileMembership.objects.create(
                channel=channel, channel_profile=self.kids, enabled=True
            )

    def _make(self, **levers):
        self._stream("┃AT┃ PULS 4 HD", self.a)
        levers = settings(create_new=True, **levers)
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(levers, [row["key"]])
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        return set(
            ChannelProfileMembership.objects.filter(channel=made, enabled=True)
            .values_list("channel_profile__name", flat=True)
        )

    def test_by_default_it_joins_the_profiles_its_group_is_in(self):
        self.assertEqual(channel_manager.DEFAULTS["profiles"], "like_its_group")
        self.assertEqual(self._make(), {"Living room"})

    def test_how_many_is_a_setting(self):
        self.assertEqual(self._make(profiles_group_more_than=9), {"Living room", "Kids"})

    def test_a_group_in_no_profile_puts_it_in_none(self):
        empty = ChannelGroup.objects.create(name="┃AT┃ EMPTY")
        self._stream("┃AT┃ PULS 4 HD", self.a)
        levers = settings(create_new=True)
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(levers, [row["key"]], groups={row["key"]: empty.id})
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertFalse(ChannelProfileMembership.objects.filter(channel=made).exists())

    def test_and_the_group_it_is_moved_to_on_the_page_is_the_one_that_counts(self):
        germany = [self._channel(f"┃DE┃ Kanal {n}", 500 + n, self.germany) for n in range(11)]
        for channel in germany:
            ChannelProfileMembership.objects.create(
                channel=channel, channel_profile=self.kids, enabled=True
            )
        self._stream("┃AT┃ PULS 4 HD", self.a)
        levers = settings(create_new=True)
        (row,) = [r for r in channel_manager.build_plan(levers)["rows"] if r["status"] == "new"]
        channel_manager.apply_plan(levers, [row["key"]], groups={row["key"]: self.germany.id})
        made = Channel.objects.get(name="┃AT┃ PULS 4")
        self.assertEqual(
            set(ChannelProfileMembership.objects.filter(channel=made).values_list(
                "channel_profile__name", flat=True)),
            {"Kids"},
        )

    def test_every_profile_and_a_list_still_work_as_they_did(self):
        self.assertEqual(self._make(profiles="all"), {"Living room", "Kids", "Bedroom"})

    def test_a_list_picked_by_hand(self):
        self.assertEqual(self._make(profiles=[self.bedroom.id]), {"Bedroom"})

    def test_saved_settings_on_the_old_default_take_the_new_one(self):
        from core.models import CoreSettings

        CoreSettings.objects.update_or_create(
            key=channel_manager.SETTINGS_KEY,
            defaults={"name": "Channel Manager", "value": {
                **channel_manager.DEFAULTS, "profiles": "all", "min_streams_new": 3, "version": 4,
            }},
        )
        loaded = channel_manager.load_settings()
        self.assertEqual(loaded["profiles"], "like_its_group")
        # ...and the rest of what was saved is kept
        self.assertEqual(loaded["min_streams_new"], 3)

    def test_but_a_choice_somebody_made_is_kept(self):
        from core.models import CoreSettings

        for chosen in ("none", [self.kids.id]):
            CoreSettings.objects.update_or_create(
                key=channel_manager.SETTINGS_KEY,
                defaults={"name": "Channel Manager", "value": {
                    **channel_manager.DEFAULTS, "profiles": chosen, "version": 4,
                }},
            )
            self.assertEqual(channel_manager.load_settings()["profiles"], chosen)


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

    def judge_with(self, name, guide_name, tvg_id="", country="us", mine="", **extra):
        """The same, with what the matching has been told about these channels."""
        return channel_manager.judge_guide(
            name, country, {"name": guide_name, "tvg_id": tvg_id}, mine, **extra
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

    def test_a_short_word_with_a_letter_more_is_another_word(self):
        """
        "┃CA EN┃ AMI TV" was given the guide of WAMI-DT, a Miami call sign, at a hundred
        per cent: "ami" and "wami" are 86 % alike, which was near enough for a spelling.
        """
        for mine, theirs in (("ami", "wami"), ("abc", "wabc"), ("cnn", "cnnx"), ("fox", "foxs")):
            self.assertEqual(channel_manager._alike([mine], [theirs]), 0, (mine, theirs))
        score, _, _ = self.judge("┃CA EN┃ AMI TV", "WAMI-DT", "467056", country="ca")
        self.assertEqual(score, 0)
        # A long word's spelling still is one
        self.assertEqual(channel_manager._alike(["bravo", "discovery"], ["bravos", "discovry"]), 100)

    def test_letters_and_digits_stuck_together_are_two_words(self):
        self.assertEqual(channel_manager.guide_words("BBC1"), ["bbc", "1"])
        score, tier, _ = self.judge("┃UK┃ BBC ONE", "BBC1", "bbc1.uk", country="gb")
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))

    def test_ordinary_short_words_are_not_taken_for_broadcasters(self):
        # "Nothing like it" would otherwise offer "like" and "it" and refuse everything
        self.assertEqual(channel_manager._names_in(channel_manager.guide_words("Nothing like it")), set())
        self.assertEqual(channel_manager._names_in(channel_manager.guide_words("PBS")), {"pbs"})

    def test_how_a_stream_is_sent_is_not_which_channel_it_is(self):
        """
        A guide has one entry for a channel however it is sent, so an unmatched "HD" was
        costing a right answer a quarter of its score -- and half the names in a playlist
        carry one.
        """
        for theirs in ("CNN HD", "CNN FHD", "CNN 1080p", "CNN"):
            score, tier, _ = self.judge("┃USA┃ CNN", theirs)
            self.assertEqual((score, tier), (100, channel_manager.CERTAIN), theirs)
        self.assertEqual(channel_manager.guide_words("┃AT┃ ORF 1 FHD"), ["orf", "1"])

    def test_words_written_as_one_come_apart(self):
        self.assertEqual(channel_manager.guide_words("FoxSports1"), ["fox", "sports", "1"])
        self.assertEqual(channel_manager.guide_words("PBSKids"), ["pbs", "kids"])
        score, tier, _ = self.judge("┃USA┃ FOX SPORTS 1", "FoxSports1")
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))

    def test_the_same_letters_parted_differently_are_the_same_name(self):
        # A playlist writes DREAMWORKS and a guide writes DreamWorks: one comes apart at
        # the camel and the other cannot, and word by word they then share nothing at all
        score, tier, _ = self.judge("┃NL┃ DREAMWORKS", "DreamWorks", "dw.nl", country="nl")
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))

    def test_a_shared_call_sign_is_the_station_itself(self):
        """
        Taken from the EPG Janitor plugin, which anchors on the call sign and rejects a
        disagreement -- only the rejecting half was here. A call sign is allocated to one
        station and nothing else, so two names carrying it are that station however little
        else they share: "PBS WHYY" and "WHYY-DT" have one word of three in common.
        """
        score, tier, why = self.judge("┃USA┃ PBS WHYY", "WHYY-DT", "whyy.us")
        self.assertEqual(tier, channel_manager.CERTAIN)
        self.assertGreaterEqual(score, 90)
        self.assertIn("WHYY", why)
        # ...and written the way a guide writes it, in brackets after the network
        self.assertEqual(self.judge("┃USA┃ ABC (WABC)", "WABC")[1], channel_manager.CERTAIN)

    def test_but_two_different_call_signs_are_still_two_stations(self):
        self.assertEqual(self.judge("┃USA┃ PBS WHYY", "KQED-DT")[0], 0)

    def test_two_numbers_that_differ_are_two_channels(self):
        self.assertEqual(self.judge("┃USA┃ PBS 12", "PBS 13", "pbs13.us")[0], 0)
        self.assertEqual(self.judge("┃UK┃ SKY SPORTS 1", "Sky Sports 2")[0], 0)

    def test_but_a_number_written_as_a_word_is_that_number(self):
        score, tier, _ = self.judge("┃AT┃ ORF 1", "ORF Eins", "orfeins.at", country="at")
        self.assertEqual(score, 100)
        self.assertEqual(tier, channel_manager.CERTAIN)

    def test_a_word_is_not_a_call_sign_because_it_starts_with_a_w(self):
        """
        "┃BE┃ NGC WILD" was matched at a hundred per cent to a Slovak Nat Geo Wild,
        because "wild" was read as an American call sign and a call sign is taken as
        proof of which station a name is.
        """
        score, tier, _ = self.judge(
            "┃BE┃ NGC WILD", "NGC Wild HD", "NGC.Wild.HD.sk", country="be"
        )
        self.assertEqual(tier, channel_manager.GUESS)
        self.assertLess(score, 80)

    def test_and_a_call_sign_is_only_one_where_they_are_allocated(self):
        # Four letters beginning with W on a German channel are four letters
        self.assertEqual(
            channel_manager._identity_of(["wdr", "wett"], "de")["call"], ""
        )
        self.assertEqual(
            channel_manager._identity_of(["wnet"], "us")["call"], "wnet"
        )

    def test_the_country_counts_even_when_a_call_sign_anchors_the_match(self):
        """
        It used to be asked only about the tier, so a match anchored on a call sign came
        out at a hundred per cent with the country flatly disagreeing.
        """
        score, tier, why = self.judge("┃USA┃ PBS WNET", "WNET", "WNET.ca")
        self.assertEqual(tier, channel_manager.GUESS)
        self.assertLessEqual(score, 70)
        self.assertIn("CA", why)

    def test_a_country_that_disagrees_says_which_two(self):
        """
        It used to say only "that guide is US's", which tells you nothing about why that
        is a disagreement. Without what the channel itself says there is no telling a
        channel marked wrong from a guide from the wrong place, and those two want
        opposite things doing about them.
        """
        _, tier, why = self.judge("┃CA┃ PBS Detroit", "PBS Detroit", "WTVS.us",
                                  country="ca", mine="WTVS.us")
        self.assertEqual(tier, channel_manager.LIKELY)
        self.assertIn("this channel says CA", why)
        self.assertIn("the guide is for US", why)

        # ...and where they agree it says nothing about countries at all
        self.assertEqual(
            self.judge("┃USA┃ PBS", "PBS", "PBS.us", country="usa", mine="PBS.us")[2],
            "its tvg-id and its name",
        )

    def test_a_country_written_long_is_the_same_country(self):
        # A playlist's box says "USA" or "GER" and a tvg-id says ".us" or ".de"
        for box, tvg in (("usa", "us"), ("ger", "de"), ("uk", "gb"), ("ned", "nl")):
            self.assertEqual(
                channel_manager._by_country(box, {"tvg_id": f"channel.{tvg}"}),
                channel_manager.SAME_COUNTRY,
                box,
            )

    def test_a_network_written_short_is_written_out(self):
        """
        "NGC WILD" and "Nat Geo Wild" share one word of three, and no amount of comparing
        letters will ever join "ngc" to "nat geo". So the short form is written out on
        both sides before anything is compared.
        """
        self.assertEqual(
            channel_manager.guide_words("┃BE┃ NGC WILD"),
            ["national", "geographic", "wild"],
        )
        score, tier, _ = self.judge(
            "┃BE┃ NGC WILD", "Nat Geo Wild", "NatGeoWild.be", country="be"
        )
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))

    def test_and_written_out_after_the_letters_and_digits_are_parted(self):
        # "FS1" is "fs 1" by the time the table sees it, and "NatGeo" is "nat geo"
        self.assertEqual(channel_manager.guide_words("FS1"), ["fox", "sports", "1"])
        self.assertEqual(
            channel_manager.guide_words("NatGeo"), ["national", "geographic"]
        )

    def test_but_one_of_a_family_is_not_the_network_itself(self):
        """
        Writing a short form out makes two names alike that were not: once "NGC WILD" is
        "national geographic wild", all that parts it from plain National Geographic is
        one word, and it scored ninety.
        """
        score, tier, _ = self.judge(
            "┃BE┃ NGC WILD", "National Geographic", "NatGeo.be", country="be"
        )
        self.assertEqual(tier, channel_manager.GUESS)
        # ...while the one that says the same family is the certainty
        self.assertEqual(
            self.judge(
                "┃USA┃ DISCOVERY SCIENCE", "Discovery Science", "discsci.us"
            )[1],
            channel_manager.CERTAIN,
        )
        self.assertEqual(
            self.judge("┃USA┃ DISCOVERY SCIENCE", "Discovery", "disc.us")[1],
            channel_manager.GUESS,
        )

    def test_a_tvg_id_filter_is_plain_text_until_it_is_a_pattern(self):
        like = channel_manager._id_is_like
        self.assertTrue(like("BBCOne.uk", ".uk"))
        self.assertFalse(like("ORF1.at", ".uk"))
        # ...and a * or a ? plainly means a pattern
        self.assertTrue(like("SkySportsMainEvent.uk", "sky*.uk"))
        self.assertFalse(like("BBCOne.uk", "sky*.uk"))
        # nothing typed is everything
        self.assertTrue(like("anything.at", ""))

    def test_which_guides_are_matched_against(self):
        rows = [
            {"id": 1, "name": "BBC One", "tvg_id": "BBCOne.uk", "epg_source_id": 7},
            {"id": 2, "name": "BBC One", "tvg_id": "BBCOne.de", "epg_source_id": 8},
        ]
        # a source left out is not matched against at all
        self.assertEqual(
            [r["id"] for r in channel_manager.guides_in_play(rows, {"sources": [7]})], [1]
        )
        # ...and so is a tvg-id that is not what was asked for
        self.assertEqual(
            [r["id"] for r in channel_manager.guides_in_play(rows, {"tvg_id_like": ".de"})],
            [2],
        )
        # ...and a country that disagrees, when that is asked for
        self.assertEqual(
            [
                r["id"]
                for r in channel_manager.guides_in_play(
                    rows, {"country_must_agree": True}, country="uk"
                )
            ],
            [1],
        )
        # with none of them set, every guide is in play and the list is not even copied
        self.assertIs(channel_manager.guides_in_play(rows, {}), rows)

    def test_a_package_in_front_is_not_a_country(self):
        """
        "GO: CNN" is a package, not Gabon. Two letters and a colon were read as a country
        whatever the letters were, and the wrong country then cost the right guide thirty
        points -- the same invisible penalty as ┃USA┃ against .us before v146.
        """
        self.assertEqual(logo_library.country_of("GO: CNN"), "")
        self.assertEqual(logo_library.country_of("SK: CNN"), "sk")  # Slovakia exists
        self.assertEqual(logo_library.country_of("US: CNN"), "us")
        # A box is taken at its word, whatever is in it
        self.assertEqual(logo_library.country_of("┃EX┃ CNN"), "ex")

    def test_a_playlists_own_package_is_not_part_of_the_name(self):
        for name in ("SLING: CNN", "GO: CNN", "PRIME: CNN", "US: SLING: CNN", "NOW | CNN"):
            self.assertEqual(channel_manager.guide_words(name), ["cnn"], name)
        # ...but a word of the name is left alone where there is no colon
        self.assertEqual(channel_manager.guide_words("Sky News"), ["sky", "news"])

    def test_a_superscript_quality_tag_leaves_nothing_behind(self):
        # "ᶠᴴᴰ" unpacks to "fHD" after the name was lowered, and the sweep that keeps only
        # lower-case letters ate the H and the D and left a stray "f" costing the score
        self.assertEqual(channel_manager.guide_words("CNN ᶠᴴᴰ"), ["cnn"])
        self.assertEqual(channel_manager.guide_words("ᴴᴰ CNN"), ["cnn"])

    def test_an_hour_later_is_not_the_same_channel(self):
        # The same programmes an hour later: a guide for one is wrong for the other by
        # exactly an hour
        score, tier, why = self.judge("ITV2 +1", "ITV2 +2", "itv2plus2.uk", country="gb")
        self.assertEqual(score, 0)
        self.assertIn("time shift", why)
        # ...and one that says it against one that does not is never a certainty
        self.assertEqual(self.judge("ITV2 +1", "ITV2", "itv2.uk", country="gb")[1],
                         channel_manager.GUESS)

    def test_a_radio_frequency_is_not_a_channel_number(self):
        # "CNN 101.5 FM" is one station, not channel 101
        self.assertEqual(
            channel_manager._identity_of(channel_manager.guide_words("CNN 101.5 FM"))["number"],
            "",
        )
        # ...while a channel number still is one
        self.assertEqual(
            channel_manager._identity_of(channel_manager.guide_words("PBS 12"))["number"],
            "12",
        )

    def test_a_channel_on_a_loop_has_no_guide_anywhere(self):
        self.assertTrue(channel_manager.round_the_clock("24/7: The Office"))
        self.assertTrue(channel_manager.round_the_clock("┃US┃ 24/7 Friends"))
        self.assertFalse(channel_manager.round_the_clock("CNN"))

    def test_a_call_sign_is_one_your_own_guides_carry(self):
        """
        Shipping the FCC's list would be a large download that goes stale and is right
        about stations nobody here has. A word is a call sign when a guide in this install
        carries it as one, which is the same answer narrowed to what anybody could watch.
        """
        catalogue = [
            {"name": "WHYY-DT", "tvg_id": "WHYY.us"},
            {"name": "KQED TV", "tvg_id": "kqed.us"},
            # Four letters beginning with W, and no station anywhere says so
            {"name": "NGC Wild HD", "tvg_id": "NGC.Wild.HD.sk"},
        ]
        found = known_channels.call_signs_in(catalogue)
        self.assertEqual(found, {"whyy", "kqed"})

        # ...so "WILD" is a word, and the Slovak guide is no longer a certainty
        score, tier, _ = self.judge_with(
            "┃BE┃ NGC WILD", "NGC Wild HD", "NGC.Wild.HD.sk", country="be", known_calls=found
        )
        self.assertEqual(tier, channel_manager.GUESS)
        # ...while a real one still anchors
        self.assertEqual(
            self.judge_with("┃USA┃ PBS WHYY", "WHYY-DT", "whyy.us", known_calls=found)[1],
            channel_manager.CERTAIN,
        )

    def test_a_name_two_channels_both_go_by_says_nothing(self):
        # Pointing it at whichever entry was read last would be worse than not knowing
        from unittest.mock import patch

        rows = [
            {"id": "SportsOne.us", "name": "Sports", "alt_names": [], "country": "US"},
            {"id": "SportsTwo.uk", "name": "Sports", "alt_names": [], "country": "GB"},
            {"id": "NatGeoWild.us", "name": "Nat Geo Wild", "alt_names": ["NGC Wild"], "country": "US"},
        ]

        class _Cache:
            def __init__(self):
                self.held = {}

            def set(self, key, value, _ttl):
                self.held[key] = value

            def get(self, key):
                return self.held.get(key)

        cache = _Cache()
        with patch("apps.channels.logo_library._get_json", return_value=rows):
            built = known_channels.build_known(cache)
        self.assertEqual(built["dropped"], 1)
        reference = known_channels.known(cache)
        self.assertIsNone(known_channels.which_channel("Sports", reference))
        # ...while a name that belongs to one channel still points at it, under each of
        # the names it goes by
        self.assertEqual(
            known_channels.which_channel("NGC Wild", reference)["id"], "NatGeoWild.us"
        )
        self.assertEqual(
            known_channels.which_channel("Nat Geo Wild", reference)["id"], "NatGeoWild.us"
        )

    def test_what_a_channel_is_called_elsewhere_settles_it(self):
        """
        Two names that are one channel in the reference are one channel however little
        they read alike -- which is the table of abbreviations, written by somebody else
        and kept up to date by somebody else.
        """
        def entry(name, channel_id):
            return {
                logo_library.match_key(name): {
                    "id": channel_id, "name": name, "country": "us",
                    "network": "", "closed": False,
                }
            }

        reference = {
            **entry("NGC Wild", "NatGeoWild.us"),
            **entry("Nat Geo Wild", "NatGeoWild.us"),
            **entry("National Geographic", "NatGeo.us"),
        }
        score, tier, why = self.judge_with(
            "┃US┃ NGC Wild", "Nat Geo Wild", "NatGeoWild.us", reference=reference
        )
        self.assertEqual((score, tier), (100, channel_manager.CERTAIN))
        self.assertIn("both names are", why)

        # ...and two that are different channels are different however much they do
        score, tier, why = self.judge_with(
            "┃US┃ NGC Wild", "National Geographic", "NatGeo.us", reference=reference
        )
        self.assertEqual((score, tier), (0, channel_manager.GUESS))
        self.assertIn("is not", why)

    def test_but_a_reference_never_overrules_what_the_names_say(self):
        # A reference that puts a channel and its +1 under one entry would otherwise hand
        # the one guide to both. A rule that a download can overrule is not a rule.
        reference = {
            logo_library.match_key(name): {
                "id": "ITV2.uk", "name": "ITV2", "country": "gb", "network": "", "closed": False
            }
            for name in ("ITV2", "ITV2 +1")
        }
        self.assertEqual(
            self.judge_with("ITV2 +1", "ITV2", "itv2.uk", country="gb", reference=reference)[1],
            channel_manager.GUESS,
        )

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

    def test_the_guide_from_the_channels_own_country_is_preferred(self):
        """
        match_key takes the country box off, so "┃AT┃ ORF 1" and a British "ORF 1" are one
        key. A plain lookup handed the Austrian channel whichever the higher-priority
        source carried -- for every channel at once, which is what the plan runs over.
        """
        # The bigger source is ranked first and carries a British entry of the same name
        british = EPGData.objects.create(tvg_id="ORF1.uk", name="ORF 1", epg_source=self.big)
        austrian = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.big)
        self.assertLess(british.id, austrian.id, "the wrong one is seen first")

        (row,) = [
            r for r in channel_manager.build_plan(settings(epg="tvg_id_then_name"))["rows"]
            if r["key"] == f"ch:{self.orf1.id}"
        ]
        self.assertEqual(row["channel"]["epg"]["id"], austrian.id)

    def test_but_another_country_is_still_better_than_none(self):
        # Nothing is lost: where the channel's own country has no entry, the plain lookup
        # still answers
        british = EPGData.objects.create(tvg_id="ORF1.uk", name="ORF 1", epg_source=self.big)
        (row,) = [
            r for r in channel_manager.build_plan(settings(epg="tvg_id_then_name"))["rows"]
            if r["key"] == f"ch:{self.orf1.id}"
        ]
        self.assertEqual(row["channel"]["epg"]["id"], british.id)

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

    def test_and_records_of_guides_that_are_gone_are_dropped(self):
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        # Enough to be worth the sweep, and all but one of them imaginary
        channel_manager.note_read({n: 0 for n in range(9000, 9000 + channel_manager.READS_KEPT)})
        channel_manager.note_read({guide.id: 5})
        kept = channel_manager.reads()
        self.assertIn(str(guide.id), kept)
        self.assertNotIn("9001", kept)

    def test_a_source_being_refreshed_is_waited_for_not_dropped(self):
        """
        The file is rewritten by a refresh, so its guides cannot be read while one is
        going. They used to be dropped with a line in the log: the button said it had
        read them and nothing had happened to any of them, which from the outside is
        exactly what a refresh looks like.
        """
        from apps.channels.tasks import read_guide_programmes

        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        with patch("core.utils.is_task_lock_held", return_value=True), patch(
            "apps.channels.tasks.read_guide_programmes.apply_async"
        ) as again:
            read_guide_programmes({str(self.local.id): [guide.id]})
        self.assertTrue(again.called, "it should try again rather than give up")
        self.assertEqual(again.call_args.kwargs["kwargs"], {"tries": 1})

    def test_and_said_to_be_unreadable_once_it_has_waited_long_enough(self):
        from apps.channels import tasks as channel_tasks

        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        with patch("core.utils.is_task_lock_held", return_value=True), patch(
            "apps.channels.tasks.read_guide_programmes.apply_async"
        ) as again:
            channel_tasks.read_guide_programmes(
                {str(self.local.id): [guide.id]}, tries=channel_tasks.READ_TRIES
            )
        self.assertFalse(again.called)
        # ...and written down as unread with the reason, not as a guide holding nothing
        said = channel_manager.reads()[str(guide.id)]
        self.assertEqual(said["found"], 0)
        self.assertIn("refreshed", said["why"])

    def test_a_source_with_no_file_yet_is_handed_over_not_given_up_on(self):
        # Dispatcharr's own task fetches the file when it is missing; ours used to give
        # up quietly and leave the guides unread with nobody told
        from apps.channels.tasks import read_guide_programmes

        empty = EPGSource.objects.create(
            name="never downloaded", source_type="xmltv", file_path="/nowhere/at/all.xml"
        )
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=empty)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay") as asked:
            read_guide_programmes({str(empty.id): [guide.id]})
        asked.assert_called_once_with(guide.id, force=True)

    def test_what_a_read_found_is_written_down_including_nothing(self):
        # A guide listed in a source's channels with no programme of its own in it is
        # common, and looks exactly like a read that failed
        guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.local)
        channel_manager.note_read({guide.id: 0})
        said = channel_manager.reads()[str(guide.id)]
        self.assertEqual((said["found"], said["why"]), (0, ""))
        self.assertTrue(said["at"])
        # ...and the picker says so rather than offering it to be read again
        (entry,) = channel_manager._what_they_carry(
            [{"id": guide.id, "name": "ORF 1", "tvg_id": "ORF1.at"}]
        )
        self.assertEqual(entry["read"]["found"], 0)

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
        """
        What the field holds, which is 512 -- this asked for 255, a number picked out of
        the air, so a name between the two was cut in half and the test said that was
        right. Taken from the model now, as stock's own bulk_create does.
        """
        from apps.channels.models import Channel

        longest = Channel._meta.get_field("name").max_length
        key = f"ch:{self.orf1.id}"
        channel_manager.apply_plan(settings(), [key], names={key: "N" * 400})
        self.orf1.refresh_from_db()
        self.assertEqual(len(self.orf1.name), 400, "400 fits in 512 and must not be cut")

        channel_manager.apply_plan(settings(), [key], names={key: "N" * (longest + 50)})
        self.orf1.refresh_from_db()
        self.assertEqual(len(self.orf1.name), longest)

    def test_a_channel_gone_since_the_page_was_looked_at_is_passed_over(self):
        """
        Not an error that takes every other row down with it: the whole apply is one
        transaction, so one channel deleted elsewhere used to undo the lot.
        """
        from apps.channels.models import Channel

        gone = Channel.objects.create(
            name="┃AT┃ GOING", channel_number=77, channel_group=self.austria
        )
        self._attach(gone, [self.existing])
        plan_key = f"ch:{gone.id}"
        keys = [plan_key, f"ch:{self.orf1.id}"]
        Channel.objects.filter(id=gone.id).delete()

        answer = channel_manager.apply_plan(
            settings(), keys, names={f"ch:{self.orf1.id}": "┃AT┃ STILL HERE"}
        )
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.name, "┃AT┃ STILL HERE", "the other row still applied")
        self.assertIsInstance(answer, dict)

    def test_what_was_added_is_counted_not_what_was_meant_to_be(self):
        # Streams taken off the row by hand are not added, and were counted as though
        # they had been
        from_b = self._stream("┃AT┃ ORF 1", self.b)
        key = f"ch:{self.orf1.id}"
        row = self._row(channel_manager.build_plan(settings()), key)
        adding = [s["id"] for s in row["streams"] if s.get("added")]
        self.assertEqual(adding, [from_b.id], "the plan wants to add the other provider's")

        answer = channel_manager.apply_plan(settings(), [key], drops={key: adding})
        self.assertEqual(answer["streams_added"], 0, "it was dropped, so nothing was added")
        self.assertNotIn(
            from_b.id,
            ChannelStream.objects.filter(channel=self.orf1).values_list("stream_id", flat=True),
        )

        # ...and when it is not dropped, it is added and counted
        answer = channel_manager.apply_plan(settings(), [key])
        self.assertEqual(answer["streams_added"], 1)

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

        from apps.channels.models import ChannelGroupM3UAccount

        ChannelGroup.objects.create(name="┃AT┃ KIDS")
        theirs = ChannelGroup.objects.create(name="AT | PROVIDER SPORT")
        self._stream("AT | SOME SPORT", self.a, group=theirs)
        switched_off = ChannelGroup.objects.create(name="AT | OLD PROVIDER")
        self.b.is_active = False
        self.b.save(update_fields=["is_active"])
        self._stream("AT | SOMETHING OLD", self.b, group=switched_off)

        # A provider lists hundreds of groups and has streams in a handful of them at any
        # moment. The empty ones are still the provider's, and what says so is that the
        # account is linked to them -- counting streams called them all "empty" and
        # offered every one.
        empty_of_theirs = ChannelGroup.objects.create(name="AF | AFRICA")
        ChannelGroupM3UAccount.objects.create(channel_group=empty_of_theirs, m3u_account=self.a)
        empty_of_an_old_one = ChannelGroup.objects.create(name="AF | OLD AFRICA")
        ChannelGroupM3UAccount.objects.create(channel_group=empty_of_an_old_one, m3u_account=self.b)

        kinds = {
            g["name"]: g["kind"]
            for g in self.client_api.get("/api/channels/channel-manager/").json()["channel_groups"]
        }
        self.assertEqual(kinds["┃AT┃ AUSTRIA"], "with_channels")
        self.assertEqual(kinds["┃AT┃ KIDS"], "empty")
        self.assertEqual(kinds["AT | PROVIDER SPORT"], "active_m3u")
        self.assertEqual(kinds["AT | OLD PROVIDER"], "inactive_m3u")
        self.assertEqual(kinds["AF | AFRICA"], "active_m3u")
        self.assertEqual(kinds["AF | OLD AFRICA"], "inactive_m3u")

    def test_a_group_with_nothing_in_it_and_no_provider_on_it_is_yours(self):
        ChannelGroup.objects.create(name="┃AT┃ KIDS")
        kinds = {
            g["name"]: g["kind"]
            for g in self.client_api.get("/api/channels/channel-manager/").json()["channel_groups"]
        }
        self.assertEqual(kinds["┃AT┃ KIDS"], "empty")

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

    def test_a_search_finds_every_guide_that_matches_not_a_dozen(self):
        """
        The window's search was the matcher's shortlist with a box on it, cut at twelve:
        "pbs" found a dozen where Dispatcharr's own guide list found hundreds, and the one
        being looked for was often not among them.
        """
        source = EPGSource.objects.create(name="PBS", source_type="xmltv", priority=9)
        for n in range(140):
            EPGData.objects.create(tvg_id=f"pbs{n}.us", name=f"PBS Station {n}", epg_source=source)
        EPGData.objects.create(tvg_id="cbs.us", name="CBS Chicago", epg_source=source)
        url = "/api/channels/channel-manager/guides/"

        first = self.client_api.get(url, {"name": "┃USA┃ PBS", "q": "pbs"}).json()
        self.assertEqual(first["total"], 140)
        # A page at a time: thousands of cards at once would lock the browser up
        self.assertEqual(len(first["guides"]), 100)

        everything = self.client_api.get(
            url, {"name": "┃USA┃ PBS", "q": "pbs", "limit": 200}
        ).json()
        self.assertEqual(len(everything["guides"]), 140)
        self.assertEqual(
            {one["name"] for one in everything["guides"]},
            {f"PBS Station {n}" for n in range(140)},
        )

        # The guide the channel has stays first, and is not counted as one of the matches
        held = EPGData.objects.get(name="CBS Chicago")
        kept = self.client_api.get(
            url, {"name": "┃USA┃ PBS", "q": "pbs", "limit": 5, "current": held.id}
        ).json()
        self.assertEqual(kept["guides"][0]["id"], held.id)
        self.assertEqual((len(kept["guides"]), kept["total"]), (6, 140))

        # Nonsense for a limit is the first page, not a 500
        self.assertEqual(
            len(self.client_api.get(url, {"q": "pbs", "limit": "lots"}).json()["guides"]), 100
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
