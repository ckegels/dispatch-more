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
- Under what each provider allows. Many refuse every stream for a while after a number of
  them are opened in a row, however slowly; each has its own number and none says what it
  is. A stream it refuses is tried against one that just played there: if that plays, it
  is that one stream; if not, the provider is at its limit. Then it is left alone -- not
  asked anything -- and tried again after longer and longer waits, which says how long the
  block lasts, and from then on the checks stay under the limit with room to spare (see
  _Budget). A limit can be typed in instead, which spares the provider even the first.
- Every failure has its kind (see KIND_TEXT): it does not play at all; the provider refuses
  it while giving its other streams; or it plays, but the picture is black, does not move,
  or is the provider's own "no stream" card -- the same still picture on several of its
  channels. All are checked again; only the first may ever be parked by autopark. The
  others play something, or are the provider's doing, and are left for a person.
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
# Failing streams whose provider's playlist was refreshed since, to be looked at again
RECHECK_KEY = "stream-check-recheck"
# The channels Stream Check hid because every real stream of theirs was parked, and only
# those: a channel hidden by a person is never shown again by Stream Check
HIDDEN_KEY = "stream-check-hidden"
PARKED_KEY = "stream-check-parked"
# Streams a person said to leave alone: off the list, and not checked again until they say
# otherwise. Nothing on the channels changes.
IGNORED_KEY = "stream-check-ignored"

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
    # person to decide. One parked by autopark is always put back when it works again.
    "restore_recovered": False,
    # A stream that failed is looked at again by itself, without waiting for the next full
    # round: every recheck_hours ("hours"), or after each playlist refresh of its provider
    # ("refresh") -- providers often mend a channel at their end, and it shows then
    "recheck_failed": True,
    "recheck_mode": "hours",
    "recheck_hours": 3,
    # Look a few seconds into every stream for a picture that is black, does not move, or is
    # the provider's "no stream" card. Each check takes that much longer.
    "picture_check": True,
    "picture_seconds": 6,
    # A stream that played last time and whose picture was looked at within this many days gets
    # the quick check (does video come) rather than the picture look: most of a run's time.
    # 0 looks at every picture on every run.
    "picture_every_days": 3,
    # A picture fault is looked at again later in the same run before it counts (see RELOOKS)
    "relook_pictures": True,
    # A channel whose every real stream is parked is hidden from what TVs and media servers
    # get -- the playlist, the guide, the HDHomeRun lineup, Xtream Codes -- rather than shown
    # with nothing but its fallback to play; shown again when a stream of it is put back
    "hide_emptied_channels": True,
    # Park a stream by itself once it has failed this many checks in a row -- only a stream
    # that does not play at all; one refused, black, frozen or showing the provider's card is
    # left for a person. Off unless turned on. After a refresh, three checks means at least
    # two refreshes went by.
    "autopark": False,
    "autopark_after": 3,
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
# A server in trouble rather than a stream gone: looked at again later in the run, like a picture
# fault, and counted only when it happens again
TRANSIENT_STATUS = {500, 502, 504, 520, 521, 522, 523, 524}
REFUSED_STATUS = {401, 403, 406, 407, 423, 429, 456, 458, 503, 509, 512, 513, 551, 882, 884}
# A refused stream is told apart from a provider at its limit by trying, this long after, a
# stream of the same provider that just played
REFUSAL_PAUSE = 3
# What a provider allows is kept here, learned or set by hand: {provider: {...}}
LIMITS_KEY = "stream-check-providers"
# The streams opened on a provider lately, as times: what its limit is counted against
OPENS_KEY = "stream-check:opens:{provider}"
# After a provider's limit is hit, when to try again to learn how long it lasts: the
# provider is not asked anything in between, not even about its logins
RECOVERY_STEPS = (30, 60, 120, 240, 480, 900, 1800, 3600)
# What is learned is used with room to spare, for viewers zapping meanwhile
LIMIT_MARGIN = 0.8
# Limits learned before a refused channel could be told from a provider at its limit may be
# nothing but a dead channel: they are not used. Limits set by hand are always kept.
LIMITS_VERSION = 2
# A provider can go on counting a connection for a while after it is closed. After a check,
# a login the provider still counts as in use is waited on this long before it is taken to
# be someone else's.
PROVIDER_LINGER = 45
# How often the provider is asked how many connections a login has open, at most. Not before
# every stream: asking is a request too, and a provider that limits requests per minute
# counted them. Measured on a real provider, a closed check is off its count at once.
PROVIDER_ASK_EVERY = 30

# How much of a stream is read before it is judged: enough for ffprobe to find the picture
READ_BYTES = 1024 * 1024
# To judge the picture itself -- black, not moving, the provider's "no stream" card -- a few
# seconds of it are needed: this much at most
PICTURE_BYTES = 40 * 1024 * 1024
# A picture black for this long, or not moving for this long, is a failure (of the seconds
# looked at); a recording shorter than PICTURE_LEAST says nothing either way
BLACK_SECONDS = 3
FROZEN_SECONDS = 4
# ...and for most of what was seen: a burst can hold thirty seconds, and three black or four
# still ones in there are a fade, a scene change or an ad break, not a channel gone
PICTURE_FAULT_SHARE = 0.8
PICTURE_LEAST = 3
# The same frozen picture on this many channels of one provider is the provider's own card
PLACEHOLDER_CHANNELS = 3
# A picture still for a few seconds is only a suspicion: a news desk, a slide or a quiet
# scene can be. It is watched longer before it is called frozen -- still for this share of
# that time, and snapshots taken every SNAPSHOT_EVERY seconds all the same picture (no more
# than SNAPSHOT_SAME_BITS of the 144 in their fingerprints different). Anything moving, and it
# plays.
FROZEN_CONFIRM_SHARE = 0.9
SNAPSHOT_EVERY = 5
SNAPSHOT_SAME_BITS = 8
CONFIRM_BYTES = 96 * 1024 * 1024
# A picture fault found by the quick check is only a suspicion: a news desk sits still, a
# scene goes dark. The same run opens the stream again, RELOOK_GAP seconds apart. Seen again,
# it is a failure; RELOOKS clean looks in a row, and it plays.
RELOOKS = 3
RELOOK_GAP = 90

# What kind of failure a check found. Only "dead" -- it does not play at all -- may ever be
# parked by autopark; the others play something, or are the provider's refusal, and are left
# for a person to decide.
DEAD, REFUSED, BLACK, FROZEN, PLACEHOLDER = "dead", "refused", "black", "frozen", "placeholder"
PICTURE_KINDS = (BLACK, FROZEN, PLACEHOLDER)
# No connection to the provider at all: the stream was never reached, so it says nothing about
# it. Looked at again in the same run like a picture fault, and never counted: after RELOOKS
# tries it is noted as not checked, and the next run looks again.
UNREACHABLE = "unreachable"
KIND_TEXT = {
    DEAD: "does not play",
    REFUSED: "refused by the provider",
    BLACK: "black picture",
    FROZEN: "picture does not move",
    PLACEHOLDER: "shows the provider's \"no stream\" picture",
}
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
        values["recheck_hours"] = min(168, max(0.5, float(values["recheck_hours"])))
        values["autopark_after"] = max(2, int(values["autopark_after"]))
        values["picture_seconds"] = min(20, max(PICTURE_LEAST + 1, float(values["picture_seconds"])))
        values["picture_every_days"] = min(60, max(0, float(values["picture_every_days"])))
    except (TypeError, ValueError):
        raise ValueError("Numbers only, please")
    if values["recheck_mode"] not in ("hours", "refresh"):
        raise ValueError("Recheck every so many hours, or after each playlist refresh")
    for field in ("window_from", "window_to"):
        if values[field] and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(values[field])):
            raise ValueError("Times as HH:MM, please")
    _store(SETTINGS_KEY, "Stream Check", {**values, "version": SETTINGS_VERSION})
    return values


def load_results():
    """{"streams": {stream id: result}, "last_run": {...}}, as the last run left them."""
    stored = _load(RESULTS_KEY, {})
    return {"streams": dict(stored.get("streams") or {}), "last_run": stored.get("last_run") or {}}


def load_ignored():
    """{stream id: {"name", "ignored_at", "reason"}}: streams a person said to leave alone."""
    return dict(_load(IGNORED_KEY, {}))


