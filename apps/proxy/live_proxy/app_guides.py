"""arrTV: "Wrong guide? Choose another".

Somebody watching a channel sees the guide is wrong -- the picture shows one programme and
the guide another -- and picks the right guide from the player: a list of the other guides
that could be this channel, each with what is on it now, to compare with the picture. The
channel is on the chosen guide from then on, for everybody, and the choice is written down
with who made it (the login, the device and the address it came from), so an admin can see
what was changed and put it back. The whole design is fork/arrTV-guide-choice.md.

Everything that decides which guide a channel could be is the Guides tab's own
(channel_manager.guide_candidates, guide_manager.apply): this is a way for arrTV to reach
it, not a second matcher. What is new is that nothing without information is offered, and
that the guides on offer have their programmes loaded before anybody asks.

**Why they need loading.** Dispatcharr reads a guide's programmes only once a channel uses
it, and every refresh deletes the programmes of every guide no channel uses -- which is
every alternative, by definition. So each channel's best candidates are read ahead of time
(a batched Celery task, apps.channels.tasks.preload_guide_choices, one pass of each source's
file for all of them) and the refresh's clean-up is asked to leave them alone
(channel_manager.kept_after_reading, which already does that for the Guides tab's reads).
The rest of a channel's list is read when somebody opens it. Dummy channels would have done
the same and appeared in every lineup, count and output Dispatcharr has.

Off by default (the "guide_choice" setting). Off, both endpoints refuse, nothing is
preloaded, and nothing is kept past the clean-up that stock would not keep: the record of
what was preloaded is emptied when the switch goes off.
"""

import logging

from . import app_devices

logger = logging.getLogger("live_proxy")

# What arrTV's reads found: {"ids": {epg id: {"found": n, "at": iso, "how": "preload" or
# "asked"}}, "preloaded_at": iso, "preloaded": how many}. The ids with programmes are the
# ones the refresh's clean-up leaves alone.
KEPT_KEY = "app-guide-kept"
# How many guides the list shows, best first across every source, and how far down the
# matcher's list each step below looks to find that many with something on now
LIST_MOST = 20
LOOK_AT = 50
# A list of maybes is better than none: until it holds LIST_MOST guides with something on,
# it widens a step at a time. "matching" is the Guides tab's matcher and settings at any
# confidence (its own bar is MIN_GUIDE_SCORE); "wide" drops the matching settings' limits
# (sources, tvg-id pattern, country) -- the TV's own sources setting still holds; "search"
# is a plain search on the words of the channel's name, then on its longest word alone.
WIDEN = ("matching", "wide", "search")
ANY_SCORE = 1
# "Load more": each page looks further down every step (LOOK_AT, plus this many per guide
# already shown), down to LOOK_MOST candidates a step; past that there is no more
LOOK_DEEPER_PER_SHOWN = 2
LOOK_MOST = 300
# How many of each channel's candidates are read ahead of time. The rest (up to LOOK_AT) are
# read when somebody opens that channel's list: preloading all forty for every channel would
# come close to reading every guide there is (about 36,000 on the user's server).
PRELOAD_PER_CHANNEL = 10
# One read at a time per channel: a list opened twice while its guides are read reads once
READING_KEY = "live:app_guides:reading:{channel}"
READING_TTL = 180
# A full preload is redone after a source refresh once it is this old; in between, a
# refresh re-reads only that source's kept guides (the file has just been replaced)
FULL_PRELOAD_EVERY_SECONDS = 24 * 3600
# Where the preload has got to, for the settings page
PRELOAD_RUN_KEY = "live:app_guides:preload"
PRELOAD_RUN_TTL = 24 * 3600
# Held while a preload runs, so there is only ever one: every source's refresh on the daily
# refresh, "Load now" and the switch all start one, and each ends by reading every source's
# file. Every batch holds it again; a chain that died (the services restarted in the middle
# of it) lets go within this long, and the page stops saying it is loading.
PRELOAD_LOCK_KEY = "live:app_guides:preload:lock"
PRELOAD_LOCK_TTL = 30 * 60
# A guide a viewer's list had read is kept this long after it was last asked for, then
# left to the next full preload to want or not: otherwise every list ever opened adds guides
# that are kept, and read again at every refresh, for good
ASKED_KEPT_DAYS = 14


def enabled():
    return bool(app_devices.load_settings().get("guide_choice"))


def allowed_sources():
    """The EPG source ids to offer from, or an empty set for every one."""
    text = str(app_devices.load_settings().get("guide_choice_sources") or "")
    found = set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            found.add(int(part))
    return found


# ---------------------------------------------------------------------------------------
# The channel a login may see
# ---------------------------------------------------------------------------------------

