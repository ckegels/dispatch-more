"""What the live proxy is doing, for the Diagnostics page in the settings.

Read-only, and everything it reads is short-lived in Redis, so it only ever shows recent
activity:

- channel starts, with where the time went (see apps.proxy.live_proxy.timing);
- Channel Switch Overlap: the accounts with their slots, and the last switches (see
  apps.proxy.live_proxy.probation.record_event).
"""

import logging
import time

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin
from core.utils import RedisClient

from . import health
from . import media_servers
from . import probation
from . import recovery
from . import timing

logger = logging.getLogger("live_proxy")


def _viewer_name(event, usernames, redis_client=None):
    """A short, readable viewer: who a media server said it is, its login, or address and app."""
    device = event.get("server_device")
    if device and redis_client:
        # "Ckegels · Chrome", which is what the media server calls whoever is watching
        name = media_servers.device_name(redis_client, device)
        if name:
            return name
    username = usernames.get(event.get("user_id"))
    if username and event.get("app"):
        return f"{username} · {event['app']}"
    if username:
        return username
    if event.get("app"):
        return f"{event['ip']} · {event['app']}"
    return event.get("ip") or "unknown"


def _account_rows(redis_client):
    """
    The accounts the switches tab is about: only those with the overlap enabled.

    An account without it never produces a switch, so listing it would say nothing; this is
    also why the tab looks empty on a setup where the feature is not switched on anywhere.
    """
    from apps.m3u.connection_pool import get_profile_connection_count
    from apps.m3u.models import M3UAccountProfile

    rows = []
    profiles = M3UAccountProfile.objects.select_related("m3u_account").filter(
        is_active=True, m3u_account__is_active=True
    )
    for profile in profiles:
        account = profile.m3u_account
        if not probation.account_allows_probation(account):
            continue
        rows.append({
            "account": account.name,
            "profile": profile.name,
            "in_use": get_profile_connection_count(profile, redis_client),
            "max_streams": profile.max_streams,
            "held_slots": probation.slots_held_for_others(redis_client, profile),
            "lan_subnets": [str(network) for network in probation.account_lan_subnets(account)],
            "stop_skipped": probation.account_stops_skipped_channels(account),
            "switch_preference": probation.account_switch_preference(account),
        })
    return sorted(rows, key=lambda row: (row["account"], row["profile"]))


def _channel_names(uuids):
    from apps.channels.models import Channel

    uuids = {uuid for uuid in uuids if uuid}
    if not uuids:
        return {}
    return {
        str(uuid): name
        for uuid, name in Channel.objects.filter(uuid__in=uuids).values_list("uuid", "name")
    }


def _usernames(user_ids):
    from apps.accounts.models import User

    user_ids = {int(user_id) for user_id in user_ids if user_id and str(user_id).isdigit()}
    if not user_ids:
        return {}
    return {
        str(user_id): username
        for user_id, username in User.objects.filter(id__in=user_ids).values_list("id", "username")
    }


def _phases(value):
    """"label=seconds|..." as a list, with how long each step took on its own."""
    phases, previous = [], 0.0
    for phase in (value or "").split("|"):
        label, _sep, seconds = phase.partition("=")
        if not seconds:
            continue
        try:
            at = float(seconds)
        except ValueError:
            continue
        phases.append({"label": label, "at": at, "took": max(at - previous, 0.0)})
        previous = at
    return phases


def _number(value, default=0.0):
    """A number out of a record Redis holds as text, where a field can be empty or half written."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _section(name, read, empty):
    """
    One part of the page, read on its own. The page is where someone goes when something
    is wrong: one record it cannot read is logged and left out, rather than taking every
    other part of the page down with it.
    """
    try:
        return read()
    except Exception:
        logger.exception(f"Diagnostics: could not read {name}, left out")
        return empty


def _starts(redis_client):
    """Recent channel starts with each phase: how far in it was reached, and how long it took."""
    starts = []
    for record in timing.recent_starts(redis_client):
        try:
            starts.append(_start(record))
        except Exception:
            logger.exception(f"Diagnostics: skipped a channel start it could not read: {record}")
    return starts


def _start(record):
    phases = _phases(record.get("phases"))
    return {
        "time": _number(record.get("time")),
        "channel": record.get("channel", ""),
        "client": probation.app_name(
            record.get("client"), record.get("client_ip")
        ) or "media server",
        "total": _number(record.get("total")),
        "slowest": record.get("slowest", ""),
        "phases": phases,
        # What the media server did with it afterwards, when one is configured
        "server_user": record.get("server_user", ""),
        "server_player": record.get("server_player", ""),
        "server_title": record.get("server_title", ""),
        "server_name": record.get("server_name", ""),
        "server_decision": record.get("server_decision", ""),
        "server_speed": record.get("server_speed", ""),
        "server_buffering": _number(record.get("server_buffering")),
        # What the server itself did, stage by stage, once it has a session
        "server_phases": _phases(record.get("server_phases")),
        "server_gave_up": record.get("server_gave_up") == "1",
        "server_playing_is_certain": record.get("server_playing_is_certain") == "1",
    }


def _events(redis_client):
    events = probation.recent_events(redis_client)
    channels = _channel_names(
        [event.get("channel") for event in events] + [event.get("from_channel") for event in events]
    )
    usernames = _usernames([event.get("user_id") for event in events])
    return [
        {
            "time": _number(event.get("time")),
            "viewer": _viewer_name(event, usernames, redis_client),
            "from_channel": channels.get(event.get("from_channel", ""), ""),
            "channel": channels.get(event.get("channel", ""), ""),
            "account": event.get("account", ""),
            "action": event.get("action", ""),
            "result": event.get("result", ""),
        }
        for event in events
    ]


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def diagnostics(request):
    """Channel starts, accounts and recent switches for the Diagnostics page.

    POST {"keep_seconds": ...} changes how long switches are kept, and returns the page as a
    GET does, so the page shows the new setting straight away.
    """
    redis_client = RedisClient.get_client()
    if not redis_client:
        return JsonResponse({"error": "Redis not available"}, status=500)

    if request.method == "POST":
        try:
            if "keep_seconds" in request.data:
                probation.set_event_ttl(redis_client, request.data.get("keep_seconds"))
            if "channel_health" in request.data:
                # Sampling costs a few writes a second whether or not anyone is looking,
                # so it can be switched off
                wanted = request.data.get("channel_health") or {}
                health.save_settings({
                    key: wanted[key] for key in health.DEFAULTS if key in wanted
                })
        except (TypeError, ValueError) as e:
            return JsonResponse({"error": str(e)}, status=400)

    enabled = probation.in_use()

    return JsonResponse({
        "starts": _section("channel starts", lambda: _starts(redis_client), []),
        # What has happened to the channels themselves (see recovery.py)
        "health": _section("channel events", lambda: recovery.recent_events(redis_client), []),
        # What the channels are doing now, and what the ones that stopped ended on
        "running": _section("running channels", lambda: health.running_now(redis_client), []),
        "stopped": _section(
            "stopped channels",
            lambda: health.stopped_lately(redis_client, probation.event_ttl(redis_client)),
            [],
        ),
        "channel_health": _section("channel health settings", health.settings, dict(health.DEFAULTS)),
        "enabled": enabled,
        "accounts": _section("accounts", lambda: _account_rows(redis_client), []) if enabled else [],
        "events": _section("switches", lambda: _events(redis_client), []) if enabled else [],
        "keep_seconds": probation.event_ttl(redis_client),
        "keep_choices": list(probation.EVENT_TTL_CHOICES),
        "timestamp": time.time(),
    })
