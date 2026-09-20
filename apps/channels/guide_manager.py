"""Guides: which programme guide each channel should be on, and where that is wrong.

Dispatcharr matches a channel to a guide when it is asked to, and leaves it there. Over
time that goes stale in ways nobody sees: a source stops carrying a channel and the guide
it left behind holds nothing, a better source is added and nothing goes back to reconsider,
or a channel was matched to the right name from the wrong country and has been showing
somebody else's evening ever since.

This goes and looks. It scores every channel in scope against every guide there is, the
way the Channel Manager's guide window does for one channel -- Dispatcharr's own fuzzy
matcher, with the country box counting for more than a near miss in the name (see
channel_manager._by_country) -- and suggests a change where one is worth making:

- **none**: the channel is on no guide at all, and something fits.
- **empty**: the guide it is on holds no programmes, and one that holds some fits. This is
  the one that matters most: an empty guide looks exactly like a working one everywhere
  in Dispatcharr except on the channel itself, where there is simply nothing on.
- **better**: the guide it is on is a worse match than another by a margin. A channel on
  the British feed of a Dutch channel is this, and so is one matched before a better
  source was added.

Nothing is changed by looking. A suggestion is a row on a page until it is ticked.

The looking is slow -- every channel against the whole guide catalogue -- so it runs in
Celery in batches that queue the next, the way Stream Check does, rather than making the
page wait. This install has one Celery worker, and a run that held it for two minutes
would hold up every M3U and EPG refresh behind it.
"""

import logging

from . import channel_manager, logo_library

logger = logging.getLogger(__name__)

SETTINGS_KEY = "guide-manager"
# {channel id as a string: {"epg", "name", "source", "score", "programmes", "now", "why"}}
SUGGESTIONS_KEY = "guide-manager-suggestions"
# Channels whose suggestion was waved away: {channel id: {"name", "epg", "at"}}. The guide
# suggested is kept, so a different suggestion later is offered again.
IGNORED_KEY = "guide-manager-ignored"

RUN_KEY = "guide-manager:run"
STOP_KEY = "guide-manager:stop"

# How many channels one batch looks at before queueing the next. Each is scored against
# the whole catalogue, so a batch is a second or two -- short enough that a refresh
# waiting behind it is not held up for long.
BATCH_CHANNELS = 150
# How long a run's record of itself lives once it has finished, so the page can still say
# what happened
RUN_KEPT_SECONDS = 24 * 3600

DEFAULTS = {
    # Channel groups to look at; empty is every channel
    "channel_groups": [],
    # Which kinds of suggestion to make. The first two are what this is for; "better" is
    # the one that can be noisy on a setup whose names do not match its guides closely,
    # so it can be turned off on its own.
    "suggest_none": True,
    "suggest_empty": True,
    "suggest_better": True,
    # A suggestion has to be at least this good to be worth showing at all
    "min_score": 70,
    # ...and to replace a guide that already works, this much better than it
    "better_by": 20,
    # A guide holding nothing is only worth swapping for one that holds something
    "only_if_it_holds_something": True,
}

SETTINGS_VERSION = 1


def _load(key, fallback):
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=key).first()
    value = row.value if row else None
    return value if isinstance(value, type(fallback)) else fallback


def _store(key, name, value):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(key=key, defaults={"name": name, "value": value})


def load_settings():
    values = dict(DEFAULTS)
    stored = _load(SETTINGS_KEY, {})
    values.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return values


def save_settings(given):
    values = load_settings()
    values.update({k: v for k, v in (given or {}).items() if k in DEFAULTS})
    try:
        values["min_score"] = min(100, max(0, int(values["min_score"])))
        values["better_by"] = min(100, max(1, int(values["better_by"])))
        values["channel_groups"] = [int(g) for g in values["channel_groups"] or ()]
    except (TypeError, ValueError):
        raise ValueError("Numbers only, please")
    _store(SETTINGS_KEY, "Guides", {**values, "version": SETTINGS_VERSION})
    return values


def settings_from(given):
    return {**load_settings(), **{k: v for k, v in (given or {}).items() if k in DEFAULTS}}


def load_suggestions():
    return dict(_load(SUGGESTIONS_KEY, {}))


