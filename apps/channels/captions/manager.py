"""
Captions made from the sound (fork/subtitles.md step 3, §5b): the settings, the caption worker
as Dispatch More sees it, and the proposal of what fits this server.

The worker (worker.py) runs apart from Dispatcharr; this only asks it things over HTTP. Before
it is installed, what the machine has is looked at from here with the worker's own functions,
so the tab can say what installing would give. Installing needs root: on a Linux/LXC install
the tab leaves a request that a root watcher carries out (fork/patcher/captions.sh), as the
Uninstall button does; in Docker the worker is a container of its own.

Off by default: nothing is installed, downloaded or run until someone asks on the tab.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import worker as machine_info

logger = logging.getLogger(__name__)

SETTINGS_KEY = "captions"
DEFAULTS = {
    "enabled": False,
    "worker_url": "",  # "" = the one this install puts in (or the Docker container's name)
    "token": "",
    "model": "",  # "" = the proposal
    "channels_at_once": 2,  # how many channels may be captioned at the same time (§5b.4)
    "quality": "balanced",  # best / balanced / channels
    "languages": [],  # the TVs' languages to translate to (step 4)
    "ollama_url": "http://127.0.0.1:11434",
}
QUALITIES = ("best", "balanced", "channels")
LOCAL_WORKER = "http://127.0.0.1:9725"
DOCKER_WORKER = "http://dispatch-more-captions:9725"
# Smallest first: the order in which a model is better and slower
ORDER = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]


# ── Settings ───────────────────────────────────────────────────────────────────

def load():
    from ..guide_manager import _load

    stored = _load(SETTINGS_KEY, {})
    return {**DEFAULTS, **{k: v for k, v in (stored or {}).items() if k in DEFAULTS}}


def save(values):
    from ..guide_manager import _store

    current = load()
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            continue
        if key == "enabled":
            value = bool(value)
        elif key == "channels_at_once":
            value = max(1, min(20, int(value)))
        elif key == "quality":
            value = value if value in QUALITIES else "balanced"
        elif key == "model":
            value = value if value in machine_info.MODELS else ""
        elif key == "languages":
            value = [str(v).strip().lower() for v in (value or []) if str(v).strip()][:10]
        else:
            value = str(value or "").strip()
        current[key] = value
    _store(SETTINGS_KEY, "Captions", current)
    return current


# ── How this server was installed ────────────────────────────────────────────

def _record():
    from django.conf import settings

    try:
        return json.loads((Path(settings.BASE_DIR) / ".fork-install.json").read_text())
    except (OSError, ValueError):
        return {}


def layout():
    record = _record()
    if record.get("layout"):
        return record["layout"]
    return "docker" if Path("/.dockerenv").exists() else "systemd"


def worker_url(settings=None):
    settings = settings or load()
    if settings.get("worker_url"):
        return settings["worker_url"].rstrip("/")
    return DOCKER_WORKER if layout() == "docker" else LOCAL_WORKER


def install_state():
    """What the installer last said, and whether a request is waiting for it."""
    record = _record()
    status = {}
    if record.get("captions_status"):
        try:
            status = json.loads(Path(record["captions_status"]).read_text())
        except (OSError, ValueError):
            pass
    request = record.get("captions_request")
    return {
        "can_request": bool(request) and record.get("layout") == "systemd",
        "requested": bool(request and Path(request).exists()),
        **status,
    }


def request(action, user=""):
    """Leave a request for the root watcher: install or remove the worker."""
    if action not in ("install", "remove"):
        raise ValueError("Install or remove?")
    record = _record()
    target = record.get("captions_request")
    if not target or record.get("layout") != "systemd":
        raise ValueError(
            "This server cannot install the caption worker from the page: in Docker it is a "
            "container of its own (see the instructions), and an install made without the "
            "installer has no watcher to do it."
        )
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps({
            "action": action, "by": user,
            "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }))
    except OSError as e:
        raise ValueError(f"Could not leave the request at {target}: {e}")
    logger.info(f"Captions: {action} of the caption worker requested by {user or 'an admin'}")


REPOSITORY_RAW = "https://raw.githubusercontent.com/ckegels/dispatch-more"


def docker_compose(gpu=False):
    """The worker as a container of its own, for docker-compose.yml beside Dispatcharr's
    (§5b.6). It fetches this release's worker.py; models stay in a folder of their own."""
    release = _record().get("release") or "feature/probation-slots"
    url = f"{REPOSITORY_RAW}/{release}/apps/channels/captions/worker.py"
    packages = "faster-whisper" + (" nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'" if gpu else "")
    lines = [
        "  dispatch-more-captions:",
        "    image: python:3.12-slim",
        "    container_name: dispatch-more-captions",
        "    restart: unless-stopped",
        "    volumes:",
        "      - ./data/models/captions:/models",
        "      - ./data/models/captions-pip:/root/.cache/pip",
        "    command: >",
        f"      sh -c \"pip install -q {packages} &&",
        f"      python -c 'import urllib.request; urllib.request.urlretrieve(\\\"{url}\\\", \\\"/worker.py\\\")' &&",
        f"      python /worker.py --host 0.0.0.0 --models /models\"",
    ]
    if gpu:
        lines += [
            "    deploy:",
            "      resources:",
            "        reservations:",
            "          devices:",
            "            - driver: nvidia",
            "              count: all",
            "              capabilities: [gpu]",
        ]
    return "\n".join(lines) + "\n"


