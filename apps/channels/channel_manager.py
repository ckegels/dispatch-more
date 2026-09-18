"""Channel Manager: recognising the same channel across providers and qualities.

A provider lists "ORF 1", "ORF 1 HD" and "ORF 1 FHD" as three streams, a second provider
lists its own three, and Dispatcharr's own sync makes a channel of each. What someone
wants is one ORF 1, with every one of those streams behind it, best first, so that when
one fails the next takes over. This works that out and shows it: for every channel as it
would come out, what goes into it and what it would look like, next to what there is
now. Nothing is written until the rows chosen are applied.

It merges into channels that already exist first, because that is how it is used: a
group of channels someone has set up, which should gain every copy of themselves the
providers carry. New channels are made only for what no channel has, and only if asked.

Three rules learned from tools that did this before, and kept:

- a channel is never emptied: a run that finds nothing for it leaves what it has;
- a stream is only taken away from a channel when that is asked for, never by default;
- two channels that could both be the one a stream belongs to is a conflict to be shown,
  not a guess to be made, because a wrong guess sends someone's channel to another
  channel's streams.

Matching reuses the key Find Logos matches on (see logo_library.match_key), which has
already been taught what goes wrong: accents folded rather than dropped, "+" and "&" as
words, and the country box in front taken off.
"""

import logging
import re

from . import logo_library

logger = logging.getLogger(__name__)

SETTINGS_KEY = "channel-manager"

DEFAULTS = {
    # ── Scope ──
    # M3U accounts to take streams from, in order of preference; empty is every one
    "accounts": [],
    # Stream groups to take streams from; empty is every one
    "stream_groups": [],
    # Channel groups whose channels may receive streams; empty is every channel
    "channel_groups": [],
    # Where new channels go; None puts each in the group its streams came from
    "target_group": None,
    # "all" joins every profile, as Dispatcharr does; "none", or a list of profile ids
    "profiles": "all",
    # ── Recognition ──
    # "exact": the whole name, country box and punctuation and all, with only the quality,
    # the words to ignore and the rules taken off, and case ignored. What DispatcharrUtils
    # and the group merge people ran by hand do, and so the default: it is what they trust.
    # "loose": letters and digits only, accents folded, "+" and "&" read as words. Finds
    # more, and merges more that should not be.
    "name_matching": "exact",
    # When more than one channel is the one a stream belongs to: "all" gives it to each of
    # them, as those tools do; "conflict" shows it and leaves it alone
    "several_matches": "all",
    "match_tvg_id": True,
    # Words that are about the stream, not the channel, taken off before matching
    "ignore_tags": "VIP, RAW, BACKUP, ALT, MULTI, [Dead], (Backup)",
    # [[find, replace], ...] applied to stream names before anything else
    "regex_rules": [],
    # {"Channel name": ["another name", ...]} for channels known by more than one
    "aliases": {},
    # Only put a stream on a channel of the same country, when both say one
    "same_country": True,
    # ── Quality ──
    # "quality" puts the best picture first, "provider" the preferred account first
    "order": "quality",
    "skip_stale": True,
    # Custom streams are made by hand and are nobody's copy of anything
    "skip_custom": True,
    "drop_sd_when_hd": False,
    # ── What to change ──
    "create_new": False,
    "min_streams_new": 1,
    "keep_country_prefix": True,
    # None numbers new channels after the highest number there is
    "number_start": None,
    "reorder_existing": False,
    "replace_streams": False,
    # ── EPG and logo ──
    # "keep", "tvg_id" or "tvg_id_then_name"
    "epg": "tvg_id_then_name",
    # "keep", "collections" (the Find Logos collections, then the stream's) or "stream"
    "logo": "collections",
}

