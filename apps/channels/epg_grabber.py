"""
Driving iptv-org/epg from Dispatcharr: where it is, what to grab, and when.

The grabber (https://github.com/iptv-org/epg) is a Node program that scrapes listing sites
and writes XMLTV. It is not rewritten here and nothing about it is changed: this points at
an install that already works, runs it the way a person would, and takes the file it
produces the rest of the way -- which is the part that was being done by hand.

What "the rest of the way" means, and why it is here rather than in a systemd timer:

- **Never replace a good guide with a bad one.** The grab writes beside the live file and
  the live file is only replaced once the new one has been read back and found to hold
  channels and programmes. A scrape that dies half way leaves last night's guide exactly
  where it was.
- **Never two at once.** A full scrape is thousands of requests over hours; two of them at
  once is twice the load on the site and on the machine, for a worse answer.
- **Say where it has got to.** The grabber counts its jobs as it goes ("[1204/4539]"), so
  the page can say that instead of spinning.
- **Hand it over.** An EPG source whose file is on disk is read from disk by Dispatcharr
  (apps/epg/tasks.py: a source with no URL and a file that exists), so the guide goes
  straight in with no HTTP server in between. The refresh is asked for as soon as the file
  is in place.

Off unless it is switched on, and it holds nothing but a `CoreSettings` row: an install
that never touches this tab behaves exactly as it did.
"""

import logging
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone

from .settings_rows import change_row

logger = logging.getLogger(__name__)

SETTINGS_KEY = "epg-grabber"

# How a run says how it is going, and how it is asked to stop. Redis, because the page and
# the worker are different processes.
RUNNING_KEY = "epg-grabber:running"
PROGRESS_KEY = "epg-grabber:progress"
STOP_KEY = "epg-grabber:stop"
# Long enough that a scrape of thousands of channels is never mistaken for one that died,
# and refreshed while it runs
RUNNING_TTL = 6 * 3600
PROGRESS_KEPT = 7 * 86400

DEFAULTS = {
    # Nothing here runs unless this is on
    "enabled": False,
    # Where the grabber is checked out. Everything is run from in here.
    "folder": "/opt/iptv-org-epg",
    # How it is started, as the words of the command rather than a line of shell: there is
    # no shell, so nothing in the settings can turn into something else.
    #
    # Three dashes on purpose. npm eats the first pair itself, and the grabber's own
    # options have to survive that -- it is what the project's own README uses.
    "command": ["npm", "run", "grab", "---"],
    # How often to grab, and between which hours (server time; empty is any time). A full
    # scrape is hours of requests, so "overnight" is a thing people want to say.
    "every_hours": 12,
    "window_from": "",
    "window_to": "",
    # A scrape that has said nothing for this long is taken to have hung and is stopped.
    # This is the guard that does the work: a grabber that is getting on with it says a
    # line per channel per day, so silence is the thing that means something is wrong.
    "silent_for_minutes": 20,
    # ...and one still going after this long is stopped whatever it is saying. Generous on
    # purpose: a scrape of a few thousand channels one request at a time is hours, and a
    # cap that cuts off a scrape which is working means it can never finish at all.
    "give_up_after_minutes": 720,
    # What to grab. Each is one run of the grabber and one file (see _argv).
    "jobs": [],
}

# What a job is, with what the grabber does when a setting is left alone. The names are the
# grabber's own options (README: --channels, --sites, --days, --lang, --timeout, --delay,
# --maxConnections, --gzip, --proxy, --output), so what is set here reads the same as what
# somebody would have typed.
JOB_DEFAULTS = {
    "id": "",
    "name": "",
    "enabled": True,
    # One of these two: a channels file (what the tvpassport PBS list is), or a
    # comma-separated list of the grabber's own sites
    "channels": "",
    "sites": "",
    # The grabber's own defaults, where it has one: days is the site's own, a request waits
    # 30 s, nothing is delayed, one request at a time
    "days": 0,
    "lang": "",
    "timeout_ms": 0,
    "delay_ms": 0,
    "max_connections": 0,
    "proxy": "",
    "gzip": False,
    # Where the finished guide goes, and which Dispatcharr EPG source reads it
    "output": "",
    "epg_source": None,
    # How the last run of this one went
    "last": {},
}


