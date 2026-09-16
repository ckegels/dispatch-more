"""What Channel Switch Overlap is doing, for its page in the settings.

Read-only: the accounts with their slots, and the last switches (see
apps.proxy.live_proxy.probation.record_event). Everything is short-lived in Redis, so this
only ever shows recent activity.
"""

import logging
import time

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin
from core.utils import RedisClient

from . import probation

logger = logging.getLogger("live_proxy")


def _viewer_name(event, usernames):
    """A short, readable viewer: its login, or its address and app."""
    username = usernames.get(event.get("user_id"))
    if username and event.get("app"):
        return f"{username} · {event['app']}"
    if username:
        return username
    if event.get("app"):
        return f"{event['ip']} · {event['app']}"
    return event.get("ip") or "unknown"


def _account_rows(redis_client):
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

    user_ids = {int(user_id) for user_id in user_ids if user_id and user_id.isdigit()}
    if not user_ids:
        return {}
    return {
        str(user_id): username
        for user_id, username in User.objects.filter(id__in=user_ids).values_list("id", "username")
    }


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def overlap_activity(request):
    """Accounts and recent switches for the Channel Switch Overlap page.

    POST {"keep_seconds": ...} changes how long switches are kept, and returns the page as a
    GET does, so the page shows the new setting straight away.
    """
    redis_client = RedisClient.get_client()
    if not redis_client:
        return JsonResponse({"error": "Redis not available"}, status=500)

    if request.method == "POST":
        try:
            probation.set_event_ttl(redis_client, request.data.get("keep_seconds"))
        except (TypeError, ValueError) as e:
            return JsonResponse({"error": str(e)}, status=400)

    enabled = probation.in_use()
    events = probation.recent_events(redis_client) if enabled else []
    channels = _channel_names(
        [event.get("channel") for event in events] + [event.get("from_channel") for event in events]
    )
    usernames = _usernames([event.get("user_id") for event in events])

    return JsonResponse({
        "enabled": enabled,
        "accounts": _account_rows(redis_client) if enabled else [],
        "events": [
            {
                "time": float(event.get("time", 0)),
                "viewer": _viewer_name(event, usernames),
                "from_channel": channels.get(event.get("from_channel", ""), ""),
                "channel": channels.get(event.get("channel", ""), ""),
                "account": event.get("account", ""),
                "action": event.get("action", ""),
                "result": event.get("result", ""),
            }
            for event in events
        ],
        "keep_seconds": probation.event_ttl(redis_client),
        "keep_choices": list(probation.EVENT_TTL_CHOICES),
        "timestamp": time.time(),
    })