def save_suggestions(found):
    _store(SUGGESTIONS_KEY, "Guide suggestions", found)


def load_ignored():
    return dict(_load(IGNORED_KEY, {}))


def ignore(channel_id, name="", epg_id=None):
    """
    Stop suggesting this. The guide suggested is kept with it: a channel waved away
    because that particular guide was wrong should still be offered a different one if a
    better source turns up later.
    """
    from django.utils import timezone

    ignored = load_ignored()
    ignored[str(channel_id)] = {
        "name": name, "epg": epg_id, "at": timezone.now().isoformat(timespec="seconds")
    }
    _store(IGNORED_KEY, "Guides ignored", ignored)
    return ignored[str(channel_id)]


def unignore(channel_id=None):
    if channel_id is None:
        _store(IGNORED_KEY, "Guides ignored", {})
        return 0
    ignored = load_ignored()
    ignored.pop(str(channel_id), None)
    _store(IGNORED_KEY, "Guides ignored", ignored)
    return len(ignored)


# ── Looking ──────────────────────────────────────────────────────────────────


def channels_in_scope(settings):
    """The channels this run looks at, in the order they are shown."""
    from .models import Channel

    channels = Channel.objects.select_related(
        "epg_data", "epg_data__epg_source", "channel_group"
    ).order_by("channel_number", "id")
    groups = settings.get("channel_groups") or []
    if groups:
        channels = channels.filter(channel_group_id__in=groups)
    return channels


def programme_counts(epg_ids):
    """How many programmes each of these guides holds, in one query."""
    from django.db.models import Count

    from apps.epg.models import ProgramData

    if not epg_ids:
        return {}
    return dict(
        ProgramData.objects.filter(epg_id__in=epg_ids)
        .values_list("epg_id")
        .annotate(held=Count("id"))
        .values_list("epg_id", "held")
    )


def what_is_on(epg_ids):
    """The programme on each of these guides at this moment, in one query."""
    from django.utils import timezone

    from apps.epg.models import ProgramData

    if not epg_ids:
        return {}
    moment = timezone.now()
    return dict(
        ProgramData.objects.filter(
            epg_id__in=epg_ids, start_time__lte=moment, end_time__gt=moment
        ).values_list("epg_id", "title")
    )


def guides_in_use(epg_ids):
    """
    Which of these guides a channel is on, which is what says whether one holding nothing
    is empty or merely unread.

    Dispatcharr reads a guide's programmes when it goes on a channel and not before, so a
    guide nobody uses holds nothing whatever it is really like. Suggesting one and calling
    it empty would be telling someone a good guide is no good; suggesting one and saying
    nothing about it is honest, and it can be read from the page.
    """
    from .models import Channel

    if not epg_ids:
        return set()
    return set(
        Channel.objects.filter(epg_data_id__in=epg_ids)
        .values_list("epg_data_id", flat=True)
    )


def _score_against(name, catalogue, sources, counts, used, playing, limit=6, channel_tvg_id=""):
    """
    The guides this channel could be, best first, with the country counting.

    The same judgement the Channel Manager's guide window makes for one channel, over a
    catalogue already in memory rather than a query each time: over a thousand channels
    that difference is the whole run.
    """
    from . import epg_matching

    plain = channel_manager._strip_country_box(name or "")
    normalized = epg_matching.normalize_name(plain)
    if not normalized:
        return []
    country = logo_library.country_of(name or "") or ""
    _, _, candidates, _ = epg_matching.fuzzy_scan_epg_list(
        normalized, catalogue, None, candidate_limit=max(limit * 3, 20)
    )
    judged = []
    for _, row in candidates:
        # Judged by what kind of match it is, not only how alike the letters are: see
        # channel_manager.judge_guide, and why "PBS 12" and "PBS 13" used to score 83
        score, tier, why = channel_manager.judge_guide(name, country, row, channel_tvg_id)
        if not score:
            continue
        judged.append((score, row.get("epg_source_priority") or 0, tier, why, row))
    judged.sort(key=lambda one: (one[0], one[1]), reverse=True)
    return [
        {
            "epg": row["id"],
            "name": row["name"],
            "tvg_id": row.get("original_tvg_id") or row.get("tvg_id") or "",
            "source": sources.get(row["epg_source_id"], ""),
            "score": score,
            "tier": tier,
            # Why it is that kind of match. Not "why", which on a row is the reason there
            # is something to suggest at all -- two different questions, one word.
            "match_why": why,
            "programmes": counts.get(row["id"], 0),
            "now": playing.get(row["id"], ""),
            # Nobody uses it, so holding nothing says nothing: it has never been read
            "in_use": row["id"] in used,
        }
        for score, _, tier, why, row in judged[:limit]
    ]