def _load():
    from core.models import CoreSettings

    row = CoreSettings.objects.filter(key=SETTINGS_KEY).first()
    stored = row.value if row and isinstance(row.value, dict) else {}
    values = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    values["jobs"] = [{**JOB_DEFAULTS, **job} for job in values.get("jobs") or [] if isinstance(job, dict)]
    return values


def load_settings():
    """How the grabber is set up, over the defaults."""
    try:
        return _load()
    except Exception as e:
        logger.debug(f"Could not read the EPG grabber settings: {e}")
        return {**DEFAULTS, "jobs": []}


def save_settings(given):
    """What was sent, over what is there, held while it is changed (see settings_rows)."""
    def change(stored):
        values = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
        values.update({k: v for k, v in (given or {}).items() if k in DEFAULTS})
        values["enabled"] = bool(values["enabled"])
        values["folder"] = str(values["folder"] or "").strip()
        values["command"] = _words(values["command"])
        try:
            values["every_hours"] = min(168, max(1, float(values["every_hours"])))
            values["silent_for_minutes"] = min(240, max(1, int(values["silent_for_minutes"])))
            values["give_up_after_minutes"] = min(2880, max(5, int(values["give_up_after_minutes"])))
        except (TypeError, ValueError):
            raise ValueError("Numbers only, please")
        for field in ("window_from", "window_to"):
            when = str(values.get(field) or "").strip()
            if when and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", when):
                raise ValueError("Times as HH:MM, please")
            values[field] = when
        values["jobs"] = [_clean_job(job) for job in values.get("jobs") or []]
        stored.clear()
        stored.update(values)
        return values

    return change_row(SETTINGS_KEY, "EPG grabber", change)


def _words(command):
    """The command as its words. A string is read the way a shell would read it, once."""
    if isinstance(command, str):
        command = shlex.split(command)
    words = [str(one) for one in command or [] if str(one).strip()]
    if not words:
        raise ValueError("What should be run?")
    return words


def _clean_job(given):
    job = {**JOB_DEFAULTS, **{k: v for k, v in (given or {}).items() if k in JOB_DEFAULTS}}
    job["id"] = str(job["id"] or "").strip() or _new_id()
    job["name"] = str(job["name"] or "").strip() or "Guide"
    job["enabled"] = bool(job["enabled"])
    for field in ("channels", "sites", "lang", "proxy", "output"):
        job[field] = str(job[field] or "").strip()
    job["gzip"] = bool(job["gzip"])
    try:
        job["days"] = max(0, int(job["days"] or 0))
        job["timeout_ms"] = max(0, int(job["timeout_ms"] or 0))
        job["delay_ms"] = max(0, int(job["delay_ms"] or 0))
        job["max_connections"] = max(0, int(job["max_connections"] or 0))
        job["epg_source"] = int(job["epg_source"]) if job["epg_source"] else None
    except (TypeError, ValueError):
        raise ValueError("Numbers only, please")
    if not job["channels"] and not job["sites"]:
        raise ValueError(f"{job['name']}: a channels file, or the sites to grab")
    if job["channels"] and job["sites"]:
        # The grabber takes one or the other: a channel list names exact channels and the
        # site each of them is on, so naming sites as well says two different things
        raise ValueError(
            f"{job['name']}: a channels file **or** sites, not both -- a channel list "
            f"already says which site each channel is on"
        )
    if not job["output"]:
        raise ValueError(f"{job['name']}: where should the guide be written?")
    job["last"] = job.get("last") if isinstance(job.get("last"), dict) else {}
    return job


def _new_id():
    import secrets

    return secrets.token_hex(4)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── What is there ────────────────────────────────────────────────────────────