QUALITY_LABELS = ["4K", "FHD", "HD", "SD"]
# How a quality is written in a name, best first. Whole words only: "HD" inside a word is
# part of the word.
#
# Only with the p or i: a number standing on its own is part of a name. With bare numbers
# counted, "Channel 480" and "Channel 720" both came down to "Channel" and collided.
QUALITY_WORDS = {
    "4K": ("8k", "4k", "uhd", "2160p"),
    "FHD": ("fhd", "1080p", "1080i"),
    "HD": ("hd", "720p"),
    "SD": ("sd", "576p", "576i", "480p", "480i"),
}
# Words about how a stream is sent rather than what it is: taken off with the quality
NOISE_WORDS = ("hevc", "h265", "h.265", "h264", "h.264", "hdr", "hdr10", "50fps", "60fps", "25fps")


def load_settings():
    from core.models import CoreSettings

    values = dict(DEFAULTS)
    try:
        stored = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
        if stored and isinstance(stored.value, dict):
            values.update({k: v for k, v in stored.value.items() if k in DEFAULTS})
    except Exception as e:
        logger.debug(f"Could not read the channel manager settings: {e}")
    return values


def save_settings(values):
    from core.models import CoreSettings

    clean = {k: values[k] for k in DEFAULTS if k in values}
    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY, defaults={"name": "Channel Manager", "value": clean}
    )
    return clean


def settings_from(given):
    """The settings to run with: what was given, over what is saved, over the defaults."""
    values = load_settings()
    values.update({k: v for k, v in (given or {}).items() if k in DEFAULTS})
    return values


# ── Recognising a channel in a name ──────────────────────────────────────────


def _word_pattern(words):
    escaped = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![0-9a-z])(?:{escaped})(?![0-9a-z])", re.IGNORECASE)


QUALITY_PATTERNS = {label: _word_pattern(words) for label, words in QUALITY_WORDS.items()}
ALL_QUALITY = _word_pattern([w for words in QUALITY_WORDS.values() for w in words] + list(NOISE_WORDS))


def _tags(settings):
    return [tag.strip() for tag in str(settings.get("ignore_tags") or "").split(",") if tag.strip()]


def clean_name(name, settings):
    """
    A stream's name with everything that is about the stream rather than the channel taken
    off: the rules given, the tags to ignore, and the quality. What is left is the channel
    as a person would name it, country box and all.
    """
    text = str(name or "")
    for rule in settings.get("regex_rules") or ():
        try:
            find, replace = rule[0], rule[1] if len(rule) > 1 else ""
            text = re.sub(find, replace, text, flags=re.IGNORECASE)
        except (re.error, IndexError, TypeError) as e:
            logger.debug(f"Skipped a regex rule that does not work: {rule}: {e}")
    for tag in _tags(settings):
        # A tag in brackets is taken as written; a bare word only as a whole word
        if tag[:1] in "[(":
            text = re.sub(re.escape(tag), " ", text, flags=re.IGNORECASE)
        else:
            text = _word_pattern([tag]).sub(" ", text)
    # Resolution in brackets, as some playlists write it: "ATV (Belgium) (1080p)". Before
    # the quality words, which would take the 1080p and leave the brackets behind.
    text = re.sub(r"\(\s*\d{3,4}[pi]\s*\)", " ", text, flags=re.IGNORECASE)
    text = ALL_QUALITY.sub(" ", text)
    # Brackets anything above emptied, which would otherwise be part of the name
    text = re.sub(r"[(\[]\s*[)\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -|:")


def _alias_map(settings):
    """Every alias's key pointing at the key of the name it stands for."""
    mapping = {}
    for canonical, others in (settings.get("aliases") or {}).items():
        target = _key(clean_name(canonical, settings), settings)
        for other in others if isinstance(others, (list, tuple)) else [others]:
            key = _key(clean_name(other, settings), settings)
            if key and target:
                mapping[key] = target
    return mapping


def _exact_key(name):
    """The name as written, apart from case and runs of spaces."""
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def _key(name, settings):
    if settings.get("name_matching") == "loose":
        return logo_library.match_key(name)
    return _exact_key(name)


def channel_key(name, settings, aliases=None):
    """What identifies a channel in a name, the same whichever provider wrote it."""
    key = _key(clean_name(name, settings), settings)
    aliases = aliases if aliases is not None else _alias_map(settings)
    return aliases.get(key, key)


