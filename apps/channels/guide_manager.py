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

from . import channel_manager, known_channels, logo_library
from .settings_rows import change_row

logger = logging.getLogger(__name__)

SETTINGS_KEY = "guide-manager"
# {channel id as a string: {"epg", "name", "source", "score", "programmes", "now", "why"}}
SUGGESTIONS_KEY = "guide-manager-suggestions"
# Channels whose suggestion was waved away: {channel id: {"name", "epg", "at"}}. The guide
# suggested is kept, so a different suggestion later is offered again.
IGNORED_KEY = "guide-manager-ignored"

# Channels whose guide is settled: {channel id: {"name", "epg", "at"}}. Nothing is
# suggested for them at all while they are still on the guide that was chosen -- not
# "this guide is wrong", which is what waving one away means, but "I have decided this
# one, stop asking". Applying a guide from the page is deciding, so it is recorded here.
CHOSEN_KEY = "guide-manager-chosen"

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
    # ...and groups to leave alone, for the ones nothing here should touch
    "exclude_channel_groups": [],
    # Only channels whose guide comes from these EPG sources, with "none" for the ones on
    # no guide at all. Empty is every channel.
    #
    # This is the other half of choosing which sources are matched against (see
    # channel_manager.load_matching): one says which channels are being asked about, the
    # other which guides may answer. Together they are "take my channels on the source
    # that has gone stale and find them somewhere else", which is a thing people actually
    # want to do and could not say before.
    "on_sources": [],
    # Which kinds of suggestion to make. The first two are what this is for; "better" is
    # the one that can be noisy on a setup whose names do not match its guides closely,
    # so it can be turned off on its own.
    "suggest_none": True,
    "suggest_empty": True,
    "suggest_better": True,
    # A suggestion has to be at least this good to be worth showing at all
    "min_score": 70,
    # ...and, with this on, has to be a certainty rather than a likelihood. What kind of
    # match it is says more than the number does: an id that agrees is not the same sort
    # of thing as two names that happen to read alike. For working through a whole source
    # at once, where nobody is going to look at every row.
    "only_certain": False,
    # ...and to replace a guide that already works, this much better than it
    "better_by": 20,
    # A guide holding nothing is only worth swapping for one that holds something
    "only_if_it_holds_something": True,
    # ...and one holding nothing for tonight is no better. A guide can be full of last
    # spring and look fine everywhere but on the screen, so a guide with nothing in the
    # next `fresh_hours` is not put forward. Off, because a guide nobody uses has not been
    # read at all and would be thrown out for it.
    "must_be_fresh": False,
    "fresh_hours": 12,
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
        values["fresh_hours"] = min(168, max(1, int(values["fresh_hours"])))
        values["channel_groups"] = [int(g) for g in values["channel_groups"] or ()]
        values["exclude_channel_groups"] = [
            int(g) for g in values["exclude_channel_groups"] or ()
        ]
        # "none" is one of the answers here -- the channels on no guide at all -- so this
        # one is not a list of numbers
        values["on_sources"] = [
            "none" if str(s).lower() == "none" else int(s)
            for s in values["on_sources"] or ()
        ]
    except (TypeError, ValueError):
        raise ValueError("Numbers only, please")
    _store(SETTINGS_KEY, "Guides", {**values, "version": SETTINGS_VERSION})
    return values


def settings_from(given):
    return {**load_settings(), **{k: v for k, v in (given or {}).items() if k in DEFAULTS}}


def load_suggestions():
    return dict(_load(SUGGESTIONS_KEY, {}))


def save_suggestions(found):
    """Everything that is being suggested, written down as a whole."""
    _store(SUGGESTIONS_KEY, "Guide suggestions", found)


def add_suggestions(found):
    """
    What a batch found, onto what is already there, holding the row while it does.

    A run writes here for as long as it takes to go through a lineup, and the page it is
    filling is where somebody sits waving suggestions away while it does. Read, changed and
    written as three steps, one of the two changes was simply gone (see settings_rows).
    """
    def change(kept):
        kept.update(found)
        return len(kept)

    return change_row(SUGGESTIONS_KEY, "Guide suggestions", change)


