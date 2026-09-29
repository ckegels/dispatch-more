"""
The show groups and their settings: which groups there are (a ready-made set, each switched on
or off, and any the owner adds), what each one takes, and the settings all of them share.

Kept in one CoreSettings row, "show-groups": {"settings": {...}, "groups": [...]}. A ready-made
group nobody touched is not stored, so one added in a later version shows up by itself, and an
improved default reaches everyone who never changed it.

Everything starts off: "live" is off and so is every group, so an update changes nothing until
the owner switches one on (the rule that every addition has an off switch).
"""

import re

from .matching import fold

SETTINGS_KEY = "show-groups"

DEFAULTS = {
    # The one switch for all of it: off, nothing is ever copied, and any copies are switched off
    "live": False,
    # The profile the copies are switched on and off in; the IPTV app reads /m3u/<profile>
    "profile_name": "Show Groups",
    "first_number": 20000,
    # Minutes a copy stays after its last viewer has gone
    "viewer_grace": 10,
    # Tell the apps on Dispatcharr's socket (arrTV) when channels come and go
    "announce_changes": True,
    "join_ahead": 60,
    "leave_after": 60,
    "linger": 15,
    "min_length": 10,
    # Channel group ids the copies may be taken from; empty is every group
    "source_groups": [],
    # How far ahead the plan looks, for "coming up"
    "plan_hours": 24,
    "online_lookups": False,
    "wikipedia_languages": "en, nl, de, fr",
    "tmdb_key": "",
}

# Every group has these; a ready-made one fills them in
GROUP_FIELDS = {
    "name": "",
    "on": False,
    "category_words": "",
    "title_words": "",
    "title_exclusions": "",
    "disqualifiers": "",
    "use_title_words": False,
    "use_disqualifiers": False,
    "always": "",
    "never": "",
    # Channel ids with a copy in the group all the time, whatever is on them
    "permanent": [],
    "one_per_airing": True,
}

# The ready-made groups. The words are matched inside the guide's categories, folded (lower
# case, no accents) and as parts of words, because Dutch and German put the telling word inside
# a compound (Kochsendung, Reisreportage). Cooking's are the plugin's, measured on the owner's
# guides; the others were written the same way, in English, Dutch, German and French.
PRESETS = [
    {
        "id": "cooking", "name": "Cooking",
        "category_words": "cooking, food, culin, baking, gastronom, kookprogramma, kochen, "
                          "kochsendung, kulinar, essen und trinken",
        "title_words": "kitchen, keuken, kuchen, kuche, kochen, kocht, kook, bakt, bakes, baking, "
                       "recipe, recept, chef, cuisine, cook, grill, bbq, restaurant, menu, dinner, "
                       "diner, tafel, smaak",
        "title_exclusions": "hell's kitchen, kitchen nightmares, nightmare, murder, crime, news, "
                            "journaal, dinner date, last supper",
        "disqualifiers": "weather, news, home improvement, consumer",
    },
    {
        "id": "travel", "name": "Travel",
        "category_words": "travel, reise, reis, voyage, tourism, tourisme, toeris, tourismus, "
                          "vakantie, urlaub, holiday",
        "title_words": "travel, trip, journey, reis, reise, voyage, getaway, abroad, holiday, "
                       "vakantie, urlaub",
        "title_exclusions": "murder, crime, news, journaal, time travel",
        "disqualifiers": "news, weather, reality",
    },
    {
        "id": "movies", "name": "Movies",
        "category_words": "movie, film, spielfilm, speelfilm, cinema, kino",
        "disqualifiers": "documentar, dokumentar, short, magazin, review, news, series, serie",
        # "Film" is in documentaries' and film magazines' categories too
        "use_disqualifiers": True,
    },
    {
        "id": "documentaries", "name": "Documentaries",
        "category_words": "documentar, dokumentar, docu, reportage, factual",
        "title_words": "documentary, docu",
        "disqualifiers": "news, reality",
    },
    {
        "id": "sport", "name": "Sport",
        "category_words": "sport, football, soccer, voetbal, fussball, tennis, cycling, "
                          "wielrennen, racing, golf, boxing",
        "disqualifiers": "news, talk, magazin, quiz",
    },
    {
        "id": "kids", "name": "Kids",
        "category_words": "children, kids, kinder, jeugd, jeunesse, enfant, cartoon, animation, "
                          "animated",
        "disqualifiers": "adult",
    },
    {
        "id": "nature", "name": "Nature",
        "category_words": "nature, natuur, natur, wildlife, animals, dieren, tiere, animaux, "
                          "environment",
    },
    {
        "id": "music", "name": "Music",
        "category_words": "music, muziek, musik, musique, concert",
        "disqualifiers": "news, quiz",
    },
    {
        "id": "comedy", "name": "Comedy",
        "category_words": "comedy, komedie, komodie, comedie, sitcom, humor, humour, kabarett, "
                          "cabaret",
    },
    {
        "id": "history", "name": "History",
        "category_words": "history, historical, geschiedenis, geschichte, histoire, historisch",
    },
    {
        "id": "science", "name": "Science & Tech",
        "category_words": "science, wetenschap, wissenschaft, technolog, space, ruimte",
        # Science fiction is drama
        "disqualifiers": "science fiction, sci fi, fiction",
        "use_disqualifiers": True,
    },
    {
        "id": "home", "name": "Home & Garden",
        "category_words": "home improvement, diy, garden, tuin, garten, jardin, wonen, interior, "
                          "renovat, house, property",
    },
    {
        "id": "crime", "name": "Crime",
        "category_words": "crime, krimi, misdaad, detective, thriller",
    },
    {
        "id": "news", "name": "News",
        "category_words": "news, nieuws, nachrichten, actualit, current affairs, journal",
        "disqualifiers": "sport",
    },
]
PRESET_IDS = [p["id"] for p in PRESETS]


