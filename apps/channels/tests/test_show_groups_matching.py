# Show Groups' matching rules, tested on the real titles the plugin's surveys met (its
# HANDOVER.md §3). Taken over from the plugin with the rules themselves.
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from apps.channels.show_groups import matching, store, themes


def cooking(**changes):
    settings = themes.preset("cooking")
    settings.update(changes)
    return matching.group_from_theme(settings)


class Plain(unittest.TestCase):
    def test_episode_names_and_brackets_are_cut(self):
        self.assertEqual(matching.plain("Dagelijkse kost - Aflevering 3"), "dagelijkse kost")
        self.assertEqual(matching.plain("Come Dine with Me: Brighton"), "come dine with me")
        self.assertEqual(matching.plain("Silvia kocht (Wh.)"), "silvia kocht")

    def test_accents_and_case_do_not_matter(self):
        self.assertEqual(matching.plain("Petits plats en équilibre"), "petits plats en equilibre")
        self.assertEqual(matching.plain("Die Küchenschlacht"), "die kuchenschlacht")


class Layers(unittest.TestCase):
    def test_guide_category_takes_it(self):
        verdict = matching.judge(cooking(), "America's Test Kitchen", ["Cooking", "How-to"])
        self.assertTrue(verdict.taken)
        self.assertEqual(verdict.layer, matching.GUIDE)
        self.assertFalse(verdict.uncertain)

    def test_a_category_given_as_one_string_still_counts(self):
        self.assertTrue(matching.judge(cooking(), "Lekker Messy", "Cooking").taken)

    def test_structural_categories_say_nothing(self):
        verdict = matching.judge(cooking(), "Kook mee met MAX", ["Series"],
                                 from_guides={"kook mee met max": ["Cooking"]})
        self.assertTrue(verdict.taken)
        self.assertEqual(verdict.layer, matching.OTHER_GUIDE)

    def test_a_guide_that_says_something_else_is_trusted_over_the_title(self):
        # A drama with "kitchen" in its name stays out even with title words on
        verdict = matching.judge(cooking(use_title_words=True), "The Kitchen", ["Drama"])
        self.assertFalse(verdict.taken)
        self.assertEqual(verdict.layer, matching.GUIDE)

    def test_online_answer_when_no_guide_knows(self):
        online = {"great british menu": {"source": "tvmaze", "genres": ["Food", "Reality"]}}
        verdict = matching.judge(cooking(), "Great British Menu", (), {}, online)
        self.assertTrue(verdict.taken)
        self.assertEqual(verdict.layer, "tvmaze")

    def test_online_answer_that_is_not_cooking(self):
        online = {"tagesschau": {"source": "wikidata", "genres": ["television news program"]}}
        self.assertFalse(matching.judge(cooking(), "Tagesschau", (), {}, online).taken)

    def test_nothing_known(self):
        verdict = matching.judge(cooking(), "NOS Journaal")
        self.assertFalse(verdict.taken)
        self.assertEqual(verdict.layer, matching.NOTHING)


class TitleWords(unittest.TestCase):
    def test_off_by_default_but_reported(self):
        verdict = matching.judge(cooking(), "Martha Bakes")
        self.assertFalse(verdict.taken)
        self.assertEqual(verdict.title_word, "bakes")
        self.assertTrue(verdict.uncertain)

    def test_when_on_it_takes_and_stays_uncertain(self):
        verdict = matching.judge(cooking(use_title_words=True), "Martha Bakes")
        self.assertTrue(verdict.taken)
        self.assertTrue(verdict.uncertain)

    def test_the_word_at_the_end_of_a_compound(self):
        group = cooking(use_title_words=True)
        self.assertTrue(matching.judge(group, "Ramon's Buitenkeuken").taken)
        self.assertTrue(matching.judge(group, "Die Küchenschlacht").taken)

    def test_exclusions(self):
        group = cooking(use_title_words=True)
        self.assertFalse(matching.judge(group, "Hell's Kitchen").taken)
        self.assertFalse(matching.judge(group, "Kitchen Nightmares").taken)
        self.assertFalse(matching.judge(group, "Murder in the Kitchen").taken)

    def test_the_words_are_not_asked_when_a_layer_knows(self):
        verdict = matching.judge(cooking(), "Chef's Table", ["Documentary"])
        self.assertEqual(verdict.title_word, "")


