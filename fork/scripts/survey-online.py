# Are the online databases good enough to find every cooking show? This takes the titles in your
# own guide that carry no category, asks each layer in turn what they are, and counts how many
# airings each layer would name: your other guides, TVmaze, Wikidata, and finally the words in
# the title itself. It also prints what it took for cooking, and every answer whose title does
# not really match -- those are the ones that would put a wrong channel in a group. Read-only,
# and it asks the databases slowly (TVmaze allows 20 questions per 10 seconds).
#
#   bash dispatcharr-shell.sh shell < survey-online.py
#   TOP=300 AHEAD=72 bash dispatcharr-shell.sh shell < survey-online.py
#
# TOP: how many titles to look up online, busiest first (default 150). AHEAD: hours of guide to
# look at (default 48). CACHE: where answers are kept between runs, so a second run is free.
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import timedelta

from django.utils import timezone

from apps.epg.models import ProgramData

AHEAD = int(os.environ.get("AHEAD", "48"))
TOP = int(os.environ.get("TOP", "150"))
CACHE = os.environ.get("CACHE", "/tmp/show-lookups.json")
AGENT = {"User-Agent": "Dispatch More show groups survey (github.com/ckegels/dispatch-more)"}
STRUCTURAL = {"series", "episode", "special", "show", "programme", "program"}
# What a cooking programme is called, in the languages of these guides
COOKING = ("cooking", "food", "culinary", "baking", "gastronom", "kookprogramma")
IN_TITLE = ("kitchen", "keuken", "kuchen", "kuche", "kochen", "kocht", "kook", "kookt", "bakt",
            "bakes", "baking", "recipe", "recept", "chef", "cuisine", "cook", "grill", "bbq",
            "restaurant", "menu", "dinner", "diner")
# Words that make a title with a cooking word in it not a cooking programme
NOT_COOKING = ("hell's kitchen", "kitchen nightmares", "nightmare", "murder", "crime", "news")


def plain(title):
    text = unicodedata.normalize("NFKD", (title or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.split(r" - |: ", text)[0]
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


answers = {}
if os.path.exists(CACHE):
    try:
        answers = json.load(open(CACHE))
    except Exception:
        answers = {}


def ask(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(5)
                continue
            return None
        except Exception:
            time.sleep(2)
    return None


def tvmaze(title):
    """The show's genres, and the name TVmaze thinks we asked about -- so a wrong match shows."""
    found = ask("https://api.tvmaze.com/singlesearch/shows?q=" + urllib.parse.quote(title))
    if found:
        return found.get("name") or "", found.get("genres") or []
    hits = ask("https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(title)) or []
    if hits and hits[0].get("score", 0) > 0.85:
        show = hits[0]["show"]
        return show.get("name") or "", show.get("genres") or []
    return "", []


def wikidata(title):
    """Wikidata knows the European ones TVmaze misses, but its search answers something for
    almost anything, so only a television programme whose name really matches is taken."""
    for language in ("en", "de", "nl", "fr"):
        found = ask("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json&limit=3"
                    "&language=%s&uselang=en&search=%s" % (language, urllib.parse.quote(title)))
        for hit in (found or {}).get("search", []):
            if plain(hit.get("label") or "") != plain(title):
                continue
            entity = ask("https://www.wikidata.org/wiki/Special:EntityData/%s.json" % hit["id"])
            if not entity:
                continue
            claims = entity["entities"][hit["id"]]["claims"]
            ids = [c["mainsnak"]["datavalue"]["value"]["id"]
                   for prop in ("P31", "P136") for c in claims.get(prop, [])
                   if c["mainsnak"].get("datavalue")]
            if not ids:
                continue
            labels = ask("https://www.wikidata.org/w/api.php?action=wbgetentities&props=labels"
                         "&languages=en&format=json&ids=" + "|".join(ids[:8])) or {}
            names = [e["labels"].get("en", {}).get("value", "") for e in labels.get("entities", {}).values()]
            if any("television" in n or "series" in n or "program" in n for n in names):
                return hit.get("label") or "", names
    return "", []


def look_up(title):
    if title in answers:
        return answers[title]
    name, genres = tvmaze(title)
    where = "tvmaze" if genres else ""
    time.sleep(0.5)
    if not genres:
        name, genres = wikidata(title)
        where = "wikidata" if genres else ""
        time.sleep(0.3)
    answers[title] = {"name": name, "genres": genres, "where": where}
    return answers[title]


now = timezone.now()
window = ProgramData.objects.filter(
    start_time__gte=now - timedelta(hours=6), start_time__lte=now + timedelta(hours=AHEAD)
).only("title", "custom_properties")

known = defaultdict(set)    # from the guides that do say what a programme is
without = Counter()         # the ones that do not, and how often they are on
written = {}
for program in window.iterator():
    key = plain(program.title)
    if not key:
        continue
    written.setdefault(key, program.title)
    categories = [str(c).strip() for c in (program.custom_properties or {}).get("categories") or () if str(c).strip()]
    real = [c for c in categories if c.lower() not in STRUCTURAL]
    if real:
        known[key].update(real)
    elif not categories:
        without[key] += 1

busiest = without.most_common(TOP)
rest = sum(n for _, n in without.items()) - sum(n for _, n in busiest)
print(f"{len(without)} titles without a category in the next {AHEAD} h, {sum(without.values())} airings.")
print(f"Looking up the {len(busiest)} busiest ({sum(n for _, n in busiest)} airings); "
      f"{rest} airings are on titles further down the list.\n")

named = Counter()           # layer -> airings it could name
cooking = defaultdict(list)  # layer -> the titles it called cooking
doubtful = []
for index, (key, airings) in enumerate(busiest, 1):
    title = written[key]
    words = key
    if key in known:
        genres, layer = sorted(known[key]), "your other guides"
    else:
        found = look_up(title)
        genres, layer = found["genres"], found["where"] or ""
        if genres and plain(found["name"]) != key:
            doubtful.append((title, found["name"], ", ".join(genres), layer))
        if not genres:
            if any(w in words for w in IN_TITLE) and not any(w in words for w in NOT_COOKING):
                genres, layer = ["(from the title)"], "the words in the title"
            else:
                layer = "nothing"
    named[layer] += airings
    blob = " ".join(genres).lower()
    if any(w in blob for w in COOKING) or layer == "the words in the title":
        cooking[layer].append((title, airings, ", ".join(genres)))
    if index % 25 == 0:
        print(f"  ... {index} of {len(busiest)}")
        try:
            json.dump(answers, open(CACHE, "w"))
        except Exception:
            pass

try:
    json.dump(answers, open(CACHE, "w"))
except Exception:
    pass

looked = sum(n for _, n in busiest)
print("\nWhat named them, by airings:")
for layer, airings in named.most_common():
    print(f"  {layer or 'nothing':24} {airings:6}  {airings * 100 // max(1, looked):3}%")

print("\nCooking programmes found:")
for layer in sorted(cooking, key=lambda l: -len(cooking[l])):
    print(f"  -- {layer} ({len(cooking[layer])} titles)")
    for title, airings, genres in sorted(cooking[layer], key=lambda c: -c[1])[:15]:
        print(f"     {title[:44]:44} {airings:3} airings  {genres[:34]}")

if doubtful:
    print(f"\nAnswers about another programme than we asked ({len(doubtful)}), "
          "the kind that would put a wrong channel in a group:")
    for title, name, genres, layer in doubtful[:20]:
        print(f"  asked {title[:34]:34} got {name[:30]:30} {genres[:26]} ({layer})")
