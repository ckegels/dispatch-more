"""Suggesting channel logos from public collections (apps.channels.logo_library).

The shapes below are copied from the real answers of both collections, measured before any
of this was written: GitHub's file listing for tv-logo/tv-logos, and iptv-org's logos.json
and channels.json. The channel names are real ones, including the ones that went wrong.
"""

import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import logo_library
from apps.channels.models import Channel, Logo

TREE = {
    "truncated": False,
    "tree": [
        {"type": "blob", "path": "countries/belgium/een-be.png"},
        {"type": "blob", "path": "countries/belgium/tfx-be.png"},
        {"type": "blob", "path": "countries/france/tfx-fr.png"},
        {"type": "blob", "path": "countries/france/nickelodeon-teen-fr.png"},
        {"type": "blob", "path": "countries/austria/orf1-at.png"},
        {"type": "blob", "path": "countries/austria/atv-at.png"},
        {"type": "blob", "path": "countries/austria/hd/atv-hd-at.png"},
        {"type": "blob", "path": "countries/belgium/atv-be.png"},
        {"type": "blob", "path": "countries/france/0_all_logos_mosaic.md"},
        {"type": "tree", "path": "countries/france"},
    ],
}
IPTV_CHANNELS = [
    {"id": "ORF1.at", "name": "ORF 1", "alt_names": ["ORF eins"], "country": "AT"},
    {"id": "TFX.fr", "name": "TFX", "alt_names": [], "country": "FR"},
    {"id": "Gone.fr", "name": "Gone TV", "alt_names": [], "country": "FR",
     "closed": "2020-01-01"},
]
IPTV_LOGOS = [
    {"channel": "ORF1.at", "url": "https://i.imgur.com/orf1.png", "format": "PNG",
     "width": 1000, "height": 1000},
    {"channel": "TFX.fr", "url": "https://i.imgur.com/tfx.png", "format": "PNG",
     "width": 512, "height": 512},
    {"channel": "Gone.fr", "url": "https://i.imgur.com/gone.png", "format": "PNG"},
    {"channel": "Unknown.xx", "url": "https://i.imgur.com/orphan.png", "format": "PNG"},
]


class _Answer:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def both_collections(url, **_kwargs):
    if "tv-logos" in url:
        return _Answer(TREE)
    if url.endswith("logos.json"):
        return _Answer(IPTV_LOGOS)
    if url.endswith("channels.json"):
        return _Answer(IPTV_CHANNELS)
    raise AssertionError(f"nothing asks for {url}")


def built_index():
    cache.delete(logo_library.INDEX_KEY)
    with patch("apps.channels.logo_library.requests.get", side_effect=both_collections):
        logo_library.build_index()
    return logo_library.load_index()


class MatchingTests(TestCase):
    def test_a_name_comes_down_to_what_makes_it_that_channel(self):
        # The decoration a playlist adds, and the ways collections write the same name
        self.assertEqual(logo_library.match_key("┃FR┃ TFX"), "tfx")
        self.assertEqual(logo_library.match_key("ORF 1"), "orf1")
        self.assertEqual(logo_library.match_key("orf-1"), "orf1")
        self.assertEqual(logo_library.match_key("[BE] Canvas"), "canvas")

    def test_plus_and_ampersand_are_words(self):
        """
        Every collection writes them out: 590 tv-logos files say "plus", 112 say "and".

        Dropped with the other punctuation, Canal+ became Canal and matched nothing of its
        own, and A&E became AE.
        """
        self.assertEqual(logo_library.match_key("┃FR┃ Canal+"), "canalplus")
        self.assertEqual(logo_library.match_key("canal-plus"), "canalplus")
        self.assertEqual(logo_library.match_key("A&E"), "aande")
        self.assertEqual(logo_library.match_key("a-and-e"), "aande")

    def test_accents_are_folded_not_dropped(self):
        """Dropped, "Eén" became "en" and missed the "een" every collection files it as."""
        self.assertEqual(logo_library.match_key("┃BE┃ Eén"), "een")

    def test_the_country_is_read_from_the_front_of_the_name(self):
        self.assertEqual(logo_library.country_of("┃FR┃ TFX"), "fr")
        self.assertEqual(logo_library.country_of("[BE] Eén"), "be")
        self.assertEqual(logo_library.country_of("UK: BBC One"), "gb")
        self.assertEqual(logo_library.country_of("TFX"), "")