def channel_for(user, given):
    """
    The channel `given` names (its UUID or its number id), if this login may see it; else None.

    The same rule the stream endpoints use: an admin sees every channel, anyone else the ones
    at or under their level and, when they have channel profiles, only those enabled in one
    of them. A channel somebody cannot watch is not one whose guide they may change, and
    telling them it exists is already more than they are owed.
    """
    from apps.channels.models import Channel

    given = str(given or "").strip()
    if not given or not app_devices._CHANNEL_ID.match(given):
        return None
    rows = Channel.objects.all()
    rows = rows.filter(id=int(given)) if given.isdigit() else rows.filter(uuid=given)
    level = getattr(user, "user_level", 0) or 0
    if level < 10:
        rows = rows.filter(user_level__lte=level)
        profiles = user.channel_profiles.all()
        if profiles.exists():
            rows = rows.filter(
                channelprofilemembership__enabled=True,
                channelprofilemembership__channel_profile__in=profiles,
            ).distinct()
    try:
        return rows.select_related("epg_data", "epg_data__epg_source").first()
    except Exception:
        return None


def _channel_json(channel):
    number = channel.channel_number
    if number is not None and float(number).is_integer():
        number = int(number)
    return {"uuid": str(channel.uuid), "id": channel.id, "name": channel.name, "number": number}


# ---------------------------------------------------------------------------------------
# What is on
# ---------------------------------------------------------------------------------------

def now_and_next(epg_ids):
    """
    {epg id: {"now": {title, start, end} or None, "next": {title, start} or None}}.

    Two queries for the whole list. Where a guide has two programmes covering this moment (a
    file that overlaps itself), the one that started last is taken, as the Guides tab does.
    """
    from django.db.models import Min
    from django.utils import timezone

    from apps.epg.models import ProgramData

    ids = {int(i) for i in epg_ids or () if i}
    if not ids:
        return {}
    moment = timezone.now()
    playing = {}
    for epg_id, title, start, end in (
        ProgramData.objects.filter(epg_id__in=ids, start_time__lte=moment, end_time__gt=moment)
        .order_by("epg_id", "start_time", "id")
        .values_list("epg_id", "title", "start_time", "end_time")
    ):
        playing[epg_id] = {"title": title or "", "start": _iso(start), "end": _iso(end)}
    firsts = dict(
        ProgramData.objects.filter(epg_id__in=ids, start_time__gt=moment)
        .values("epg_id")
        .annotate(first=Min("start_time"))
        .values_list("epg_id", "first")
    )
    upcoming = {}
    if firsts:
        for epg_id, title, start in ProgramData.objects.filter(
            epg_id__in=list(firsts), start_time__in=set(firsts.values())
        ).values_list("epg_id", "title", "start_time"):
            if firsts.get(epg_id) == start and epg_id not in upcoming:
                upcoming[epg_id] = {"title": title or "", "start": _iso(start)}
    return {epg_id: {"now": playing.get(epg_id), "next": upcoming.get(epg_id)} for epg_id in ids}


