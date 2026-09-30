"""
Every show in the guide next to what the online databases say it is: the Show Groups tab's
"Shows" comparison. For each title the guide's own categories, the categories other guides
give it, each database's answer (TVmaze, Wikidata, Wikipedia, TMDB), and which groups take
it -- and, the reason to look, where the guides and the databases disagree about a group.
"""

import json
from datetime import datetime, timezone

from . import lookups, matching, store, themes
from . import plan as plans

FILTERS = ("all", "taken", "databases", "disagree", "unknown")


def load_shows():
    try:
        with open(store.path_of(plans.SHOWS), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def row(key, show, answers, judges, vague=frozenset()):
    """One show as the comparison draws it."""
    own = matching.real_categories(show.get("categories"))
    elsewhere = matching.real_categories(show.get("elsewhere"))
    sources = {}
    for source in lookups.SOURCES:
        answer = answers.get(source)
        if answer is None:
            sources[source] = None  # not asked
        else:
            sources[source] = {"name": answer.get("name") or "", "genres": answer.get("genres") or []}
    online = store.online_answers({key: answers}).get(key)
    takes, disagree = [], []
    for gid, group in judges.items():
        shares = {key: show.get("shares") or {}}
        verdict = matching.judge(group, show["title"], own, {key: elsewhere}, {key: online} if online else {},
                                 shares)
        if verdict.taken:
            takes.append({"group": gid, "layer": verdict.layer, "why": verdict.reason})
        if online and verdict.layer != matching.PIN:
            # What the databases alone would say, against what the guides alone would
            by_guides = matching.judge(group, show["title"], own, {key: elsewhere}, {}, shares)
            by_them = matching._answer(group, online["source"], matching.real_categories(online["genres"]))
            if by_guides.layer in (matching.GUIDE, matching.OTHER_GUIDE) and by_guides.taken != by_them.taken:
                disagree.append({"group": gid, "guides": by_guides.taken, "databases": by_them.taken})
    return {
        "key": key, "title": show["title"], "airings": show.get("airings", 0),
        "guide": own, "elsewhere": elsewhere,
        "vague": bool(own or elsewhere) and not matching.telling(own, vague)
        and not matching.telling(elsewhere, vague),
        "sources": sources, "takes": takes, "disagree": disagree,
    }


def shows(query="", only="all", offset=0, limit=50):
    from apps.channels import service_keys

    settings, groups = service_keys.with_keys(themes.load_settings()), themes.load_groups()
    chosen = [g for g in groups if g.get("on")] or groups
    vague = plans.vague_of(settings)
    combine = settings.get("combine_guides", True) is not False
    judges = {g["id"]: matching.group_from_theme(g, vague, combine) for g in chosen}
    data = load_shows()
    everything = data.get("shows") or {}
    answers = store.load_lookups()
    wanted = matching.fold(query).strip()
    ordered = sorted(everything.items(), key=lambda kv: (-kv[1].get("airings", 0), kv[0]))
    if only == "all":
        # Every show counts, so only the page shown needs judging (not all 9,000 each time)
        matching_ones = [(k, s) for k, s in ordered if not wanted or wanted in matching.fold(s["title"])]
        return {
            "made": data.get("made"), "total": len(matching_ones),
            "shows": [row(k, s, answers.get(k, {}), judges, vague)
                      for k, s in matching_ones[offset:offset + limit]],
            "groups": [{"id": g["id"], "name": g["name"]} for g in chosen],
            "sources": [s for s in lookups.SOURCES if s in lookups.enabled_sources(settings)],
            "count": len(everything),
        }
    rows = []
    for key, show in ordered:
        if wanted and wanted not in matching.fold(show["title"]):
            continue
        one = row(key, show, answers.get(key, {}), judges, vague)
        known = any(a and a["genres"] for a in one["sources"].values())
        if only == "taken" and not one["takes"]:
            continue
        if only == "databases" and not known:
            continue
        if only == "disagree" and not one["disagree"]:
            continue
        if only == "unknown" and (known or matching.telling(one["guide"], vague)
                                  or matching.telling(one["elsewhere"], vague)):
            continue
        rows.append(one)
    return {
        "made": data.get("made"), "total": len(rows), "shows": rows[offset:offset + limit],
        "groups": [{"id": g["id"], "name": g["name"]} for g in chosen],
        "sources": [s for s in lookups.SOURCES if s in lookups.enabled_sources(settings)],
        "count": len(everything),
    }


def ask_now(title):
    """Ask every database about one title now, whatever the queue says, and keep the answers."""
    from . import live

    from apps.channels import service_keys

    settings = service_keys.with_keys(themes.load_settings())
    key = matching.plain(title)
    if not key:
        raise ValueError("No title")
    now = datetime.now(timezone.utc).isoformat()
    new = {}
    for source in lookups.enabled_sources(settings):
        try:
            answer = lookups.ask(source, title, settings)
        except lookups.Unavailable:
            continue
        except Exception:
            answer = None
        new[source] = {**(answer or {}), "asked": now}
    store.merge_lookups({key: new})
    live.ask_rebuild()
    return key