class SuggestionTests(TestCase):
    def setUp(self):
        self.index = built_index()

    def _urls(self, name):
        return [s["url"] for s in logo_library.suggestions_for(name, self.index)]

    def test_the_channels_own_country_comes_first(self):
        """There is a TFX in France and one in Belgium, with different logos."""
        found = self._urls("┃FR┃ TFX")
        self.assertIn("tfx-fr.png", found[0])
        self.assertTrue(any("tfx-be.png" in url for url in found))

        found = self._urls("┃BE┃ TFX")
        self.assertIn("tfx-be.png", found[0])

    def test_an_accented_name_is_found(self):
        self.assertIn("een-be.png", self._urls("┃BE┃ Eén")[0])

    def test_whole_names_only(self):
        """Looking for "een" as a part turns up Nickelodeon Teen, which is another channel."""
        self.assertFalse(any("nickelodeon" in url for url in self._urls("┃BE┃ Eén")))

    def test_a_channel_the_collections_do_not_have_gets_nothing_rather_than_a_guess(self):
        # Both real: in neither collection
        self.assertEqual(self._urls("┃BE┃ NGC"), [])
        self.assertEqual(self._urls("┃FR┃ ANGERS TV"), [])

    def test_another_name_the_channel_goes_by_is_found(self):
        self.assertEqual(self._urls("ORF eins"), ["https://i.imgur.com/orf1.png"])

    def test_hd_in_the_name_is_tried_without_it(self):
        self.assertTrue(self._urls("┃AT┃ ORF 1 HD"))

    def test_the_collection_whose_links_last_is_preferred(self):
        """tv-logos is on GitHub; iptv-org points at image hosts that come and go."""
        found = logo_library.suggestions_for("┃AT┃ ORF 1", self.index)
        self.assertEqual(found[0]["source"], logo_library.TV_LOGOS)
        self.assertEqual(found[1]["source"], logo_library.IPTV_ORG)

    def test_only_logos_are_indexed(self):
        entries = [e for es in self.index["entries"].values() for e in es]
        self.assertFalse(any(e["url"].endswith(".md") for e in entries))
        # A logo for a channel iptv-org does not list belongs to nothing
        self.assertFalse(any("orphan" in e["url"] for e in entries))


class BuildingTests(TestCase):
    def test_one_collection_that_cannot_be_reached_does_not_cost_the_other(self):
        def iptv_down(url, **kwargs):
            if "iptv-org" in url:
                raise ConnectionError("unreachable")
            return both_collections(url, **kwargs)

        cache.delete(logo_library.INDEX_KEY)
        with patch("apps.channels.logo_library.requests.get", side_effect=iptv_down):
            built = logo_library.build_index()

        self.assertIn(logo_library.TV_LOGOS, built["counts"])
        self.assertIn(logo_library.IPTV_ORG, built["errors"])
        self.assertTrue(logo_library.suggestions_for("┃BE┃ Eén", logo_library.load_index()))


