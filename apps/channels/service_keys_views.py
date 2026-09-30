"""Settings → System → Service keys: the keys for TMDB, TheTVDB, Trakt and OMDb (service_keys)."""

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes

from apps.accounts.permissions import IsAdmin

from . import service_keys

# Asked when a key is tested: a show every one of these services knows
TEST_TITLE = "MasterChef"


@api_view(["GET", "PUT"])
@permission_classes([IsAdmin])
def service_keys_page(request):
    """GET: the services and their keys. PUT {values}: save them."""
    if request.method == "PUT":
        service_keys.save(request.data.get("values") or {})
    return JsonResponse({"services": service_keys.SERVICES, "values": service_keys.load()})


@api_view(["POST"])
@permission_classes([IsAdmin])
def service_keys_test(request):
    """{service}: ask it about one well-known show with the saved key, and say how it went."""
    from .show_groups import lookups

    service = request.data.get("service")
    if service not in lookups.KEYED:
        return JsonResponse({"error": f"Unknown service: {service}"}, status=400)
    settings = service_keys.load()
    if not settings.get(lookups.KEYED[service]):
        return JsonResponse({"ok": False, "message": "No key saved yet."})
    try:
        answer = lookups.ask(service, TEST_TITLE, settings)
    except lookups.Unavailable as why:
        return JsonResponse({"ok": False, "message": str(why)})
    except Exception as e:
        return JsonResponse({"ok": False, "message": f"It could not be asked: {e}"})
    if answer and answer.get("genres"):
        return JsonResponse({"ok": True, "message": f"Works: {answer['name']} is {', '.join(answer['genres'][:5])}."})
    return JsonResponse({"ok": False, "message": f"It answered, but found no “{TEST_TITLE}”. The key may not be right."})