def drop_suggestion(channel_id):
    """This channel is no longer being suggested for -- settled, or waved away."""
    def change(kept):
        kept.pop(str(channel_id), None)
        return len(kept)

    return change_row(SUGGESTIONS_KEY, "Guide suggestions", change)


def load_ignored():
    return dict(_load(IGNORED_KEY, {}))


def ignore(channel_id, name="", epg_id=None):
    """
    Stop suggesting this. The guide suggested is kept with it: a channel waved away
    because that particular guide was wrong should still be offered a different one if a
    better source turns up later.
    """
    from django.utils import timezone

    entry = {"name": name, "epg": epg_id, "at": timezone.now().isoformat(timespec="seconds")}

    def change(ignored):
        ignored[str(channel_id)] = entry
        return entry

    return change_row(IGNORED_KEY, "Guides ignored", change)


def unignore(channel_id=None):
    def change(ignored):
        if channel_id is None:
            ignored.clear()
            return 0
        ignored.pop(str(channel_id), None)
        return len(ignored)

    return change_row(IGNORED_KEY, "Guides ignored", change)


def load_chosen():
    return dict(_load(CHOSEN_KEY, {}))


def choose(channel_id, name="", epg_id=None):
    """
    This channel's guide is settled: suggest nothing for it.

    The guide is written down with it, because the decision was about that guide. If the
    channel later ends up on a different one -- changed in the Lineup, or by Dispatcharr's
    own matching -- then what was decided is no longer what is there, and the channel is
    looked at again like any other.
    """
    from django.utils import timezone

    entry = {"name": name, "epg": epg_id, "at": timezone.now().isoformat(timespec="seconds")}

    def change(chosen):
        chosen[str(channel_id)] = entry
        return entry

    return change_row(CHOSEN_KEY, "Guides chosen", change)


def unchoose(channel_id=None):
    """Unsettle one channel, or every one of them, so they are suggested for again."""
    def change(chosen):
        if channel_id is None:
            chosen.clear()
            return 0
        chosen.pop(str(channel_id), None)
        return len(chosen)

    return change_row(CHOSEN_KEY, "Guides chosen", change)


def settled(channel_id, epg_id, chosen=None):
    """Whether this channel is settled on the guide it is on now."""
    entry = (load_chosen() if chosen is None else chosen).get(str(channel_id))
    if not entry:
        return False
    return (entry.get("epg") or None) == (epg_id or None)


def mark_waved_away(rows, ignored=None):
    """
    Say on each row whether a suggestion for it was waved away, and which guide.

    Waving one away is not the same as settling a channel, and the page had no way to
    look at what had been waved away at all -- only a count in the settings, which is a
    number you cannot undo one row of.
    """
    ignored = load_ignored() if ignored is None else ignored
    for row in rows:
        said = ignored.get(str(row.get("channel")))
        row["waved_away"] = bool(said)
        row["waved_away_guide"] = (said or {}).get("name") or ""
    return rows


def mark_chosen(rows, chosen=None):
    """
    Say on each row whether that channel is settled, so the page can keep them apart.

    Done over the rows rather than written into them when a run looks, because settling a
    channel and a run looking at it happen in either order: a channel settled after the
    last run would otherwise carry a run's word for it and be offered around again.
    """
    chosen = load_chosen() if chosen is None else chosen
    for row in rows:
        entry = chosen.get(str(row.get("channel")))
        on_now = row.get("instead_of_epg") or None
        row["chosen"] = bool(entry) and (entry.get("epg") or None) == on_now
        row["chosen_at"] = entry.get("at", "") if row["chosen"] else ""
        # Settled means nothing is put forward for it. The suggestion itself stays on the
        # row: "every channel" is for looking, and being able to see what would have been
        # suggested is the point of looking.
        if row["chosen"]:
            row["why"] = ""
    return rows


# ── Looking ──────────────────────────────────────────────────────────────────


