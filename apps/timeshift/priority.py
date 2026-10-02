"""Look-back priority: making room on a provider for a look back (fork/lookback-priority.md).

A look back can only come from the providers that keep the programme's archive. When every
connection of those providers is in use by live viewers, the session mint looks for one of
those live channels that has another stream of its own on a provider with room, moves it there
with the live proxy's normal stream switch (the viewers keep their connection), checks the
picture comes from the new stream, and only then lets the look back start. A move that does not
play is undone, and the look-back viewer is told "unavailable due to current viewing
priorities", with a cooldown so asking again does not move anyone again.

Never moved: recordings, channels on a custom (fallback) stream, a channel moved for a look
back in the last 10 minutes. Off (`look_back_priority` in the arrTV settings) is stock: the mint
answers as it always did and the player gets the stock 503.
"""

import json
import logging
import time

import gevent

logger = logging.getLogger(__name__)

STEP_TEXT = {
    "found": "Another viewer is watching on {provider}",
    "moving": "Moving them to another stream ({stream})",
    "moved": "Stream moved",
    "verified": "Stream verified",
    "ready": "Starting look back",
    "reverting": "That stream did not play; moving them back",
    "refused": "Unavailable due to current viewing priorities",
}
REFUSED_TEXT = STEP_TEXT["refused"]

COOLDOWN_SECONDS = 300
MOVED_RECENTLY_SECONDS = 600
VERIFY_SECONDS = 10
ROOM_TTL = 120


def room_key(session_id):
    return f"timeshift:api-room:{session_id}"


def cooldown_key(user_id, channel_uuid):
    return f"timeshift:priority-cooldown:{user_id}:{channel_uuid}"


def moved_key(channel_uuid):
    return f"timeshift:priority-moved:{channel_uuid}"


def _s(value):
    return value.decode() if isinstance(value, bytes) else value


def settings():
    try:
        from apps.proxy.live_proxy.app_devices import load_settings
        conf = load_settings()
        return bool(conf.get("look_back_priority")), bool(conf.get("look_back_priority_notify"))
    except Exception:
        return False, False


def _catchup_profiles(channel):
    """The active profiles of every account keeping the channel's archive, default first."""
    from apps.channels.utils import get_channel_catchup_streams

    accounts, profiles = set(), []
    for stream in get_channel_catchup_streams(channel):
        account = stream.m3u_account
        if account is None or account.id in accounts:
            continue
        accounts.add(account.id)
        profiles += sorted(account.profiles.filter(is_active=True), key=lambda p: not p.is_default)
    return accounts, profiles


def has_room(channel, redis_client):
    from apps.m3u.connection_pool import pool_has_capacity_for_profile

    _accounts, profiles = _catchup_profiles(channel)
    return any(pool_has_capacity_for_profile(p, redis_client) for p in profiles)


def _viewers(redis_client, channel_uuid):
    """The channel's clients that are people: (count, any recording)."""
    from apps.channels.captions import is_caption_client
    from apps.proxy.live_proxy.probation import _channel_clients, is_recording

    count, recording = 0, False
    for client in _channel_clients(redis_client, channel_uuid):
        agent = client.get("user_agent") or ""
        if is_recording(agent):
            recording = True
        elif not is_caption_client(agent):
            count += 1
    return count, recording