def look_at_it(settings=None):
    """
    Whether the grabber is where it is said to be, and what it holds.

    Everything the page needs to say "this is set up" or "this is why it is not": the
    folder, whether it is the grabber, whether what runs it can be found, and how many
    sites it knows.
    """
    import shutil

    settings = settings or load_settings()
    folder = settings.get("folder") or ""
    found = {
        "folder": folder, "ok": False, "why": "", "sites": 0, "runs": "",
        "writable": False,
    }
    if not folder or not os.path.isdir(folder):
        found["why"] = "That folder is not there."
        return found
    if not os.path.isfile(os.path.join(folder, "package.json")):
        found["why"] = "No package.json in there: that is not the grabber."
        return found
    sites = os.path.join(folder, "sites")
    if not os.path.isdir(sites):
        found["why"] = "No sites folder in there: that is not the grabber, or it is not installed."
        return found
    if not os.path.isdir(os.path.join(folder, "node_modules")):
        found["why"] = "It is there, but npm install has not been run in it."
        return found
    try:
        found["sites"] = len([one for one in os.listdir(sites) if not one.startswith(".")])
    except OSError:
        found["sites"] = 0
    runs = _words(settings.get("command") or DEFAULTS["command"])[0]
    where = shutil.which(runs)
    if not where:
        found["why"] = f"{runs} was not found, so nothing here can start it."
        return found
    found["runs"] = where
    found["writable"] = os.access(folder, os.W_OK)
    found["ok"] = True
    return found


def channel_files(settings=None):
    """
    The channel lists the grabber has, for the picker: [{"path", "site", "channels"}].

    Both kinds: the ones that come with it (sites/<site>/<site>.channels.xml) and any that
    have been made by hand and left in the folder, which is where a list of one site's PBS
    stations ends up.
    """
    settings = settings or load_settings()
    folder = settings.get("folder") or ""
    found = []
    if not folder or not os.path.isdir(folder):
        return found
    for where in (os.path.join(folder, "sites"), folder, os.path.join(folder, "data")):
        if not os.path.isdir(where):
            continue
        for root, dirs, files in os.walk(where):
            # One level of sites/, and never into node_modules
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
            for name in files:
                if not name.endswith(".channels.xml") and not name.endswith(".xml"):
                    continue
                if not name.endswith(".channels.xml") and root == folder:
                    continue
                path = os.path.join(root, name)
                found.append({
                    "path": path,
                    "site": os.path.basename(root),
                    "channels": _count_channels(path),
                })
            if root == folder:
                break
    return sorted(found, key=lambda one: one["path"])


def _count_channels(path):
    """How many channels a list holds, counted without reading it into memory."""
    try:
        if os.path.getsize(path) > 64 * 1024 * 1024:
            return 0
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return sum(chunk.count("<channel ") for chunk in iter(lambda: handle.read(1 << 20), ""))
    except OSError:
        return 0


# ── Making a channel list ────────────────────────────────────────────────────


def make_list(from_path, keeping, leaving_out="", into=None, write=False, show=12):
    """
    A channel list made out of a bigger one: the entries that say a word, and not the
    ones that say another.

    This is the grep that was being done by hand ("grep -i PBS
    sites/tvpassport.com/tvpassport.com.channels.xml"), done properly. Two reasons it is
    worth having here rather than in a shell:

    - **Grep gives you fragments, not a document.** A file of bare <channel> lines is not
      XML, and the grabber answers it with "Text data outside of root node" at the last
      line, which says nothing about what is wrong. What comes out of here is a document.
    - **You can see what you are about to keep.** Without `write` it says how many matched
      and the first few of them, so a word that matches four thousand channels or four is
      found out before the scrape rather than during it.

    Matching is on everything a line of grep would have seen: what the channel is called,
    its site id, and its xmltv id, without regard to case.
    """
    from lxml import etree

    if not from_path or not os.path.isfile(from_path):
        raise ValueError("That channel list is not there")
    wanted = [one.strip().lower() for one in str(keeping or "").split(",") if one.strip()]
    unwanted = [one.strip().lower() for one in str(leaving_out or "").split(",") if one.strip()]
    if not wanted:
        raise ValueError("What should be kept?")
    try:
        tree = etree.parse(from_path, etree.XMLParser(recover=True))
    except Exception as e:
        raise ValueError(f"That channel list could not be read: {e}")

    kept = []
    for element in tree.iter("channel"):
        said = " ".join(
            [element.text or ""] + [str(value) for value in element.attrib.values()]
        ).lower()
        if not any(word in said for word in wanted):
            continue
        if unwanted and any(word in said for word in unwanted):
            continue
        kept.append(element)

    found = {
        "kept": len(kept),
        "of": sum(1 for _ in tree.iter("channel")),
        "sample": [(one.text or "").strip() for one in kept[:show]],
        "into": into or "",
        "written": False,
    }
    if not write:
        return found
    if not into:
        raise ValueError("Where should the list be written?")
    if os.path.abspath(into) == os.path.abspath(from_path):
        raise ValueError("That would write over the list it was made from")
    if not kept:
        raise ValueError("Nothing matched, so there is no list to make")
    try:
        os.makedirs(os.path.dirname(into) or ".", exist_ok=True)
        # A document, not a heap of lines: this is what "Text data outside of root node"
        # means when a grep's output is handed to the grabber
        made = etree.Element("channels")
        for one in kept:
            made.append(etree.fromstring(etree.tostring(one)))
        etree.ElementTree(made).write(
            into, encoding="UTF-8", xml_declaration=True, pretty_print=True
        )
    except OSError as e:
        raise ValueError(f"Could not write {into}: {e}")
    logger.info(f"EPG grabber: made {into} -- {len(kept)} channel(s) of {found['of']} from {from_path}")
    found["written"] = True
    return found


