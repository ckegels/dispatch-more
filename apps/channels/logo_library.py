"""Logo libraries: public collections of channel logos, and suggesting one for each channel.

A channel's logo comes from wherever its stream or its guide happened to point, which is
often nowhere, a dead link, or something that is not really its logo. There are public
collections that have done the work of gathering the proper ones, so Dispatcharr can look
each channel up in them and offer what it finds next to what the channel has now. Nothing
is changed until someone chooses to: this suggests, and applies only what it is told to.

Two collections, measured before being relied on:

- tv-logo/tv-logos on GitHub: about 10,800 logos, named "<channel>-<country>.png" in a
  folder per country. Hosted on GitHub itself, so its links last. No index is published,
  but GitHub lists every file in the repository in one answer.
- iptv-org: about 36,700 logos for about 31,400 channels, published as JSON with each
  channel's country and its other names. Bigger, and the other names catch channels
  known by more than one, but the images live on image hosts that come and go.

Both are kept together in one index, cached, and built again on request rather than on
every look: together they are several megabytes to download.
"""

import json
import logging
import re
import time
import unicodedata

import requests

logger = logging.getLogger(__name__)

TV_LOGOS = "tv-logos"
IPTV_ORG = "iptv-org"

TV_LOGOS_TREE = "https://api.github.com/repos/tv-logo/tv-logos/git/trees/HEAD?recursive=1"
TV_LOGOS_RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/HEAD/"
IPTV_ORG_LOGOS = "https://iptv-org.github.io/api/logos.json"
IPTV_ORG_CHANNELS = "https://iptv-org.github.io/api/channels.json"

INDEX_KEY = "logo_library:index"
# A week: the collections change slowly, and rebuilding costs a few megabytes each time
INDEX_TTL = 7 * 24 * 3600
DOWNLOAD_TIMEOUT = 60

# What a country is called in a channel's name, in each collection, and in ISO. Only the
# ones that differ are listed: everything else is the same two letters everywhere.
COUNTRY_ALIASES = {"uk": "gb", "gb": "gb"}

# Words a channel carries that say how it is sent, not which channel it is. Kept as a
# second key rather than stripped outright, because for some channels they are the name.
QUALITY_WORDS = ("uhd", "fhd", "hd", "sd", "4k", "hevc", "h265", "raw")


def match_key(name) -> str:
    """
    A channel name reduced to what makes it that channel, for comparing across sources.

    Three things are taken off. The decoration a playlist adds ("┃FR┃ TFX" is "TFX"), which
    no collection repeats. Accents, by folding them rather than dropping them: "Eén" is
    "een", and a comparison on plain letters alone turned it into "en" and missed it.
    And everything that is not a letter or a digit, because one collection writes "ORF 1"
    as "orf1" and another as "orf-1".
    """
    text = str(name or "")
    text = re.sub(r"[┃|\[(][^┃|\])]*[┃|\])]", " ", text)
    # Written out in words by the collections, so they count as words here. Taken off with
    # the rest they lose the one thing that tells Canal+ from Canal, or A&E from AE.
    text = text.replace("+", " plus ").replace("&", " and ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^0-9a-z]", "", text.lower())


def without_quality(key) -> str:
    """The same key with a trailing "hd", "4k" and the like taken off, as a second try."""
    for word in QUALITY_WORDS:
        if key.endswith(word) and len(key) > len(word):
            return key[: -len(word)]
    return key


def country_of(name) -> str:
    """
    The country a channel name says it is from, as its two letters, or "".

    Playlists write it in front in a box of some kind ("┃FR┃", "[BE]", "FR |"), which is
    the best hint there is for choosing between channels of the same name: there is a TFX
    in France and one in Belgium, with different logos.
    """
    found = re.match(r"\s*[┃|\[(]?\s*([A-Za-z]{2,3})\s*[┃|\])]", str(name or ""))
    if not found:
        found = re.match(r"\s*([A-Za-z]{2})\s*[:|-]\s", str(name or ""))
    if not found:
        return ""
    code = found.group(1).lower()
    return COUNTRY_ALIASES.get(code, code)


# ── Building the index ───────────────────────────────────────────────────────


def _get_json(url):
    response = requests.get(
        url,
        timeout=DOWNLOAD_TIMEOUT,
        headers={"Accept": "application/json", "User-Agent": "Dispatcharr"},
    )
    response.raise_for_status()
    return response.json()


