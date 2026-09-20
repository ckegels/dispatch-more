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
    groups = [g for g in (request.GET.get("groups") or "").split(",") if g.strip()]
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
        numbers = guide_layout.numbers_for(
            order, existing, int(moved) if moved not in (None, "") else None
        )
    return JsonResponse({
        "numbers": {str(k): v for k, v in numbers.items()},
        # What would actually change, which is what the page shows before applying
        "changing": sorted(
            str(k) for k, v in numbers.items() if existing.get(k) != v
        ),
    })


@api_view(["POST"])
@permission_classes([IsAdmin])
def guide_layout_apply(request):
    """Write the numbers, and any changes of group, worked out again as they are applied."""
    numbers = request.data.get("numbers")
    groups = request.data.get("groups")
    if not isinstance(numbers, dict) and not isinstance(groups, dict):
        return JsonResponse({"error": "Nothing to apply"}, status=400)
    return JsonResponse(guide_layout.apply(numbers or {}, groups or {}))