def _iso(moment):
    from datetime import timezone as tz

    return moment.astimezone(tz.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if moment else ""


# ---------------------------------------------------------------------------------------
# The candidates
# ---------------------------------------------------------------------------------------

def _offerable(epg_ids):
    """
    {epg id: {"name", "tvg_id", "source_id", "source"}} for the guides that may be offered:
    an active source, not a dummy one (it makes programmes up from the channel's name and
    says nothing about which channel this is), and one of the chosen sources if any are.
    """
    from apps.epg.models import EPGData

    only = allowed_sources()
    found = {}
    for row in EPGData.objects.filter(id__in=list(epg_ids)).values(
        "id", "name", "tvg_id", "epg_source_id", "epg_source__name",
        "epg_source__source_type", "epg_source__is_active",
    ):
        if not row["epg_source_id"] or row["epg_source__source_type"] == "dummy":
            continue
        if row["epg_source__is_active"] is False:
            continue
        if only and row["epg_source_id"] not in only:
            continue
        found[row["id"]] = {
            "name": row["name"] or "", "tvg_id": row["tvg_id"] or "",
            "source_id": row["epg_source_id"], "source": row["epg_source__name"] or "",
        }
    return found


def candidates(channel, how_many=LOOK_AT, step="matching"):
    """
    [(epg id, score)] this channel could be, best first, its current guide left out, at one
    of the WIDEN steps. Only guides that may be offered.

    Below the Guides tab's own bar (MIN_GUIDE_SCORE) -- and in the wide and search steps,
    all of it -- a guide is only offered when it is related to the channel (`_related`):
    it shares a word that says which channel it is, or its call sign, and is not from
    another country. Without that, "any confidence" offered a Belgian channel for "PBS |
    TOLEDO OHIO | WGTE" (the user's example): a list of maybes has to be maybes.
    """
    from apps.channels import channel_manager

    current = channel.epg_data_id
    limit = min(LOOK_MOST, how_many + 1)
    about = _about(channel)
    if step == "search":
        listed = []
        for words in _search_words(channel.name, about):
            listed += channel_manager.guide_candidates(
                channel.name, search=words, limit=limit, current=current, most=LOOK_MOST
            )
    else:
        listed = channel_manager.guide_candidates(
            channel.name, getattr(channel, "tvg_id", "") or "",
            limit=limit, current=current, min_score=ANY_SCORE, wide=(step == "wide"), most=LOOK_MOST,
        )
    wanted, seen = [], set()
    for entry in listed:
        if entry["id"] == current or entry["id"] in seen:
            continue
        sure = step == "matching" and (entry.get("score") or 0) >= channel_manager.MIN_GUIDE_SCORE
        if not sure and not _related(entry, about):
            continue
        seen.add(entry["id"])
        wanted.append((entry["id"], entry.get("score") or 0))
    offerable = _offerable(epg_id for epg_id, _ in wanted)
    return [(epg_id, score) for epg_id, score in wanted if epg_id in offerable][:how_many]


# Words that say nothing about which channel a name is: shared by half the guides there are
FILLER_WORDS = {
    "tv", "hd", "fhd", "uhd", "sd", "4k", "the", "and", "channel", "network", "live", "plus",
    "de", "la", "le", "el", "of", "news", "east", "west",
}
# Countries whose channels are carried in one another's guides often enough to be offered:
# an American station in a Canadian guide, ORF in a German one, VRT in a Dutch one. Anything
# else from another country is not this channel (a Belgian guide for an American station).
NEIGHBOURS = ({"us", "ca"}, {"de", "at", "ch"}, {"nl", "be"}, {"fr", "be", "ch"}, {"gb", "uk", "ie"})


def _about(channel):
    """
    What a channel's name says about which channel it is: its country (from the name, its
    group, or an American call sign), its call signs, its network, the parts of its name
    between the bars, and the words that say which channel it is.
    """
    import re

    from apps.channels import channel_manager, logo_library

    name = channel.name or ""
    group = getattr(getattr(channel, "channel_group", None), "name", "") or ""
    country = channel_manager._one_country(logo_library.country_of(name) or logo_library.country_of(group))
    signs = set()
    if country in channel_manager.CALL_SIGN_COUNTRIES:
        signs = {s.split("-")[0] for s in channel_manager.call_signs_of(name, country or "us")}
        if signs and not country:
            country = "us"
    plain = channel_manager._strip_country_box(name)
    parts = [p.strip() for p in re.split(r"[|/┃()\[\]]", plain) if p.strip()]
    words = {w for w in channel_manager.guide_words(plain) if w not in FILLER_WORDS and (len(w) >= 3 or w.isdigit())}
    return {
        "country": country, "signs": signs, "network": channel_manager.network_of(name),
        "parts": parts, "words": words,
    }


def _same_place(ours, theirs):
    if not ours or not theirs or ours == theirs:
        return True
    return any(ours in pair and theirs in pair for pair in NEIGHBOURS)


def _related(entry, about):
    """A guide that could be this channel: not another country, and something in common."""
    from apps.channels import channel_manager

    theirs = channel_manager._one_country(channel_manager._country_of_guide(entry))
    if not _same_place(about["country"], theirs):
        return False
    said = set(channel_manager.guide_words(entry.get("name") or ""))
    said |= set(channel_manager.guide_words((entry.get("tvg_id") or "").rsplit(".", 1)[0]))
    if about["signs"] & said:
        return True
    shared = {w for w in about["words"] & said if not w.isdigit()}
    # A local station is its town or call sign, not its network: every PBS station shares
    # "pbs", and PBS Dallas is not WGTE. A number alone ("1") says nothing either.
    if about["network"] and (about["signs"] or len(about["words"] - {about["network"]}) > 0):
        shared.discard(about["network"])
    return bool(shared)


def _search_words(name, about=None):
    """
    What the last step searches for, the most telling first: the call sign ("wgte"), each
    part of the name between the bars ("toledo ohio"), the place's first word ("toledo"),
    the network with it ("pbs toledo"), the whole name's words, then its longest word.
    """
    from apps.channels import channel_manager, epg_matching

    tries = []

    def add(text):
        text = " ".join((text or "").lower().split())
        if len(text) >= 3 and text not in tries:
            tries.append(text)

    about = about or {"signs": set(), "parts": [], "network": ""}
    for sign in sorted(about["signs"]):
        add(sign)
    network = about.get("network") or ""
    for part in about["parts"]:
        words = [w for w in epg_matching.normalize_name(part).split() if w not in FILLER_WORDS]
        if not words or words == [network] or " ".join(words) in about["signs"]:
            continue
        if network and network in words:
            continue
        add(" ".join(words))
        if len(words) > 1:
            add(words[0])
        if network:
            add(f"{network} {words[0]}")
    words = epg_matching.normalize_name(channel_manager._strip_country_box(name or "")).split()
    if words:
        add(" ".join(words))
        longest = max(words, key=len)
        if len(words) > 1:
            add(longest)
    return tries


def choices_for(channel, user=None, start_reading=True, shown=()):
    """
    The answer to GET /api/core/app-guide/: this channel's guide now, and the guides it
    could be that hold a programme right now, best first (at most LIST_MOST).

    A guide with nothing on now is left out, whether it was never read or read and found
    empty: whatever is picked has a title on screen at once. Candidates nobody has read yet
    are read in the background, and `reading` says so, so the app can ask once more.

    `shown` is "Load more": the guides the app already lists. They are left out, each step
    looks deeper the more there are, and `more` says whether asking again can find more.
    """
    shown = {int(i) for i in shown or () if str(i).strip().isdigit()}
    look = min(LOOK_MOST, LOOK_AT + LOOK_DEEPER_PER_SHOWN * len(shown))
    current_id = channel.epg_data_id
    on = now_and_next([current_id] if current_id else [])
    ids, guides = [], []
    deeper = False
    for step in WIDEN:
        found = candidates(channel, how_many=look, step=step)
        deeper = deeper or len(found) >= look
        ranked = [(epg_id, score) for epg_id, score in found if epg_id not in on and epg_id not in shown]
        if not ranked:
            continue
        ids += [epg_id for epg_id, _ in ranked]
        about = _offerable(epg_id for epg_id, _ in ranked)
        on.update(now_and_next([epg_id for epg_id, _ in ranked]))
        for epg_id, score in ranked:
            what = on.get(epg_id) or {}
            if not what.get("now"):
                continue
            entry = about[epg_id]
            guides.append({
                "epg_id": epg_id, "name": entry["name"], "tvg_id": entry["tvg_id"], "score": score,
                "source": {"id": entry["source_id"], "name": entry["source"]},
                "now": what["now"], "next": what.get("next"),
            })
            if len(guides) > LIST_MOST:
                break
        if len(guides) > LIST_MOST:
            break
    more = len(guides) > LIST_MOST or (deeper and look < LOOK_MOST)
    guides = guides[:LIST_MOST]

    current = None
    if current_id:
        epg = channel.epg_data
        what = on.get(current_id) or {}
        current = {
            "epg_id": current_id, "name": getattr(epg, "name", "") or "",
            "source": getattr(getattr(epg, "epg_source", None), "name", "") or "",
            "now": what.get("now"), "next": what.get("next"),
        }

    reading = False
    if start_reading and len(guides) < LIST_MOST:
        record, guides_tab = _record(), _guides_tab_reads()
        unread = _unread(
            [epg_id for epg_id in ids if not (on.get(epg_id) or {}).get("now")], record, guides_tab
        )
        if unread:
            reading = _read_for(channel, unread, look)
    return {
        "channel": _channel_json(channel), "current": current, "guides": guides,
        "reading": reading, "more": more,
    }


def _guides_tab_reads():
    from apps.channels import channel_manager

    return channel_manager.reads()


def _unread(epg_ids, record, guides_tab):
    """Of these, the ones nobody has read: not arrTV, not the Guides tab, and no channel
    uses them (Dispatcharr reads those itself)."""
    from apps.channels.models import Channel

    ids = [epg_id for epg_id in epg_ids
           if str(epg_id) not in record.get("ids", {}) and str(epg_id) not in guides_tab]
    used = set(Channel.objects.filter(epg_data_id__in=ids).values_list("epg_data_id", flat=True))
    return [epg_id for epg_id in ids if epg_id not in used]


def _read_for(channel, epg_ids, depth=LOOK_AT):
    """Read these of one channel's candidates, once per depth ("Load more" looks further
    down, and reads what it finds there); True while a read is on its way."""
    from core.utils import RedisClient

    try:
        redis_client = RedisClient.get_client()
        key = READING_KEY.format(channel=channel.id if depth == LOOK_AT else f"{channel.id}:{depth}")
        if not redis_client.set(key, "1", nx=True, ex=READING_TTL):
            return True
    except Exception:
        pass
    queued = read(epg_ids, how="asked")
    return bool(queued)


# ---------------------------------------------------------------------------------------
# Reading and keeping
# ---------------------------------------------------------------------------------------

def read(epg_ids, how="preload"):
    """
    Read these guides' programmes in the background, one pass of each source's file, and
    write down what was found under KEPT_KEY rather than the Guides tab's record (which is
    that page's, and shows on it as progress). Returns how many were asked for.
    """
    from apps.epg.models import EPGData

    by_source = {}
    other = []
    for row in EPGData.objects.filter(id__in=list(epg_ids)).values(
        "id", "tvg_id", "epg_source_id", "epg_source__source_type"
    ):
        kind = row["epg_source__source_type"]
        if kind == "dummy" or not row["epg_source_id"]:
            continue
        if kind == "xmltv" and (row["tvg_id"] or "").strip():
            by_source.setdefault(str(row["epg_source_id"]), []).append(row["id"])
        else:
            other.append(row["id"])
    if by_source:
        from apps.channels.tasks import read_guide_programmes

        read_guide_programmes.delay(by_source, record="app-guide", how=how)
    if other:
        # Not a file to go through (Schedules Direct): Dispatcharr's own task knows how.
        # Written down as read now, since that task writes nothing of its own down.
        from apps.epg.tasks import parse_programs_for_tvg_id

        for epg_id in other:
            parse_programs_for_tvg_id.delay(epg_id, force=True)
        note_read({epg_id: 1 for epg_id in other}, how=how)
    return sum(len(ids) for ids in by_source.values()) + len(other)


def _record():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=KEPT_KEY).first()
    value = row.value if row and isinstance(row.value, dict) else {}
    if not isinstance(value.get("ids"), dict):
        value = {**value, "ids": {}}
    return value


