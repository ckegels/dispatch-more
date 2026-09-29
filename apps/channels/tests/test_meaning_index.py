"""Finding a channel's guide by what its name means (apps.channels.meaning_index).

The real model is not loaded: WordsModel (test_meaning) scores two names by the words they
share. The index is written to a folder of the test's own.
"""

import tempfile
from unittest.mock import patch

from django.test import TestCase

from apps.channels import channel_manager, guide_manager, meaning_index
from apps.channels.models import Channel, ChannelGroup
from apps.channels.tests.test_meaning import WordsModel
from apps.epg.models import EPGData, EPGSource


class MeaningIndexTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, value in (
            ("apps.channels.meaning_index.INDEX_DIR", folder.name),
            ("apps.channels.epg_matching.get_sentence_transformer", (WordsModel(), None)),
        ):
            patcher = patch(target, value) if target.endswith("INDEX_DIR") else patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        meaning_index._loaded["stamp"] = None
        self.de = EPGSource.objects.create(name="EPGShare DE", source_type="xmltv")
        self.discovery = EPGData.objects.create(tvg_id="Discovery.de", name="Discovery Deutschland", epg_source=self.de)
        self.dmax = EPGData.objects.create(tvg_id="DMAX.de", name="DMAX", epg_source=self.de)
        self.uk = EPGData.objects.create(tvg_id="Discovery.uk", name="Discovery UK", epg_source=self.de)
        group = ChannelGroup.objects.create(name="┃DE┃ ALLGEMEINES")
        self.channel = Channel.objects.create(name="┃DE┃ DISCOVERY", channel_number=72, channel_group=group)

    def test_built_once_then_only_what_changed(self):
        first = meaning_index.build()
        self.assertEqual((first["guides"], first["channels"]), (3, 1))
        self.assertEqual(meaning_index.build()["encoded"], 0, "nothing new: nothing worked out again")
        EPGData.objects.create(tvg_id="Sixx.de", name="Sixx", epg_source=self.de)
        self.assertEqual(meaning_index.build()["encoded"], 1)

    def test_a_channel_finds_the_guide_it_means_in_its_country(self):
        meaning_index.build()
        found = meaning_index.similar_guides("┃DE┃ DISCOVERY", "de")
        self.assertEqual(found[0][0], self.discovery.id)
        self.assertNotIn(self.uk.id, [i for i, _ in found], "another country")
        self.assertEqual(meaning_index.similar_guides("┃DE┃ SOMETHING NEW", "de"), [], "newer than the index")

    def test_no_index_no_meaning(self):
        self.assertEqual(meaning_index.similar_guides("┃DE┃ DISCOVERY", "de"), [])
        self.assertEqual(meaning_index.status(), {})

    def test_the_picker_offers_the_models_pick_and_says_so(self):
        far = EPGData.objects.create(tvg_id="xyz.de", name="Entdeckung Kanal", epg_source=self.de)
        with patch("apps.channels.meaning_index.similar_guides", return_value=[(far.id, 0.93)]):
            guides = channel_manager.guide_candidates("┃DE┃ DISCOVERY", limit=10)
        pick = next((g for g in guides if g["id"] == far.id), None)
        self.assertIsNotNone(pick, "found by meaning though the names share nothing")
        self.assertEqual(pick["score"], 93)
        self.assertIn("by meaning", pick["why"])

    def test_a_contradicted_pick_is_still_dropped(self):
        other = EPGData.objects.create(tvg_id="Discovery2.de", name="Discovery 2", epg_source=self.de)
        with patch("apps.channels.meaning_index.similar_guides", return_value=[(other.id, 0.95)]):
            guides = channel_manager.guide_candidates("┃DE┃ DISCOVERY 1", limit=10)
        self.assertNotIn(other.id, [g["id"] for g in guides], "1 is not 2, whatever the model says")

    def test_switched_off_in_the_guides_settings_the_model_is_not_asked(self):
        guide_manager.save_settings({**guide_manager.load_settings(), "use_language_model": False})
        with patch("apps.channels.meaning_index.similar_guides") as asked:
            channel_manager.guide_candidates("┃DE┃ DISCOVERY", limit=10)
        asked.assert_not_called()

    def test_the_guides_run_counts_the_models_pick_too(self):
        far = EPGData.objects.create(tvg_id="xyz.de", name="Entdeckung Kanal", epg_source=self.de)
        row = {"id": far.id, "name": far.name, "tvg_id": "xyz.de", "epg_source_id": self.de.id}
        with patch("apps.channels.meaning_index.similar_guides", return_value=[(far.id, 0.9)]):
            found = guide_manager._score_against(
                "┃DE┃ DISCOVERY", [], {self.de.id: "EPGShare DE"}, {}, set(), {}, by_meaning={far.id: row},
            )
        self.assertEqual([(f["epg"], f["score"]) for f in found], [(far.id, 90)])
        self.assertIn("by meaning", found[0]["match_why"])