def candidates(channel, redis_client):
    """Live channels holding the archive's providers that could move, fewest viewers first.

    Each is (holding channel, its stream id, its profile id, [(stream, profile), ...]): the
    alternatives on providers with room, never the archive's own providers, never custom streams.
    """
    from apps.channels.models import Channel
    from apps.m3u.connection_pool import pool_has_capacity_for_profile
    from apps.proxy.live_proxy.constants import ChannelMetadataField
    from apps.proxy.live_proxy.probation import _active_channels
    from apps.proxy.live_proxy.redis_keys import RedisKeys

    archive_accounts, archive_profiles = _catchup_profiles(channel)
    held = {p.id for p in archive_profiles}
    found = []
    for channel_uuid, profile_id in _active_channels(redis_client):
        if profile_id not in held or channel_uuid == str(channel.uuid):
            continue
        if redis_client.exists(moved_key(channel_uuid)):
            continue
        viewers, recording = _viewers(redis_client, channel_uuid)
        if recording:
            continue
        holding = Channel.objects.filter(uuid=channel_uuid).first()
        if holding is None:
            continue
        stream_id = _s(redis_client.hget(RedisKeys.channel_metadata(channel_uuid), ChannelMetadataField.STREAM_ID))
        alternatives = []
        for stream in holding.streams.select_related("m3u_account").order_by("channelstream__order"):
            account = stream.m3u_account
            if stream.is_custom or account is None or not account.is_active:
                continue
            if account.id in archive_accounts or str(stream.id) == str(stream_id):
                continue
            for profile in sorted(account.profiles.filter(is_active=True), key=lambda p: not p.is_default):
                if pool_has_capacity_for_profile(profile, redis_client):
                    alternatives.append((stream, profile))
                    break
        if alternatives:
            found.append((viewers, holding, int(stream_id) if stream_id else None, profile_id, alternatives))
    found.sort(key=lambda item: item[0])
    return [item[1:] for item in found]


def write_step(redis_client, session_id, step, **extra):
    status = {"state": "refused" if step == "refused" else ("ready" if step == "ready" else "making_room"),
              "step": step, "text": STEP_TEXT.get(step, step).format(**{"provider": "", "stream": "", **extra}),
              "at": time.time(), **extra}
    raw = _s(redis_client.get(room_key(session_id)))
    steps = (json.loads(raw).get("steps") if raw else None) or []
    steps.append({"step": step, "text": status["text"]})
    status["steps"] = steps
    redis_client.set(room_key(session_id), json.dumps(status), ex=ROOM_TTL)
    return status


def read_room(session_id, redis_client):
    raw = _s(redis_client.get(room_key(session_id)))
    return json.loads(raw) if raw else None


def refuse(redis_client, user_id, channel_uuid):
    redis_client.set(cooldown_key(user_id, channel_uuid), "1", ex=COOLDOWN_SECONDS)
    return {"error": REFUSED_TEXT, "reason": "viewing_priorities", "retry_after": COOLDOWN_SECONDS}


def cooling_down(redis_client, user_id, channel_uuid):
    ttl = redis_client.ttl(cooldown_key(user_id, channel_uuid))
    return int(ttl) if ttl and int(ttl) > 0 else 0


def make_room(user, channel, redis_client):
    """Before minting: None to go on as stock; else ("refused", body) or ("moving", candidate)."""
    enabled, _notify = settings()
    if not enabled or redis_client is None:
        return None
    if has_room(channel, redis_client):
        # The other viewer left: whatever cooldown there was is over
        redis_client.delete(cooldown_key(user.id, channel.uuid))
        return None
    left = cooling_down(redis_client, user.id, channel.uuid)
    if left:
        return "refused", {"error": REFUSED_TEXT, "reason": "viewing_priorities", "retry_after": left}
    found = candidates(channel, redis_client)
    if not found:
        logger.info(f"Look-back priority: no live viewer on {channel.name}'s archive providers can move; refused")
        return "refused", refuse(redis_client, user.id, channel.uuid)
    return "moving", found


def _switch(channel_uuid, stream, profile):
    from apps.proxy.live_proxy.services.channel_service import ChannelService
    from apps.proxy.live_proxy.url_utils import get_stream_info_for_switch

    info = get_stream_info_for_switch(channel_uuid, stream.id, profile.id)
    if "error" in info:
        return False
    result = ChannelService.change_stream_url(
        channel_uuid, info["url"], info["user_agent"], stream.id, profile.id, info.get("stream_name"),
    )
    return bool(result.get("success"))