def quality_of(name, stats=None):
    """
    How good a stream's picture is, as a label and a rank (lower is better).

    The picture a probe measured, where there is one, because a name can claim anything.
    Otherwise what the name says. A stream that says nothing is ranked between HD and SD:
    it is usually one or the other, and putting it last would bury a good stream.
    """
    height = 0
    resolution = (stats or {}).get("resolution") if isinstance(stats, dict) else None
    if isinstance(resolution, str) and "x" in resolution:
        try:
            height = int(resolution.lower().split("x", 1)[1])
        except ValueError:
            height = 0
    if height:
        label = "4K" if height >= 2000 else "FHD" if height >= 1000 else "HD" if height >= 700 else "SD"
        return label, QUALITY_LABELS.index(label), True
    for label in QUALITY_LABELS:
        if QUALITY_PATTERNS[label].search(str(name or "")):
            return label, QUALITY_LABELS.index(label), False
    return "", 2.5, False


def _strip_country_box(name):
    return re.sub(r"^\s*[┃|\[(][^┃|\])]*[┃|\])]\s*", "", name or "").strip()


def country_for(name, group_name=""):
    """The country a stream or channel says it is from: its name first, then its group."""
    return logo_library.country_of(name) or logo_library.country_of(group_name or "")


# ── Building the plan ────────────────────────────────────────────────────────


def _stream_rows(settings):
    """The streams in scope, as plain rows, with what they are and where they come from."""
    from apps.m3u.models import M3UAccount

    from .models import ChannelGroup, Stream

    streams = Stream.objects.all()
    accounts = [int(a) for a in settings.get("accounts") or ()]
    if accounts:
        streams = streams.filter(m3u_account_id__in=accounts)
    else:
        streams = streams.filter(m3u_account__is_active=True) | streams.filter(m3u_account__isnull=True)
    groups = [int(g) for g in settings.get("stream_groups") or ()]
    if groups:
        streams = streams.filter(channel_group_id__in=groups)
    if settings.get("skip_stale"):
        streams = streams.filter(is_stale=False)
    if settings.get("skip_custom"):
        streams = streams.filter(is_custom=False)

    account_names = dict(M3UAccount.objects.values_list("id", "name"))
    group_names = dict(ChannelGroup.objects.values_list("id", "name"))
    priority = {account_id: index for index, account_id in enumerate(accounts)}
    aliases = _alias_map(settings)

    return [
        _row(s, settings, aliases, account_names, group_names, priority, in_scope=True)
        for s in streams.values(*STREAM_FIELDS)
    ]


STREAM_FIELDS = (
    "id", "name", "tvg_id", "logo_url", "m3u_account_id", "channel_group_id",
    "stream_stats", "is_custom", "stream_hash",
)


def _attached_rows(stream_ids, settings):
    """
    Streams already on a channel but outside what is being looked at: another account, a
    group not chosen, a custom stream. They are shown, and kept exactly where they are.
    Nothing is decided about them, because they were not part of what was asked.
    """
    from apps.m3u.models import M3UAccount

    from .models import ChannelGroup, Stream

    if not stream_ids:
        return []
    account_names = dict(M3UAccount.objects.values_list("id", "name"))
    group_names = dict(ChannelGroup.objects.values_list("id", "name"))
    aliases = _alias_map(settings)
    return [
        _row(s, settings, aliases, account_names, group_names, {}, in_scope=False)
        for s in Stream.objects.filter(id__in=list(stream_ids)).values(*STREAM_FIELDS)
    ]


def _row(s, settings, aliases, account_names, group_names, priority, in_scope):
    """One stream as the plan works with it: what it is, where from, and how good."""
    label, rank, probed = quality_of(s["name"], s["stream_stats"])
    group_name = group_names.get(s["channel_group_id"], "")
    return {
        "id": s["id"],
        "name": s["name"] or "",
        "clean": clean_name(s["name"], settings),
        "key": channel_key(s["name"], settings, aliases),
        "country": country_for(s["name"], group_name),
        "tvg_id": (s["tvg_id"] or "").strip(),
        "logo_url": s["logo_url"] or "",
        "account_id": s["m3u_account_id"],
        "account": account_names.get(s["m3u_account_id"], "custom"),
        "priority": priority.get(s["m3u_account_id"], len(priority)),
        "group": group_name,
        "group_id": s["channel_group_id"],
        "quality": label,
        "quality_rank": rank,
        "probed": probed,
        "custom": bool(s["is_custom"]),
        "in_scope": in_scope,
        # What the preview player plays it by, so two streams can be watched to see if
        # they are really the same channel
        "hash": s["stream_hash"] or "",
    }


