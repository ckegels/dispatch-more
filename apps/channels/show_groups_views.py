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
        channel_profile_id=live.profile_id(settings, state) or 0, channel_id__in=list(ours), enabled=True)
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


@api_view(["GET", "POST"])
@permission_classes([IsAdmin])
def show_groups_shows(request):
    """GET ?q=&only=all|taken|databases|disagree|unknown&offset=: every show in the guide next
    to what the online databases say. POST {title}: ask the databases about it now."""
    from .show_groups import compare

    if request.method == "POST":
        title = str(request.data.get("title") or "").strip()
        try:
            compare.ask_now(title)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        return JsonResponse(compare.shows(query=title, only="all"))
    only = request.GET.get("only") or "all"
    if only not in compare.FILTERS:
        return JsonResponse({"error": f"Unknown filter: {only}"}, status=400)
    try:
        offset = max(0, int(request.GET.get("offset") or 0))
    except ValueError:
        offset = 0
    return JsonResponse(compare.shows(query=request.GET.get("q") or "", only=only, offset=offset))


@api_view(["GET"])
@permission_classes([IsAdmin])
def show_groups_kinds(request):
    """?group=<id>: your channels iptv-org files under that group's kinds, to keep in it for
    good. The first ask downloads iptv-org's channels (kept a week)."""
    from .show_groups import kinds

    group = next((g for g in themes.load_groups() if g["id"] == request.GET.get("group")), None)
    if group is None:
        return JsonResponse({"error": "No such group"}, status=404)
    try:
        found = kinds.suggestions(group, exclude_ids=live.own_copy_ids(live.load_state()))
    except Exception as e:
        logger.warning(f"Show Groups: iptv-org's channels could not be read: {e}")
        return JsonResponse({"error": "iptv-org's channel list could not be downloaded; try again later."},
                            status=502)
    return JsonResponse({"kinds": group.get("channel_kinds") or [], "all_kinds": kinds.CATEGORIES,
                         "channels": found})


def _work_out(settings, groups):
    """The plan made by the Celery worker, waited for (see tasks.show_groups_work_out); made
    here only when no worker answers, so the button never fails for it."""
    from celery.exceptions import TimeoutError as StillWorking

    try:
        from dispatcharr.celery import app

        from .tasks import show_groups_work_out

        # No worker answering (Celery down, or the tests): made here, as before
        if not app.control.ping(timeout=1.0):
            raise RuntimeError("no Celery worker answered")
        show_groups_work_out.apply_async().get(timeout=600, propagate=True)
    except StillWorking:
        raise live.Refused("The plan is still being made; look again in a minute.")
    except Exception as e:
        logger.warning(f"Show Groups: the plan could not be made by the worker ({e}); making it here")
        live.work_out(settings, groups)


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
            _work_out(settings, groups)
            message = "Worked out what the groups that are on would hold."
        elif action == "update":
            if themes.active(settings, groups):
                _work_out(settings, groups)
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