def _worth_suggesting(channel, found, settings, counts, catalogue_scores):
    """
    Which of the guides found is worth suggesting for this channel, and why -- or nothing.

    Three reasons, each asked for on its own, because they are three different problems.
    A channel on nothing has no guide to compare against. A channel on a guide holding
    nothing has one that looks fine everywhere except on the screen. A channel on a guide
    that works, but that something else matches better, is the one to be careful with:
    it is only suggested when the difference is a margin rather than a nose, since
    replacing a working guide with a slightly better-scoring one is how a good setup gets
    churned for nothing.
    """
    least = int(settings.get("min_score") or 0)
    # A guess is never suggested. It is still on the list for the picker, so it can be
    # looked at and taken by hand, but nothing that rests on how two names happen to read
    # is put forward as a change to make: that is how a completely different station came
    # to be offered at ninety-six per cent.
    worth = [
        one for one in found
        if one["score"] >= least and one.get("tier") in (channel_manager.CERTAIN, channel_manager.LIKELY)
    ]
    if settings.get("only_if_it_holds_something", True):
        # A guide a channel is already on and that holds nothing is empty, and swapping
        # one empty guide for another helps nobody. A guide nobody uses holds nothing
        # because it has never been read, which is not the same thing and not a reason to
        # pass it over -- it may be the right one, and can be read from the page.
        worth = [one for one in worth if one["programmes"] or not one["in_use"]]
    # What is known to hold programmes comes first, since it can be judged on the spot
    with_programmes = [one for one in worth if one["programmes"]]

    on_now = channel.epg_data_id
    if not on_now:
        if not settings.get("suggest_none", True):
            return None
        best = (with_programmes or worth or [None])[0]
        return {**best, "why": "none"} if best else None

    held = counts.get(on_now, 0)
    if not held:
        if not settings.get("suggest_empty", True):
            return None
        best = next(
            (one for one in (with_programmes or worth) if one["epg"] != on_now), None
        )
        return {**best, "why": "empty"} if best else None

    if not settings.get("suggest_better", True):
        return None
    # What the guide it is on scores for this channel, so "better" means better at the
    # same thing rather than better than nothing
    mine = catalogue_scores.get(on_now, 0)
    margin = int(settings.get("better_by") or 0)
    best = next(
        (one for one in (with_programmes or worth) if one["epg"] != on_now), None
    )
    if best and best["score"] >= mine + margin:
        return {**best, "why": "better", "instead_of_score": mine}
    return None


