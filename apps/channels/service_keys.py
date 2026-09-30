"""
Keys for online services (TMDB, TheTVDB, Trakt, OMDb), kept in one place so every feature
that asks one of them uses the same key: Settings → System → Service keys. Show Groups is the
first to use them (what a show is, show_groups/lookups.py).

One CoreSettings row, "service-keys". A service without a key is simply not asked.
"""

SETTINGS_KEY = "service-keys"

SERVICES = [
    {
        "id": "tmdb", "name": "TMDB", "url": "https://www.themoviedb.org/settings/api",
        "about": "Free. Genres and keywords for shows and films (“cooking competition”, “road trip”).",
        "fields": [{"key": "tmdb_key", "label": "API key or read access token", "secret": True}],
    },
    {
        "id": "tvdb", "name": "TheTVDB", "url": "https://thetvdb.com/dashboard/account/apikey",
        "about": "Free key. Show genres such as Food, Travel, Home and Garden, Documentary.",
        "fields": [
            {"key": "tvdb_key", "label": "API key", "secret": True},
            {"key": "tvdb_pin", "label": "Subscriber PIN (only if your key asks for one)", "secret": True},
        ],
    },
    {
        "id": "trakt", "name": "Trakt", "url": "https://trakt.tv/oauth/applications",
        "about": "Free: make an app, use its Client ID. Show genres such as documentary, reality, home-and-garden.",
        "fields": [{"key": "trakt_client_id", "label": "Client ID", "secret": True}],
    },
    {
        "id": "omdb", "name": "OMDb", "url": "https://www.omdbapi.com/apikey.aspx",
        "about": "Free key, about 1,000 requests a day. IMDb's genres (Documentary, Reality-TV, Animation...).",
        "fields": [{"key": "omdb_key", "label": "API key", "secret": True}],
    },
]
FIELDS = [field["key"] for service in SERVICES for field in service["fields"]]


def _row():
    from core.models import CoreSettings

    return CoreSettings.objects.filter(key=SETTINGS_KEY).first()


def load():
    """{field: value} for every field, "" when not given. A TMDB key typed into Show Groups'
    own settings before this existed is taken over once."""
    row = _row()
    stored = row.value if row and isinstance(row.value, dict) else None
    if stored is None:
        stored = {}
        try:
            from .show_groups import themes

            old = (themes._load().get("settings") or {}).get("tmdb_key")
            if old:
                stored = {"tmdb_key": old}
                save(stored)
        except Exception:
            pass
    return {key: str(stored.get(key) or "").strip() for key in FIELDS}


def save(values):
    from core.models import CoreSettings

    row = _row()
    current = row.value if row and isinstance(row.value, dict) else {}
    current = {key: str(current.get(key) or "").strip() for key in FIELDS}
    current.update({k: str(v or "").strip() for k, v in (values or {}).items() if k in FIELDS})
    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY, defaults={"name": "Service keys", "value": current})
    return current


def with_keys(settings):
    """These settings with the service keys added, for code that asks the services."""
    return {**(settings or {}), **load()}
