"""
Which provider stream is which of your channels, remembered.

A stream is on a channel because somebody put it there -- by hand, with the Lineup, or
with a tool before this one -- and that is the best evidence there is of which channel it
is. It used to live only in the link itself, so it lasted exactly as long as the stream did.
With the stream hash built from its name, a provider renaming "BE| VRT 1 HD" to "BE| VRT 1
FHD" makes a new stream, the old one goes stale and is deleted, and the channel loses it:
the Lineup then has to find it again from the names alone, which is the part that guesses.

Here each link is written down with what identifies the stream at its provider and
survives a rename: the provider's own stream number (`stream_id`, which Xtream Codes
providers keep), its tvg-id and its name. After a playlist refresh, a remembered stream that
went stale or is gone is looked for again at the same provider, and the one found goes
back where the old one was.

What was taken off on purpose stays off. A stream that is still there, still listed by its
provider and no longer on its channel was taken off by somebody, and is forgotten -- unless
Stream Check parked it, which puts it back itself.

Kept in a CoreSettings row, as everything else of the fork's is (no migrations):
{"channels": {channel id: [{"stream", "account", "sid", "name", "tvg", "how", "at",
"lost_at"?}]}, "saved_at": iso}.
"""

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from .settings_rows import change_row

logger = logging.getLogger(__name__)

PAIRINGS_KEY = "channel-pairings"
PAIRINGS_NAME = "Channel Manager remembered streams"
# A remembered stream whose provider stopped listing it is looked for again after every
# refresh for this long, then forgotten: a channel that ended does not come back
LOST_KEPT_DAYS = 60


def enabled():
    from .channel_manager import load_settings

    return bool(load_settings().get("remember_pairings", True))


def load():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=PAIRINGS_KEY).first()
    value = row.value if row and isinstance(row.value, dict) else {}
    return {"channels": dict(value.get("channels") or {}), "saved_at": value.get("saved_at")}


def summary():
    """For the settings: how much is remembered, and since when."""
    record = load()
    entries = [e for es in record["channels"].values() for e in es]
    return {
        "enabled": enabled(),
        "channels": sum(1 for es in record["channels"].values() if es),
        "streams": len(entries),
        "lost": sum(1 for e in entries if e.get("lost_at")),
        "saved_at": record["saved_at"],
    }


def _now():
    return timezone.now().isoformat(timespec="seconds")


def _entry(stream, how, at):
    return {
        "stream": stream.id,
        "account": stream.m3u_account_id,
        "sid": stream.stream_id,
        "name": stream.name or "",
        "tvg": stream.tvg_id or "",
        "how": how,
        "at": at,
    }


def _links(channel_ids=None):
    """{channel id: [stream, ...]} of the provider streams on channels, in their order."""
    from .models import ChannelStream

    links = (
        ChannelStream.objects.filter(stream__is_custom=False, stream__m3u_account__isnull=False)
        .select_related("stream")
        .order_by("channel_id", "order")
    )
    if channel_ids is not None:
        links = links.filter(channel_id__in=list(channel_ids))
    on = {}
    for link in links:
        on.setdefault(link.channel_id, []).append(link.stream)
    return on


def _parked():
    try:
        from .stream_check import parked_ids

        return parked_ids()
    except Exception:
        return set()


def sync(how="kept", channel_ids=None):
    """
    Write down what is on the channels now, and forget what was taken off on purpose.
    Everything on a channel is remembered (the first run is the backfill of everything
    already matched); a remembered stream that still exists, is still listed by its
    provider and is on no longer its channel was taken off, and is forgotten. Returns
    {"added", "forgotten"}.
    """
    from .models import Stream

    on = _links(channel_ids)
    parked = _parked()
    at = _now()
    counts = {"added": 0, "forgotten": 0}

    def change(record):
        channels = record.setdefault("channels", {})
        scope = set(on) if channel_ids is None else {int(i) for i in channel_ids}
        remembered_ids = {
            e["stream"] for key, es in channels.items() if channel_ids is None or int(key) in scope for e in es
        }
        alive = dict(
            Stream.objects.filter(id__in=remembered_ids).values_list("id", "is_stale")
        )
        for key in list(channels) if channel_ids is None else [str(i) for i in scope]:
            present = {s.id for s in on.get(int(key), [])}
            kept = []
            for e in channels.get(key) or []:
                stream_id = e.get("stream")
                taken_off = (
                    stream_id in alive and not alive[stream_id]
                    and stream_id not in present and stream_id not in parked
                )
                if taken_off:
                    counts["forgotten"] += 1
                    continue
                kept.append(e)
            if kept:
                channels[key] = kept
            else:
                channels.pop(key, None)
        for channel_id, streams in on.items():
            key = str(channel_id)
            entries = channels.setdefault(key, [])
            known = {e["stream"]: e for e in entries}
            for stream in streams:
                if stream.id in known:
                    # The same stream: what identifies it is kept current
                    known[stream.id].update(
                        sid=stream.stream_id, name=stream.name or "", tvg=stream.tvg_id or ""
                    )
                    known[stream.id].pop("lost_at", None)
                else:
                    entries.append(_entry(stream, how, at))
                    counts["added"] += 1
        record["saved_at"] = at
        return counts

    change_row(PAIRINGS_KEY, PAIRINGS_NAME, change, default={})
    if counts["added"] or counts["forgotten"]:
        logger.info(
            f"Remembered streams: {counts['added']} written down ({how}), {counts['forgotten']} "
            f"taken off by hand and forgotten"
        )
    return counts