def note_read(found, how="preload"):
    """Write down what arrTV's reads found: {epg id: how many programmes}."""
    from django.utils import timezone

    from apps.channels.settings_rows import change_row

    if not found:
        return
    at = timezone.now().isoformat(timespec="seconds")

    def change(kept):
        ids = kept.setdefault("ids", {})
        for epg_id, how_many in found.items():
            before = ids.get(str(epg_id)) or {}
            # A guide somebody asked for stays "asked" when a preload reads it again, so a
            # later full preload that no longer wants it does not drop what a viewer used
            entry = {
                "found": int(how_many or 0), "at": at,
                "how": "asked" if "asked" in (how, before.get("how")) else how,
            }
            # When a viewer's list last asked for it: "at" moves with every re-read after a
            # refresh, so it cannot say whether anybody still wants the guide
            asked_at = at if how == "asked" else before.get("asked_at") or (before.get("at") if before.get("how") == "asked" else None)
            if asked_at:
                entry["asked_at"] = asked_at
            ids[str(epg_id)] = entry
        return len(ids)

    change_row(KEPT_KEY, "arrTV guides kept", change)


def kept_ids():
    """
    The guides arrTV read that hold programmes: the refresh's clean-up leaves them alone.
    Empty while the feature is off, which is stock.
    """
    if not enabled():
        return set()
    return {
        int(key) for key, what in _record().get("ids", {}).items()
        if key.isdigit() and isinstance(what, dict) and what.get("found")
    }


