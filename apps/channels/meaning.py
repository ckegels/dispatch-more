"""
A stream the Lineup's rules could not place, placed by what its name means.

The rules compare names as they are written, and two providers write one channel in ways no
rule foresees: "DE| DISCOVERY CHANNEL HD" and "┃DE┃ DISCOVERY", "BE| Plug RTL" and "┃BE┃ RTL
PLUG", "FR| CSTAR" and "┃FR┃ C STAR". Dispatcharr already ships a small language model for
its own EPG matching (all-MiniLM-L6-v2, 22M parameters, in apps/channels/epg_matching.py),
which turns a name into what it means; two names that mean one channel land close together.

Measured on the user's lineup (2026-09-29, 5,031 streams already on channels): the model's
first choice among the channels of the same country was the channel the stream is on for
94 % of them; for the 1,099 whose names differ from their channel's -- the ones the rules
miss -- 87 % at a score of 0.80 with the checks below, and most of the rest were the other
copy of a channel the lineup had twice. Its own mistakes were numbers ("VTM 1" onto VTM 3),
a "+" ("AMC" onto AMC+), and a longer name landing on a shorter one ("ZIGGO SPORT GOLF" onto
Ziggo Sport, "SWR BW" onto SWR). So a placement is only offered when nothing contradicts it:
the Lineup's own checks for a tvg-id (country, language, call sign, network, East/West, "+",
station and town), the same numbers, and no word in the stream's name that says more than
the channel's.

Only for streams the rules left without a channel, and only as a suggestion: the row says
the stream came "by meaning" and with what score. What each name means is remembered, so a
preview after an apply works out only what is new, and the model is let go ten minutes after
it was last used.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

# How close two names have to mean before a stream is offered to a channel. 0.80 kept 87 %
# right on the user's lineup; below it the right answers thin out fast (74 % at 0.60).
MIN_SCORE = 0.80
# Words that say nothing about which channel a name is, so one more of them on the stream's
# side is no reason to doubt it: "DISCOVERY CHANNEL" is Discovery
FILLER = {
    "channel", "tv", "television", "network", "the", "and", "de", "la", "le", "hd", "fhd",
    "uhd", "sd", "4k", "8k", "hevc", "h265", "h264", "raw", "vip", "live", "fernsehen",
}
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}


def _words(name, settings):
    from . import channel_manager

    text = channel_manager._strip_country_box(channel_manager.clean_name(name, settings))
    text = re.sub(r"[(\[][^)\]]*[)\]]", lambda m: " " + m.group(0)[1:-1] + " ", text)
    return [w for w in re.findall(r"[0-9a-z+!&]+", text.lower()) if w]


def _numbers(words):
    return {ROMAN.get(w, w) for w in words if w.isdigit() or w in ROMAN}


def agrees(stream, record, settings):
    """Nothing in the two names says they are two channels."""
    from . import channel_manager

    if channel_manager._tvg_contradicted(stream, record):
        return False
    theirs = _words(stream["name"], settings)
    mine = _words(record["channel"].name, settings)
    if _numbers(theirs) != _numbers(mine):
        return False
    # A word on the stream's side the channel does not have says more than the channel does:
    # "ZIGGO SPORT GOLF" is not Ziggo Sport. Written together is still the word ("ZDFINFO"
    # is ZDF INFO), and filler says nothing.
    joined = "".join(mine)
    for word in theirs:
        if word in mine or word in FILLER or word in joined or word.isdigit() or word in ROMAN:
            # Numbers were compared above, as numbers: "3" is "III"
            continue
        return False
    return True


# What each name means, by its text, for as long as the process lives: a preview after an
# apply asks about the same names again, and working them all out anew -- with the model
# loaded from disk first -- made every apply wait for it (the user's find, 2026-09-29)
_MEANINGS = {}
_MEANINGS_MOST = 100_000
# The model is let go this long after it was last used, not straight after each run
RELEASE_AFTER_SECONDS = 600
_release = {"timer": None}


def _let_go_later():
    import threading

    from . import epg_matching

    timer = _release["timer"]
    if timer is not None:
        timer.cancel()
    timer = threading.Timer(RELEASE_AFTER_SECONDS, epg_matching.release_ml_models)
    timer.daemon = True
    timer.start()
    _release["timer"] = timer


# ── Out of the web workers (v241) ────────────────────────────────────────────
#
# The Lineup preview runs in a uWSGI web worker. Loading the model there imports PyTorch,
# which a running Python cannot unload: measured on the user's server (2026-09-30) all four
# web workers held it, 550-650 MB each instead of 160-290 MB, 1.6 GB in all, long after the
# model itself had been let go. In a web worker the meanings are therefore worked out by a
# separate process that exits when done (its memory goes with it), and kept on disk --
# float32, so every score is what it was -- where every worker and the next start find them:
# a preview after an apply still has nothing new to work out. Celery (the guide index) and
# the tests work in-process as before. DISPATCHARR_MEANINGS_IN_PROCESS=true: as before
# everywhere.

STORE_DIR = os.path.join(os.environ.get("DISPATCHARR_MODELS_DIR", "/data/models"), "meanings", "lineup")
STORE_MOST = 200_000
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
ENCODER = """
import json, os, sys
import numpy as np
from sentence_transformers import SentenceTransformer
texts = json.load(open(sys.argv[1]))
model = SentenceTransformer(sys.argv[3], cache_folder=sys.argv[4])
vectors = model.encode(texts, normalize_embeddings=True, batch_size=256)
np.save(sys.argv[2], np.asarray(vectors, dtype="float32"))
"""


def in_web_worker():
    if os.environ.get("DISPATCHARR_MEANINGS_IN_PROCESS", "").lower() in ("true", "1", "yes", "on"):
        return False
    try:
        import uwsgi  # noqa: F401 -- only importable inside a uWSGI worker

        return True
    except ImportError:
        return False


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _python():
    """Dispatcharr's own Python: in a uWSGI worker sys.executable is uWSGI itself."""
    for candidate in (os.path.join(sys.prefix, "bin", "python3"), os.path.join(sys.prefix, "bin", "python"),
                      sys.executable if "python" in os.path.basename(sys.executable or "") else "",
                      shutil.which("python3") or ""):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _store_paths():
    return (os.path.join(STORE_DIR, "vectors.npy"), os.path.join(STORE_DIR, "hashes.json"),
            os.path.join(STORE_DIR, "lock"))


