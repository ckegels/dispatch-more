# What happens, step by step, when Stream Check opens one stream -- to find out why a stream that
# plays for a viewer "could not connect" in a check. Prints hosts, times and answers; never the
# login (addresses are shown without their path).
#
#   NAME="WELT FHD" ACCOUNT=TiviBridge2 bash dispatcharr-shell.sh shell < trace-stream.py
#
# NAME: part of the stream's name. ACCOUNT: the M3U account (optional). TIMES: how often to
# repeat (default 3). It opens another stream of the same provider first, then waits the pause a
# run leaves (Stream Check settings), then this one -- the way a run meets it. Skips a provider
# someone is watching through, and takes and gives back a connection the way a viewer does.
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse

logging.getLogger("apps.m3u.credentials").setLevel(logging.WARNING)  # it logs the login

import requests
from apps.channels import stream_check
from apps.channels.models import Stream
from apps.m3u.connection_pool import release_profile_slot, reserve_profile_slot
from core.utils import RedisClient

NAME = os.environ.get("NAME", "WELT FHD")
ACCOUNT = os.environ.get("ACCOUNT", "")
TIMES = int(os.environ.get("TIMES", "3"))

redis = RedisClient.get_client()
settings = stream_check.load_settings()
providers = stream_check._Providers(redis)

streams = Stream.objects.filter(name__icontains=NAME, is_custom=False).select_related("m3u_account")
if ACCOUNT:
    streams = streams.filter(m3u_account__name=ACCOUNT)
stream = streams.first()
if stream is None:
    raise SystemExit(f"No stream with '{NAME}' in its name{' on ' + ACCOUNT if ACCOUNT else ''}")
account = stream.m3u_account
other = (Stream.objects.filter(m3u_account=account, is_custom=False, channelstream__isnull=False)
         .exclude(id=stream.id).first())
agent = account.get_user_agent_string() or ""
gap = float(settings.get("gap_seconds") or 3)
print(f"{stream.name} on {account.name} ({account.account_type}); User-Agent: {agent or '(none)'}")
print(f"Warm-up stream before it, as in a run: {other.name if other else '(none)'}; pause {gap:.0f} s\n")


def where(url):
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"


def provider_count(login):
    if account.account_type != "XC":
        return ""
    try:
        info = stream_check._xc_user_info(account, login, agent)
        return f"provider counts {info.get('active_cons')}/{info.get('max_connections')} open"
    except Exception as e:
        return f"provider count unavailable ({type(e).__name__})"


def dns_and_tcp(url):
    parts = urlparse(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    started = time.monotonic()
    try:
        addresses = sorted({a[4][0] for a in socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as e:
        return f"    DNS {parts.hostname}: FAILED ({e}) after {time.monotonic() - started:.2f} s"
    lookup = time.monotonic() - started
    started = time.monotonic()
    try:
        with socket.create_connection((parts.hostname, port), timeout=5):
            pass
        tcp = f"TCP connect {time.monotonic() - started:.2f} s"
    except OSError as e:
        tcp = f"TCP connect FAILED after {time.monotonic() - started:.2f} s ({e.__class__.__name__}: {e.strerror or e})"
    return f"    DNS {parts.hostname} -> {', '.join(addresses)} in {lookup:.2f} s; {tcp}"


def open_like_a_check(url, label):
    """Every hop of the request, as requests would follow it, then the first bytes."""
    session = requests.Session()
    headers = {"User-Agent": agent} if agent else {}
    hop, started = url, time.monotonic()
    try:
        for step in range(6):
            print(dns_and_tcp(hop))
            t = time.monotonic()
            answer = session.get(hop, headers=headers, stream=True, timeout=(5, 5), allow_redirects=False)
            took = time.monotonic() - t
            if answer.is_redirect:
                nxt = urljoin(hop, answer.headers.get("Location", ""))
                print(f"    {label} hop {step + 1}: {where(hop)} answered {answer.status_code} in {took:.2f} s -> {where(nxt)}")
                answer.close()
                hop = nxt
                continue
            first = next(answer.iter_content(64 * 1024), b"")
            print(f"    {label} hop {step + 1}: {where(hop)} answered {answer.status_code} in {took:.2f} s, "
                  f"first {len(first) // 1024} KB after {time.monotonic() - t:.2f} s "
                  f"({answer.headers.get('Content-Type', '?')})")
            answer.close()
            return True
    except requests.exceptions.RequestException as e:
        print(f"    {label}: FAILED after {time.monotonic() - started:.2f} s at {where(hop)}: "
              f"{stream_check._why_no_connection(e)} ({type(e).__name__})")
        return False
    finally:
        session.close()


for attempt in range(1, TIMES + 1):
    print(f"── Try {attempt} of {TIMES} ──")
    key = providers.provider_of(account.id)
    if providers.in_use(key):
        print("    someone is watching through this provider: left alone, try again later")
        break
    login = next((p for p in stream_check._profiles_of(account) if reserve_profile_slot(p, redis)[0]), None)
    if login is None:
        print("    no connection of this account is free: left alone")
        break
    try:
        print(f"    before: {provider_count(login)}")
        if other:
            open_like_a_check(stream_check._url_for(other, login), "warm-up")
            print(f"    after warm-up: {provider_count(login)}; waiting {gap:.0f} s")
            time.sleep(gap)
            print(f"    after the pause: {provider_count(login)}")
        open_like_a_check(stream_check._url_for(stream, login), "this stream")
        # Reading as long as the picture look does, and where the data paused
        url = stream_check._url_for(stream, login)
        seconds = float(settings["picture_seconds"]) + 3 if settings.get("picture_check") else 3
        with requests.get(url, headers={"User-Agent": agent} if agent else {}, stream=True, timeout=(5, 30)) as answer:
            started = last = time.monotonic()
            total, longest = 0, 0.0
            for chunk in answer.iter_content(32 * 1024):
                now = time.monotonic()
                longest = max(longest, now - last)
                last, total = now, total + len(chunk)
                if now - started > seconds:
                    break
        print(f"    reading {seconds:.0f} s like the picture look: {total // 1024} KB, "
              f"longest pause in the data {longest:.1f} s"
              f"{'  <-- over the 5 s the check used to wait' if longest > 5 else ''}")
        found = stream_check.probe(
            stream_check._url_for(stream, login), agent, settings["timeout_seconds"],
            picture_seconds=settings["picture_seconds"] if settings.get("picture_check") else 0,
        )
        print(f"    Stream Check's own check right after: {'plays' if found['ok'] else 'FAILS'} "
              f"| {found.get('kind') or ''} | {found.get('reason') or found.get('resolution')}")
    finally:
        if login.max_streams > 0:
            release_profile_slot(login.id, redis)
    time.sleep(5)
    print()