# ── Running it ───────────────────────────────────────────────────────────────


def _argv(job, settings):
    """
    The command for one job: what the person would have typed.

    Only what was set is passed. Everything else is the grabber's own default, which is
    the right thing to leave alone -- a site's own number of days is usually the number of
    days that site has.
    """
    argv = list(_words(settings.get("command") or DEFAULTS["command"]))
    if job.get("channels"):
        argv.append(f"--channels={job['channels']}")
    if job.get("sites"):
        argv.append(f"--sites={job['sites']}")
    argv.append(f"--output={_part_of(job['output'])}")
    if job.get("days"):
        argv.append(f"--days={int(job['days'])}")
    if job.get("lang"):
        argv.append(f"--lang={job['lang']}")
    if job.get("timeout_ms"):
        argv.append(f"--timeout={int(job['timeout_ms'])}")
    if job.get("delay_ms"):
        argv.append(f"--delay={int(job['delay_ms'])}")
    if job.get("max_connections"):
        argv.append(f"--maxConnections={int(job['max_connections'])}")
    if job.get("proxy"):
        argv.append(f"--proxy={job['proxy']}")
    if job.get("gzip"):
        argv.append("--gzip")
    return argv


def _part_of(output):
    """Where a grab writes while it is running: beside the live file, never over it."""
    return f"{output}.part"


# "[1204/4539] tvpassport.com (en) - alabama-public-tv--pbs-apt/5145 - Sep 23, 2026 (37 programs)"
_COUNTED = re.compile(r"\[(\d+)/(\d+)\]")
# How long to wait for the next line before asking whether to carry on at all
LOOK_EVERY = 2.0
# ...and how often to say that a grab is still going, so its lock outlives it
HOLD_EVERY = 60.0
# Floors under the two settings, so neither can be set to something that would stop a
# scrape that is simply working
LEAST_SILENCE = 60
LEAST_RUN = 300


def _say(redis_client, **changes):
    import json

    try:
        current = json.loads(redis_client.get(PROGRESS_KEY) or "{}")
    except (ValueError, TypeError):
        current = {}
    current.update(changes)
    try:
        redis_client.set(PROGRESS_KEY, json.dumps(current), ex=PROGRESS_KEPT)
    except Exception as e:
        logger.debug(f"Could not say how the grab is going: {e}")
    return current


def progress(redis_client):
    import json

    try:
        return json.loads(redis_client.get(PROGRESS_KEY) or "{}")
    except (ValueError, TypeError):
        return {}


def is_running(redis_client):
    try:
        return bool(redis_client.exists(RUNNING_KEY))
    except Exception:
        return False


def request_stop(redis_client):
    """Ask the grab to stop. It stops at the next line it prints."""
    if not is_running(redis_client):
        return False
    redis_client.set(STOP_KEY, "1", ex=RUNNING_TTL)
    return True


def run(redis_client, only=None):
    """
    Grab every job that is due, or the one given.

    One at a time and never two at once: a full scrape is thousands of requests, and two of
    them together is twice the load for a worse answer.
    """
    settings = load_settings()
    if only:
        jobs = [job for job in settings["jobs"] if job["id"] == only]
        if not jobs:
            return {"error": "That guide is not set up any more"}
    else:
        jobs = [job for job in settings["jobs"] if job["enabled"]]
    if not jobs:
        return {"ran": 0, "why": "nothing to grab"}
    if not redis_client.set(RUNNING_KEY, "1", nx=True, ex=RUNNING_TTL):
        return {"error": "A grab is already running"}
    redis_client.delete(STOP_KEY)
    done = []
    try:
        for job in jobs:
            done.append(_one(redis_client, job, settings))
            if redis_client.exists(STOP_KEY):
                break
    finally:
        redis_client.delete(RUNNING_KEY, STOP_KEY)
        _say(redis_client, state="done", finished_at=_now(), now="")
    return {"ran": len(done), "jobs": done}