def ignore(stream_id):
    """
    Leave a stream alone: off the list, and not checked again, until a person says otherwise.
    Nothing on its channels changes -- it is not parked, not removed.
    """
    from .models import Stream

    stream = Stream.objects.filter(id=stream_id).first()
    if stream is None or stream.is_custom:
        raise ValueError("Only a provider's stream can be ignored")
    result = load_results()["streams"].get(str(stream_id)) or {}

    def change(ignored):
        ignored[str(stream_id)] = {
            "name": stream.name,
            "ignored_at": _now(),
            "reason": (result.get("suspect") or {}).get("reason") or result.get("reason", ""),
        }

    _change_key(IGNORED_KEY, "Stream Check ignored streams", change)
    return 1


def unignore(stream_id):
    """Check a stream again, and show it again when it fails."""
    found = {}

    def change(ignored):
        found["was"] = ignored.pop(str(stream_id), None)

    _change_key(IGNORED_KEY, "Stream Check ignored streams", change)
    return 1 if found.get("was") else 0


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

    # Each provider is keyed by a name that does not depend on the order accounts were
    # joined in: its server when it has one -- the first by name, if several -- otherwise
    # the smallest of what joined it. What a provider allows is kept under this key, and
    # must still be found next time.
    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    stable = {}
    for root, nodes in groups.items():
        servers = sorted(n for n in nodes if n[0] == "server")
        stable[root] = servers[0] if servers else min(nodes, key=lambda n: (n[0], str(n[1])))

    key_of, members = {}, {}
    for profile in profiles:
        node = ("account", profile.m3u_account_id)
        key = stable[find(node)]
        key_of[node] = key
        members.setdefault(key, []).append(profile)
    return key_of, members


class _ProviderCount:
    """
    How many connections an Xtream Codes provider says its logins have open, asked before
    every stream: the provider sees every viewer, in Dispatcharr or in any other app. A
    provider that says any of its logins is in use is not checked.
    """

    def __init__(self):
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

    def free(self, logins, should_give_up):
        """
        Whether the provider says nobody is on any of its logins. Right after a check it
        may still count that one: then it is waited on, up to PROVIDER_LINGER, rather than
        taken to be a viewer. A provider that cannot be asked is taken to be in use.
        """
        while True:
            # [(account, profile, user agent)]: the provider's Xtream Codes logins known to work
            counts = [self._open(*login) for login in logins]
            if all(count == 0 for count in counts):
                return True
            if any(count is None for count in counts):
                return False
            ended = self.last_check_ended
            if ended is None or time.monotonic() - ended > PROVIDER_LINGER or should_give_up():
                return False
            time.sleep(PROVIDER_ASK_EVERY)

    def check_ended(self):
        # What was asked stays good until PROVIDER_ASK_EVERY has passed: our own check is off
        # the provider's count as soon as it closes, and anyone else is seen at the next ask
        self.last_check_ended = time.monotonic()


# ── What each provider allows ────────────────────────────────────────────────


def load_limits():
    """{provider key: {"name", "limit", "window", "how", ...}}, learned or set by hand."""
    limits = {}
    for key, state in _load(LIMITS_KEY, {}).items():
        state = dict(state or {})
        if state.get("how") != "set by hand" and state.get("v") != LIMITS_VERSION:
            # Learned (or being learned) the old way: kept only for the stream that played
            state = {k: v for k, v in state.items() if k in ("name", "good_stream")}
        limits[key] = state
    return limits


def _change_limits(change):
    """Read, change and write what providers allow in one go, so two threads cannot race."""
    from django.db import transaction

    from core.models import CoreSettings

    with transaction.atomic():
        row, _ = CoreSettings.objects.select_for_update().get_or_create(
            key=LIMITS_KEY, defaults={"name": "Stream Check provider limits", "value": {}}
        )
        limits = dict(row.value or {})
        answer = change(limits)
        row.value = limits
        row.save(update_fields=["value"])
    return answer


def set_limit(key, name, limit=None, window_minutes=None):
    """A provider's limit set by hand, or with no limit given, forgotten so it is learned again."""
    def change(limits):
        if limit:
            limits[key] = {
                "name": name, "limit": max(1, int(limit)), "window": max(60, int(float(window_minutes or 10) * 60)),
                "how": "set by hand", "at": _now(),
            }
        else:
            limits.pop(key, None)
        return limits.get(key)

    return _change_limits(change)


class _Budget:
    """
    How many streams a provider lets be opened in a while, and keeping under it.

    Providers limit how many channels a login may open in a stretch of time -- one refused
    every stream with HTTP 407 after 34, whatever the pace -- and each has its own number.
    None is assumed: a limit is learned the first time it is hit, how long its block lasts is
    learned by trying again after longer and longer waits, and from then on the checks stay
    under it, with room to spare. A limit set by hand is used as it is and not relearned.
    """

    def __init__(self, redis_client, key, name, state=None):
        self.redis = redis_client
        self.key = key
        self.name = name
        self.state = dict(state or {})
        self.changed = False

    def _save(self):
        # Kept by the batch once its threads are done (see save): a thread only talks to
        # providers and Redis, never the database
        self.changed = True

    def save(self):
        if not self.changed:
            return
        state = dict(self.state)

        def change(limits):
            limits[self.key] = {**state, "name": self.name}

        _change_limits(change)
        self.changed = False

    def note_open(self):
        now = time.time()
        opens = OPENS_KEY.format(provider=self.key)
        self.redis.zadd(opens, {f"{now:.6f}": now})
        self.redis.zremrangebyscore(opens, 0, now - 4 * 3600)
        self.redis.expire(opens, 4 * 3600)

    def _opens(self, since):
        return self.redis.zcount(OPENS_KEY.format(provider=self.key), since, "+inf")

    def resting(self):
        """Seconds until the provider may be tried again after hitting its limit, 0 if it may
        be now, or None when it is not resting at all."""
        if not self.state.get("blocked_since"):
            return None
        return max(0.0, float(self.state.get("next_try", 0)) - time.time())

    def wait(self):
        """Seconds until one more stream may be opened without going over the limit."""
        limit, window = self.state.get("limit"), self.state.get("window")
        if not limit or not window:
            return 0.0
        now = time.time()
        if self._opens(now - window) < limit:
            return 0.0
        first = self.redis.zrangebyscore(OPENS_KEY.format(provider=self.key), now - window, "+inf", start=0, num=1, withscores=True)
        return max(1.0, (first[0][1] + window - now) if first else window)

    def hit(self):
        """The limit was reached: count what it took, and start finding out how long it lasts."""
        now = time.time()
        since = float(self.state.get("clear_since") or now - 3600)
        count = self._opens(since)
        first = self.redis.zrangebyscore(OPENS_KEY.format(provider=self.key), since, "+inf", start=0, num=1, withscores=True)
        self.state.update(
            blocked_since=now, count=int(count), span=(now - first[0][1]) if first else 0.0,
            step=0, next_try=now + RECOVERY_STEPS[0], v=LIMITS_VERSION,
        )
        self._save()
        logger.info(f"Stream Check: {self.name} refused after {count} streams; finding out how long for")

    def still_blocked(self):
        step = min(int(self.state.get("step", 0)) + 1, len(RECOVERY_STEPS) - 1)
        self.state.update(step=step, next_try=time.time() + RECOVERY_STEPS[step])
        self._save()

    def recovered(self):
        """Answering again: the limit is what was opened, the window how long that took plus
        how long the block lasted -- used with room to spare."""
        now = time.time()
        if self.state.get("how") != "set by hand":
            blocked = now - float(self.state["blocked_since"])
            count = int(self.state.get("count") or 1)
            window = int(float(self.state.get("span") or 0) + blocked + 0.999)
            learned = max(1, int(count * LIMIT_MARGIN))
            if self.state.get("limit") and count <= int(self.state["limit"]):
                # Hit even under what was learned: the provider allows less than it seemed
                learned = max(1, int(int(self.state["limit"]) * LIMIT_MARGIN))
            self.state.update(
                limit=learned, window=max(window, int(self.state.get("window") or 0)), how="learned", at=_now(),
                v=LIMITS_VERSION,
            )
            logger.info(f"Stream Check: {self.name} allows about {count} streams; keeping to {learned} every {window // 60} min")
        for field in ("blocked_since", "count", "span", "step", "next_try"):
            self.state.pop(field, None)
        self.state["clear_since"] = now
        self._save()

    def describe(self):
        if self.state.get("blocked_since"):
            return f"hit its limit after {self.state.get('count')} streams; finding out how long it lasts"
        if self.state.get("limit"):
            return f"allows {self.state['limit']} streams every {max(1, int(self.state['window']) // 60)} min ({self.state.get('how')})"
        return ""


