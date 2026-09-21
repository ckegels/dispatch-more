"""
What a channel is, according to somebody who is not a provider.

The matching has, until now, had only two names to go on and its own judgement about how
alike they read. That is why it needed a hand-written table to know that "NGC" is National
Geographic, and a hand-written list of four-letter words that are not American call signs.
Both of those are me guessing, and both have been wrong.

Two references replace the guessing:

- **iptv-org's channel database**, which the logo library already downloads for its logos
  (`logo_library.IPTV_ORG_CHANNELS`). Every channel carries its name, **the other names it
  goes by**, its network, its country and whether it has closed -- and an id in the exact
  `Name.cc` shape an XMLTV tvg-id is written in. Two names that resolve to one entry are
  one channel however little they read alike; two names that resolve to different entries
  are different channels however much they do.

- **The call signs in your own guides.** A call sign is proof of which station a name is,
  so reading one where there is none is how a Belgian channel was offered a Slovak guide.
  Rather than ship the FCC's list -- which is a large download, goes stale, and is right
  about stations nobody here has -- a word is only taken as a call sign when some guide in
  this install actually carries it as one: as "WHYY-DT" in a name, or "WHYY.us" in a
  tvg-id. Self-calibrating, nothing to download, and wrong about nothing anybody watches.

Both are held in the cache rather than a table, the way the logo index is (§3: no
migrations). Neither is ever required: with nothing downloaded and no catalogue read, the
matching behaves exactly as it did before, which is also what the tests rely on.
"""

import logging
import re

logger = logging.getLogger(__name__)

# How long a downloaded reference is kept before it is fetched again. Channel databases
# change by the week, not the minute.
KEPT_SECONDS = 24 * 3600
KNOWN_KEY = "channel-manager:known-channels"
CALL_SIGNS_KEY = "channel-manager:call-signs"
KNOWN_SIZE_KEY = "channel-manager:known-channels-size"

# What an American station's name ends in, which is what says the letters in front of it
# are a call sign and not a word: digital television, a low-power or class-A licence.
STATION_ENDINGS = "dt|tv|cd|ld|lp|ca|dt1|dt2"
# "WHYY-DT", "KQED TV": the ending is what says the letters in front of it are a station's
# call sign rather than four letters that happen to begin with W. The shape alone is not
# enough and never was -- that is exactly how "NGC WILD" came to be a station.
CALL_SIGN_IN_NAME = re.compile(r"\b([kw][a-z]{2,3})[-_. ](?:%s)\b" % STATION_ENDINGS)
# "WHYY.us", "WHYY-DT.us": the id a station is published under, which is the call sign and
# nothing else. A guide called "NGC.Wild.HD.sk" is not this shape and says nothing.
CALL_SIGN_AS_ID = re.compile(
    r"^([kw][a-z]{2,3})(?:[-_.](?:%s))?(?:\.[a-z]{2})?$" % STATION_ENDINGS
)


def _cache():
    from django.core.cache import cache

    return cache


# ── Which channel a name is, according to iptv-org ───────────────────────────


def build_known(cache=None):
    """
    Download iptv-org's channel list and keep it as {name as a match key: entry}.

    Every name a channel goes by points at the same entry, which is the whole point: a
    playlist writes "NGC WILD" and a guide writes "Nat Geo Wild", and the database knows
    they are one channel without anybody having to write that down here.

    A name two different channels both go by is dropped rather than guessed at: pointing
    "Sports" at whichever entry happened to be read last would be worse than not knowing.
    """
    from . import logo_library

    cache = _cache() if cache is None else cache
    found = {}
    clashes = set()
    for channel in logo_library._get_json(logo_library.IPTV_ORG_CHANNELS):
        entry = {
            "id": channel.get("id") or "",
            "name": channel.get("name") or "",
            "country": (channel.get("country") or "").lower(),
            "network": channel.get("network") or "",
            "closed": bool(channel.get("closed")),
        }
        if not entry["id"]:
            continue
        for name in [channel.get("name")] + list(channel.get("alt_names") or []):
            key = logo_library.match_key(name or "")
            if not key or key in clashes:
                continue
            there = found.get(key)
            if there and there["id"] != entry["id"]:
                # Two channels of one name: this name says nothing about which
                del found[key]
                clashes.add(key)
                continue
            found[key] = entry
    size = logo_library.keep_json(cache, KNOWN_KEY, found, KEPT_SECONDS, KNOWN_SIZE_KEY)
    forget_what_is_held()
    logger.info(
        f"Channel reference built: {len(found)} names, {len(clashes)} names dropped for "
        f"belonging to more than one channel, {size / 1024 / 1024:.1f} MB kept"
    )
    return {"names": len(found), "dropped": len(clashes), "bytes": size}


