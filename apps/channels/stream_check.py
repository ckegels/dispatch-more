"""Stream Check: finding the streams on channels that no longer play.

A provider drops a channel, moves it, or leaves it answering with nothing, and the stream
on the channel stays. Dispatcharr only finds out when someone tunes in, and they sit
through every dead stream before a live one. This opens each stream on a channel, sees
whether a picture comes, and closes it again, so the dead ones can be dealt with before
anyone is sent to them.

How it goes about it, and why:

- Never on a provider someone is using. A provider is every M3U account that reaches the
  same server, uses the same login, or is in the same server group -- one customer to the
  provider, whatever Dispatcharr calls them. Before every stream, and all the while one is
  read, it looks whether anyone watches anything through that provider, live or VOD; for
  Xtream Codes it also asks the provider, which sees viewers in other apps too. A provider
  in use is left alone and the others are checked meanwhile; one someone starts watching
  through has its check dropped at once. A viewer who finds a provider full because of a
  check asks it to make way (see make_way), and gets the connection within a second.
  Stricter still, only_when_idle checks nothing while anything at all plays.
- Only providers that work. Before its streams, each account's logins are looked at:
  expired, or refused by an Xtream Codes provider, and the account is left for the round.
  A provider that is down fails every stream, so when its first streams all fail it is
  taken to be down, and left too. Either way its streams are not counted against: they
  were never really looked at.
- One stream at a time per provider, however many accounts it has, every provider at
  once. A provider's connections
  are taken the way a viewer takes them (connection_pool.reserve_profile_slot), so a run
  never goes past a provider's limit, and with a pause between streams so it does not
  look like a flood.
- A picture, not an answer. A provider that is done with a channel often still answers
  200, with an error page, an empty playlist or a stream that never sends anything. So a
  stream counts as working only when video arrives, read by ffprobe where it is installed.
- In batches of a few minutes. A round over every stream can take hours, and a worker
  held that long is one the playlist and guide refreshes cannot have. Each batch keeps
  what it found and queues the next, so other work gets its turn in between; one that
  ends because someone is watching is carried on by the next tick (every five minutes).
- One failure is not dead. Providers hiccup. A stream is "broken" only after failing a
  number of runs in a row (broken_after), and "failing" until then.

What is done about a broken stream is decided by a person, on the page: take it off the
channel, or park it. A parked stream comes off its channels -- so no viewer is sent to it
-- but is remembered with where it was, checked again on every run, and can be put back
exactly where it was when it works again. Parked streams are left out of the Channel
Manager's merge, which would otherwise put them straight back.

Everything is kept in CoreSettings rather than tables of its own, so installing this needs
no migration and switching it off leaves Dispatcharr exactly as it was.
"""

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

SETTINGS_KEY = "stream-check"
RESULTS_KEY = "stream-check-results"
PARKED_KEY = "stream-check-parked"

DEFAULTS = {
    # Off unless turned on: a check takes provider connections
    "enabled": False,
    # How often each stream is looked at
    "every_hours": 24,
    # Only start or carry on between these times ("HH:MM", server time); empty is any time
    "window_from": "",
    "window_to": "",
    # How long a stream has to show a picture
    "timeout_seconds": 12,
    # The pause between two streams of one provider: about what zapping leaves, so the
    # provider -- or a bridge in front of it -- has let go of one before the next
    "gap_seconds": 3,
    # Runs in a row a stream has to fail before it is called broken
    "broken_after": 2,
    # Channel groups to check; empty is every channel
    "channel_groups": [],
    # Put a parked stream back by itself when it works again, rather than leaving it to a
    # person to decide
    "restore_recovered": False,
    # Only while nothing at all is playing through Dispatcharr. Off by default: checks go
    # on while people watch, but never on a provider any of them is using (see
    # _Providers). On, nothing is checked while anyone watches anything.
    "only_when_idle": False,
    # A provider whose first streams in a run all fail is down, not its streams: after this
    # many in a row it is left for the rest of the run, and those failures do not count
    "account_failures": 5,
}

# ── Redis keys, all short-lived: what outlives a run is written to CoreSettings ──
RUN_KEY = "stream-check:running"
# Refreshed while a run is alive, so a worker that died does not block the next forever
RUN_TTL = 120
STOP_KEY = "stream-check:stop"
YIELD_KEY = "stream-check:make-way"
YIELD_SECONDS = 20
PROGRESS_KEY = "stream-check:progress"
# Set while the next batch is queued, so the tick does not start a second chain of them
QUEUED_KEY = "stream-check:queued"
# How soon a round that is waiting (a provider in use or refusing) is tried again. The tick
# every five minutes is there if this is ever lost.
RETRY_WAITING = 60
# The round going: which streams it is to look at, so a batch knows where to carry on
ROUND_KEY = "stream-check:round"
ROUND_TTL = 7 * 86400
# How long one batch runs before it makes room for other work
BATCH_SECONDS = 240
LIVE_RESULTS_KEY = "stream-check:live-results"

# What a provider answers when it will not give another connection -- too many open on the
# login, or the login blocked for a moment -- rather than when a stream is gone. Not the
# stream's fault, and not counted against it.
REFUSED_STATUS = {401, 403, 406, 407, 423, 429, 456, 458, 503, 509, 512, 513, 551, 882, 884}
# A refusal is often a moment's: the stream is tried once more after this long, and only
# this many refusals in a row -- of different streams -- mean the provider is busy for now
REFUSAL_PAUSE = 15
REFUSALS_IN_A_ROW = 3
# Each refusal doubles the pause between a provider's streams, up to this: a provider that
# rate-limits new connections, or a bridge that keeps its own upstream connection open a
# while after ours closes, is given room. Each stream that plays brings it back down.
SLOWEST_GAP = 60
# A provider can go on counting a connection for a while after it is closed. After a check,
# a login the provider still counts as in use is waited on this long before it is taken to
# be someone else's.
PROVIDER_LINGER = 45
# How often the provider is asked how many connections a login has open, at most
PROVIDER_ASK_EVERY = 5

# How much of a stream is read before it is judged: enough for ffprobe to find the picture
READ_BYTES = 1024 * 1024
# Less than this, and a stream that ended by itself did not really start
TOO_LITTLE = 16 * 1024
# How often, while waiting for viewers to finish, it looks again
QUIET_POLL = 5
# Kept per stream, so the page can say "failed 3 of the last 5 runs"
HISTORY_KEPT = 10


# ── Settings and what is kept ────────────────────────────────────────────────


def _load(key, default):
    from core.models import CoreSettings

    try:
        stored = CoreSettings.objects.filter(key=key).first()
        if stored and isinstance(stored.value, dict):
            return stored.value
    except Exception as e:
        logger.debug(f"Could not read {key}: {e}")
    return default


def _store(key, name, value):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(key=key, defaults={"name": name, "value": value})