def _one(redis_client, job, settings):
    """One job: run it, check what came out, and put it in place if it is good."""
    started = time.time()
    _say(
        redis_client, state="running", job=job["id"], name=job["name"], started_at=_now(),
        done=0, total=0, now="starting", said="", finished_at="",
    )
    logger.info(f"EPG grabber: {job['name']} starting")
    how = _grab(redis_client, job, settings)
    if how["ok"]:
        how.update(_take_it(job))
    how.update(at=_now(), seconds=round(time.time() - started, 1))
    _remember(job["id"], how)
    _say(redis_client, said=how.get("why") or "", now="")
    logger.info(
        f"EPG grabber: {job['name']} {'finished' if how['ok'] else 'did not finish'}"
        + (f" -- {how['why']}" if how.get("why") else "")
    )
    return {"id": job["id"], "name": job["name"], **how}


def _grab(redis_client, job, settings):
    """Run the grabber itself, saying where it has got to, and say how it went."""
    part = _part_of(job["output"])
    try:
        os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
    except OSError as e:
        return {"ok": False, "why": f"Nowhere to write {part}: {e}"}
    argv = _argv(job, settings)
    logger.info(f"EPG grabber: {' '.join(argv)} (in {settings['folder']})")
    silence = max(LEAST_SILENCE, float(settings.get("silent_for_minutes") or 20) * 60)
    deadline = time.time() + max(LEAST_RUN, float(settings.get("give_up_after_minutes") or 360) * 60)
    try:
        running = subprocess.Popen(
            argv, cwd=settings["folder"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
    except OSError as e:
        return {"ok": False, "why": f"Could not start it: {e}"}

    # What it says is read by a thread of its own, so that waiting for the next line is
    # not the same as waiting for ever: a grabber that hangs says nothing at all, and the
    # three questions below -- stop, too long, too quiet -- have to be asked while nothing
    # is arriving, which is exactly when they matter.
    import queue
    import threading

    lines = queue.Queue()

    def pump():
        try:
            for line in running.stdout:
                lines.put(line)
        except Exception as e:
            logger.debug(f"EPG grabber: could not read what it was saying: {e}")
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True, name="epg-grabber-read").start()

    said_at = time.time()
    held_at = time.time()
    last = ""
    while True:
        try:
            line = lines.get(timeout=LOOK_EVERY)
        except queue.Empty:
            line = ""
        if line is None:
            break
        line = (line or "").strip()
        if line:
            said_at = time.time()
            last = line
            counted = _COUNTED.search(line)
            if counted:
                _say(
                    redis_client, done=int(counted.group(1)), total=int(counted.group(2)),
                    now=line[:200],
                )
            else:
                _say(redis_client, now=line[:200])
        if redis_client.exists(STOP_KEY):
            return _stop(running, "asked to stop")
        if time.time() > deadline:
            return _stop(running, "it ran longer than it is allowed to")
        if time.time() - said_at > silence:
            return _stop(running, "it said nothing for too long")
        # ...and say that this one is still going. A grab can be allowed longer than the
        # lock lives, and a lock that quietly expires under a running grab is how a second
        # one starts on top of the first.
        if time.time() - held_at > HOLD_EVERY:
            held_at = time.time()
            try:
                redis_client.expire(RUNNING_KEY, RUNNING_TTL)
            except Exception as e:
                logger.debug(f"EPG grabber: could not hold the lock: {e}")
    code = running.wait()
    if code != 0:
        return {"ok": False, "why": f"It stopped with code {code}: {last[:200]}"}
    return {"ok": True, "why": "", "said": last[:200]}


def _stop(running, why):
    """Stop the grabber and everything it started."""
    import signal

    try:
        os.killpg(os.getpgid(running.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            running.terminate()
        except Exception:
            pass
    try:
        running.wait(timeout=20)
    except Exception:
        try:
            os.killpg(os.getpgid(running.pid), signal.SIGKILL)
        except Exception:
            pass
    return {"ok": False, "why": why, "stopped": True}


def what_it_holds(path):
    """How many channels and programmes an XMLTV file holds, or None if it cannot be read."""
    from lxml import etree

    from apps.epg.tasks import _open_xmltv_file

    channels = programmes = 0
    handle = None
    try:
        handle = _open_xmltv_file(path)
        for _, element in etree.iterparse(handle, events=("end",), tag=("channel", "programme"), recover=True):
            if element.tag == "channel":
                channels += 1
            else:
                programmes += 1
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
    except Exception as e:
        logger.warning(f"EPG grabber: could not read {path}: {e}")
        return None
    finally:
        if handle:
            try:
                handle.close()
            except Exception:
                pass
    return {"channels": channels, "programmes": programmes}


def _take_it(job):
    """
    What came out, checked and put in place -- or left where it is.

    The live guide is only replaced once the new one has been read back and found to hold
    something. A scrape that died half way through writes a file that is XML and holds
    nothing much, and swapping that in loses a working guide for a broken one.
    """
    part = _part_of(job["output"])
    if not os.path.exists(part):
        return {"ok": False, "why": "It finished without writing anything"}
    held = what_it_holds(part)
    if held is None:
        return {"ok": False, "why": "What it wrote could not be read as XMLTV"}
    if not held["channels"] or not held["programmes"]:
        return {
            "ok": False,
            "why": f"What it wrote holds {held['channels']} channel(s) and "
                   f"{held['programmes']} programme(s): keeping the guide you had",
            **held,
        }
    try:
        # Onto the live file in one step, so nothing ever reads half a guide
        os.replace(part, job["output"])
        # ...and the packed copy beside it, where one was asked for
        if job.get("gzip") and os.path.exists(f"{part}.gz"):
            os.replace(f"{part}.gz", f"{job['output']}.gz")
    except OSError as e:
        return {"ok": False, "why": f"Could not put it in place: {e}", **held}
    answer = {"ok": True, "why": "", **held, "output": job["output"]}
    answer.update(_hand_over(job))
    return answer


def _hand_over(job):
    """Tell Dispatcharr to read the guide that has just been written."""
    if not job.get("epg_source"):
        return {"read_by": ""}
    from apps.epg.models import EPGSource
    from apps.epg.tasks import refresh_epg_data

    source = EPGSource.objects.filter(id=job["epg_source"]).first()
    if source is None:
        return {"read_by": "", "why": "The EPG source it feeds is gone"}
    fields = []
    if source.file_path != job["output"]:
        source.file_path = job["output"]
        fields.append("file_path")
    if source.url:
        # A source with a URL fetches it and ignores the file; this one is a file
        source.url = None
        fields.append("url")
    if fields:
        source.save(update_fields=fields)
    try:
        refresh_epg_data.delay(source.id, force=True)
    except Exception as e:
        logger.warning(f"EPG grabber: could not ask {source.name} to read the new guide: {e}")
        return {"read_by": source.name, "why": "The guide is in place; Dispatcharr was not told to read it"}
    return {"read_by": source.name}


def _remember(job_id, how):
    """How a job's last run went, kept with the job."""
    def change(stored):
        for job in stored.get("jobs") or []:
            if job.get("id") == job_id:
                job["last"] = how

    try:
        change_row(SETTINGS_KEY, "EPG grabber", change)
    except Exception as e:
        logger.warning(f"EPG grabber: could not write down how {job_id} went: {e}")


# ── When ─────────────────────────────────────────────────────────────────────


def in_window(settings, now=None):
    """Whether the hour allows it. Empty times are any time."""
    start, end = settings.get("window_from"), settings.get("window_to")
    if not start or not end:
        return True
    now = (now or datetime.now()).strftime("%H:%M")
    if start <= end:
        return start <= now <= end
    # Over midnight
    return now >= start or now <= end


def due(settings, redis_client, now=None):
    """Whether a grab should start by itself now."""
    if not settings.get("enabled") or is_running(redis_client):
        return False
    if not [job for job in settings["jobs"] if job["enabled"]]:
        return False
    if not in_window(settings, now):
        return False
    last = max(
        (str((job.get("last") or {}).get("at") or "") for job in settings["jobs"]),
        default="",
    )
    if not last:
        return True
    try:
        finished = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - finished).total_seconds() >= float(settings["every_hours"]) * 3600