def forget_kept():
    """
    The feature went off: nothing is kept any more. The next refresh's clean-up then removes
    those programmes as stock would.
    """
    from core.models import CoreSettings

    CoreSettings.objects.filter(key=KEPT_KEY).delete()


def after_refresh(source_id):
    """
    An EPG source has just been refreshed: its file is new, and the programmes arrTV kept
    for its guides are the old file's. Read them again -- or, once a day, redo the whole
    preload, since channels and candidates change too.
    """
    if not enabled():
        return
    from datetime import datetime

    from django.utils import timezone

    record = _record()
    last = record.get("preloaded_at")
    try:
        stale = not last or (timezone.now() - datetime.fromisoformat(last)).total_seconds() >= FULL_PRELOAD_EVERY_SECONDS
    except (TypeError, ValueError):
        stale = True
    if stale:
        start_preload()
        return
    from apps.epg.models import EPGData

    ours = [int(k) for k in record.get("ids", {}) if k.isdigit()]
    again = list(
        EPGData.objects.filter(id__in=ours, epg_source_id=source_id).values_list("id", flat=True)
    )
    if again:
        logger.info(f"arrTV guides: re-reading {len(again)} kept guide(s) of refreshed source {source_id}")
        read(again, how="preload")


# ---------------------------------------------------------------------------------------
# Preloading every channel's best candidates
# ---------------------------------------------------------------------------------------

def _redis():
    from core.utils import RedisClient

    return RedisClient.get_client()


