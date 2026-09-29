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