def channels_in_scope(settings):
    """
    The channels this run looks at, in the order they are shown.

    Narrowed three ways, each of which somebody asked for out loud: the groups to look at,
    the groups to leave alone, and the sources the channels are on now -- which is how
    "everything that is on the guide source that went stale" is said.
    """
    from django.db.models import Q

    from .models import Channel

    channels = Channel.objects.select_related(
        "epg_data", "epg_data__epg_source", "channel_group"
    ).order_by("channel_number", "id")
    groups = settings.get("channel_groups") or []
    if groups:
        channels = channels.filter(channel_group_id__in=groups)
    leave_alone = settings.get("exclude_channel_groups") or []
    if leave_alone:
        channels = channels.exclude(channel_group_id__in=[int(g) for g in leave_alone])
    on_sources = [str(one) for one in settings.get("on_sources") or []]
    if on_sources:
        ids = [int(one) for one in on_sources if one.isdigit()]
        wanted = Q(epg_data__epg_source_id__in=ids) if ids else Q(pk__in=[])
        if "none" in on_sources:
            wanted = wanted | Q(epg_data__isnull=True)
        channels = channels.filter(wanted)
    return channels


# Past this many guides, naming them all in the query costs more than not filtering at
# all: a run asks about every guide there is, which on this install is a list of thirty-six
# thousand ids -- half a megabyte of SQL -- sent three times over, ten times a run.
TOO_MANY_TO_NAME = 2000


def programme_counts(epg_ids=None):
    """
    How many programmes each of these guides holds, in one query.

    `epg_ids` of None means every guide. So does a list longer than TOO_MANY_TO_NAME:
    counting them all and looking up the ones wanted is cheaper than naming them.
    """
    from django.db.models import Count

    from apps.epg.models import ProgramData

    if epg_ids is not None and not epg_ids:
        return {}
    rows = ProgramData.objects.all()
    if epg_ids is not None and len(epg_ids) <= TOO_MANY_TO_NAME:
        rows = rows.filter(epg_id__in=epg_ids)
    return dict(
        rows.values_list("epg_id").annotate(held=Count("id")).values_list("epg_id", "held")
    )


