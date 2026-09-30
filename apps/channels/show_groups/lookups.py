# What the online databases say a title is (the plugin's HANDOVER.md §4 layers 3-4, §5). Each source is asked
# for the title exactly as the guide writes it and answers only about a programme whose name is
# that title: the surveys found fuzzy search answers about another programme ("True Crime
# Story" came back as "My True Crime Story"), and a wrong answer puts a wrong channel in a group.
# No Django here, so the rules are tested without a server or the network.
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .matching import fold, plain

# Wikimedia asks every client to say who it is; no personal details, only the project
AGENT = "DispatchMore-ShowGroups/1.0 (github.com/ckegels/dispatch-more)"
PAUSE = 0.5  # seconds between requests: TVmaze allows 20 per 10 seconds, the others more

SOURCES = ("tvmaze", "wikidata", "wikipedia", "tmdb", "tvdb", "trakt", "omdb")
# The key each source needs (Settings → System → Service keys); the others are free
KEYED = {"tmdb": "tmdb_key", "tvdb": "tvdb_key", "trakt": "trakt_client_id", "omdb": "omdb_key"}


class Unavailable(Exception):
    """The source could not be asked (a key refused, a daily limit reached): nothing is known
    about the title, so nothing is recorded and it is asked again later."""


def get_json(url, headers=None):
    """One GET, with a retry on a rate limit, a server error or a dropped connection. None only
    when the answer is "not found"; the caller then records "asked, nobody knew".

    Anything else that keeps an answer from coming raises Unavailable, so nothing is recorded
    and the title is asked again later. It used to return None for those too, and every
    timeout was written down as "this database does not know the show" for 30 days: on the
    user's server TheTVDB's first runs recorded Ben & Holly's Little Kingdom and Sofia the
    First as unknown, both of which it knows."""
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, **(headers or {})})
    host = urllib.parse.urlparse(url).netloc
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                return None
            if exc.code in (401, 403):
                raise Unavailable(f"{host} refused the request (a key?)")
            time.sleep(5 * (attempt + 1) if exc.code == 429 else 2)
        except (OSError, ValueError):
            time.sleep(2)
    raise Unavailable(f"{host} could not be asked just now")


def _ask(url, headers=None):
    answer = get_json(url, headers)
    time.sleep(PAUSE)
    return answer


def _q(text):
    return urllib.parse.quote(text)


# ---- TVmaze: genre "Food" marks cooking. Free, no key. -----------------------------------------

def tvmaze(title):
    key = plain(title)
    show = _ask("https://api.tvmaze.com/singlesearch/shows?q=" + _q(title))
    if not show or plain(show.get("name")) != key:
        hits = _ask("https://api.tvmaze.com/search/shows?q=" + _q(title)) or []
        show = next((h["show"] for h in hits if plain(h.get("show", {}).get("name")) == key), None)
    if not show:
        return None
    return {"name": show.get("name") or "", "genres": show.get("genres") or []}


# ---- Wikidata: genre "cooking show", in many languages. Free. ----------------------------------

def wikidata(title, languages=("en", "nl", "de", "fr")):
    """Wikidata's search answers something for almost anything, so a hit counts only when its
    label is the title and it is a television programme (handover §3.6: it called "Milk Street"
    a drama film)."""
    key = plain(title)
    for language in languages:
        found = _ask("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json"
                     f"&limit=3&language={language}&uselang=en&search={_q(title)}")
        for hit in (found or {}).get("search", []):
            if plain(hit.get("label")) != key:
                continue
            entity = _ask(f"https://www.wikidata.org/wiki/Special:EntityData/{hit['id']}.json")
            try:
                claims = entity["entities"][hit["id"]]["claims"]
            except (TypeError, KeyError):
                continue
            ids = [c["mainsnak"]["datavalue"]["value"]["id"]
                   for prop in ("P31", "P136") for c in claims.get(prop, [])
                   if c.get("mainsnak", {}).get("datavalue")]
            if not ids:
                continue
            labels = _ask("https://www.wikidata.org/w/api.php?action=wbgetentities&props=labels"
                          "&languages=en&format=json&ids=" + "|".join(ids[:8])) or {}
            names = [e.get("labels", {}).get("en", {}).get("value", "")
                     for e in labels.get("entities", {}).values()]
            names = [n for n in names if n]
            if any(w in n for n in names for w in ("television", "series", "program", "show")):
                return {"name": hit.get("label") or "", "genres": names}
    return None


# ---- Wikipedia: the article's categories. Free, per language. ----------------------------------

# How a Wikipedia category says "this article is a television programme", per language
# (a broadcaster's own list counts: "Channel 4 original programming", "Programma van Eén")
TV_MARKERS = ("television series", "television program", "television show", "tv series",
              "tv program", "original programming", "televisieprogramma", "televisieserie",
              "programma van", "kookprogramma", "fernsehsendung", "fernsehserie", "sendung",
              "emission de television", "serie televisee", "emission culinaire")