def provider_limits(redis_client):
    """Every provider, with what it allows as far as is known, for the page."""
    providers = _Providers(redis_client)
    limits = load_limits()
    rows = []
    for key, profiles in providers.members.items():
        text = _key_text(key)
        state = limits.get(text) or {}
        name = " + ".join(sorted({p.m3u_account.name for p in profiles}))
        budget = _Budget(redis_client, text, name, state)
        rows.append({
            "key": text,
            "name": name,
            "limit": state.get("limit"),
            "window_minutes": round(int(state["window"]) / 60, 1) if state.get("window") else None,
            "how": state.get("how", ""),
            "resting": bool(state.get("blocked_since")),
            "said": budget.describe() or "no limit found yet",
        })
    return sorted(rows, key=lambda row: row["name"].lower())


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
    # A page (Cloudflare's, a panel's) says it best in its title; only its start was read,
    # so a tag cut off at the end is dropped rather than shown
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if title:
        text = title.group(1)
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"<[^>]*$", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _picture(data):
    """
    What the picture does over the seconds read: {"length", "black", "frozen", "frame"}, or
    None without ffmpeg. frame is a tiny fingerprint of one frame (16x9, grey, a bit each
    for lighter or darker than the rest): the same card shown on many channels has the same
    one, where a real channel's changes.
    """
    try:
        pts = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-of", "csv=p=0", "-i", "pipe:0"],
            input=data, capture_output=True, timeout=30,
        )
        times = []
        for line in (pts.stdout or b"").decode().split():
            try:
                times.append(float(line.strip(",")))
            except ValueError:
                continue
        length = (max(times) - min(times)) if len(times) > 1 else 0.0
        detect = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", "pipe:0", "-an",
             "-vf", f"blackdetect=d={BLACK_SECONDS}:pix_th=0.10,freezedetect=n=0.001:d={FROZEN_SECONDS}",
             "-f", "null", "-"],
            input=data, capture_output=True, timeout=60,
        )
        frame = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-an",
             "-vf", "select=gte(n\\,10),scale=16:9,format=gray", "-frames:v", "1", "-f", "rawvideo", "-"],
            input=data, capture_output=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"The picture could not be looked at: {e}")
        return None
    log = (detect.stderr or b"").decode("utf-8", "replace")
    start = min(times) if times else 0.0

    def spans(begin, end):
        """Seconds covered by begin/end pairs in the log; one still going runs to the end."""
        total = 0.0
        opened = None
        for match in re.finditer(rf"({re.escape(begin)}|{re.escape(end)})[:=]\s*(-?[\d.]+)", log):
            what, value = match.group(1), float(match.group(2))
            if what == begin:
                opened = value
            elif opened is not None:
                total += value - opened
                opened = None
        if opened is not None:
            total += max(0.0, start + length - opened)
        return total

    grey = frame.stdout or b""
    fingerprint = ""
    if len(grey) >= 144:
        pixels = list(grey[:144])
        mean = sum(pixels) / len(pixels)
        bits = "".join("1" if p > mean else "0" for p in pixels)
        fingerprint = f"{int(bits, 2):036x}"
    return {
        "length": round(length, 1),
        "black": round(min(length, spans("black_start", "black_end")), 1),
        "frozen": round(min(length, spans("lavfi.freezedetect.freeze_start", "lavfi.freezedetect.freeze_end")), 1),
        "frame": fingerprint,
    }


def _snapshots(data, every=SNAPSHOT_EVERY):
    """A fingerprint of one frame every few seconds (see _picture), in order."""
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-an",
             "-vf", f"fps=1/{every},scale=16:9,format=gray", "-f", "rawvideo", "-"],
            input=data, capture_output=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    raw = done.stdout or b""
    prints = []
    for start in range(0, len(raw) - 143, 144):
        pixels = list(raw[start:start + 144])
        mean = sum(pixels) / 144
        prints.append(int("".join("1" if p > mean else "0" for p in pixels), 2))
    return prints


def _still(data, seconds):
    """
    Whether a longer look confirms a picture that does not move: {"frozen", "length",
    "still", "frame"}, or None when too little came to say.
    """
    picture = _picture(data)
    if not picture or picture["length"] < seconds / 2:
        return None
    prints = _snapshots(data)
    same = len(prints) >= 2 and all(bin(p ^ prints[0]).count("1") <= SNAPSHOT_SAME_BITS for p in prints)
    return {
        "frozen": picture["frozen"] >= FROZEN_CONFIRM_SHARE * picture["length"] and same,
        "length": picture["length"],
        "still": picture["frozen"],
        "frame": picture["frame"],
    }


def _watch_longer(session, url, playlist, headers, seconds, should_stop):
    """
    More of the stream, for seconds: one long read of a plain stream, or the newest segment of
    an HLS playlist fetched again and again for that long.
    """
    until = time.monotonic() + seconds
    if playlist is None:
        _, data = _read(session, url, headers, until, should_stop, limit=CONFIRM_BYTES)
        return data
    seen, gathered = set(), bytearray()
    while time.monotonic() < until and len(gathered) < CONFIRM_BYTES:
        response, text = _read(session, playlist, headers, time.monotonic() + 10, should_stop, limit=256 * 1024)
        if response.status_code >= 400:
            break
        segment = _hls_segment(session, response.url or playlist, text.decode("utf-8", "replace"), headers, time.monotonic() + 10, should_stop)
        if segment and segment not in seen:
            seen.add(segment)
            _, piece = _read(session, segment, headers, time.monotonic() + 15, should_stop, limit=CONFIRM_BYTES)
            gathered.extend(piece)
        if not _pause(2, should_stop):
            raise Stopped()
    return bytes(gathered)


def _why_no_connection(error):
    """
    Which of the ways a connection fails this was, in words. Never the error's own text: it
    carries the stream's address, and for Xtream Codes the address carries the login.
    """
    text = repr(error)
    for needles, words in (
        (("Name or service not known", "Temporary failure in name resolution", "nodename nor servname",
          "getaddrinfo failed", "NameResolutionError"), "its address could not be looked up"),
        (("Connection refused", "ECONNREFUSED", "Errno 111"), "its server refused the connection"),
        (("RemoteDisconnected", "Connection aborted", "without response"),
         "its server took the connection and closed it without answering"),
        (("Connection reset", "ECONNRESET", "Errno 104"), "its server cut the connection off"),
        (("Network is unreachable", "No route to host", "Errno 101", "Errno 113"),
         "no network route to its server"),
        (("timed out", "Timeout"), "its server did not answer in time"),
        (("SSLError", "CERTIFICATE", "SSL"), "a secure connection could not be made"),
        (("TooManyRedirects", "Exceeded"), "it sent the request round in circles"),
    ):
        if any(needle in text for needle in needles):
            return words
    return type(error).__name__


def _looks_like_ts(data):
    """MPEG-TS: a sync byte every 188 bytes, three in a row somewhere near the start."""
    for start in range(min(188, len(data))):
        if len(data) > start + 376 and data[start] == data[start + 188] == data[start + 376] == 0x47:
            return True
    return False


class _Stalled(Exception):
    """Connected and answered, then no data came for the whole read timeout."""


# How long the data may pause once a stream is reading, before the read gives up. Live streams
# come in bursts; a picture look reads for seconds, and a pause of five in there threw away a
# stream that played (and was reported as "could not connect", which is what requests calls a
# read timeout in the middle of a body).
READ_PAUSE_SECONDS = 10


# Once a stream has sent something, a pause this long ends the read and what came is judged.
# Providers send a burst -- ten seconds of video in under one -- and then nothing until real
# time catches up; waiting that out cost every check on them seconds. A stream sending in real
# time never pauses this long, and is read for as long as asked.
PAUSE_ENDS_READ = 1.5