# Saved settings from before a default changed keep everything but what changed. Version 2:
# only_when_idle went from on to off, once providers in use could be told apart. Version 3:
# gap_seconds went from 1 to 3.
SETTINGS_VERSION = 3
CHANGED_IN = {2: ("only_when_idle",), 3: ("gap_seconds",)}


def load_settings():
    values = dict(DEFAULTS)
    stored = _load(SETTINGS_KEY, {})
    version = stored.get("version", 1) if isinstance(stored.get("version"), int) else 1
    outdated = {k for v, keys in CHANGED_IN.items() if version < v for k in keys}
    values.update({k: v for k, v in stored.items() if k in DEFAULTS and k not in outdated})
    return values


def save_settings(given):
    values = load_settings()
    values.update({k: v for k, v in (given or {}).items() if k in DEFAULTS})
    try:
        values["every_hours"] = max(1, float(values["every_hours"]))
        values["timeout_seconds"] = min(60, max(3, float(values["timeout_seconds"])))
        values["gap_seconds"] = min(60, max(0, float(values["gap_seconds"])))
        values["broken_after"] = max(1, int(values["broken_after"]))
        values["account_failures"] = max(2, int(values["account_failures"]))
    except (TypeError, ValueError):
        raise ValueError("Numbers only, please")
    for field in ("window_from", "window_to"):
        if values[field] and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(values[field])):
            raise ValueError("Times as HH:MM, please")
    _store(SETTINGS_KEY, "Stream Check", {**values, "version": SETTINGS_VERSION})
    return values


def load_results():
    """{"streams": {stream id: result}, "last_run": {...}}, as the last run left them."""
    stored = _load(RESULTS_KEY, {})
    return {"streams": dict(stored.get("streams") or {}), "last_run": stored.get("last_run") or {}}


def load_parked():
    """{stream id: {"channels": [{"channel": id, "order": n}], "parked_at": ..., ...}}"""
    return dict(_load(PARKED_KEY, {}))


def parked_ids():
    """The streams parked, for the Channel Manager to leave out of its merge."""
    return {int(i) for i in load_parked()}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── Which providers are in use ───────────────────────────────────────────────


class _Providers:
    """
    The providers behind the M3U accounts, and whether anyone is using one.

    A provider is every account that reaches the same server, uses the same login, or is
    in the same server group: to the provider these are one customer, whatever Dispatcharr
    calls them. A provider anyone is using -- through any of its accounts, live or VOD --
    is not touched at all; the others are checked meanwhile. Asked before every stream and
    every moment a stream is read, since a viewer can start on any provider at any time.

    What Redis says is kept for half a second: finding it means walking its keys, and every
    provider's thread asks many times a second.
    """

    def __init__(self, redis_client, profiles=None):
        self.redis = redis_client
        self.lock = threading.Lock()
        self.at = -1.0
        self.playing = False
        self.live_profiles = set()
        profiles = list(profiles if profiles is not None else _all_profiles())
        self.key_of, self.members = _group_providers(profiles)

    def _look(self):
        with self.lock:
            if time.monotonic() - self.at < 0.5:
                return
            playing = False
            for pattern in ("live:channel:*:metadata", "vod_proxy:connection:*"):
                for _ in self.redis.scan_iter(match=pattern, count=500):
                    playing = True
                    break
            live = set()
            if playing:
                # The login each running channel is on (set by the proxy as it starts one)
                for key in self.redis.scan_iter(match="stream_profile:*", count=500):
                    try:
                        live.add(int(self.redis.get(key) or 0))
                    except (TypeError, ValueError):
                        pass
            self.playing, self.live_profiles, self.at = playing, live, time.monotonic()

    def anything_playing(self):
        self._look()
        return self.playing

    def provider_of(self, account_id):
        return self.key_of.get(("account", account_id), ("account", account_id))

    def in_use(self, provider, holding=None):
        """
        Whether anyone but the check is on this provider. holding: the login the check has
        a connection on, which is its own and not a viewer's.

        With nothing playing anywhere, a count left above zero is one a stream that ended
        badly failed to give back (it happens), not a viewer; trusting it would leave that
        provider unchecked for good.
        """
        from apps.m3u.connection_pool import get_profile_connection_count

        self._look()
        if not self.playing:
            return False
        open_here = 0
        for profile in self.members.get(provider, ()):
            if profile.id in self.live_profiles:
                return True
            open_here += get_profile_connection_count(profile, self.redis)
        own = 1 if holding is not None and holding.max_streams > 0 else 0
        return open_here - own > 0


def _all_profiles():
    from apps.m3u.models import M3UAccountProfile

    return M3UAccountProfile.objects.filter(is_active=True, m3u_account__is_active=True).select_related(
        "m3u_account__server_group"
    )


def _group_providers(profiles):
    """
    ({("account", id): provider key}, {provider key: [profiles]}): accounts joined when they
    share a server, a login or a server group. Reads the database (a login's credentials),
    so it is worked out once per batch, before the providers' threads start.
    """
    from apps.m3u.connection_pool import get_profile_credential_fingerprint

    parent = {}

    def find(node):
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def join(a, b):
        parent[find(a)] = find(b)

    for profile in profiles:
        account = profile.m3u_account
        node = ("account", account.id)
        find(node)
        server = (account.server_url or "").split("://")[-1].split("/")[0].split(":")[0].lower()
        if server:
            join(node, ("server", server))
        if account.server_group_id:
            join(node, ("group", account.server_group_id))
        try:
            fingerprint = get_profile_credential_fingerprint(profile)
        except Exception:
            fingerprint = None
        if fingerprint:
            # The same login through a different address is still the same customer
            join(node, ("login", fingerprint))

    key_of, members = {}, {}
    for profile in profiles:
        node = ("account", profile.m3u_account_id)
        key = find(node)
        key_of[node] = key
        members.setdefault(key, []).append(profile)
    return key_of, members


class _ProviderCount:
    """
    How many connections an Xtream Codes provider says its logins have open, asked before
    every stream: the provider sees every viewer, in Dispatcharr or in any other app. A
    provider that says any of its logins is in use is not checked.
    """

    def __init__(self, logins):
        # [(account, profile, user agent)] of the provider's Xtream Codes logins
        self.logins = logins
        self.asked = {}
        self.last_check_ended = None

    def _open(self, account, profile, agent):
        at, count = self.asked.get(profile.id, (0.0, None))
        if time.monotonic() - at >= PROVIDER_ASK_EVERY:
            try:
                count = int(_xc_user_info(account, profile, agent).get("active_cons") or 0)
            except Exception as e:
                logger.info(f"Stream Check: could not ask {account.name} how busy {profile.name} is: {e}")
                count = None
            self.asked[profile.id] = (time.monotonic(), count)
        return count

    def free(self, should_give_up):
        """
        Whether the provider says nobody is on any of its logins. Right after a check it
        may still count that one: then it is waited on, up to PROVIDER_LINGER, rather than
        taken to be a viewer. A provider that cannot be asked is taken to be in use.
        """
        while True:
            counts = [self._open(*login) for login in self.logins]
            if all(count == 0 for count in counts):
                return True
            if any(count is None for count in counts):
                return False
            ended = self.last_check_ended
            if ended is None or time.monotonic() - ended > PROVIDER_LINGER or should_give_up():
                return False
            time.sleep(PROVIDER_ASK_EVERY)

    def check_ended(self):
        self.last_check_ended = time.monotonic()
        # What was asked before the check is no longer true
        self.asked.clear()