# ── The worker ─────────────────────────────────────────────────────────────────

def ask(path, method="GET", settings=None, body=None, timeout=4):
    """The worker's answer as a dict, or None when it does not answer."""
    settings = settings or load()
    req = urllib.request.Request(worker_url(settings) + path, data=body, method=method)
    if settings.get("token"):
        req.add_header("X-Worker-Token", settings["token"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as answer:
            return json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read()).get("error") or str(e)}
        except Exception:
            return {"error": str(e)}
    except (OSError, ValueError):
        return None


def here():
    """What this machine has, looked at from Dispatcharr itself (no worker needed). In Docker
    this is the container's view: a card given only to the worker's container is not seen."""
    return {
        "gpus": machine_info.gpus(),
        "cpu": machine_info.cpu(),
        "memory": machine_info.memory_mb(),
        "runtime": {"faster_whisper": None, "cuda_devices": 0},
        "seen_from": "dispatcharr",
    }


def ollama(url):
    """The models an Ollama server has, or None when there is none there."""
    if not url:
        return None
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2) as answer:
            return [m.get("name") for m in json.loads(answer.read()).get("models") or []]
    except (OSError, ValueError):
        return None


# ── What fits this server (§5b.3) ───────────────────────────────────────────────

def guess(machine):
    """The hardware table: a model and a way to translate, before anything is measured."""
    gpus = machine.get("gpus") or []
    cpu = machine.get("cpu") or {}
    vram = max((g.get("memory_mb") or 0 for g in gpus), default=0)
    if vram >= 10000:
        return {"model": "large-v3-turbo", "translation": "ollama",
                "why": f"An NVIDIA card with {vram // 1024} GB: the best fast model, with room for a translation model beside it."}
    if vram >= 4000:
        return {"model": "large-v3-turbo", "translation": "opus-mt",
                "why": f"An NVIDIA card with {vram // 1024} GB: the best fast model; translation with small models."}
    if vram:
        return {"model": "small", "translation": "",
                "why": f"An NVIDIA card with {vram} MB: a small model fits."}
    cores = cpu.get("cores") or 1
    arch = (cpu.get("arch") or "").lower()
    if arch not in ("x86_64", "amd64"):
        return {"model": "tiny", "translation": "",
                "why": f"No NVIDIA card, a {arch or 'non-x86'} processor: only the smallest model, and that one roughly."}
    if cores >= 8 and cpu.get("avx2"):
        return {"model": "small", "translation": "opus-mt",
                "why": f"No NVIDIA card; {cores} processor threads with AVX2: a small model, for a channel or two."}
    if cores >= 4:
        return {"model": "base", "translation": "",
                "why": f"No NVIDIA card; {cores} processor threads: a basic model, for one channel."}
    return {"model": "tiny", "translation": "",
            "why": f"No NVIDIA card and {cores} processor threads: only the smallest model, and that one roughly."}