class PinsAndDisqualifiers(unittest.TestCase):
    def test_never_beats_the_guide(self):
        group = cooking(never="Météo à la carte\n")
        verdict = matching.judge(group, "Météo à la carte", ["Cooking", "Public affairs", "Weather"])
        self.assertFalse(verdict.taken)
        self.assertEqual(verdict.layer, matching.PIN)

    def test_always_beats_nothing_known(self):
        group = cooking(always="Silvia kocht")
        self.assertTrue(matching.judge(group, "Silvia kocht - Folge 12").taken)

    def test_a_pin_may_contain_a_comma(self):
        group = cooking(always="Eat, Drink, Love")
        self.assertTrue(matching.judge(group, "Eat, Drink, Love").taken)

    def test_disqualifier_is_reported_while_off(self):
        verdict = matching.judge(cooking(), "Food Factory", ["Consumer", "Cooking"])
        self.assertTrue(verdict.taken)
        self.assertEqual(verdict.disqualifier, "Consumer")

    def test_disqualifier_refuses_when_on(self):
        verdict = matching.judge(cooking(use_disqualifiers=True), "BinnensteBuiten",
                                 ["Cooking", "Drama", "Home improvement"])
        self.assertFalse(verdict.taken)
        self.assertEqual(verdict.disqualifier, "Home improvement")


class ReadyMadeGroups(unittest.TestCase):
    """The groups that come with Show Groups besides Cooking, on categories guides write."""

    def judge(self, group_id, categories, title="Something"):
        return matching.judge(matching.group_from_theme(themes.preset(group_id)), title, categories)

    def test_travel_in_four_languages(self):
        for category in ("Travel", "Reisreportage", "Voyage", "Urlaub & Reisen"):
            self.assertTrue(self.judge("travel", [category]).taken, category)
        self.assertFalse(self.judge("travel", ["Drama"]).taken)

    def test_a_film_is_a_movie_but_a_documentary_film_is_not(self):
        self.assertTrue(self.judge("movies", ["Movie", "Comedy"]).taken)
        self.assertTrue(self.judge("movies", ["Spielfilm"]).taken)
        self.assertFalse(self.judge("movies", ["Documentary film"]).taken)
        self.assertFalse(self.judge("movies", ["Filmmagazin"]).taken)

    def test_science_fiction_is_not_science(self):
        self.assertTrue(self.judge("science", ["Science"]).taken)
        self.assertFalse(self.judge("science", ["Science fiction"]).taken)

    def test_every_ready_made_group_takes_something(self):
        for group_id in themes.PRESET_IDS:
            group = matching.group_from_theme(themes.preset(group_id))
            self.assertTrue(group.category_words, group_id)


class Timing(unittest.TestCase):
    """The owner's rule: join when a show starts within the hour, leave when nothing follows
    within the hour, and stay a little longer before leaving."""

    def at(self, hour, minute=0):
        return datetime(2026, 9, 22, hour, minute, tzinfo=timezone.utc)

    def test_joins_an_hour_ahead_and_lingers_after(self):
        [stay] = matching.stays([(self.at(18), self.at(18, 30), "a")], 60, 60, 15)
        self.assertEqual(stay.joins, self.at(17))
        self.assertEqual(stay.leaves, self.at(18, 45))

    def test_a_show_within_the_hour_keeps_the_channel(self):
        found = matching.stays([(self.at(18), self.at(18, 30), "a"),
                                (self.at(19, 20), self.at(20), "b")], 60, 60, 15)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].leaves, self.at(20, 15))

    def test_nothing_within_the_hour_lets_it_go(self):
        found = matching.stays([(self.at(18), self.at(18, 30), "a"),
                                (self.at(21), self.at(21, 30), "b")], 60, 60, 15)
        self.assertEqual([s.leaves for s in found], [self.at(18, 45), self.at(21, 45)])
        self.assertEqual(found[1].joins, self.at(20))

    def test_a_later_join_that_overlaps_the_linger_is_one_stay(self):
        # Leaves at 18:45 at the earliest, next show joins at 18:40 (80 min gap > leave_after 60)
        found = matching.stays([(self.at(17), self.at(18, 30), "a"),
                                (self.at(19, 50), self.at(20), "b")], 70, 60, 15)
        self.assertEqual(len(found), 1)

    def test_zero_everything_is_just_the_show(self):
        [stay] = matching.stays([(self.at(18), self.at(18, 30), "a")], 0, 0, 0)
        self.assertEqual((stay.joins, stay.leaves), (self.at(18), self.at(18, 30)))


