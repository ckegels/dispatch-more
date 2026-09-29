"""The Show Groups tab: the groups, what is in each one now and what joins next, and its buttons.

See apps/channels/show_groups for how a group is filled.
"""

import logging
from datetime import datetime

from django.http import JsonResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from .show_groups import live, themes
from .show_groups import plan as plans

logger = logging.getLogger(__name__)


def _page():
    from django.db.models import Count

    from apps.channels.models import Channel, ChannelProfileMembership

    now = timezone.now()
    settings, groups = themes.load_settings(), themes.load_groups()
    state = live.load_state()
    plan = plans.load()
    ours = live.own_copy_ids(state)
    enabled = set(ChannelProfileMembership.objects.filter(
        channel_profile_id=state.get("profile_id") or 0, channel_id__in=list(ours), enabled=True)
        .values_list("channel_id", flat=True))
    copies = {c["id"]: c for c in Channel.objects.filter(id__in=list(ours)).values(
        "id", "name", "channel_number", "uuid")}
    zone = live._zone()

    shown = []
    for group in groups:
        entry = state["groups"].get(group["id"], {"copies": {}})
        now_in = plans.stays_at(plan, group["id"], now)
        members = []
        for source_id, copy_entry in entry["copies"].items():
            copy = copies.get(int(copy_entry["id"]))
            if copy is None or copy["id"] not in enabled:
                continue
            stay = now_in.get(source_id)
            airing = plans.showing(stay, now) if stay else None
            members.append({
                "copy": copy["id"], "source": int(source_id), "name": copy["name"],
                "number": copy["channel_number"],
                "always": int(source_id) in (group.get("permanent") or ()),
                "showing": airing,
                "leaves": stay["leaves"] if stay else None,
                "viewers": live.viewers(copy["uuid"]) or 0,
            })
        members.sort(key=lambda m: (m["number"] is None, m["number"] or 0))
        planned = (plan or {}).get("groups", {}).get(group["id"])
        shown.append({
            **group,
            "in_group": members,
            "copies": len(entry["copies"]),
            "coming": plans.coming(plan, group["id"], now) if planned else [],
            "titles": (planned or {}).get("titles", []),
            "title_count": (planned or {}).get("title_count", 0),
            "planned": planned is not None,
            "refused": state["refused"].get(group["id"], ""),
        })

    counted = (Channel.objects.filter(channel_group__isnull=False)
               .values("channel_group_id", "channel_group__name")
               .annotate(channels=Count("id")).order_by("channel_group__name"))
    made = (plan or {}).get("made")
    return {
        "settings": settings,
        "defaults": themes.DEFAULTS,
        "groups": shown,
        "presets": themes.PRESET_IDS,
        "plan_made": made,
        "plan_made_local": (datetime.fromisoformat(made).astimezone(zone).strftime("%a %H:%M")
                            if made else ""),
        "plugin": live.plugin(),
        "lookups": live.lookup_status(),
        "activity": live.activity(),
        # For "always in this group": every channel of yours, not the copies
        "channels": [
            {"id": c["id"], "name": c["name"], "number": c["channel_number"]}
            for c in Channel.objects.exclude(id__in=list(ours)).order_by("channel_number", "name")
            .values("id", "name", "channel_number")
        ],
        "channel_groups": [
            {"id": g["channel_group_id"], "name": g["channel_group__name"], "count": g["channels"]}
            for g in counted if g["channel_group_id"] not in
            {e.get("group_id") for e in state["groups"].values()}
        ],
    }


@api_view(["GET", "PUT"])
@permission_classes([IsAdmin])
def show_groups_page(request):
    """GET: everything the tab draws. PUT {settings?, groups?}: save them; the next minute acts
    on them."""
    if request.method == "PUT":
        try:
            themes.save(request.data.get("settings"), request.data.get("groups"))
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(_page())


@api_view(["POST"])
@permission_classes([IsAdmin])
def show_groups_run(request):
    """{action}: "update" does the minute's pass now (working the plan out first), "plan" works
    it out without changing a channel (a preview, also with live off), "remove" deletes every
    copy, "take_over" takes the plugin's group over."""
    action = request.data.get("action")
    settings, groups = themes.load_settings(), themes.load_groups()
    try:
        if action == "plan":
            live.work_out(settings, groups)
            message = "Worked out what the groups that are on would hold."
        elif action == "update":
            if themes.active(settings, groups):
                live.work_out(settings, groups)
            joined, left, held = live.tick(settings, groups, plans.load())
            message = (f"{len(joined)} joined, {len(left)} left"
                       + (f", {len(held)} kept for viewers" if held else ""))
        elif action == "remove":
            deleted, kept, removed = live.remove_all(settings)
            message = (f"Removed {len(deleted)} copies" + (", " + ", ".join(removed) if removed else "")
                       + (f"; {len(kept)} someone is watching stay until they stop" if kept else ""))
        elif action == "take_over":
            taken = live.take_over()
            message = f"Took the plugin's group over with {taken['copies']} copies; the plugin is off."
        else:
            return JsonResponse({"error": f"Unknown action: {action}"}, status=400)
    except live.Refused as e:
        return JsonResponse({"error": str(e)}, status=409)
    return JsonResponse({"message": message, **_page()})