def _from_tv_logos():
    """
    Every logo in tv-logo/tv-logos, from GitHub's list of the files in the repository.

    Named "<channel>-<country>.png", in countries/<country>/ and sometimes a further hd/
    folder, so the name and country both come out of the file name.
    """
    tree = _get_json(TV_LOGOS_TREE)
    if tree.get("truncated"):
        # GitHub stops listing past a certain size; better to say so than to be partial
        logger.warning("GitHub truncated the tv-logos listing; some logos are missing")
    entries = []
    for item in tree.get("tree") or ():
        path = item.get("path") or ""
        if item.get("type") != "blob" or not path.lower().endswith(".png"):
            continue
        stem = path.rsplit("/", 1)[-1][:-4]
        head, _, tail = stem.rpartition("-")
        if head and 2 <= len(tail) <= 3:
            name, country = head, tail
        else:
            name, country = stem, ""
        entries.append({
            "key": match_key(name),
            "name": name.replace("-", " "),
            "country": COUNTRY_ALIASES.get(country, country),
            "url": TV_LOGOS_RAW + path,
            "source": TV_LOGOS,
            "format": "PNG",
            "hd": "/hd/" in path,
        })
    return entries


def _from_iptv_org():
    """
    Every logo iptv-org lists, joined to the channel it is for.

    Each channel may have other names, which is how one known by an abbreviation in one
    place and in full in another is still found. Channels marked as closed keep their
    logos but are ranked below the rest: what they show is the least likely to be current.
    """
    channels = {channel["id"]: channel for channel in _get_json(IPTV_ORG_CHANNELS)}
    entries = []
    for logo in _get_json(IPTV_ORG_LOGOS):
        channel = channels.get(logo.get("channel"))
        if not channel or not logo.get("url"):
            continue
        country = (channel.get("country") or "").lower()
        for name in [channel.get("name")] + list(channel.get("alt_names") or []):
            if not name:
                continue
            entries.append({
                "key": match_key(name),
                "name": channel.get("name") or name,
                "country": COUNTRY_ALIASES.get(country, country),
                "url": logo["url"],
                "source": IPTV_ORG,
                "format": (logo.get("format") or "").upper(),
                "width": logo.get("width"),
                "height": logo.get("height"),
                "closed": bool(channel.get("closed")),
            })
    return entries


# ── Collections anyone can add ───────────────────────────────────────────────
#
# Plenty of places publish logos in bulk, in a handful of shapes. Each shape is read the
# same way whoever publishes it, so adding one is a link and what kind of thing it is.

GITHUB = "github"
M3U = "m3u"
XMLTV = "xmltv"
JSON_LIST = "json"
SOURCE_TYPES = (GITHUB, M3U, XMLTV, JSON_LIST)

SOURCES_KEY = "logo-library-sources"
IMAGE_EXTENSIONS = (".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif")
# A list of logos is text; anything much bigger than this is not what was meant
MAX_DOWNLOAD = 100 * 1024 * 1024


def new_source_id() -> str:
    import secrets

    return secrets.token_hex(4)


def load_sources():
    """The collections added from the page, and whether the built-in ones are wanted."""
    from core.models import CoreSettings

    try:
        stored = CoreSettings.objects.filter(key=SOURCES_KEY).first()
        value = getattr(stored, "value", None) or {}
    except Exception as e:
        logger.debug(f"Could not read the logo sources: {e}")
        value = {}
    return {
        "added": [s for s in value.get("added") or [] if isinstance(s, dict)],
        "off": [s for s in value.get("off") or [] if isinstance(s, str)],
    }


def save_sources(sources):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(
        key=SOURCES_KEY,
        defaults={"name": "Logo library sources", "value": sources},
    )


def _download(url):
    """A source's contents, refusing anything far bigger than a list of logos could be."""
    response = requests.get(
        url, timeout=DOWNLOAD_TIMEOUT, stream=True, headers={"User-Agent": "Dispatcharr"}
    )
    response.raise_for_status()
    body = bytearray()
    for chunk in response.iter_content(1024 * 256):
        body.extend(chunk)
        if len(body) > MAX_DOWNLOAD:
            raise ValueError("That is far bigger than a list of logos; nothing was read")
    return bytes(body)


