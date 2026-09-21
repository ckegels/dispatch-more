"""The logo library tab: what each channel has, what the collections would give it, and
applying what is chosen. See logo_library for where the logos come from and how a channel
is matched to one."""

import logging

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import logo_library

logger = logging.getLogger(__name__)

# More than this in one apply is refused: it is a list someone looked at, not a bulk job
MAX_APPLY = 5000


def _status(index):
    if not index:
        return {
            "built": False, "counts": {}, "errors": {}, "built_at": None,
            "out_of_date": [],
        }
    return {
        "built": True,
        "counts": index.get("counts") or {},
        "errors": index.get("errors") or {},
        "built_at": index.get("built_at"),
        # Collections switched on that this index was built without: their logos are not
        # being suggested, and nothing else on the page would say so
        "out_of_date": logo_library.out_of_date(index),
        # What the downloaded lists take up, so it is a number and not a worry
        "bytes": logo_library.index_size(),
    }


@api_view(["GET"])
@permission_classes([IsAdmin])
def logo_library_suggestions(request):
    """
    Every channel, with the logo it has and the ones the collections would give it.

    ?show=suggested (the default) lists only channels something was found for, which is
    the list worth working through. ?show=missing lists channels with no logo at all, and
    ?show=all lists everything. ?search= narrows by name.
    """
    from django.db.models import Prefetch

    from .models import Channel, Stream

    index = logo_library.load_index()
    show = request.query_params.get("show") or "suggested"
    search = (request.query_params.get("search") or "").strip().lower()

    # What a channel already has comes first and needs nothing downloaded, so it is offered
    # even before the collections have been; they add to it once they are
    # Every guide in Dispatcharr, looked up by name, not only the one each channel is
    # mapped to: read once for the whole page
    guide_icons = logo_library.guide_icons_by_key()
    channels = (
        Channel.objects.select_related("logo", "epg_data", "epg_data__epg_source")
        .prefetch_related(
            Prefetch("streams", queryset=Stream.objects.only("id", "name", "logo_url"))
        )
        .order_by("channel_number", "name")
    )

    rows = []
    for channel in channels:
        if search and search not in (channel.name or "").lower():
            continue
        current = (
            {"id": channel.logo.id, "name": channel.logo.name, "url": channel.logo.url}
            if channel.logo_id
            else None
        )
        if show == "missing" and current:
            continue
        # The collections first: they are why this page exists, and the logos a playlist
        # came with are usually the ones being replaced. Those, and the guides' icons, are
        # still offered after, as alternatives to fall back on
        suggestions = []
        if index:
            suggestions += logo_library.suggestions_for(channel.name, index)
        suggestions += logo_library.local_suggestions(channel)
        suggestions += logo_library.guide_suggestions(channel, guide_icons)
        unique, seen = [], set()
        for suggestion in suggestions:
            if suggestion["url"] not in seen:
                seen.add(suggestion["url"])
                unique.append(suggestion)
        suggestions = unique
        # A suggestion that is already the logo it has is nothing to suggest
        if current:
            suggestions = [s for s in suggestions if s["url"] != current["url"]]
        if show == "suggested" and not suggestions:
            continue
        rows.append({
            "channel_id": channel.id,
            "number": channel.channel_number,
            "name": channel.name,
            "country": logo_library.country_of(channel.name),
            "current": current,
            "suggestions": suggestions,
        })

    return JsonResponse({"status": _status(index), "channels": rows})


@api_view(["POST"])
@permission_classes([IsAdmin])
def logo_library_refresh(request):
    """
    Download the collections again, in the background: they are several megabytes, which
    is too long to hold a request open for. The page asks for the status until it changes.
    """
    from .tasks import build_logo_library

    task = build_logo_library.delay()
    return JsonResponse({"started": True, "task_id": task.id})


@api_view(["GET"])
@permission_classes([IsAdmin])
def logo_library_status(request):
    return JsonResponse(_status(logo_library.load_index()))


@api_view(["POST"])
@permission_classes([IsAdmin])
def logo_library_forget(request):
    """
    Throw the downloaded lists away, freeing what they take up.

    Everything in them is public and comes back in seconds, so this is not a deletion of
    anything: the collections are kept, and only the copy is forgotten. Until the next
    download no logos are suggested, which the page says in the same words it uses for a
    collection that has not been downloaded yet.
    """
    from . import known_channels

    freed = logo_library.forget_index()
    if request.data.get("reference"):
        freed += known_channels.forget()
    return JsonResponse({"freed": freed, **_status(logo_library.load_index())})


@api_view(["POST"])
@permission_classes([IsAdmin])
def logo_library_apply(request):
    """
    Give channels the logos chosen for them.

    Takes [{"channel_id", "url", "name"}]. Only what is sent changes: nothing is applied
    that was not chosen on the page.
    """
    chosen = request.data.get("assignments")
    if not isinstance(chosen, list) or not chosen:
        return JsonResponse({"error": "Choose at least one logo to apply"}, status=400)
    if len(chosen) > MAX_APPLY:
        return JsonResponse(
            {"error": f"No more than {MAX_APPLY} at once"}, status=400
        )
    # By address for logos from a collection or a pasted link, by id for one uploaded from
    # the page, which is kept on disk and has no address of its own to give
    by_url, by_id = [], []
    try:
        for item in chosen:
            if item.get("logo_id"):
                by_id.append((int(item["channel_id"]), int(item["logo_id"])))
            else:
                by_url.append(
                    (int(item["channel_id"]), str(item["url"]), str(item.get("name") or ""))
                )
    except (KeyError, TypeError, ValueError):
        return JsonResponse(
            {"error": "Each logo needs a channel_id, and a url or a logo_id"}, status=400
        )

    result = {"updated": 0, "created_logos": 0}
    for done in (
        logo_library.apply_logos(by_url) if by_url else None,
        logo_library.apply_logo_ids(by_id) if by_id else None,
    ):
        if done:
            result["updated"] += done["updated"]
            result["created_logos"] += done["created_logos"]
    return JsonResponse(result)