def _read(session, url, headers, deadline, should_stop, limit=READ_BYTES):
    """
    Up to limit bytes of url, before the deadline; (response, bytes). The body is pumped by a
    thread, so the wait for each piece can be decided here: up to READ_PAUSE_SECONDS for the
    first, PAUSE_ENDS_READ once something has come. What came before a pause, or before the
    connection broke, is kept; a stream that sends nothing at all raises _Stalled.
    """
    import queue
    import threading

    import requests

    left = max(1.0, deadline - time.monotonic())
    response = session.get(url, headers=headers, stream=True, timeout=(min(5.0, left), READ_PAUSE_SECONDS))
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

        pieces = queue.Queue()

        def pump():
            try:
                for chunk in response.iter_content(chunk_size=32 * 1024):
                    pieces.put(chunk)
                pieces.put(None)
            except Exception as e:
                # Also how it ends when the read below is done and the response closed
                pieces.put(e)

        threading.Thread(target=pump, daemon=True, name="stream-check-read").start()
        while True:
            if should_stop():
                raise Stopped()
            now = time.monotonic()
            if data and now > deadline:
                break
            wait = READ_PAUSE_SECONDS if not data else min(PAUSE_ENDS_READ, max(0.05, deadline - now))
            try:
                piece = pieces.get(timeout=wait)
            except queue.Empty:
                if not data:
                    raise _Stalled()
                break
            if piece is None:
                break
            if isinstance(piece, Exception):
                if isinstance(piece, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)):
                    # The data stopped, or the connection broke, part way: what came is judged
                    if not data:
                        raise _Stalled()
                    break
                raise piece
            data.extend(piece)
            if len(data) >= limit:
                break
    finally:
        _hang_up(response)
        response.close()
    return response, bytes(data)


def _hang_up(response):
    """
    Shut the connection's socket, so a read still waiting on it (the pump's) returns at once.
    Closing alone waits for that read to finish -- out to the read timeout, the very pause
    the reader stopped to avoid.
    """
    import socket

    for path in (("raw", "_connection", "sock"), ("raw", "_fp", "fp", "raw", "_sock")):
        found = response
        for name in path:
            found = getattr(found, name, None)
            if found is None:
                break
        if isinstance(found, socket.socket):
            try:
                found.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return


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


def probe(url, user_agent="", timeout=12, should_stop=lambda: False, picture_seconds=0, frozen_confirm_seconds=0):
    """
    Whether url plays: {"ok", "reason", "kind", "resolution", "codec", "bytes", "seconds"}.

    With picture_seconds, that many seconds are read and the picture itself is judged: black,
    or not moving, is a failure of its own kind (see KIND_TEXT), and "frame" is kept, so the
    same card on many channels can be recognised as the provider's (see _placeholders). A
    picture that looks frozen is watched frozen_confirm_seconds longer before it is called so.

    Raises Stopped if should_stop says so part way: a check cut short says nothing about
    the stream, and must not count against it.
    """
    import requests

    started = time.monotonic()
    deadline = started + float(timeout)
    headers = {"User-Agent": user_agent} if user_agent else {}
    result = {"ok": False, "reason": "", "kind": DEAD, "resolution": "", "codec": "", "bytes": 0, "seconds": 0.0}
    limit = PICTURE_BYTES if picture_seconds else READ_BYTES
    # Reading stops at the limit or the deadline: for a live stream, that is a few seconds of it
    read_until = min(deadline, started + float(picture_seconds) + 3) if picture_seconds else deadline
    session = requests.Session()
    playlist = None
    try:
        response, data = _read(session, url, headers, read_until, should_stop, limit=limit)
        if response.status_code >= 400:
            said = _said(data)
            result["reason"] = f"The provider answered HTTP {response.status_code}" + (f": {said}" if said else "")
            result["refused"] = response.status_code in REFUSED_STATUS
            result["transient"] = response.status_code in TRANSIENT_STATUS
            return result
        kind = (response.headers.get("Content-Type") or "").lower()
        if data[:7] == b"#EXTM3U" or "mpegurl" in kind:
            playlist = response.url or url
            segment = _hls_segment(session, response.url or url, data.decode("utf-8", "replace"), headers, deadline, should_stop)
            if not segment:
                result["reason"] = "The playlist lists nothing to play"
                return result
            response, data = _read(session, segment, headers, max(read_until, time.monotonic() + 5), should_stop, limit=limit)
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
            picture = _picture(data) if picture_seconds else None
            if picture and picture["length"] >= PICTURE_LEAST:
                result["frame"] = picture["frame"]
                result["picture_looked"] = True
                most = PICTURE_FAULT_SHARE * picture["length"]
                if picture["black"] >= max(BLACK_SECONDS, most):
                    result.update(ok=False, kind=BLACK, reason=f"Black picture ({picture['black']:.0f} of {picture['length']:.0f} s)")
                elif picture["frozen"] >= max(FROZEN_SECONDS, most) and not frozen_confirm_seconds:
                    result.update(
                        ok=False, kind=FROZEN,
                        reason=f"The picture does not move ({picture['frozen']:.0f} of {picture['length']:.0f} s)",
                        frozen_frame=picture["frame"],
                    )
                elif picture["frozen"] >= max(FROZEN_SECONDS, most):
                    # Only a suspicion yet: watched longer, and it has to stay still throughout
                    longer = _still(
                        _watch_longer(session, url, playlist, headers, float(frozen_confirm_seconds), should_stop),
                        float(frozen_confirm_seconds),
                    )
                    if longer and longer["frozen"]:
                        result.update(
                            ok=False, kind=FROZEN,
                            reason=f"The picture does not move ({longer['still']:.0f} of {longer['length']:.0f} s, watched to be sure)",
                            frozen_frame=longer["frame"],
                        )
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
        result["transient"] = e.status in TRANSIENT_STATUS
    except requests.exceptions.ConnectTimeout:
        result.update(reason="The provider did not answer (no connection within the time)", kind=UNREACHABLE)
    except _Stalled:
        result.update(reason="Its server answered, then stopped sending", transient=True)
    except requests.exceptions.ReadTimeout:
        result.update(reason="The provider answered, then sent nothing", transient=True)
    except requests.exceptions.ConnectionError as e:
        if "Read timed out" in repr(e):
            # requests' name for a read timeout inside a body: connected, then silence
            result.update(reason="Its server answered, then stopped sending", transient=True)
        else:
            result.update(reason=f"Could not connect to the provider ({_why_no_connection(e)})", kind=UNREACHABLE)
    except requests.exceptions.RequestException as e:
        result["reason"] = f"Could not be opened: {type(e).__name__}"
    finally:
        session.close()
        result["seconds"] = round(time.monotonic() - started, 1)
    return result


# ── Which streams, and a provider connection for each ────────────────────────


def _targets(settings, only=None, due_before=None, skip_accounts=(), waiting_too=False, now=None):
    """
    The streams to look at, by account: those on channels (in the groups chosen), and
    every parked one. Custom streams are made by hand -- a fallback screen -- and are not
    a provider's to lose, and ignored ones a person has said to leave alone.

    Within a round, a stream it suspects of a picture fault is due again once its next look
    is (see _record); waiting_too counts those not due yet, so the round is not over while
    it still has one to decide.
    """
    from .models import ChannelStream, Stream

    links = ChannelStream.objects.filter(stream__is_custom=False, stream__m3u_account__isnull=False)
    groups = [int(g) for g in settings.get("channel_groups") or ()]
    if groups:
        links = links.filter(channel__channel_group_id__in=groups)
    wanted = set(links.values_list("stream_id", flat=True)) | parked_ids()
    wanted -= {int(i) for i in load_ignored()}
    if only is not None:
        wanted &= {int(i) for i in only}
    if due_before is not None:
        # A run that stopped part way goes on where it was: what was looked at lately is
        # not looked at again -- but for a suspect whose next look has come
        results = load_results()["streams"]
        now = now if now is not None else time.time()

        def due(record):
            if _relook(record, due_before):
                return waiting_too or float(record["suspect"].get("next_at") or 0) <= now
            return record.get("checked_at", "") < due_before

        wanted = {i for i in wanted if due(results.get(str(i)) or {})}

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