class ApplyTests(TestCase):
    def test_the_chosen_logos_are_given_and_nothing_else(self):
        chosen = Channel.objects.create(channel_number=1, name="┃BE┃ Eén")
        left_alone = Channel.objects.create(channel_number=2, name="┃FR┃ TFX")
        url = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/belgium/een-be.png"

        result = logo_library.apply_logos([(chosen.id, url, "een")])

        chosen.refresh_from_db()
        left_alone.refresh_from_db()
        self.assertEqual(chosen.logo.url, url)
        self.assertIsNone(left_alone.logo_id)
        self.assertEqual(result, {"updated": 1, "created_logos": 1})

    def test_a_logo_dispatcharr_already_has_is_used_rather_than_copied(self):
        existing = Logo.objects.create(name="Eén", url="https://example.com/een.png")
        channel = Channel.objects.create(channel_number=1, name="Eén")

        result = logo_library.apply_logos([(channel.id, existing.url, "een")])

        channel.refresh_from_db()
        self.assertEqual(channel.logo_id, existing.id)
        self.assertEqual(result["created_logos"], 0)
        self.assertEqual(Logo.objects.filter(url=existing.url).count(), 1)

    def test_anything_that_is_not_an_address_is_ignored(self):
        channel = Channel.objects.create(channel_number=1, name="Eén")
        result = logo_library.apply_logos([(channel.id, "not a url", "x")])
        self.assertEqual(result["updated"], 0)