# ── Is an account working at all ─────────────────────────────────────────────


class NoLogin(ValueError):
    """An Xtream Codes profile whose login cannot be worked out: nothing to ask with."""


def _xc_user_info(account, profile, user_agent):
    """
    What an Xtream Codes provider says about a login: user_info from player_api.php, the
    call its apps make to log in. Not a stream: it takes no connection. Raises when the
    provider cannot be asked.
    """
    import requests

    from apps.m3u.credentials import get_transformed_credentials

    server, username, password = get_transformed_credentials(account, profile)
    if not (server and username and password):
        raise NoLogin("Dispatcharr could not work out its login (check the profile's search and replace)")
    response = requests.get(
        f"{server.rstrip('/')}/player_api.php",
        params={"username": username, "password": password},
        headers={"User-Agent": user_agent} if user_agent else {},
        timeout=10,
    )
    response.raise_for_status()
    info = (response.json() or {}).get("user_info")
    if not isinstance(info, dict):
        raise ValueError("the provider's answer has no account in it")
    return info


def _login_problem(account, profile, user_agent, now=None):
    """
    Why a login cannot be checked with, or None: it expired, or its provider refuses it.
    For an Xtream Codes account the provider is asked; for any other, only the expiry date
    Dispatcharr has can be looked at, and a provider that is down shows as its streams
    failing (see account_failures).
    """
    import requests

    now = now or datetime.now(timezone.utc)
    expires = profile.get_account_expiration() if hasattr(profile, "get_account_expiration") else None
    if expires and expires < now:
        return f"its login expired on {expires:%Y-%m-%d}"
    if account.account_type != "XC":
        return None
    try:
        info = _xc_user_info(account, profile, user_agent)
    except NoLogin as e:
        return str(e)
    except (requests.exceptions.RequestException, ValueError) as e:
        return f"the provider did not answer when asked about the login ({type(e).__name__})"
    if str(info.get("auth", "1")) != "1":
        return "the provider refused the login"
    status = str(info.get("status") or "Active")
    if status.lower() != "active":
        return f"the provider says the login is {status}"
    return None


def _usable_logins(account, profiles, user_agent):
    """
    The account's logins that can be checked with, and why none can when none can.
    Asked once per account per batch: a provider's login does not come and go by the minute.
    """
    usable, problems = [], []
    for profile in profiles:
        problem = _login_problem(account, profile, user_agent)
        if problem:
            problems.append(f"{profile.name}: {problem}" if len(profiles) > 1 else problem)
        else:
            usable.append(profile)
    if not profiles:
        problems.append("it has no active profile")
    return usable, "; ".join(problems)


def make_way(redis_client):
    """
    A viewer found a provider full: if a check could be holding the connection, have it
    let go. Called from the viewer's path, so it costs one lookup when no run is going.
    """
    try:
        if redis_client and redis_client.exists(RUN_KEY):
            redis_client.set(YIELD_KEY, "1", ex=YIELD_SECONDS)
            logger.info("Stream Check: a viewer needs a connection, checks are making way")
    except Exception as e:
        logger.debug(f"Stream Check could not be asked to make way: {e}")


def in_window(settings, now=None):
    start, end = settings.get("window_from"), settings.get("window_to")
    if not start or not end:
        return True
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    a = int(start[:-3]) * 60 + int(start[-2:])
    b = int(end[:-3]) * 60 + int(end[-2:])
    # A window over midnight, as a night is: 23:00 to 06:00
    return a <= minutes < b if a <= b else minutes >= a or minutes < b


# ── Looking at one stream ────────────────────────────────────────────────────


class Stopped(Exception):
    """The check was called off -- someone started watching -- and says nothing."""


class _Answered(Exception):
    """A part of an HLS stream answered with an error status."""

    def __init__(self, status, what):
        super().__init__(status)
        self.status, self.what = status, what


def _ffprobe(data):
    """What ffprobe finds in the bytes read: {"video": bool, "audio": bool, ...}, or None."""
    try:
        done = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height", "-of", "json", "-i", "pipe:0",
            ],
            input=data, capture_output=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"ffprobe not usable here: {e}")
        return None
    try:
        streams = json.loads(done.stdout or b"{}").get("streams") or []
    except ValueError:
        return {"video": False, "audio": False}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    return {
        "video": video is not None,
        "audio": any(s.get("codec_type") == "audio" for s in streams),
        "codec": (video or {}).get("codec_name", ""),
        "resolution": f"{video['width']}x{video['height']}" if video and video.get("width") else "",
    }


def _said(data):
    """An error answer's text, short and without markup, or nothing if it is not text."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data or "")
    if "\ufffd" in text[:100]:
        return ""
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _looks_like_ts(data):
    """MPEG-TS: a sync byte every 188 bytes, three in a row somewhere near the start."""
    for start in range(min(188, len(data))):
        if len(data) > start + 376 and data[start] == data[start + 188] == data[start + 376] == 0x47:
            return True
    return False


def _read(session, url, headers, deadline, should_stop, limit=READ_BYTES):
    """Up to limit bytes of url, before the deadline; (response, bytes)."""
    left = max(1.0, deadline - time.monotonic())
    response = session.get(url, headers=headers, stream=True, timeout=(min(5.0, left), min(5.0, left)))
    data = bytearray()
    try:
        if response.status_code >= 400:
            # What it says with the error, for the page: a provider's own words are often
            # the only way to tell "gone" from "full" from "not allowed"
            try:
                data.extend(next(response.iter_content(chunk_size=512), b"")[:512])
            except Exception:
                pass
            return response, bytes(data)
        for chunk in response.iter_content(chunk_size=32 * 1024):
            if should_stop():
                raise Stopped()
            data.extend(chunk)
            if len(data) >= limit or time.monotonic() > deadline:
                break
    finally:
        response.close()
    return response, bytes(data)


def _hls_segment(session, url, text, headers, deadline, should_stop, depth=0):
    """The URL of a segment of an HLS playlist: through a variant, the newest segment."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    uris = [line for line in lines if not line.startswith("#")]
    if not uris:
        return None
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines) and depth < 2:
        variant = urljoin(url, uris[0])
        response, data = _read(session, variant, headers, deadline, should_stop, limit=256 * 1024)
        if response.status_code >= 400:
            raise _Answered(response.status_code, "The playlist's variant")
        return _hls_segment(session, variant, data.decode("utf-8", "replace"), headers, deadline, should_stop, depth + 1)
    # The newest: on a live playlist the first ones may already be gone
    return urljoin(url, uris[-1])