def _entry(name, url, source, country=""):
    return {
        "key": match_key(name),
        "name": name,
        "country": COUNTRY_ALIASES.get(country, country) if country else country_of(name),
        "url": url,
        "source": source,
        "format": url.rsplit(".", 1)[-1].upper() if "." in url.rsplit("/", 1)[-1] else "",
    }


def _github_repo(url):
    """"owner/repo" out of whatever was pasted: the bare pair, or any link into the repo."""
    text = str(url or "").strip()
    text = re.sub(r"^https?://(www\.)?github\.com/", "", text)
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        raise ValueError("A GitHub collection is owner/repository, or a link to one")
    return parts[0], parts[1].removesuffix(".git")


def _from_github(url, label):
    """
    Every image in a GitHub repository, named after its file.

    Read through GitHub's listing of the repository at its default branch ("HEAD" means
    whichever that is), so it works whether a repository calls it main or master. A file
    ending in "-xx" is taken as being from country xx, which is the convention tv-logos and
    the repositories copied from it use.
    """
    owner, repo = _github_repo(url)
    tree = json.loads(
        _download(f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1")
    )
    if tree.get("message"):
        raise ValueError(f"GitHub said: {tree['message']}")
    entries = []
    for item in tree.get("tree") or ():
        path = item.get("path") or ""
        if item.get("type") != "blob" or not path.lower().endswith(IMAGE_EXTENSIONS):
            continue
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        head, _, tail = stem.rpartition("-")
        name, country = (head, tail.lower()) if head and len(tail) == 2 else (stem, "")
        entries.append(_entry(
            name.replace("-", " ").replace("_", " "),
            f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}",
            label,
            country,
        ))
    return entries


def _from_m3u(url, label):
    """
    The logo of every channel in an M3U playlist.

    Playlists name their logos on each channel as tvg-logo, which is the same attribute
    Dispatcharr itself reads. The name is tvg-name where there is one, which is the tidy
    one, and the channel's title otherwise.
    """
    text = _download(url).decode("utf-8", "replace")
    entries = []
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        logo = re.search(r'tvg-logo="([^"]+)"', line)
        if not logo or not logo.group(1).startswith(("http://", "https://")):
            continue
        name = re.search(r'tvg-name="([^"]+)"', line)
        title = line.rsplit(",", 1)[-1].strip() if "," in line else ""
        country = re.search(r'tvg-country="([A-Za-z]{2})', line)
        entries.append(_entry(
            (name.group(1) if name else title) or title,
            logo.group(1),
            label,
            country.group(1).lower() if country else "",
        ))
    return entries


def _from_xmltv(url, label):
    """
    The icon of every channel in an XMLTV guide, under each of its display names.

    Read a piece at a time: a guide is mostly programmes, which are no use here and can run
    to hundreds of megabytes, so they are passed over rather than held.
    """
    import io
    import xml.etree.ElementTree as ET

    entries = []
    for _event, element in ET.iterparse(io.BytesIO(_download(url)), events=("end",)):
        if element.tag == "channel":
            icon = element.find("icon")
            src = icon.get("src") if icon is not None else ""
            if src and src.startswith(("http://", "https://")):
                for display in element.findall("display-name"):
                    if display.text:
                        entries.append(_entry(display.text.strip(), src, label))
            element.clear()
        elif element.tag == "programme":
            element.clear()
    return entries


def _from_json(url, label):
    """
    A JSON list of logos: [{"name", "url"}], or {"name": "url"}.

    The two shapes people write by hand. "logo" and "icon" are taken for "url", since that
    is what they tend to be called, and "country" is used where there is one.
    """
    data = json.loads(_download(url))
    if isinstance(data, dict):
        data = [{"name": name, "url": link} for name, link in data.items()]
    entries = []
    for item in data if isinstance(data, list) else ():
        if not isinstance(item, dict):
            continue
        link = item.get("url") or item.get("logo") or item.get("icon") or ""
        name = item.get("name") or item.get("title") or ""
        if name and isinstance(link, str) and link.startswith(("http://", "https://")):
            entries.append(_entry(str(name), link, label, str(item.get("country") or "").lower()))
    return entries


READERS = {GITHUB: _from_github, M3U: _from_m3u, XMLTV: _from_xmltv, JSON_LIST: _from_json}


