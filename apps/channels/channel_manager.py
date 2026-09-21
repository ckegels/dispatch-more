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

Out of the box it matches the way DispatcharrUtils does, because that is what people
trust: the whole name, country box and all, with only a quality at its end and the case
set aside, and every channel of that name given the stream. Nothing else is trusted by
default -- not a tvg-id, which providers share between channels that are not the same,
and not a guess at the country. Every other way of matching is a lever to turn on.

Two rules learned from tools that did this before, and kept:

- a channel is never emptied: a run that finds nothing for it leaves what it has;
- a stream is only taken away from a channel when that is asked for, never by default.

Loose matching reuses the key Find Logos matches on (see logo_library.match_key), which
has already been taught what goes wrong: accents folded rather than dropped, "+" and "&"
as words, and the country box in front taken off.
"""

import fnmatch
import logging
import re

from . import known_channels, logo_library

logger = logging.getLogger(__name__)

SETTINGS_KEY = "channel-manager"
# Suggestions a person said not to make again: {row key: {"name", "kind", "streams", "at"}}.
# A new channel or a conflict is ignored whole; for a channel you have, only the streams it
# would have gained or lost, so a stream the provider adds later is still suggested.
IGNORED_KEY = "channel-manager-ignored"

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
    # Which groups the group picker offers, of the four kinds the page works out
    # (see channel_manager_views): the ones you have channels in and the ones with
    # nothing in them, which are the ones somebody made by hand. A provider's groups are
    # not offered unless asked for: there are hundreds of them and no channel is in any.
    "group_choices": ["with_channels", "empty"],
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
    # Two channels that are the same channel are made one: the streams of all of them go
    # on the one kept and the rest are deleted. On, because having one channel twice is
    # the thing people come here to fix and DispatcharrUtils leaves you with both. Like
    # every other suggestion it is only a suggestion: nothing is deleted until its row is
    # ticked and applied, and the row names every channel that would go. It is still the
    # only thing here that removes a channel, and one removed is gone until a backup is
    # restored, so the row says so and the apply warns again.
    "combine_duplicates": True,
    # Off by default, as DispatcharrUtils has no such thing: providers give one tvg-id to
    # many channels -- every CBS station, an East and a West feed, and on one real setup a
    # Krone stream carrying Euronews' -- and trusting it merged them all
    "match_tvg_id": False,
    # Words that are about the stream, not the channel, taken off before matching. The two
    # the group merge people ran by hand took off; anything more is theirs to add.
    # "⏺ʳᵉᶜ" is how providers mark a stream they are recording: it says nothing about
    # which channel it is, and left on it makes one channel look like two
    "ignore_tags": "VIP, RAW, ⏺ʳᵉᶜ",
    # [[find, replace], ...] applied to stream names before anything else
    "regex_rules": [],
    # {"Channel name": ["another name", ...]} for channels known by more than one
    "aliases": {},
    # Only put a stream on a channel of the same country, when both say one. Off, as in
    # DispatcharrUtils: with the whole name compared, the country box already decides it
    "same_country": False,
    # ── Quality ──
    # "quality" puts the best picture first, "provider" the preferred account first.
    # "provider" is what DispatcharrUtils does.
    "order": "provider",
    "skip_stale": True,
    # Custom streams are made by hand and are nobody's copy of anything
    "skip_custom": True,
    "drop_sd_when_hd": False,
    # ── What to change ──
    # Streams that match no channel are suggested as new ones. Only suggested: nothing is
    # made unless its row is ticked and applied
    "create_new": True,
    # Which streams are suggested as new channels: "followed", from the stream groups you
    # already take channels from (a provider adding a channel to a group you use), or "all"
    # -- every stream, which on most setups is tens of thousands
    "new_from": "followed",
    # Give a new channel the custom fallback stream most of your channels end in
    "new_fallback": True,
    # A new channel's logo: "collections" (the Find Logos collections, then the stream's),
    # "stream", or "none". A channel you have keeps its logo unless "logo" below says
    "new_logo": "collections",
    "min_streams_new": 1,
    "keep_country_prefix": True,
    # None numbers each new channel after the last one in its group, on a number nobody has
    "number_start": None,
    "reorder_existing": False,
    "replace_streams": False,
    # ── EPG and logo ──
    # "keep", "tvg_id" or "tvg_id_then_name". "keep" by default: DispatcharrUtils does
    # not touch a channel's guide
    "epg": "keep",
    # "keep", "collections" (the Find Logos collections, then the stream's) or "stream".
    # "keep" by default, for the same reason
    "logo": "keep",
}

# Which settings a saved set is allowed to keep when the defaults change under it. The
# first defaults matched far more loosely than DispatcharrUtils, and a page opened once
# saved them all, so they would have outlived the fix. What was chosen to look at is
# kept; how matching is done goes back to the defaults.
DEFAULTS_VERSION = 4
# What changed in each version, and so what a set saved before it takes from the defaults;
# the rest of what was saved is kept. Version 3: new channels are suggested by default.
# Version 4: channels that are the same channel are combined, and the mark providers put
# on a stream they are recording is ignored.
CHANGED_IN = {3: ("create_new",), 4: ("combine_duplicates", "ignore_tags")}
SCOPE_SETTINGS = ("accounts", "stream_groups", "channel_groups", "target_group", "profiles")

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
            version = stored.value.get("version")
            if version == DEFAULTS_VERSION:
                kept = set(DEFAULTS)
            elif version in (2,):
                changed = {k for v, keys in CHANGED_IN.items() if v > version for k in keys}
                kept = set(DEFAULTS) - changed
            else:
                kept = set(SCOPE_SETTINGS)
            values.update({k: v for k, v in stored.value.items() if k in kept})
    except Exception as e:
        logger.debug(f"Could not read the channel manager settings: {e}")
    return values


def save_settings(values):
    from core.models import CoreSettings

    clean = {k: values[k] for k in DEFAULTS if k in values}
    CoreSettings.objects.update_or_create(
        key=SETTINGS_KEY,
        defaults={"name": "Channel Manager", "value": {**clean, "version": DEFAULTS_VERSION}},
    )
    return clean


def settings_from(given):
    """The settings to run with: what was given, over what is saved, over the defaults."""
    values = load_settings()
    values.update({k: v for k, v in (given or {}).items() if k in DEFAULTS})
    return values


# ── Recognising a channel in a name ──────────────────────────────────────────


def _tag_pattern(tag):
    """
    How one word to ignore is looked for.

    A bare word is only taken as a whole word, so "RAW" does not come out of "DRAWING".
    But that guard only makes sense at an edge that is a letter or a digit: a tag like
    "⏺ʳᵉᶜ", the mark providers put on a stream they are recording, starts and ends with
    something that is neither, and asking for a word boundary there means it is missed
    the moment a provider writes it up against the name -- "NPO 1⏺ʳᵉᶜ".
    """
    before = "(?<![0-9a-z])" if tag[:1].lower() in "0123456789abcdefghijklmnopqrstuvwxyz" else ""
    after = "(?![0-9a-z])" if tag[-1:].lower() in "0123456789abcdefghijklmnopqrstuvwxyz" else ""
    return re.compile(rf"{before}{re.escape(tag)}{after}", re.IGNORECASE)


def _word_pattern(words):
    escaped = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![0-9a-z])(?:{escaped})(?![0-9a-z])", re.IGNORECASE)


QUALITY_PATTERNS = {label: _word_pattern(words) for label, words in QUALITY_WORDS.items()}
ALL_QUALITY = _word_pattern([w for words in QUALITY_WORDS.values() for w in words] + list(NOISE_WORDS))
# One quality or noise word ending a name, bare or in brackets: "ORF 1 HD", "ORF 1 (1080p)".
# Taken off again and again, so "ORF 1 FHD HEVC" is ORF 1 too.
_ENDINGS = "|".join(
    re.escape(w)
    for w in sorted([w for words in QUALITY_WORDS.values() for w in words] + list(NOISE_WORDS), key=len, reverse=True)
)
TRAILING_QUALITY = re.compile(
    rf"[\s\-|:]*(?:[(\[]\s*(?:{_ENDINGS})\s*[)\]]|(?<![0-9a-z])(?:{_ENDINGS}))\s*$", re.IGNORECASE
)


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
        text = _tag_pattern(tag).sub(" ", text)
    if settings.get("name_matching") == "loose":
        # Resolution in brackets, as some playlists write it: "ATV (Belgium) (1080p)".
        # Before the quality words, which would take the 1080p and leave the brackets.
        text = re.sub(r"\(\s*\d{3,4}[pi]\s*\)", " ", text, flags=re.IGNORECASE)
        text = ALL_QUALITY.sub(" ", text)
        # Brackets anything above emptied, which would otherwise be part of the name
        text = re.sub(r"[(\[]\s*[)\]]", " ", text)
        return re.sub(r"\s+", " ", text).strip(" -|:")
    # Exactly: only the quality at the end of the name, as DispatcharrUtils' normalizer and
    # the group merge take it off. In the middle a word like "4K" or "HD" can be part of
    # what the channel is, and taking it out made different channels one.
    text = re.sub(r"\s+", " ", text).strip()
    while True:
        shorter = TRAILING_QUALITY.sub("", text).strip()
        if shorter == text or not shorter:
            break
        text = shorter
    return text.strip(" -|:")


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
    """
    The name without the country a playlist writes in front of it.

    Both shapes: a box of some kind, and the "US: " a playlist writes instead -- but only
    where those two letters are a country (logo_library.country_of), since the same shape
    is how a package is written and a package is a word of nobody's name.
    """
    plain = re.sub(r"^\s*[┃|\[(][^┃|\])]*[┃|\])]\s*", "", name or "").strip()
    if plain == (name or "").strip() and logo_library.country_of(plain):
        plain = re.sub(r"^\s*[A-Za-z]{2}\s*[:|-]\s*", "", plain).strip()
    return plain


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
    # Parked by Stream Check: taken off their channels until they work again, and merging
    # them would put them straight back
    from .stream_check import parked_ids

    parked = parked_ids()
    if parked:
        streams = streams.exclude(id__in=parked)

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

    channels = Channel.objects.select_related(
        "channel_group", "logo", "epg_data", "epg_data__epg_source"
    )
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


def _pick(candidates, country, same_country, give_all=False):
    """
    The one channel a stream belongs to among those with its name, or None and why not.

    The same country first. A country nobody states matches any, because a stream or a
    channel that does not say is not a different country. Two that fit equally is a
    conflict, not a choice to make.

    Unless every one of them is to have it, as in DispatcharrUtils: then they all fit, and
    the country only narrows them when that is asked for.
    """
    if not candidates:
        return None, None
    if give_all:
        if same_country and country:
            candidates = [c for c in candidates if c["country"] in (country, "")]
        if len(candidates) == 1:
            return candidates[0], None
        return None, candidates or None
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
        # By id as well as by name, so the page can narrow to a group without matching
        # on text -- and so a new channel's suggested group and a channel you have are
        # the same kind of thing to narrow by
        "group_id": channel.channel_group_id,
        "logo_url": channel.logo.url if channel.logo_id else "",
        # The source is part of it: the page shows the guide a channel has beside the one
        # it would come out with, and two entries of one name are told apart by where
        # they come from
        "epg": {
            "id": epg.id,
            "name": epg.name,
            "tvg_id": epg.tvg_id,
            "source": epg.epg_source.name if epg.epg_source_id else "",
            "how": "kept",
        } if epg else None,
    }


def _fill_what_is_on(rows):
    """
    What is on each guide the plan names, on both sides of every row.

    The page shows the guide a channel is on beside the one it would come out with, and
    a name is not enough to tell whether it is the right one: the programme on it at this
    moment is. Worked out for the whole plan at once -- two queries on the index
    ProgramData already has -- rather than per row, which on a thousand channels would be
    a thousand of them.
    """
    from django.db.models import Count
    from django.utils import timezone

    from apps.epg.models import ProgramData

    guides = []
    for row in rows:
        for side in (row.get("channel"), (row.get("before") or {}).get("channel")):
            if side and side.get("epg"):
                guides.append(side["epg"])
    ids = {guide["id"] for guide in guides if guide.get("id")}
    if not ids:
        return rows
    counts = dict(
        ProgramData.objects.filter(epg_id__in=ids)
        .values_list("epg_id")
        .annotate(held=Count("id"))
        .values_list("epg_id", "held")
    )
    moment = timezone.now()
    playing = dict(
        ProgramData.objects.filter(epg_id__in=ids, start_time__lte=moment, end_time__gt=moment)
        .values_list("epg_id", "title")
    )
    for guide in guides:
        guide["programmes"] = counts.get(guide["id"], 0)
        guide["now"] = playing.get(guide["id"], "")
    return rows


class _Guides:
    """
    Guide entries by tvg-id and by name, looked up once for the whole plan.

    A source switched off is not offered, and the ones with the higher priority are read
    first, so that when two sources carry the same channel the one you put first wins.
    Before that it was whichever the database happened to hand over, which is how a big
    source nobody ranked came to shadow the right entry. An entry belonging to no source
    at all is still offered: nobody switched it off.

    This stays a plain lookup on purpose: it runs over every channel at once, and on a
    setup with a thousand channels there is no room here for anything cleverer. The better
    matching is in guide_candidates, asked for one row at a time when someone opens it.
    """

    def __init__(self):
        from apps.epg.models import EPGData

        self.by_tvg_id = {}
        self.by_key = {}
        entries = (
            EPGData.objects.exclude(epg_source__is_active=False)
            .order_by("-epg_source__priority", "id")
            .values_list("id", "tvg_id", "name", "epg_source__name")
        )
        for epg_id, tvg_id, name, source in entries:
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


# How alike a guide's name has to be before it is worth offering at all. The matcher
# scores everything it sees, so without a floor a channel with no guide is still offered
# the three least unlike names in the whole file, which reads as if they were matches.
# "ORF 1" against "ORF Eins" is the same channel written twice and scores a hundred, a
# number word being read as its number; against "Sender Eins" it scores forty-six, which
# is two names that share a word. Anything under it is what the search is for.
MIN_GUIDE_SCORE = 55

# What a guide's country is worth when the channel says which one it is. The matcher
# never sees it: normalize_name takes the box off before scoring, so "┃NL┃ DREAMWORKS"
# and a British "DreamWorks" are both "dreamworks" and score a flat 100 -- the same
# channel from the wrong country, offered as a certainty. The box is the surest thing
# there is about a channel of this fork's, so it is worth more than a near miss in the
# name: a guide from the country the channel says it is from is lifted, and one from a
# country it says it is not from falls below every candidate that could still be right.
SAME_COUNTRY = 10
OTHER_COUNTRY = 30
# The other ways one country gets written. A playlist's box says "UK", "USA", "GER";
# an XMLTV tvg-id says ".uk", ".us", ".de". Without this a British channel is penalised
# against a British guide and an American one against an American guide -- and since the
# country now counts against every match and not only against the tier, that penalty is
# no longer invisible.
ALSO_CALLED = {
    "uk": "gb", "eng": "gb", "gbr": "gb",
    "usa": "us", "can": "ca", "mex": "mx",
    "ger": "de", "deu": "de", "ned": "nl", "nld": "nl", "hol": "nl",
    "fra": "fr", "esp": "es", "ita": "it", "por": "pt", "bel": "be",
    "aut": "at", "sui": "ch", "che": "ch", "swe": "se", "nor": "no",
    "den": "dk", "dnk": "dk", "fin": "fi", "pol": "pl", "svk": "sk",
    "cze": "cz", "hun": "hu", "rom": "ro", "rou": "ro", "gre": "gr", "grc": "gr",
    "tur": "tr", "rus": "ru", "ukr": "ua", "aus": "au", "nzl": "nz",
    "bra": "br", "arg": "ar", "ire": "ie", "irl": "ie", "ind": "in",
}


def _one_country(code):
    """One country written one way, so "USA" and "us" are not two places."""
    code = (code or "").strip().lower()
    return ALSO_CALLED.get(code, code)


# A number written as a word, so "ORF Eins" and "ORF 1" are the one channel while
# "PBS 12" and "PBS 13" are not. English for the guides, then the languages this setup's
# channels are actually in.
NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eins": "1", "zwei": "2", "drei": "3", "vier": "4", "fuenf": "5", "funf": "5",
    "sechs": "6", "sieben": "7", "acht": "8", "neun": "9", "zehn": "10",
    "een": "1", "twee": "2", "drie": "3", "vijf": "5", "zes": "6", "zeven": "7",
    "negen": "9", "tien": "10",
    "un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5",
    "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
}

# What a name says that makes it one channel rather than its sibling. These are not
# decoration and must never be dropped before comparing: stock's own normalising takes
# "east" and "west" off as extraneous, which turns "PBS East" and "PBS West" into the
# same word and matches them at a hundred per cent.
SIDE_WORDS = {"east", "west", "eastern", "western", "atlantic", "pacific"}

# What a playlist writes in front to say which of its own packages a channel came from.
# It is not part of the channel's name and no guide has ever heard of it, so it is taken
# off before anything is compared -- an unmatched "SLING" costs a right answer a third of
# its score. Only in front, and only with the colon or bar a playlist writes it with:
# "PRIME" in the middle of a name is Amazon Prime and belongs to the channel.
PROVIDER_PREFIXES = {
    "sling", "go", "now", "vip", "prime", "sky", "plex", "pluto", "samsung", "tubi",
    "fubo", "philo", "peacock", "stirr", "xumo", "roku", "frndly", "directv", "dtv",
    "hulu", "youtube", "yt", "backup", "bk", "alt", "raw", "src", "source", "test",
}

# A channel that plays the same thing round the clock has no schedule anywhere, so no
# guide is the right guide for it: a match is wrong before it is scored.
ROUND_THE_CLOCK = ("24/7", "24-7", "247:", "24 7")

# The word that makes a channel one of a network's family rather than the network itself.
# Nat Geo Wild is not National Geographic, Nick Jr is not Nickelodeon, Discovery Science
# is not Discovery -- and once a short form is written out in full (ALSO_WRITTEN) the two
# are alike enough to be offered for each other, since all that parts them is this one
# word. So one name carrying one and the other carrying none is treated like a number one
# of them says and the other does not: still on the list, never put forward.
FAMILY_WORDS = {
    "wild", "junior", "jr", "kids", "baby", "science", "people", "turbo", "crime",
    "investigation", "life", "gold", "classic", "extra", "xtra", "movies", "music",
    "comedy", "family", "action", "drama", "nature", "history",
}


# How a stream is sent, and the mark an American station's name ends in. A guide has one
# entry for a channel however it is sent, so "CNN" and "CNN HD" are the same channel and
# an unmatched "HD" should not cost that match a quarter of its score. "DT" is digital
# television, which every American station is.
NOT_THE_CHANNEL = {
    "hd", "fhd", "uhd", "sd", "4k", "8k", "hevc", "h264", "h265", "raw", "dt", "tv",
    "1080p", "1080i", "720p", "576p", "480p", "50fps", "60fps",
    # What a playlist marks a spare copy with. Not the channel either way.
    "backup", "bkup",
}


# The same channel written short in one place and long in the other. A playlist says
# "NGC WILD" and a guide says "Nat Geo Wild": word for word those share one word of
# three, which is not enough for either to be offered for the other, and no amount of
# comparing letters will ever join them -- "ngc" and "nat geo" have nothing in common to
# compare. So the short form is written out, on both sides, before anything is compared.
#
# Deliberately short and deliberately dull. Every entry here is a claim that two names
# are one channel, which is exactly the claim this fork has got wrong before, so the only
# ones in it are abbreviations of a network's own name that a guide writes out in full.
# Nothing is guessed from initials alone: "CN" is Cartoon Network in one playlist and
# China in the next, "AP" is Animal Planet or Associated Press, and neither is here.
#
# Read longest first, so "nat geo wild" is not turned into "national geographic wild"
# twice over.
ALSO_WRITTEN = {
    ("nat", "geo"): ("national", "geographic"),
    ("natgeo",): ("national", "geographic"),
    ("ngc",): ("national", "geographic"),
    ("ngw",): ("national", "geographic", "wild"),
    ("cartoon", "netw"): ("cartoon", "network"),
    ("ctn",): ("cartoon", "network"),
    ("comedy", "cent"): ("comedy", "central"),
    ("comedy", "ctrl"): ("comedy", "central"),
    ("disc",): ("discovery",),
    ("discovery", "ch"): ("discovery",),
    ("anim", "planet"): ("animal", "planet"),
    ("animal", "pl"): ("animal", "planet"),
    ("hist",): ("history",),
    ("sci", "fi"): ("syfy",),
    ("scifi",): ("syfy",),
    ("nick", "jr"): ("nickelodeon", "junior"),
    ("nickjr",): ("nickelodeon", "junior"),
    ("nick",): ("nickelodeon",),
    ("dw",): ("deutsche", "welle"),
    ("fs", "1"): ("fox", "sports", "1"),
    ("fs", "2"): ("fox", "sports", "2"),
    ("fox", "spt"): ("fox", "sports"),
    ("sky", "spt"): ("sky", "sports"),
    ("sky", "sp"): ("sky", "sports"),
    ("cbssn",): ("cbs", "sports", "network"),
    ("nbcsn",): ("nbc", "sports", "network"),
    ("ch",): ("channel",),
    ("chan",): ("channel",),
}
# How many words the longest key is, so the window knows where to start
_MOST_WRITTEN = max(len(k) for k in ALSO_WRITTEN)


def _written_out(words):
    """The same words with any short form written out in full, longest form first."""
    if not any(w in _STARTS_ONE for w in words):
        return words
    out, at = [], 0
    while at < len(words):
        for span in range(min(_MOST_WRITTEN, len(words) - at), 0, -1):
            longer = ALSO_WRITTEN.get(tuple(words[at:at + span]))
            if longer:
                out.extend(longer)
                at += span
                break
        else:
            out.append(words[at])
            at += 1
    return out


# The first word of every short form, so a name with none of them is left alone without
# walking the table: this runs for every channel against every guide in the catalogue.
_STARTS_ONE = {key[0] for key in ALSO_WRITTEN}


def _without_prefix(text):
    """
    The name with a playlist's own package prefix taken off the front.

    Taken off one at a time, because they come in pairs: "US: SLING: CNN". The colon or
    bar is what makes it a prefix; without one it is a word of the name.
    """
    for _ in range(3):
        found = re.match(r"\s*([A-Za-z][A-Za-z0-9/\-]{0,9})\s*[:|]\s*", text)
        if not found or found.group(1).strip().lower() not in PROVIDER_PREFIXES:
            break
        text = text[found.end():]
    return text


def round_the_clock(name):
    """
    Whether this is a channel that plays one thing on a loop, which has no guide anywhere.

    Written in front like a package is ("24/7: The Office"), and the only thing on it is
    the thing in its name -- so the right answer is no guide at all, and any guide offered
    for it is wrong however well the names read.
    """
    plain = _strip_country_box(str(name or "")).strip().lower()
    return plain.startswith(ROUND_THE_CLOCK)


def guide_words(name):
    """
    A name as the words that say which channel it is.

    The country box comes off, words stuck together in camel case come apart
    ("FoxSports1" is "Fox Sports 1"), accents are folded, punctuation becomes space, how
    the stream is sent is dropped (NOT_THE_CHANNEL), a number written as a word becomes
    the number and a network's name written short is written out (ALSO_WRITTEN: "NGC" is
    "national geographic"). Nothing is thrown away -- not "east", not "network" -- because
    what looks like decoration next to one name is the whole difference next to its
    sibling.
    """
    import unicodedata

    text = _strip_country_box(str(name or ""))
    text = _without_prefix(text)
    # Camel case is two words written as one, and guides are full of it
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text).lower()
    text = text.replace("&", " and ").replace("+", " plus ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Lowered again after the unpacking, not only before it: "ᶠᴴᴰ" unpacks to "fHD", and
    # the sweep below keeps only lower-case letters -- so it ate the H and the D and left
    # a stray "f" on the name, costing the right guide a quarter of its score
    text = re.sub(r"[^0-9a-z]+", " ", text.lower())
    # Dropped before letters and digits are parted, so "4k" is still one word here
    kept = " ".join(w for w in text.split() if w not in NOT_THE_CHANNEL)
    # Letters and digits stuck together are two words: "BBC1" is "BBC 1", and has to be,
    # or it never matches "BBC One"
    kept = re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", " ", kept)
    words = []
    for word in kept.split():
        if word in NOT_THE_CHANNEL:
            continue
        words.append(NUMBER_WORDS.get(word, word))
    # Last, so a short form is looked for in words that have already been split apart and
    # had their numbers settled: "NatGeo" is "nat geo" by now, and "FS1" is "fs 1"
    return _written_out(words)


# A short word carrying few vowels is a name rather than a word: PBS, CBS, ABC, NBC, ORF,
# RTL, BBC, ITV, ZDF. Two of them are two broadcasters, and the one letter between them is
# the whole difference -- which comparing letters cannot see: "PBS Philadelphia" against
# "CBS Philadelphia" is ninety-four per cent alike and a different television station.
#
# The vowels are what keep ordinary short words out of it. "Nothing like it" would
# otherwise offer "like" and "it" as broadcasters and refuse everything.
NAME_LIKE = 4
VOWELS = set("aeiou")


def _names_in(words):
    return {
        w for w in words
        if w.isalpha() and len(w) <= NAME_LIKE
        and sum(1 for letter in w if letter in VOWELS) * 2 < len(w)
    }


# How alike two words have to be to count as the same word: enough for a spelling or a
# plural, not enough for a different name
SAME_WORD = 85


def _alike(mine, theirs):
    """
    How much of these two names is the same, counted in words rather than letters.

    Letters are the wrong unit. "PBS Philadelphia" and "CBS Philadelphia" share all but
    one of them and are two stations; "PBS Philadelphia" and "PBS WHYY Philadelphia" share
    every word there is and are one, while being only forty per cent alike letter by
    letter. So each word is paired with the nearest one on the other side -- near enough
    for a spelling, not for a different name -- and the score is how much of both names
    those pairs account for.
    """
    from rapidfuzz import fuzz

    if not mine or not theirs:
        return 0
    # The same letters, parted differently. A playlist writes "DREAMWORKS" and a guide
    # writes "DreamWorks": one comes apart at the camel and the other cannot, and word by
    # word they then share nothing at all.
    if "".join(mine) == "".join(theirs):
        return 100
    taken, shared = set(), 0
    for word in mine:
        best, at = 0, None
        for index, other in enumerate(theirs):
            if index in taken:
                continue
            how = fuzz.ratio(word, other)
            if how > best:
                best, at = how, index
        if at is not None and best >= SAME_WORD:
            taken.add(at)
            shared += 1
    return round(200 * shared / (len(mine) + len(theirs)))


# Four letters beginning with K or W that are a word and not a station. A call sign is
# taken as proof of which station a name is, so anything read as one that is not is a
# channel matched to somebody else's guide -- "┃BE┃ NGC WILD" was matched at a hundred
# per cent to a Slovak Nat Geo Wild because both names say "wild".
NOT_A_CALL_SIGN = {
    "kids", "kids", "kino", "kult", "kunst", "kanal", "kanaal", "kino",
    "west", "wild", "wind", "wine", "work", "wall", "west", "welt", "weer", "week",
    "wonen", "waar", "wire", "wave", "wish", "king", "kick",
}
# Where call signs are allocated. They are a North American way of naming a station, so
# four letters on a Belgian or German channel are four letters and nothing more. Written
# whichever way the box writes them, since a box saying "USA" is the commonest of all
# and "us" alone would have left every American channel without its call sign.
CALL_SIGN_COUNTRIES = {"", "us", "ca", "mx", "pr"}


def _shift_of(words):
    """
    How many hours this name says it is shifted by, as a string, or "".

    "ITV2 +1" is not ITV2: it is the same programmes an hour later, and a guide for one is
    wrong for the other by exactly an hour. Written "+1" (which guide_words has turned into
    "plus 1" by now), and sometimes "+24" or "+2".
    """
    for at, word in enumerate(words):
        if word == "plus" and at + 1 < len(words) and words[at + 1].isdigit():
            return words[at + 1]
    return ""


def _is_frequency(words, at):
    """
    Whether the number at this place is a radio frequency rather than a channel number.

    "CNN 101.5 FM" is one station, not channel 101. A frequency is two numbers the dot
    between them has parted, with FM or AM beside them.
    """
    if at + 1 < len(words) and words[at + 1].isdigit():
        if any(w in ("fm", "am") for w in words[at:at + 4]):
            return True
    if at and words[at - 1].isdigit() and any(w in ("fm", "am") for w in words[at - 1:at + 3]):
        return True
    return False


def _identity_of(words, country="", known_calls=None):
    """
    What in these words picks one channel out from the others of its name: the number it
    carries, which side of the country it is for, its call sign, and how far it is shifted.

    A call sign is the American way of naming a station -- WNET, KQED -- and two of them
    are never the same station. Only taken as one where it can be one: in a country that
    allocates them, four letters beginning with K or W, and not a word.
    """
    shift = _shift_of(words)
    number = next(
        (
            w for at, w in enumerate(words)
            if w.isdigit() and not _is_frequency(words, at)
            # The hours of a time shift are not the channel's number
            and not (shift and w == shift and at and words[at - 1] == "plus")
        ),
        "",
    )
    side = next((w for w in words if w in SIDE_WORDS), "")
    call = ""
    if _one_country(country) in CALL_SIGN_COUNTRIES:
        if known_calls is None:
            # Nobody has read the guides yet, so the shape of the word is all there is
            call = next(
                (
                    w for w in words
                    if len(w) == 4 and w[0] in "kw" and w.isalpha() and w not in NOT_A_CALL_SIGN
                ),
                "",
            )
        else:
            # A word is a call sign when some guide in this install carries it as one, and
            # not because it happens to be four letters beginning with W. See
            # known_channels.call_signs_in: it is the FCC's list, narrowed to the stations
            # anybody here could be watching, and it never goes stale.
            call = next((w for w in words if w in known_calls), "")
    return {"number": number, "side": side, "call": call, "shift": shift}


def _country_of_guide(entry):
    """
    Which country a guide entry is for, as its two letters, or "".

    A tvg-id carries it on the end -- "dreamworks.nl", "BBCOne.uk" -- which is how XMLTV
    files are written; failing that the name may say it in a box, as a playlist's does.
    """
    found = re.findall(r"\.([A-Za-z]{2})(?![A-Za-z])", entry.get("tvg_id") or "")
    if found:
        return found[-1].lower()
    return logo_library.country_of(entry.get("name") or "")


# What a match is, rather than only how alike two names look. Taken from how the
# epgmatcharr plugin reports its work: a name that happens to read alike is not the same
# kind of thing as an id that agrees, and calling both of them "96%" is what made a
# completely different channel look like a certainty.
CERTAIN, LIKELY, GUESS = "certain", "likely", "guess"
# How alike the names have to be before a match is more than a guess, once nothing
# contradicts and the country agrees
LIKELY_SCORE = 80
# How alike the names have to read before a tvg-id that agrees is taken as the thing
# itself rather than as strong evidence
TVG_NEEDS_NAME = 55


def judge_guide(name, country, entry, tvg_id="", known_calls=None, reference=None):
    """
    How good a match this guide is for this channel, and what kind of match it is.

    Returns (score out of a hundred, one of CERTAIN/LIKELY/GUESS, why in a few words).

    Three things decide it, in this order:

    1. **A contradiction ends it, whatever the ids say.** If both names carry a number and
       the numbers differ, they are not the same channel however alike the rest reads --
       "PBS 12" and "PBS 13" are two stations, and on the letters alone they score
       eighty-three. The same for the side of the country ("PBS East"/"PBS West") and for
       call signs, which are never shared. This is what was wrong: the words that tell two
       channels apart carry the least weight in a comparison of letters, and one of them
       was being deleted before the comparison even happened.
    2. **A tvg-id that agrees is strong, and it is not proof.** A provider writes it, and
       providers write it wrongly: they hand one id to channels that are not the same, and
       they leave it behind when a channel is renamed or sold. This fork has been bitten
       by that already -- a Krone stream carrying Euronews' id, every CBS station sharing
       one -- which is why matching on tvg-id is off by default in the Channel Manager.
       So the id agreeing with a name that reads alike is a certainty; the id agreeing
       with a name that reads nothing like it is worth offering and saying so, because it
       is as likely to be a channel that was renamed as a provider that is wrong, and the
       only thing that settles it is what is on the guide now.
    3. **Otherwise it is how alike the names are**, with the country counting (see
       _by_country), and it is only better than a guess when the names are close, the
       country agrees, and nothing at all contradicts.
    """
    theirs = f"{entry.get('name') or ''}"
    mine_words, their_words = guide_words(name), guide_words(theirs)
    if not mine_words or not their_words:
        return 0, GUESS, "nothing to compare"

    their_tvg = (entry.get("original_tvg_id") or entry.get("tvg_id") or "").strip().lower()
    same_id = bool(tvg_id and their_tvg and tvg_id.strip().lower() == their_tvg)

    # Two broadcasters' names with nothing in common: a different channel, whatever the
    # rest of the letters do. Only where both say one -- "PBS" against "Public
    # Broadcasting Service" says nothing either way.
    my_names, their_names = _names_in(mine_words), _names_in(their_words)
    if my_names and their_names and not (my_names & their_names):
        said = f"{sorted(my_names)[0].upper()} is not {sorted(their_names)[0].upper()}"
        return 0, GUESS, f"{said} (whatever its tvg-id says)" if same_id else said

    # Each side's identity read in its own country, since a call sign is only a call
    # sign where call signs are allocated
    their_country = _country_of_guide(entry)
    mine = _identity_of(mine_words, country, known_calls)
    theirs_id = _identity_of(their_words, their_country, known_calls)

    for what, said in (("number", "a different number"), ("side", "the other side of the country"),
                       ("call", "another station's call sign"),
                       ("shift", "a different time shift")):
        if mine[what] and theirs_id[what] and mine[what] != theirs_id[what]:
            # Even with the id agreeing: a provider writing one id on two stations is the
            # commoner mistake by far, and it is the mistake this fork has already made
            return 0, GUESS, f"{said} (whatever its tvg-id says)" if same_id else said

    # What somebody who is not a provider says these two names are. Two names that are one
    # channel in the reference are one channel however little they read alike, and two
    # that are different channels are different however much they do -- which is worth
    # more than any amount of comparing letters, and is not a table anybody here wrote.
    #
    # Asked after the contradictions and not before: a reference that has ITV2 and its
    # +1 under one entry would otherwise hand the one guide to both, and a rule that can
    # be overruled by a download is not a rule.
    # One of them says a number, a side, a call sign or a time shift and the other says
    # nothing at all. It may well be the same channel written shorter, and it may be its
    # sibling: either way it is not something to be sure about.
    half_said = any(
        bool(mine[w]) != bool(theirs_id[w]) for w in ("number", "side", "call", "shift")
    )
    if reference and not half_said:
        ours = known_channels.which_channel(name, reference)
        theirs_known = known_channels.which_channel(theirs, reference)
        if ours and theirs_known:
            if ours["id"] != theirs_known["id"]:
                return 0, GUESS, f"{ours['name']} is not {theirs_known['name']}"
            by_country = _by_country(country, entry)
            return (
                max(0, min(100, 100 + min(0, by_country))),
                CERTAIN if by_country >= 0 else LIKELY,
                f"both names are {ours['name']}",
            )

    alike = _alike(mine_words, their_words)
    # A call sign is a station's own name, allocated to it and to nothing else, so two
    # names carrying the same one are the same station however little else they share:
    # "PBS WHYY" and "WHYY-DT" have one word in common out of three. Taken from the EPG
    # Janitor plugin, which anchors a match on the call sign and rejects a disagreement --
    # the rejecting half of that was already here.
    # What the country says, counted the same way whatever else anchors the match. It
    # used to be asked only about the tier, so a match anchored on a call sign or a
    # tvg-id came out at a hundred per cent with the country flatly disagreeing -- which
    # is how a Belgian channel was offered a Slovak guide as the better of the two.
    by_country = _by_country(country, entry)
    elsewhere = by_country < 0
    # Both halves of the disagreement, named. It used to say only "that guide is US's",
    # which tells you nothing about why that is a disagreement: without what the channel
    # itself says, there is no way to tell a channel marked wrong from a guide from the
    # wrong place, and those want opposite things doing about them.
    two_countries = (
        f"this channel says {_one_country(country).upper()} and the guide is for "
        f"{_one_country(their_country).upper()}"
    )
    if mine["call"] and mine["call"] == theirs_id["call"]:
        anchored = max(0, min(100, int(round(max(alike, 90) + by_country))))
        if elsewhere:
            # Two stations of one call sign in two countries are two stations. Still on
            # the list to be taken by hand, since a tvg-id's country is the provider's
            # word and not gospel; never put forward as a change to make.
            return anchored, GUESS, (
                f"its call sign, {mine['call'].upper()}, but {two_countries}"
            )
        return anchored, CERTAIN, f"its call sign, {mine['call'].upper()}"
    if same_id:
        if alike >= TVG_NEEDS_NAME:
            if elsewhere:
                return max(0, min(100, 100 + by_country)), LIKELY, (
                    f"its tvg-id and its name, but {two_countries}"
                )
            return 100, CERTAIN, "its tvg-id and its name"
        # The id says yes and the name says nothing of the kind. That is a channel that
        # was renamed as often as it is a provider with the wrong id, and only what is on
        # the guide now tells you which
        return (
            max(0, min(100, int(round(max(alike, 80) + by_country)))),
            GUESS if elsewhere else LIKELY,
            "its tvg-id, though the names do not read alike"
            + (f", and {two_countries}" if elsewhere else ""),
        )

    score = max(0, min(100, int(round(alike + by_country))))

    agrees = not elsewhere
    # ...and the same where one says which of the family it is and the other does not
    my_family = {w for w in mine_words if w in FAMILY_WORDS}
    their_family = {w for w in their_words if w in FAMILY_WORDS}
    half_said = half_said or my_family != their_family

    if "".join(mine_words) == "".join(their_words) and agrees:
        return max(score, 100 if not half_said else score), CERTAIN, "its name exactly"
    if score >= LIKELY_SCORE and agrees and not half_said:
        return score, LIKELY, "its name, and the country agrees"
    # The plain name path: where the country is what stopped it being more than a guess,
    # say so, since "how the names read" sounds like the names were the problem
    if elsewhere:
        return score, GUESS, f"how the names read, but {two_countries}"
    return score, GUESS, "how the names read"


def _by_country(country, entry):
    """
    What to add to a guide's score for the country it is for, given the channel's.

    Nothing either way unless both say which country they are: a guide that names none
    may well be the right one, and is left to be judged on its name alone.
    """
    if not country:
        return 0
    theirs = _country_of_guide(entry)
    if not theirs:
        return 0
    if _one_country(country) == _one_country(theirs):
        return SAME_COUNTRY
    return -OTHER_COUNTRY


def _guide_entry(epg_id, tvg_id, name, source, how, score=None):
    entry = {
        "id": epg_id,
        "tvg_id": tvg_id or "",
        "name": name or "",
        "source": source or "",
        "how": how,
    }
    if score is not None:
        # A score is a percentage to read, so it stays between nothing and everything:
        # the region bonus can push the raw one past a hundred or under zero
        entry["score"] = max(0, min(100, int(round(score))))
    return entry


def _what_they_carry(entries):
    """
    What each guide on the list actually holds: the programme on it at this moment, how
    many it has altogether, and whether any channel is using it.

    This is what settles the choice. Two entries called "ORF 1" from two sources look the
    same in any list of names; the one showing Zeit im Bild is the Austrian one.

    Holding nothing means two different things, and saying the wrong one is worse than
    saying nothing. Dispatcharr only reads a guide's programmes once something is using
    it -- a refresh takes every source's channel list, but the programmes only for the
    entries assigned to a channel (`apps/channels/signals.py`, on assignment). So an entry
    no channel uses and that holds nothing has almost certainly never been read, not been
    read and found empty. `in_use` is what tells them apart, and load_programmes is how
    one is read without having to assign it first.

    Three queries for the whole list, all on indexes that are already there.
    """
    from django.db.models import Count
    from django.utils import timezone

    from apps.epg.models import ProgramData

    from .models import Channel

    ids = [entry["id"] for entry in entries]
    if not ids:
        return entries
    counts = dict(
        ProgramData.objects.filter(epg_id__in=ids)
        .values_list("epg_id")
        .annotate(held=Count("id"))
        .values_list("epg_id", "held")
    )
    moment = timezone.now()
    playing = dict(
        ProgramData.objects.filter(epg_id__in=ids, start_time__lte=moment, end_time__gt=moment)
        .values_list("epg_id", "title")
    )
    used = set(
        Channel.objects.filter(epg_data_id__in=ids).values_list("epg_data_id", flat=True)
    )
    # What a read of each of these came back with, so a guide somebody has already looked
    # at and found empty is not offered as one nobody has read yet
    was_read = reads()
    for entry in entries:
        entry["programmes"] = counts.get(entry["id"], 0)
        entry["now"] = playing.get(entry["id"], "")
        entry["in_use"] = entry["id"] in used
        entry["read"] = was_read.get(str(entry["id"])) or None
    return entries


# How the reading of guides says where it has got to. Reading is a pass of each source's
# whole file, which on a big guide is minutes, and a button that only spins is
# indistinguishable from one that has jammed.
READING_KEY = "guide-read:run"
READING_KEPT_SECONDS = 6 * 3600


def reading_state(redis_client=None):
    """How the reading of guides is going, for whichever page asked for it."""
    if redis_client is None:
        redis_client = _reading_redis()
    if not redis_client:
        return {}
    raw = redis_client.hgetall(READING_KEY) or {}
    state = {}
    for key, value in raw.items():
        key = key.decode() if isinstance(key, bytes) else key
        value = value.decode() if isinstance(value, bytes) else value
        state[key] = value
    for number in ("done", "total"):
        if number in state:
            try:
                state[number] = int(state[number])
            except (TypeError, ValueError):
                state[number] = 0
    state["reading"] = state.get("state") == "reading"
    return state


def _reading_redis():
    from core.utils import RedisClient

    try:
        return RedisClient.get_client()
    except Exception as e:
        logger.warning(f"Guides: no Redis to say how the reading is going ({e})")
        return None


def say_reading(mapping, redis_client=None):
    """Put down where the reading has got to; quietly does nothing without Redis."""
    if redis_client is None:
        redis_client = _reading_redis()
    if not redis_client:
        return
    try:
        redis_client.hset(READING_KEY, mapping={k: str(v) for k, v in mapping.items()})
        redis_client.expire(READING_KEY, READING_KEPT_SECONDS)
    except Exception as e:
        logger.debug(f"Guides: could not say how the reading is going ({e})")


# What a read of a guide found, so the page can tell "nobody has looked" from "somebody
# looked and there was nothing there". A guide can be listed in a source's channel section
# and have not one programme in its programme section, which is common and looks exactly
# like a read that failed.
READS_KEY = "guide-reads"


def reads():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=READS_KEY).first()
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def note_read(found, why=""):
    """
    Write down what a read of each of these guides came back with: {epg id: how many}.

    `why` is what stopped it, where something did -- a source in the middle of a refresh,
    a file that is not there. A guide that could not be read is not a guide with nothing
    in it, and saying the second when the first is true is how somebody comes to believe
    a working source is empty.
    """
    from django.utils import timezone

    from core.models import CoreSettings

    kept = reads()
    at = timezone.now().isoformat(timespec="seconds")
    for epg_id, how_many in (found or {}).items():
        kept[str(epg_id)] = {"at": at, "found": int(how_many or 0), "why": why}
    CoreSettings.objects.update_or_create(
        key=READS_KEY, defaults={"name": "Guides read", "value": kept}
    )
    return kept


def load_programmes(epg_ids):
    """
    Read these guides' programmes now, without having to put them on a channel first.

    Dispatcharr reads a guide's programmes when it is assigned to a channel, and not
    before -- so the entries nobody has chosen hold nothing, which is exactly the state
    they are in while someone is trying to choose between them.

    **Reading one costs the whole file.** Dispatcharr's own task streams the source's
    XMLTV from beginning to end and keeps the programmes whose `channel` is the one
    tvg_id it was asked for (`parse_programs_for_tvg_id`). So reading one entry is as
    expensive as reading the file, and reading a window's worth one at a time would read
    the same file a dozen times over. That is why they are read together: the fork's own
    task goes through each source's file **once** for every entry wanted from it, using
    Dispatcharr's own helpers for everything inside a programme so the rows come out the
    same as its own parse would make them.

    Anything that is not a plain XMLTV file -- Schedules Direct, which is fetched rather
    than parsed -- goes to Dispatcharr's task per entry instead, which knows how. A dummy
    source is refused: it makes its programmes up as they are asked for.

    `force` matters. That task returns without doing anything for a guide no channel
    uses, which is every guide this is for; asking without it is how the button came to
    do nothing at all.
    """
    from apps.epg.models import EPGData

    wanted = []
    for epg_id in epg_ids if isinstance(epg_ids, (list, tuple, set)) else [epg_ids]:
        try:
            wanted.append(int(epg_id))
        except (TypeError, ValueError):
            continue
    entries = list(
        EPGData.objects.filter(id__in=wanted)
        .values("id", "tvg_id", "epg_source_id", "epg_source__source_type")
    )
    if not entries:
        return {"error": "Those guides are gone", "reading": 0}

    from apps.epg.tasks import parse_programs_for_tvg_id

    reading = 0
    by_source = {}
    for entry in entries:
        kind = entry["epg_source__source_type"]
        if kind == "dummy" or not entry["epg_source_id"]:
            continue
        if kind == "xmltv" and (entry["tvg_id"] or "").strip():
            by_source.setdefault(entry["epg_source_id"], []).append(entry["id"])
        else:
            # Not a file to go through: Dispatcharr's own task knows how to get it
            parse_programs_for_tvg_id.delay(entry["id"], force=True)
        reading += 1

    if reading:
        from django.utils import timezone

        say_reading({
            "state": "reading",
            "stage": "asking for them",
            "at": "",
            "done": 0,
            "total": reading,
            "since": timezone.now().isoformat(timespec="seconds"),
        })
    if by_source:
        from .tasks import read_guide_programmes

        read_guide_programmes.delay(
            {str(source): ids for source, ids in by_source.items()}
        )
    logger.info(f"Channel Manager: reading the programmes of {reading} guide(s), to choose by")
    return {"queued": reading > 0, "reading": reading}


# Which guides are matched against at all, and on what terms. Kept in one place because
# the Guides tab's runs and the window on a Lineup row have to agree: a guide the Guides
# tab has been told to leave out that the window still offers is worse than either.
#
# Nothing here changes what is on a channel now. A source switched off here is still read,
# still refreshed and still used by every channel already on it -- it is only left out of
# the matching, which is the question "what should this channel be on?" and nobody else's.
MATCHING_KEY = "guide-matching"
MATCHING_DEFAULTS = {
    # EPG source ids to match against. Empty is every active source, which is stock.
    "sources": [],
    # The guide's tvg-id has to carry this. Plain text, unless it has a * or a ? in it,
    # and then it is a pattern: ".uk" finds every British id, "sky*.uk" the Sky ones.
    "tvg_id_like": "",
    # A guide whose country disagrees with the channel's is not offered at all, rather
    # than offered as a guess. For a setup whose names all carry a country box and whose
    # guides all carry a country suffix, this is the single biggest thing that can be
    # said; for anyone else it throws away right answers, so it is off.
    "country_must_agree": False,
}


def load_matching():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=MATCHING_KEY).first()
    stored = row.value if row and isinstance(row.value, dict) else {}
    values = dict(MATCHING_DEFAULTS)
    values.update({k: v for k, v in stored.items() if k in MATCHING_DEFAULTS})
    try:
        values["sources"] = [int(s) for s in values["sources"] or ()]
    except (TypeError, ValueError):
        values["sources"] = []
    values["tvg_id_like"] = str(values.get("tvg_id_like") or "").strip()
    values["country_must_agree"] = bool(values.get("country_must_agree"))
    return values


def save_matching(given):
    from core.models import CoreSettings

    values = load_matching()
    values.update({k: v for k, v in (given or {}).items() if k in MATCHING_DEFAULTS})
    try:
        values["sources"] = [int(s) for s in values["sources"] or ()]
    except (TypeError, ValueError):
        raise ValueError("Those are not EPG sources")
    values["tvg_id_like"] = str(values.get("tvg_id_like") or "").strip()
    values["country_must_agree"] = bool(values.get("country_must_agree"))
    CoreSettings.objects.update_or_create(
        key=MATCHING_KEY, defaults={"name": "Guide matching", "value": values}
    )
    return values


def _id_is_like(tvg_id, pattern):
    """
    Whether a guide's tvg-id answers to what was typed.

    Plain text unless there is a * or a ? in it. Somebody typing ".uk" means ids with
    ".uk" in them and should not have to learn a pattern language to say so; somebody
    typing "sky*.uk" plainly does mean a pattern, and gets one.
    """
    if not pattern:
        return True
    tvg_id = (tvg_id or "").strip().lower()
    pattern = pattern.strip().lower()
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(tvg_id, pattern)
    return pattern in tvg_id


def in_play(entry, matching, country=None):
    """
    Whether this guide is one to match against at all, given the settings.

    `entry` is a catalogue row or anything else carrying a name, a tvg-id and a source id.
    """
    sources = matching.get("sources") or []
    if sources and entry.get("epg_source_id") not in sources:
        return False
    if not _id_is_like(entry.get("original_tvg_id") or entry.get("tvg_id"), matching.get("tvg_id_like")):
        return False
    if matching.get("country_must_agree") and country:
        theirs = _country_of_guide(entry)
        if theirs and _one_country(theirs) != _one_country(country):
            return False
    return True


def guides_in_play(catalogue, matching=None, country=None):
    """The rows of a catalogue worth matching against, given the settings."""
    matching = load_matching() if matching is None else matching
    if not (matching.get("sources") or matching.get("tvg_id_like") or
            (matching.get("country_must_agree") and country)):
        return catalogue
    return [row for row in catalogue if in_play(row, matching, country)]


def guide_candidates(name, tvg_id="", search="", limit=12, current=None, source=None):
    """
    The guide entries one channel could be, best first, for the picker on its row.

    `current` is the guide it has or was matched to. It is always on the list, first and
    however the search went, so what the channel is now is always there to go back to --
    and so that it says what it holds like every other entry, which the plan's own summary
    does not know.

    The plan's own matching (see _Guides) is deliberately plain, because it runs over
    every channel at once. When someone opens one row and asks, there is time to do
    better, so this uses Dispatcharr's own matcher: the same fuzzy scoring and country
    preference its "Match EPG" button uses, which knows that "ORF 1" and "ORF1.at" are
    the same channel. Not the ML part of it -- that loads a model, and this has to answer
    while a menu is open.

    The country box in front is taken off first. It is this fork's own way of writing
    names and means nothing to a guide, and left on it drags every score down.

    With `search`, it is a plain search instead: every guide whose name or tvg-id carries
    what was typed, so a channel the matcher cannot see is still there to be chosen.
    """
    from django.db.models import Q

    from apps.epg.models import EPGData, EPGSource

    from . import epg_matching

    active = EPGData.objects.exclude(epg_source__is_active=False)
    # What is being matched against: the settings, narrowed further to one source when
    # the window is being used to try them one at a time
    matching = load_matching()
    if source not in (None, "", 0, "0", "all"):
        try:
            matching = {**matching, "sources": [int(source)]}
        except (TypeError, ValueError):
            pass
    if matching["sources"]:
        active = active.filter(epg_source_id__in=matching["sources"])
    try:
        limit = max(1, min(int(limit or 12), 50))
    except (TypeError, ValueError):
        limit = 12

    found = []
    seen = set()
    if current not in (None, "", 0, "0"):
        try:
            held = (
                active.filter(id=int(current))
                .values_list("id", "tvg_id", "name", "epg_source__name")
                .first()
            )
        except (TypeError, ValueError):
            held = None
        if held:
            found.append(_guide_entry(*held, "kept"))
            seen.add(held[0])

    wanted = (search or "").strip()
    if wanted:
        # Every word, anywhere, in any order -- not the phrase as typed. Somebody looking
        # for the Philadelphia PBS station types "pbs philadelphia", and the guide calls
        # it "PBS WHYY Philadelphia": the words are all there and the phrase is not.
        rows = active.exclude(id__in=seen)
        for word in wanted.split():
            rows = rows.filter(Q(name__icontains=word) | Q(tvg_id__icontains=word))
        rows = [
            row for row in rows.order_by("-epg_source__priority", "name")
            .values_list("id", "tvg_id", "name", "epg_source__name")[: limit * 8]
            if _id_is_like(row[1], matching["tvg_id_like"])
        ][: limit * 4]
        # Nearest to what was typed first, so the words being in a shorter name counts
        typed = guide_words(wanted)
        rows.sort(key=lambda row: _alike(typed, guide_words(row[2])), reverse=True)
        return _what_they_carry(
            found + [_guide_entry(*row, "search") for row in rows[:limit]]
        )

    # An exact tvg-id is not a guess: whatever the names look like, it goes first
    if (tvg_id or "").strip():
        exact = (
            active.filter(tvg_id__iexact=tvg_id.strip())
            .order_by("-epg_source__priority", "id")
            .values_list("id", "tvg_id", "name", "epg_source__name")
            .first()
        )
        if exact:
            # Judged like any other, because an id a provider wrote is evidence and not
            # proof: it goes first on the list either way, and says what it is worth
            country = logo_library.country_of(name or "") or ""
            score, tier, why = judge_guide(
                name, country, {"name": exact[2], "tvg_id": exact[1]}, tvg_id,
                known_calls=known_channels.call_signs(), reference=known_channels.known(),
            )
            if score:
                entry = _guide_entry(*exact, "tvg-id", score)
                entry["tier"], entry["why"] = tier, why
                found.append(entry)
                seen.add(exact[0])

    plain = _strip_country_box(name or "")
    normalized = epg_matching.normalize_name(plain)
    if normalized:
        # The country is this fork's own doing, so the scoring for it is too, and the
        # matcher is asked not to apply its own: its one preferred region is for a
        # library where every channel is from the same place, and it reads a ".uk" as a
        # country that is not "gb", which is the wrong answer for half of them.
        country = logo_library.country_of(name or "") or epg_matching.get_preferred_region_code() or ""
        known_calls = known_channels.call_signs()
        reference = known_channels.known()
        # More candidates than will be shown, because one from the right country can sit
        # below a wrongly-scored pile of them and has to be there to be lifted past it
        _, _, candidates, _ = epg_matching.stream_fuzzy_epg_scan(
            normalized, None, candidate_limit=max(limit * 3, 20)
        )
        # The matcher works in source ids; the page shows which source an entry is from
        sources = dict(EPGSource.objects.values_list("id", "name"))
        judged = []
        for _, row in candidates:
            if row["id"] in seen:
                continue
            if not in_play(row, matching, country):
                continue
            score, tier, why = judge_guide(
                name, country, row, tvg_id, known_calls=known_calls, reference=reference
            )
            if score < MIN_GUIDE_SCORE:
                continue
            entry = _guide_entry(
                row["id"], row.get("original_tvg_id") or row.get("tvg_id"), row["name"],
                sources.get(row["epg_source_id"], ""), "name", score,
            )
            entry["tier"], entry["why"] = tier, why
            judged.append((entry["score"], row.get("epg_source_priority") or 0, entry))
        judged.sort(key=lambda one: (one[0], one[1]), reverse=True)
        for _, _, entry in judged:
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            found.append(entry)
    return _what_they_carry(found[:limit])


def _logo_for(name, streams, mode, index):
    if mode in ("keep", "none"):
        return ""
    if mode == "collections" and index:
        found = logo_library.suggestions_for(name, index, limit=1)
        if found:
            return found[0]["url"]
    return next((s["logo_url"] for s in streams if s["logo_url"].startswith("http")), "")


def load_ignored():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=IGNORED_KEY).first()
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _save_ignored(ignored):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(
        key=IGNORED_KEY, defaults={"name": "Channel Manager ignored", "value": ignored}
    )


def ignore(key, name="", kind="", streams=()):
    """Stop suggesting this row: a new channel or conflict whole, a channel's streams only."""
    from datetime import datetime, timezone

    ignored = load_ignored()
    before = set((ignored.get(key) or {}).get("streams") or ())
    ignored[key] = {
        "name": name,
        "kind": kind,
        "streams": sorted(before | {int(i) for i in streams or ()}),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_ignored(ignored)
    return ignored[key]


def unignore(key=None):
    """Suggest it again; without a key, every ignored suggestion."""
    ignored = {} if key is None else {k: v for k, v in load_ignored().items() if k != key}
    _save_ignored(ignored)
    return len(ignored)


def _followed_groups():
    """The stream groups at least one channel takes streams from: the ones in use."""
    from .models import ChannelStream

    return set(
        ChannelStream.objects.filter(stream__is_custom=False, stream__channel_group_id__isnull=False)
        .values_list("stream__channel_group_id", flat=True).distinct()
    )


class _NewHomes:
    """
    Where a new channel would go, and on which number.

    Its group: the one your channels from the same stream group are in -- a provider's
    "AT | AUSTRIA" streams feed your "┃AT┃ AUSTRIA" channels -- or else a group whose channels
    carry the same country box, or else the stream's own group. Its number: the next one
    after the last channel of that group that no channel has, so it lands with its group.
    """

    def __init__(self):
        from collections import Counter

        from .models import Channel, ChannelGroup, ChannelStream

        self.names = dict(ChannelGroup.objects.values_list("id", "name"))
        by_stream_group = {}
        for stream_group, channel_group in ChannelStream.objects.filter(
            stream__is_custom=False, channel__channel_group_id__isnull=False,
        ).values_list("stream__channel_group_id", "channel__channel_group_id"):
            by_stream_group.setdefault(stream_group, Counter())[channel_group] += 1
        self.by_stream_group = {g: c.most_common(1)[0][0] for g, c in by_stream_group.items()}
        by_country = {}
        self.highest_in = {}
        self.taken = set()
        self.highest = 0
        for number, group_id, name in Channel.objects.values_list("channel_number", "channel_group_id", "name"):
            if number is not None:
                self.taken.add(float(number))
                self.highest = max(self.highest, float(number))
                if group_id:
                    self.highest_in[group_id] = max(self.highest_in.get(group_id, 0), float(number))
            country = country_for(name, self.names.get(group_id, ""))
            if country and group_id:
                by_country.setdefault(country, Counter())[group_id] += 1
        self.by_country = {c: counter.most_common(1)[0][0] for c, counter in by_country.items()}

    def group_for(self, stream, country):
        """(group id, why) for a new channel of this stream."""
        if stream["group_id"] in self.by_stream_group:
            return self.by_stream_group[stream["group_id"]], "where your channels from this stream group are"
        if country and country in self.by_country:
            return self.by_country[country], f"where most of your {country} channels are"
        return stream["group_id"], "the stream's own group"

    def number_in(self, group_id):
        """The next number after the group's last channel that nobody has, and keep it."""
        number = float(int(self.highest_in.get(group_id, self.highest)) + 1)
        while number in self.taken:
            number += 1
        self.taken.add(number)
        if group_id:
            self.highest_in[group_id] = max(self.highest_in.get(group_id, 0), number)
        return number


def _usual_fallback():
    """The custom stream most channels end in (a "could not dispatch" screen), or None."""
    from collections import Counter

    from .models import ChannelStream

    last = {}
    for channel_id, stream_id, custom in (
        ChannelStream.objects.order_by("channel_id", "order").values_list("channel_id", "stream_id", "stream__is_custom")
    ):
        last[channel_id] = (stream_id, custom)
    counts = Counter(stream_id for stream_id, custom in last.values() if custom)
    if not counts:
        return None
    stream_id, count = counts.most_common(1)[0]
    # Only when it really is what channels end in, not one channel's odd stream
    return stream_id if count * 2 >= len(last) else None


def _duplicate_sets(by_key, same_country=False):
    """
    The channels that are the same channel as each other, as {key: [records]}.

    Only ones whose countries do not contradict each other. "┃UK┃ BBC ONE" in your news
    group and the same channel in your entertainment group are one channel in two places,
    which is the thing worth combining; "┃AT┃ ORF 1" and "┃DE┃ ORF 1" are two channels
    that share a name, and the whole of this fork is built on telling those apart. A
    channel stating no country joins whichever set it matches, since saying nothing is
    not saying something different.
    """
    sets = {}
    for key, records in by_key.items():
        if len(records) < 2:
            continue
        stated = {r["country"] for r in records if r["country"]}
        if len(stated) > 1:
            # Two countries named: these are different channels, whatever their names say
            for country in stated:
                theirs = [r for r in records if r["country"] in (country, "")]
                if len(theirs) > 1:
                    sets[f"{country}:{key}"] = theirs
            continue
        if same_country and not stated:
            continue
        sets[f"{next(iter(stated), '')}:{key}"] = records
    return sets


def _which_to_keep(records, group_id):
    """
    Which of these channels survives being combined: the one already in the group the
    combined channel is to be in, and of those the one with the lowest number.

    The lowest number because that is the one set up on purpose and the one media servers
    already point at; the group first because the point of combining is to end with one
    channel where it belongs, and a channel already there needs no moving.
    """
    def order(record):
        channel = record["channel"]
        return (
            0 if group_id and channel.channel_group_id == group_id else 1,
            float(channel.channel_number) if channel.channel_number is not None else float("inf"),
            channel.id,
        )

    return sorted(records, key=order)[0]


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
    ignored = load_ignored()
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
        give_all = settings.get("several_matches") != "conflict"
        if settings.get("match_tvg_id") and stream["tvg_id"]:
            record, tied = _pick(
                by_tvg.get(stream["tvg_id"].lower(), []), stream["country"], same_country, give_all
            )
        if record is None and tied is None and stream["key"]:
            record, tied = _pick(by_key.get(stream["key"], []), stream["country"], same_country, give_all)
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

    wants_collections = "collections" in (settings.get("logo"), settings.get("new_logo", "collections"))
    index = logo_library.load_index() if wants_collections else None
    guides = _Guides()
    rows = []

    # ── Channels that are the same channel as each other ──
    # Worked out before the rows, so the ones being folded into another do not also get a
    # row of their own saying what streams they would gain: they are not going to be here.
    combining = {}
    clusters = {}
    if settings.get("combine_duplicates"):
        into = _NewHomes()
        for set_key, records in _duplicate_sets(by_key, bool(settings.get("same_country"))).items():
            if f"combine:{set_key}" in ignored:
                continue
            country = set_key.split(":", 1)[0]
            target = settings.get("target_group")
            if target:
                group_id, why = int(target), "the group chosen in the levers"
            elif country and country in into.by_country:
                group_id, why = into.by_country[country], f"where most of your {country} channels are"
            else:
                # No country to go on: whichever group most of them are in already
                from collections import Counter

                theirs = Counter(
                    r["channel"].channel_group_id for r in records if r["channel"].channel_group_id
                )
                group_id = theirs.most_common(1)[0][0] if theirs else None
                why = "where most of them are already"
            keeper = _which_to_keep(records, group_id)
            clusters[set_key] = {
                "records": records, "keeper": keeper, "group_id": group_id, "why": why,
                "country": country,
            }
            for record in records:
                combining[record["channel"].id] = set_key

    # ── Channels there already are ──
    for channel_id, record in existing.items():
        if channel_id in combining and clusters[combining[channel_id]]["keeper"] is not record:
            # Folded into another channel, and deleted with it: its own row would say what
            # it is about to gain, moments before it stops existing
            continue
        channel = record["channel"]
        # The one kept out of a set of duplicates carries all of their streams
        folding = clusters.get(combining.get(channel_id)) if channel_id in combining else None
        others = [r for r in folding["records"] if r is not record] if folding else []
        held = list(record["stream_ids"])
        for other in others:
            held += [s for s in other["stream_ids"] if s not in held]
        attached_now = [by_id[s] for s in held if s in by_id]
        # A custom stream on a channel is its fallback -- the screen that says the channel
        # could not be played -- and belongs at the end, after every real stream. Anything
        # added goes in before it, or it would never be tried before the fallback.
        custom = [s for s in attached_now if s["custom"]]
        normal = [s for s in attached_now if not s["custom"]]
        # Streams a person said not to suggest for this channel again, either way
        left_alone = set((ignored.get(f"ch:{channel_id}") or {}).get("streams") or ())
        coming = list(additions.get(channel_id, []))
        for other in others:
            coming += [
                s for s in additions.get(other["channel"].id, [])
                if s["id"] not in {one["id"] for one in coming}
            ]
        added = [
            s for s in coming
            if s["id"] not in left_alone and s["id"] not in {one["id"] for one in attached_now}
        ]
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
            wrong = [s for s in wrong if s["id"] not in left_alone]
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

        if folding:
            status = "combine"
            if folding["group_id"] and folding["group_id"] != channel.channel_group_id:
                summary["group_id"] = folding["group_id"]
                summary["group"] = _NewHomes().names.get(folding["group_id"], "")
                changes.append("group")
        else:
            status = "merge" if (added or removed or changes or reordered) else "unchanged"
        rows.append({
            "key": f"combine:{combining[channel_id]}" if folding else f"ch:{channel_id}",
            "status": status,
            # The channels that stop existing when this is applied, named before it is
            "combining": [_channel_summary(o["channel"]) for o in others] if folding else [],
            "group_why": folding["why"] if folding else "",
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
        if f"conflict:{country}:{key}" in ignored:
            continue
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
        homes = _NewHomes()
        target = settings.get("target_group")
        number = settings.get("number_start")
        number = float(number) if number not in (None, "") else None
        # Only streams from the groups in use, unless every group is asked for or groups
        # were chosen by hand
        followed = None
        if settings.get("new_from", "followed") == "followed" and not settings.get("stream_groups"):
            followed = _followed_groups()
        # The custom stream your channels end in, so a new one ends in it too
        fallback_id = _usual_fallback() if settings.get("new_fallback", True) else None
        fallback = (_attached_rows([fallback_id], settings) or [None])[0] if fallback_id else None

        planned = []
        for (country, key), found in homeless.items():
            if f"new:{country}:{key}" in ignored:
                continue
            if followed is not None:
                found = [s for s in found if s["group_id"] in followed]
            if len(found) < int(settings.get("min_streams_new") or 1) or not found:
                continue
            ordered = _ordered(found, settings)
            if settings.get("drop_sd_when_hd") and any(s["quality_rank"] < 3 for s in ordered):
                ordered = [s for s in ordered if s["quality"] != "SD"]
            best = min(ordered, key=lambda s: (s["priority"], s["quality_rank"]))
            name = best["clean"] if settings.get("keep_country_prefix") else _strip_country_box(best["clean"])
            if target:
                group_id, why = int(target), "the group chosen in the levers"
            else:
                group_id, why = homes.group_for(best, country)
            planned.append((name, country, key, ordered, group_id, why))

        # By group, then name, so numbers follow on within each group
        for name, country, key, ordered, group_id, why in sorted(
            planned, key=lambda p: (homes.names.get(p[4], "").lower(), p[0].lower())
        ):
            epg = guides.find(ordered, _strip_country_box(name), settings.get("epg"))
            logo = _logo_for(name, ordered, settings.get("new_logo", "collections"), index)
            if number is not None:
                channel_number = number
                number += 1
            else:
                channel_number = homes.number_in(group_id)
            streams_after = [_stream_summary(s, added=True) for s in ordered]
            if fallback:
                streams_after.append(_stream_summary(fallback, added=True))
            rows.append({
                "key": f"new:{country}:{key}",
                "status": "new",
                "channel": {
                    "id": None,
                    "name": name,
                    "number": channel_number,
                    "group": homes.names.get(group_id, ""),
                    "group_id": group_id,
                    "group_why": why,
                    "logo_url": logo,
                    "epg": epg,
                },
                "before": {"channel": None, "streams": [_stream_summary(s) for s in ordered]},
                "streams": streams_after,
                "adds": len(ordered),
                "removes": 0,
                "changes": [],
                "country": country,
            })

    _fill_what_is_on(rows)
    order = {"new": 0, "combine": 1, "merge": 2, "conflict": 3, "unchanged": 4}
    rows.sort(key=lambda r: (order[r["status"]], (r["channel"] or {}).get("number") or 0))
    summary = {status: sum(1 for r in rows if r["status"] == status) for status in order}
    summary["streams"] = len(streams)
    summary["streams_added"] = sum(r["adds"] for r in rows)
    summary["ignored"] = len(ignored)
    return {
        "rows": rows,
        "summary": summary,
        "ignored": [
            {"key": k, **v} for k, v in sorted(ignored.items(), key=lambda kv: (kv[1].get("name") or "").lower())
        ],
    }


# ── Applying ─────────────────────────────────────────────────────────────────


def _logo_id(url):
    if not url:
        return None
    from .models import Logo

    logo, _ = Logo.objects.get_or_create(url=url, defaults={"name": url.rsplit("/", 1)[-1][:255]})
    return logo.id


def _in_the_order_given(final_ids, given, custom_ids):
    """
    The streams of a row in the order someone put them in on the page, if that order is
    still of the same streams. Anything else -- a stream gone or come since the page was
    looked at -- and the plan's own order stands. A custom stream stays last either way:
    it is the channel's fallback, and one put before a real stream would be played first.
    """
    try:
        given = [int(i) for i in given or ()]
    except (TypeError, ValueError):
        return final_ids
    if sorted(given) != sorted(final_ids):
        return final_ids
    return [i for i in given if i not in custom_ids] + [i for i in given if i in custom_ids]


def _names_given(names):
    """The names typed on the page, by row: emptied or all spaces is no name at all."""
    out = {}
    for key, name in (names if isinstance(names, dict) else {}).items():
        name = str(name or "").strip()[:255]
        if name:
            out[key] = name
    return out


def _guides_given(epgs):
    """
    The guides chosen on the page, by row: an id to use, or None for "no guide" -- which
    is a choice of its own, and how a guide matched wrongly is taken off again.
    """
    from apps.epg.models import EPGData

    out = {}
    for key, epg_id in (epgs if isinstance(epgs, dict) else {}).items():
        if epg_id in (None, "", 0, "0", "none"):
            out[key] = None
            continue
        try:
            out[key] = int(epg_id)
        except (TypeError, ValueError):
            continue
    wanted = [i for i in out.values() if i is not None]
    if wanted:
        # A guide deleted since the page was looked at is not applied to anything
        known = {
            entry["id"]: entry
            for entry in EPGData.objects.filter(id__in=wanted).values("id", "tvg_id")
        }
        out = {k: v for k, v in out.items() if v is None or v in known}
        return out, known
    return out, {}


def apply_plan(settings, keys, orders=None, groups=None, drops=None, names=None, epgs=None):
    """
    Carry out the chosen rows of the plan, worked out again now rather than trusted from the
    page: if the streams have changed since it was looked at, what is applied is what is
    true now, not what was true then. Conflicts are never applied.

    orders is {row key: [stream ids]} for rows whose streams were put in another order on
    the page; a channel with nothing else to change is applied for its order alone. groups
    is {row key: channel group id} for new channels put in another group than suggested,
    which are then numbered in that group. drops is {row key: [stream ids]}: streams taken
    out of a row on the page -- not added, or taken off a channel that has them. A custom
    fallback is never dropped.

    names is {row key: name} and epgs is {row key: guide id, or None for none}: the name
    and the guide as they were set by hand on the row. They name a channel being made, or
    rename and re-guide one that is already there -- the only two things this changes about
    a channel you have beyond its streams, and only ever on a row that was ticked.
    """
    from django.db import transaction

    from .models import Channel, ChannelProfile, ChannelProfileMembership, ChannelStream

    wanted = set(keys or ())
    orders = orders if isinstance(orders, dict) else {}
    groups = groups if isinstance(groups, dict) else {}
    drops = {k: {int(i) for i in v} for k, v in (drops if isinstance(drops, dict) else {}).items()}
    names = _names_given(names)
    epgs, guides_known = _guides_given(epgs)
    plan = build_plan(settings)
    homes = _NewHomes() if groups else None
    if homes:
        # The numbers the plan gave its new channels are spoken for, whichever get made
        homes.taken.update(float(r["channel"]["number"]) for r in plan["rows"] if r["status"] == "new")
    rows = [
        r for r in plan["rows"]
        if r["key"] in wanted
        and (r["status"] in ("new", "merge", "combine") or (
            r["status"] == "unchanged"
            and (r["key"] in orders or r["key"] in drops or r["key"] in names or r["key"] in epgs)
        ))
    ]
    created = updated = streams_added = combined = 0

    with transaction.atomic():
        for row in rows:
            dropped = {
                s["id"] for s in row["streams"] if s["id"] in drops.get(row["key"], ()) and not s.get("custom")
            }
            final_ids = [s["id"] for s in row["streams"] if not s["removed"] and s["id"] not in dropped]
            if row["key"] in orders:
                custom_ids = {s["id"] for s in row["streams"] if s.get("custom")}
                final_ids = _in_the_order_given(final_ids, orders[row["key"]], custom_ids)
            if not final_ids:
                # Never leaves a channel with nothing to play
                continue
            info = row["channel"]
            chosen_name = names.get(row["key"])
            if row["status"] == "combine":
                chosen_group = groups.get(row["key"])
                if chosen_group:
                    info = {**info, "group_id": int(chosen_group)}
            if row["key"] in epgs:
                epg_id = epgs[row["key"]]
                info = {**info, "epg": {
                    "id": epg_id,
                    "tvg_id": (guides_known.get(epg_id) or {}).get("tvg_id") or "",
                } if epg_id else None}
            if row["status"] == "new":
                chosen_group = groups.get(row["key"])
                if chosen_group and int(chosen_group) != info.get("group_id"):
                    info = {**info, "group_id": int(chosen_group), "number": homes.number_in(int(chosen_group))}
                channel = Channel.objects.create(
                    name=(chosen_name or info["name"])[:255],
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
                if row["status"] == "combine" and info.get("group_id") != channel.channel_group_id:
                    channel.channel_group_id = info.get("group_id")
                    fields.append("channel_group")
                if chosen_name and chosen_name != channel.name:
                    channel.name = chosen_name
                    fields.append("name")
                if row["key"] in epgs:
                    # Chosen by hand, which includes choosing no guide at all
                    epg = info.get("epg")
                    channel.epg_data_id = epg["id"] if epg else None
                    fields.append("epg_data")
                    if epg and epg.get("tvg_id"):
                        channel.tvg_id = epg["tvg_id"]
                        fields.append("tvg_id")
                elif "epg" in row["changes"] and info.get("epg"):
                    channel.epg_data_id = info["epg"]["id"]
                    fields.append("epg_data")
                if "logo" in row["changes"] and info.get("logo_url"):
                    channel.logo_id = _logo_id(info["logo_url"])
                    fields.append("logo")
                if fields:
                    channel.save(update_fields=fields)
                updated += 1

            # Only what the plan names as removed goes; anything it did not look at stays
            removed_ids = [s["id"] for s in row["streams"] if s["removed"] or s["id"] in dropped]
            if removed_ids:
                ChannelStream.objects.filter(channel=channel, stream_id__in=removed_ids).delete()
            present = set(ChannelStream.objects.filter(channel=channel).values_list("stream_id", flat=True))
            for order, stream_id in enumerate(final_ids):
                if stream_id in present:
                    ChannelStream.objects.filter(channel=channel, stream_id=stream_id).update(order=order)
                else:
                    ChannelStream.objects.create(channel=channel, stream_id=stream_id, order=order)
            streams_added += row["adds"]

            if row["status"] == "combine":
                # Their streams are on this channel now, so the channels themselves go.
                # Only the ones the plan named, worked out again a moment ago -- never a
                # channel that has come along since the page was looked at.
                going = [
                    one["id"] for one in row.get("combining") or ()
                    if one.get("id") and one["id"] != channel.id
                ]
                if going:
                    Channel.objects.filter(id__in=going).delete()
                    combined += len(going)

    logger.info(
        f"Channel Manager: {created} channel(s) made, {updated} merged, "
        f"{streams_added} stream(s) added, {combined} duplicate channel(s) deleted"
    )
    return {
        "created": created, "updated": updated,
        "streams_added": streams_added, "combined": combined,
    }