def verify(redis_client, channel_uuid, stream_id, seconds=VERIFY_SECONDS, sleep=gevent.sleep):
    """The channel names the new stream, is active, and its buffer grows: bytes from it."""
    from apps.proxy.live_proxy.constants import ChannelMetadataField, ChannelState
    from apps.proxy.live_proxy.redis_keys import RedisKeys

    meta_key = RedisKeys.channel_metadata(channel_uuid)
    first_index = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        current = _s(redis_client.hget(meta_key, ChannelMetadataField.STREAM_ID))
        state = _s(redis_client.hget(meta_key, ChannelMetadataField.STATE))
        index = _s(redis_client.get(RedisKeys.buffer_index(channel_uuid)))
        if current == str(stream_id) and state == ChannelState.ACTIVE and index is not None:
            if first_index is None:
                first_index = int(index)
            elif int(index) > first_index:
                return True
        sleep(0.5)
    return False


def _notify(holding, stream, notify):
    if not notify:
        return
    try:
        from core.utils import send_websocket_update
        send_websocket_update("updates", "update", {
            "type": "lookback_moved", "channel": str(holding.uuid),
            "stream": stream.name, "text": f"Moved to {stream.name} for another viewer",
        })
    except Exception as e:
        logger.debug(f"Look-back priority: could not tell the moved viewer: {e}")


def run_move(session_id, user_id, channel, found, redis_client, sleep=gevent.sleep):
    """The move, step by step, written for arrTV's poll. Returns the final step."""
    from apps.m3u.models import M3UAccountProfile
    from apps.channels.models import Stream

    _enabled, notify = settings()
    for number, (holding, old_stream_id, old_profile_id, alternatives) in enumerate(found):
        channel_uuid = str(holding.uuid)
        old_profile = M3UAccountProfile.objects.select_related("m3u_account").filter(id=old_profile_id).first()
        provider = _provider_name(old_profile_id)
        if number:  # the first was written by start_move, before the mint answered
            write_step(redis_client, session_id, "found", provider=provider, channel=holding.name)
        for stream, profile in alternatives:
            write_step(redis_client, session_id, "moving", stream=stream.name, provider=provider)
            redis_client.set(moved_key(channel_uuid), "1", ex=MOVED_RECENTLY_SECONDS)
            logger.info(f"Look-back priority: moving {holding.name} to {stream.name} (profile {profile.id}) "
                        f"so {channel.name}'s look back can use {provider}")
            if not _switch(channel_uuid, stream, profile):
                continue
            write_step(redis_client, session_id, "moved", stream=stream.name)
            if verify(redis_client, channel_uuid, stream.id, sleep=sleep):
                write_step(redis_client, session_id, "verified", stream=stream.name)
                _notify(holding, stream, notify)
                if has_room(channel, redis_client):
                    return write_step(redis_client, session_id, "ready")
                break
            # It did not play: back to what they were watching
            write_step(redis_client, session_id, "reverting", stream=stream.name)
            logger.warning(f"Look-back priority: {holding.name} did not play on {stream.name}; moving it back")
            old_stream = Stream.objects.filter(id=old_stream_id).first() if old_stream_id else None
            if old_stream is not None and old_profile is not None:
                _switch(channel_uuid, old_stream, old_profile)
                verify(redis_client, channel_uuid, old_stream.id, sleep=sleep)
            break
    refuse(redis_client, user_id, channel.uuid)
    return write_step(redis_client, session_id, "refused", retry_after=COOLDOWN_SECONDS)


def _provider_name(profile_id):
    from apps.m3u.models import M3UAccountProfile

    profile = M3UAccountProfile.objects.select_related("m3u_account").filter(id=profile_id).first()
    return profile.m3u_account.name if profile else "this provider"


def start_move(session_id, user_id, channel, found, redis_client):
    """Answer at once with the first step; the move runs on in the background."""
    holding, _stream_id, profile_id, _alternatives = found[0]
    status = write_step(redis_client, session_id, "found", provider=_provider_name(profile_id), channel=holding.name)
    gevent.spawn(_run_safely, session_id, user_id, channel, found, redis_client)
    return status


def _run_safely(session_id, user_id, channel, found, redis_client):
    try:
        from django.db import close_old_connections
        run_move(session_id, user_id, channel, found, redis_client)
        close_old_connections()
    except Exception as e:
        logger.exception(f"Look-back priority: making room failed: {e}")
        try:
            refuse(redis_client, user_id, channel.uuid)
            write_step(redis_client, session_id, "refused", retry_after=COOLDOWN_SECONDS)
        except Exception:
            pass
