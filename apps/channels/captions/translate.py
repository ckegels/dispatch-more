"""
Translated captions (fork/subtitles.md §9 step 4): a TV asks for its language (`&lang=nl` on the
poll) and gets the same lines -- same `seq`, same stream times -- in that language.

One translation per (channel, language), shared: each line is translated once and kept in Redis
for every TV reading that channel in that language. Lines go out in order; a line whose
translation is not there yet holds back the ones after it until the next poll (the TV asks
again from the last `seq` it got, so nothing is skipped).

The translator, in order (or the one chosen on the captions card): DeepL when its key is set
(Service keys), Ollama when one answers with a model, Opus-MT in the caption worker (a small
model per language pair, fetched on first use). None: the original lines, and the answer says
why.
"""

import json
import logging
import urllib.request

from . import manager

logger = logging.getLogger(__name__)

KEPT_SECONDS = 600
LOCK_SECONDS = 20
ENGINES = ("deepl", "ollama", "opus-mt")
LANGUAGE_NAMES = {
    "en": "English", "nl": "Dutch", "de": "German", "fr": "French", "it": "Italian", "es": "Spanish",
    "pt": "Portuguese", "pl": "Polish", "tr": "Turkish", "sv": "Swedish", "no": "Norwegian",
    "da": "Danish", "fi": "Finnish",
}


def _s(value):
    return value.decode() if isinstance(value, bytes) else value


def _redis():
    from core.utils import RedisClient

    return RedisClient.get_client()


def cache_key(channel_uuid, lang):
    return f"captions:tr:{channel_uuid}:{lang}"


def engine(settings):
    """(name, detail) of the translator to use, or (None, why)."""
    from ..service_keys import load as keys

    chosen = settings.get("translator") or ""
    if chosen == "off":
        return None, "Translation is switched off on the server."
    deepl_key = keys().get("deepl_key")
    if chosen in ("", "deepl") and deepl_key:
        return "deepl", deepl_key
    if chosen in ("", "ollama"):
        tags = manager.ollama(settings.get("ollama_url")) or []
        model = settings.get("ollama_model") or (tags[0] if tags else "")
        if model and (not tags or model in tags):
            return "ollama", model
    if chosen in ("", "opus-mt"):
        status = manager.ask("/status", settings=settings) or {}
        if status.get("translate"):
            return "opus-mt", ""
    return None, "No translator on the server: a DeepL key, an Ollama model, or the caption worker with Opus-MT."


def deepl(key, lines, source, target):
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    body = json.dumps({"text": lines, "source_lang": source.upper(), "target_lang": target.upper()}).encode()
    request = urllib.request.Request(f"https://{host}/v2/translate", data=body, method="POST", headers={
        "Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=8) as answer:
        return [t["text"] for t in json.loads(answer.read())["translations"]]


def ollama(url, model, lines, source, target):
    """An LLM, asked for one line per line back as a JSON list (anything else is not used)."""
    prompt = (
        f"Translate these TV subtitle lines from {LANGUAGE_NAMES.get(source, source)} to "
        f"{LANGUAGE_NAMES.get(target, target)}. Answer with only a JSON list of strings, one per "
        f"line, in the same order.\n{json.dumps(lines, ensure_ascii=False)}"
    )
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json",
                       "options": {"temperature": 0}}).encode()
    request = urllib.request.Request(url.rstrip("/") + "/api/generate", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as answer:
        text = json.loads(answer.read()).get("response") or ""
    found = json.loads(text)
    if isinstance(found, dict):  # some models wrap the list: {"lines": [...]}
        found = next((v for v in found.values() if isinstance(v, list)), None)
    if not isinstance(found, list) or len(found) != len(lines):
        raise ValueError("the model did not answer one line per line")
    return [str(x) for x in found]


def opus(settings, lines, source, target):
    body = json.dumps({"lines": lines, "source": source, "target": target}).encode()
    # The first use of a pair fetches its model (~300 MB): this poll gives up, a later one finds it
    answer = manager.ask("/translate", method="POST", settings=settings, body=body, timeout=8)
    if not answer or "lines" not in answer:
        raise ValueError((answer or {}).get("error") or "the worker did not translate")
    return answer["lines"]


def run(name, detail, settings, lines, source, target):
    if name == "deepl":
        return deepl(detail, lines, source, target)
    if name == "ollama":
        return ollama(settings.get("ollama_url"), detail, lines, source, target)
    return opus(settings, lines, source, target)


def translated(answer, channel_uuid, lang, settings=None, redis_client=None):
    """The poll's answer with its cues in `lang`, where translated already or now."""
    lang = (lang or "").strip().lower()[:2]
    source = (answer.get("language") or "")[:2]
    cues = answer.get("cues") or []
    if not lang or not cues:
        return answer
    if not source:
        # The model has not settled on the programme's language yet: hold the lines a moment
        return {**answer, "cues": [], "translation": {"to": lang, "state": "waiting for the language"}}
    if source == lang:
        return {**answer, "translation": {"to": lang, "state": "same language"}}
    settings = manager.load() if settings is None else settings
    redis_client = _redis() if redis_client is None else redis_client
    key = cache_key(channel_uuid, lang)
    have = {int(_s(k)): _s(v) for k, v in (redis_client.hgetall(key) or {}).items()}
    missing = [c for c in cues if c["seq"] not in have]
    name, detail = None, ""
    if missing:
        name, detail = engine(settings)
        if name is None:
            return {**answer, "translation": {"to": lang, "state": "not available", "reason": detail}}
        # One TV translates a piece; the others read what it kept
        if redis_client.set(f"{key}:lock", "1", nx=True, ex=LOCK_SECONDS):
            try:
                lines = run(name, detail, settings, [c["text"].strip() for c in missing], source, lang)
                redis_client.hset(key, mapping={str(c["seq"]): t for c, t in zip(missing, lines)})
                redis_client.expire(key, KEPT_SECONDS)
                have.update({c["seq"]: t for c, t in zip(missing, lines)})
            except Exception as e:
                logger.info(f"Captions: translating {channel_uuid} to {lang} with {name} failed: {e}")
                if not have:
                    # Better the original lines than none at all
                    return {**answer, "translation": {"to": lang, "state": "failed", "reason": str(e)}}
            finally:
                redis_client.delete(f"{key}:lock")
    out = []
    for cue in cues:
        if cue["seq"] not in have:
            break  # in order: the rest waits for the next poll
        out.append({**cue, "text": have[cue["seq"]], "original": cue["text"]})
    return {**answer, "cues": out,
            "translation": {"to": lang, "from": source, "state": "translated", "engine": name or "kept"}}