@api_view(["GET"])
@permission_classes([IsAdmin])
def logo_library_search(request):
    """
    Search every logo in the collections by name, for a channel nothing was suggested for
    or the wrong thing was. ?q= what to look for, ?country= to put one country first.
    """
    query = request.query_params.get("q") or ""
    # Dispatcharr's own guides are searched whether or not the collections have been
    # downloaded: they are already here, and current
    results = logo_library.search_guides(query)
    index = logo_library.load_index()
    if index:
        taken = {r["url"] for r in results}
        results += [
            r
            for r in logo_library.search(
                query, index, request.query_params.get("country") or ""
            )
            if r["url"] not in taken
        ]
    return JsonResponse({"built": bool(index), "results": results})


BUILT_IN = [
    {
        "id": logo_library.TV_LOGOS,
        "name": logo_library.TV_LOGOS,
        "type": logo_library.GITHUB,
        "url": "https://github.com/tv-logo/tv-logos",
        "built_in": True,
    },
    {
        "id": logo_library.IPTV_ORG,
        "name": logo_library.IPTV_ORG,
        "type": logo_library.JSON_LIST,
        "url": "https://iptv-org.github.io/api/logos.json",
        "built_in": True,
    },
]


def _sources_page():
    """Every collection, built in or added, with what the last download found in it."""
    sources = logo_library.load_sources()
    status = _status(logo_library.load_index())
    rows = []
    for source in BUILT_IN + sources["added"]:
        name = source.get("name") or source.get("url")
        enabled = (
            name not in sources["off"]
            if source.get("built_in")
            else source.get("enabled", True)
        )
        rows.append({
            **source,
            "enabled": enabled,
            "count": status["counts"].get(name),
            "error": status["errors"].get(name),
        })
    return {
        "sources": rows,
        "types": list(logo_library.SOURCE_TYPES),
        # Switched on, but not in the index: their logos are not being suggested yet
        "out_of_date": logo_library.out_of_date(sources=sources),
    }


def _checked(source):
    """
    What a collection holds, read now, before it is kept.

    Adding a link that turns out to hold nothing, or not to be the kind of thing it was said
    to be, is found out here rather than at the next download, where it would just be a
    collection that quietly contributes nothing.
    """
    entries = logo_library.read_source(source)
    return {
        "count": len(entries),
        "sample": [
            {"name": entry["name"], "url": entry["url"], "country": entry["country"]}
            for entry in entries[:12]
        ],
    }


@api_view(["GET", "POST", "PATCH", "DELETE"])
@permission_classes([IsAdmin])
def logo_library_sources(request):
    """
    The collections logos are looked for in.

    GET lists them. POST {"type", "url", "name", "check": true} reads one without keeping
    it, to see what it holds; without "check" it is read and then kept. PATCH {"id",
    "enabled"} switches one off or on, built in ones included. DELETE ?id= removes an added
    one. Nothing here downloads the collections again: that is Update logo lists.
    """
    sources = logo_library.load_sources()

    if request.method == "DELETE":
        source_id = request.query_params.get("id")
        sources["added"] = [s for s in sources["added"] if s.get("id") != source_id]
        logo_library.save_sources(sources)
        return JsonResponse(_sources_page())

    if request.method == "PATCH":
        source_id = request.data.get("id")
        enabled = bool(request.data.get("enabled"))
        if source_id in {b["id"] for b in BUILT_IN}:
            off = set(sources["off"])
            (off.discard if enabled else off.add)(source_id)
            sources["off"] = sorted(off)
        else:
            for source in sources["added"]:
                if source.get("id") == source_id:
                    source["enabled"] = enabled
        logo_library.save_sources(sources)
        return JsonResponse(_sources_page())

    if request.method == "POST":
        kind = request.data.get("type")
        url = (request.data.get("url") or "").strip()
        name = (request.data.get("name") or "").strip() or url
        if kind not in logo_library.SOURCE_TYPES:
            return JsonResponse({"error": "Choose what kind of collection it is"}, status=400)
        if kind != logo_library.GITHUB and not url.startswith(("http://", "https://")):
            return JsonResponse(
                {"error": "A link starts with http:// or https://"}, status=400
            )
        if not url:
            return JsonResponse({"error": "Give the link to the collection"}, status=400)
        taken = {b["name"] for b in BUILT_IN} | {s.get("name") for s in sources["added"]}
        if not request.data.get("check") and name in taken:
            return JsonResponse(
                {"error": f"There is already a collection called {name}"}, status=400
            )

        source = {"id": logo_library.new_source_id(), "type": kind, "url": url, "name": name}
        try:
            checked = _checked(source)
        except Exception as e:
            return JsonResponse(
                {"error": f"Could not read that as {kind}: {str(e)[:200]}"}, status=400
            )
        if request.data.get("check"):
            return JsonResponse(checked)
        if not checked["count"]:
            return JsonResponse(
                {"error": "That was read, but there are no logos in it"}, status=400
            )

        sources["added"].append({**source, "enabled": True})
        logo_library.save_sources(sources)
        logger.info(f"Added logo collection {name} ({kind}, {checked['count']} logos)")
        return JsonResponse({**_sources_page(), "added": checked})

    return JsonResponse(_sources_page())