def propose(machine, models, settings):
    """The model to use: measured where there are measurements, guessed where not. The largest
    model that keeps up with the channels asked for; for "channels", the one that can follow
    the most; with "balanced", room for one channel more than asked."""
    first = guess(machine)
    need = int(settings.get("channels_at_once") or 1)
    quality = settings.get("quality") or "balanced"
    measured = {name: (models.get(name) or {}).get("benchmark") for name in ORDER}
    measured = {name: m for name, m in measured.items() if m}
    proposal = {**first, "measured": False, "channels": None}
    if measured:
        if quality == "channels":
            best = max(measured, key=lambda n: (measured[n]["channels"], ORDER.index(n)))
            pick = best
        else:
            want = need + 1 if quality == "balanced" else need
            fits = [n for n in ORDER if n in measured and measured[n]["channels"] >= want]
            fits = fits or [n for n in ORDER if n in measured and measured[n]["channels"] >= need]
            pick = fits[-1] if fits else None
        if pick:
            m = measured[pick]
            proposal.update(
                model=pick, measured=True, channels=m["channels"],
                why=f"Measured: {pick} turns a second of sound into text in {m['rtf']:.2f} s on the "
                    f"{'card' if m['device'] == 'cuda' else 'processor'}, so it follows {m['channels']} "
                    f"channel{'s' if m['channels'] != 1 else ''} at once.",
            )
        else:
            fastest = min(measured, key=lambda n: measured[n]["rtf"])
            proposal.update(
                model=fastest, measured=True, channels=measured[fastest]["channels"],
                why=f"None of the measured models keeps up with {need} channels; {fastest} follows "
                    f"{measured[fastest]['channels']}. Ask for fewer channels at once, or measure a smaller model.",
            )
        if first["model"] not in measured and ORDER.index(first["model"]) > ORDER.index(proposal["model"]):
            proposal["measure_next"] = first["model"]
    return proposal


def status(user_settings=None):
    """Everything the tab's captions card shows."""
    settings = user_settings or load()
    answer = ask("/status", settings=settings)
    running = bool(answer and "machine" in answer)
    machine = answer["machine"] if running else here()
    models = answer.get("models", {}) if running else {}
    from ..service_keys import load as keys

    tags = ollama(settings.get("ollama_url"))
    return {
        "settings": {**settings, "token": "•" * 8 if settings.get("token") else ""},
        "layout": layout(),
        "worker_url": worker_url(settings),
        "worker": {
            "running": running,
            "error": (answer or {}).get("error", "") if not running else "",
            "device": answer.get("device") if running else "",
            "cuda_failed": answer.get("cuda_failed", "") if running else "",
            "version": answer.get("version") if running else None,
        },
        "install": install_state(),
        "docker": {"cpu": docker_compose(False), "gpu": docker_compose(True)} if layout() == "docker" else None,
        "machine": machine,
        "models": [
            {"name": name, **machine_info.MODELS[name], **(models.get(name) or {})}
            for name in ORDER
        ],
        "proposal": propose(machine, models, settings),
        "translation": {
            "ollama": tags,
            "deepl": bool(keys().get("deepl_key")),
        },
        "checked_at": time.time(),
    }


def worker_action(action, model=""):
    """Ask the worker to look again, download or measure a model."""
    if action == "look":
        answer = ask("/look", method="POST", timeout=20)
    elif action in ("download", "benchmark"):
        if model not in machine_info.MODELS:
            raise ValueError(f"Unknown model: {model}")
        answer = ask(f"/{action}?model={model}", method="POST")
    else:
        raise ValueError("Look, download or benchmark?")
    if answer is None:
        raise ValueError("The caption worker does not answer.")
    if answer.get("error"):
        raise ValueError(answer["error"])
    return answer