def probe(url, user_agent="", timeout=12, should_stop=lambda: False):
    """
    Whether url plays: {"ok", "reason", "resolution", "codec", "bytes", "seconds"}.

    Raises Stopped if should_stop says so part way: a check cut short says nothing about
    the stream, and must not count against it.
    """
    import requests

    started = time.monotonic()
    deadline = started + float(timeout)
    headers = {"User-Agent": user_agent} if user_agent else {}
    result = {"ok": False, "reason": "", "resolution": "", "codec": "", "bytes": 0, "seconds": 0.0}
    session = requests.Session()
    try:
        response, data = _read(session, url, headers, deadline, should_stop)
        if response.status_code >= 400:
            said = _said(data)
            result["reason"] = f"The provider answered HTTP {response.status_code}" + (f": {said}" if said else "")
            result["refused"] = response.status_code in REFUSED_STATUS
            return result
        kind = (response.headers.get("Content-Type") or "").lower()
        if data[:7] == b"#EXTM3U" or "mpegurl" in kind:
            segment = _hls_segment(session, response.url or url, data.decode("utf-8", "replace"), headers, deadline, should_stop)
            if not segment:
                result["reason"] = "The playlist lists nothing to play"
                return result
            response, data = _read(session, segment, headers, deadline, should_stop)
            if response.status_code >= 400:
                raise _Answered(response.status_code, "The playlist's video")
        result["bytes"] = len(data)
        if len(data) < TOO_LITTLE:
            result["reason"] = (
                "Nothing came within {:.0f} s".format(timeout) if not data
                else f"Only {len(data) // 1024} KB came, then nothing"
            )
            return result
        if data[:200].lstrip().lower().startswith((b"<!doctype", b"<html", b"{", b"<?xml")):
            result["reason"] = "The provider sent a page, not video"
            return result
        found = _ffprobe(data)
        if found is None:
            # No ffprobe here: MPEG-TS that keeps coming is the best sign left
            result["ok"] = _looks_like_ts(data) or kind.startswith("video/")
            result["reason"] = "" if result["ok"] else "What came is not video Dispatcharr knows"
        elif found["video"]:
            result.update(ok=True, resolution=found["resolution"], codec=found["codec"])
        elif found["audio"]:
            # A radio channel is a channel that works
            result.update(ok=True, codec="audio only")
        else:
            result["reason"] = "No picture or sound in what came"
        return result
    except Stopped:
        raise
    except _Answered as e:
        result["reason"] = f"{e.what} answered HTTP {e.status}"
        result["refused"] = e.status in REFUSED_STATUS
    except requests.exceptions.ConnectTimeout:
        result["reason"] = "The provider did not answer"
    except requests.exceptions.ReadTimeout:
        result["reason"] = "The provider answered, then sent nothing"
    except requests.exceptions.ConnectionError:
        result["reason"] = "Could not connect to the provider"
    except requests.exceptions.RequestException as e:
        result["reason"] = f"Could not be opened: {type(e).__name__}"
    finally:
        session.close()
        result["seconds"] = round(time.monotonic() - started, 1)
    return result


# ── Which streams, and a provider connection for each ────────────────────────


def _targets(settings, only=None, due_before=None, skip_accounts=()):
    """
    The streams to look at, by account: those on channels (in the groups chosen), and
    every parked one. Custom streams are made by hand -- a fallback screen -- and are not
    a provider's to lose.
    """
    from .models import ChannelStream, Stream

    links = ChannelStream.objects.filter(stream__is_custom=False, stream__m3u_account__isnull=False)
    groups = [int(g) for g in settings.get("channel_groups") or ()]
    if groups:
        links = links.filter(channel__channel_group_id__in=groups)
    wanted = set(links.values_list("stream_id", flat=True)) | parked_ids()
    if only is not None:
        wanted &= {int(i) for i in only}
    if due_before is not None:
        # A run that stopped part way goes on where it was: what was looked at lately is
        # not looked at again
        results = load_results()["streams"]
        wanted = {i for i in wanted if (results.get(str(i)) or {}).get("checked_at", "") < due_before}

    by_account = {}
    for stream in (
        Stream.objects.filter(id__in=wanted, m3u_account__is_active=True)
        .exclude(m3u_account_id__in=[int(a) for a in skip_accounts])
        .select_related("m3u_account")
        .order_by("id")
    ):
        by_account.setdefault(stream.m3u_account_id, []).append(stream)
    return by_account


def _profiles_of(account):
    """The account's profiles a viewer could be given, the default first."""
    # With its account and server group, which taking a connection reads
    return sorted(
        (p for p in account.profiles.select_related("m3u_account__server_group") if p.is_active),
        key=lambda p: (not p.is_default, p.id),
    )


def _take_connection(logins, redis_client):
    """A connection on one of the account's logins, taken the way a viewer takes one; or None."""
    from apps.m3u.connection_pool import reserve_profile_slot

    for profile in logins:
        reserved, _, _ = reserve_profile_slot(profile, redis_client)
        if reserved:
            return profile
    return None


def _url_for(stream, profile):
    """
    The address a viewer would be sent to, made the way the proxy makes it. For an Xtream
    Codes account that is the login as it is now plus the provider's stream id, not the
    address kept from the last playlist refresh: that one can carry an old login, and a
    provider answers it with an error (HTTP 407 from a bridge, on a real installation) that
    a viewer never sees.
    """
    from apps.proxy.live_proxy.url_utils import _resolve_live_stream_url

    return _resolve_live_stream_url(stream, profile.m3u_account, profile)


# ── A run ────────────────────────────────────────────────────────────────────


def _record(redis_client, results, stream, outcome, settings):
    """What a check found, onto what earlier runs found."""
    previous = results.get(str(stream.id)) or {}
    history = ([1 if outcome["ok"] else 0] + list(previous.get("history") or []))[:HISTORY_KEPT]
    failures = 0 if outcome["ok"] else int(previous.get("failures") or 0) + 1
    record = {
        "ok": outcome["ok"],
        "reason": outcome["reason"],
        "resolution": outcome["resolution"],
        "codec": outcome["codec"],
        "seconds": outcome["seconds"],
        "checked_at": _now(),
        "last_ok": _now() if outcome["ok"] else previous.get("last_ok", ""),
        "failures": failures,
        "history": history,
        "name": stream.name,
    }
    record["state"] = state_of(record, settings)
    results[str(stream.id)] = record
    redis_client.hset(LIVE_RESULTS_KEY, str(stream.id), json.dumps(record))
    return record


def state_of(record, settings):
    """"ok", "failing" (not yet enough runs in a row) or "broken"."""
    if not record or record.get("skipped"):
        # Looked at, but nothing could be learned: the provider was full the whole time
        return "unchecked"
    if record.get("ok"):
        return "ok"
    return "broken" if int(record.get("failures") or 0) >= int(settings.get("broken_after") or 1) else "failing"


