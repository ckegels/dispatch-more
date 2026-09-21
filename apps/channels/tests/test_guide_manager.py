"""Which guide each channel should be on, and where that is wrong (apps.channels.guide_manager).

The three things worth suggesting are three different problems: a channel on no guide, a
channel on a guide that holds nothing, and a channel on a guide something else matches
better. The last is the one to be careful with -- replacing a working guide because
something scores a nose higher is how a good setup gets churned for nothing.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import channel_manager, guide_manager
from apps.channels.epg_matching import build_epg_matching_catalog
from apps.channels.models import Channel, ChannelGroup
from apps.epg.models import EPGData, EPGSource, ProgramData


def settings(**overrides):
    return {**guide_manager.DEFAULTS, **overrides}


class FakeRedis:
    """Enough Redis for a run to say how it is going, without one running."""

    def __init__(self):
        self.values = {}
        self.hashes = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        held = self.hashes.setdefault(key, {})
        held.update({k: str(v) for k, v in (mapping or {}).items()})
        if field is not None:
            held[field] = str(value)

    def hincrby(self, key, field, by=1):
        held = self.hashes.setdefault(key, {})
        held[field] = str(int(held.get(field, 0)) + by)

    def set(self, key, value, ex=None):
        self.values[key] = value

    def exists(self, key):
        return key in self.values

    def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)

    def expire(self, key, seconds):
        return True


class _Setup(TestCase):
    def setUp(self):
        self.austria = ChannelGroup.objects.create(name="┃AT┃ AUSTRIA")
        self.holland = ChannelGroup.objects.create(name="┃NL┃ HOLLAND")
        self.source = EPGSource.objects.create(name="xmltv.at", source_type="xmltv", priority=5)

    def _channel(self, name, number, group=None, epg=None):
        return Channel.objects.create(
            name=name, channel_number=number, channel_group=group or self.austria, epg_data=epg
        )

    def _guide(self, tvg_id, name, programmes=0):
        guide = EPGData.objects.create(tvg_id=tvg_id, name=name, epg_source=self.source)
        moment = timezone.now()
        for n in range(programmes):
            ProgramData.objects.create(
                epg=guide, title=f"Programme {n}",
                start_time=moment + timedelta(hours=n), end_time=moment + timedelta(hours=n + 1),
            )
        return guide

    def _suggested(self, levers=None, channels=None):
        """Only the rows with something to suggest: a run keeps one for every channel."""
        return {k: v for k, v in self._look(levers, channels).items() if v.get("why")}

    def _look(self, levers=None, channels=None):
        catalogue, _ = build_epg_matching_catalog()
        counts = guide_manager.programme_counts([row["id"] for row in catalogue])
        sources = dict(EPGSource.objects.values_list("id", "name"))
        return guide_manager.look_at(
            channels if channels is not None else list(guide_manager.channels_in_scope(levers or settings())),
            levers or settings(), catalogue, sources, counts,
        )


class SuggestionTests(_Setup):
    def test_a_channel_on_no_guide_is_offered_one(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        found = self._look()
        self.assertEqual(found[str(channel.id)]["epg"], guide.id)
        self.assertEqual(found[str(channel.id)]["why"], "none")
        # and can be watched from the page, since a guide can be right and the channel
        # behind it something else entirely
        self.assertEqual(found[str(channel.id)]["uuid"], str(channel.uuid))

    def test_what_is_on_the_guide_it_is_on_now_is_said_too(self):
        # On a guide that is a worse match, and carrying somebody else's evening
        on_it = self._guide("orfeins.old", "ORF Eins")
        moment = timezone.now()
        ProgramData.objects.create(
            epg=on_it, title="Somebody else's evening",
            start_time=moment - timedelta(minutes=5), end_time=moment + timedelta(minutes=25),
        )
        self._guide("ORF1.at", "ORF 1", programmes=9)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=on_it)
        found = self._look()
        self.assertEqual(found[str(channel.id)]["instead_of_now"], "Somebody else's evening")
        self.assertEqual(found[str(channel.id)]["instead_of_source"], "xmltv.at")

    def test_a_guide_holding_nothing_is_swapped_for_one_that_holds_something(self):
        empty = self._guide("orf1.old", "ORF 1")
        full = self._guide("ORF1.at", "ORF 1", programmes=5)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=empty)
        found = self._look()
        self.assertEqual(found[str(channel.id)]["epg"], full.id)
        self.assertEqual(found[str(channel.id)]["why"], "empty")
        self.assertEqual(found[str(channel.id)]["instead_of_holds"], 0)

    def test_but_not_for_another_a_channel_uses_that_holds_nothing_either(self):
        empty = self._guide("orf1.old", "ORF 1")
        also_empty = self._guide("orf1.other", "ORF 1")
        # Used by something, so it holds nothing because it is empty, not unread
        self._channel("┃AT┃ ORF 1 HD", 2, epg=also_empty)
        self._channel("┃AT┃ ORF 1", 1, epg=empty)
        found = self._look()
        self.assertNotIn("1", {str(one["epg"]) for one in found.values()})
        self.assertEqual([one["epg"] for one in found.values() if one["why"] == "empty"], [])

    def test_a_guide_nobody_uses_is_unread_rather_than_empty(self):
        # Dispatcharr reads a guide's programmes when it goes on a channel and not before,
        # so one nothing uses holds nothing whatever it is really like. Passing it over
        # would be calling a good guide no good.
        never_read = self._guide("ORF1.at", "ORF 1")
        channel = self._channel("┃AT┃ ORF 1", 1)
        found = self._look()
        self.assertEqual(found[str(channel.id)]["epg"], never_read.id)
        self.assertFalse(found[str(channel.id)]["in_use"])
        self.assertEqual(found[str(channel.id)]["programmes"], 0)

    def test_but_one_that_holds_programmes_is_preferred_to_one_not_read(self):
        self._guide("orf1.unread", "ORF 1")
        full = self._guide("ORF1.at", "ORF 1", programmes=5)
        channel = self._channel("┃AT┃ ORF 1", 1)
        self.assertEqual(self._look()[str(channel.id)]["epg"], full.id)

    def test_a_guide_from_the_wrong_country_is_bettered_by_the_right_one(self):
        wrong = self._guide("dreamworks.uk", "DreamWorks", programmes=4)
        right = self._guide("dreamworks.nl", "DreamWorks", programmes=4)
        channel = self._channel("┃NL┃ DREAMWORKS", 20, group=self.holland, epg=wrong)
        found = self._look()
        self.assertEqual(found[str(channel.id)]["epg"], right.id)
        self.assertEqual(found[str(channel.id)]["why"], "better")

    def test_a_guide_that_works_is_left_alone_when_the_difference_is_a_nose(self):
        on_it = self._guide("ORF1.at", "ORF 1", programmes=4)
        self._guide("ORF1b.at", "ORF 1 Austria", programmes=4)
        self._channel("┃AT┃ ORF 1", 1, epg=on_it)
        self.assertEqual(self._suggested(), {})

    def test_nothing_is_suggested_that_is_not_good_enough(self):
        self._guide("x.at", "Something else entirely", programmes=4)
        self._channel("┃AT┃ ORF 1", 1)
        self.assertEqual(self._suggested(), {})

    def test_each_kind_can_be_turned_off_on_its_own(self):
        self._guide("ORF1.at", "ORF 1", programmes=3)
        self._channel("┃AT┃ ORF 1", 1)
        self.assertEqual(self._suggested(settings(suggest_none=False)), {})

    def test_a_guess_is_never_suggested_however_the_letters_read(self):
        # The station nobody would pick: a different number, which on letters alone
        # scores in the eighties
        self._guide("pbs13.us", "PBS 13", programmes=9)
        self._channel("┃USA┃ PBS 12", 12)
        self.assertEqual(self._suggested(), {})

    def test_but_the_channel_is_still_on_the_list_to_settle_by_hand(self):
        # Nothing to suggest is not nothing to know: a channel no guide fits is exactly
        # the one somebody goes looking for
        self._guide("pbs13.us", "PBS 13", programmes=9)
        channel = self._channel("┃USA┃ PBS 12", 12)
        self.assertIn(str(channel.id), self._look())
        self.assertEqual(self._look()[str(channel.id)]["why"], "")

    def test_only_the_groups_chosen_are_looked_at(self):
        self._guide("ORF1.at", "ORF 1", programmes=3)
        self._guide("npo1.nl", "NPO 1", programmes=3)
        self._channel("┃AT┃ ORF 1", 1)
        dutch = self._channel("┃NL┃ NPO 1", 50, group=self.holland)
        found = self._look(settings(channel_groups=[self.holland.id]))
        self.assertEqual(list(found), [str(dutch.id)])

    def test_a_suggestion_waved_away_is_not_made_again(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.ignore(channel.id, channel.name, guide.id)
        self.assertEqual(self._suggested(), {})

    def test_but_a_different_guide_later_is_offered_all_the_same(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.ignore(channel.id, channel.name, guide.id)
        better = self._guide("ORF1.at.new", "ORF 1", programmes=99)
        found = self._look()
        # Whichever wins, it is not the one waved away that is offered again
        self.assertIn(str(channel.id), found)
        self.assertIn(found[str(channel.id)]["epg"], [better.id])


class ChosenTests(_Setup):
    """A channel whose guide is settled is not asked about again."""

    def test_a_settled_channel_has_nothing_suggested_for_it(self):
        self._guide("ORF1.at", "ORF 1", programmes=99)
        # Settled on no guide at all, which is a decision like any other
        channel = self._channel("┃AT┃ ORF 1", 1)
        self.assertTrue(self._suggested())
        guide_manager.choose(channel.id, "", None)
        self.assertEqual(self._suggested(), {})

    def test_but_it_still_gets_a_row_of_its_own(self):
        # Settled means nothing is put forward, not that the channel disappears: what
        # would have been suggested is exactly what somebody wants to look at
        better = self._guide("ORF1.at", "ORF 1", programmes=99)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.choose(channel.id, "", None)
        row = self._look()[str(channel.id)]
        self.assertTrue(row["chosen"])
        self.assertEqual(row["why"], "")
        self.assertEqual(row["epg"], better.id)

    def test_a_guide_changed_underneath_unsettles_it(self):
        # What was settled was that guide. On a different one, the decision is no longer
        # about what is there, so the channel is looked at like any other.
        on_it = self._guide("orfeins.old", "ORF Eins", programmes=1)
        self._guide("ORF1.at", "ORF 1", programmes=99)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=on_it)
        guide_manager.choose(channel.id, on_it.name, on_it.id)
        self.assertEqual(self._suggested(), {})
        channel.epg_data = None
        channel.save(update_fields=["epg_data"])
        self.assertTrue(self._suggested())

    def test_applying_a_guide_settles_the_channel(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay"):
            guide_manager.apply({channel.id: guide.id})
        self.assertEqual(guide_manager.load_chosen()[str(channel.id)]["epg"], guide.id)
        self.assertEqual(self._suggested(), {})

    def test_unsettling_one_asks_about_it_again(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay"):
            guide_manager.apply({channel.id: guide.id})
        guide_manager.unchoose(channel.id)
        self.assertEqual(guide_manager.load_chosen(), {})

    def test_unsettling_them_all_asks_about_them_all_again(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        one = self._channel("┃AT┃ ORF 1", 1)
        two = self._channel("┃AT┃ ORF 2", 2)
        guide_manager.choose(one.id, guide.name, guide.id)
        guide_manager.choose(two.id, guide.name, guide.id)
        self.assertEqual(guide_manager.unchoose(), 0)
        self.assertEqual(guide_manager.load_chosen(), {})

    def test_the_page_says_which_rows_were_waved_away(self):
        # Waving a suggestion away is not settling a channel, and there was no way to
        # look at what had been waved away -- only a count, which is a number you cannot
        # undo one row of
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.ignore(channel.id, channel.name, guide.id)
        rows = [{"channel": channel.id}]
        guide_manager.mark_waved_away(rows)
        self.assertTrue(rows[0]["waved_away"])
        self.assertEqual(rows[0]["waved_away_guide"], channel.name)

    def test_the_page_says_which_rows_are_settled(self):
        on_it = self._guide("orfeins.old", "ORF Eins", programmes=1)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=on_it)
        rows = [{"channel": channel.id, "instead_of_epg": on_it.id, "why": "better"}]
        guide_manager.choose(channel.id, on_it.name, on_it.id)
        guide_manager.mark_chosen(rows)
        self.assertTrue(rows[0]["chosen"])
        # Settled, so nothing is put forward for it whatever the run wrote down
        self.assertEqual(rows[0]["why"], "")

    def test_a_row_settled_on_another_guide_is_not_marked(self):
        on_it = self._guide("orfeins.old", "ORF Eins", programmes=1)
        other = self._guide("ORF1.at", "ORF 1", programmes=9)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=on_it)
        guide_manager.choose(channel.id, other.name, other.id)
        rows = [{"channel": channel.id, "instead_of_epg": on_it.id, "why": "better"}]
        guide_manager.mark_chosen(rows)
        self.assertFalse(rows[0]["chosen"])
        self.assertEqual(rows[0]["why"], "better")


class FreshnessTests(_Setup):
    """
    A guide can hold thousands of programmes and none of them from this week. Counting
    them says it is full; asking what is on tonight says whether it is any use.
    """

    def _stale_guide(self, tvg_id, name):
        from datetime import timedelta

        guide = EPGData.objects.create(tvg_id=tvg_id, name=name, epg_source=self.source)
        was = timezone.now() - timedelta(days=200)
        for n in range(50):
            ProgramData.objects.create(
                epg=guide, title=f"Last spring {n}",
                start_time=was + timedelta(hours=n), end_time=was + timedelta(hours=n + 1),
            )
        return guide

    def test_a_guide_full_of_last_spring_is_not_put_forward(self):
        stale = self._stale_guide("ORF1.at", "ORF 1")
        channel = self._channel("┃AT┃ ORF 1", 1)
        # It holds fifty programmes, so nothing about how full it is would stop it
        self.assertEqual(guide_manager.programme_counts([stale.id])[stale.id], 50)
        self.assertTrue(self._suggested())

        self.assertEqual(
            guide_manager.programmes_soon([stale.id]), set(),
            "nothing is on it in the next twelve hours",
        )
        self.assertEqual(self._suggested(settings(must_be_fresh=True)), {})

    def test_while_one_with_something_on_tonight_still_is(self):
        good = self._guide("ORF1.at", "ORF 1", programmes=9)
        channel = self._channel("┃AT┃ ORF 1", 1)
        self.assertIn(good.id, guide_manager.programmes_soon([good.id]))
        self.assertTrue(self._suggested(settings(must_be_fresh=True)))

    def test_and_a_guide_nobody_has_read_is_not_thrown_out_for_it(self):
        # It holds nothing because nobody has looked, which is not the same as nothing
        # being on it
        unread = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.source)
        channel = self._channel("┃AT┃ ORF 1", 1)
        found = self._suggested(settings(must_be_fresh=True))
        self.assertEqual(found[str(channel.id)]["epg"], unread.id)


class LoopTests(_Setup):
    def test_a_channel_on_a_loop_is_never_offered_a_guide(self):
        # One thing round the clock has no schedule anywhere, so every guide is wrong
        self._guide("theoffice.us", "The Office", programmes=9)
        loop = self._channel("24/7: The Office", 1)
        found = self._look()
        self.assertEqual(found[str(loop.id)].get("why"), "")
        self.assertFalse(found[str(loop.id)].get("epg"))


class BatchTests(_Setup):
    """The looking runs in batches that queue the next, so one Celery worker is not held."""

    def test_a_batch_says_where_it_has_got_to_as_it_goes(self):
        from apps.channels.tasks import suggest_guides

        self._guide("ORF1.at", "ORF 1", programmes=3)
        for number in range(3):
            self._channel(f"┃AT┃ ORF {number + 1}", number + 1)
        fake = FakeRedis()
        with patch("apps.channels.guide_manager.redis", return_value=fake):
            with patch("apps.channels.tasks.suggest_guides.delay"):
                suggest_guides(settings(), 0)
        said = fake.hashes[guide_manager.RUN_KEY]
        # Reading the catalogue is most of a batch and happens before a channel is looked
        # at, so the page is told that is what is going on rather than shown a still bar
        self.assertEqual(said["stage"], "looking at your channels")
        self.assertEqual(said["done"], "3")

    def test_a_batch_looks_at_its_share_and_queues_the_one_after_it(self):
        from apps.channels.tasks import suggest_guides

        self._guide("ORF1.at", "ORF 1", programmes=3)
        for number in range(3):
            self._channel(f"┃AT┃ ORF {number + 1}", number + 1)
        fake = FakeRedis()
        with patch("apps.channels.guide_manager.redis", return_value=fake):
            with patch("apps.channels.guide_manager.BATCH_CHANNELS", 2):
                with patch("apps.channels.tasks.suggest_guides.delay") as next_batch:
                    suggest_guides(settings(), 0)
        next_batch.assert_called_once_with(settings(), 2)
        self.assertEqual(fake.hashes[guide_manager.RUN_KEY]["done"], "2")

    def test_the_last_batch_says_the_run_is_done_and_queues_nothing(self):
        from apps.channels.tasks import suggest_guides

        fake = FakeRedis()
        with patch("apps.channels.guide_manager.redis", return_value=fake):
            with patch("apps.channels.tasks.suggest_guides.delay") as next_batch:
                suggest_guides(settings(), 0)
        next_batch.assert_not_called()
        self.assertEqual(fake.hashes[guide_manager.RUN_KEY]["state"], "done")

    def test_a_run_asked_to_stop_stops_at_the_batch_it_is_in(self):
        from apps.channels.tasks import suggest_guides

        self._guide("ORF1.at", "ORF 1", programmes=3)
        self._channel("┃AT┃ ORF 1", 1)
        fake = FakeRedis()
        guide_manager.stop(fake)
        with patch("apps.channels.guide_manager.redis", return_value=fake):
            with patch("apps.channels.tasks.suggest_guides.delay") as next_batch:
                self.assertEqual(suggest_guides(settings(), 0), "Stopped")
        next_batch.assert_not_called()
        self.assertEqual(guide_manager.load_suggestions(), {})


class ApplyTests(_Setup):
    def test_the_guide_chosen_goes_on_the_channel_with_its_tvg_id(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay"):
            self.assertEqual(guide_manager.apply({channel.id: guide.id}), {"changed": 1})
        channel.refresh_from_db()
        self.assertEqual(channel.epg_data_id, guide.id)
        self.assertEqual(channel.tvg_id, "ORF1.at")

    def test_applying_reads_the_new_guides_programmes(self):
        # Saved one at a time with update_fields, because that is what the signal watches
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay") as read:
            guide_manager.apply({channel.id: guide.id})
        read.assert_called_once_with(guide.id)

    def test_no_guide_can_be_chosen_too(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=guide)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay"):
            guide_manager.apply({channel.id: None})
        channel.refresh_from_db()
        self.assertIsNone(channel.epg_data_id)

    def test_a_guide_gone_since_the_page_was_looked_at_changes_nothing(self):
        channel = self._channel("┃AT┃ ORF 1", 1)
        self.assertEqual(guide_manager.apply({channel.id: 9999}), {"changed": 0})
        channel.refresh_from_db()
        self.assertIsNone(channel.epg_data_id)

    def test_what_was_applied_comes_off_the_list_of_suggestions(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.save_suggestions(self._look())
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay"):
            guide_manager.apply({channel.id: guide.id})
        self.assertEqual(guide_manager.load_suggestions(), {})


class ViewTests(_Setup):
    def setUp(self):
        super().setUp()
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )

    def test_the_page_says_what_was_found_and_what_can_be_chosen(self):
        self._guide("ORF1.at", "ORF 1", programmes=3)
        self._channel("┃AT┃ ORF 1", 1)
        guide_manager.save_suggestions(self._look())
        data = self.client_api.get("/api/channels/guides/").json()
        self.assertEqual(len(data["suggestions"]), 1)
        self.assertEqual(data["suggestions"][0]["why"], "none")
        self.assertIn("┃AT┃ AUSTRIA", [g["name"] for g in data["channel_groups"]])
        self.assertEqual(data["defaults"]["min_score"], 70)

    def test_which_guides_are_matched_against_is_said_and_kept(self):
        """
        The same settings for a run and for the window on a Lineup row: a guide one of
        them has been told to leave out and the other still offers is worse than either.
        """
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=guide)

        answer = self.client_api.get("/api/channels/channel-manager/matching/").json()
        self.assertEqual(answer["matching"]["sources"], [])
        said = next(s for s in answer["sources"] if s["id"] == self.source.id)
        # How much each source holds and how many channels are on it, because "leave this
        # source out" is not a question anybody can answer from a name alone
        self.assertEqual((said["holds"], said["channels"]), (1, 1))

        kept = self.client_api.put(
            "/api/channels/channel-manager/matching/",
            {"matching": {"sources": [self.source.id], "tvg_id_like": ".at"}},
            format="json",
        ).json()
        self.assertEqual(kept["matching"]["sources"], [self.source.id])
        self.assertEqual(channel_manager.load_matching()["tvg_id_like"], ".at")

    def test_a_source_left_out_is_not_offered_for_a_channel(self):
        self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        offered = self.client_api.get(
            f"/api/channels/channel-manager/guides/?name={channel.name}"
        ).json()
        self.assertTrue(offered["guides"])

        channel_manager.save_matching({"sources": [self.source.id + 999]})
        offered = self.client_api.get(
            f"/api/channels/channel-manager/guides/?name={channel.name}"
        ).json()
        self.assertEqual(offered["guides"], [])

    def test_and_one_source_can_be_tried_on_its_own(self):
        # A question about this channel, not a setting: it does not change what is kept
        self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        offered = self.client_api.get(
            f"/api/channels/channel-manager/guides/?name={channel.name}"
            f"&source={self.source.id + 999}"
        ).json()
        self.assertEqual(offered["guides"], [])
        self.assertEqual(channel_manager.load_matching()["sources"], [])

    def test_choosing_a_source_still_finds_what_that_source_has(self):
        """
        The scan goes over every guide and keeps the best handful it sees. Filtering
        after that meant choosing one source scanned all of them, kept the best twenty
        from everywhere, and then dropped the ones from the other sources -- often every
        one of them. Choosing a source to match against was a way of getting no matches.
        """
        other = EPGSource.objects.create(name="a big one", source_type="xmltv", priority=9)
        # The same channel, under the same name, in a source that is not wanted -- forty
        # of them, which is twice the shortlist. Made first, so they are what the scan
        # sees first and what the shortlist fills up with.
        for n in range(40):
            EPGData.objects.create(
                tvg_id=f"orf1.copy{n}.at", name="ORF 1", epg_source=other
            )
        wanted = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)

        channel_manager.save_matching({"sources": [self.source.id]})
        found = self._look()[str(channel.id)]
        self.assertEqual(
            found.get("epg"), wanted.id,
            "the one source chosen has the channel, and it should be found",
        )

    def test_a_run_leaves_out_what_the_matching_settings_leave_out(self):
        self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        self.assertTrue(self._look()[str(channel.id)]["epg"])

        channel_manager.save_matching({"tvg_id_like": ".uk"})
        self.assertFalse(self._look()[str(channel.id)].get("epg"))

    def test_every_channel_means_every_channel_run_or_no_run(self):
        """
        Listing only what the last run stored made "every channel" mean "every channel
        the last run happened to reach" -- and after a run that was stopped, narrowed to
        a group, or never done, that is a handful. The channels somebody looks for there
        are exactly the ones nothing was found for.
        """
        self._guide("ORF1.at", "ORF 1", programmes=3)
        looked_at = self._channel("┃AT┃ ORF 1", 1)
        never_looked_at = self._channel("┃AT┃ SOMETHING ELSE", 2)
        guide_manager.save_suggestions(
            {k: v for k, v in self._look().items() if k == str(looked_at.id)}
        )

        only_found = self.client_api.get("/api/channels/guides/").json()["suggestions"]
        self.assertEqual([one["channel"] for one in only_found], [looked_at.id])

        everything = self.client_api.get("/api/channels/guides/?all=1").json()["suggestions"]
        self.assertEqual(
            sorted(one["channel"] for one in everything),
            sorted([looked_at.id, never_looked_at.id]),
        )

    def test_and_what_a_run_did_find_is_kept_over_the_top(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.save_suggestions(self._look())
        everything = self.client_api.get("/api/channels/guides/?all=1").json()["suggestions"]
        (row,) = [one for one in everything if one["channel"] == channel.id]
        self.assertEqual(row["epg"], guide.id)
        self.assertEqual(row["why"], "none")

    def test_a_channel_nothing_was_found_for_still_says_what_it_is_on(self):
        guide = self._guide("orf1.old", "ORF 1", programmes=4)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=guide)
        everything = self.client_api.get("/api/channels/guides/?all=1").json()["suggestions"]
        (row,) = [one for one in everything if one["channel"] == channel.id]
        self.assertEqual(row["instead_of"], "ORF 1")
        self.assertEqual(row["instead_of_holds"], 4)
        self.assertEqual(row["why"], "")

    def test_what_a_guide_holds_is_taken_again_rather_than_remembered(self):
        """
        A run writes down what a guide held when it looked. Reading its programmes
        afterwards -- which the window beside it is for -- does not go back and change
        that, so the page went on saying "not read yet" about a guide that had been read.
        """
        guide = self._guide("ORF1.at", "ORF 1")  # nothing in it when the run looked
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.save_suggestions(self._look())
        self.assertEqual(guide_manager.load_suggestions()[str(channel.id)]["programmes"], 0)

        # ...and then somebody reads it
        moment = timezone.now()
        for n in range(3):
            ProgramData.objects.create(
                epg=guide, title=f"Programme {n}",
                start_time=moment + timedelta(hours=n), end_time=moment + timedelta(hours=n + 1),
            )
        (row,) = self.client_api.get("/api/channels/guides/").json()["suggestions"]
        self.assertEqual(row["programmes"], 3)

    def test_and_so_is_what_the_channel_is_on_now(self):
        held = self._guide("orf1.old", "ORF 1 Old", programmes=2)
        self._guide("ORF1.at", "ORF 1", programmes=9)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=held)
        guide_manager.save_suggestions(self._look())
        ProgramData.objects.filter(epg=held).delete()
        (row,) = [
            one for one in self.client_api.get("/api/channels/guides/").json()["suggestions"]
            if one["channel"] == channel.id
        ]
        self.assertEqual(row["instead_of_holds"], 0)

    def test_a_run_is_started_and_can_be_stopped(self):
        self._channel("┃AT┃ ORF 1", 1)
        fake = FakeRedis()
        with patch("apps.channels.guide_manager.redis", return_value=fake):
            with patch("apps.channels.tasks.suggest_guides.delay") as looking:
                answer = self.client_api.post(
                    "/api/channels/guides/run/", {"action": "start"}, format="json"
                ).json()
            self.assertTrue(answer["started"])
            self.assertEqual(answer["total"], 1)
            looking.assert_called_once()

            # While one is going, another is not started on top of it
            with patch("apps.channels.tasks.suggest_guides.delay") as again:
                second = self.client_api.post(
                    "/api/channels/guides/run/", {"action": "start"}, format="json"
                ).json()
            self.assertFalse(second["started"])
            again.assert_not_called()

            self.assertTrue(
                self.client_api.post(
                    "/api/channels/guides/run/", {"action": "stop"}, format="json"
                ).json()["stopping"]
            )
            self.assertTrue(guide_manager.asked_to_stop(fake))

    def test_applying_through_the_page(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay"):
            answer = self.client_api.post(
                "/api/channels/guides/apply/",
                {"choices": {str(channel.id): guide.id}}, format="json",
            )
        self.assertEqual(answer.json(), {"changed": 1})
        # Nothing chosen is a refusal, not a 500
        self.assertEqual(
            self.client_api.post("/api/channels/guides/apply/", {"choices": {}}, format="json").status_code,
            400,
        )

    def test_waving_a_suggestion_away_through_the_page(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.save_suggestions(self._look())
        url = "/api/channels/guides/ignore/"
        self.client_api.post(
            url, {"action": "ignore", "channel": channel.id, "name": channel.name, "epg": guide.id},
            format="json",
        )
        self.assertIn(str(channel.id), guide_manager.load_ignored())
        # and off the list it was on
        self.assertEqual(guide_manager.load_suggestions(), {})
        self.client_api.post(url, {"action": "unignore", "channel": channel.id}, format="json")
        self.assertEqual(guide_manager.load_ignored(), {})

    def test_a_channel_can_be_settled_and_unsettled_from_the_page(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1, epg=guide)
        url = "/api/channels/guides/chosen/"
        answer = self.client_api.post(
            url, {"action": "choose", "channel": channel.id, "name": guide.name, "epg": guide.id},
            format="json",
        )
        self.assertEqual(answer.status_code, 200)
        self.assertIn(str(channel.id), guide_manager.load_chosen())
        data = self.client_api.get("/api/channels/guides/?all=1").json()
        self.assertTrue(data["suggestions"][0]["chosen"])
        self.assertEqual([one["channel"] for one in data["chosen"]], [str(channel.id)])

        self.client_api.post(url, {"action": "unchoose", "channel": channel.id}, format="json")
        self.assertEqual(guide_manager.load_chosen(), {})

    def test_settling_takes_it_off_the_list_being_put_forward(self):
        guide = self._guide("ORF1.at", "ORF 1", programmes=3)
        channel = self._channel("┃AT┃ ORF 1", 1)
        guide_manager.save_suggestions(self._look())
        self.client_api.post(
            "/api/channels/guides/chosen/",
            {"action": "choose", "channel": channel.id, "name": "", "epg": None},
            format="json",
        )
        self.assertEqual(guide_manager.load_suggestions(), {})

    def test_the_settings_are_kept_and_checked(self):
        self.client_api.put(
            "/api/channels/guides/settings/", {"settings": {"min_score": 85}}, format="json"
        )
        self.assertEqual(guide_manager.load_settings()["min_score"], 85)
        self.assertEqual(
            self.client_api.put(
                "/api/channels/guides/settings/", {"settings": {"min_score": "lots"}}, format="json"
            ).status_code,
            400,
        )

    def test_only_an_admin(self):
        plain = APIClient()
        plain.force_authenticate(
            user=User.objects.create_user(username="someone", password="x", user_level=1)
        )
        self.assertEqual(plain.get("/api/channels/guides/").status_code, 403)
