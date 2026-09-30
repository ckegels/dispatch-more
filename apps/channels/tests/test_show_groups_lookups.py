# Show Groups' online sources, on recorded answers: no network.
import json
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

from apps.channels.show_groups import lookups


class Web:
    """Answers GET requests from a table of (url fragment, answer); records what was asked."""

    def __init__(self, table):
        self.table = table
        self.asked = []

    def __call__(self, url, headers=None):
        self.asked.append((unquote(url), headers or {}))
        for fragment, answer in self.table:
            if fragment in unquote(url):
                return answer(url) if callable(answer) else answer
        return None


def serve(table):
    web = Web(table)
    patches = [mock.patch.object(lookups, "get_json", web), mock.patch.object(lookups, "PAUSE", 0)]
    return web, patches


class Case(unittest.TestCase):
    def web(self, table):
        web, patches = serve(table)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return web


class TVmaze(Case):
    def test_the_exact_show(self):
        self.web([("singlesearch/shows?q=Great British Menu",
                   {"name": "Great British Menu", "genres": ["Food"]})])
        self.assertEqual(lookups.tvmaze("Great British Menu"),
                         {"name": "Great British Menu", "genres": ["Food"]})

    def test_a_fuzzy_answer_about_another_show_is_refused(self):
        self.web([("singlesearch", {"name": "My True Crime Story", "genres": ["Crime"]}),
                  ("search/shows", [{"score": 0.9, "show": {"name": "My True Crime Story"}}])])
        self.assertIsNone(lookups.tvmaze("True Crime Story"))

    def test_the_search_finds_it_when_singlesearch_picks_another(self):
        self.web([("singlesearch", {"name": "Next Level Chef Australia", "genres": []}),
                  ("search/shows", [{"show": {"name": "Next Level Chef Australia"}},
                                    {"show": {"name": "Next Level Chef", "genres": ["Food"]}}])])
        self.assertEqual(lookups.tvmaze("Next Level Chef")["genres"], ["Food"])


class Wikipedia(Case):
    def pages(self, *pages):
        return {"query": {"pages": list(pages)}}

    def test_a_programme_in_dutch(self):
        page = {"title": "Dagelijkse kost", "categories": [
            {"title": "Categorie:Vlaams kookprogramma"}, {"title": "Categorie:Programma van Eén"}]}
        web = self.web([("en.wikipedia.org", self.pages({"title": "Dagelijkse kost", "missing": True})),
                        ("en.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&list=search",
                         {"query": {"search": []}}),
                        ("nl.wikipedia.org", self.pages(page))])
        answer = lookups.wikipedia("Dagelijkse kost")
        self.assertEqual(answer["genres"], ["Vlaams kookprogramma", "Programma van Eén"])
        self.assertEqual(answer["language"], "nl")
        # Asked the title and its disambiguated forms in one request
        first = web.asked[0][0]
        self.assertIn("Dagelijkse kost|Dagelijkse kost (TV series)", first)

    def test_a_redirect_to_the_chef_is_refused(self):
        chef = {"title": "Jamie Oliver", "categories": [
            {"title": "Category:British television chefs"}, {"title": "Category:1975 births"}]}
        self.web([("wikipedia.org", self.pages(chef))])
        self.assertIsNone(lookups.wikipedia("Jamie's 15 Minute Meals", languages=("en",)))

    def test_a_person_is_refused_even_under_the_same_name(self):
        person = {"title": "Sandra Bekkari", "categories": [
            {"title": "Categorie:Belgisch televisiepresentator"}, {"title": "Categorie:Levend persoon"}]}
        self.web([("wikipedia.org", self.pages(person))])
        self.assertIsNone(lookups.wikipedia("Sandra Bekkari", languages=("nl",)))

    def test_a_page_that_is_not_about_television_is_refused(self):
        dish = {"title": "Pasta & Risotto", "categories": [{"title": "Category:Italian cuisine"}]}
        self.web([("wikipedia.org", self.pages(dish))])
        self.assertIsNone(lookups.wikipedia("Pasta & Risotto", languages=("en",)))

    def test_capitals_in_the_guide_are_found_by_search(self):
        page = {"title": "Klaar in 20 minuten", "categories": [{"title": "Categorie:Kookprogramma"}]}

        def by_titles(url):
            titles = parse_qs(urlparse(url).query).get("titles", [""])[0]
            return self.pages(page) if titles == "Klaar in 20 minuten" else self.pages()

        self.web([("list=search", {"query": {"search": [{"title": "Klaar in 20 minuten"}]}}),
                  ("prop=categories", by_titles)])
        answer = lookups.wikipedia("KLAAR IN 20 minuten", languages=("nl",))
        self.assertEqual(answer["genres"], ["Kookprogramma"])


