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