def start_preload():
    """
    Queue a full preload (switched on, "Load now", or a day since the last), unless one is
    running already. Returns whether one was started.
    """
    from django.utils import timezone

    from apps.channels.tasks import preload_guide_choices

    try:
        if not _redis().set(PRELOAD_LOCK_KEY, timezone.now().isoformat(), nx=True, ex=PRELOAD_LOCK_TTL):
            logger.info("arrTV guides: a preload is running already; not starting another")
            return False
    except Exception as e:
        # Without Redis there is nothing to hold; one more preload is better than none
        logger.debug(f"arrTV guides: could not hold the preload ({e})")
    _say_preload({"state": "working", "done": 0, "total": 0, "stage": "finding each channel's guides"})
    preload_guide_choices.delay()
    return True


def _hold_preload():
    """Every batch: still running, so still held."""
    try:
        _redis().expire(PRELOAD_LOCK_KEY, PRELOAD_LOCK_TTL)
    except Exception:
        pass


def end_preload(state="done"):
    """The preload finished, or stopped because the feature went off: let go of it."""
    try:
        _redis().delete(PRELOAD_LOCK_KEY)
    except Exception:
        pass
    if state != "done":
        _say_preload({"state": state, "stage": ""})


def preload_running():
    try:
        return bool(_redis().exists(PRELOAD_LOCK_KEY))
    except Exception:
        return False


def preload_batch(offset, batch, wanted_so_far):
    """
    One batch of channels: their best PRELOAD_PER_CHANNEL candidates. Returns (the ids
    wanted so far, whether there are more channels).
    """
    from apps.channels.models import Channel

    _hold_preload()
    channels = list(Channel.objects.order_by("id")[offset:offset + batch])
    wanted = set(wanted_so_far or ())
    for channel in channels:
        try:
            wanted.update(epg_id for epg_id, _ in candidates(channel, PRELOAD_PER_CHANNEL))
        except Exception as e:
            logger.debug(f"arrTV guides: no candidates for {channel.name}: {e}")
    total = Channel.objects.count()
    _say_preload({
        "state": "working", "done": min(offset + len(channels), total), "total": total,
        "stage": "finding each channel's guides",
    })
    return wanted, offset + batch < total


def finish_preload(wanted):
    """
    Every channel looked at: read the guides wanted that are not in use (a channel's own
    guide Dispatcharr reads itself), and make the record hold just these, plus what viewers
    asked for.
    """
    from django.utils import timezone

    from apps.channels.models import Channel
    from apps.channels.settings_rows import change_row

    from datetime import datetime, timedelta

    wanted = {int(i) for i in wanted or ()}
    used = set(Channel.objects.filter(epg_data_id__in=wanted).values_list("epg_data_id", flat=True))
    to_read = sorted(wanted - used)
    now = timezone.now()
    at = now.isoformat(timespec="seconds")
    asked_since = now - timedelta(days=ASKED_KEPT_DAYS)

    def asked_lately(what):
        if what.get("how") != "asked":
            return False
        try:
            return datetime.fromisoformat(what.get("asked_at") or what.get("at") or "") >= asked_since
        except (TypeError, ValueError):
            return False

    def change(kept):
        ids = kept.setdefault("ids", {})
        for key in list(ids):
            what = ids[key] or {}
            if key.isdigit() and int(key) in wanted:
                continue
            if not asked_lately(what):
                del ids[key]
        kept["preloaded_at"] = at
        kept["preloaded"] = len(to_read)

    change_row(KEPT_KEY, "arrTV guides kept", change)
    asked = read(to_read, how="preload") if to_read else 0
    _say_preload({"state": "done", "done": asked, "total": len(to_read), "stage": ""})
    end_preload()
    logger.info(f"arrTV guides: reading the programmes of {asked} guide(s) on offer")
    return asked


def _say_preload(mapping):
    from core.utils import RedisClient

    try:
        redis_client = RedisClient.get_client()
        redis_client.hset(PRELOAD_RUN_KEY, mapping={k: str(v) for k, v in mapping.items()})
        redis_client.expire(PRELOAD_RUN_KEY, PRELOAD_RUN_TTL)
    except Exception as e:
        logger.debug(f"arrTV guides: could not say how the preload is going ({e})")


def preload_state():
    """For the settings page: how the preload is going and what is kept."""
    from core.utils import RedisClient

    state = {}
    try:
        raw = RedisClient.get_client().hgetall(PRELOAD_RUN_KEY) or {}
        for key, value in raw.items():
            key = key.decode() if isinstance(key, bytes) else key
            state[key] = value.decode() if isinstance(value, bytes) else value
    except Exception:
        pass
    record = _record()
    ids = record.get("ids", {})
    said = state.get("state") or ""
    if said == "working" and not preload_running():
        # It said it was working and nothing holds it any more: it died part way (a restart)
        said = "stopped"
    return {
        "state": said,
        "stage": state.get("stage") or "",
        "done": int(state.get("done") or 0),
        "total": int(state.get("total") or 0),
        "kept": sum(1 for what in ids.values() if isinstance(what, dict) and what.get("found")),
        "read": len(ids),
        "preloaded_at": record.get("preloaded_at") or "",
    }