def _replacement(entry, account_id, exclude):
    """
    The stream a remembered one is now, at the same provider: the same stream number,
    else the one stream with the same tvg-id and name, else the one with the same name.
    """
    from .models import Stream

    streams = Stream.objects.filter(m3u_account_id=account_id, is_stale=False, is_custom=False).exclude(
        id__in=exclude
    )
    if entry.get("sid") is not None:
        found = list(streams.filter(stream_id=entry["sid"]).values_list("id", flat=True)[:2])
        if len(found) == 1:
            return found[0]
        if found:
            return None
    for lookup in (
        {"tvg_id": entry.get("tvg"), "name": entry.get("name")} if entry.get("tvg") else None,
        {"name": entry.get("name")} if entry.get("name") else None,
    ):
        if not lookup:
            continue
        found = list(streams.filter(**lookup).values_list("id", flat=True)[:2])
        if len(found) == 1:
            return found[0]
    return None


def after_refresh(account_id):
    """
    A provider's playlist was refreshed: its remembered streams that went stale or are gone
    are looked for again, and the ones found go back where the old ones were. Then what is
    on the channels is written down. Returns how many were put back.
    """
    if not enabled():
        return 0
    from django.db import transaction

    from .models import Channel, ChannelStream, Stream

    record = load()
    if not record["channels"]:
        # Nothing written down yet: the first thing to remember is what is matched now
        sync()
        return 0
    mine = [
        (int(key), e) for key, es in record["channels"].items() for e in es
        if e.get("account") == account_id
    ]
    if not mine:
        sync()
        return 0
    state = dict(
        Stream.objects.filter(id__in=[e["stream"] for _, e in mine]).values_list("id", "is_stale")
    )
    channels = {c.id: c for c in Channel.objects.filter(id__in={cid for cid, _ in mine})}
    parked = _parked()
    put_back, replaced = 0, {}
    cutoff = timezone.now() - timedelta(days=LOST_KEPT_DAYS)
    for channel_id, entry in mine:
        old = entry["stream"]
        gone = old not in state or state[old]
        channel = channels.get(channel_id)
        if not gone or channel is None or old in parked:
            continue
        on_channel = set(ChannelStream.objects.filter(channel=channel).values_list("stream_id", flat=True))
        new = _replacement(entry, account_id, exclude={old})
        if new is None:
            entry.setdefault("lost_at", _now())
            continue
        if new in parked:
            continue
        with transaction.atomic():
            if new not in on_channel:
                link = ChannelStream.objects.filter(channel=channel, stream_id=old).first()
                if link is not None:
                    # Where the old one was: it is stale, and its place is the new one's
                    link.stream_id = new
                    link.save(update_fields=["stream"])
                else:
                    from .channel_manager import _put_on_channel

                    _put_on_channel(channel, [new])
                put_back += 1
            elif ChannelStream.objects.filter(channel=channel, stream_id=old).exists():
                ChannelStream.objects.filter(channel=channel, stream_id=old).delete()
        replaced[(channel_id, old)] = new

    def change(saved):
        channels = saved.setdefault("channels", {})
        for key, es in list(channels.items()):
            for e in es:
                new = replaced.get((int(key), e.get("stream")))
                if new is not None:
                    e["stream"] = new
                    e.pop("lost_at", None)
                    e["how"] = "put back"
                elif e.get("account") == account_id:
                    lost = next(
                        (x for cid, x in mine if cid == int(key) and x["stream"] == e.get("stream")), None
                    )
                    if lost is not None and lost.get("lost_at") and not e.get("lost_at"):
                        e["lost_at"] = lost["lost_at"]
            channels[key] = [
                e for e in es
                if not (e.get("lost_at") and datetime.fromisoformat(e["lost_at"]) < cutoff)
            ]
        return saved

    change_row(PAIRINGS_KEY, PAIRINGS_NAME, change, default={})
    if put_back:
        logger.info(f"Remembered streams: {put_back} put back on their channels after a playlist refresh")
    sync(how="kept")
    return put_back


def forget():
    """Everything remembered goes; the next sync writes down what is matched then."""
    def change(record):
        n = sum(len(es) for es in (record.get("channels") or {}).values())
        record["channels"] = {}
        return n

    return change_row(PAIRINGS_KEY, PAIRINGS_NAME, change, default={})
