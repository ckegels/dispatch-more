"""The Lineup placing, by what its name means, a stream its rules could not place (meaning.py).

The real model is not loaded here: a stand-in scores two names by the words they share, which
is enough to see what is placed, what is refused, and that nothing happens with it off.
"""

import re
from unittest.mock import patch

import numpy as np
from django.test import TestCase

from apps.channels import channel_manager, meaning
from apps.channels.models import Channel, ChannelGroup, ChannelStream, Stream
from apps.m3u.models import M3UAccount


class WordsModel:
    """A name as the words it has, each word a direction: shared words make two names close."""

    def encode(self, texts, normalize_embeddings=True, batch_size=None):
        vectors = []
        for text in texts:
            v = np.zeros(512)
            for word in re.findall(r"[0-9a-z+!&]+", text.lower()):
                if word not in meaning.FILLER:
                    v[hash(word) % 512] += 1
            n = np.linalg.norm(v)
            vectors.append(v / n if n else v)
        return np.array(vectors)


def settings(**overrides):
    return {**channel_manager.DEFAULTS, **overrides}


class MeaningTests(TestCase):
    def setUp(self):
        # Each test from nothing remembered, and no model let go ten minutes on
        meaning._MEANINGS.clear()
        self.addCleanup(meaning._MEANINGS.clear)
        later = patch.object(meaning, "_let_go_later")
        later.start()
        self.addCleanup(later.stop)
        patcher = patch("apps.channels.epg_matching.get_sentence_transformer", return_value=(WordsModel(), None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.digi = M3UAccount.objects.create(name="Digitalizard", account_type="XC", server_url="http://d", is_active=True)
        self.group = ChannelGroup.objects.create(name="┃DE┃ ALLGEMEINES")
        self.nl = ChannelGroup.objects.create(name="┃NL┃ SPORT")
        self.discovery = Channel.objects.create(name="┃DE┃ DISCOVERY", channel_number=72, channel_group=self.group)
        self.ziggo = Channel.objects.create(name="┃NL┃ ZIGGO SPORT", channel_number=300, channel_group=self.nl)

    def stream(self, name, group=None):
        return Stream.objects.create(
            name=name, url=f"http://d/{name}", m3u_account=self.digi, channel_group=group or self.group
        )

    def added(self, plan, channel):
        row = next((r for r in plan["rows"] if r["key"] == f"ch:{channel.id}"), None)
        return {s["name"]: s.get("meaning") for s in (row or {}).get("streams", []) if s["added"]}

    def test_off_a_stream_whose_name_differs_is_only_a_new_channel(self):
        self.stream("DE| DISCOVERY CHANNEL HD")
        plan = channel_manager.build_plan(settings())
        self.assertEqual(self.added(plan, self.discovery), {})

    def test_on_it_goes_onto_the_channel_it_means_and_says_so(self):
        self.stream("DE| DISCOVERY CHANNEL HD")
        plan = channel_manager.build_plan(settings(use_language_model=True))
        self.assertEqual(self.added(plan, self.discovery), {"DE| DISCOVERY CHANNEL HD": 1.0})
        self.assertFalse(
            any(r["status"] == "new" and "DISCOVERY" in (r.get("name") or "") for r in plan["rows"]),
            "no longer offered as a new channel",
        )

    def test_a_longer_name_does_not_land_on_a_shorter_one(self):
        self.stream("NL| ZIGGO SPORT GOLF FHD", self.nl)
        plan = channel_manager.build_plan(settings(use_language_model=True))
        self.assertEqual(self.added(plan, self.ziggo), {})

    def test_numbers_plus_and_countries_must_agree(self):
        vtm3 = {"channel": Channel(name="┃BE┃ VTM 3"), "country": "be"}
        orf3 = {"channel": Channel(name="┃AT┃ ORF III"), "country": "at"}
        amc_plus = {"channel": Channel(name="AMC+"), "country": ""}
        s = settings()
        self.assertFalse(meaning.agrees({"name": "BE| VTM 1 FHD", "country": "be"}, vtm3, s))
        self.assertTrue(meaning.agrees({"name": "AT| ORF 3 HD", "country": "at"}, orf3, s), "III is 3")
        self.assertFalse(meaning.agrees({"name": "US| AMC HD", "country": "us"}, amc_plus, s))
        self.assertFalse(
            meaning.agrees({"name": "NL| DISCOVERY", "country": "nl"}, {"channel": self.discovery, "country": "de"}, s)
        )

    def test_written_together_is_still_the_word(self):
        zdf = {"channel": Channel(name="┃DE┃ ZDF INFO"), "country": "de"}
        self.assertTrue(meaning.agrees({"name": "DE| ZDFINFO FHD", "country": "de"}, zdf, settings()))

    def test_without_the_model_nothing_is_placed_and_nothing_breaks(self):
        self.stream("DE| DISCOVERY CHANNEL HD")
        with patch("apps.channels.epg_matching.get_sentence_transformer", return_value=(None, None)):
            plan = channel_manager.build_plan(settings(use_language_model=True))
        self.assertEqual(self.added(plan, self.discovery), {})


class RememberedMeaningTests(TestCase):
    def setUp(self):
        from apps.channels import meaning as m

        m._MEANINGS.clear()
        self.addCleanup(m._MEANINGS.clear)

    def test_a_second_preview_asks_the_model_about_nothing_it_has_seen(self):
        model = WordsModel()
        with patch("apps.channels.epg_matching.get_sentence_transformer", return_value=(model, None)) as loaded, \
                patch.object(meaning, "_let_go_later"):
            meaning.meanings(["discovery", "vrt 1"])
            meaning.meanings(["discovery", "vrt 1"])
            self.assertEqual(loaded.call_count, 1, "loaded once, for what was new")
            meaning.meanings(["discovery", "orf 1"])
            self.assertEqual(loaded.call_count, 2, "and again only for the new name")


class RecognitionLevelTests(TestCase):
    """How hard to look, as one choice, with the language model by default (the user's)."""

    def test_thorough_by_default_with_the_language_model(self):
        values = channel_manager.load_settings()
        self.assertEqual(values["recognition"], "thorough")
        self.assertTrue(values["use_language_model"])
        self.assertTrue(values["country_any_way"] and values["match_call_signs"] and values["same_country"])

    def test_a_level_sets_the_levers_and_custom_leaves_them(self):
        exact = channel_manager.settings_from({"recognition": "exact", "use_language_model": True})
        self.assertFalse(exact["use_language_model"], "the level wins over a single lever")
        custom = channel_manager.settings_from({"recognition": "custom", "use_language_model": True, "match_call_signs": False})
        self.assertTrue(custom["use_language_model"])
        self.assertFalse(custom["match_call_signs"])


class LocalStationTests(TestCase):
    def test_a_local_station_is_not_its_network_whatever_id_they_share(self):
        """The user's find: US| FOX 05 (WNYW) NEW YORK went onto ┃USA┃ FOX HD by foxwnyw.us."""
        fox = {"channel": Channel(name="┃USA┃ FOX HD"), "country": "us"}
        wnyw = {"name": "US| FOX 05 (WNYW) NEW YORK", "country": "us"}
        self.assertTrue(channel_manager._tvg_contradicted(wnyw, fox))
        local = {"channel": Channel(name="FOX 5 | NEW YORK | WNYW"), "country": "us"}
        self.assertFalse(channel_manager._tvg_contradicted(wnyw, local), "the station itself")


class ProgressTests(TestCase):
    def test_a_preview_says_where_it_has_got_to(self):
        from django.core.cache import cache

        cache.delete(channel_manager.PROGRESS_KEY)
        channel_manager.build_plan(settings())
        self.assertEqual(channel_manager.load_progress()["stage"], "Done")