# ---------------------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------------------

class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def choose(channel, epg_id, request, user):
    """
    Put this channel on this guide, for everybody, and write down who did it.

    Only a guide it would have offered: from a source that may be offered, and holding a
    programme now, so the app can never put a channel on an empty guide. The same guide the
    channel is already on is not an error -- it means "this one is right", and the Guides
    tab stops suggesting another for the channel.
    """
    from apps.channels import guide_manager
    from apps.epg.models import EPGData

    try:
        epg_id = int(epg_id)
    except (TypeError, ValueError):
        raise Refused(404, "That guide no longer exists")
    if not EPGData.objects.filter(id=epg_id).exists():
        raise Refused(404, "That guide no longer exists")
    if epg_id != channel.epg_data_id and epg_id not in _offerable([epg_id]):
        raise Refused(404, "That guide is not one to choose from")
    on = now_and_next([epg_id]).get(epg_id) or {}
    if not on.get("now"):
        raise Refused(409, "That guide has nothing on now; choose another")

    who = author(request, user)
    guide_manager.apply({channel.id: epg_id}, extra={channel.id: _record_of_choice(channel, epg_id, who)})
    channel.refresh_from_db()

    guide = EPGData.objects.select_related("epg_source").get(id=epg_id)
    source = guide.epg_source.name if guide.epg_source else ""
    device = who["device_name"] or who["device"] or "an unknown device"
    logger.info(
        f"Guide: {channel.name} → {guide.name} ({source}), by {who['username']} on {device} "
        f"from {who['ip'] or 'an unknown address'}"
    )
    return {
        "ok": True,
        "channel": _channel_json(channel),
        "guide": {
            "epg_id": epg_id, "name": guide.name or "", "source": source,
            "now": on.get("now"), "next": on.get("next"),
        },
    }


# How many earlier arrTV changes of one channel are kept, for putting back one at a time
HISTORY_MOST = 10


def _arrtv_entry(channel):
    """The channel's "chosen" entry when it is an arrTV change still in force, else None."""
    from apps.channels import guide_manager

    entry = guide_manager.load_chosen().get(str(channel.id)) or {}
    by = entry.get("by")
    if not isinstance(by, dict) or by.get("via") != "arrTV":
        return None
    if (entry.get("epg") or None) != (channel.epg_data_id or None):
        # Changed underneath since (the Lineup, Dispatcharr's own matching): not in force
        return None
    return entry


def _record_of_choice(channel, epg_id, who):
    """
    What goes into the channel's "chosen" entry for this choice.

    A new guide: who chose it and the guide it replaced, with the arrTV changes before it
    kept underneath (newest first), so Put back can go back one change at a time rather
    than only ever to the last guide.

    The guide it is already on ("this one is right"): the change being confirmed stays as
    it is -- who made it and what it replaced -- and who confirmed it is added. Before, a
    confirmation wrote the guide over what it replaced, and the change could no longer be
    put back.
    """
    from django.utils import timezone

    previous = _arrtv_entry(channel)
    if epg_id == channel.epg_data_id and previous:
        kept = {k: previous[k] for k in ("at", "was", "by", "history", "confirmed") if k in previous}
        confirmed = list(previous.get("confirmed") or [])[-(HISTORY_MOST - 1):]
        kept["confirmed"] = confirmed + [{**who, "at": timezone.now().isoformat(timespec="seconds")}]
        return kept
    history = []
    if previous:
        history = [{k: v for k, v in previous.items() if k != "history"}] + list(previous.get("history") or [])
    return {"was": channel.epg_data_id, "by": who, "history": history[:HISTORY_MOST]}


def author(request, user):
    """Who made a choice: the login, the device it says it is, and the address it came from."""
    from dispatcharr.utils import get_client_ip

    device = app_devices._said(request, "device")
    if not app_devices._DEVICE_ID.match(device or ""):
        device = ""
    name = app_devices._said(request, "device_name")[:120] if device else ""
    try:
        ip = get_client_ip(request) or ""
    except Exception:
        ip = ""
    return {
        "via": "arrTV",
        "user_id": getattr(user, "id", None),
        "username": getattr(user, "username", "") or "",
        "device": device or None,
        "device_name": name or None,
        "ip": ip,
    }