def _progress(redis_client, **changes):
    try:
        current = json.loads(redis_client.get(PROGRESS_KEY) or "{}")
    except ValueError:
        current = {}
    current.update(changes)
    redis_client.set(PROGRESS_KEY, json.dumps(current), ex=7 * 86400)
    return current


def progress(redis_client):
    try:
        return json.loads(redis_client.get(PROGRESS_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def start_round(redis_client, force=False):
    """
    Begin looking at every stream again. A round is worked through in batches (see run),
    so it can take as long as the providers make it without holding a worker all that time.

    force looks at every stream (started from the page); otherwise only those not looked at
    for every_hours. Returns how many streams the round has to look at, or None if one is
    already going.
    """
    if redis_client.exists(ROUND_KEY):
        return None
    settings = load_settings()
    since = _now() if force else datetime.fromtimestamp(
        time.time() - float(settings["every_hours"]) * 3600 * 0.9, timezone.utc
    ).isoformat(timespec="milliseconds")
    total = sum(len(s) for s in _targets(settings, due_before=since).values())
    redis_client.set(ROUND_KEY, json.dumps({"since": since, "forced": bool(force), "total": total}), ex=ROUND_TTL)
    redis_client.delete(STOP_KEY)
    _progress(
        redis_client, state="running", started_at=_now(), finished_at="", total=total,
        done=0, broken=0, accounts={}, waiting=False, message="",
    )
    logger.info(f"Stream Check: a round of {total} streams begins")
    return total


def queue_next(redis_client, task, seconds, ended):
    """
    Queue the next batch of the round, once: the tick sees it is queued and leaves it. Says
    on the page and in the log why the batch ended and when the next one is.
    """
    if current_round(redis_client) is None:
        return False
    if not redis_client.set(QUEUED_KEY, "1", nx=True, ex=int(seconds) + 120):
        return False
    task.apply_async(countdown=seconds)
    at = datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(timespec="milliseconds")
    _progress(redis_client, next_batch_at=at, last_batch_ended=ended)
    message = progress(redis_client).get("message") or ""
    logger.info(
        f"Stream Check: batch ended ({ended}{': ' + message if message else ''}), next in {seconds} s"
    )
    return True


def current_round(redis_client):
    try:
        return json.loads(redis_client.get(ROUND_KEY) or "null")
    except (ValueError, TypeError):
        return None


def run(redis_client, only=None, batch_seconds=None):
    """
    One batch of the round going, or a check of the streams given (only).

    A batch looks at streams, every provider at once, for batch_seconds, keeps what it
    found, and ends. Each provider's part:

    - first, whether the account works at all: its logins' expiry dates, and for an Xtream
      Codes account whether the provider still accepts them. One that does not is left for
      the rest of the round, and its streams are not counted against;
    - then, before every stream, a login nobody is using: a viewer can move onto this
      provider at any time, and a check never takes a login from one. With every login of
      the account in use, that provider waits and the others go on;
    - and when its first streams all fail (account_failures in a row), the provider is
      taken to be down rather than its streams, and left for the rest of the round.

    Returns why it ended: "more" (time is up and there is more: the caller queues the next
    batch at once), "waiting" (what is left is on logins in use, or the window is closed:
    the next tick carries on), "done", "stopped", or "already running".

    In batches because a worker held for the hours a round can take is a worker the
    playlist and guide refreshes cannot have.
    """
    from apps.m3u.connection_pool import release_profile_slot
    from django.db import connections

    settings = load_settings()
    if not redis_client.set(RUN_KEY, "1", nx=True, ex=RUN_TTL):
        return "already running"
    if only is None:
        redis_client.delete(QUEUED_KEY)
    try:
        round_ = None if only is not None else current_round(redis_client)
        if only is None and round_ is None:
            return "done"
        scheduled = round_ is not None and not round_.get("forced")
        if scheduled and not in_window(settings):
            _progress(redis_client, waiting=True, message="Waiting for its window")
            return "waiting"
        if redis_client.exists(YIELD_KEY):
            # A viewer asked for a connection a moment ago; give them the moment
            _progress(redis_client, waiting=True, message="Making way for a viewer")
            return "waiting"
        idle_only = bool(settings.get("only_when_idle"))
        if idle_only and _Providers(redis_client, profiles=[]).anything_playing():
            _progress(redis_client, waiting=True, message="Waiting until nothing is playing")
            return "waiting"
        _progress(redis_client, waiting=False, message="", next_batch_at="")

        unavailable = dict((round_ or {}).get("unavailable") or {})
        by_account = _targets(
            settings, only=only, due_before=round_["since"] if round_ else None, skip_accounts=unavailable
        )
        if not by_account:
            return _finish(redis_client, round_) if round_ else "done"
        deadline = time.monotonic() + float(batch_seconds or BATCH_SECONDS)
        results = load_results()["streams"]
        lock = threading.Lock()
        # Everything the providers' threads need from the database, read here: a thread has
        # a database connection of its own, and all it should do is talk to providers
        providers = _Providers(redis_client)
        profiles = {a: _profiles_of(streams[0].m3u_account) for a, streams in by_account.items()}
        agents = {a: streams[0].m3u_account.get_user_agent_string() or "" for a, streams in by_account.items()}
        was_parked = {str(i) for i in parked_ids()}
        own_connection = connections["default"]
        give_up_after = int(settings.get("account_failures") or 5)
        recovered = []
        stopped = {"asked": False}
        waiting = set()

        # One thread per provider, not per account: two accounts of one provider checked at
        # once would be two connections to it
        by_provider = {}
        for account_id, streams in by_account.items():
            by_provider.setdefault(providers.provider_of(account_id), []).extend(streams)
        names = {
            key: " + ".join(sorted({s.m3u_account.name for s in streams}))
            for key, streams in by_provider.items()
        }
        accounts = progress(redis_client).get("accounts") or {}
        for key, streams in by_provider.items():
            entry = accounts.setdefault(_key_text(key), {"name": names[key], "done": 0})
            entry.update(name=names[key], left=len(streams), now="", status="checking", reason="")

        def stop_asked():
            return bool(redis_client.exists(STOP_KEY)) or (scheduled and not in_window(settings))

        def someone_watching():
            # With only_when_idle, anything playing anywhere ends every provider's part
            return idle_only and providers.anything_playing()

        def set_status(entry, status, reason=""):
            with lock:
                entry.update(status=status, reason=reason, now="")
                _progress(redis_client, accounts=accounts)

        def count(stream, outcome):
            """What was found kept, under the lock."""
            record = _record(redis_client, results, stream, outcome, settings)
            if record["state"] == "broken":
                _progress(redis_client, broken=progress(redis_client).get("broken", 0) + 1)
            if outcome["ok"] and str(stream.id) in was_parked:
                recovered.append(stream.id)

        def one_provider(key, streams):
            entry = accounts[_key_text(key)]
            logins = {}
            # Per account: failures before anything of it played, held back until it is
            # clear whether its streams failed or the account is down
            held, played, given_up = {}, set(), set()
            xc_logins = []
            try:
                for account_id in dict.fromkeys(s.m3u_account_id for s in streams):
                    account = next(s.m3u_account for s in streams if s.m3u_account_id == account_id)
                    usable, problem = _usable_logins(account, profiles[account_id], agents[account_id])
                    if not usable:
                        _unavailable(redis_client, round_, account_id, account.name, problem, lock)
                        given_up.add(account_id)
                        continue
                    logins[account_id] = usable
                    if account.account_type == "XC":
                        xc_logins.extend((account, p, agents[account_id]) for p in usable)
                provider_count = _ProviderCount(xc_logins)
                refusals_in_a_row = 0
                gap = float(settings["gap_seconds"])
                if not logins:
                    set_status(entry, "unavailable", "no account of it can be used")
                    return

                for stream in streams:
                    account_id = stream.m3u_account_id
                    if account_id in given_up:
                        continue
                    tries = 0
                    while True:
                        tries += 1
                        if stop_asked():
                            stopped["asked"] = bool(redis_client.exists(STOP_KEY))
                            waiting.add(key)
                            return
                        if redis_client.exists(YIELD_KEY):
                            waiting.add(key)
                            set_status(entry, "waiting", "making way for a viewer")
                            return
                        if someone_watching():
                            waiting.add(key)
                            set_status(entry, "waiting", "something is playing")
                            return
                        # Nobody on this provider, as Dispatcharr sees it and, for Xtream Codes,
                        # as the provider itself sees it
                        if providers.in_use(key):
                            waiting.add(key)
                            set_status(entry, "in use", "someone is watching through it")
                            return
                        if not provider_count.free(lambda: stop_asked() or someone_watching() or providers.in_use(key)):
                            waiting.add(key)
                            set_status(entry, "in use", "the provider says one of its logins is in use")
                            return
                        profile = _take_connection(logins[account_id], redis_client)
                        if profile is None:
                            waiting.add(key)
                            set_status(entry, "in use", "no connection of it is free")
                            return
                        if providers.in_use(key, holding=profile):
                            # A viewer came onto it between the asking and the taking
                            if profile.max_streams > 0:
                                release_profile_slot(profile.id, redis_client)
                            waiting.add(key)
                            set_status(entry, "in use", "someone started watching through it")
                            return
                        redis_client.expire(RUN_KEY, RUN_TTL)
                        with lock:
                            entry.update(now=stream.name, status="checking", reason="")
                            _progress(redis_client, accounts=accounts)

                        def should_stop(profile=profile):
                            # Cut short: asked to stop, a viewer needs a connection, or someone
                            # started watching through this provider
                            return (
                                stop_asked()
                                or bool(redis_client.exists(YIELD_KEY))
                                or someone_watching()
                                or providers.in_use(key, holding=profile)
                            )

                        outcome = None
                        try:
                            url = _url_for(stream, profile)
                            if url and url.startswith(("http://", "https://")):
                                outcome = probe(url, agents[account_id], settings["timeout_seconds"], should_stop)
                        except Stopped:
                            # Looked at again once the viewer is done, and not counted
                            waiting.add(key)
                            set_status(entry, "in use", "someone started watching through it")
                            return
                        finally:
                            # An unlimited profile took no slot, so there is none to give back;
                            # releasing anyway would free one a viewer holds
                            if profile.max_streams > 0:
                                release_profile_slot(profile.id, redis_client)
                            provider_count.check_ended()

                        if outcome is not None and outcome.get("refused"):
                            # The provider would not give this one: often for a moment. Once
                            # more after a pause; then it is noted as not checked -- never
                            # counted against the stream -- and the next is tried.
                            if tries == 1:
                                with lock:
                                    entry.update(status="checking", reason=f"refused, trying again in {REFUSAL_PAUSE} s ({outcome['reason']})")
                                    _progress(redis_client, accounts=accounts)
                                # Interrupted or not, the checks at the top decide what next
                                _pause(REFUSAL_PAUSE, lambda: stop_asked() or someone_watching() or providers.in_use(key))
                                continue
                            refusals_in_a_row += 1
                            gap = min(SLOWEST_GAP, max(gap * 2, 5.0))
                            with lock:
                                _refused(results, stream, outcome["reason"])
                                entry["done"] += 1
                                entry["left"] -= 1
                                _progress(redis_client, accounts=accounts, done=progress(redis_client).get("done", 0) + 1)
                            if refusals_in_a_row >= REFUSALS_IN_A_ROW:
                                # Not a stream or two: the provider will not give connections now
                                waiting.add(key)
                                set_status(
                                    entry, "in use",
                                    f"the provider refused {refusals_in_a_row} streams in a row ({outcome['reason']}); trying again later",
                                )
                                return
                            outcome = "refused"
                        break

                    if outcome == "refused":
                        _pause(gap, lambda: stop_asked() or someone_watching() or providers.in_use(key))
                        continue
                    refusals_in_a_row = 0
                    gap = max(float(settings["gap_seconds"]), gap * 0.75)
                    account_held = held.setdefault(account_id, [])
                    with lock:
                        if outcome is None:
                            # Not a URL that can be opened: noted as looked at, so the round
                            # moves on
                            _touch(results, stream)
                        elif outcome["ok"]:
                            played.add(account_id)
                            for earlier, found in account_held:
                                count(earlier, found)
                            account_held.clear()
                            count(stream, outcome)
                        elif account_id in played:
                            count(stream, outcome)
                        else:
                            account_held.append((stream, outcome))
                        entry["done"] += 1
                        entry["left"] -= 1
                        _progress(redis_client, accounts=accounts, done=progress(redis_client).get("done", 0) + 1)

                    if len(account_held) >= give_up_after:
                        reason = f"its first {len(account_held)} streams all failed ({account_held[-1][1]['reason']})"
                        with lock:
                            for earlier, _ in account_held:
                                _touch(results, earlier)
                        account_held.clear()
                        given_up.add(account_id)
                        _unavailable(redis_client, round_, account_id, stream.m3u_account.name, reason, lock)
                        set_status(entry, "checking", "")
                    time.sleep(gap)
                    # After a stream rather than before, so every batch gets somewhere
                    if time.monotonic() > deadline:
                        return
            except Exception:
                logger.exception(f"Stream Check: provider {names.get(key)} stopped on an error")
            finally:
                # Fewer failures than it takes to call an account down: they are the streams'
                with lock:
                    for account_id, account_held in held.items():
                        if account_id not in given_up:
                            for earlier, found in account_held:
                                count(earlier, found)
                if entry.get("status") == "checking":
                    set_status(entry, "done" if entry["left"] <= 0 else "checking")
                # Where accounts share a login (a server group), taking a connection reads
                # the database, which gives this thread a connection of its own to close.
                # Under gevent a "thread" can share the batch's own, which must stay open.
                if connections["default"] is not own_connection:
                    connections["default"].close()

        threads = [
            threading.Thread(target=one_provider, args=(key, streams), daemon=True, name="stream-check")
            for key, streams in by_provider.items()
        ]
        try:
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                redis_client.expire(RUN_KEY, RUN_TTL)
                for thread in threads:
                    thread.join(timeout=5)
        finally:
            if settings.get("restore_recovered"):
                for stream_id in recovered:
                    try:
                        restore(stream_id)
                        logger.info(f"Stream Check: parked stream {stream_id} works again and was put back")
                    except ValueError as e:
                        logger.info(f"Stream Check: parked stream {stream_id} works again, not put back: {e}")
            _keep_results(results)
            redis_client.delete(LIVE_RESULTS_KEY)

        if only is not None:
            return "done"
        if stopped["asked"]:
            return _stopped(redis_client)
        round_ = current_round(redis_client) or round_
        left = _targets(settings, due_before=round_["since"], skip_accounts=round_.get("unavailable") or {})
        if not left:
            return _finish(redis_client, round_)
        if {providers.provider_of(a) for a in left} <= waiting:
            # Everything left is on providers that cannot be used now: in use, or refusing
            reasons = "; ".join(
                f"{e['name']}: {e['reason']}" for e in accounts.values()
                if e.get("reason") and e.get("status") in ("in use", "waiting")
            )
            _progress(redis_client, waiting=True, message=reasons or "Waiting for viewers to finish")
            return "waiting"
        return "more"
    finally:
        redis_client.delete(RUN_KEY)


def _key_text(key):
    """A provider's key as the page keeps it: "server:example.com", "login:…", "account:3"."""
    return f"{key[0]}:{key[1]}"


def _unavailable(redis_client, round_, account_id, name, reason, lock):
    """An account left for the rest of the round, and why, for the page to say."""
    logger.info(f"Stream Check: {name} left for this round: {reason}")
    if round_ is None:
        return
    with lock:
        current = current_round(redis_client) or round_
        current.setdefault("unavailable", {})[str(account_id)] = {"name": name, "reason": reason}
        round_.setdefault("unavailable", {})[str(account_id)] = {"name": name, "reason": reason}
        redis_client.set(ROUND_KEY, json.dumps(current), ex=ROUND_TTL)


def _pause(seconds, interrupted):
    """Wait, unless interrupted says to stop. True when the wait ran its course."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if interrupted():
            return False
        time.sleep(min(1.0, max(0.0, until - time.monotonic())))
    return True


def _refused(results, stream, reason):
    """A stream the provider would not give: looked at, not checked, and said why."""
    _touch(results, stream)
    results[str(stream.id)] = {**results[str(stream.id)], "refused": reason}


def _touch(results, stream):
    previous = results.get(str(stream.id))
    if previous:
        results[str(stream.id)] = {**previous, "checked_at": _now()}
    else:
        results[str(stream.id)] = {
            "ok": True, "reason": "", "resolution": "", "codec": "", "seconds": 0, "checked_at": _now(),
            "last_ok": "", "failures": 0, "history": [], "name": stream.name, "state": "unchecked",
            "skipped": True,
        }


def _finish(redis_client, round_):
    redis_client.delete(QUEUED_KEY)
    done = progress(redis_client).get("done", 0)
    _keep_last_run({
        "finished_at": _now(), "checked": done, "total": (round_ or {}).get("total", done),
        # The providers whose streams were not looked at, and why
        "unavailable": list(((current_round(redis_client) or round_ or {}).get("unavailable") or {}).values()),
    })
    redis_client.delete(ROUND_KEY)
    _progress(redis_client, state="done", finished_at=_now(), waiting=False, message="")
    logger.info(f"Stream Check: round finished, {done} streams looked at")
    return "done"


def _stopped(redis_client):
    redis_client.delete(QUEUED_KEY)
    redis_client.delete(ROUND_KEY, STOP_KEY)
    done = progress(redis_client).get("done", 0)
    _keep_last_run({"finished_at": _now(), "checked": done, "total": progress(redis_client).get("total", done), "stopped": True})
    _progress(redis_client, state="stopped", finished_at=_now(), waiting=False, message="")
    return "stopped"


def _keep_results(results):
    # Streams gone from Dispatcharr altogether are not kept
    from .models import Stream

    existing = {str(i) for i in Stream.objects.filter(id__in=[int(i) for i in results]).values_list("id", flat=True)}
    _store(
        RESULTS_KEY, "Stream Check results",
        {"streams": {k: v for k, v in results.items() if k in existing}, "last_run": load_results()["last_run"]},
    )


def _keep_last_run(last_run):
    stored = load_results()
    _store(RESULTS_KEY, "Stream Check results", {"streams": stored["streams"], "last_run": last_run})


def current_results(redis_client):
    """What is known, with what the batch going now has found over what was kept."""
    results = load_results()
    try:
        for stream_id, raw in (redis_client.hgetall(LIVE_RESULTS_KEY) or {}).items():
            key = stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
            results["streams"][key] = json.loads(raw)
    except Exception as e:
        logger.debug(f"Could not read the live Stream Check results: {e}")
    return results


def is_running(redis_client):
    """A round going, between batches or in one, or a single check."""
    return bool(redis_client.exists(ROUND_KEY) or redis_client.exists(RUN_KEY))


def request_stop(redis_client):
    """End the round: the batch going stops within a second, and no other is started."""
    if redis_client.exists(RUN_KEY):
        redis_client.set(STOP_KEY, "1", ex=3600)
        return True
    if redis_client.exists(ROUND_KEY):
        _stopped(redis_client)
        return True
    return False


def due(settings, redis_client, now=None):
    """Whether a round should begin by itself now."""
    if not settings.get("enabled") or is_running(redis_client):
        return False
    if not in_window(settings, now):
        return False
    last = load_results()["last_run"].get("finished_at") or ""
    if not last:
        return True
    try:
        finished = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - finished).total_seconds() >= float(settings["every_hours"]) * 3600


# ── What a person decides ────────────────────────────────────────────────────


def _update_parked(change):
    """Read, change and write the parked streams in one go, so two clicks cannot race."""
    from django.db import transaction

    from core.models import CoreSettings

    with transaction.atomic():
        row, _ = CoreSettings.objects.select_for_update().get_or_create(
            key=PARKED_KEY, defaults={"name": "Stream Check parked streams", "value": {}}
        )
        parked = dict(row.value or {})
        answer = change(parked)
        row.value = parked
        row.save(update_fields=["value"])
    return answer


def park(stream_id, reason=""):
    """
    Take a stream off every channel it is on, remembering where it was, so it can be put
    back. The streams after it close up, as they would had it been removed by hand.
    """
    from django.db import transaction

    from .models import ChannelStream, Stream

    stream = Stream.objects.filter(id=stream_id).first()
    if stream is None or stream.is_custom:
        raise ValueError("Only a provider's stream can be parked")

    def change(parked):
        with transaction.atomic():
            links = list(ChannelStream.objects.filter(stream_id=stream_id).values("channel_id", "order"))
            was = (parked.get(str(stream_id)) or {}).get("channels") or []
            places = {p["channel"]: p["order"] for p in was}
            places.update({link["channel_id"]: link["order"] for link in links})
            ChannelStream.objects.filter(stream_id=stream_id).delete()
            for link in links:
                _close_up(link["channel_id"])
            parked[str(stream_id)] = {
                "channels": [{"channel": c, "order": o} for c, o in sorted(places.items())],
                "parked_at": (parked.get(str(stream_id)) or {}).get("parked_at") or _now(),
                "name": stream.name,
                "reason": reason,
            }
            return len(links)

    return _update_parked(change)


def restore(stream_id):
    """
    Put a parked stream back on the channels it came off, where it was: before the
    fallback, which stays last whatever the order kept says. A channel deleted since is
    passed over.
    """
    from django.db import transaction

    from .models import Channel, ChannelStream, Stream

    if not Stream.objects.filter(id=stream_id).exists():
        raise ValueError("That stream is gone: the provider no longer lists it")

    def change(parked):
        entry = parked.pop(str(stream_id), None)
        if not entry:
            return 0
        put = 0
        with transaction.atomic():
            for place in entry.get("channels") or []:
                if not Channel.objects.filter(id=place["channel"]).exists():
                    continue
                current = list(
                    ChannelStream.objects.filter(channel_id=place["channel"])
                    .select_related("stream").order_by("order")
                )
                if any(link.stream_id == stream_id for link in current):
                    continue
                real = [link for link in current if not link.stream.is_custom]
                custom = [link for link in current if link.stream.is_custom]
                at = min(int(place.get("order") or 0), len(real))
                ordered = [link.stream_id for link in real]
                ordered.insert(at, stream_id)
                ordered += [link.stream_id for link in custom]
                ChannelStream.objects.create(channel_id=place["channel"], stream_id=stream_id, order=at)
                for order, sid in enumerate(ordered):
                    ChannelStream.objects.filter(channel_id=place["channel"], stream_id=sid).update(order=order)
                put += 1
        return put

    return _update_parked(change)


def clear_results():
    """Forget everything the runs found: every stream is unchecked again. Parked stay parked."""
    _store(RESULTS_KEY, "Stream Check results", {"streams": {}, "last_run": {}})


def forget(stream_id):
    """Stop keeping a parked stream: it stays off its channels, for good."""
    return _update_parked(lambda parked: 1 if parked.pop(str(stream_id), None) else 0)


def remove(stream_id, channel_id=None):
    """
    Take a stream off a channel, or off every channel it is on, for good. The stream itself
    stays: it is the provider's, and the next playlist refresh would bring it back anyway.
    """
    from django.db import transaction

    from .models import ChannelStream, Stream

    if Stream.objects.filter(id=stream_id, is_custom=True).exists():
        raise ValueError("A custom stream is a channel's fallback, and is not removed from here")
    with transaction.atomic():
        links = ChannelStream.objects.filter(stream_id=stream_id)
        if channel_id is not None:
            links = links.filter(channel_id=channel_id)
        channels = list(links.values_list("channel_id", flat=True))
        links.delete()
        for channel in channels:
            _close_up(channel)
    return len(channels)


def _close_up(channel_id):
    from .models import ChannelStream

    for order, link in enumerate(ChannelStream.objects.filter(channel_id=channel_id).order_by("order")):
        if link.order != order:
            ChannelStream.objects.filter(id=link.id).update(order=order)


# ── What the page shows ──────────────────────────────────────────────────────


def issues(redis_client, show="problems"):
    """
    The channels with a stream that does not play, each with all its streams as last
    found, and the parked streams. show: "problems" (failing or broken), "broken", or
    "all" (every channel looked at).
    """
    from .models import Channel, ChannelStream, Stream

    settings = load_settings()
    results = current_results(redis_client)["streams"]
    parked = load_parked()

    def state(stream_id):
        return state_of(results.get(str(stream_id)), settings) if str(stream_id) in results else "unchecked"

    links = list(
        ChannelStream.objects.select_related("stream", "stream__m3u_account", "channel", "channel__channel_group", "channel__logo")
        .order_by("channel__channel_number", "channel_id", "order")
    )
    by_channel = {}
    for link in links:
        by_channel.setdefault(link.channel_id, {"channel": link.channel, "links": []})["links"].append(link)

    rows = []
    for channel_id, found in by_channel.items():
        channel = found["channel"]
        streams = [_stream_row(link.stream, results, state(link.stream_id)) for link in found["links"]]
        real = [s for s in streams if not s["custom"]]
        bad = [s for s in real if s["state"] in ("failing", "broken")]
        broken = [s for s in real if s["state"] == "broken"]
        working = [s for s in real if s["state"] == "ok"]
        if show == "broken" and not broken:
            continue
        if show == "problems" and not bad:
            continue
        if show == "all" and all(s["state"] == "unchecked" for s in real):
            continue
        rows.append({
            "key": f"ch:{channel_id}",
            "kind": "channel",
            "channel": {
                "id": channel.id,
                "name": channel.name,
                "number": channel.channel_number,
                "group": channel.channel_group.name if channel.channel_group_id else "",
                "logo_url": channel.logo.url if channel.logo_id else "",
            },
            "streams": streams,
            "broken": len(broken),
            "failing": len(bad) - len(broken),
            "working": len(working),
            # Nothing left that plays: a viewer gets the fallback or nothing
            "dead": bool(real) and not working and len(broken) == len(real),
        })

    names = {c.id: c.name for c in Channel.objects.filter(id__in={p["channel"] for e in parked.values() for p in e.get("channels") or []})}
    known = {s.id: s for s in Stream.objects.select_related("m3u_account").filter(id__in=[int(i) for i in parked])}
    parked_rows = []
    for stream_id, entry in parked.items():
        stream = known.get(int(stream_id))
        row = _stream_row(stream, results, state(stream_id)) if stream else {
            "id": int(stream_id), "name": entry.get("name", ""), "account": "", "gone": True,
            "state": "gone", "custom": False, "hash": "", "result": None,
        }
        row["parked_at"] = entry.get("parked_at", "")
        row["from"] = [
            {"id": p["channel"], "name": names.get(p["channel"], "a deleted channel"), "order": p.get("order", 0)}
            for p in entry.get("channels") or []
        ]
        parked_rows.append(row)
    parked_rows.sort(key=lambda r: r["name"].lower())
    return {"rows": rows, "parked": parked_rows}


def _stream_row(stream, results, state):
    result = results.get(str(stream.id))
    return {
        "id": stream.id,
        "name": stream.name,
        "account": stream.m3u_account.name if stream.m3u_account_id else "custom",
        "custom": bool(stream.is_custom),
        "hash": stream.stream_hash or "",
        "state": "fallback" if stream.is_custom else state,
        "result": result,
    }