# ...and "this article is about a person", which a show's title sometimes redirects to
PERSON_MARKERS = ("births", "living people", "deaths", "geboren", "levend persoon",
                  "lebende person", "naissance", "deces", "personnalite")
# Parenthetical forms Wikipedia uses to tell a programme apart from a same-named thing
DISAMBIGUATION = {
    "en": ("TV series", "TV programme", "TV program", "British TV series", "American TV series"),
    "nl": ("televisieprogramma", "Vlaams televisieprogramma", "Nederlands televisieprogramma"),
    "de": ("Fernsehsendung", "Fernsehserie"),
    "fr": ("émission de télévision", "série télévisée"),
}


def _category_name(title):
    return title.split(":", 1)[1] if ":" in title else title


def _pages_with_categories(language, titles):
    found = _ask(f"https://{language}.wikipedia.org/w/api.php?action=query&format=json"
                 "&formatversion=2&redirects=1&prop=categories&clshow=!hidden&cllimit=max"
                 "&titles=" + _q("|".join(titles)))
    pages = (found or {}).get("query", {}).get("pages", [])
    return {p.get("title"): [_category_name(c.get("title", "")) for c in p.get("categories", [])]
            for p in pages if not p.get("missing") and not p.get("invalid")}


def _programme(page_title, categories, key):
    """A page counts when its name is the asked title (a redirect to a chef's biography is not)
    and its categories say it is a television programme and not a person."""
    if plain(page_title) != key:
        return False
    folded = [" ".join(re.sub(r"[^a-z0-9 ]+", " ", fold(c)).split()) for c in categories]
    return (any(m in f for f in folded for m in TV_MARKERS)
            and not any(m in f for f in folded for m in PERSON_MARKERS))


def wikipedia(title, languages=("en", "nl", "de", "fr")):
    key = plain(title)
    for language in languages:
        # The title as written and as Wikipedia would disambiguate it, in one request
        variants = [title] + [f"{title} ({d})" for d in DISAMBIGUATION.get(language, ())]
        pages = _pages_with_categories(language, variants)
        # Guides often write titles in capitals or lower case, which an exact lookup misses
        if not pages:
            found = _ask(f"https://{language}.wikipedia.org/w/api.php?action=query&format=json"
                         f"&formatversion=2&list=search&srlimit=3&srsearch={_q(title)}")
            named = [h["title"] for h in (found or {}).get("query", {}).get("search", [])
                     if plain(h.get("title")) == key]
            pages = _pages_with_categories(language, named) if named else {}
        for page_title, categories in pages.items():
            if _programme(page_title, categories, key):
                return {"name": page_title, "genres": categories, "language": language}
    return None


# ---- TMDB: genres plus keywords ("cooking competition", "baking"). Free key. -------------------

def tmdb(title, key_or_token):
    """TMDB's genres cannot say "cooking" (handover §5), but its keywords can. Accepts a v3 API
    key (32 characters) or a v4 read access token."""
    if not key_or_token:
        return None
    headers, auth = {}, ""
    if len(key_or_token) <= 40:
        auth = "&api_key=" + _q(key_or_token)
    else:
        headers = {"Authorization": "Bearer " + key_or_token}
    key = plain(title)
    found = _ask("https://api.themoviedb.org/3/search/tv?include_adult=false&query="
                 + _q(title) + auth, headers)
    show = next((r for r in (found or {}).get("results", [])
                 if key in (plain(r.get("name")), plain(r.get("original_name")))), None)
    if not show:
        return None
    detail = _ask(f"https://api.themoviedb.org/3/tv/{show['id']}?append_to_response=keywords"
                  + auth, headers) or {}
    genres = [g.get("name") for g in detail.get("genres", []) if g.get("name")]
    keywords = [k.get("name") for k in detail.get("keywords", {}).get("results", []) if k.get("name")]
    return {"name": show.get("name") or "", "genres": genres + keywords}


# ---- TheTVDB: genres Food, Travel, Home and Garden... Free key, some with a PIN. ------------------

TVDB = "https://api4.thetvdb.com/v4"
# One login per worker process: the token lasts a month, and is kept here for 20 days
_tvdb = {"key": None, "token": None, "at": 0.0}
TVDB_TOKEN_SECONDS = 20 * 24 * 3600