def changes():
    """The guide changes made from arrTV, newest first, for Settings → arrTV."""
    from apps.channels import guide_manager
    from apps.epg.models import EPGData

    chosen = guide_manager.load_chosen()
    rows = []
    for channel_id, entry in chosen.items():
        by = (entry or {}).get("by")
        if not isinstance(by, dict) or by.get("via") != "arrTV":
            continue
        if not str(channel_id).isdigit():
            continue
        rows.append({**entry, "channel": int(channel_id)})
    names = dict(
        EPGData.objects.filter(
            id__in=[i for row in rows for i in (row.get("epg"), row.get("was")) if i]
        ).values_list("id", "name")
    )
    from apps.channels.models import Channel

    channels = {
        cid: (name, epg_id, uuid) for cid, name, epg_id, uuid in Channel.objects.filter(
            id__in=[row["channel"] for row in rows]
        ).values_list("id", "name", "epg_data_id", "uuid")
    }
    # What each guide shows now, and how much it holds: a change is judged against the
    # picture, and an empty guide has to say so rather than look like a working one
    from django.db.models import Count

    from apps.epg.models import ProgramData

    guide_ids = {i for row in rows for i in (row.get("epg"), row.get("was")) if i}
    on = now_and_next(guide_ids)
    holds = dict(
        ProgramData.objects.filter(epg_id__in=guide_ids).values("epg_id")
        .annotate(n=Count("id")).values_list("epg_id", "n")
    )
    for row in rows:
        name, on_now, uuid = channels.get(row["channel"], ("", None, None))
        row["channel_name"] = name
        row["channel_uuid"] = str(uuid) if uuid else ""
        for key, epg_id in (("guide", row.get("epg")), ("was", row.get("was"))):
            what = on.get(epg_id) or {}
            row[f"{key}_now"] = what.get("now")
            row[f"{key}_next"] = what.get("next")
            row[f"{key}_holds"] = holds.get(epg_id, 0) if epg_id else None
        row["guide_name"] = names.get(row.get("epg"), row.get("name") or "")
        row["was_name"] = names.get(row.get("was"), "") if row.get("was") else ""
        # Changed underneath since (the Lineup, Dispatcharr's own matching): putting it back
        # would undo that change, not this one, so the page offers only to take it off the list
        row["in_force"] = (row.get("epg") or None) == (on_now or None)
        # How many arrTV changes before this one Put back can go on to, one at a time
        row["earlier"] = len(row.pop("history", None) or [])
        row["confirmed"] = [
            {k: c.get(k) for k in ("username", "device_name", "device", "ip", "at")}
            for c in (row.get("confirmed") or []) if isinstance(c, dict)
        ]
    rows.sort(key=lambda row: row.get("at") or "", reverse=True)
    return rows


def put_back(channel_id):
    """
    Undo one arrTV choice: the channel goes back to the guide it had (through the Guides
    tab's apply, so it is saved the proper way), and the record of the choice goes.
    """
    from apps.channels import guide_manager
    from apps.channels.models import Channel

    entry = guide_manager.load_chosen().get(str(channel_id))
    if not entry or not isinstance(entry.get("by"), dict) or entry["by"].get("via") != "arrTV":
        raise Refused(404, "No guide change from arrTV for that channel")
    on_now = Channel.objects.filter(id=int(channel_id)).values_list("epg_data_id", flat=True).first()
    if (entry.get("epg") or None) != (on_now or None):
        raise Refused(409, "The channel's guide was changed again since; putting this back would undo that")
    was = entry.get("was")
    history = list(entry.get("history") or [])
    earlier = history[0] if history and (history[0].get("epg") or None) == (was or None) else None
    if earlier:
        # The guide it goes back to was itself an arrTV change: that one is in force again,
        # and can be put back in its turn
        guide_manager.apply({int(channel_id): was}, extra={int(channel_id): {**earlier, "history": history[1:]}})
    else:
        guide_manager.apply({int(channel_id): was})
        guide_manager.unchoose(int(channel_id))
    logger.info(f"Guide: channel {channel_id} put back on the guide it had before arrTV changed it")
    return {"ok": True}


def keep(channel_id):
    """
    An admin looked at an arrTV change and it is right: it leaves the list of changes. The
    channel stays settled on that guide, as any guide chosen on the Guides tab is; only who
    chose it, and what it replaced, are no longer kept.
    """
    from apps.channels import guide_manager
    from apps.channels.settings_rows import change_row

    def change(chosen):
        entry = chosen.get(str(channel_id))
        if not entry or not isinstance(entry.get("by"), dict) or entry["by"].get("via") != "arrTV":
            return False
        for key in ("by", "was", "history", "confirmed"):
            entry.pop(key, None)
        return True

    if not change_row(guide_manager.CHOSEN_KEY, "Guides chosen", change):
        raise Refused(404, "No guide change from arrTV for that channel")
    return {"ok": True}