def look_at(channels, settings, catalogue, sources, counts, used=None, playing=None, say=None):
    """
    What to suggest for these channels, as {channel id as a string: suggestion}.

    The guide a channel is already on is scored too, by the same measure as everything
    else: "a better guide" has to mean better at being this channel, not merely a high
    score next to a number nobody worked out.
    """
    from . import epg_matching

    if used is None:
        used = guides_in_use([row["id"] for row in catalogue])
    if playing is None:
        playing = what_is_on([row["id"] for row in catalogue])
    ignored = load_ignored()
    found = {}
    for at, channel in enumerate(channels):
        # Said as it goes rather than once the batch is over: a batch is a hundred and
        # fifty channels against the whole catalogue, and a bar that only moves between
        # batches looks like a page that has stopped
        if say and at % 10 == 0:
            say(at, channel.name)
        candidates = _score_against(
            channel.name, catalogue, sources, counts, used, playing,
            channel_tvg_id=channel.tvg_id or "",
        )
        # A suggestion waved away was waved away for that guide, not for the channel:
        # the guide comes off this channel's list and the next best is offered instead,
        # so a better source added later is still found
        waved = ignored.get(str(channel.id))
        if waved and waved.get("epg"):
            candidates = [one for one in candidates if one["epg"] != waved["epg"]]
        mine = {}
        if channel.epg_data_id:
            already = next((one for one in candidates if one["epg"] == channel.epg_data_id), None)
            if already:
                mine[channel.epg_data_id] = already["score"]
            else:
                # Not among the best, so it has to be scored on its own
                plain = channel_manager._strip_country_box(channel.name or "")
                normalized = epg_matching.normalize_name(plain)
                row = next((r for r in catalogue if r["id"] == channel.epg_data_id), None)
                if row and normalized:
                    country = logo_library.country_of(channel.name or "") or ""
                    mine[channel.epg_data_id] = max(0, min(100, int(round(
                        epg_matching._compute_fuzzy_score(normalized, row, None)
                        + channel_manager._by_country(country, row)
                    ))))
        worth = _worth_suggesting(channel, candidates, settings, counts, mine)
        # A row for every channel looked at, whether or not there is anything to suggest.
        # Nothing to suggest is not nothing to know: a channel no guide fits is exactly
        # the one somebody wants to find and settle by hand, and it is invisible in a
        # list that only holds suggestions.
        best = worth or (candidates[0] if candidates else {})
        found[str(channel.id)] = {
            **best,
            "why": (worth or {}).get("why", ""),
            "channel": channel.id,
            "channel_name": channel.name,
            # So the channel itself can be watched from the page: a guide can look right
            # and the channel behind it be something else entirely
            "uuid": str(channel.uuid) if getattr(channel, "uuid", None) else "",
            "number": channel.channel_number,
            "group": channel.channel_group.name if channel.channel_group_id else "",
            "group_id": channel.channel_group_id,
            "instead_of": channel.epg_data.name if channel.epg_data_id else "",
            "instead_of_epg": channel.epg_data_id,
            "instead_of_holds": counts.get(channel.epg_data_id, 0) if channel.epg_data_id else 0,
            # What is on the guide it is on now, so the two can be read against each other
            "instead_of_now": playing.get(channel.epg_data_id, "") if channel.epg_data_id else "",
            "instead_of_source": (
                channel.epg_data.epg_source.name
                if channel.epg_data_id and channel.epg_data.epg_source_id
                else ""
            ),
        }
    return found


def redis():
    """
    The Redis a run says how it is going through, or None.

    One way in, used by the page and by the task alike, so there is a single place to
    look when a run says nothing.
    """
    from core.utils import RedisClient

    try:
        return RedisClient.get_client()
    except Exception as e:
        logger.warning(f"Guides: no Redis to say how a run is going ({e})")
        return None


def every_channel(settings, suggestions=None):
    """
    A row for every channel in scope, whether a run has ever looked at it or not.

    "Every channel" has to mean every channel. Listing only what the last run stored made
    it mean "every channel the last run happened to reach", which after a run that was
    stopped, or narrowed to a group, or simply never done, is a handful -- and the
    channels somebody is looking for there are exactly the ones nothing has been found
    for.

    What a run did find is kept over the top, so a channel with a suggestion keeps it.
    """
    stored = dict(suggestions if suggestions is not None else load_suggestions())
    channels = list(channels_in_scope(settings))
    on_now = [c.epg_data_id for c in channels if c.epg_data_id]
    counts = programme_counts(on_now)
    playing = what_is_on(on_now)

    rows = {}
    for channel in channels:
        rows[str(channel.id)] = {
            "channel": channel.id,
            "channel_name": channel.name,
            "uuid": str(channel.uuid) if getattr(channel, "uuid", None) else "",
            "number": channel.channel_number,
            "group": channel.channel_group.name if channel.channel_group_id else "",
            "group_id": channel.channel_group_id,
            "why": "",
            "epg": None,
            "name": "",
            "tvg_id": "",
            "source": "",
            "programmes": 0,
            "now": "",
            "instead_of": channel.epg_data.name if channel.epg_data_id else "",
            "instead_of_epg": channel.epg_data_id,
            "instead_of_holds": counts.get(channel.epg_data_id, 0) if channel.epg_data_id else 0,
            "instead_of_now": playing.get(channel.epg_data_id, "") if channel.epg_data_id else "",
            "instead_of_source": (
                channel.epg_data.epg_source.name
                if channel.epg_data_id and channel.epg_data.epg_source_id
                else ""
            ),
        }
    rows.update(stored)
    return list(rows.values())


