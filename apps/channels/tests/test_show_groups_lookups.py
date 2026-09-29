# Show Groups' online sources, on recorded answers: no network.
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