def _ordered(streams, settings):
    if settings.get("order") == "provider":
        return sorted(streams, key=lambda s: (s["priority"], s["quality_rank"], s["name"]))
    return sorted(streams, key=lambda s: (s["quality_rank"], s["priority"], s["name"]))


def _existing_channels(settings, aliases):
    """The channels streams may be added to, with what they have now."""
    from .models import Channel, ChannelStream

    channels = Channel.objects.select_related("channel_group", "logo", "epg_data")
    groups = [int(g) for g in settings.get("channel_groups") or ()]
    if groups:
        channels = channels.filter(channel_group_id__in=groups)
    found = {}
    for channel in channels:
        group_name = channel.channel_group.name if channel.channel_group_id else ""
        found[channel.id] = {
            "channel": channel,
            "key": channel_key(channel.name, settings, aliases),
            "country": country_for(channel.name, group_name),
            "stream_ids": [],
        }
    for channel_id, stream_id in (
        ChannelStream.objects.filter(channel_id__in=list(found))
        .order_by("channel_id", "order")
        .values_list("channel_id", "stream_id")
    ):
        found[channel_id]["stream_ids"].append(stream_id)
    return found


def _pick(candidates, country, same_country):
    """
    The one channel a stream belongs to among those with its name, or None and why not.

    The same country first. A country nobody states matches any, because a stream or a
    channel that does not say is not a different country. Two that fit equally is a
    conflict, not a choice to make.
    """
    if not candidates:
        return None, None
    if country:
        same = [c for c in candidates if c["country"] == country]
        if len(same) == 1:
            return same[0], None
        if len(same) > 1:
            return None, same
        unstated = [c for c in candidates if not c["country"]]
        if len(unstated) == 1:
            return unstated[0], None
        if len(unstated) > 1:
            return None, unstated
        if same_country:
            return None, None
    if len(candidates) == 1:
        return candidates[0], None
    return None, candidates


def _stream_summary(stream, added=False, removed=False):
    return {
        "hash": stream.get("hash", ""),
        "custom": stream.get("custom", False),
        "in_scope": stream.get("in_scope", True),
        "id": stream["id"],
        "name": stream["name"],
        "account": stream["account"],
        "group": stream["group"],
        "quality": stream["quality"],
        "probed": stream["probed"],
        "tvg_id": stream["tvg_id"],
        "logo_url": stream["logo_url"],
        "added": added,
        "removed": removed,
    }


def _channel_summary(channel):
    epg = channel.epg_data
    return {
        "id": channel.id,
        "name": channel.name,
        "number": channel.channel_number,
        "group": channel.channel_group.name if channel.channel_group_id else "",
        "logo_url": channel.logo.url if channel.logo_id else "",
        "epg": {"id": epg.id, "name": epg.name, "tvg_id": epg.tvg_id, "how": "kept"} if epg else None,
    }


class _Guides:
    """Guide entries by tvg-id and by name, looked up once for the whole plan."""

    def __init__(self):
        from apps.epg.models import EPGData

        self.by_tvg_id = {}
        self.by_key = {}
        for epg_id, tvg_id, name, source in EPGData.objects.values_list(
            "id", "tvg_id", "name", "epg_source__name"
        ):
            entry = {"id": epg_id, "tvg_id": tvg_id or "", "name": name or "", "source": source or ""}
            if tvg_id:
                self.by_tvg_id.setdefault(tvg_id.lower(), entry)
            key = logo_library.match_key(name)
            if key:
                self.by_key.setdefault(key, entry)

    def find(self, streams, name, mode):
        if mode == "keep":
            return None
        for stream in streams:
            entry = self.by_tvg_id.get(stream["tvg_id"].lower()) if stream["tvg_id"] else None
            if entry:
                return {**entry, "how": "tvg-id"}
        if mode == "tvg_id_then_name":
            entry = self.by_key.get(logo_library.match_key(name))
            if entry:
                return {**entry, "how": "name"}
        return None


