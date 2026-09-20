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

import logging
import re

from . import logo_library

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
    # Off by default, as DispatcharrUtils has no such thing: providers give one tvg-id to
    # many channels -- every CBS station, an East and a West feed, and on one real setup a
    # Krone stream carrying Euronews' -- and trusting it merged them all
    "match_tvg_id": False,
    # Words that are about the stream, not the channel, taken off before matching. The two
    # the group merge people ran by hand took off; anything more is theirs to add.
    "ignore_tags": "VIP, RAW",
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
DEFAULTS_VERSION = 3
# What changed in each version, and so what a set saved before it takes from the defaults;
# the rest of what was saved is kept. Version 3: new channels are suggested by default.
CHANGED_IN = {3: ("create_new",)}
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
        # A tag in brackets is taken as written; a bare word only as a whole word
        if tag[:1] in "[(":
            text = re.sub(re.escape(tag), " ", text, flags=re.IGNORECASE)
        else:
            text = _word_pattern([tag]).sub(" ", text)
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
# "ORF 1" against "ORF Eins" scores 62 and belongs in the list; against "Sender Eins" it
# scores 25 and does not. Anything under it is what the search is for.
MIN_GUIDE_SCORE = 40


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
    for entry in entries:
        entry["programmes"] = counts.get(entry["id"], 0)
        entry["now"] = playing.get(entry["id"], "")
        entry["in_use"] = entry["id"] in used
    return entries


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

    if by_source:
        from .tasks import read_guide_programmes

        read_guide_programmes.delay(
            {str(source): ids for source, ids in by_source.items()}
        )
    logger.info(f"Channel Manager: reading the programmes of {reading} guide(s), to choose by")
    return {"queued": reading > 0, "reading": reading}


def guide_candidates(name, tvg_id="", search="", limit=12, current=None):
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
        rows = (
            active.filter(Q(name__icontains=wanted) | Q(tvg_id__icontains=wanted))
            .exclude(id__in=seen)
            .order_by("-epg_source__priority", "name")
            .values_list("id", "tvg_id", "name", "epg_source__name")[:limit]
        )
        return _what_they_carry(found + [_guide_entry(*row, "search") for row in rows])

    # An exact tvg-id is not a guess: whatever the names look like, it goes first
    if (tvg_id or "").strip():
        exact = (
            active.filter(tvg_id__iexact=tvg_id.strip())
            .order_by("-epg_source__priority", "id")
            .values_list("id", "tvg_id", "name", "epg_source__name")
            .first()
        )
        if exact:
            found.append(_guide_entry(*exact, "tvg-id", 100))
            seen.add(exact[0])

    plain = _strip_country_box(name or "")
    normalized = epg_matching.normalize_name(plain)
    if normalized:
        _, _, candidates, _ = epg_matching.stream_fuzzy_epg_scan(
            normalized, epg_matching.get_preferred_region_code(), candidate_limit=limit + len(found)
        )
        # The matcher works in source ids; the page shows which source an entry is from
        sources = dict(EPGSource.objects.values_list("id", "name"))
        for score, row in candidates:
            if row["id"] in seen or score < MIN_GUIDE_SCORE:
                continue
            seen.add(row["id"])
            found.append(_guide_entry(
                row["id"], row.get("original_tvg_id") or row.get("tvg_id"), row["name"],
                sources.get(row["epg_source_id"], ""), "name", score,
            ))
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

    # ── Channels there already are ──
    for channel_id, record in existing.items():
        channel = record["channel"]
        attached_now = [by_id[s] for s in record["stream_ids"] if s in by_id]
        # A custom stream on a channel is its fallback -- the screen that says the channel
        # could not be played -- and belongs at the end, after every real stream. Anything
        # added goes in before it, or it would never be tried before the fallback.
        custom = [s for s in attached_now if s["custom"]]
        normal = [s for s in attached_now if not s["custom"]]
        # Streams a person said not to suggest for this channel again, either way
        left_alone = set((ignored.get(f"ch:{channel_id}") or {}).get("streams") or ())
        added = [s for s in additions.get(channel_id, []) if s["id"] not in left_alone]
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

    order = {"new": 0, "merge": 1, "conflict": 2, "unchanged": 3}
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
        and (r["status"] in ("new", "merge") or (
            r["status"] == "unchanged"
            and (r["key"] in orders or r["key"] in drops or r["key"] in names or r["key"] in epgs)
        ))
    ]
    created = updated = streams_added = 0

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

    logger.info(
        f"Channel Manager: {created} channel(s) made, {updated} merged, "
        f"{streams_added} stream(s) added"
    )
    return {"created": created, "updated": updated, "streams_added": streams_added}
