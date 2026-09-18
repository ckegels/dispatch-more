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

TV_LOGOS_TREE = "https://api.github.com/repos/tv-logo/tv-logos/git/trees/main?recursive=1"
TV_LOGOS_RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/"
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
    for source, load in ((TV_LOGOS, _from_tv_logos), (IPTV_ORG, _from_iptv_org)):
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
    another logo. Then the collection whose links last, then one that is still running, then
    a plain PNG, which every player shows, over SVG and the rest, which some do not.
    """
    return (
        0 if country and entry.get("country") == country else 1,
        0 if entry.get("source") == TV_LOGOS else 1,
        1 if entry.get("closed") else 0,
        0 if entry.get("format") in ("PNG", "") else 1,
        1 if entry.get("hd") else 0,
    )


def suggestions_for(name, index, limit=6):
    """
    The logos the collections have for a channel of this name, best first.

    Whole names only. A logo whose name merely contains this one belongs to another channel
    more often than not: looking for "een" as a part turns up "nickelodeon teen". When the
    exact name finds nothing, the same name without "HD" and the like is tried.
    """
    entries = (index or {}).get("entries") or {}
    key = match_key(name)
    if not key:
        return []
    found = list(entries.get(key) or ())
    if not found:
        found = list(entries.get(without_quality(key)) or ())
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


# ── Applying ─────────────────────────────────────────────────────────────────


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
