"""
Do a channel's streams show the same thing? (Dispatch More, fork/picture-check.md, step 1)

Names say which provider stream is which channel, and names are wrong now and then: a
provider's MTV from Canada on a French MTV. What is on screen is the only proof, so every
stream to compare is opened at the same moment and read for a minute and a half, and what
the picture and the sound do is compared -- never one frame, since providers are seconds
apart and a still frame proves nothing.

**A fingerprint** (`record`): ffmpeg reads the stream for RECORD_SECONDS and writes two
pictures a second shrunk to 9x8 grey (a 64-bit difference hash each: which of two
neighbouring pixels is lighter), the loudness ten times a second, and a small picture every
THUMB_EVERY seconds for a person to look at. No video is kept.

**A comparison** (`compare`): one fingerprint is slid along the other, half a second at a
time and up to MAX_OFFSET either way -- providers are often 5-30 s apart -- and at each
offset the pictures are compared where the reference *changes* (a cut, a move: a picture
that stays as it is says nothing, a studio or a logo looks alike on many channels). The
offset where most of them match is the delay between the two. Same: most changing moments
match there, clearly better than at any other offset, and the sound does not say otherwise.
Different: no delay stands out and not even half match at the best one. Anything else, or too few changing moments: can't
tell, which is never evidence either way.

**A channel** (`compare_channel`): its first stream is the reference. Two logins of one
provider carry the same streams, so a stream is compared once per provider and name, not
once per login. Each session opens the reference and, at the same time, one other stream
on each other login (a login holds one connection) -- the HD and FHD of one provider through
its two logins, and another provider's through its own -- until every one was compared.
A connection is taken the way a viewer takes one, and a viewer needing it stops the session.

It only says so (Stream Check's page shows it); nothing is changed.
"""

import base64
import glob
import logging
import os
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

RECORD_SECONDS = 90
FRAMES_PER_SECOND = 2
LOUD_PER_SECOND = 10
AUDIO_RATE = 2000
THUMB_EVERY = 10
# Providers are this far apart at most, either way
MAX_OFFSET = 60
# Aligned stretches shorter than this say too little
LEAST_OVERLAP = 30
# Two pictures are the same scene within this many of 64 bits: another encode, another
# size or a provider's logo in a corner moves a few
SAME_BITS = 12
# The reference picture changed at least this much in the second before: a moment worth
# comparing (a still picture matches anything that is still)
MOVING_BITS = 6
# Too few changing moments and there is nothing to go on
LEAST_MOVING = 15
SAME_SHARE = 0.6
# ... and this much better than the offsets around it: one clear delay, not a picture that
# looks alike at every offset
CLEAR_BY = 0.3
# Different: no delay stands out from the rest (a programme matches another now and then,
# flat pictures most of all -- black bars, a studio -- but never clearly at one delay) and
# not even half match at the best of them
DIFFERENT_CLEAR = 0.15
DIFFERENT_SHARE = 0.5
# The sound at the same delay: below this, it says the two are not the same
SOUND_DISAGREES = 0.2
SAME, DIFFERENT, UNSURE = "same", "different", "unsure"

RESULT_KEY = "picture-compare:{channel_id}"
RESULT_TTL = 14 * 86400
LOCK_KEY = "picture-compare:running"
LOCK_TTL = 30 * 60


# ── A fingerprint ────────────────────────────────────────────────────────────


def dhash(gray):
    """The 64-bit difference hash of a 9x8 grey picture: is each pixel lighter than the next?"""
    bits = 0
    for row in range(8):
        line = gray[row * 9:row * 9 + 9]
        for col in range(8):
            bits = (bits << 1) | (1 if line[col] > line[col + 1] else 0)
    return bits


def _bits(a, b):
    return bin(a ^ b).count("1")