def run_state(redis_client):
    """How a run is going, for the page: nothing at all when none has ever been made."""
    if not redis_client:
        return {}
    raw = redis_client.hgetall(RUN_KEY) or {}
    state = {}
    for key, value in raw.items():
        key = key.decode() if isinstance(key, bytes) else key
        value = value.decode() if isinstance(value, bytes) else value
        state[key] = value
    for number in ("done", "total", "found", "batches"):
        if number in state:
            try:
                state[number] = int(state[number])
            except (TypeError, ValueError):
                state[number] = 0
    state["running"] = state.get("state") == "running"
    return state


def start(settings, redis_client):
    """Set a run going, unless one already is."""
    state = run_state(redis_client)
    if state.get("running"):
        return {"started": False, "why": "A run is already going", **state}
    total = channels_in_scope(settings).count()
    if redis_client:
        from django.utils import timezone

        redis_client.delete(STOP_KEY)
        redis_client.delete(RUN_KEY)
        redis_client.hset(RUN_KEY, mapping={
            "state": "running", "done": 0, "total": total, "found": 0, "batches": 0,
            # Said before anything heavy starts, so the page has something to show the
            # moment the button is pressed rather than a blank bar for a minute
            "stage": "reading the guides there are",
            "at": "",
            # Not "started": that is the word start() answers with, and a run already
            # going would have its own timestamp read as a yes
            "since": timezone.now().isoformat(timespec="seconds"),
        })
        redis_client.expire(RUN_KEY, RUN_KEPT_SECONDS)
    save_suggestions({})

    from .tasks import suggest_guides

    suggest_guides.delay(settings, 0)
    logger.info(f"Guides: looking at {total} channel(s)")
    return {"started": True, "total": total}


def stop(redis_client):
    """Ask a run to stop after the batch it is in."""
    if redis_client:
        redis_client.set(STOP_KEY, "1", ex=3600)
    return {"stopping": True}


def asked_to_stop(redis_client):
    return bool(redis_client and redis_client.exists(STOP_KEY))


# ── Applying ─────────────────────────────────────────────────────────────────


def apply(choices):
    """
    Put these guides on these channels: {channel id: guide id, or None for no guide}.

    Saved one at a time with update_fields, because that is what Dispatcharr's own signal
    watches: it drops the guide cache and reads the new guide's programmes, which is the
    whole point of the change. A queryset update would do neither.

    Worked out against what is true now rather than the page: a channel or a guide gone
    since the suggestions were made is left alone rather than being an error.
    """
    from apps.epg.models import EPGData

    from .models import Channel

    wanted = {}
    for channel_id, epg_id in (choices or {}).items():
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            continue
        if epg_id in (None, "", 0, "0", "none"):
            wanted[channel_id] = None
            continue
        try:
            wanted[channel_id] = int(epg_id)
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {"changed": 0}

    guides = {
        entry["id"]: entry
        for entry in EPGData.objects.filter(
            id__in=[e for e in wanted.values() if e]
        ).values("id", "tvg_id")
    }
    changed = 0
    suggestions = load_suggestions()
    for channel in Channel.objects.filter(id__in=wanted):
        epg_id = wanted[channel.id]
        if epg_id is not None and epg_id not in guides:
            continue
        if channel.epg_data_id == epg_id:
            suggestions.pop(str(channel.id), None)
            continue
        channel.epg_data_id = epg_id
        fields = ["epg_data"]
        tvg_id = (guides.get(epg_id) or {}).get("tvg_id")
        if tvg_id:
            channel.tvg_id = tvg_id
            fields.append("tvg_id")
        channel.save(update_fields=fields)
        suggestions.pop(str(channel.id), None)
        changed += 1
    save_suggestions(suggestions)
    logger.info(f"Guides: {changed} channel(s) put on another guide")
    return {"changed": changed}