def programmes_soon(epg_ids=None, hours=12):
    """
    Which of these guides have a programme in the next `hours`, in one query.

    A guide can hold thousands of programmes and none of them from this week: a source
    that stopped being updated in March looks exactly like a working one everywhere except
    on the screen. Counting them says it is full; asking what is on tonight says whether it
    is any use. Taken from the EPG Janitor plugin, which will not call a match good until
    the guide has programme data in the next twelve hours.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.epg.models import ProgramData

    if epg_ids is not None and not epg_ids:
        return set()
    moment = timezone.now()
    rows = ProgramData.objects.filter(
        end_time__gt=moment, start_time__lt=moment + timedelta(hours=hours)
    )
    if epg_ids is not None and len(epg_ids) <= TOO_MANY_TO_NAME:
        rows = rows.filter(epg_id__in=epg_ids)
    return set(rows.values_list("epg_id", flat=True).distinct())


def what_is_on(epg_ids=None):
    """The programme on each of these guides at this moment, in one query."""
    if epg_ids is not None and not epg_ids:
        return {}
    wanted = epg_ids if epg_ids is not None and len(epg_ids) <= TOO_MANY_TO_NAME else None
    # The same pick as the window beside the page makes, when a guide holds two at once
    return {epg_id: title for epg_id, (title, _) in channel_manager.airing(wanted).items()}


def guides_in_use(epg_ids=None):
    """
    Which of these guides a channel is on, which is what says whether one holding nothing
    is empty or merely unread.

    Dispatcharr reads a guide's programmes when it goes on a channel and not before, so a
    guide nobody uses holds nothing whatever it is really like. Suggesting one and calling
    it empty would be telling someone a good guide is no good; suggesting one and saying
    nothing about it is honest, and it can be read from the page.
    """
    from .models import Channel

    if epg_ids is not None and not epg_ids:
        return set()
    rows = Channel.objects.filter(epg_data__isnull=False)
    if epg_ids is not None and len(epg_ids) <= TOO_MANY_TO_NAME:
        rows = rows.filter(epg_data_id__in=epg_ids)
    return set(rows.values_list("epg_data_id", flat=True))


def _score_against(name, catalogue, sources, counts, used, playing, limit=6,
                   channel_tvg_id="", matching=None, fresh=None, known_calls=None,
                   reference=None, countries=None):
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
    # What is left out is left out before the shortlist is cut -- but the sources and the
    # tvg-id are the same for every channel and were taken out of the catalogue once, by
    # look_at. Only the country is this channel's own, and it is looked up rather than
    # asked of every guide: doing that per channel was a thousand passes of thirty-six
    # thousand guides, which is the run.
    if countries is not None:
        here = channel_manager.in_this_country(countries, country)
        if here is not None:
            catalogue = here
    _, _, candidates, _ = epg_matching.fuzzy_scan_epg_list(
        normalized, catalogue, None, candidate_limit=max(limit * 3, 20)
    )
    judged = []
    for _, row in candidates:
        # Judged by what kind of match it is, not only how alike the letters are: see
        # channel_manager.judge_guide, and why "PBS 12" and "PBS 13" used to score 83
        score, tier, why = channel_manager.judge_guide(
            name, country, row, channel_tvg_id, known_calls=known_calls, reference=reference
        )
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
            # Whether it has anything on in the next few hours, which is a different
            # question from how many programmes it holds altogether
            "fresh": None if fresh is None else row["id"] in fresh,
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
    tiers = (
        (channel_manager.CERTAIN,)
        if settings.get("only_certain")
        else (channel_manager.CERTAIN, channel_manager.LIKELY)
    )
    worth = [
        one for one in found
        if one["score"] >= least and one.get("tier") in tiers
    ]
    if settings.get("must_be_fresh"):
        # A guide with nothing on tonight is no use whatever it holds altogether. Only
        # asked of guides that hold something: one holding nothing holds nothing because
        # nobody has read it, and "nothing on tonight" is not something that can be said
        # about a guide nobody has looked at (see holds/in_use).
        worth = [
            one for one in worth
            if not (one.get("fresh") is False and one.get("programmes"))
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


def look_at(channels, settings, catalogue, sources, counts, used=None, playing=None, say=None,
            matching=None, fresh=None, known_calls=None, reference=None):
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
    if matching is None:
        matching = channel_manager.load_matching()
    # The sources and the tvg-id are the same for every channel in the run, so they come
    # out of the catalogue once here rather than once per channel. The country is the
    # channel's own, so it is indexed instead and looked up per channel.
    # Every guide there is, kept before the narrowing below: what a channel is on now has
    # to be scored whether or not its source is one of the ones being matched against.
    # Narrowed to another source -- which is exactly what "replace these with those" is --
    # the guide it has could not be found at all, so "better" was measured against nothing
    # and the page said its guide scored 0%.
    everything = {row["id"]: row for row in catalogue}
    if channel_manager.narrows(matching):
        catalogue = channel_manager.guides_in_play(
            catalogue, {**matching, "country_must_agree": False}
        )
    countries = channel_manager.by_country(catalogue) if matching.get("country_must_agree") else None
    if fresh is None and settings.get("must_be_fresh"):
        fresh = programmes_soon(
            [row["id"] for row in catalogue], int(settings.get("fresh_hours") or 12)
        )
    # What a call sign really is here, and what somebody who is not a provider says these
    # channels are. Both worked out once for the whole run, and both optional: with
    # neither, the matching is exactly what it was (see known_channels).
    if known_calls is None:
        known_calls = known_channels.call_signs()
        if known_calls is None and catalogue:
            known_calls = known_channels.build_call_signs(catalogue)
    if reference is None:
        reference = known_channels.known()
    ignored = load_ignored()
    chosen = load_chosen()
    found = {}
    for at, channel in enumerate(channels):
        # Said as it goes rather than once the batch is over: a batch is a hundred and
        # fifty channels against the whole catalogue, and a bar that only moves between
        # batches looks like a page that has stopped
        if say and at % 10 == 0:
            say(at, channel.name)
        candidates = _score_against(
            channel.name, catalogue, sources, counts, used, playing,
            channel_tvg_id=channel.tvg_id or "", matching=matching, fresh=fresh,
            known_calls=known_calls, reference=reference, countries=countries,
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
                # By id rather than by walking the catalogue: this is once per channel
                # against every guide there is, which on this install is thirty-six
                # thousand rows a hundred and fifty times a batch
                row = everything.get(channel.epg_data_id)
                if row and normalized:
                    country = logo_library.country_of(channel.name or "") or ""
                    mine[channel.epg_data_id] = max(0, min(100, int(round(
                        epg_matching._compute_fuzzy_score(normalized, row, None)
                        + channel_manager._by_country(country, row)
                    ))))
        # One thing on a loop has no schedule anywhere, so no guide is the right guide for
        # it and every one offered would be wrong however well the names read
        on_a_loop = channel_manager.round_the_clock(channel.name)
        if on_a_loop:
            candidates = []
        # A channel whose guide is settled is not asked about again while it is still on
        # the guide that was settled on. It is still scored and still gets a row, because
        # "every channel" means every channel -- what is not done is putting something
        # forward as a change to make.
        if settled(channel.id, channel.epg_data_id, chosen) or on_a_loop:
            worth = None
        else:
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
            "chosen": settled(channel.id, channel.epg_data_id, chosen),
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


def freshen(rows):
    """
    What each guide on these rows holds, as it is now rather than as the run left it.

    A run writes down how many programmes a guide had at the moment it looked. Reading a
    guide's programmes afterwards -- which is what the button on the window is for --
    does not go back and change that, so the page went on saying "not read yet" about a
    guide that had been read, while the window beside it showed the programmes. The
    counts are cheap and the run's are stale by definition, so they are taken again here.
    """
    ids = set()
    for row in rows:
        for key in ("epg", "instead_of_epg"):
            if row.get(key):
                ids.add(row[key])
    if not ids:
        return rows
    counts = programme_counts(ids)
    # With when that changes, so the page can ask again then rather than go on showing
    # a programme that finished while somebody was working down the list
    playing = channel_manager.on_now(ids)
    nothing = {"now": "", "changes_at": ""}
    used = guides_in_use(ids)
    # What a read of each guide came back with, which is the difference between "nobody
    # has looked" and "somebody looked and there was nothing there"
    was_read = channel_manager.reads()
    for row in rows:
        if row.get("epg"):
            row["programmes"] = counts.get(row["epg"], 0)
            row["now"] = playing.get(row["epg"], nothing)["now"]
            row["now_changes_at"] = playing.get(row["epg"], nothing)["changes_at"]
            row["in_use"] = row["epg"] in used
            row["read"] = was_read.get(str(row["epg"])) or None
        if row.get("instead_of_epg"):
            row["instead_of_holds"] = counts.get(row["instead_of_epg"], 0)
            row["instead_of_now"] = playing.get(row["instead_of_epg"], nothing)["now"]
            row["instead_of_now_changes_at"] = (
                playing.get(row["instead_of_epg"], nothing)["changes_at"]
            )
    return rows


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
        ).values("id", "tvg_id", "name")
    }
    from django.utils import timezone

    changed = 0
    settled_channels = {}
    done_with = []
    # Putting a guide on a channel from this page is deciding what that channel is on,
    # whether the guide came from a suggestion or was searched for by hand. So it is
    # written down as settled, and nothing is put forward for that channel again until
    # somebody asks for it to be or its guide changes underneath.
    settled_now = timezone.now().isoformat(timespec="seconds")
    for channel in Channel.objects.filter(id__in=wanted):
        epg_id = wanted[channel.id]
        if epg_id is not None and epg_id not in guides:
            continue
        name = (guides.get(epg_id) or {}).get("name") or ""
        settled_channels[str(channel.id)] = {"name": name, "epg": epg_id, "at": settled_now}
        if channel.epg_data_id == epg_id:
            done_with.append(str(channel.id))
            continue
        channel.epg_data_id = epg_id
        fields = ["epg_data"]
        tvg_id = (guides.get(epg_id) or {}).get("tvg_id")
        if tvg_id:
            channel.tvg_id = tvg_id
            fields.append("tvg_id")
        channel.save(update_fields=fields)
        done_with.append(str(channel.id))
        changed += 1

    def settle(chosen):
        chosen.update(settled_channels)

    def drop(kept):
        for channel_id in done_with:
            kept.pop(channel_id, None)

    change_row(CHOSEN_KEY, "Guides chosen", settle)
    change_row(SUGGESTIONS_KEY, "Guide suggestions", drop)
    logger.info(f"Guides: {changed} channel(s) put on another guide")
    return {"changed": changed}
