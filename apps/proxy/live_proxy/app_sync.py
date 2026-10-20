"""
arrTV's start-up check (fork/guide-loading-plan.md): instead of downloading the channel list,
every server guide record and the guide at each launch, the app asks here once whether any of
it changed since it last looked, and fetches only what did.

On a 1.4 GB Google TV those launch downloads filled the app's whole 256 MB heap for a minute or
two: the picture froze with 13 s buffered and the app said "Reconnecting". The heaviest part was
`/api/epg/epgdata/`, every guide record of every source on the server (hundreds of thousands
here) to learn the tvg-id of the ~1500 guides the login's channels use. `guide_links` answers
that question for those channels only.

Read-only, for any logged-in user, about what that login can already read through Dispatcharr's
own endpoints, so nothing here needs a switch: an app that never asks changes nothing.

Stamps are hashes of the values the app uses, not `updated_at`: bulk `update()` calls (the
Channel Manager, Show Groups, guide choice) do not touch `auto_now` fields, and a stamp that
misses a change would leave a TV on an old lineup until the next full download.
"""

import hashlib
import json
import threading
import time

SYNC_URL = "/api/core/app-sync/"
GUIDE_LINKS_URL = "/api/core/app-guide-links/"
GUIDE_MARKS_URL = "/api/core/app-guide-marks/"

# One answer per login for this long: the app asks at start, and its background check, the
# live socket and a second device of the same login may ask within seconds of each other.
CACHE_SECONDS = 10
_CACHE = {}
_CACHE_LOCK = threading.Lock()

# What the app reads from /api/channels/channels/ (arrTV DispatcharrChannel), plus what decides
# whether it lists a channel at all. The effective values are what an override puts in front.
LINEUP_FIELDS = (
    "id", "uuid", "name", "channel_number", "channel_group_id", "logo_id", "tvg_id",
    "epg_data_id", "is_catchup", "catchup_days", "hidden_from_output", "user_level", "is_adult",
)
EFFECTIVE_FIELDS = (
    "effective_name", "effective_channel_number", "effective_channel_group_id",
    "effective_logo_id", "effective_tvg_id", "effective_epg_data_id",
)


