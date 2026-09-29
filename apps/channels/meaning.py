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
the stream came "by meaning" and with what score. The model is loaded for the run and let go
after it, as stock does.
"""

import logging
import re

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


def place(streams, records, settings):
    """
    [(stream, record, score)]: the channel each of these streams most likely is, where the
    model is sure enough and nothing contradicts it. Empty when the model cannot be loaded.
    """
    if not streams or not records:
        return []
    from . import channel_manager, epg_matching

    model, _ = epg_matching.get_sentence_transformer()
    if model is None:
        logger.warning("Lineup: Dispatcharr's language model could not be loaded; placed nothing by meaning")
        return []
    try:
        import numpy as np

        def text(name):
            return " ".join(_words(name, settings))

        records = [r for r in records if r["channel"] is not None]
        wanted = model.encode([text(r["channel"].name) for r in records], normalize_embeddings=True, batch_size=256)
        given = model.encode([text(s["name"]) for s in streams], normalize_embeddings=True, batch_size=256)
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
    finally:
        epg_matching.release_ml_models()
