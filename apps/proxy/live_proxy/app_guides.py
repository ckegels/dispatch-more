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
# matcher's list it looks to find that many with something on now
LIST_MOST = 20
LOOK_AT = 40
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


def candidates(channel, how_many=LOOK_AT):
    """
    [(epg id, score)] this channel could be, best first, its current guide left out.

    The Guides tab's own matcher, with the settings the Guides tab has (which sources are
    matched against at all), and only guides that may be offered.
    """
    from apps.channels import channel_manager

    current = channel.epg_data_id
    listed = channel_manager.guide_candidates(
        channel.name, getattr(channel, "tvg_id", "") or "",
        limit=min(50, how_many + 1), current=current,
    )
    wanted = [(entry["id"], entry.get("score") or 0) for entry in listed if entry["id"] != current]
    offerable = _offerable(epg_id for epg_id, _ in wanted)
    return [(epg_id, score) for epg_id, score in wanted if epg_id in offerable][:how_many]


def choices_for(channel, user=None, start_reading=True):
    """
    The answer to GET /api/core/app-guide/: this channel's guide now, and the guides it
    could be that hold a programme right now, best first (at most LIST_MOST).

    A guide with nothing on now is left out, whether it was never read or read and found
    empty: whatever is picked has a title on screen at once. Candidates nobody has read yet
    are read in the background, and `reading` says so, so the app can ask once more.
    """
    ranked = candidates(channel)
    ids = [epg_id for epg_id, _ in ranked]
    about = _offerable(ids)
    current_id = channel.epg_data_id
    on = now_and_next(ids + ([current_id] if current_id else []))

    guides = []
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
        if len(guides) >= LIST_MOST:
            break

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
        unread = [epg_id for epg_id in ids if not (on.get(epg_id) or {}).get("now") and _never_read(epg_id)]
        if unread:
            reading = _read_for(channel, unread)
    return {"channel": _channel_json(channel), "current": current, "guides": guides, "reading": reading}


def _never_read(epg_id, record=None, guides_tab=None):
    """Whether nobody has read this guide's programmes: not arrTV, not the Guides tab, and
    no channel uses it (Dispatcharr reads those itself)."""
    from apps.channels import channel_manager
    from apps.channels.models import Channel

    record = _record() if record is None else record
    if str(epg_id) in record.get("ids", {}):
        return False
    guides_tab = channel_manager.reads() if guides_tab is None else guides_tab
    if str(epg_id) in guides_tab:
        return False
    return not Channel.objects.filter(epg_data_id=epg_id).exists()


def _read_for(channel, epg_ids):
    """Read these of one channel's candidates, once; True while a read is on its way."""
    from core.utils import RedisClient

    try:
        redis_client = RedisClient.get_client()
        if not redis_client.set(READING_KEY.format(channel=channel.id), "1", nx=True, ex=READING_TTL):
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
            ids[str(epg_id)] = {
                "found": int(how_many or 0), "at": at,
                "how": "asked" if "asked" in (how, before.get("how")) else how,
            }
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

def start_preload():
    """Queue a full preload (switched on, "Load now", or a day since the last)."""
    from apps.channels.tasks import preload_guide_choices

    _say_preload({"state": "working", "done": 0, "total": 0, "stage": "finding each channel's guides"})
    preload_guide_choices.delay()


def preload_batch(offset, batch, wanted_so_far):
    """
    One batch of channels: their best PRELOAD_PER_CHANNEL candidates. Returns (the ids
    wanted so far, whether there are more channels).
    """
    from apps.channels.models import Channel

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

    wanted = {int(i) for i in wanted or ()}
    used = set(Channel.objects.filter(epg_data_id__in=wanted).values_list("epg_data_id", flat=True))
    to_read = sorted(wanted - used)
    at = timezone.now().isoformat(timespec="seconds")

    def change(kept):
        ids = kept.setdefault("ids", {})
        for key in list(ids):
            what = ids[key] or {}
            if what.get("how") != "asked" and (not key.isdigit() or int(key) not in wanted):
                del ids[key]
        kept["preloaded_at"] = at
        kept["preloaded"] = len(to_read)

    change_row(KEPT_KEY, "arrTV guides kept", change)
    asked = read(to_read, how="preload") if to_read else 0
    _say_preload({"state": "done", "done": asked, "total": len(to_read), "stage": ""})
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
    return {
        "state": state.get("state") or "",
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

    was = channel.epg_data_id
    who = author(request, user)
    guide_manager.apply({channel.id: epg_id}, extra={channel.id: {"was": was, "by": who}})
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

    channel_names = dict(
        Channel.objects.filter(id__in=[row["channel"] for row in rows]).values_list("id", "name")
    )
    for row in rows:
        row["channel_name"] = channel_names.get(row["channel"], "")
        row["guide_name"] = names.get(row.get("epg"), row.get("name") or "")
        row["was_name"] = names.get(row.get("was"), "") if row.get("was") else ""
    rows.sort(key=lambda row: row.get("at") or "", reverse=True)
    return rows


def put_back(channel_id):
    """
    Undo one arrTV choice: the channel goes back to the guide it had (through the Guides
    tab's apply, so it is saved the proper way), and the record of the choice goes.
    """
    from apps.channels import guide_manager

    entry = guide_manager.load_chosen().get(str(channel_id))
    if not entry or not isinstance(entry.get("by"), dict) or entry["by"].get("via") != "arrTV":
        raise Refused(404, "No guide change from arrTV for that channel")
    guide_manager.apply({int(channel_id): entry.get("was")})
    guide_manager.unchoose(int(channel_id))
    logger.info(f"Guide: channel {channel_id} put back on the guide it had before arrTV changed it")
    return {"ok": True}
