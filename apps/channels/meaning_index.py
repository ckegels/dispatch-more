"""
What every guide's name means, worked out once and kept on disk, so a guide can be found by
meaning in the time a page waits (meaning.py does the same for streams in the Lineup).

Measured on the user's server (2026-09-29, 1,377 channels with a guide, 94,789 guides):
the language model's first choice was the channel's guide for 70 % against 52 % for the name
matcher, and it was in its first five for 78 % against 62 %. What the model found and the
matcher did not were mostly American locals written another way and names with a quality in
them; the matcher kept a few the model missed ("US - PBS WBIQ Birmingham AL"), so the two are
used together, the model's picks judged like the matcher's.

Working out 94,789 names took 113 s, which no page can wait for, so it is done by a Celery
task (tasks.build_meaning_index) after a guide refresh, and again only for what changed:
each name's vector is kept under the text it was worked out from. Channels are done in the
same pass, so finding guides for one needs no model at all, only the two files: the web
processes never load torch. Each channel's closest guides are worked out in the same pass, so a lookup reads a list.

Kept beside the model Dispatcharr downloads (/data/models): guides.npy and channels.npy
(float16, 384 wide), and index.json saying which row is which.
"""

import hashlib
import json
import logging
import os
import re
import time
import unicodedata

logger = logging.getLogger(__name__)

INDEX_DIR = os.path.join(os.environ.get("DISPATCHARR_MODELS_DIR", "/data/models"), "meanings")
BUILDING_KEY = "channel-manager:meaning-index:building"
# How close a guide's name has to mean before the model's pick is offered
MIN_SCORE = 0.70
# How many guides are kept for each channel
TOP = 60
QUALITY = r"\b(4k|8k|uhd|fhd|hd|sd|hevc|h265|h264|1080p|720p|raw|vip)\b"
_loaded = {"stamp": None}


def _clean(name):
    text = re.sub(r"^\s*[┃\[|(]?\s*[A-Za-z]{2,3}\s*[┃\]|:)]\s*", "", name or "")
    text = unicodedata.normalize("NFKD", text).lower()
    text = re.sub(QUALITY, " ", text)
    return " ".join(re.sub(r"[^0-9a-z+]+", " ", text).split())


def guide_text(name, tvg_id):
    """A guide as the model reads it: its name and its tvg-id without the country."""
    tid = re.sub(r"\.[a-z]{2}$", "", (tvg_id or "").lower())
    return f"{_clean(name)} {_clean(re.sub(r'[._-]', ' ', tid))}".strip()


def channel_text(name):
    return _clean(name)


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _country(name, tvg_id):
    from . import channel_manager

    return channel_manager._one_country(channel_manager._country_of_guide({"name": name, "tvg_id": tvg_id}))


def status():
    try:
        with open(os.path.join(INDEX_DIR, "index.json")) as handle:
            meta = json.load(handle)
        return {k: meta.get(k) for k in ("built_at", "guides", "channels", "seconds", "encoded")}
    except (OSError, ValueError):
        return {}