def read_source(source):
    """Every logo in one added collection."""
    reader = READERS.get(source.get("type"))
    if reader is None:
        raise ValueError(f"Not a kind of collection Dispatcharr can read: {source.get('type')}")
    return [e for e in reader(source.get("url"), source.get("name") or source.get("url")) if e["key"]]


def build_index(cache=None):
    """
    Download the collections and keep them as one index, by match key.

    One that cannot be reached is left out and said so, rather than failing the other:
    half the logos is more use than none. Returns what was built, per collection.
    """
    if cache is None:
        from django.core.cache import cache

    by_key = {}
    counts = {}
    errors = {}
    sources = load_sources()
    loaders = [
        (name, load)
        for name, load in ((TV_LOGOS, _from_tv_logos), (IPTV_ORG, _from_iptv_org))
        if name not in sources["off"]
    ] + [
        (added.get("name") or added.get("url"), lambda added=added: read_source(added))
        for added in sources["added"]
        if added.get("enabled", True)
    ]
    for source, load in loaders:
        try:
            entries = load()
        except Exception as e:
            logger.warning(f"Could not download the {source} logo list: {e}")
            errors[source] = str(e)[:200]
            continue
        counts[source] = len(entries)
        for entry in entries:
            if entry["key"]:
                by_key.setdefault(entry["key"], []).append(entry)

    index = {
        "built_at": time.time(),
        "counts": counts,
        "errors": errors,
        "entries": by_key,
    }
    cache.set(INDEX_KEY, json.dumps(index), INDEX_TTL)
    logger.info(
        f"Logo library built: {sum(counts.values())} logos under {len(by_key)} names "
        f"({', '.join(f'{k} {v}' for k, v in counts.items()) or 'none'})"
    )
    return {"counts": counts, "errors": errors, "names": len(by_key)}