class Wikidata(Case):
    def test_a_television_programme_with_that_label(self):
        self.web([("wbsearchentities", {"search": [{"id": "Q1", "label": "Silvia kocht"}]}),
                  ("EntityData/Q1", {"entities": {"Q1": {"claims": {"P31": [
                      {"mainsnak": {"datavalue": {"value": {"id": "Q5398426"}}}}]}}}}),
                  ("wbgetentities", {"entities": {"Q5398426": {"labels": {
                      "en": {"value": "cooking television program"}}}}})])
        self.assertEqual(lookups.wikidata("Silvia kocht")["genres"], ["cooking television program"])

    def test_a_film_with_another_label_is_refused(self):
        self.web([("wbsearchentities", {"search": [{"id": "Q2", "label": "Milk Street (film)"}]}),
                  ("EntityData", {"entities": {}})])
        self.assertIsNone(lookups.wikidata("Christopher Kimball's Milk Street Television"))


class TMDB(Case):
    def test_keywords_count_as_genres(self):
        web = self.web([("search/tv", {"results": [{"id": 7, "name": "Masterchef New Zealand"}]}),
                        ("tv/7", {"genres": [{"name": "Reality"}], "keywords": {"results": [
                            {"name": "cooking competition"}, {"name": "chef"}]}})])
        answer = lookups.tmdb("Masterchef New Zealand", "0123456789abcdef0123456789abcdef")
        self.assertEqual(answer["genres"], ["Reality", "cooking competition", "chef"])
        self.assertIn("api_key=0123456789abcdef0123456789abcdef", web.asked[0][0])

    def test_a_read_token_goes_in_a_header(self):
        web = self.web([("search/tv", {"results": []})])
        lookups.tmdb("Anything", "ey" + "x" * 200)
        self.assertEqual(web.asked[0][1]["Authorization"], "Bearer ey" + "x" * 200)

    def test_no_key_asks_nothing(self):
        web = self.web([])
        self.assertIsNone(lookups.tmdb("Anything", ""))
        self.assertEqual(web.asked, [])
        self.assertNotIn("tmdb", lookups.enabled_sources({"tmdb_key": ""}))
        self.assertIn("tmdb", lookups.enabled_sources({"tmdb_key": "abc"}))

    def test_another_show_is_refused(self):
        self.web([("search/tv", {"results": [{"id": 1, "name": "MasterChef Australia"}]})])
        self.assertIsNone(lookups.tmdb("Masterchef New Zealand", "k" * 32))


if __name__ == "__main__":
    unittest.main()


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opened:
    """urlopen from a table of (url fragment, answer or HTTP status); records the requests."""

    def __init__(self, table):
        self.table = table
        self.requests = []

    def __call__(self, request, timeout=None):
        import urllib.error

        self.requests.append(request)
        for fragment, answer in self.table:
            if fragment in unquote(request.full_url):
                if isinstance(answer, int):
                    raise urllib.error.HTTPError(request.full_url, answer, "no", {}, None)
                return FakeResponse(answer)
        raise urllib.error.HTTPError(request.full_url, 404, "no", {}, None)