def build(say=None):
    """Work out what every active guide and every channel means; only new texts are encoded."""
    import numpy as np

    from apps.epg.models import EPGData

    from . import epg_matching
    from .models import Channel

    started = time.monotonic()
    guides = list(
        EPGData.objects.exclude(epg_source__is_active=False)
        .exclude(epg_source__source_type="dummy")
        .values_list("id", "name", "tvg_id")
    )
    channels = list(Channel.objects.values_list("id", "name"))
    g_texts = [guide_text(name, tvg) for _, name, tvg in guides]
    c_texts = [channel_text(name) for _, name in channels]

    # What was worked out last time, by text: most of it has not changed
    known = {}
    try:
        with open(os.path.join(INDEX_DIR, "index.json")) as handle:
            old = json.load(handle)
        for kind in ("guides", "channels"):
            vectors = np.load(os.path.join(INDEX_DIR, f"{kind}.npy"))
            for row, h in enumerate(old.get(f"{kind}_hashes") or []):
                known[h] = vectors[row]
    except (OSError, ValueError):
        pass

    wanted = [t for t in dict.fromkeys(g_texts + c_texts) if _hash(t) not in known]
    if wanted:
        model, _ = epg_matching.get_sentence_transformer()
        if model is None:
            raise RuntimeError("Dispatcharr's language model could not be loaded")
        try:
            batch = 2048
            for start in range(0, len(wanted), batch):
                if say:
                    say(start, len(wanted))
                part = wanted[start:start + batch]
                for text, vector in zip(part, model.encode(part, normalize_embeddings=True, batch_size=256)):
                    known[_hash(text)] = vector.astype(np.float16)
        finally:
            epg_matching.release_ml_models()

    os.makedirs(INDEX_DIR, exist_ok=True)
    width = len(next(iter(known.values()))) if known else 384
    for kind, texts in (("guides", g_texts), ("channels", c_texts)):
        matrix = np.zeros((len(texts), width), dtype=np.float16)
        for row, text in enumerate(texts):
            matrix[row] = known[_hash(text)]
        # Written beside and moved into place: a reader never sees half a file
        np.save(os.path.join(INDEX_DIR, f"{kind}.tmp.npy"), matrix)
        os.replace(os.path.join(INDEX_DIR, f"{kind}.tmp.npy"), os.path.join(INDEX_DIR, f"{kind}.npy"))
    # Each channel's closest guides, worked out here once: finding them is then a lookup,
    # with no vectors read by the web processes at all
    from . import logo_library

    countries = np.array([_country(name, tvg) for _, name, tvg in guides])
    guide_ids = np.array([gid for gid, _, _ in guides])
    g_matrix = np.load(os.path.join(INDEX_DIR, "guides.npy")).astype(np.float32)
    c_matrix = np.load(os.path.join(INDEX_DIR, "channels.npy")).astype(np.float32)
    tops = {}
    by_country = {}
    for row, (_, name) in enumerate(channels):
        text = c_texts[row]
        if text in tops or not len(guide_ids):
            continue
        from . import channel_manager

        mine = channel_manager._one_country(logo_library.country_of(name or "") or "")
        if mine not in by_country:
            rows = np.nonzero((countries == mine) | (countries == ""))[0] if mine else np.arange(len(guide_ids))
            by_country[mine] = (rows, g_matrix[rows])
        rows, sub = by_country[mine]
        if not len(rows):
            tops[text] = []
            continue
        scores = sub @ c_matrix[row]
        best = np.argsort(-scores)[:TOP]
        tops[text] = [[int(guide_ids[rows[i]]), round(float(scores[i]), 3)] for i in best if scores[i] >= MIN_SCORE]
    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "guides": len(guides), "channels": len(channels), "encoded": len(wanted),
        "seconds": round(time.monotonic() - started, 1),
        "guide_ids": [gid for gid, _, _ in guides],
        "guide_countries": [_country(name, tvg) for _, name, tvg in guides],
        "guides_hashes": [_hash(t) for t in g_texts],
        "channel_top": tops,
        "channels_hashes": [_hash(t) for t in c_texts],
    }
    with open(os.path.join(INDEX_DIR, "index.tmp.json"), "w") as handle:
        json.dump(meta, handle)
    os.replace(os.path.join(INDEX_DIR, "index.tmp.json"), os.path.join(INDEX_DIR, "index.json"))
    logger.info(
        f"Guide meanings: {len(guides)} guides and {len(channels)} channels, "
        f"{len(wanted)} worked out anew, in {meta['seconds']} s"
    )
    return status()


def _index():
    """index.json, read again only when it changes."""
    path = os.path.join(INDEX_DIR, "index.json")
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    if _loaded["stamp"] != stamp:
        with open(path) as handle:
            _loaded.update(stamp=stamp, meta=json.load(handle))
    return _loaded


def similar_guides(channel_name, country="", limit=20):
    """
    [(epg id, score)] the guides whose names mean most what this channel's does, best first,
    from its own country or one that does not say. Empty without an index, or for a channel
    whose name is newer than it (the next build has it).
    """
    index = _index()
    if index is None:
        return []
    found = index["meta"].get("channel_top", {}).get(channel_text(channel_name)) or []
    return [(epg_id, score) for epg_id, score in found[:limit]]


def queue_build():
    """Ask the Celery worker for a build, once at a time."""
    try:
        from django.core.cache import cache

        if not cache.add(BUILDING_KEY, 1, 30 * 60):
            return False
        from .tasks import build_meaning_index

        build_meaning_index.delay()
        return True
    except Exception as e:
        logger.debug(f"Guide meanings: could not queue a build: {e}")
        return False