def record(source, user_agent="", seconds=RECORD_SECONDS, should_stop=lambda: False):
    """
    {"frames": [hash, two a second], "loud": [ten a second] or [], "thumbs": [base64 JPEG],
    "error": ""} for seconds of source (a URL or a file).
    """
    with tempfile.TemporaryDirectory(prefix="picture-") as folder:
        sound = os.path.join(folder, "sound.raw")
        thumbs = os.path.join(folder, "thumb%02d.jpg")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if user_agent:
            command += ["-user_agent", user_agent]
        if "://" in str(source):
            command += ["-rw_timeout", "15000000"]
        command += [
            "-t", str(seconds), "-i", str(source),
            "-map", "0:v:0", "-vf", f"fps={FRAMES_PER_SECOND},scale=9:8:flags=area,format=gray",
            "-f", "rawvideo", "pipe:1",
            "-map", "0:a:0?", "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", sound,
            "-map", "0:v:0", "-vf", f"fps=1/{THUMB_EVERY},scale=192:-2", "-q:v", "7", thumbs,
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (FileNotFoundError, OSError) as e:
            return {"frames": [], "loud": [], "thumbs": [], "error": f"ffmpeg could not start: {e}"}
        out = bytearray()
        reader = threading.Thread(target=lambda: out.extend(process.stdout.read()), daemon=True)
        reader.start()
        give_up = time.monotonic() + seconds + 60
        stopped = False
        while process.poll() is None:
            if should_stop() or time.monotonic() > give_up:
                stopped = True
                process.kill()
                break
            time.sleep(0.2)
        process.wait()
        reader.join(timeout=5)
        error = (process.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()
        frames = [dhash(out[at:at + 72]) for at in range(0, len(out) - 71, 72)]
        loud = _loudness(sound)
        pictures = []
        for path in sorted(glob.glob(os.path.join(folder, "thumb*.jpg"))):
            with open(path, "rb") as f:
                pictures.append(base64.b64encode(f.read()).decode())
    if stopped and should_stop():
        why = "stopped"
    elif not frames:
        why = error[-1] if error else "Nothing came"
    else:
        why = ""
    return {"frames": frames, "loud": loud, "thumbs": pictures, "error": why}


def _loudness(path):
    """The sound's strength ten times a second (root mean square), or [] without sound."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    block = AUDIO_RATE // LOUD_PER_SECOND * 2
    loud = []
    for at in range(0, len(raw) - block + 1, block):
        piece = memoryview(raw[at:at + block]).cast("h")
        loud.append((sum(v * v for v in piece) / len(piece)) ** 0.5)
    return loud if any(loud) else []


# ── A comparison ─────────────────────────────────────────────────────────────


def _correlation(a, b):
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    da = [x - ma for x in a]
    db = [y - mb for y in b]
    top = sum(x * y for x, y in zip(da, db))
    bottom = (sum(x * x for x in da) * sum(y * y for y in db)) ** 0.5
    return top / bottom if bottom else None


def _sound_at(ref, other, offset_frames):
    """How alike the loudness of the two is with other offset_frames behind ref."""
    if not ref or not other:
        return None
    shift = round(offset_frames * LOUD_PER_SECOND / FRAMES_PER_SECOND)
    if shift >= 0:
        return _correlation(ref, other[shift:])
    return _correlation(ref[-shift:], other)


def compare(ref, other, max_offset=MAX_OFFSET):
    """
    {"verdict", "offset" (seconds other is behind ref), "moving", "matched", "share", "clear",
    "sound", "why"}: what the two fingerprints say about being one channel.
    """
    a, b = ref["frames"], other["frames"]
    gap = FRAMES_PER_SECOND
    moving = [i for i in range(gap, len(a)) if _bits(a[i], a[i - gap]) >= MOVING_BITS]
    span = max_offset * FRAMES_PER_SECOND
    least = LEAST_OVERLAP * FRAMES_PER_SECOND
    shares = {}
    for offset in range(-span, span + 1):
        looked = matched = 0
        for i in moving:
            j = i + offset
            if 0 <= j < len(b):
                looked += 1
                matched += _bits(a[i], b[j]) <= SAME_BITS
        overlap = min(len(a), len(b) - offset) - max(0, -offset)
        # Too few moments at an offset and chance alone makes a peak there
        if overlap >= least and looked >= LEAST_MOVING:
            shares[offset] = (matched / looked, looked, matched)
    found = {"verdict": UNSURE, "offset": None, "moving": len(moving), "matched": 0, "share": 0.0,
             "clear": 0.0, "sound": None, "why": ""}
    if len(moving) < LEAST_MOVING:
        found["why"] = "The picture hardly changes, so it proves nothing"
        return found
    if not shares:
        found["why"] = "Too little came from one of them to compare"
        return found
    best = max(shares, key=lambda o: shares[o][0])
    share, looked, matched = shares[best]
    # The best elsewhere, away from this delay and its neighbours (a slow scene matches a
    # second either side too)
    away = [s for o, (s, _, _) in shares.items() if abs(o - best) > 2 * FRAMES_PER_SECOND]
    clear = share - (max(away) if away else 0.0)
    sound = _sound_at(ref.get("loud"), other.get("loud"), best)
    found.update(offset=best / FRAMES_PER_SECOND, matched=matched, share=round(share, 2),
                 clear=round(clear, 2), sound=None if sound is None else round(sound, 2))
    behind = abs(best / FRAMES_PER_SECOND)
    when = f"{behind:g} s {'behind' if best > 0 else 'ahead'}" if best else "in step"
    if share >= SAME_SHARE and clear >= CLEAR_BY:
        if sound is not None and sound < SOUND_DISAGREES:
            found["why"] = f"The picture matches ({when}), but the sound does not"
        else:
            found["verdict"] = SAME
            found["why"] = f"{matched} of {looked} changing moments match, {when}"
    elif clear < DIFFERENT_CLEAR and share < DIFFERENT_SHARE:
        found["verdict"] = DIFFERENT
        found["why"] = f"At best {matched} of {looked} changing moments match"
    else:
        found["why"] = f"{matched} of {looked} changing moments match ({when}): not clearly either"
    return found


def _thumb_near(thumbs, second):
    if not thumbs:
        return ""
    at = max(0, min(len(thumbs) - 1, round(second / THUMB_EVERY)))
    return thumbs[at]


# ── A channel ────────────────────────────────────────────────────────────────


def _same_name(name):
    return " ".join((name or "").lower().split())


def plan_sessions(streams, provider_of, logins_of):
    """
    ([(reference, {login: stream})], [streams left out]): what is opened together, in
    order. streams are the channel's real streams in its order, each {"id", "name",
    "account_id"}; provider_of maps an account to its provider; logins_of an account to its
    logins (profile ids), each holding one connection.

    A stream is compared once per provider and name: two logins of one provider carry the
    same one, and whichever of them has a login free is opened. Each session opens the
    reference on its login and one stream on every other login. What is only on the
    reference's own login cannot be opened at the same moment, and is left out.
    """
    if not streams:
        return [], []
    ref = streams[0]
    ref_logins = logins_of.get(ref["account_id"]) or []
    if not ref_logins:
        return [], []
    ref_login = ref_logins[0]
    ref_key = (provider_of(ref["account_id"]), _same_name(ref["name"]))
    alike = {}
    for stream in streams[1:]:
        key = (provider_of(stream["account_id"]), _same_name(stream["name"]))
        if key != ref_key:
            alike.setdefault(key, []).append(stream)
    left = list(alike.values())
    sessions = []
    while left:
        taken, rest = {}, []
        for copies in left:
            picked = None
            for stream in copies:
                free = [
                    login for login in logins_of.get(stream["account_id"]) or []
                    if login != ref_login and login not in taken
                ]
                if free:
                    picked = (free[0], stream)
                    break
            if picked:
                taken[picked[0]] = picked[1]
            else:
                rest.append(copies)
        if not taken:
            break
        sessions.append((ref, taken))
        left = rest
    return sessions, [copies[0] for copies in left]


def _save(redis_client, channel_id, state):
    import json

    redis_client.set(RESULT_KEY.format(channel_id=channel_id), json.dumps(state), ex=RESULT_TTL)


def load(redis_client, channel_id):
    import json

    raw = redis_client.get(RESULT_KEY.format(channel_id=channel_id))
    return json.loads(raw) if raw else None


def compare_channel(channel_id, redis_client, seconds=RECORD_SECONDS):
    """Compare every stream of the channel with its first (see the module's text); kept in Redis."""
    from apps.m3u.connection_pool import release_profile_slot

    from .models import Channel
    from .stream_check import YIELD_KEY, _all_profiles, _group_providers, _take_connection, _url_for

    channel = Channel.objects.get(id=channel_id)
    streams = [s for s in channel.streams.all().order_by("channelstream__order") if not s.is_custom]
    by_id = {s.id: s for s in streams}
    profiles = list(_all_profiles())
    key_of, _ = _group_providers(profiles)
    profile_by_id = {p.id: p for p in profiles}
    logins_of = {}
    for p in sorted(profiles, key=lambda p: (not p.is_default, p.id)):
        logins_of.setdefault(p.m3u_account_id, []).append(p.id)
    names = {p.m3u_account_id: p.m3u_account.name for p in profiles}

    def provider_of(account_id):
        return key_of.get(("account", account_id), ("account", account_id))

    plain = [{"id": s.id, "name": s.name, "account_id": s.m3u_account_id} for s in streams]
    sessions, left_out = plan_sessions(plain, provider_of, logins_of)
    state = {
        "channel": {"id": channel.id, "name": channel.name},
        "state": "running", "started": time.time(), "session": 0, "sessions": len(sessions),
        "reference": None, "results": [], "error": "",
    }
    if not sessions:
        state.update(state="done", error="Nothing to compare: one stream, or one login for all of them")
        _save(redis_client, channel_id, state)
        return state
    ref = by_id[sessions[0][0]["id"]]
    state["reference"] = {"id": ref.id, "name": ref.name, "account": names.get(ref.m3u_account_id, "")}
    _save(redis_client, channel_id, state)

    def a_viewer_needs_it():
        return bool(redis_client.exists(YIELD_KEY))

    for number, (_, taken) in enumerate(sessions, 1):
        state["session"] = number
        _save(redis_client, channel_id, state)
        wanted = {logins_of[ref.m3u_account_id][0]: ref, **{l: by_id[s["id"]] for l, s in taken.items()}}
        held = {}
        try:
            for login, stream in wanted.items():
                got = _take_connection([profile_by_id[login]], redis_client)
                if got is not None:
                    held[login] = (got, stream)
            if logins_of[ref.m3u_account_id][0] not in held:
                state.update(state="done", error="The reference's login is in use: try again when nobody is watching")
                _save(redis_client, channel_id, state)
                return state
            recordings = {}

            def run(login, profile, stream):
                recordings[login] = record(
                    _url_for(stream, profile), profile.m3u_account.get_user_agent_string() or "",
                    seconds, a_viewer_needs_it,
                )

            threads = [threading.Thread(target=run, args=(l, p, s), daemon=True) for l, (p, s) in held.items()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            for profile, _ in held.values():
                release_profile_slot(profile.id, redis_client)
        if a_viewer_needs_it():
            state.update(state="done", error="Stopped: a viewer needed the connection")
            _save(redis_client, channel_id, state)
            return state
        ref_print = recordings.get(logins_of[ref.m3u_account_id][0]) or {}
        for login, stream in ((l, by_id[s["id"]]) for l, s in taken.items()):
            row = {"id": stream.id, "name": stream.name, "account": names.get(stream.m3u_account_id, ""),
                   "same_provider": provider_of(stream.m3u_account_id) == provider_of(ref.m3u_account_id)}
            mine = recordings.get(login)
            if login not in held:
                row.update(verdict=UNSURE, why="Its login was in use")
            elif ref_print.get("error") or not ref_print.get("frames"):
                row.update(verdict=UNSURE, why=f"The reference did not play: {ref_print.get('error') or 'nothing came'}")
            elif not mine or mine.get("error") or not mine.get("frames"):
                row.update(verdict=UNSURE, why=f"It did not play: {(mine or {}).get('error') or 'nothing came'}")
            else:
                row.update(compare(ref_print, mine))
                middle = seconds / 2
                row["thumb_ref"] = _thumb_near(ref_print["thumbs"], middle)
                row["thumb"] = _thumb_near(mine["thumbs"], middle + (row.get("offset") or 0))
            state["results"].append(row)
        _save(redis_client, channel_id, state)
    for stream in left_out:
        state["results"].append({
            "id": stream["id"], "name": stream["name"], "account": names.get(stream["account_id"], ""),
            "verdict": UNSURE, "why": "Only on the reference's own login, which cannot open two at once",
        })
    state["state"] = "done"
    _save(redis_client, channel_id, state)
    return state