def _read_store():
    """({hash: row}, vectors read from disk as needed), or ({}, None)."""
    import fcntl

    import numpy as np

    vectors_path, hashes_path, lock_path = _store_paths()
    if not os.path.exists(hashes_path):
        return {}, None
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            with open(hashes_path) as fh:
                hashes = json.load(fh)
            vectors = np.load(vectors_path, mmap_mode="r")
        except (OSError, ValueError):
            return {}, None
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    if len(hashes) != len(vectors):
        return {}, None
    return {h: row for row, h in enumerate(hashes)}, vectors


def _add_to_store(hashes, vectors):
    """Append these, rewriting both files beside the old ones and moving them into place."""
    import fcntl

    import numpy as np

    vectors_path, hashes_path, lock_path = _store_paths()
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            index, old = _read_store_unlocked(vectors_path, hashes_path)
            keep = [h for h in hashes if h not in index]
            if not keep:
                return
            rows = [vectors[hashes.index(h)] for h in keep]
            if old is not None and len(index) + len(keep) <= STORE_MOST:
                all_hashes = list(index) + keep
                matrix = np.concatenate([np.asarray(old, dtype="float32"), np.asarray(rows, dtype="float32")])
            else:
                all_hashes, matrix = keep, np.asarray(rows, dtype="float32")
            np.save(vectors_path + ".tmp.npy", matrix)
            with open(hashes_path + ".tmp", "w") as fh:
                json.dump(all_hashes, fh)
            os.replace(vectors_path + ".tmp.npy", vectors_path)
            os.replace(hashes_path + ".tmp", hashes_path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_store_unlocked(vectors_path, hashes_path):
    import numpy as np

    try:
        with open(hashes_path) as fh:
            hashes = json.load(fh)
        vectors = np.load(vectors_path)
    except (OSError, ValueError):
        return {}, None
    if len(hashes) != len(vectors):
        return {}, None
    return {h: row for row, h in enumerate(hashes)}, vectors


def _encode_elsewhere(texts):
    """The texts' vectors, worked out by a process of their own, or None."""
    import numpy as np

    python = _python()
    if python is None:
        logger.warning("Lineup: no Python found to run the language model with")
        return None
    cache = os.environ.get("DISPATCHARR_MODELS_DIR", "/data/models")
    if os.environ.get("DISABLE_ML_DOWNLOADS", "false").lower() == "true" and not os.path.exists(
            os.path.join(cache, f"models--{MODEL_NAME.replace('/', '--')}")):
        logger.warning("Lineup: the language model is not downloaded and downloads are off")
        return None
    with tempfile.TemporaryDirectory() as work:
        given, answer = os.path.join(work, "texts.json"), os.path.join(work, "vectors.npy")
        with open(given, "w") as fh:
            json.dump(list(texts), fh)
        try:
            done = subprocess.run(
                [python, "-c", ENCODER, given, answer, MODEL_NAME, cache],
                capture_output=True, timeout=900,
                env={**os.environ, "TOKENIZERS_PARALLELISM": "false"},
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Lineup: the language model could not be run: {e}")
            return None
        if done.returncode != 0 or not os.path.exists(answer):
            logger.warning(f"Lineup: the language model failed: {done.stderr.decode('utf-8', 'replace')[-400:]}")
            return None
        return np.load(answer)


def _meanings_elsewhere(texts):
    unique = list(dict.fromkeys(texts))
    keys = {t: _hash(t) for t in unique}
    import numpy as np

    index, vectors = _read_store()
    new = [t for t in unique if keys[t] not in index]
    if new:
        worked_out = _encode_elsewhere(new)
        if worked_out is None or len(worked_out) != len(new):
            return None
        try:
            _add_to_store([keys[t] for t in new], worked_out)
        except OSError as e:
            logger.warning(f"Lineup: the meanings could not be kept on disk: {e}")
        stored_index, stored = _read_store()
        if stored is None or any(keys[t] not in stored_index for t in unique):
            # Not kept (a full disk): what was worked out, and what was read before, answer
            by_text = dict(zip(new, worked_out))
            return {t: (by_text[t] if t in by_text else np.asarray(vectors[index[keys[t]]]))
                    for t in texts}
        index, vectors = stored_index, stored

    return {t: np.asarray(vectors[index[keys[t]]]) for t in texts}


def meanings(texts):
    """
    {text: vector} for these texts: the ones worked out before from memory, the rest by the
    model (loaded only when something is new). None when the model cannot be loaded. In a
    web worker, from the store on disk and a process of their own (see above).
    """
    if in_web_worker():
        return _meanings_elsewhere(texts)
    from . import epg_matching

    new = [t for t in dict.fromkeys(texts) if t not in _MEANINGS]
    if new:
        model, _ = epg_matching.get_sentence_transformer()
        if model is None:
            return None
        if len(_MEANINGS) + len(new) > _MEANINGS_MOST:
            _MEANINGS.clear()
        for text, vector in zip(new, model.encode(new, normalize_embeddings=True, batch_size=256)):
            _MEANINGS[text] = vector
        _let_go_later()
    return {t: _MEANINGS[t] for t in texts}


def place(streams, records, settings):
    """
    [(stream, record, score)]: the channel each of these streams most likely is, where the
    model is sure enough and nothing contradicts it. Empty when the model cannot be loaded.
    """
    if not streams or not records:
        return []
    import numpy as np

    from . import channel_manager

    def text(name):
        return " ".join(_words(name, settings))

    records = [r for r in records if r["channel"] is not None]
    record_texts = [text(r["channel"].name) for r in records]
    stream_texts = [text(s["name"]) for s in streams]
    known = meanings(record_texts + stream_texts)
    if known is None:
        logger.warning("Lineup: Dispatcharr's language model could not be loaded; placed nothing by meaning")
        return []
    wanted = np.array([known[t] for t in record_texts])
    given = np.array([known[t] for t in stream_texts])
    countries = [channel_manager._one_country(r["country"] or "") for r in records]
    placed = []
    for k, stream in enumerate(streams):
        theirs = channel_manager._one_country(stream["country"] or "")
        candidates = [i for i, mine in enumerate(countries) if not theirs or not mine or mine == theirs]
        if not candidates:
            continue
        scores = wanted[candidates] @ given[k]
        order = np.argsort(-scores)
        for j in order[:3]:
            score = float(scores[j])
            if score < MIN_SCORE:
                break
            record = records[candidates[j]]
            if agrees(stream, record, settings):
                placed.append((stream, record, score))
                break
    logger.info(f"Lineup: {len(placed)} of {len(streams)} unplaced streams placed by meaning")
    return placed
