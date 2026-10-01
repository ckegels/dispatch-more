"""
The Subtitles tab, step 1 of fork/subtitles.md: which subtitles each channel's streams carry
(read by Stream Check, stream_check.subtitle_details) and the language each channel speaks.
Nothing is made or translated yet; that is steps 3 and 4 of the design.

What a stream carries is in Stream.stream_stats: "subtitles" ([] when looked and none found,
missing when never looked), "audio_languages", "subtitles_checked_at".
"""

SETTINGS_KEY = "subtitles"

# The language a channel's country most likely speaks, when no audio track says (ISO 639-2, as
# stream tags write it). Belgium and Switzerland speak more than one: the audio tag, or a
# person on the tab, says which.
COUNTRY_LANGUAGE = {
    "de": "deu", "at": "deu", "ch": "deu", "nl": "dut", "be": "dut", "fr": "fra", "it": "ita",
    "gb": "eng", "us": "eng", "ca": "eng", "au": "eng", "ie": "eng", "es": "spa", "pt": "por",
    "pl": "pol", "tr": "tur", "se": "swe", "no": "nor", "dk": "dan", "fi": "fin",
}
# A tag written one way or another is one language
SAME_LANGUAGE = {"nld": "dut", "ger": "deu", "fre": "fra", "en": "eng", "de": "deu", "nl": "dut",
                 "fr": "fra", "it": "ita"}
KINDS = ("teletext", "dvb", "cc", "text")


def _same(lang):
    lang = (lang or "").strip().lower()
    return SAME_LANGUAGE.get(lang, lang)


def load_spoken():
    """{channel id: language} set by hand on the tab."""
    from .guide_manager import _load

    value = _load(SETTINGS_KEY, {})
    return {str(k): v for k, v in (value.get("spoken") or {}).items()}


def set_spoken(channel_id, language):
    """Set (or with "" clear) the language a channel speaks."""
    from .guide_manager import _load, _store

    value = _load(SETTINGS_KEY, {})
    spoken = dict(value.get("spoken") or {})
    language = _same(language)
    if language:
        spoken[str(int(channel_id))] = language
    else:
        spoken.pop(str(int(channel_id)), None)
    _store(SETTINGS_KEY, "Subtitles", {**value, "spoken": spoken})
    return spoken


def rows():
    """One row per channel (Show Groups' copies left out), with what its streams carry."""
    from .logo_library import country_of
    from .models import Channel, ChannelStream
    from .show_groups.live import copy_group_ids

    channels = list(
        Channel.objects.exclude(channel_group_id__in=copy_group_ids())
        .select_related("channel_group").order_by("channel_number", "name")
        .values("id", "uuid", "name", "channel_number", "channel_group_id", "channel_group__name")
    )
    links = {}
    for channel_id, stream_id, name, account, stats, custom in (
        ChannelStream.objects.filter(channel_id__in=[c["id"] for c in channels])
        .order_by("channel_id", "order")
        .values_list("channel_id", "stream_id", "stream__name", "stream__m3u_account__name",
                     "stream__stream_stats", "stream__is_custom")
    ):
        if custom:
            continue  # the fallback stream says nothing about the channel
        stats = stats if isinstance(stats, dict) else {}
        links.setdefault(channel_id, []).append({
            "id": stream_id, "name": name, "account": account or "",
            "checked": "subtitles" in stats,
            "checked_at": stats.get("subtitles_checked_at") or "",
            "subtitles": stats.get("subtitles") or [],
            "audio_languages": [_same(l) for l in stats.get("audio_languages") or []],
        })
    spoken_by_hand = load_spoken()
    found = []
    for channel in channels:
        streams = links.get(channel["id"], [])
        carried = []
        for stream in streams:
            for sub in stream["subtitles"]:
                entry = {"kind": sub.get("kind"), "lang": _same(sub.get("lang")),
                         "hearing_impaired": bool(sub.get("hearing_impaired"))}
                if entry not in carried:
                    carried.append(entry)
        tagged = next((l for s in streams for l in s["audio_languages"]), "")
        country = country_of(channel["name"]) or country_of(channel["channel_group__name"] or "")
        if str(channel["id"]) in spoken_by_hand:
            spoken, spoken_from = spoken_by_hand[str(channel["id"])], "set"
        elif tagged:
            spoken, spoken_from = tagged, "audio"
        elif COUNTRY_LANGUAGE.get(country):
            spoken, spoken_from = COUNTRY_LANGUAGE[country], "country"
        else:
            spoken, spoken_from = "", ""
        checked = any(s["checked"] for s in streams)
        found.append({
            "id": channel["id"], "uuid": str(channel["uuid"]), "name": channel["name"],
            "number": channel["channel_number"], "group": channel["channel_group__name"] or "",
            "group_id": channel["channel_group_id"],
            "spoken": spoken, "spoken_from": spoken_from,
            "subtitles": carried,
            "state": "found" if carried else "none" if checked else "unchecked",
            "streams": streams,
        })
    return found


def summary(found):
    counts = {"channels": len(found), "found": 0, "none": 0, "unchecked": 0}
    counts.update({kind: 0 for kind in KINDS})
    for row in found:
        counts[row["state"]] += 1
        for kind in {s["kind"] for s in row["subtitles"]}:
            if kind in counts:
                counts[kind] += 1
    return counts