class SurveyImport(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ["SHOW_GROUPS_DIR"] = self.folder.name

    def tearDown(self):
        os.environ.pop("SHOW_GROUPS_DIR", None)
        self.folder.cleanup()

    def survey(self, answers):
        path = os.path.join(self.folder.name, "show-lookups.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(answers, fh)
        return path

    def test_import_refuses_answers_about_another_programme(self):
        path = self.survey({
            "True Crime Story": {"name": "My True Crime Story", "genres": ["Crime"], "where": "tvmaze"},
            "Great British Menu": {"name": "Great British Menu", "genres": ["Food"], "where": "tvmaze"},
            "Dagelijkse Kost": {"name": "Dagelijkse kost", "genres": ["cooking show"], "where": "wikidata"},
            "Nobody Knows": {"name": "", "genres": [], "where": ""},
        })
        counts, refused = store.import_survey(path)
        self.assertEqual(counts["added"], 4)
        self.assertEqual(counts["refused (another programme)"], 1)
        self.assertEqual(counts["nobody knew"], 1)
        self.assertEqual(refused, ["True Crime Story -> My True Crime Story"])

        titles = store.load_lookups()
        # Refused: recorded as "TVmaze did not know it", so the other sources are still asked
        self.assertEqual(set(titles["true crime story"]), {"tvmaze"})
        self.assertNotIn("genres", titles["true crime story"]["tvmaze"])
        self.assertEqual(titles["great british menu"]["tvmaze"]["genres"], ["Food"])
        # The survey asked Wikidata only when TVmaze had nothing
        self.assertEqual(set(titles["dagelijkse kost"]), {"tvmaze", "wikidata"})
        online = store.online_answers(titles)
        self.assertNotIn("true crime story", online)
        self.assertTrue(matching.judge(cooking(), "Dagelijkse kost", (), {}, online).taken)
        self.assertEqual(online["dagelijkse kost"]["source"], "wikidata")

    def test_a_second_import_keeps_what_is_known(self):
        path = self.survey({"Great British Menu": {"name": "Great British Menu", "genres": ["Food"],
                                                   "where": "tvmaze"}})
        store.import_survey(path)
        counts, _ = store.import_survey(path)
        self.assertEqual(counts["added"], 0)
        self.assertEqual(counts["known already"], 1)

    def test_a_missing_dictionary_is_empty(self):
        self.assertEqual(store.load_lookups(), {})

    def test_a_version_1_dictionary_is_read_as_sources(self):
        with open(os.path.join(self.folder.name, store.LOOKUPS), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "titles": {
                "great british menu": {"source": "tvmaze", "name": "Great British Menu",
                                       "genres": ["Food"], "asked": "2026-09-22T18:00:00+00:00"},
                "nos journaal": {"source": "", "name": "", "genres": []},
            }}, fh)
        titles = store.load_lookups()
        self.assertEqual(titles["great british menu"]["tvmaze"]["genres"], ["Food"])
        self.assertEqual(set(titles["nos journaal"]), {"tvmaze", "wikidata"})
        store.save_lookups(titles)
        self.assertEqual(store.load_lookups(), titles)

    def test_answers_from_several_sources_are_combined(self):
        titles = {"komen eten": {
            "tvmaze": {"name": "Komen Eten", "genres": ["Reality"]},
            "wikipedia": {"name": "Komen Eten", "genres": ["Vlaams kookprogramma", "Reality"]},
            "tmdb": {"asked": "2026-09-22T18:00:00+00:00"},
        }}
        online = store.online_answers(titles)
        self.assertEqual(online["komen eten"], {"source": "tvmaze+wikipedia",
                                                "genres": ["Reality", "Vlaams kookprogramma"]})
        self.assertTrue(matching.judge(cooking(), "Komen Eten", (), {}, online).taken)


class NewDefaults(unittest.TestCase):
    def test_wikipedia_categories_in_four_languages(self):
        group = cooking()
        for category in ("Cooking television series", "Vlaams kookprogramma", "Kochsendung",
                         "Émission de télévision culinaire", "Culinair televisieprogramma"):
            self.assertTrue(matching.judge(group, "X", [category]).taken, category)


if __name__ == "__main__":
    unittest.main()