def _picture_due(record, settings, round_id=None):
    """
    Whether this check should look at the picture: on, and the stream did not play last time,
    or is being looked at again, or its picture was not looked at within picture_every_days.
    """
    if not settings.get("picture_check"):
        return False
    record = record or {}
    days = float(settings.get("picture_every_days", 3) or 0)
    if days <= 0 or not record.get("ok") or record.get("suspect") or _relook(record, round_id):
        return True
    looked = record.get("picture_at") or ""
    cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat(timespec="milliseconds")
    return looked < cutoff


def _relook(record, round_id):
    """The suspicion this round has of a stream's picture, or None."""
    suspect = (record or {}).get("suspect")
    return suspect if suspect and suspect.get("round") == round_id else None


def _record(redis_client, results, stream, outcome, settings, round_id=None):
    """
    What a check found, onto what earlier runs found.

    A picture fault is a suspicion first: kept beside the record as it was, not counted, and
    looked at again later in the same round (round_id). Seen again, it is recorded as the
    failure it is; RELOOKS clean looks in a row, and the stream plays.
    """
    previous = results.get(str(stream.id)) or {}
    suspect = _relook(previous, round_id)
    fault = "" if outcome["ok"] else outcome.get("kind") or DEAD
    picture_fault = fault in PICTURE_KINDS
    # A timeout or a server error: counted only if it happens again later in the run
    transient = not outcome["ok"] and bool(outcome.get("transient")) and fault == DEAD

    def keep_suspecting(**changes):
        record = {**previous, "name": stream.name, "checked_at": _now(), "suspect": {
            **(suspect or {}), "round": round_id, "next_at": time.time() + RELOOK_GAP, **changes,
        }}
        return _keep_record(redis_client, results, stream, record, settings)

    def not_counted(reason):
        """Looked at, never reached: noted, counted nowhere, looked at again next run."""
        record = {k: v for k, v in previous.items() if k != "suspect"}
        record.update(name=stream.name, checked_at=_now(), skipped=True, refused=reason)
        return _keep_record(redis_client, results, stream, record, settings)

    if suspect:
        looks = int(suspect.get("looks") or 0) + 1
        if fault == UNREACHABLE:
            # Still no way through: never taken for the stream's fault
            if looks >= (RELOOKS if suspect.get("kind") == UNREACHABLE else RELOOKS * 2):
                return not_counted(f"{outcome['reason']}, every time it was tried in this run: not counted")
            return keep_suspecting(looks=looks)
        if suspect.get("kind") == UNREACHABLE and picture_fault and settings.get("relook_pictures", True):
            # Reached at last, and the picture looks wrong: that is a suspicion of its own now
            return keep_suspecting(kind=fault, reason=outcome["reason"], clean=0, looks=0)
        if suspect.get("kind") in PICTURE_KINDS and outcome["ok"] and int(suspect.get("clean") or 0) + 1 < RELOOKS:
            return keep_suspecting(clean=int(suspect.get("clean") or 0) + 1, looks=looks)
    elif fault == UNREACHABLE:
        return keep_suspecting(kind=UNREACHABLE, reason=outcome["reason"], clean=0, looks=0)
    elif picture_fault and settings.get("relook_pictures", True):
        return keep_suspecting(kind=fault, reason=outcome["reason"], clean=0, looks=0)
    elif transient:
        return keep_suspecting(kind="transient", reason=outcome["reason"], clean=0, looks=0)
    # Unreachable only reaches here with looking again switched off by the run's end: never counted
    if fault == UNREACHABLE:
        return not_counted(f"{outcome['reason']}: not counted")
    history = ([1 if outcome["ok"] else 0] + list(previous.get("history") or []))[:HISTORY_KEPT]
    failures = 0 if outcome["ok"] else int(previous.get("failures") or 0) + 1
    kind = "" if outcome["ok"] else outcome.get("kind") or DEAD
    # Failures in a row of a stream that does not play at all: what autopark counts. Any other
    # kind of failure, or playing, starts it again.
    dead_streak = int(previous.get("dead_streak") or 0) + 1 if kind == DEAD else 0
    record = {
        "ok": outcome["ok"],
        "kind": kind,
        "dead_streak": dead_streak,
        "frame": outcome.get("frozen_frame") or "",
        "reason": outcome["reason"],
        "resolution": outcome["resolution"],
        "codec": outcome["codec"],
        "seconds": outcome["seconds"],
        "checked_at": _now(),
        # When the picture was last really looked at (see _picture_due)
        "picture_at": _now() if outcome.get("picture_looked") else previous.get("picture_at", ""),
        "last_ok": _now() if outcome["ok"] else previous.get("last_ok", ""),
        "failures": failures,
        "history": history,
        "name": stream.name,
    }
    if suspect and not outcome["ok"] and suspect.get("kind") in PICTURE_KINDS + ("transient",):
        record["reason"] = f"{outcome['reason']}, again when looked at later"
    return _keep_record(redis_client, results, stream, record, settings)


def _keep_record(redis_client, results, stream, record, settings):
    record["state"] = state_of(record, settings)
    results[str(stream.id)] = record
    redis_client.hset(LIVE_RESULTS_KEY, str(stream.id), json.dumps(record))
    return record


def state_of(record, settings):
    """"ok", "failing" (not yet enough runs in a row) or "broken"."""
    if not record or record.get("skipped"):
        # Looked at, but nothing could be learned: the provider was full the whole time
        return "unchecked"
    if record.get("suspect"):
        # A picture fault seen once, to be looked at again before it counts
        return "suspect"
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
        found = json.loads(redis_client.get(PROGRESS_KEY) or "{}")
    except (ValueError, TypeError):
        return {}
    found["eta_seconds"] = _eta(found)
    return found


# The pace of a run is judged over this long: long enough to take in the waits a provider's
# limit brings, short enough to follow a run that speeds up or slows down
PACE_WINDOW = 3600


def _note_pace(redis_client):
    """A (time, streams done) sample, for the estimate of how long a run has left."""
    current = progress(redis_client)
    samples = [s for s in current.get("samples") or [] if s[0] > time.time() - 6 * 3600][-60:]
    samples.append([time.time(), int(current.get("done") or 0)])
    _progress(redis_client, samples=samples)


def _eta(found):
    """Seconds a running round has left at the pace of the last hour, or None to say nothing."""
    samples = found.get("samples") or []
    left = int(found.get("total") or 0) - int(found.get("done") or 0)
    if found.get("state") != "running" or left <= 0 or len(samples) < 2:
        return None
    now = time.time()
    since = next((s for s in samples if s[0] >= now - PACE_WINDOW), samples[0])
    last = samples[-1]
    if since is last:
        since = samples[-2]
    took, done = last[0] - since[0], last[1] - since[1]
    if took < 60 or done <= 0:
        return None
    return int(left * took / done)