def _logo_for(name, streams, mode, index):
    if mode == "keep":
        return ""
    if mode == "collections" and index:
        found = logo_library.suggestions_for(name, index, limit=1)
        if found:
            return found[0]["url"]
    return next((s["logo_url"] for s in streams if s["logo_url"].startswith("http")), "")


def build_plan(settings):
    """
    Every channel as it would come out: what goes into it, and what it would look like.

    Streams are matched to the channels in scope by tvg-id, where both have one and it is
    asked for, and otherwise by what their names say they are. What matches no channel
    becomes a new one when that is asked for. Nothing is written.
    """
    from .models import ChannelGroup

    aliases = _alias_map(settings)
    streams = _stream_rows(settings)
    by_id = {s["id"]: s for s in streams}
    existing = _existing_channels(settings, aliases)
    # What is on those channels already but outside the scope, so it can be shown and kept
    attached = {i for r in existing.values() for i in r["stream_ids"]} - set(by_id)
    for stream in _attached_rows(attached, settings):
        by_id[stream["id"]] = stream

    by_key = {}
    by_tvg = {}
    for record in existing.values():
        if record["key"]:
            by_key.setdefault(record["key"], []).append(record)
        tvg_id = (record["channel"].tvg_id or "").strip().lower()
        if tvg_id:
            by_tvg.setdefault(tvg_id, []).append(record)

    # Every stream already on a channel in scope counts as that channel's, whatever it is
    # called: it was put there by someone
    belongs = {}
    for record in existing.values():
        for stream_id in record["stream_ids"]:
            belongs.setdefault(stream_id, set()).add(record["channel"].id)

    additions = {}
    conflicts = {}
    homeless = {}
    same_country = bool(settings.get("same_country"))
    for stream in streams:
        record, tied = None, None
        if settings.get("match_tvg_id") and stream["tvg_id"]:
            record, tied = _pick(by_tvg.get(stream["tvg_id"].lower(), []), stream["country"], same_country)
        if record is None and tied is None and stream["key"]:
            record, tied = _pick(by_key.get(stream["key"], []), stream["country"], same_country)
        if tied:
            if settings.get("several_matches") == "conflict":
                conflict_key = (stream["country"], stream["key"])
                conflicts.setdefault(conflict_key, {"streams": [], "channels": tied})["streams"].append(stream)
            else:
                # Each channel of that name gets it, as DispatcharrUtils does
                for each in tied:
                    if stream["id"] not in each["stream_ids"]:
                        additions.setdefault(each["channel"].id, []).append(stream)
            continue
        if record is not None:
            if stream["id"] not in record["stream_ids"]:
                additions.setdefault(record["channel"].id, []).append(stream)
            continue
        if stream["key"] and stream["id"] not in belongs:
            homeless.setdefault((stream["country"], stream["key"]), []).append(stream)

    index = logo_library.load_index() if settings.get("logo") == "collections" else None
    guides = _Guides()
    rows = []

    # ── Channels there already are ──
    for channel_id, record in existing.items():
        channel = record["channel"]
        attached_now = [by_id[s] for s in record["stream_ids"] if s in by_id]
        # A custom stream on a channel is its fallback -- the screen that says the channel
        # could not be played -- and belongs at the end, after every real stream. Anything
        # added goes in before it, or it would never be tried before the fallback.
        custom = [s for s in attached_now if s["custom"]]
        normal = [s for s in attached_now if not s["custom"]]
        added = additions.get(channel_id, [])
        if settings.get("drop_sd_when_hd"):
            if any(s["quality_rank"] < 3 for s in normal + added):
                added = [s for s in added if s["quality"] != "SD"]

        removed = []
        if settings.get("replace_streams"):
            # Only what was looked at can be judged: a stream from another account or group
            # was not part of the question, and stays
            judged = [s for s in normal if s["in_scope"]]
            wrong = [s for s in judged if not (s["key"] == record["key"] or (
                s["tvg_id"] and s["tvg_id"].lower() == (channel.tvg_id or "").lower()))]
            # Never empties a channel: with nothing of its own left it keeps what it has
            if len(normal) - len(wrong) + len(added) > 0:
                removed = wrong

        kept = [s for s in normal if s not in removed]
        if settings.get("reorder_existing"):
            in_scope = [s for s in kept if s["in_scope"]]
            outside = [s for s in kept if not s["in_scope"]]
            final = _ordered(in_scope + added, settings) + outside + custom
        else:
            final = kept + _ordered(added, settings) + custom
        added_ids = {s["id"] for s in added}
        reordered = [s["id"] for s in final if s["id"] not in added_ids] != [
            s["id"] for s in kept + custom
        ]

        summary = _channel_summary(channel)
        changes = []
        if not channel.epg_data_id and settings.get("epg") != "keep":
            found = guides.find(final, _strip_country_box(channel.name), settings.get("epg"))
            if found:
                summary["epg"] = found
                changes.append("epg")
        if not channel.logo_id and settings.get("logo") != "keep":
            url = _logo_for(channel.name, final, settings.get("logo"), index)
            if url:
                summary["logo_url"] = url
                changes.append("logo")

        status = "merge" if (added or removed or changes or reordered) else "unchanged"
        rows.append({
            "key": f"ch:{channel_id}",
            "status": status,
            "channel": summary,
            "before": {
                "channel": _channel_summary(channel),
                "streams": [_stream_summary(s) for s in attached_now],
            },
            "streams": [_stream_summary(s, added=s["id"] in added_ids) for s in final]
            + [_stream_summary(s, removed=True) for s in removed],
            "adds": len(added),
            "removes": len(removed),
            "changes": changes,
            "country": record["country"],
        })

    # ── Two channels that could both be it ──
    for (country, key), conflict in conflicts.items():
        rows.append({
            "key": f"conflict:{country}:{key}",
            "status": "conflict",
            "channel": None,
            "candidates": [_channel_summary(c["channel"]) for c in conflict["channels"]],
            "before": {"channel": None, "streams": [_stream_summary(s) for s in conflict["streams"]]},
            "streams": [],
            "adds": 0,
            "removes": 0,
            "changes": [],
            "country": country,
        })

    # ── Channels there are not, yet ──
    if settings.get("create_new"):
        from .models import Channel

        target = settings.get("target_group")
        target_name = ""
        if target:
            target_name = ChannelGroup.objects.filter(id=target).values_list("name", flat=True).first() or ""
        number = settings.get("number_start")
        if number in (None, ""):
            highest = Channel.objects.order_by("-channel_number").values_list("channel_number", flat=True).first()
            number = int(highest or 0) + 1
        number = float(number)

        planned = []
        for (country, key), found in homeless.items():
            if len(found) < int(settings.get("min_streams_new") or 1):
                continue
            ordered = _ordered(found, settings)
            if settings.get("drop_sd_when_hd") and any(s["quality_rank"] < 3 for s in ordered):
                ordered = [s for s in ordered if s["quality"] != "SD"]
            best = min(ordered, key=lambda s: (s["priority"], s["quality_rank"]))
            name = best["clean"] if settings.get("keep_country_prefix") else _strip_country_box(best["clean"])
            planned.append((name, country, key, ordered, best))

        for name, country, key, ordered, best in sorted(planned, key=lambda p: p[0].lower()):
            epg = guides.find(ordered, _strip_country_box(name), settings.get("epg"))
            logo = _logo_for(name, ordered, settings.get("logo"), index)
            rows.append({
                "key": f"new:{country}:{key}",
                "status": "new",
                "channel": {
                    "id": None,
                    "name": name,
                    "number": number,
                    "group": target_name or best["group"],
                    "group_id": target or best["group_id"],
                    "logo_url": logo,
                    "epg": epg,
                },
                "before": {"channel": None, "streams": [_stream_summary(s) for s in ordered]},
                "streams": [_stream_summary(s, added=True) for s in ordered],
                "adds": len(ordered),
                "removes": 0,
                "changes": [],
                "country": country,
            })
            number += 1

    order = {"new": 0, "merge": 1, "conflict": 2, "unchanged": 3}
    rows.sort(key=lambda r: (order[r["status"]], (r["channel"] or {}).get("number") or 0))
    summary = {status: sum(1 for r in rows if r["status"] == status) for status in order}
    summary["streams"] = len(streams)
    summary["streams_added"] = sum(r["adds"] for r in rows)
    return {"rows": rows, "summary": summary}