def load_index(cache=None):
    """The index as it was last built, or None when it has not been."""
    if cache is None:
        from django.core.cache import cache
    raw = cache.get(INDEX_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ── Suggesting ───────────────────────────────────────────────────────────────


def _rank(entry, country):
    """
    How good a candidate is for a channel from this country, lower being better.

    The same country first, because the same name elsewhere is often another channel with
    another logo. Then the collection: tv-logos, whose links last; then any added from the
    page, which someone chose on purpose; then iptv-org, whose images live on hosts that
    come and go. Then one still running, then a plain PNG, which every player shows, over
    SVG and the rest, which some do not.
    """
    source = entry.get("source")
    return (
        0 if country and entry.get("country") == country else 1,
        0 if source == TV_LOGOS else 2 if source == IPTV_ORG else 1,
        1 if entry.get("closed") else 0,
        0 if entry.get("format") in ("PNG", "") else 1,
        1 if entry.get("hd") else 0,
    )


def shorter_names(name):
    """
    The channel's name with words taken off it, longest first: what to look for when its
    whole name finds nothing.

    "PBS Philadelphia" is a station of a network whose logo the collections have under
    "PBS", and the whole name finds nothing at all. The words are dropped from the end
    first, because what a channel is called usually begins with who it belongs to and ends
    with which one of them it is.

    Only ever the channel's own name made shorter, never a longer one that contains it --
    that way round is how "Eén" turns up "Nickelodeon Teen".
    """
    text = re.sub(r"[┃|\[(][^┃|\])]*[┃|\])]", " ", str(name or ""))
    words = text.split()
    if len(words) < 2:
        return []
    return [" ".join(words[:take]) for take in range(len(words) - 1, 0, -1)] + [
        " ".join(words[drop:]) for drop in range(1, len(words))
    ]


def suggestions_for(name, index, limit=6):
    """
    The logos the collections have for a channel of this name, best first.

    Whole names first, and a logo whose name merely contains this one is never taken: that
    is how looking for "een" turns up "nickelodeon teen". When the whole name finds
    nothing, the same name without "HD" and the like is tried, and then the name with its
    words taken off one at a time (see shorter_names) -- which is what finds the network's
    logo for a station of it.
    """
    entries = (index or {}).get("entries") or {}
    key = match_key(name)
    if not key:
        return []
    found = list(entries.get(key) or ())
    if not found:
        found = list(entries.get(without_quality(key)) or ())
    if not found:
        for shorter in shorter_names(name):
            found = list(entries.get(match_key(shorter)) or ())
            if found:
                break
    if not found:
        return []

    country = country_of(name)
    found.sort(key=lambda entry: _rank(entry, country))
    unique = []
    seen = set()
    for entry in found:
        if entry["url"] in seen:
            continue
        seen.add(entry["url"])
        unique.append(entry)
        if len(unique) >= limit:
            break
    return unique


def search(query, index, country="", limit=60):
    """
    Every logo whose name contains what was typed, for finding one by hand.

    Looser than the suggestions on purpose: a suggestion is made unasked, so it has to be
    the whole name or nothing, but someone searching is looking at what comes back and can
    tell Nickelodeon Teen from Eén. So a part of a name counts here. What matches the whole
    of it comes first, then what starts with it, then the rest; within each, the country
    asked for first and the collection whose links last.
    """
    wanted = match_key(query)
    if len(wanted) < 2:
        return []
    entries = (index or {}).get("entries") or {}
    found = []
    for key, candidates in entries.items():
        if wanted not in key:
            continue
        closeness = 0 if key == wanted else 1 if key.startswith(wanted) else 2
        for candidate in candidates:
            found.append((closeness, candidate))

    country = COUNTRY_ALIASES.get(country.lower(), country.lower()) if country else ""
    found.sort(key=lambda pair: (pair[0], len(pair[1]["key"])) + _rank(pair[1], country))
    results = []
    seen = set()
    for _closeness, candidate in found:
        if candidate["url"] in seen:
            continue
        seen.add(candidate["url"])
        results.append(candidate)
        if len(results) >= limit:
            break
    return results


# ── What Dispatcharr already has ─────────────────────────────────────────────

YOUR_GUIDE = "your guide"
YOUR_PLAYLIST = "your playlist"


def local_suggestions(channel):
    """
    The logos this channel's own streams came with.

    Not found by name at all: the channel holds those streams, so there is no guessing
    which channel they are for. The guides' icons are offered separately, and last (see
    guide_suggestions).

    Expects the channel with its guide entry and streams already loaded, since this is asked
    of every channel on the page.
    """
    found = []
    country = country_of(channel.name)
    for stream in channel.streams.all():
        logo = (stream.logo_url or "").strip()
        if logo.startswith(("http://", "https://")):
            found.append(_entry(stream.name or channel.name, logo, YOUR_PLAYLIST, country))

    unique = []
    seen = set()
    for entry in found:
        if entry["url"] not in seen:
            seen.add(entry["url"])
            unique.append(entry)
    return unique


GUIDE_ICONS_KEY = "logo_library:guide_icons"
# Long enough to cover someone typing a search, short enough that a guide refreshed a
# moment ago is in it
GUIDE_ICONS_TTL = 300


def _guide_icons(cache=None):
    """Every channel icon in Dispatcharr's own guides, by match key, kept briefly."""
    if cache is None:
        from django.core.cache import cache
    cached = cache.get(GUIDE_ICONS_KEY)
    if cached:
        return json.loads(cached)

    from apps.epg.models import EPGData

    icons = [
        {"key": match_key(name), "name": name, "url": url, "guide": guide or ""}
        for name, url, guide in EPGData.objects.filter(
            icon_url__startswith="http"
        ).values_list("name", "icon_url", "epg_source__name")
        if name
    ]
    cache.set(GUIDE_ICONS_KEY, json.dumps(icons), GUIDE_ICONS_TTL)
    return icons


def search_guides(query, limit=24):
    """
    The icons in Dispatcharr's own guides whose channel name contains this.

    Compared by match key, not by the database: the database compares accents as they are,
    so "een" did not find "Eén", which is the same fault that once kept Eén from being
    suggested at all. The guides are Dispatcharr's own, so they are current without
    anything being downloaded; the list is only kept for a few minutes.
    """
    wanted = match_key(query)
    if len(wanted) < 2:
        return []
    found = [
        (0 if icon["key"] == wanted else 1 if icon["key"].startswith(wanted) else 2, icon)
        for icon in _guide_icons()
        if wanted in icon["key"]
    ]
    found.sort(key=lambda pair: (pair[0], len(pair[1]["key"])))
    results = []
    seen = set()
    for _closeness, icon in found:
        if icon["url"] in seen:
            continue
        seen.add(icon["url"])
        results.append({**_entry(icon["name"], icon["url"], YOUR_GUIDE), "guide": icon["guide"]})
        if len(results) >= limit:
            break
    return results


def guide_icons_by_key():
    """Every guide icon in Dispatcharr, grouped by match key, for looking channels up."""
    by_key = {}
    for icon in _guide_icons():
        if icon["key"]:
            by_key.setdefault(icon["key"], []).append(icon)
    return by_key


def guide_suggestions(channel, icons_by_key):
    """
    The icons every guide in Dispatcharr has for this channel.

    The guide entry the channel is mapped to first, since that one is tied to it. Then the
    same channel in every other guide, by whole name the way the collections are matched:
    each guide source names channels its own way and carries its own icons, and a channel
    is only mapped to one of them.
    """
    found = []
    epg = getattr(channel, "epg_data", None)
    icon = (getattr(epg, "icon_url", "") or "").strip()
    if icon.startswith(("http://", "https://")):
        source = getattr(getattr(epg, "epg_source", None), "name", "") or ""
        found.append({**_entry(epg.name or channel.name, icon, YOUR_GUIDE), "guide": source})

    key = match_key(channel.name)
    matches = icons_by_key.get(key) or icons_by_key.get(without_quality(key)) or []
    for match in matches:
        found.append({**_entry(match["name"], match["url"], YOUR_GUIDE), "guide": match["guide"]})

    unique, seen = [], set()
    for entry in found:
        if entry["url"] not in seen:
            seen.add(entry["url"])
            unique.append(entry)
    return unique


# ── Applying ─────────────────────────────────────────────────────────────────


def apply_logo_ids(assignments):
    """
    Give channels logos Dispatcharr already has, by id: [(channel_id, logo_id)].

    For a logo uploaded from the page, which is stored on disk rather than at an address and
    so cannot go through apply_logos. Only logos that exist are given.
    """
    from .models import Channel, Logo

    wanted = {int(channel_id): int(logo_id) for channel_id, logo_id in assignments}
    existing = set(
        Logo.objects.filter(id__in=set(wanted.values())).values_list("id", flat=True)
    )
    changed = []
    for channel in Channel.objects.filter(id__in=list(wanted)):
        logo_id = wanted[channel.id]
        if logo_id in existing and channel.logo_id != logo_id:
            channel.logo_id = logo_id
            changed.append(channel)
    if changed:
        Channel.objects.bulk_update(changed, ["logo"])
    return {"updated": len(changed), "created_logos": 0}


def apply_logos(assignments):
    """
    Give channels the logos chosen for them: [(channel_id, url, name)].

    Made the way Dispatcharr already does it for logos from a guide: the logos are looked
    up or created by address in one go, and the channels saved in one go. A channel that
    already has that logo is left as it is. Returns how many channels changed and how many
    logos were new.
    """
    from .models import Channel, Logo

    wanted = {}
    for channel_id, url, name in assignments:
        url = (url or "").strip()
        if channel_id and url.startswith(("http://", "https://")):
            wanted[int(channel_id)] = (url, name or "")
    if not wanted:
        return {"updated": 0, "created_logos": 0}

    names_by_url = {url: name for url, name in wanted.values()}
    logo_by_url = {
        logo.url: logo for logo in Logo.objects.filter(url__in=list(names_by_url))
    }
    missing = [url for url in names_by_url if url not in logo_by_url]
    if missing:
        Logo.objects.bulk_create(
            [Logo(name=names_by_url[url] or url, url=url) for url in missing],
            ignore_conflicts=True,
        )
        for logo in Logo.objects.filter(url__in=missing):
            logo_by_url[logo.url] = logo

    changed = []
    for channel in Channel.objects.filter(id__in=list(wanted)).select_related("logo"):
        logo = logo_by_url.get(wanted[channel.id][0])
        if logo is None or channel.logo_id == logo.id:
            continue
        channel.logo = logo
        changed.append(channel)
    if changed:
        Channel.objects.bulk_update(changed, ["logo"])

    logger.info(
        f"Logo library: gave {len(changed)} channel(s) a logo, "
        f"{len(missing)} logo(s) new to Dispatcharr"
    )
    return {"updated": len(changed), "created_logos": len(missing)}