# The reference unpacked, kept for a moment so a run does not unpack it again for every
# batch and the picker not twice for one window. Three and a half megabytes of JSON to
# gunzip and parse is nothing once and a great deal ten times.
_HELD = {"at": 0.0, "known": None, "calls": None}
HELD_SECONDS = 60


def known(cache=None):
    """The reference as it was last built, or {} when it has never been."""
    import time

    from . import logo_library

    if cache is None and _HELD["known"] is not None and time.time() - _HELD["at"] < HELD_SECONDS:
        return _HELD["known"]
    unpacked = logo_library.read_json(_cache() if cache is None else cache, KNOWN_KEY) or {}
    if cache is None:
        _HELD["known"], _HELD["at"] = unpacked, time.time()
    return unpacked


def forget_what_is_held():
    """Drop what is held, so the next ask reads it again. For tests and after a build."""
    _HELD.update(at=0.0, known=None, calls=None)


def known_size(cache=None):
    """How much room the kept reference takes, in bytes."""
    cache = _cache() if cache is None else cache
    try:
        return int(cache.get(KNOWN_SIZE_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def forget(cache=None):
    """Throw the downloaded reference away; it is one file and comes back in a second."""
    cache = _cache() if cache is None else cache
    size = known_size(cache)
    cache.delete(KNOWN_KEY)
    cache.delete(KNOWN_SIZE_KEY)
    cache.delete(CALL_SIGNS_KEY)
    forget_what_is_held()
    logger.info(f"Channel reference forgotten ({size} bytes freed)")
    return size


def which_channel(name, reference=None):
    """
    Which channel this name is, or None when nobody knows.

    The whole name only. A name that merely contains a known one is not that channel --
    that way round is how "Nickelodeon Teen" answers to "Eén" (see logo_library).
    """
    from . import logo_library

    reference = known() if reference is None else reference
    if not reference:
        return None
    return reference.get(logo_library.match_key(name or "")) or None


# ── Which words in your guides are really call signs ─────────────────────────


def call_signs_in(catalogue):
    """
    Every call sign the guides in this install actually carry, as lower-case letters.

    Taken from what a guide calls itself ("WHYY-DT") and from the id it is published under
    ("WHYY.us"), which are the two places a station's call sign is written down. A word
    that is a call sign nowhere in your own guides is a word.
    """
    found = set()
    for row in catalogue or ():
        name = str(row.get("name") or "").lower()
        tvg = str(row.get("original_tvg_id") or row.get("tvg_id") or "").strip().lower()
        for said in CALL_SIGN_IN_NAME.finditer(name):
            found.add(said.group(1))
        said = CALL_SIGN_AS_ID.match(tvg)
        if said:
            found.add(said.group(1))
    return found


def build_call_signs(catalogue, cache=None):
    """Work out the call signs in these guides and keep them, so a run does it once."""
    from . import logo_library

    cache = _cache() if cache is None else cache
    found = sorted(call_signs_in(catalogue))
    # Packed like everything else beside it, so one way in and one way out
    logo_library.keep_json(cache, CALL_SIGNS_KEY, found, KEPT_SECONDS)
    forget_what_is_held()
    logger.info(f"Channel reference: {len(found)} call sign(s) found in the guides")
    return found


def call_signs(cache=None):
    """
    The call signs last found in the guides, or None when they have never been looked for.

    None and an empty set are different answers: nothing looked yet means judge a call
    sign the way it was always judged, and nothing found means there are none.
    """
    import time

    from . import logo_library

    if cache is None and _HELD["calls"] is not None and time.time() - _HELD["at"] < HELD_SECONDS:
        return _HELD["calls"]
    found = logo_library.read_json(_cache() if cache is None else cache, CALL_SIGNS_KEY)
    found = set(found) if found is not None else None
    if cache is None:
        _HELD["calls"] = found
    return found
