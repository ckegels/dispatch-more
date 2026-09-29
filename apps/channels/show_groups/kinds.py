"""
Whole channels of a kind: which of your channels iptv-org's channel database files under
cooking, travel, movies... so a group can keep them in for good ("always in the group")
instead of waiting for their guide to say what each programme is.

iptv-org gives each of its ~31,000 channels categories of its own (cooking, travel, movies,
documentary, kids, sports, news, music, comedy, science, outdoor...). Measured on the user's
1,432 channels (2026-09-29): Gusto, Cooking Channel, Food Network (CA, UK), 24Kitchen and
Njam! came out as cooking; Nat Geo, Discovery ID, Animal Planet, Sky Documentaries as
documentary; Cartoon Network, Nickelodeon, Disney as kids. It misses some (BonGusto has no
category there), which the guide catches anyway.

Only ever a suggestion: nothing joins a group until the owner ticks it on the tab.

A name is matched whole (logo_library.match_key), in the channel's own country first. A
brand is often listed in one country only (24Kitchen only as US), so when the channel's
country has no entry of that name the others are taken; the page says which one it was.
The channel's tvg-id, when it is an iptv-org id, is taken over any name.
"""

import logging

logger = logging.getLogger(__name__)

INDEX_KEY = "show-groups:iptv-org-kinds"
KEPT_SECONDS = 7 * 24 * 3600
# iptv-org's categories (https://iptv-org.github.io/api/categories.json)
CATEGORIES = [
    "animation", "auto", "business", "classic", "comedy", "cooking", "culture", "documentary",
    "education", "entertainment", "family", "general", "kids", "legislative", "lifestyle",
    "movies", "music", "news", "outdoor", "public", "relax", "religious", "science", "series",
    "shop", "sports", "travel", "weather",
]
# What each ready-made group takes of them. "general" and "entertainment" say nothing, and
# "lifestyle" is fashion and home and food at once, so none of those is used.
PRESET_KINDS = {
    "cooking": ["cooking"],
    "travel": ["travel"],
    "movies": ["movies"],
    "documentaries": ["documentary"],
    "sport": ["sports"],
    "kids": ["kids", "animation"],
    "nature": ["outdoor"],
    "music": ["music"],
    "comedy": ["comedy"],
    "science": ["science"],
    "news": ["news"],
}


def _cache():
    from django.core.cache import cache

    return cache


def build_index(cache=None):
    """Download iptv-org's channels and keep, per name and per id, where they are listed and
    under what: {"names": {key: [[country, id, name, [categories], other name?]]},
    "ids": {id: [country, id, name, [categories]]}}. Closed channels are left out."""
    from apps.channels import logo_library

    names, ids = {}, {}
    for channel in logo_library._get_json(logo_library.IPTV_ORG_CHANNELS):
        categories = [c for c in channel.get("categories") or [] if c]
        # One without a category is kept too: that a country has a channel of this name
        # at all is what stops another country's being taken for it (kinds_of)
        if channel.get("closed") or not channel.get("id"):
            continue
        # In the codes country_of() answers in: iptv-org writes the UK as "UK", a playlist's
        # box is read as "gb"
        country = (channel.get("country") or "").lower()
        entry = [logo_library.COUNTRY_ALIASES.get(country, country), channel["id"],
                 channel.get("name") or "", categories]
        ids[channel["id"].lower()] = entry
        for other, name in enumerate([channel.get("name")] + list(channel.get("alt_names") or [])):
            key = logo_library.match_key(name or "")
            if len(key) >= 3:
                # The last field: 1 when this name is one of its other names
                names.setdefault(key, []).append(entry + [1 if other else 0])
    index = {"names": names, "ids": ids}
    logo_library.keep_json(_cache() if cache is None else cache, INDEX_KEY, index, KEPT_SECONDS)
    logger.info(f"Show Groups: iptv-org's channel kinds kept, {len(ids)} channels")
    return index


def index(cache=None):
    from apps.channels import logo_library

    kept = logo_library.read_json(_cache() if cache is None else cache, INDEX_KEY)
    return kept if kept else build_index(cache)


def kinds_of(name, tvg_ids, found):
    """(categories, where it was found) for one channel, or (set(), "")."""
    from apps.channels import logo_library

    for tvg_id in tvg_ids:
        entry = found["ids"].get((tvg_id or "").lower().split("@")[0])
        if entry:
            # Which channel it is, for certain: its kinds or none, no guessing by name after
            if not entry[3]:
                return set(), ""
            return set(entry[3]), f"{entry[2]} ({entry[0].upper()}), by its tvg-id"
    key = logo_library.match_key(name)
    listed = found["names"].get(key) or found["names"].get(logo_library.without_quality(key)) or []
    if not listed:
        return set(), ""
    country = logo_library.country_of(name)
    here = [entry for entry in listed if entry[0] == country] if country else []
    if here:
        chosen = here
    else:
        # Somewhere else only by its own name: another name is too loose abroad ("HGTV" is
        # also a Vietnamese channel's, "Nickelodeon" one of Nick/Comedy Central +1's)
        chosen = [entry for entry in listed if not entry[4]] if country else listed
    chosen = [entry for entry in chosen if entry[3]]
    if not chosen:
        return set(), ""
    categories = set().union(*(set(entry[3]) for entry in chosen))
    countries = sorted({entry[0].upper() for entry in chosen})
    where = f"{chosen[0][2]} ({', '.join(countries[:4])}{'…' if len(countries) > 4 else ''})"
    return categories, where


def suggestions(group, exclude_ids=()):
    """Your channels iptv-org files under this group's kinds, lowest number first."""
    from apps.channels.models import Channel

    wanted = set(group.get("channel_kinds") or ())
    if not wanted:
        return []
    found = index()
    always = set(group.get("permanent") or ())
    rows = (Channel.objects.exclude(id__in=list(exclude_ids))
            .values("id", "name", "channel_number", "tvg_id", "epg_data__tvg_id",
                    "channel_group__name", "hidden_from_output")
            .order_by("channel_number", "name"))
    out = []
    for row in rows:
        categories, where = kinds_of(row["name"], [row["tvg_id"], row["epg_data__tvg_id"]], found)
        hit = sorted(categories & wanted)
        if hit:
            out.append({
                "id": row["id"], "name": row["name"], "number": row["channel_number"],
                "group": row["channel_group__name"] or "", "hidden": row["hidden_from_output"],
                "kinds": hit, "listed_as": where, "always": row["id"] in always,
            })
    return out