class ViewTests(TestCase):
    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )
        built_index()
        self.een = Channel.objects.create(channel_number=1, name="┃BE┃ Eén")
        self.ngc = Channel.objects.create(channel_number=2, name="┃BE┃ NGC")

    def test_each_channel_comes_with_what_it_has_and_what_it_could_have(self):
        data = self.client_api.get("/api/channels/logo-library/").json()

        self.assertTrue(data["status"]["built"])
        (row,) = data["channels"]  # NGC has nothing to suggest, so is not listed
        self.assertEqual(row["name"], "┃BE┃ Eén")
        self.assertIsNone(row["current"])
        self.assertIn("een-be.png", row["suggestions"][0]["url"])

    def test_every_channel_can_be_listed(self):
        data = self.client_api.get("/api/channels/logo-library/?show=all").json()
        self.assertEqual({row["name"] for row in data["channels"]}, {"┃BE┃ Eén", "┃BE┃ NGC"})

    def test_a_logo_it_already_has_is_not_suggested_back_to_it(self):
        url = logo_library.TV_LOGOS_RAW + "countries/belgium/een-be.png"
        self.een.logo = Logo.objects.create(name="een", url=url)
        self.een.save()

        data = self.client_api.get("/api/channels/logo-library/?show=all").json()
        row = next(r for r in data["channels"] if r["name"] == "┃BE┃ Eén")
        self.assertNotIn(url, [s["url"] for s in row["suggestions"]])

    def test_applying(self):
        url = logo_library.TV_LOGOS_RAW + "countries/belgium/een-be.png"
        response = self.client_api.post(
            "/api/channels/logo-library/apply/",
            {"assignments": [{"channel_id": self.een.id, "url": url, "name": "een"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.een.refresh_from_db()
        self.assertEqual(self.een.logo.url, url)

    def test_nothing_chosen_is_refused(self):
        response = self.client_api.post(
            "/api/channels/logo-library/apply/", {"assignments": []}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(
            user=User.objects.create_user(username="viewer", password="x", user_level=0)
        )
        self.assertEqual(viewer.get("/api/channels/logo-library/").status_code, 403)


class SearchTests(TestCase):
    """Finding a logo by hand, for a channel nothing was suggested for."""

    def setUp(self):
        self.index = built_index()

    def _names(self, query, country=""):
        return [r["url"].rsplit("/", 1)[-1] for r in logo_library.search(query, self.index, country)]

    def test_a_part_of_a_name_counts_when_someone_is_looking(self):
        """Unasked, only whole names are offered; searching, the person can tell them apart."""
        found = self._names("een")
        self.assertIn("een-be.png", found)
        self.assertIn("nickelodeon-teen-fr.png", found)

    def test_the_whole_name_comes_before_names_it_is_part_of(self):
        found = self._names("een")
        self.assertEqual(found[0], "een-be.png")

    def test_the_country_asked_for_comes_first(self):
        self.assertEqual(self._names("tfx", "be")[0], "tfx-be.png")
        self.assertEqual(self._names("tfx", "fr")[0], "tfx-fr.png")

    def test_too_little_to_go_on_finds_nothing(self):
        self.assertEqual(self._names("a"), [])
        self.assertEqual(self._names("┃BE┃"), [])

    def test_the_same_logo_is_listed_once(self):
        """iptv-org files a channel under each of its names, all pointing at one image."""
        found = logo_library.search("orf", self.index)
        urls = [r["url"] for r in found]
        self.assertEqual(len(urls), len(set(urls)))


class ApplyByIdTests(TestCase):
    """A logo uploaded from the page is on disk, not at an address, so it goes by id."""

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )

    def test_an_uploaded_logo_is_given_by_id(self):
        uploaded = Logo.objects.create(name="angers", url="/data/logos/angers.png")
        channel = Channel.objects.create(channel_number=1, name="┃FR┃ ANGERS TV")

        response = self.client_api.post(
            "/api/channels/logo-library/apply/",
            {"assignments": [{"channel_id": channel.id, "logo_id": uploaded.id}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["updated"], 1)
        channel.refresh_from_db()
        self.assertEqual(channel.logo_id, uploaded.id)

    def test_links_and_uploads_can_be_applied_together(self):
        uploaded = Logo.objects.create(name="angers", url="/data/logos/angers.png")
        angers = Channel.objects.create(channel_number=1, name="┃FR┃ ANGERS TV")
        ngc = Channel.objects.create(channel_number=2, name="┃BE┃ NGC")

        response = self.client_api.post(
            "/api/channels/logo-library/apply/",
            {
                "assignments": [
                    {"channel_id": angers.id, "logo_id": uploaded.id},
                    {"channel_id": ngc.id, "url": "https://example.com/ngc.png", "name": "NGC"},
                ]
            },
            format="json",
        )

        self.assertEqual(response.json(), {"updated": 2, "created_logos": 1})

    def test_a_logo_that_does_not_exist_is_not_given(self):
        channel = Channel.objects.create(channel_number=1, name="Anything")
        result = logo_library.apply_logo_ids([(channel.id, 999999)])
        self.assertEqual(result["updated"], 0)

    def test_searching(self):
        built_index()
        data = self.client_api.get("/api/channels/logo-library/search/?q=tfx&country=be").json()
        self.assertTrue(data["built"])
        self.assertIn("tfx-be.png", data["results"][0]["url"])


# The shapes below are copied from real ones: an iptv-org country playlist, and the XMLTV
# Dispatcharr itself writes.
M3U_TEXT = b'''#EXTM3U
#EXTINF:-1 tvg-id="ATV.be@SD" tvg-logo="https://upload.wikimedia.org/atv-be.png" group-title="General",ATV (Belgium) (1080p)
http://example.com/atv.m3u8
#EXTINF:-1 tvg-id="BelRTL.be" tvg-name="Bel RTL" tvg-country="BE" tvg-logo="https://i.imgur.com/belrtl.png",Bel RTL (1080p)
http://example.com/belrtl.m3u8
#EXTINF:-1 tvg-id="NoLogo.be",No Logo TV
http://example.com/nologo.m3u8
#EXTINF:-1 tvg-logo="logos/local.png",Local Only
http://example.com/local.m3u8
'''
XMLTV_TEXT = b'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="1"><display-name>ORF 1</display-name><display-name>ORF eins</display-name>
    <icon src="https://example.com/orf1.png" /></channel>
  <channel id="2"><display-name>No Icon</display-name></channel>
  <programme start="20260101000000 +0000" stop="20260101010000 +0000" channel="1">
    <title>Zeit im Bild</title></programme>
</tv>
'''
TREE_OF_ANOTHER_REPO = json.dumps({
    "tree": [
        {"type": "blob", "path": "logos/be/een-be.png"},
        {"type": "blob", "path": "logos/national_geographic.svg"},
        {"type": "blob", "path": "README.md"},
    ]
}).encode()


class AddedSourceTests(TestCase):
    """Collections added from the page, one reader per shape."""

    def _read(self, kind, body, url="https://example.com/list"):
        with patch("apps.channels.logo_library._download", return_value=body):
            return logo_library.read_source({"type": kind, "url": url, "name": "mine"})

    def test_a_playlist_gives_the_logo_on_each_channel(self):
        entries = self._read(logo_library.M3U, M3U_TEXT)
        by_key = {e["key"]: e for e in entries}
        # "(Belgium) (1080p)" is decoration, like the country box in front
        self.assertEqual(by_key["atv"]["url"], "https://upload.wikimedia.org/atv-be.png")
        # tvg-name and tvg-country are used where a playlist gives them
        self.assertEqual(by_key["belrtl"]["country"], "be")
        # A channel with no logo, or one that is not a link, gives nothing
        self.assertEqual(set(by_key), {"atv", "belrtl"})

    def test_a_guide_gives_each_channels_icon_under_every_name(self):
        entries = self._read(logo_library.XMLTV, XMLTV_TEXT)
        self.assertEqual({e["key"] for e in entries}, {"orf1", "orfeins"})
        self.assertTrue(all(e["url"] == "https://example.com/orf1.png" for e in entries))

    def test_a_json_list_in_either_shape(self):
        as_list = json.dumps([
            {"name": "TFX", "logo": "https://example.com/tfx.png", "country": "FR"},
            {"name": "Not a link", "url": "tfx.png"},
        ]).encode()
        as_map = json.dumps({"Eén": "https://example.com/een.png"}).encode()

        (tfx,) = self._read(logo_library.JSON_LIST, as_list)
        self.assertEqual((tfx["key"], tfx["country"]), ("tfx", "fr"))
        (een,) = self._read(logo_library.JSON_LIST, as_map)
        self.assertEqual(een["key"], "een")

    def test_any_github_repository_of_images(self):
        """At its default branch, whatever that is called, and only the images."""
        entries = self._read(
            logo_library.GITHUB, TREE_OF_ANOTHER_REPO, "https://github.com/someone/logos"
        )
        by_key = {e["key"]: e for e in entries}
        self.assertEqual(set(by_key), {"een", "nationalgeographic"})
        self.assertEqual(by_key["een"]["country"], "be")
        self.assertEqual(
            by_key["een"]["url"],
            "https://raw.githubusercontent.com/someone/logos/HEAD/logos/be/een-be.png",
        )

    def test_a_repository_can_be_given_as_owner_and_name(self):
        self.assertEqual(logo_library._github_repo("someone/logos"), ("someone", "logos"))
        self.assertEqual(
            logo_library._github_repo("https://github.com/someone/logos.git/tree/main"),
            ("someone", "logos"),
        )

    def test_something_far_too_big_is_not_read(self):
        class Huge:
            def raise_for_status(self):
                pass

            def iter_content(self, size):
                while True:
                    yield b"x" * size

        with patch("apps.channels.logo_library.requests.get", return_value=Huge()):
            with self.assertRaises(ValueError):
                logo_library._download("https://example.com/endless")

    def test_added_collections_rank_between_the_two_built_in(self):
        """Chosen on purpose, so ahead of iptv-org; its links, unlike tv-logos', unknown."""
        mine = {"source": "mine", "country": "", "format": "PNG"}
        ours = {"source": logo_library.TV_LOGOS, "country": "", "format": "PNG"}
        theirs = {"source": logo_library.IPTV_ORG, "country": "", "format": "PNG"}
        ranked = sorted([theirs, mine, ours], key=lambda e: logo_library._rank(e, ""))
        self.assertEqual([e["source"] for e in ranked], [logo_library.TV_LOGOS, "mine", logo_library.IPTV_ORG])


class SourcesViewTests(TestCase):
    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )

    def _post(self, **body):
        with patch("apps.channels.logo_library._download", return_value=M3U_TEXT):
            return self.client_api.post(
                "/api/channels/logo-library/sources/", body, format="json"
            )

    def test_the_built_in_ones_are_listed(self):
        data = self.client_api.get("/api/channels/logo-library/sources/").json()
        self.assertEqual(
            [s["name"] for s in data["sources"]],
            [logo_library.TV_LOGOS, logo_library.IPTV_ORG],
        )
        self.assertEqual(set(data["types"]), set(logo_library.SOURCE_TYPES))

    def test_a_collection_is_checked_before_it_is_kept(self):
        response = self._post(type="m3u", url="https://example.com/be.m3u", check=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 2)
        # Only looked at, not kept
        self.assertEqual(logo_library.load_sources()["added"], [])

    def test_adding_one_keeps_it(self):
        response = self._post(type="m3u", url="https://example.com/be.m3u", name="Belgium")
        self.assertEqual(response.status_code, 200, response.content)
        (added,) = logo_library.load_sources()["added"]
        self.assertEqual((added["name"], added["type"]), ("Belgium", "m3u"))

    def test_one_with_nothing_in_it_is_not_kept(self):
        with patch("apps.channels.logo_library._download", return_value=b"#EXTM3U\n"):
            response = self.client_api.post(
                "/api/channels/logo-library/sources/",
                {"type": "m3u", "url": "https://example.com/empty.m3u"},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no logos", response.json()["error"])

    def test_one_that_is_not_what_it_was_said_to_be_says_so(self):
        with patch("apps.channels.logo_library._download", return_value=b"not json"):
            response = self.client_api.post(
                "/api/channels/logo-library/sources/",
                {"type": "json", "url": "https://example.com/x"},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Could not read that as json", response.json()["error"])

    def test_a_built_in_one_can_be_switched_off_and_is_then_not_downloaded(self):
        self.client_api.patch(
            "/api/channels/logo-library/sources/",
            {"id": logo_library.IPTV_ORG, "enabled": False},
            format="json",
        )
        self.assertIn(logo_library.IPTV_ORG, logo_library.load_sources()["off"])

        cache.delete(logo_library.INDEX_KEY)
        with patch("apps.channels.logo_library.requests.get", side_effect=both_collections):
            built = logo_library.build_index()
        self.assertEqual(set(built["counts"]), {logo_library.TV_LOGOS})

    def test_an_added_one_is_part_of_the_next_download(self):
        self._post(type="m3u", url="https://example.com/be.m3u", name="Belgium")

        cache.delete(logo_library.INDEX_KEY)
        with patch(
            "apps.channels.logo_library.requests.get", side_effect=both_collections
        ), patch("apps.channels.logo_library._download", return_value=M3U_TEXT):
            built = logo_library.build_index()

        self.assertEqual(built["counts"]["Belgium"], 2)
        index = logo_library.load_index()
        self.assertTrue(logo_library.suggestions_for("┃BE┃ Bel RTL", index))

    def test_an_added_one_can_be_removed(self):
        self._post(type="m3u", url="https://example.com/be.m3u", name="Belgium")
        (added,) = logo_library.load_sources()["added"]
        self.client_api.delete(f"/api/channels/logo-library/sources/?id={added['id']}")
        self.assertEqual(logo_library.load_sources()["added"], [])

    def test_two_cannot_share_a_name(self):
        self._post(type="m3u", url="https://example.com/be.m3u", name="Belgium")
        response = self._post(type="m3u", url="https://example.com/other.m3u", name="Belgium")
        self.assertEqual(response.status_code, 400)


class WhatDispatcharrAlreadyHasTests(TestCase):
    """
    A channel's own guide entry and streams, which are bound to it by its mapping rather
    than found by its name, and so come before anything a collection has.
    """

    def setUp(self):
        from apps.channels.models import ChannelStream, Stream
        from apps.epg.models import EPGData

        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )
        # A real Schedules Direct station logo, as your guide carries them
        self.icon = (
            "https://schedulesdirect-api20141201-logos.s3.dualstack.us-east-1."
            "amazonaws.com/stationLogos/s53814_dark_360w_270h.png"
        )
        cache.delete(logo_library.GUIDE_ICONS_KEY)
        epg = EPGData.objects.create(tvg_id="een.be", name="Eén", icon_url=self.icon)
        self.channel = Channel.objects.create(
            channel_number=1, name="┃BE┃ Eén", epg_data=epg
        )
        stream = Stream.objects.create(
            name="BE: EEN HD", url="http://provider/een", logo_url="https://provider/een.png"
        )
        ChannelStream.objects.create(channel=self.channel, stream=stream, order=0)

    def _row(self, show="all"):
        data = self.client_api.get(f"/api/channels/logo-library/?show={show}").json()
        return next(r for r in data["channels"] if r["channel_id"] == self.channel.id)

    def test_the_collections_come_first_then_the_playlist_then_the_guide(self):
        """The playlist's logo is usually the one being replaced, so it is not offered first."""
        built_index()
        sources = [s["source"] for s in self._row()["suggestions"]]
        self.assertEqual(sources[0], logo_library.TV_LOGOS)
        self.assertLess(sources.index(logo_library.YOUR_PLAYLIST), sources.index(logo_library.YOUR_GUIDE))
        self.assertEqual(sources[-1], logo_library.YOUR_GUIDE)

    def test_every_guide_is_looked_in_not_only_the_one_it_is_mapped_to(self):
        """Each guide source names channels its own way and has its own icons."""
        from apps.epg.models import EPGData, EPGSource

        other = EPGSource.objects.create(name="Another guide", source_type="xmltv")
        EPGData.objects.create(
            tvg_id="een.other", name="EEN", icon_url="https://other.example/een.png",
            epg_source=other,
        )
        # Another channel's icon in that guide is not this channel's
        EPGData.objects.create(
            tvg_id="canvas.other", name="Canvas", icon_url="https://other.example/canvas.png",
            epg_source=other,
        )
        cache.delete(logo_library.GUIDE_ICONS_KEY)
        built_index()

        guide = [s for s in self._row()["suggestions"] if s["source"] == logo_library.YOUR_GUIDE]
        urls = [s["url"] for s in guide]
        # The one it is mapped to first, then the same channel in the other guide
        self.assertEqual(urls, [self.icon, "https://other.example/een.png"])
        self.assertEqual(guide[1]["guide"], "Another guide")

    def test_they_are_offered_before_anything_is_downloaded(self):
        """Nothing needs fetching for them: they are already in Dispatcharr."""
        cache.delete(logo_library.INDEX_KEY)
        row = self._row(show="suggested")
        self.assertEqual(
            [s["source"] for s in row["suggestions"]],
            [logo_library.YOUR_PLAYLIST, logo_library.YOUR_GUIDE],
        )

    def test_the_logo_it_already_has_is_not_offered_back(self):
        self.channel.logo = Logo.objects.create(name="Eén", url=self.icon)
        self.channel.save()
        built_index()
        urls = [s["url"] for s in self._row()["suggestions"]]
        self.assertNotIn(self.icon, urls)

    def test_a_guide_is_searched_too_and_comes_first(self):
        built_index()
        data = self.client_api.get("/api/channels/logo-library/search/?q=een").json()
        self.assertEqual(data["results"][0]["url"], self.icon)
        self.assertEqual(data["results"][0]["source"], logo_library.YOUR_GUIDE)

    def test_a_guide_is_searched_with_accents_folded(self):
        """The database compares accents as they are: "een" did not find "Eén"."""
        results = logo_library.search_guides("een")
        self.assertEqual([r["url"] for r in results], [self.icon])

    def test_a_guide_is_searched_even_before_anything_is_downloaded(self):
        cache.delete(logo_library.INDEX_KEY)
        data = self.client_api.get("/api/channels/logo-library/search/?q=een").json()
        self.assertFalse(data["built"])
        self.assertEqual([r["url"] for r in data["results"]], [self.icon])