# ── Applying ─────────────────────────────────────────────────────────────────


def _logo_id(url):
    if not url:
        return None
    from .models import Logo

    logo, _ = Logo.objects.get_or_create(url=url, defaults={"name": url.rsplit("/", 1)[-1][:255]})
    return logo.id


def apply_plan(settings, keys):
    """
    Carry out the chosen rows of the plan, worked out again now rather than trusted from the
    page: if the streams have changed since it was looked at, what is applied is what is
    true now, not what was true then. Conflicts are never applied.
    """
    from django.db import transaction

    from .models import Channel, ChannelProfile, ChannelProfileMembership, ChannelStream

    wanted = set(keys or ())
    plan = build_plan(settings)
    rows = [r for r in plan["rows"] if r["key"] in wanted and r["status"] in ("new", "merge")]
    created = updated = streams_added = 0

    with transaction.atomic():
        for row in rows:
            final_ids = [s["id"] for s in row["streams"] if not s["removed"]]
            if not final_ids:
                # Never leaves a channel with nothing to play
                continue
            info = row["channel"]
            if row["status"] == "new":
                channel = Channel.objects.create(
                    name=info["name"][:255],
                    channel_number=info["number"],
                    channel_group_id=info.get("group_id"),
                    epg_data_id=(info.get("epg") or {}).get("id"),
                    logo_id=_logo_id(info.get("logo_url")),
                    tvg_id=(info.get("epg") or {}).get("tvg_id") or None,
                )
                profiles = settings.get("profiles")
                if profiles == "all":
                    chosen = ChannelProfile.objects.all()
                elif isinstance(profiles, list):
                    chosen = ChannelProfile.objects.filter(id__in=profiles)
                else:
                    chosen = ChannelProfile.objects.none()
                ChannelProfileMembership.objects.bulk_create(
                    [ChannelProfileMembership(channel_profile=p, channel=channel, enabled=True) for p in chosen],
                    ignore_conflicts=True,
                )
                created += 1
            else:
                channel = Channel.objects.get(id=info["id"])
                fields = []
                if "epg" in row["changes"] and info.get("epg"):
                    channel.epg_data_id = info["epg"]["id"]
                    fields.append("epg_data")
                if "logo" in row["changes"] and info.get("logo_url"):
                    channel.logo_id = _logo_id(info["logo_url"])
                    fields.append("logo")
                if fields:
                    channel.save(update_fields=fields)
                updated += 1

            # Only what the plan names as removed goes; anything it did not look at stays
            removed_ids = [s["id"] for s in row["streams"] if s["removed"]]
            if removed_ids:
                ChannelStream.objects.filter(channel=channel, stream_id__in=removed_ids).delete()
            present = set(ChannelStream.objects.filter(channel=channel).values_list("stream_id", flat=True))
            for order, stream_id in enumerate(final_ids):
                if stream_id in present:
                    ChannelStream.objects.filter(channel=channel, stream_id=stream_id).update(order=order)
                else:
                    ChannelStream.objects.create(channel=channel, stream_id=stream_id, order=order)
            streams_added += row["adds"]

    logger.info(
        f"Channel Manager: {created} channel(s) made, {updated} merged, "
        f"{streams_added} stream(s) added"
    )
    return {"created": created, "updated": updated, "streams_added": streams_added}
