# Show Groups' files, in /data/show_groups: the online answers (which took hours to gather), the
# record of which copies are whose, the plan and the activity log. The Show Groups plugin kept
# them in the same folder, so what it gathered carries over.
import json
import os
import tempfile
from datetime import datetime, timezone

from .matching import plain

LOOKUPS = "lookups.json"


def state_dir():
    # SHOW_GROUPS_DIR exists for the tests; on a server it is always /data/show_groups
    path = os.environ.get("SHOW_GROUPS_DIR") or "/data/show_groups"
    os.makedirs(path, exist_ok=True)
    return path


def path_of(name):
    return os.path.join(state_dir(), name)


def write_text(name, text):
    """Write through a temporary file, so a report or dictionary is never left half written
    when two workers run the same action at once."""
    target = path_of(name)
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.replace(temporary, target)
    return target


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _from_version_1(entry):
    """Version 1 kept one answer per title, from the survey: TVmaze first, Wikidata only when
    TVmaze had nothing. Rewritten as the sources that were asked and what each said."""
    asked = entry.get("asked") or _now()
    answer = {"name": entry.get("name") or "", "genres": entry.get("genres") or [], "asked": asked}
    source = entry.get("source") or ""
    if source == "tvmaze":
        return {"tvmaze": answer}
    if source == "wikidata":
        return {"tvmaze": {"asked": asked}, "wikidata": answer}
    return {"tvmaze": {"asked": asked}, "wikidata": {"asked": asked}}


def load_lookups():
    """plain title -> {source: answer}, one answer per source that was asked: {"name",
    "genres", "asked"}, or only {"asked"} when it did not know the title. Keeping the empty
    answers means a title is not asked again every minute."""
    try:
        with open(path_of(LOOKUPS), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    titles = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(titles, dict):
        return {}
    if data.get("version", 1) < 2:
        titles = {key: _from_version_1(entry) for key, entry in titles.items()
                  if isinstance(entry, dict)}
    return titles


def save_lookups(titles):
    return write_text(LOOKUPS, json.dumps({"version": 2, "titles": titles}, ensure_ascii=False,
                                          indent=0, sort_keys=True))


def merge_lookups(new):
    """Write new answers into the file, re-reading it first, so an import running at the same
    time as the background lookups does not lose either one's answers."""
    titles = load_lookups()
    for key, answers in new.items():
        titles.setdefault(key, {}).update(answers)
    save_lookups(titles)
    return titles


MENDED = "mended-empty-answers"


def forget_empty_answers_once():
    """Forget every "this source does not know the title" recorded before a failed request
    stopped counting as one (v234), so each is asked again. Once: a marker file says it was
    done. Only titles still in the plan's queue are asked again, busiest first, so this costs a
    few hundred requests, not one per title ever asked. Returns how many were forgotten."""
    if os.path.exists(path_of(MENDED)):
        return 0
    titles = load_lookups()
    forgotten = 0
    for answers in titles.values():
        for source in [s for s, a in answers.items() if not (a or {}).get("genres")]:
            del answers[source]
            forgotten += 1
    save_lookups({k: v for k, v in titles.items() if v})
    write_text(MENDED, _now() + f" {forgotten}\n")
    return forgotten


def online_answers(titles):
    """What the matching sees (matching.judge's from_online): per title, every genre any source
    gave, and which sources gave them. Titles no source knew are left out."""
    merged = {}
    for key, answers in titles.items():
        sources, genres = [], []
        for source, answer in sorted(answers.items(), key=lambda sa: _ORDER.get(sa[0], 9)):
            given = [g for g in (answer or {}).get("genres") or [] if g not in genres]
            if given:
                sources.append(source)
                genres.extend(given)
        if genres:
            merged[key] = {"source": "+".join(sources), "genres": genres}
    return merged


_ORDER = {"tvmaze": 0, "wikidata": 1, "wikipedia": 2, "tmdb": 3, "tvdb": 4, "trakt": 5,
          "omdb": 6}


def import_survey(survey_path):
    """Take the answers survey-online.py and survey-cooking.py gathered, so the hours they spent
    asking are not spent again.

    The survey cache is keyed by the title as written and stores TVmaze's fuzzy search answers
    unchecked. An answer whose name is not the asked title is refused here (handover §3.3: "True
    Crime Story" came back as "My True Crime Story"); Wikidata answers were already held to that
    rule when they were gathered. Answers the plugin already has are left alone."""
    with open(survey_path, encoding="utf-8") as fh:
        survey = json.load(fh)
    if not isinstance(survey, dict):
        raise ValueError(f"{survey_path} is not a survey answer cache")

    titles = load_lookups()
    counts = {"added": 0, "known already": 0, "refused (another programme)": 0, "nobody knew": 0}
    refused = []
    new = {}
    for written, answer in survey.items():
        key = plain(written)
        if not key or not isinstance(answer, dict):
            continue
        if key in titles or key in new:
            counts["known already"] += 1
            continue
        genres = [str(g) for g in answer.get("genres") or [] if str(g).strip()]
        name = str(answer.get("name") or "")
        source = answer.get("where") or ""
        if genres and plain(name) != key:
            counts["refused (another programme)"] += 1
            refused.append(f"{written} -> {name}")
            # Recorded as "TVmaze did not know it", so the other sources are still asked
            new[key] = {"tvmaze": {"asked": _now()}}
        else:
            if not genres:
                counts["nobody knew"] += 1
            new[key] = _from_version_1({"source": source, "name": name, "genres": genres})
        counts["added"] += 1
    merge_lookups(new)
    return counts, refused
