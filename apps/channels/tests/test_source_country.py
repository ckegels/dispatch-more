"""A guide that does not say its country takes its source's (channel_manager.source_countries)."""

from django.test import TestCase

from apps.channels import channel_manager
from apps.epg.models import EPGData, EPGSource


class SourceCountryTests(TestCase):
    def setUp(self):
        channel_manager._SOURCE_COUNTRIES["at"] = 0.0

    def test_the_users_case_a_british_source_is_not_offered_to_a_belgian_channel(self):
        """epg.pw gb numbers its guides, so its Nat Geo Wild said no country and came first, certain."""
        gb = EPGSource.objects.create(name="epg.pw gb", source_type="xmltv")
        wild = EPGData.objects.create(tvg_id="9300", name="Nat Geo Wild", epg_source=gb)
        entry = {"name": wild.name, "tvg_id": wild.tvg_id, "epg_source_id": gb.id}
        self.assertEqual(channel_manager._country_of_guide(entry), "gb")
        score, _, why = channel_manager.judge_guide("┃BE┃ NGC WILD", "be", entry)
        self.assertLess(score, channel_manager.MIN_GUIDE_SCORE, why)
        self.assertIn("the guide is for GB", why)

    def test_a_neighbours_guide_costs_less_than_a_far_one(self):
        nl = {"name": "Nat Geo Wild", "tvg_id": "NatGeoWild.nl"}
        gb = {"name": "Nat Geo Wild", "tvg_id": "NatGeoWild.uk"}
        near, _, _ = channel_manager.judge_guide("┃BE┃ NAT GEO WILD", "be", nl)
        far, _, _ = channel_manager.judge_guide("┃BE┃ NAT GEO WILD", "be", gb)
        self.assertGreater(near, far)
        self.assertLess(far, channel_manager.MIN_GUIDE_SCORE)

    def test_the_last_code_in_the_name_wins_and_tv_is_not_tuvalu(self):
        be = EPGSource.objects.create(name="free-epg.de be", source_type="xmltv")
        de = EPGSource.objects.create(name="iptv-epg.org DE", source_type="xmltv")
        us = EPGSource.objects.create(name="epgshare01 US LOCALS 1", source_type="xmltv")
        pbs = EPGSource.objects.create(name="PBS TV", source_type="xmltv")
        found = channel_manager.source_countries()
        self.assertEqual((found[be.id], found[de.id], found[us.id]), ("be", "de", "us"))
        self.assertNotIn(pbs.id, found)

    def test_a_source_whose_guides_nearly_all_say_one_country_is_that_country(self):
        mixed = EPGSource.objects.create(name="open epg", source_type="xmltv")
        belgian = EPGSource.objects.create(name="github", source_type="xmltv")
        for i in range(10):
            EPGData.objects.create(tvg_id=f"a{i}.be", name=f"A {i}", epg_source=belgian)
            EPGData.objects.create(tvg_id=f"b{i}.{'be' if i < 7 else 'at'}", name=f"B {i}", epg_source=mixed)
        found = channel_manager.source_countries()
        self.assertEqual(found.get(belgian.id), "be")
        self.assertNotIn(mixed.id, found, "70 % is not a source of one country")

    def test_a_guide_that_says_its_country_keeps_it(self):
        gb = EPGSource.objects.create(name="epg.pw gb", source_type="xmltv")
        entry = {"name": "Nat Geo Wild", "tvg_id": "NatGeoWild.ie", "epg_source_id": gb.id}
        self.assertEqual(channel_manager._country_of_guide(entry), "ie")

    def test_one_network_is_not_another_whatever_the_id_says(self):
        """The user's find: ┃CA EN┃ ABC WEST took CA - CBS WEST as certain."""
        cbs = {"name": "CA - CBS WEST", "tvg_id": "CBSKIRO-West.ca"}
        score, _, why = channel_manager.judge_guide("┃CA EN┃ ABC WEST", "ca", cbs, "CBSKIRO-West.ca")
        self.assertEqual(score, 0)
        self.assertIn("ABC is not CBS", why)
        abc = {"name": "CA - ABC WEST", "tvg_id": "ABCKOMO-West.ca"}
        self.assertGreater(channel_manager.judge_guide("┃CA EN┃ ABC WEST", "ca", abc)[0], 0)

    def test_schedules_direct_is_north_american(self):
        """The user's find: ┃DE┃ HGTV took Schedules Direct's HGTV as certain."""
        sd = EPGSource.objects.create(name="Schedules direct", source_type="schedules_direct")
        hgtv = {"name": "HGTV", "tvg_id": "21257", "epg_source_id": sd.id}
        self.assertEqual(channel_manager._country_of_guide(hgtv), "us")
        self.assertLess(channel_manager.judge_guide("┃DE┃ HGTV", "de", hgtv)[0], channel_manager.MIN_GUIDE_SCORE)

    def test_a_guide_saying_the_channel_is_gone_is_dead(self):
        from apps.channels.guide_manager import is_a_dead_guide

        for gone in ("Channel No Longer Available", "This channel is not available", "Station no longer broadcasting"):
            self.assertTrue(is_a_dead_guide(gone), gone)
        for fine in ("Available Light", "Zeit im Bild", "", None):
            self.assertFalse(is_a_dead_guide(fine), fine)

    def test_without_the_guides_country_nothing_is_certain(self):
        """The user's rule: if the countries cannot be matched it cannot be 100 %."""
        nowhere = EPGSource.objects.create(name="epg ripper ALL", source_type="xmltv")
        hgtv = {"name": "HGTV", "tvg_id": "hgtv", "epg_source_id": nowhere.id}
        score, tier, why = channel_manager.judge_guide("┃DE┃ HGTV", "de", hgtv)
        self.assertLessEqual(score, channel_manager.UNKNOWN_COUNTRY_MOST)
        self.assertNotEqual(tier, channel_manager.CERTAIN)
        self.assertIn("country is unknown", why)
        known = {"name": "HGTV", "tvg_id": "HGTV.de"}
        self.assertEqual(channel_manager.judge_guide("┃DE┃ HGTV", "de", known)[1], channel_manager.CERTAIN)
