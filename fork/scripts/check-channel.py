# Test every stream of every channel whose name contains NAME, the way Stream Check opens
# them, and look 10 seconds into each for a picture that is black or frozen, or no sound.
# Prints what Stream Check has on record for each too. Prints no login.
# Skips any provider someone is watching through; takes and gives back a connection the way
# a viewer does.
import json
import logging
import os
import subprocess
import tempfile
import time

logging.getLogger("apps.m3u.credentials").setLevel(logging.WARNING)  # it logs the login

import requests
from apps.channels import stream_check
from apps.channels.models import Channel, ChannelStream
from apps.m3u.connection_pool import release_profile_slot, reserve_profile_slot
from core.utils import RedisClient

NAME = os.environ.get("NAME", "euronews")
SECONDS = int(os.environ.get("SECONDS", "10"))

redis = RedisClient.get_client()
settings = stream_check.load_settings()
results = stream_check.current_results(redis)["streams"]
parked = stream_check.load_parked()
providers = stream_check._Providers(redis)
groups = set(int(g) for g in settings.get("channel_groups") or ())


def record(url, agent, seconds):
    """seconds of the stream into a file, or (None, why)."""
    handle, path = tempfile.mkstemp(suffix=".ts")
    os.close(handle)
    started, size = time.monotonic(), 0
    try:
        with requests.get(url, headers={"User-Agent": agent} if agent else {}, stream=True, timeout=(5, 10)) as answer:
            if answer.status_code >= 400:
                return None, f"HTTP {answer.status_code}: {stream_check._said(answer.raw.read(300) or b'')}"
            with open(path, "wb") as out:
                for chunk in answer.iter_content(64 * 1024):
                    out.write(chunk)
                    size += len(chunk)
                    if time.monotonic() - started > seconds:
                        break
        return path, f"{size / 1024 / 1024:.1f} MB in {time.monotonic() - started:.1f} s ({size * 8 / 1000 / max(0.1, time.monotonic() - started):.0f} kbps)"
    except requests.RequestException as e:
        return None, f"could not be opened: {type(e).__name__}"


def look(path):
    """What is in the recording: picture, sound, and whether it is black, frozen or silent."""
    found = []
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=30,
    )
    info = json.loads(probe.stdout or "{}")
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            found.append(f"video {s.get('codec_name')} {s.get('width')}x{s.get('height')}")
        elif s.get("codec_type") == "audio":
            found.append(f"audio {s.get('codec_name')}")
    found.append(f"{float(info.get('format', {}).get('duration') or 0):.1f} s long")
    check = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-vf", "blackdetect=d=2:pix_th=0.10,freezedetect=n=0.003:d=4",
         "-af", "silencedetect=n=-50dB:d=4", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    log = check.stderr
    if "black_start" in log:
        found.append("BLACK picture")
    if "freeze_start" in log:
        found.append("FROZEN picture")
    if "silence_start" in log:
        found.append("SILENT sound")
    return ", ".join(found)


channels = Channel.objects.filter(name__icontains=NAME).select_related("channel_group").order_by("channel_number")
print(f"{channels.count()} channel(s) with '{NAME}' in the name; Stream Check "
      f"{'is on' if settings.get('enabled') else 'is off'}, checking "
      f"{'every group' if not groups else f'{len(groups)} group(s)'}\n")

for channel in channels:
    in_scope = not groups or channel.channel_group_id in groups
    print(f"== {channel.channel_number} {channel.name}  [{channel.channel_group.name if channel.channel_group_id else 'no group'}]"
          f"{'' if in_scope else '  -- NOT in the groups Stream Check checks'}")
    links = ChannelStream.objects.filter(channel=channel).select_related("stream", "stream__m3u_account").order_by("order")
    if not links:
        print("   no streams")
    for link in links:
        stream = link.stream
        account = stream.m3u_account
        kept = results.get(str(stream.id))
        on_record = (
            "never checked" if not kept else
            f"{stream_check.state_of(kept, settings)} ({kept.get('reason') or kept.get('resolution') or 'plays'}, "
            f"{kept.get('failures', 0)} failure(s), last {kept.get('checked_at', '')[:16]})"
        )
        print(f"  {link.order + 1}. {stream.name}  | {account.name if account else 'custom'}")
        print(f"     Stream Check has: {on_record}")
        if stream.is_custom or account is None:
            print("     custom stream (a fallback): not checked")
            continue
        if not account.is_active:
            print("     account switched off: not checked")
            continue
        key = providers.provider_of(account.id)
        if providers.in_use(key):
            print("     someone is watching through this provider: left alone")
            continue
        logins = [p for p in stream_check._profiles_of(account)]
        login = next((p for p in logins if reserve_profile_slot(p, redis)[0]), None)
        if login is None:
            print("     no connection of this provider is free: left alone")
            continue
        try:
            url = stream_check._url_for(stream, login)
            agent = account.get_user_agent_string() or ""
            quick = stream_check.probe(url, agent, 12)
            print(f"     quick check (what Stream Check does): {'plays' if quick['ok'] else 'FAILS'}"
                  f" {quick.get('reason') or ''} {quick.get('resolution') or ''} {quick.get('codec') or ''}")
            path, how = record(url, agent, SECONDS)
            print(f"     {SECONDS} s recording: {how}")
            if path:
                print(f"     in it: {look(path)}")
                os.unlink(path)
        finally:
            if login.max_streams > 0:
                release_profile_slot(login.id, redis)
        time.sleep(3)
    print()