class Keyed(Case):
    def opened(self, table):
        opened = Opened(table)
        for patch in (mock.patch("urllib.request.urlopen", opened), mock.patch.object(lookups, "PAUSE", 0)):
            patch.start()
            self.addCleanup(patch.stop)
        lookups._tvdb.update(key=None, token=None, at=0.0)
        return opened

    def test_thetvdb_logs_in_once_and_reads_the_genres(self):
        opened = self.opened([
            ("/login", {"data": {"token": "t1"}}),
            ("/search?type=series", {"data": [
                {"name": "Chopped Junior", "tvdb_id": "1"},
                {"name": "Chopped", "tvdb_id": "2", "aliases": []}]}),
            ("/series/2/extended", {"data": {"genres": [{"name": "Food"}, {"name": "Game Show"}]}}),
        ])
        self.assertEqual(lookups.tvdb("Chopped", "k"), {"name": "Chopped", "genres": ["Food", "Game Show"]})
        lookups.tvdb("Chopped", "k")
        self.assertEqual(sum("/login" in r.full_url for r in opened.requests), 1)
        self.assertEqual(json.loads(opened.requests[0].data), {"apikey": "k"}, "no PIN unless given")

    def test_thetvdb_a_refused_key_is_not_a_show_nobody_knows(self):
        self.opened([("/login", 401)])
        with self.assertRaises(lookups.Unavailable):
            lookups.tvdb("Chopped", "wrong")

    def test_thetvdb_by_another_name(self):
        self.opened([
            ("/login", {"data": {"token": "t1"}}),
            ("/search", {"data": [{"name": "Heel Holland Bakt", "tvdb_id": "9",
                                   "aliases": ["Holland Bakes"]}]}),
            ("/series/9/extended", {"data": {"genres": [{"name": "Food"}]}}),
        ])
        self.assertEqual(lookups.tvdb("Holland Bakes", "k")["genres"], ["Food"])

    def test_trakt(self):
        opened = self.opened([("api.trakt.tv/search/show", [
            {"show": {"title": "Chopped After Hours", "genres": ["reality"]}},
            {"show": {"title": "Chopped", "genres": ["reality", "game-show"]}}])])
        self.assertEqual(lookups.trakt("Chopped", "cid")["genres"], ["reality", "game-show"])
        self.assertEqual(opened.requests[0].get_header("Trakt-api-key"), "cid")

    def test_trakt_refused(self):
        self.opened([("api.trakt.tv", 401)])
        with self.assertRaises(lookups.Unavailable):
            lookups.trakt("Chopped", "wrong")

    def test_omdb_and_its_daily_limit(self):
        self.web([("t=Planet Earth", {"Response": "True", "Title": "Planet Earth", "Genre": "Documentary"}),
                  ("t=Other", {"Response": "False", "Error": "Series not found!"}),
                  ("t=Late", {"Response": "False", "Error": "Request limit reached!"})])
        self.assertEqual(lookups.omdb("Planet Earth", "k")["genres"], ["Documentary"])
        self.assertIsNone(lookups.omdb("Other", "k"))
        with self.assertRaises(lookups.Unavailable):
            lookups.omdb("Late", "k")

    def test_only_the_sources_with_a_key(self):
        self.assertEqual(lookups.enabled_sources({}), ("tvmaze", "wikidata", "wikipedia"))
        self.assertIn("trakt", lookups.enabled_sources({"trakt_client_id": "x"}))


class FailingIsNotUnknown(Case):
    """A failed request is not "this database does not know the show"."""

    def opened(self, table):
        opened = Opened(table)
        for patch in (mock.patch("urllib.request.urlopen", opened), mock.patch.object(lookups, "PAUSE", 0),
                      mock.patch.object(lookups.time, "sleep", lambda s: None)):
            patch.start()
            self.addCleanup(patch.stop)
        lookups._tvdb.update(key=None, token=None, at=0.0)
        return opened

    def test_not_found_is_unknown(self):
        self.opened([("singlesearch", 404), ("search/shows", [])])
        self.assertIsNone(lookups.tvmaze("Nobody Knows This"))

    def test_a_server_error_is_unavailable(self):
        opened = self.opened([("singlesearch", 503)])
        with self.assertRaises(lookups.Unavailable):
            lookups.tvmaze("Great British Menu")
        self.assertEqual(len(opened.requests), 3, "tried three times first")

    def test_thetvdb_timing_out_is_unavailable(self):
        self.opened([("/login", {"data": {"token": "t"}}), ("/search", 500)])
        with self.assertRaises(lookups.Unavailable):
            lookups.tvdb("Ben & Holly's Little Kingdom", "k")

    def test_thetvdb_genres_failing_is_unavailable(self):
        self.opened([("/login", {"data": {"token": "t"}}),
                     ("/search", {"data": [{"name": "Bar Rescue", "tvdb_id": "5"}]}),
                     ("/series/5/extended", 502)])
        with self.assertRaises(lookups.Unavailable):
            lookups.tvdb("Bar Rescue", "k")
