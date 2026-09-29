"""
What every show group should hold, worked out from the guide: for each group, which channels
join when and leave when, and why. The plugin's preview.compute, for many groups in one pass:
the guide is read once and every programme judged for each group that is on.

The plan is kept as a file (plan.json), worked out again every half hour, after a guide
refresh, and when a setting changes; the minute tick and the page read it from there.
"""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

from . import matching, store

PLAN = "plan.json"
MAX_AGE = timedelta(minutes=30)
# Titles no guide knows, kept for the online lookups, busiest first
UNKNOWN_KEPT = 1000


def source_channels(settings, exclude_ids=()):
    """The channels a copy may be taken of: every channel with a guide, by its effective guide
    (an override can replace an auto-synced channel's guide, and the one the viewer sees is the
    one that counts), in the chosen channel groups. Hidden channels are left out: the owner took
    those out of every output. Returns guide id -> channels, lowest number first."""
    from apps.channels.managers import with_effective_values
    from apps.channels.models import Channel

    allowed = set(settings.get("source_groups") or ())
    rows = with_effective_values(Channel.objects.filter(hidden_from_output=False)).values(
        "id", "effective_name", "effective_channel_number", "effective_epg_data_id",
        "effective_channel_group_id")
    by_guide = defaultdict(list)
    for row in rows:
        if row["effective_epg_data_id"] is None or row["id"] in exclude_ids:
            continue
        if allowed and row["effective_channel_group_id"] not in allowed:
            continue
        by_guide[row["effective_epg_data_id"]].append(row)
    for channels in by_guide.values():
        channels.sort(key=_order)
    return by_guide


def _order(channel):
    number = channel["effective_channel_number"]
    return (number is None, number or 0, channel["id"])


def _label(channel):
    number = channel["effective_channel_number"]
    name = channel["effective_name"] or f"channel {channel['id']}"
    return f"{name} (#{number:g})" if number is not None else name


def titles_from_guides():
    """Every category any guide gives a title, over the whole guide loaded: a title categorised
    on Saturday teaches the guide that airs it uncategorised on Tuesday."""
    from apps.epg.models import ProgramData

    found = defaultdict(set)
    rows = ProgramData.objects.filter(custom_properties__has_key="categories").values_list(
        "title", "custom_properties")
    for title, props in rows.iterator(chunk_size=5000):
        categories = matching.real_categories((props or {}).get("categories"))
        if categories:
            found[matching.plain(title)].update(categories)
    return {key: sorted(categories) for key, categories in found.items()}


def settings_key(settings, groups):
    """What the plan depends on; a change means working it out again."""
    wanted = {k: settings.get(k) for k in ("join_ahead", "leave_after", "linger", "min_length",
                                            "source_groups", "plan_hours")}
    wanted["groups"] = [{k: v for k, v in g.items() if k not in ("permanent",)}
                        for g in groups if g.get("on")]
    return hashlib.sha1(json.dumps(wanted, sort_keys=True, default=str).encode()).hexdigest()[:16]