def preset(group_id):
    found = next((p for p in PRESETS if p["id"] == group_id), None)
    if found is None:
        return None
    return {**GROUP_FIELDS, **found, "preset": True}


def _load():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
    value = row.value if row else None
    return value if isinstance(value, dict) else {}


def _store(value):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY, defaults={"name": "Show Groups", "value": value}
    )


def load_settings():
    values = dict(DEFAULTS)
    values.update({k: v for k, v in (_load().get("settings") or {}).items() if k in DEFAULTS})
    return values


def load_groups():
    """Every group: the ready-made ones in their order, each as the owner left it or as it
    comes, then the owner's own in the order they were added."""
    stored = {g.get("id"): g for g in _load().get("groups") or [] if isinstance(g, dict)}
    groups = []
    for pid in PRESET_IDS:
        mine = stored.pop(pid, None)
        groups.append({**preset(pid), **_clean(mine)} if mine else preset(pid))
    for gid, mine in stored.items():
        groups.append({**GROUP_FIELDS, **_clean(mine), "id": gid, "preset": False})
    return groups


def _clean(group):
    clean = {k: group[k] for k in GROUP_FIELDS if k in (group or {})}
    for key in ("on", "use_title_words", "use_disqualifiers", "one_per_airing"):
        if key in clean:
            clean[key] = bool(clean[key])
    if "permanent" in clean:
        clean["permanent"] = _ids(clean["permanent"])
    if "name" in clean:
        clean["name"] = str(clean["name"] or "").strip()[:60]
    return clean


def _ids(values):
    found = []
    for value in values if isinstance(values, (list, tuple)) else re.split(r"[,\s]+", str(values or "")):
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in found:
            found.append(number)
    return found


def new_id(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-")[:30] or "group"
    gid, n = f"my-{base}", 2
    while gid in taken:
        gid, n = f"my-{base}-{n}", n + 1
    return gid


def save(settings=None, groups=None):
    """Save what was sent: the shared settings, and every group as the page shows it. A
    ready-made group still exactly as it comes is left out of the store (see the top)."""
    stored = _load()
    if settings is not None:
        values = load_settings()
        values.update({k: v for k, v in settings.items() if k in DEFAULTS})
        try:
            for key in ("first_number", "viewer_grace", "join_ahead", "leave_after", "linger",
                        "min_length", "plan_hours"):
                values[key] = max(0, int(float(values[key])))
            values["plan_hours"] = min(168, max(1, values["plan_hours"]))
            values["first_number"] = max(1, values["first_number"])
        except (TypeError, ValueError):
            raise ValueError("Numbers only, please")
        for key in ("live", "announce_changes", "online_lookups"):
            values[key] = bool(values[key])
        values["source_groups"] = _ids(values["source_groups"])
        values["profile_name"] = str(values["profile_name"] or "").strip() or "Show Groups"
        stored["settings"] = values
    if groups is not None:
        kept, names = [], set()
        for group in groups:
            if not isinstance(group, dict) or not group.get("id"):
                continue
            clean = _clean(group)
            base = preset(group["id"])
            if not clean.get("name"):
                clean["name"] = base["name"] if base else "Show group"
            folded = fold(clean["name"]).strip()
            if folded in names:
                raise ValueError(f'Two groups are called "{clean["name"]}"')
            names.add(folded)
            if base is not None and all(clean.get(k, base[k]) == base[k] for k in GROUP_FIELDS):
                continue
            kept.append({"id": str(group["id"])[:40], **clean})
        stored["groups"] = kept
    _store(stored)
    return load_settings(), load_groups()


def active(settings, groups):
    """The groups that are working: live on, and switched on themselves."""
    if not settings.get("live"):
        return []
    return [g for g in groups if g.get("on")]