def _tvdb_login(key, pin=""):
    body = {"apikey": key, **({"pin": pin} if pin else {})}
    request = urllib.request.Request(
        TVDB + "/login", data=json.dumps(body).encode(), method="POST",
        headers={"User-Agent": AGENT, "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            token = (json.load(response).get("data") or {}).get("token")
    except urllib.error.HTTPError as exc:
        time.sleep(PAUSE)
        if exc.code in (400, 401, 403):
            raise Unavailable("TheTVDB refused the key (or wants its PIN)")
        token = None
    except (OSError, ValueError):
        token = None
    time.sleep(PAUSE)
    _tvdb.update(key=key, token=token, at=time.time())
    if not token:
        raise Unavailable("TheTVDB could not be reached")
    return token


def _tvdb_get(path, key, pin):
    """One GET with the token, logging in first (or again after a refusal)."""
    for attempt in range(2):
        fresh = _tvdb["key"] == key and _tvdb["token"] and time.time() - _tvdb["at"] < TVDB_TOKEN_SECONDS
        token = _tvdb["token"] if fresh and attempt == 0 else _tvdb_login(key, pin)
        if not token:
            return None
        request = urllib.request.Request(TVDB + path, headers={
            "User-Agent": AGENT, "Accept": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                answer = json.load(response)
            time.sleep(PAUSE)
            return answer
        except urllib.error.HTTPError as exc:
            time.sleep(PAUSE)
            if exc.code == 401 and attempt == 0:
                _tvdb["token"] = None
                continue
            if exc.code in (404, 410):
                return None
            if exc.code == 429:
                time.sleep(5)
            raise Unavailable(f"TheTVDB answered {exc.code}")
        except (OSError, ValueError):
            raise Unavailable("TheTVDB could not be reached")
    raise Unavailable("TheTVDB refused the token twice")


def tvdb(title, key, pin=""):
    """A series whose name, or one of its other names, is the title, and its genres."""
    if not key:
        return None
    want = plain(title)
    found = _tvdb_get("/search?type=series&limit=10&query=" + _q(title), key, pin) or {}
    hit = None
    for result in found.get("data") or []:
        names = [result.get("name")] + list(result.get("aliases") or []) + list(
            (result.get("translations") or {}).values())
        if any(plain(n) == want for n in names if isinstance(n, str)):
            hit = result
            break
    if hit is None:
        return None
    series_id = hit.get("tvdb_id") or str(hit.get("id") or "").replace("series-", "")
    detail = _tvdb_get(f"/series/{series_id}/extended?short=true", key, pin) or {}
    genres = [g.get("name") for g in (detail.get("data") or {}).get("genres") or [] if g.get("name")]
    if not genres:
        genres = [g for g in hit.get("genres") or [] if isinstance(g, str)]
    return {"name": hit.get("name") or "", "genres": genres}


# ---- Trakt: show genres (documentary, reality, home-and-garden...). Free Client ID. -------------

def trakt(title, client_id):
    if not client_id:
        return None
    want = plain(title)
    headers = {"trakt-api-version": "2", "trakt-api-key": client_id, "Content-Type": "application/json"}
    request = urllib.request.Request(
        "https://api.trakt.tv/search/show?extended=full&limit=10&query=" + _q(title),
        headers={"User-Agent": AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            found = json.load(response)
    except urllib.error.HTTPError as exc:
        time.sleep(PAUSE)
        if exc.code in (401, 403):
            raise Unavailable("Trakt refused the Client ID")
        if exc.code == 404:
            return None
        raise Unavailable(f"Trakt answered {exc.code}")
    except (OSError, ValueError):
        raise Unavailable("Trakt could not be reached")
    time.sleep(PAUSE)
    for hit in found or []:
        show = hit.get("show") or {}
        if plain(show.get("title")) == want:
            return {"name": show.get("title") or "", "genres": list(show.get("genres") or [])}
    return None


# ---- OMDb: IMDb's genres. Free key, about 1,000 requests a day. -----------------------------------

def omdb(title, key):
    if not key:
        return None
    found = get_json("https://www.omdbapi.com/?apikey=" + _q(key) + "&t=" + _q(title))
    time.sleep(PAUSE)
    if found is None:
        raise Unavailable("OMDb could not be reached, or refused the key")
    if found.get("Response") != "True":
        error = str(found.get("Error") or "").lower()
        if "limit" in error or "key" in error:
            raise Unavailable(f"OMDb: {found.get('Error')}")
        return None
    if plain(found.get("Title")) != plain(title):
        return None
    genres = [g.strip() for g in str(found.get("Genre") or "").split(",") if g.strip() and g.strip() != "N/A"]
    return {"name": found.get("Title") or "", "genres": genres}


def ask(source, title, settings):
    """One source's answer about one title: {"name", "genres"}, or None when it does not know
    the title (or knows only programmes with another name)."""
    languages = tuple(l.strip() for l in str(settings.get("wikipedia_languages") or "en, nl, de, fr")
                      .split(",") if l.strip())
    if source == "tvmaze":
        return tvmaze(title)
    if source == "wikidata":
        return wikidata(title, languages or ("en",))
    if source == "wikipedia":
        return wikipedia(title, languages or ("en",))
    if source == "tmdb":
        return tmdb(title, str(settings.get("tmdb_key") or "").strip())
    if source == "tvdb":
        return tvdb(title, str(settings.get("tvdb_key") or "").strip(),
                    str(settings.get("tvdb_pin") or "").strip())
    if source == "trakt":
        return trakt(title, str(settings.get("trakt_client_id") or "").strip())
    if source == "omdb":
        return omdb(title, str(settings.get("omdb_key") or "").strip())
    return None


def enabled_sources(settings):
    """The keyed sources only with their key; the others are free and always asked."""
    return tuple(s for s in SOURCES
                 if s not in KEYED or str(settings.get(KEYED[s]) or "").strip())