def compute(settings, groups, now=None, exclude_ids=()):
    """The plan for every group that is on, as the dict plan.json holds."""
    from apps.epg.models import ProgramData

    now = now or timezone.now()
    on = [g for g in groups if g.get("on")]
    judges = {g["id"]: matching.group_from_theme(g) for g in on}
    hours = float(settings.get("plan_hours") or 24)
    join_ahead = float(settings.get("join_ahead") or 0)
    leave_after = float(settings.get("leave_after") or 0)
    linger = float(settings.get("linger") or 0)
    min_length = float(settings.get("min_length") or 0)

    by_guide = source_channels(settings, exclude_ids)
    from_guides = titles_from_guides() if on else {}
    from_online = store.online_answers(store.load_lookups()) if on else {}

    horizon = now + timedelta(hours=hours) + timedelta(minutes=join_ahead + leave_after + linger)
    window_end = now + timedelta(hours=hours)
    programmes = (ProgramData.objects
                  .filter(epg_id__in=list(by_guide), start_time__lt=horizon,
                          end_time__gt=now - timedelta(minutes=linger))
                  .order_by("start_time", "epg_id")
                  .values_list("epg_id", "title", "start_time", "end_time", "custom_properties"))

    judged = {}
    airings = defaultdict(list)  # group id -> [(title, start, end, verdict, channels)]
    titles = defaultdict(dict)  # group id -> plain title -> {"title", "airings", "taken", "why"}
    unknown = Counter()
    written = {}
    for epg_id, title, start, end, props in programmes.iterator(chunk_size=5000):
        key = matching.plain(title)
        if not key:
            continue
        own = tuple(matching.real_categories((props or {}).get("categories")))
        counted = start < window_end and end > now
        if counted and not own and key not in from_guides:
            unknown[key] += 1
            written.setdefault(key, title)
        short = (end - start).total_seconds() / 60 < min_length
        for gid, group in judges.items():
            verdict = judged.get((gid, key, own))
            if verdict is None:
                verdict = matching.judge(group, title, own, from_guides, from_online)
                judged[(gid, key, own)] = verdict
            if verdict.taken and not short:
                airings[gid].append((title, start, end, verdict, by_guide[epg_id]))
            if counted and verdict.taken:
                row = titles[gid].setdefault(key, {
                    "title": matching.show_name(title), "airings": 0, "taken": 0,
                    "why": f"{verdict.layer}: {verdict.reason}", "uncertain": verdict.uncertain})
                row["airings"] += 1
                row["taken"] += 0 if short else 1

    plan = {"made": now.isoformat(), "key": settings_key(settings, groups), "groups": {},
            "unknown": [[n, written[k], k] for k, n in unknown.most_common(UNKNOWN_KEPT)]}
    for gid in judges:
        group = next(g for g in on if g["id"] == gid)
        # The same programme at the same moment on several channels (regional feeds, a BE and
        # an NL copy of one channel) is one airing: only the lowest number takes it
        together = defaultdict(list)
        for airing in airings[gid]:
            for channel in airing[4]:
                together[(matching.plain(airing[0]), airing[1], airing[2])].append((channel, airing))
        per_channel = defaultdict(list)
        labels = {}
        for pairs in together.values():
            pairs.sort(key=lambda pair: _order(pair[0]))
            for channel, airing in (pairs[:1] if group.get("one_per_airing", True) else pairs):
                per_channel[channel["id"]].append((airing[1], airing[2], airing))
                labels[str(channel["id"])] = _label(channel)
        stays = {}
        for channel_id, held in per_channel.items():
            stays[str(channel_id)] = [{
                "joins": stay.joins.isoformat(), "leaves": stay.leaves.isoformat(),
                "airings": [{"title": a[0], "start": s.isoformat(), "end": e.isoformat(),
                             "why": f"{a[3].layer}: {a[3].reason}"} for s, e, a in stay.airings],
            } for stay in matching.stays(held, join_ahead, leave_after, linger)]
        taken = sorted(titles[gid].values(), key=lambda t: (-t["taken"], t["title"]))
        plan["groups"][gid] = {"stays": stays, "labels": labels, "titles": taken[:80],
                               "title_count": len(taken)}
    return plan


def save(plan):
    store.write_text(PLAN, json.dumps(plan))


def load():
    try:
        with open(store.path_of(PLAN), encoding="utf-8") as fh:
            plan = json.load(fh)
        return plan if isinstance(plan, dict) else None
    except (OSError, ValueError):
        return None


def stale(plan, settings, groups, now=None):
    now = now or timezone.now()
    if not plan or plan.get("key") != settings_key(settings, groups):
        return True
    try:
        return now - datetime.fromisoformat(plan["made"]) >= MAX_AGE
    except (KeyError, TypeError, ValueError):
        return True


def stays_at(plan, group_id, moment):
    """{source channel id: stay} for every channel in the group at this moment."""
    found = {}
    for source_id, stays in ((plan or {}).get("groups", {}).get(group_id, {}).get("stays") or {}).items():
        for stay in stays:
            if datetime.fromisoformat(stay["joins"]) <= moment < datetime.fromisoformat(stay["leaves"]):
                found[source_id] = stay
                break
    return found


def coming(plan, group_id, moment, most=50):
    """The joins still to come, soonest first."""
    group = (plan or {}).get("groups", {}).get(group_id, {})
    found = []
    for source_id, stays in (group.get("stays") or {}).items():
        for stay in stays:
            if datetime.fromisoformat(stay["joins"]) > moment:
                first = stay["airings"][0]
                found.append({"source": int(source_id), "channel": group["labels"].get(source_id, ""),
                              "joins": stay["joins"], "leaves": stay["leaves"], **first})
    found.sort(key=lambda row: row["joins"])
    return found[:most]


def showing(stay, moment):
    """The airing a stay is there for at this moment: the one on, or else the next."""
    for airing in stay["airings"]:
        if datetime.fromisoformat(airing["end"]) > moment:
            return airing
    return stay["airings"][-1]
