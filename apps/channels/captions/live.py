"""
Captions while a TV watches (fork/subtitles.md §4): what a TV polls for, about once a second,
while "Generated captions" is picked in its Subtitles menu.

Dispatch More only passes things along. The caption worker (worker.py, a job per channel)
reads the channel through this server's own proxy -- one more client of the channel the TV
is already playing, so no connection to the provider of its own -- turns its sound into text
and stamps each line with the stream's own time (PTS, seconds). The TV shows a line while the
picture it is showing has that time, which keeps them together whatever the delays are.

A job ends by itself when nobody has asked for it for a few seconds (worker.JOB_IDLE_SECONDS),
and at once when a TV says it is done (stop): changing channel, turning captions off, closing
the player.
"""

import json
import time

from . import manager

# ISO 639-2 (as streams and the Subtitles tab write it) -> the model's codes
WHISPER_LANGUAGE = {
    "dut": "nl", "nld": "nl", "eng": "en", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
    "ita": "it", "spa": "es", "por": "pt", "pol": "pl", "tur": "tr", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi",
}
# The model the proposal picks, remembered a minute: it asks the worker what it measured
_model_cache = {"at": 0.0, "model": ""}


def chosen_model(settings):
    if settings.get("model"):
        return settings["model"]
    if time.monotonic() - _model_cache["at"] < 60 and _model_cache["model"]:
        return _model_cache["model"]
    answer = manager.ask("/status", settings=settings) or {}
    machine = answer.get("machine") or manager.here()
    model = manager.propose(machine, answer.get("models") or {}, settings)["model"]
    _model_cache.update(at=time.monotonic(), model=model)
    return model


def stream_base(settings):
    if settings.get("stream_base"):
        return settings["stream_base"].rstrip("/")
    return "http://127.0.0.1:9191"


def language_for(channel_id):
    """The language set by hand for the channel on the Subtitles tab, as the model writes it,
    or None: then the worker finds it (a channel shows programmes in more than one)."""
    from ..subtitles import load_spoken

    return WHISPER_LANGUAGE.get(load_spoken().get(str(channel_id), ""))


def playing(channel_uuid):
    """Whether the proxy is playing this channel now: a job is only ever a reader alongside."""
    try:
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        from core.utils import RedisClient

        redis_client = RedisClient.get_client()
        return bool(redis_client and redis_client.exists(RedisKeys.channel_metadata(str(channel_uuid))))
    except Exception:
        return False


def poll(channel, since=0):
    """What a TV gets: {"state", "cues": [{seq, start, end, text}], ...}. States: "off" (not
    switched on, or no worker), "not playing", "busy" (every caption slot in use), and the
    worker's own: "starting", "loading model", "listening", "ended", "error"."""
    settings = manager.load()
    if not settings.get("live"):
        return {"state": "off", "cues": [], "reason": "Generated captions are switched off on the server."}
    if not playing(channel.uuid):
        return {"state": "not playing", "cues": []}
    key = str(channel.uuid)
    running = manager.ask("/jobs", settings=settings)
    if running is None or running.get("error"):
        return {"state": "off", "cues": [], "reason": "The caption worker is not running on the server."}
    others = [j for j in running.get("jobs") or [] if j.get("key") != key]
    if len(others) >= int(settings.get("channels_at_once") or 1):
        return {"state": "busy", "cues": [],
                "reason": f"Captions are being made for {len(others)} other channel(s), as many as the server allows."}
    body = json.dumps({
        "key": key,
        "url": f"{stream_base(settings)}/proxy/ts/stream/{key}",
        "model": chosen_model(settings),
        "language": language_for(channel.id),
        "since": int(since or 0),
    }).encode()
    answer = manager.ask("/jobs/poll", method="POST", settings=settings, body=body, timeout=6)
    if answer is None:
        return {"state": "off", "cues": [], "reason": "The caption worker does not answer."}
    if answer.get("error") and "state" not in answer:
        return {"state": "error", "cues": [], "reason": answer["error"]}
    return answer


def stop(channel_uuid):
    settings = manager.load()
    answer = manager.ask(f"/jobs/stop?key={channel_uuid}", method="POST", settings=settings)
    return bool(answer and answer.get("stopped"))