def start_round(redis_client, force=False, only=None):
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
    since = _now() if force or only is not None else datetime.fromtimestamp(
        time.time() - float(settings["every_hours"]) * 3600 * 0.9, timezone.utc
    ).isoformat(timespec="milliseconds")
    total = sum(len(s) for s in _targets(settings, only=only, due_before=since).values())
    round_ = {"since": since, "forced": bool(force), "total": total}
    if only is not None:
        # A recheck: only the failing streams due to be looked at again
        round_.update(only=[int(i) for i in only], kind="recheck")
    redis_client.set(ROUND_KEY, json.dumps(round_), ex=ROUND_TTL)
    redis_client.delete(STOP_KEY)
    _progress(
        redis_client, state="running", started_at=_now(), finished_at="", total=total,
        done=0, broken=0, accounts={}, waiting=False, message="",
        # Where the pace is measured from (see _eta)
        samples=[[time.time(), 0]],
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

    from .models import Stream

    settings = load_settings()
    if not redis_client.set(RUN_KEY, "1", nx=True, ex=RUN_TTL):
        return "already running"
    if only is None:
        redis_client.delete(QUEUED_KEY)
    try:
        round_ = None if only is not None else current_round(redis_client)
        if only is None and round_ is None:
            return "done"
        # What a suspicion of a stream's picture belongs to: this round, or a check asked for
        # by hand, which looks again by itself (see tasks.run_stream_check)
        round_id = round_["since"] if round_ else "only"
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
        round_only = (round_ or {}).get("only")
        by_account = _targets(
            settings, only=only if only is not None else round_only,
            due_before=round_["since"] if round_ else None, skip_accounts=unavailable,
        )
        if not by_account:
            if round_ is None:
                return "done"
            # Nothing due now; streams whose picture is to be looked at again may still be
            pending = _targets(
                settings, only=round_.get("only"), due_before=round_["since"],
                skip_accounts=unavailable, waiting_too=True,
            )
            return _relook_wait(redis_client, round_, pending) if pending else _finish(redis_client, round_)
        deadline = time.monotonic() + float(batch_seconds or BATCH_SECONDS)
        results = load_results()["streams"]
        lock = threading.Lock()
        # Everything the providers' threads need from the database, read here: a thread has
        # a database connection of its own, and all it should do is talk to providers
        providers = _Providers(redis_client)
        profiles = {a: _profiles_of(streams[0].m3u_account) for a, streams in by_account.items()}
        agents = {a: streams[0].m3u_account.get_user_agent_string() or "" for a, streams in by_account.items()}
        parked_now = load_parked()
        was_parked = set(parked_now)
        to_park = []
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
        # What each provider allows, and a stream known to play there
        limits = load_limits()
        budgets = {
            key: _Budget(redis_client, _key_text(key), names[key], limits.get(_key_text(key)))
            for key in by_provider
        }
        good_ids = {key: budget.state.get("good_stream") for key, budget in budgets.items() if budget.state.get("good_stream")}
        good_streams = {}
        for stream in Stream.objects.filter(id__in=list(good_ids.values())).select_related("m3u_account"):
            for key, stream_id in good_ids.items():
                if stream_id == stream.id and providers.provider_of(stream.m3u_account_id) == key:
                    good_streams[key] = stream
        for stream in good_streams.values():
            profiles.setdefault(stream.m3u_account_id, _profiles_of(stream.m3u_account))
            agents.setdefault(stream.m3u_account_id, stream.m3u_account.get_user_agent_string() or "")
        provider_counts = {key: _ProviderCount() for key in by_provider}
        resume_in = {}

        accounts = progress(redis_client).get("accounts") or {}
        for key, streams in by_provider.items():
            entry = accounts.setdefault(_key_text(key), {"name": names[key], "done": 0})
            entry.update(name=names[key], left=len(streams), now="", status="checking", reason="")

        def stop_asked():
            # Stop ends a round; a stream checked by hand is not part of one and goes on
            return (only is None and bool(redis_client.exists(STOP_KEY))) or (scheduled and not in_window(settings))

        def someone_watching():
            # With only_when_idle, anything playing anywhere ends every provider's part
            return idle_only and providers.anything_playing()

        def set_status(entry, status, reason=""):
            with lock:
                entry.update(status=status, reason=reason, now="")
                _progress(redis_client, accounts=accounts)

        def count(stream, outcome):
            """What was found kept, under the lock."""
            record = _record(redis_client, results, stream, outcome, settings, round_id)
            if record["state"] == "broken":
                _progress(redis_client, broken=progress(redis_client).get("broken", 0) + 1)
            if record["state"] == "ok" and str(stream.id) in was_parked:
                recovered.append(stream.id)
            elif (
                settings.get("autopark")
                and not outcome["ok"]
                and record.get("kind") == DEAD
                and int(record.get("dead_streak") or 0) >= int(settings.get("autopark_after") or 3)
                and str(stream.id) not in was_parked
            ):
                to_park.append((stream.id, record["reason"], record["failures"]))

        def one_provider(key, streams):
            entry = accounts[_key_text(key)]
            budget = budgets[key]
            logins = {}
            account_of = {}
            # Per account: failures before anything of it played, held back until it is
            # clear whether its streams failed or the account is down
            held, played, given_up = {}, set(), set()
            gap = float(settings["gap_seconds"])

            def rest(status, reason, seconds=None):
                waiting.add(key)
                if seconds is not None:
                    resume_in[key] = seconds
                # When it goes on, as a moment rather than a clock time: the page shows it in
                # the viewer's own time zone, where the server's would read hours off
                entry["until"] = (
                    datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat(timespec="seconds")
                    if seconds else ""
                )
                set_status(entry, status, reason)

            def logins_of(account_id, account):
                if account_id not in logins:
                    account_of[account_id] = account
                    usable, problem = _usable_logins(account, profiles[account_id], agents[account_id])
                    logins[account_id] = usable
                    if not usable:
                        _unavailable(redis_client, round_, account_id, account.name, problem, lock)
                        given_up.add(account_id)
                return logins[account_id]

            def attempt(stream):
                """Open one stream as a viewer would: its outcome, "busy" when the provider
                cannot be used now (the status says why), or "stopped" when someone came."""
                account_id = stream.m3u_account_id
                usable = logins_of(account_id, stream.m3u_account)
                if not usable:
                    return None
                if providers.in_use(key):
                    rest("in use", "someone is watching through it")
                    return "busy"
                xc_logins = [
                    (account_of[a], login, agents[a])
                    for a, usable_here in logins.items() if account_of[a].account_type == "XC"
                    for login in usable_here
                ]
                if not provider_counts[key].free(xc_logins, lambda: stop_asked() or someone_watching() or providers.in_use(key)):
                    rest("in use", "the provider says one of its logins is in use")
                    return "busy"
                profile = _take_connection(usable, redis_client)
                if profile is None:
                    rest("in use", "no connection of it is free")
                    return "busy"
                if providers.in_use(key, holding=profile):
                    # A viewer came onto it between the asking and the taking
                    if profile.max_streams > 0:
                        release_profile_slot(profile.id, redis_client)
                    rest("in use", "someone started watching through it")
                    return "busy"
                redis_client.expire(RUN_KEY, RUN_TTL)
                with lock:
                    entry.update(now=stream.name, status="checking", reason="", until="")
                    _progress(redis_client, accounts=accounts)

                def should_stop():
                    # Cut short: asked to stop, a viewer needs a connection, or someone
                    # started watching through this provider
                    return (
                        stop_asked()
                        or bool(redis_client.exists(YIELD_KEY))
                        or someone_watching()
                        or providers.in_use(key, holding=profile)
                    )

                try:
                    url = _url_for(stream, profile)
                    if not (url and url.startswith(("http://", "https://"))):
                        return None
                    budget.note_open()
                    return probe(
                        url, agents[account_id], settings["timeout_seconds"], should_stop,
                        picture_seconds=settings["picture_seconds"]
                        if (only is not None and settings.get("picture_check"))
                        or _picture_due(results.get(str(stream.id)), settings, round_id) else 0,
                    )
                except Stopped:
                    # Looked at again once the viewer is done, and not counted
                    rest("in use", "someone started watching through it")
                    return "stopped"
                finally:
                    # An unlimited profile took no slot, so there is none to give back;
                    # releasing anyway would free one a viewer holds
                    if profile.max_streams > 0:
                        release_profile_slot(profile.id, redis_client)
                    provider_counts[key].check_ended()

            def ready():
                """Whether this provider may be used now; if not, the status says why."""
                if stop_asked():
                    stopped["asked"] = bool(redis_client.exists(STOP_KEY))
                    waiting.add(key)
                    return False
                if redis_client.exists(YIELD_KEY):
                    rest("waiting", "making way for a viewer")
                    return False
                if someone_watching():
                    rest("waiting", "something is playing")
                    return False
                return True

            try:
                # A provider resting after hitting its limit is not asked anything -- not
                # even about its logins -- until it is time to try it again
                resting = budget.resting()
                if resting is not None:
                    if resting > 0:
                        rest("resting", budget.describe(), resting)
                        return
                    good = good_streams.get(key)
                    if good is None:
                        # Nothing known to play there: the first stream of this round will do
                        good = streams[0]
                    if not ready():
                        return
                    found = attempt(good)
                    if found in ("busy", "stopped") or found is None:
                        return
                    if not found["ok"]:
                        budget.still_blocked()
                        rest("resting", budget.describe(), budget.resting())
                        return
                    budget.recovered()
                    with lock:
                        count(good, found)

                for index, stream in enumerate(streams):
                    account_id = stream.m3u_account_id
                    if account_id in given_up:
                        continue
                    if (
                        str(stream.id) in results
                        and results[str(stream.id)].get("checked_at", "") >= (round_ or {}).get("since", "~")
                        and not _relook(results[str(stream.id)], round_id)
                    ):
                        # Already looked at in this round (the stream tried to learn the limit)
                        continue
                    if not ready():
                        return
                    # Staying under what the provider allows
                    wait = budget.wait()
                    if wait > 0:
                        if wait > 30:
                            rest("resting", budget.describe(), wait)
                            return
                        _pause(wait, lambda: stop_asked() or someone_watching())
                        if not ready():
                            return

                    outcome = attempt(stream)
                    if outcome in ("busy", "stopped"):
                        return
                    if account_id in given_up:
                        continue

                    if outcome is not None and outcome.get("refused"):
                        # This stream, or the provider? Another of its streams tells: one that
                        # just played there, or else the next in line. A provider refuses a
                        # channel it no longer carries the same way it refuses when at its
                        # limit -- one answered every Euronews HD with HTTP 407 while playing
                        # everything around it -- and only the other stream tells them apart.
                        other = None
                        compare = good_streams.get(key)
                        if compare is None or compare.id == stream.id:
                            compare = next(
                                (s for s in streams[index + 1:] if s.m3u_account_id not in given_up),
                                None,
                            )
                        if compare is not None:
                            _pause(REFUSAL_PAUSE, lambda: stop_asked() or someone_watching())
                            if not ready():
                                return
                            other = attempt(compare)
                            if other in ("busy", "stopped"):
                                return
                            if other is not None and other.get("refused"):
                                # Both refused: the provider will not give any. Its limit.
                                budget.hit()
                                rest("resting", f"{budget.describe()} ({outcome['reason']})", budget.resting())
                                return
                            if other is not None:
                                with lock:
                                    count(compare, other)
                                    if other["ok"]:
                                        good_streams[key] = compare
                                        budget.state["good_stream"] = compare.id
                                        played.add(compare.m3u_account_id)
                                    if compare in streams[index + 1:]:
                                        entry["done"] += 1
                                        entry["left"] -= 1
                        # The provider gives other streams: this one it does not. A failure of
                        # the stream, counted like any other, so rechecks and autopark see it.
                        outcome = {
                            **outcome, "refused": False, "kind": REFUSED,
                            "reason": f"{outcome['reason']}, while the provider plays its other streams"
                            if compare is not None and other is not None and other.get("ok") else outcome["reason"],
                        }

                    account_held = held.setdefault(account_id, [])
                    with lock:
                        if outcome is None:
                            # Not a URL that can be opened: noted as looked at, so the round
                            # moves on
                            _touch(results, stream)
                        elif outcome["ok"]:
                            played.add(account_id)
                            good_streams[key] = stream
                            budget.state["good_stream"] = stream.id
                            for earlier, found in account_held:
                                count(earlier, found)
                            account_held.clear()
                            count(stream, outcome)
                        elif account_id in played or outcome.get("kind", DEAD) not in (DEAD, UNREACHABLE):
                            # Playing something, or refused while others play: the stream's own
                            # failure. Only streams that do not play at all might be the
                            # provider being down.
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
                # The stream known to play there, for telling a refused stream from a limit
                if budget.state.get("good_stream"):
                    budget._save()
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
            for stream_id in recovered:
                # Parked by autopark, it goes back by itself as it came; by a person, only
                # when that is asked for
                if not (settings.get("restore_recovered") or (parked_now.get(str(stream_id)) or {}).get("auto")):
                    continue
                try:
                    restore(stream_id)
                    logger.info(f"Stream Check: parked stream {stream_id} works again and was put back")
                except ValueError as e:
                    logger.info(f"Stream Check: parked stream {stream_id} works again, not put back: {e}")
            for stream_id, reason, failures in to_park:
                try:
                    park(stream_id, f"{reason} ({failures} checks in a row)", auto=True)
                    logger.info(f"Stream Check: autopark parked stream {stream_id} after {failures} failed checks")
                except ValueError as e:
                    logger.info(f"Stream Check: autopark could not park stream {stream_id}: {e}")
            _placeholders(results, providers.provider_of)
            _keep_results(results)
            if only is None:
                # How far this batch got, for how long the round has left
                _note_pace(redis_client)
            redis_client.delete(LIVE_RESULTS_KEY)
            # What each provider allows, learned in this batch
            for budget in budgets.values():
                try:
                    budget.save()
                except Exception as e:
                    logger.warning(f"Stream Check could not keep what {budget.name} allows: {e}")

        if only is not None:
            return "done"
        if stopped["asked"]:
            return _stopped(redis_client)
        round_ = current_round(redis_client) or round_
        left = _targets(
            settings, only=round_.get("only"), due_before=round_["since"],
            skip_accounts=round_.get("unavailable") or {}, waiting_too=True,
        )
        if not left:
            return _finish(redis_client, round_)
        if not _targets(
            settings, only=round_.get("only"), due_before=round_["since"],
            skip_accounts=round_.get("unavailable") or {},
        ):
            return _relook_wait(redis_client, round_, left)
        if {providers.provider_of(a) for a in left} <= waiting:
            # Everything left is on providers that cannot be used now: in use, or refusing
            reasons = "; ".join(
                f"{e['name']}: {e['reason']}" for e in accounts.values()
                if e.get("reason") and e.get("status") in ("in use", "waiting", "resting")
            )
            # When to look again: the soonest a resting provider may be tried, but no sooner
            # than a minute (a viewer may be done by then) and no later than an hour
            soonest = min(resume_in.values(), default=RETRY_WAITING)
            _progress(
                redis_client, waiting=True, message=reasons or "Waiting for viewers to finish",
                resume_in=int(min(3600, max(RETRY_WAITING, soonest if len(resume_in) == len(waiting) else RETRY_WAITING))),
            )
            return "waiting"
        return "more"
    finally:
        redis_client.delete(RUN_KEY)


def _relook_wait(redis_client, round_, left):
    """All that is left of the round is pictures to look at again: wait for the first of them."""
    results = load_results()["streams"]
    waits = [
        float(_relook(results.get(str(s.id)), round_["since"])["next_at"]) - time.time()
        for streams in left.values() for s in streams
        if _relook(results.get(str(s.id)), round_["since"])
    ]
    _progress(
        redis_client, waiting=True,
        message="Looking again at pictures that looked wrong, to be sure",
        resume_in=int(min(3600, max(RETRY_WAITING, min(waits, default=RETRY_WAITING)))),
    )
    return "waiting"


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


def _placeholders(results, provider_of):
    """
    Frozen pictures that are the provider's own card rather than a channel's: the same still
    picture on PLACEHOLDER_CHANNELS or more channels of one provider. A channel that has
    stopped shows its last frame, which is its own; a provider's "no stream" card is the same
    on every channel it stands in for. Changes the records in place.
    """
    from .models import ChannelStream, Stream

    still = {sid: r for sid, r in results.items() if r.get("kind") in (FROZEN, PLACEHOLDER) and r.get("frame")}
    if not still:
        return
    ids = [int(i) for i in still]
    account_of = dict(Stream.objects.filter(id__in=ids).values_list("id", "m3u_account_id"))
    channels = {}
    for stream_id, channel_id in ChannelStream.objects.filter(stream_id__in=ids).values_list("stream_id", "channel_id"):
        channels.setdefault(str(stream_id), set()).add(channel_id)
    groups = {}
    for stream_id, record in still.items():
        groups.setdefault((provider_of(account_of.get(int(stream_id))), record["frame"]), []).append(stream_id)
    for stream_ids in groups.values():
        shown_on = set().union(*(channels.get(s, set()) for s in stream_ids))
        for stream_id in stream_ids:
            record = results[stream_id]
            if len(shown_on) >= PLACEHOLDER_CHANNELS and record["kind"] != PLACEHOLDER:
                record.update(
                    kind=PLACEHOLDER,
                    reason=f"Shows the same still picture as {len(shown_on) - 1} other channels of this provider: its \"no stream\" card",
                )
            elif len(shown_on) < PLACEHOLDER_CHANNELS and record["kind"] == PLACEHOLDER:
                record["kind"] = FROZEN


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
    if (round_ or {}).get("kind") == "recheck":
        # Not a full run: when the next full one is due is left as it was
        _rechecked(round_.get("only") or ())
        redis_client.delete(ROUND_KEY)
        _progress(redis_client, state="done", finished_at=_now(), waiting=False, message="", last_recheck=_now())
        logger.info(f"Stream Check: recheck finished, {done} failing streams looked at again")
        return "done"
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


def _failing(results):
    return {
        stream_id: record for stream_id, record in results.items()
        if record and not record.get("ok") and not record.get("skipped")
    }


def rechecks_due(settings, now=None):
    """The failing streams due to be looked at again, by the rule chosen."""
    if not settings.get("recheck_failed"):
        return []
    failing = _failing(load_results()["streams"])
    if settings.get("recheck_mode") == "refresh":
        refreshed = set(_load(RECHECK_KEY, {}).get("due") or ())
        return sorted(int(i) for i in failing if i in refreshed)
    cutoff = datetime.fromtimestamp(
        (now or time.time()) - float(settings.get("recheck_hours") or 3) * 3600, timezone.utc
    ).isoformat(timespec="milliseconds")
    return sorted(int(i) for i, record in failing.items() if record.get("checked_at", "") < cutoff)


def after_playlist_refresh(account_id):
    """
    A provider's playlist was refreshed: its failing streams are to be looked at again, when
    rechecks follow refreshes. Called at the end of every successful M3U refresh, so it does
    nothing at all unless Stream Check is on and set to it.
    """
    settings = load_settings()
    if not (settings.get("enabled") and settings.get("recheck_failed") and settings.get("recheck_mode") == "refresh"):
        return 0
    from django.db import transaction

    from core.models import CoreSettings

    from .models import Stream

    failing = _failing(load_results()["streams"])
    if not failing:
        return 0
    of_account = {
        str(i) for i in Stream.objects.filter(id__in=[int(i) for i in failing], m3u_account_id=account_id)
        .values_list("id", flat=True)
    }
    if not of_account:
        return 0
    with transaction.atomic():
        row, _ = CoreSettings.objects.select_for_update().get_or_create(
            key=RECHECK_KEY, defaults={"name": "Stream Check rechecks", "value": {}}
        )
        due = set((row.value or {}).get("due") or ()) | of_account
        row.value = {"due": sorted(due)}
        row.save(update_fields=["value"])
    logger.info(f"Stream Check: {len(of_account)} failing streams to look at again after a playlist refresh")
    return len(of_account)


def _rechecked(stream_ids):
    """Streams looked at again: no longer waiting for a refresh to be looked at."""
    done = {str(i) for i in stream_ids}
    if not done:
        return
    from django.db import transaction

    from core.models import CoreSettings

    with transaction.atomic():
        row = CoreSettings.objects.select_for_update().filter(key=RECHECK_KEY).first()
        if row is None:
            return
        row.value = {"due": sorted(set((row.value or {}).get("due") or ()) - done)}
        row.save(update_fields=["value"])


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


def park(stream_id, reason="", auto=False):
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
            _hide_emptied([link["channel_id"] for link in links])
            parked[str(stream_id)] = {
                "channels": [{"channel": c, "order": o} for c, o in sorted(places.items())],
                "parked_at": (parked.get(str(stream_id)) or {}).get("parked_at") or _now(),
                "name": stream.name,
                "reason": reason,
                # Parked by autopark rather than by a person: put back by itself when it plays
                "auto": bool(auto),
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
            _show_again([place["channel"] for place in entry.get("channels") or []])
        return put

    return _update_parked(change)


def hidden_channels():
    """{channel id: {"name", "hidden_at"}}: the channels Stream Check hid, and only those."""
    return dict(_load(HIDDEN_KEY, {}))


def _hide_emptied(channel_ids):
    """
    Hide the channels a park left without a real stream -- a custom fallback does not count:
    it plays a "could not play" screen, not the channel. Stock Dispatcharr's own switch for it
    (hidden_from_output), which every output honours. Set with an update rather than a save,
    so it passes by what a save sets off: in a group numbered compactly, stock gives a hidden
    channel's number away, and it would come back elsewhere.
    """
    from .models import Channel, ChannelStream

    if not load_settings().get("hide_emptied_channels", True) or not channel_ids:
        return
    real_left = set(
        ChannelStream.objects.filter(channel_id__in=channel_ids, stream__is_custom=False)
        .values_list("channel_id", flat=True)
    )
    emptied = [
        channel for channel in Channel.objects.filter(id__in=set(channel_ids) - real_left)
        # One a person hid already is theirs: not recorded, so never shown again by this
        if not channel.hidden_from_output
    ]
    if not emptied:
        return
    Channel.objects.filter(id__in=[c.id for c in emptied]).update(hidden_from_output=True)

    def change(hidden):
        for channel in emptied:
            hidden[str(channel.id)] = {"name": channel.name, "number": channel.channel_number, "hidden_at": _now()}

    _change_key(HIDDEN_KEY, "Stream Check hidden channels", change)
    logger.info(f"Stream Check: hid {len(emptied)} channel(s) with nothing but parked streams: "
                + ", ".join(c.name for c in emptied))


def _show_again(channel_ids):
    """Show again the channels Stream Check hid, now that a stream of theirs is back."""
    from .models import Channel, ChannelStream

    ours = hidden_channels()
    back = [
        channel_id for channel_id in set(channel_ids)
        if str(channel_id) in ours
        and ChannelStream.objects.filter(channel_id=channel_id, stream__is_custom=False).exists()
    ]
    if not back:
        return
    Channel.objects.filter(id__in=back).update(hidden_from_output=False)

    def change(hidden):
        for channel_id in back:
            hidden.pop(str(channel_id), None)

    _change_key(HIDDEN_KEY, "Stream Check hidden channels", change)
    logger.info(f"Stream Check: showed {len(back)} channel(s) again, a stream of theirs being back")


def _change_key(key, name, change):
    """Read, change and write one CoreSettings row in one go."""
    from django.db import transaction

    from core.models import CoreSettings

    with transaction.atomic():
        row, _ = CoreSettings.objects.select_for_update().get_or_create(key=key, defaults={"name": name, "value": {}})
        value = dict(row.value or {})
        change(value)
        row.value = value
        row.save(update_fields=["value"])


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
    ignored = load_ignored()

    def state(stream_id):
        if str(stream_id) in ignored:
            return "ignored"
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
        # Picture faults being looked at again: shown, not yet counted
        suspects = [s for s in real if s["state"] == "suspect"]
        # Failing in a way autopark never acts on: refused, or playing the wrong picture
        needs_you = [s for s in bad if (s.get("result") or {}).get("kind") not in ("", None, DEAD)]
        broken = [s for s in real if s["state"] == "broken"]
        working = [s for s in real if s["state"] == "ok"]
        if show == "broken" and not broken:
            continue
        if show == "problems" and not bad and not suspects:
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
            "needs_you": len(needs_you),
            "suspects": len(suspects),
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
        # By autopark or by a person, and why
        row["auto"] = bool(entry.get("auto"))
        row["park_reason"] = entry.get("reason", "")
        row["from"] = [
            {"id": p["channel"], "name": names.get(p["channel"], "a deleted channel"), "order": p.get("order", 0)}
            for p in entry.get("channels") or []
        ]
        parked_rows.append(row)
    parked_rows.sort(key=lambda r: r["name"].lower())
    # The channels hidden because nothing of theirs is left but parked streams; one a person
    # has shown again since is no longer listed
    hidden = hidden_channels()
    still_hidden = set(
        Channel.objects.filter(id__in=[int(i) for i in hidden], hidden_from_output=True).values_list("id", flat=True)
    )
    hidden_rows = sorted(
        ({"id": int(i), **entry} for i, entry in hidden.items() if int(i) in still_hidden),
        key=lambda row: (row.get("number") or 0),
    )
    known_ignored = {
        s.id: s for s in Stream.objects.select_related("m3u_account").filter(id__in=[int(i) for i in ignored])
    }
    ignored_rows = sorted(
        (
            {
                **(_stream_row(known_ignored[int(i)], results, "ignored") if int(i) in known_ignored
                   else {"id": int(i), "name": entry.get("name", ""), "account": "", "gone": True}),
                "ignored_at": entry.get("ignored_at", ""),
                "ignore_reason": entry.get("reason", ""),
            }
            for i, entry in ignored.items()
        ),
        key=lambda row: row["name"].lower(),
    )
    return {"rows": rows, "parked": parked_rows, "hidden_channels": hidden_rows, "ignored": ignored_rows}


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
