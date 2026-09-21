"""The Guide Layout tab: the lineup as it stands, as it would be, and applying it.

See guide_layout for the rules -- above all that channels keep the numbers they have.
"""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import guide_layout

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAdmin])
def guide_layout_page(request):
    """Every group with its channels in number order, and where numbers clash."""
    try:
        groups = [int(g) for g in (request.GET.get("groups") or "").split(",") if g.strip()]
    except (TypeError, ValueError):
        return JsonResponse({"error": "Groups are given by id"}, status=400)
    return JsonResponse(guide_layout.layout(groups))


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_layout_arrange(request):
    """
    What the numbers would be, arranged like this. Worked out here rather than in the page
    so there is one set of rules: the page asks after each drag and shows what came back.

    order: the channel ids as they are to come out. moved: the one just dragged, which
    takes the place it was dropped in. With `start`, the whole order is renumbered from
    there instead, every `step`.
    """
    try:
        order = [int(one) for one in request.data.get("order") or ()]
    except (TypeError, ValueError):
        return JsonResponse({"error": "Channels are given by id"}, status=400)
    if not order:
        return JsonResponse({"error": "No channels were given"}, status=400)

    from .models import Channel

    existing = dict(
        Channel.objects.filter(id__in=order).values_list("id", "channel_number")
    )
    start = request.data.get("start")
    if start not in (None, ""):
        try:
            numbers = guide_layout.renumbered(order, float(start), float(request.data.get("step") or 1))
        except (TypeError, ValueError):
            return JsonResponse({"error": "Numbers only, please"}, status=400)
    else:
        moved = request.data.get("moved")
        try:
            moved = int(moved) if moved not in (None, "") else None
        except (TypeError, ValueError):
            return JsonResponse({"error": "Channels are given by id"}, status=400)
        numbers = guide_layout.numbers_for(order, existing, moved)
    # Pushing channels along can run into the group above: the numbers are worked out
    # within one group, and nothing was stopping them reaching the next one's. Said rather
    # than prevented -- a drag that silently did nothing would be worse -- and the page
    # can warn before it is applied.
    from .models import Channel

    highest = max((v for v in numbers.values() if v is not None), default=None)
    ours = set(order)
    in_the_way = []
    if highest is not None:
        lowest = min((v for v in numbers.values() if v is not None), default=highest)
        in_the_way = [
            {"id": c.id, "name": c.name, "number": c.channel_number}
            for c in Channel.objects.filter(
                channel_number__gte=lowest, channel_number__lte=highest
            ).exclude(id__in=ours).order_by("channel_number")[:20]
        ]
    return JsonResponse({
        "numbers": {str(k): v for k, v in numbers.items()},
        # What would actually change, which is what the page shows before applying
        "changing": sorted(
            str(k) for k, v in numbers.items() if existing.get(k) != v
        ),
        # Channels of other groups these numbers would land on
        "in_the_way": in_the_way,
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_layout_rename(request):
    """
    Rename a group, or channels, or work out what taking something out of a group's names
    would leave -- which is asked for before it is done, like everything else here.
    """
    if request.data.get("group") is not None:
        try:
            return JsonResponse(guide_layout.rename_group(
                int(request.data["group"]), request.data.get("name")
            ))
        except (TypeError, ValueError) as e:
            return JsonResponse({"error": str(e) or "Which group?"}, status=400)

    take_off = request.data.get("take_off")
    if take_off:
        from .models import Channel

        try:
            ids = [int(one) for one in request.data.get("channels") or ()]
        except (TypeError, ValueError):
            return JsonResponse({"error": "Channels are given by id"}, status=400)
        names = dict(Channel.objects.filter(id__in=ids).values_list("id", "name"))
        changed = guide_layout.renamed(names, take_off, request.data.get("replace_with", ""))
        if request.data.get("apply"):
            return JsonResponse(guide_layout.rename_channels(changed))
        return JsonResponse({"names": {str(k): v for k, v in changed.items()}})

    return JsonResponse(guide_layout.rename_channels(request.data.get("names") or {}))


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_layout_apply(request):
    """Write the numbers, and any changes of group, worked out again as they are applied."""
    numbers = request.data.get("numbers")
    groups = request.data.get("groups")
    if not isinstance(numbers, dict) and not isinstance(groups, dict):
        return JsonResponse({"error": "Nothing to apply"}, status=400)
    return JsonResponse(guide_layout.apply(numbers or {}, groups or {}))