def _digest(rows):
    """A short, stable hash of rows of plain values (order matters, so callers sort)."""
    h = hashlib.sha1()
    for row in rows:
        h.update(json.dumps(row, default=str, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()[:20]


def lineup_queryset(user):
    """
    The channels /api/channels/channels/ lists for this login, by the channel list's own rules
    (level, adult content, channel profiles, hidden channels): taken from ChannelViewSet itself
    so the stamp cannot drift from what the app downloads when upstream changes those rules.
    """
    from django.http import HttpRequest
    from rest_framework.request import Request

    from apps.channels.api_views import ChannelViewSet
    from apps.channels.managers import with_effective_values

    raw = HttpRequest()
    raw.method = "GET"
    request = Request(raw)
    request.user = user
    view = ChannelViewSet()
    view.request = request
    view.action = "list"
    view.format_kwarg = None
    view.args = ()
    view.kwargs = {}
    # The list's prefetches are for its serializer; plain values need none of them
    return with_effective_values(view.get_queryset().prefetch_related(None))


def _lineup(user):
    rows = list(lineup_queryset(user).order_by("id").values_list(*LINEUP_FIELDS, *EFFECTIVE_FIELDS))
    return [list(r) for r in rows]


def _groups(rows):
    """Names of the groups the lineup's channels are in (the app shows them as group pills)."""
    from apps.channels.models import ChannelGroup

    at = len(LINEUP_FIELDS) + EFFECTIVE_FIELDS.index("effective_channel_group_id")
    ids = {r[at] for r in rows if r[at] is not None}
    return [list(g) for g in ChannelGroup.objects.filter(id__in=ids).order_by("id").values_list("id", "name")]


def _profiles(user):
    """
    The login's own channel profiles and their enabled channels: arrTV filters its lineup by
    them itself (its account filter), also for an admin, whom the channel list does not filter.
    """
    from apps.channels.models import ChannelProfileMembership

    profile_ids = list(user.channel_profiles.order_by("id").values_list("id", flat=True))
    if not profile_ids:
        return []
    members = ChannelProfileMembership.objects.filter(channel_profile_id__in=profile_ids, enabled=True)
    return [profile_ids, [list(m) for m in members.order_by("channel_profile_id", "channel_id")
                          .values_list("channel_profile_id", "channel_id")]]


def _epg_ids(rows):
    """Every guide the lineup's channels point at, by their own field or by an override."""
    own = LINEUP_FIELDS.index("epg_data_id")
    effective = len(LINEUP_FIELDS) + EFFECTIVE_FIELDS.index("effective_epg_data_id")
    return {r[i] for r in rows for i in (own, effective) if r[i] is not None}


def guide_links(epg_ids):
    """
    The guide records these channels use, shaped like /api/epg/epgdata/ rows (id, tvg_id,
    epg_source) so the app reads them with the decoder it already has, plus the source's type
    (arrTV GH #53 asks for it separately otherwise).
    """
    from apps.epg.models import EPGData

    rows = (
        EPGData.objects.filter(id__in=epg_ids)
        .order_by("id")
        .values_list("id", "tvg_id", "epg_source_id", "epg_source__source_type")
    )
    return [{"id": i, "tvg_id": t, "epg_source": s, "source_type": st} for i, t, s, st in rows]


def guide_marks(epg_ids):
    """
    A short fingerprint of each guide's programmes ({epg id: mark}): the count and the newest
    row, which change when a refresh re-imports the guide or anything else writes to it. The app
    compares them with its own and fetches the programmes of the guides that changed only
    (`/api/epg/grid/?epg_ids=`), instead of the whole 24 h grid -- 41,000 programmes, 16 MB --
    at every source refresh and Show Groups change (measured 2026-10-20). A guide without
    programmes is "0".
    """
    from django.db.models import Count, Max

    from apps.epg.models import ProgramData

    marks = {str(i): "0" for i in epg_ids}
    total = 0
    rows = (
        ProgramData.objects.filter(epg_id__in=epg_ids)
        .values("epg_id")
        .annotate(n=Count("id"), last=Max("id"))
        .order_by()
    )
    for row in rows:
        total += row["n"]
        marks[str(row["epg_id"])] = hashlib.sha1(f"{row['n']}:{row['last']}".encode()).hexdigest()[:8]
    return marks, total


def _sources(links):
    """The sources the login's guides come from, with their last successful refresh."""
    from apps.epg.models import EPGSource

    used = {g["epg_source"] for g in links if g["epg_source"] is not None}
    return [
        {"id": s[0], "type": s[1], "active": s[2], "refreshed": s[3].isoformat() if s[3] else None}
        for s in EPGSource.objects.filter(id__in=used).order_by("id")
        .values_list("id", "source_type", "is_active", "updated_at")
    ]


def answer(user):
    """What GET /api/core/app-sync/ answers this login (cached CACHE_SECONDS)."""
    key = getattr(user, "id", None)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    rows = _lineup(user)
    epg_ids = _epg_ids(rows)
    links = guide_links(epg_ids)
    marks, programmes = guide_marks(epg_ids)
    result = {
        "sync": 1,
        "channels": len(rows),
        "lineup": _digest(rows + [["groups"]] + _groups(rows) + [["profiles"]] + [_profiles(user)]),
        "guide_links": _digest([[g["id"], g["tvg_id"], g["epg_source"], g["source_type"]] for g in links]),
        # Only this login's guides count: a refresh of a source none of its channels use (the
        # user's server refreshes 43 in turn, several an hour) is no reason to download anything
        "guides": _digest(sorted(marks.items())),
        "programmes": programmes,
        "sources": _sources(links),
        "guide_links_url": GUIDE_LINKS_URL,
        "guide_marks_url": GUIDE_MARKS_URL,
    }
    with _CACHE_LOCK:
        _CACHE[key] = (now, result, marks)
    return result


def marks_for(user):
    """What GET /api/core/app-guide-marks/ answers this login: {"guides": stamp, "marks": {...}}."""
    result = answer(user)
    with _CACHE_LOCK:
        hit = _CACHE.get(getattr(user, "id", None))
    marks = hit[2] if hit and hit[1] is result else guide_marks(_epg_ids(_lineup(user)))[0]
    return {"guides": result["guides"], "marks": marks}


def links_for(user):
    """What GET /api/core/app-guide-links/ answers this login."""
    return guide_links(_epg_ids(_lineup(user)))
