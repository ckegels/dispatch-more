"""
The caption worker (fork/subtitles.md §5, §5b): a small HTTP service that finds out what the
machine it runs on can do, measures the speech-to-text models on it, downloads the ones asked
for, and turns pieces of sound into timed text.

It runs **apart from Dispatcharr** -- in a virtualenv of its own on the same machine, or on
another machine with a GPU -- because the models are hundreds of MB to GBs and would otherwise
sit in Dispatcharr's own processes (the reason v241 moved the Lineup's model out too). It needs
no database and never talks to a provider: Dispatch More sends it sound and gets text back.

Standard library only, plus faster-whisper (CTranslate2) where it is installed: without it the
worker still answers what the machine has, so the tab can say what installing would give.

    python worker.py --port 9725 --models /data/models/captions

GET  /status                 what the machine has, which models are here, what was measured
POST /look                   find out again
POST /download?model=small   fetch a model (in the background; /status shows the progress)
POST /benchmark?model=small  time it on the test clip (in the background)
POST /transcribe?model=small&offset=12.5[&language=nl]
     body: 16 kHz mono 16-bit PCM, or a WAV file
     -> {"language", "segments": [{"start", "end", "text"}]}, times plus the offset
POST /translate  {"lines": [...], "source": "nl", "target": "en"}
     -> {"lines": [...], "via": ["nl-en"]}  Opus-MT (Helsinki-NLP) through CTranslate2: a small
     model per language pair, fetched and converted on first use, through English when there is
     no direct pair (fork/subtitles.md §9 step 4)
"""

import argparse
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = 2
SAMPLE_RATE = 16000

# The models offered, by faster-whisper's own names; sizes and memory to show before a download
# (CTranslate2 files; memory in float16 on a GPU, int8 on a CPU). fork/subtitles.md §6.1.
MODELS = {
    "tiny": {"label": "Whisper tiny", "download_mb": 75, "gpu_mb": 400, "cpu_mb": 300},
    "base": {"label": "Whisper base", "download_mb": 145, "gpu_mb": 600, "cpu_mb": 400},
    "small": {"label": "Whisper small", "download_mb": 484, "gpu_mb": 1200, "cpu_mb": 900},
    "medium": {"label": "Whisper medium", "download_mb": 1530, "gpu_mb": 2600, "cpu_mb": 2000},
    "large-v3-turbo": {"label": "Whisper large-v3-turbo", "download_mb": 1620, "gpu_mb": 3000, "cpu_mb": 2600},
    "large-v3": {"label": "Whisper large-v3", "download_mb": 3100, "gpu_mb": 4900, "cpu_mb": 4200},
}
# A public-domain speech clip (President Kennedy's inaugural address, the one faster-whisper's
# own tests use), played three times: ~33 s of speech to time a model on
TEST_CLIP_URL = "https://github.com/SYSTRAN/faster-whisper/raw/master/tests/data/jfk.flac"
# Real-time factor at which a model still keeps up, with room to spare (§5b.2)
KEEP_UP = 0.6

state = {
    "machine": {},
    "models": {},  # name -> {"downloaded", "downloading", "benchmark": {...}}
    "busy": "",
}
loaded = {}  # (name, device) -> WhisperModel
lock = threading.Lock()


# ── What the machine has (§5b.1) ────────────────────────────────────────────────

def _run(cmd, timeout=10):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gpus():
    """NVIDIA cards as nvidia-smi sees them; [] without one (or without the driver)."""
    found = []
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits"])
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try:
                found.append({"vendor": "nvidia", "name": parts[0], "memory_mb": int(parts[1]),
                              "free_mb": int(parts[2]), "driver": parts[3]})
            except ValueError:
                pass
    return found


def cpu():
    flags = set()
    model = platform.processor() or ""
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("flags") and not flags:
                    flags = set(line.split(":", 1)[1].split())
                elif line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return {"model": model, "cores": os.cpu_count() or 1, "arch": platform.machine(),
            "avx2": "avx2" in flags, "avx512": "avx512f" in flags}


def memory_mb():
    try:
        with open("/proc/meminfo") as fh:
            info = {line.split(":")[0]: int(line.split()[1]) for line in fh if ":" in line}
        return {"total_mb": info.get("MemTotal", 0) // 1024, "free_mb": info.get("MemAvailable", 0) // 1024}
    except (OSError, ValueError, IndexError):
        return {"total_mb": 0, "free_mb": 0}


def runtime():
    """What can run models here: faster-whisper, and whether it sees a CUDA device."""
    try:
        import ctranslate2  # noqa: F401
        import faster_whisper  # noqa: F401

        cuda = 0
        try:
            cuda = ctranslate2.get_cuda_device_count()
        except Exception:
            pass
        return {"faster_whisper": getattr(faster_whisper, "__version__", "?"), "cuda_devices": cuda}
    except ImportError:
        return {"faster_whisper": None, "cuda_devices": 0}


def look(models_dir):
    disk = shutil.disk_usage(models_dir if os.path.isdir(models_dir) else os.path.dirname(models_dir) or "/")
    state["machine"] = {
        "worker_version": VERSION,
        "python": sys.version.split()[0],
        "gpus": gpus(),
        "cpu": cpu(),
        "memory": memory_mb(),
        "disk_free_mb": disk.free // (1024 * 1024),
        "runtime": runtime(),
        "looked_at": time.time(),
    }
    for name in MODELS:
        entry = state["models"].setdefault(name, {})
        entry["downloaded"] = _downloaded(models_dir, name)
    return state["machine"]


def device():
    machine = state["machine"]
    if state.get("cuda_failed"):
        return "cpu"
    return "cuda" if machine.get("runtime", {}).get("cuda_devices") else "cpu"


# ── Models ──────────────────────────────────────────────────────────────────────

def _repo_dir(models_dir, name):
    return os.path.join(models_dir, name)


def _downloaded(models_dir, name):
    path = _repo_dir(models_dir, name)
    return os.path.isfile(os.path.join(path, "model.bin"))


def download(models_dir, name):
    """Fetch a model into models_dir/<name> with faster-whisper's own downloader."""
    from faster_whisper.utils import download_model

    entry = state["models"].setdefault(name, {})
    entry.update(downloading=True, error="")
    try:
        os.makedirs(models_dir, exist_ok=True)
        download_model(name, output_dir=_repo_dir(models_dir, name))
        entry["downloaded"] = _downloaded(models_dir, name)
    except Exception as e:
        entry["error"] = f"download failed: {e}"
    finally:
        entry["downloading"] = False


def model(models_dir, name):
    from faster_whisper import WhisperModel

    where = device()
    key = (name, where)
    with lock:
        if key not in loaded:
            path_or_name = _repo_dir(models_dir, name)
            if not _downloaded(models_dir, name):
                path_or_name = name
            try:
                loaded[key] = WhisperModel(path_or_name, device=where,
                                           compute_type="float16" if where == "cuda" else "int8",
                                           download_root=models_dir)
            except Exception as e:
                if where != "cuda":
                    raise
                # A card is there but its CUDA libraries are not (or too old): use the CPU
                state["cuda_failed"] = str(e)[:300]
                key = (name, "cpu")
                loaded[key] = WhisperModel(path_or_name, device="cpu", compute_type="int8",
                                           download_root=models_dir)
        return loaded[key]


# ── Sound ──────────────────────────────────────────────────────────────────────

def pcm_to_float(raw):
    import numpy as np

    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def body_to_audio(raw):
    """A WAV file or raw 16 kHz mono 16-bit PCM, as float samples."""
    if raw[:4] == b"RIFF":
        with wave.open(io.BytesIO(raw)) as wav:
            if wav.getframerate() != SAMPLE_RATE or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("WAV must be 16 kHz mono 16-bit")
            raw = wav.readframes(wav.getnframes())
    return pcm_to_float(raw)


def test_clip():
    """~33 s of speech, as float samples at 16 kHz (decoded by faster-whisper itself)."""
    import numpy as np
    from faster_whisper import decode_audio

    cache = os.path.join(tempfile.gettempdir(), "dispatch-more-test-clip.flac")
    if not os.path.exists(cache):
        urllib.request.urlretrieve(TEST_CLIP_URL, cache + ".part")
        os.replace(cache + ".part", cache)
    once = decode_audio(cache, sampling_rate=SAMPLE_RATE)
    return np.concatenate([once, once, once])


def transcribe(models_dir, name, audio, language=None, offset=0.0):
    whisper = model(models_dir, name)
    segments, info = whisper.transcribe(
        audio, language=language or None, beam_size=1, vad_filter=True,
        condition_on_previous_text=False, without_timestamps=False,
    )
    out = [{"start": round(offset + s.start, 2), "end": round(offset + s.end, 2), "text": s.text.strip()}
           for s in segments if s.text.strip()]
    return {"language": info.language, "language_probability": round(info.language_probability, 2),
            "segments": out}


def benchmark(models_dir, name):
    """Time a model on the test clip (§5b.2): how long a second of sound takes, and from that
    how many channels it can follow live at once, kept within the memory there is."""
    entry = state["models"].setdefault(name, {})
    entry.update(benchmarking=True, error="")
    try:
        audio = test_clip()
        seconds = len(audio) / SAMPLE_RATE
        transcribe(models_dir, name, audio[: SAMPLE_RATE * 5])  # load and warm up first
        began = time.monotonic()
        result = transcribe(models_dir, name, audio)
        took = time.monotonic() - began
        rtf = took / seconds
        where = device()
        need = MODELS[name]["gpu_mb" if where == "cuda" else "cpu_mb"]
        if where == "cuda" and state["machine"].get("gpus"):
            room = state["machine"]["gpus"][0]["free_mb"] + need  # it is loaded already
        else:
            room = state["machine"].get("memory", {}).get("free_mb", 0) + need
        by_speed = int(KEEP_UP / rtf) if rtf > 0 else 0
        by_memory = max(1, room // need) if need else by_speed
        entry["benchmark"] = {
            "device": where, "seconds": round(seconds, 1), "took": round(took, 2),
            "rtf": round(rtf, 3), "channels": max(0, min(by_speed, by_memory)),
            "text": " ".join(s["text"] for s in result["segments"])[:200],
            "at": time.time(),
        }
    except Exception as e:
        entry["error"] = f"benchmark failed: {e}"
    finally:
        entry["benchmarking"] = False


# ── Captions while a TV watches (fork/subtitles.md §4) ─────────────────────────────

# The stream is read through Dispatcharr's own proxy, as one more client of the channel the
# TV is already watching: no connection to the provider of its own. Named, so the proxy's
# Force Close and viewer checks know it is not a viewer (apps/channels/captions/__init__.py).
CAPTIONS_USER_AGENT = "DispatchMore-Captions/1"
# A job nobody has asked for its captions this long is stopped (the TV polls every second)
JOB_IDLE_SECONDS = 10
# A piece of sound is cut at a pause once it is this long, and at the longest regardless
CHUNK_LEAST, CHUNK_MOST = 2.5, 7.0
# Captions kept per job, for a TV that asks again after a moment
CUES_KEPT = 200
# Pieces waiting for the model past which the oldest are dropped: live is what matters
QUEUE_MOST = 3
# The spoken language is found by the model and fixed once two pieces in a row agree with
# this much certainty; looked at again this often (a programme in another language)
LANGUAGE_SURE, LANGUAGE_AGAIN = 0.7, 300

jobs = {}  # key -> Job
jobs_lock = threading.Lock()


class Chunker:
    """
    Cuts a stream of 16 kHz mono samples into pieces for the model, at a pause where there is
    one: a piece is let go once it is CHUNK_LEAST long and its last 300 ms are quiet next to
    the rest, or at CHUNK_MOST. Each piece carries the stream time (PTS, in seconds) of its
    first sample, which is what the TV times the caption by.
    """

    def __init__(self):
        self.samples = []
        self.start = None
        self.count = 0

    def feed(self, samples, start_time):
        import numpy as np

        if self.start is None:
            self.start = start_time
        self.samples.append(samples)
        self.count += len(samples)
        seconds = self.count / SAMPLE_RATE
        if seconds < CHUNK_LEAST:
            return None
        audio = np.concatenate(self.samples).astype(np.float32) / 32768.0
        tail = audio[-int(0.3 * SAMPLE_RATE):]
        loud = float(np.sqrt(np.mean(audio ** 2))) or 1e-9
        quiet = float(np.sqrt(np.mean(tail ** 2))) < 0.35 * loud
        if not quiet and seconds < CHUNK_MOST:
            return None
        piece = (audio, self.start)
        self.samples, self.start, self.count = [], None, 0
        return piece


class Job:
    """One channel's captions: a reader that decodes the channel's sound from Dispatcharr's
    proxy (PyAV, no ffmpeg needed) and a transcriber that turns its pieces into timed text."""

    def __init__(self, key, url, model_name, language, models_dir):
        self.key, self.url, self.model_name = key, url, model_name
        # Given (set by hand on the Subtitles tab): always that one. Not given: found
        self.forced_language = language or None
        self.language = self.forced_language
        self.heard = []  # (language, certainty) of the last pieces, while finding it
        self.language_at = 0.0
        self.models_dir = models_dir
        self.cues = []
        self.seq = 0
        self.reading = "starting"  # starting / reading / ended / failed
        self.model_state = "waiting"  # waiting / loading / ready / failed
        self.error = ""
        self.behind = 0.0
        self.live_at = None  # stream time of the newest sound read
        self.touched = time.monotonic()
        self.stop = threading.Event()  # asked to stop, or nobody asks any more
        self.pieces = []
        self.have_piece = threading.Condition()

    @property
    def state(self):
        if self.reading == "failed" or self.model_state == "failed":
            return "error"
        if self.reading == "ended" and not self.pieces:
            return "ended"
        if self.reading == "starting":
            return "starting"
        if self.model_state in ("waiting", "loading"):
            return "loading model"
        return "listening"

    def start(self):
        threading.Thread(target=self._read, daemon=True, name=f"captions-read-{self.key}").start()
        threading.Thread(target=self._transcribe, daemon=True, name=f"captions-text-{self.key}").start()

    def idle(self):
        return time.monotonic() - self.touched > JOB_IDLE_SECONDS

    def finished(self):
        return self.stop.is_set() or self.state in ("ended", "error")

    def _read(self):
        import av

        chunker = Chunker()
        try:
            container = av.open(self.url, options={"user_agent": CAPTIONS_USER_AGENT, "timeout": "10000000"})
            audio = next((s for s in container.streams if s.type == "audio"), None)
            if audio is None:
                raise RuntimeError("the channel has no sound")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            self.reading = "reading"
            for packet in container.demux(audio):
                if self.stop.is_set() or self.idle():
                    break
                for frame in packet.decode():
                    if frame.time is None:
                        continue
                    self.live_at = float(frame.time)
                    for out in resampler.resample(frame):
                        piece = chunker.feed(out.to_ndarray().reshape(-1), float(frame.time))
                        if piece is not None:
                            with self.have_piece:
                                self.pieces.append(piece)
                                if len(self.pieces) > QUEUE_MOST:
                                    del self.pieces[0]  # behind: live first
                                self.have_piece.notify()
            container.close()
            self.reading = "ended"
        except Exception as e:
            self.reading, self.error = "failed", f"could not read the channel: {e}"
        finally:
            with self.have_piece:
                self.have_piece.notify()

    def _transcribe(self):
        while not self.stop.is_set():
            with self.have_piece:
                while not self.pieces and self.reading in ("starting", "reading") and not self.stop.is_set():
                    self.have_piece.wait(1.0)
                if not self.pieces:
                    break  # the reading is over and everything read is done
                audio, start = self.pieces.pop(0)
            try:
                if self.model_state == "waiting":
                    self.model_state = "loading"  # the first call loads (or downloads) it
                if (not self.forced_language and self.language
                        and time.monotonic() - self.language_at > LANGUAGE_AGAIN):
                    self.language, self.heard = None, []  # look again
                found = transcribe(self.models_dir, self.model_name, audio, self.language, start)
                self.model_state = "ready"
                if self.language is None and found["segments"]:
                    self.heard = (self.heard + [(found["language"], found["language_probability"])])[-2:]
                    if (len(self.heard) == 2 and self.heard[0][0] == self.heard[1][0]
                            and min(p for _l, p in self.heard) >= LANGUAGE_SURE):
                        self.language, self.language_at = self.heard[0][0], time.monotonic()
            except Exception as e:
                self.model_state, self.error = "failed", f"the model failed: {e}"
                break
            if self.live_at is not None:
                self.behind = round(max(0.0, self.live_at - start - len(audio) / SAMPLE_RATE), 1)
            for segment in found["segments"]:
                self.seq += 1
                self.cues.append({"seq": self.seq, "start": segment["start"], "end": segment["end"],
                                  "text": segment["text"]})
            del self.cues[:-CUES_KEPT]

    def answer(self, since=0):
        return {"key": self.key, "state": self.state, "error": self.error, "behind": self.behind,
                "model": self.model_name, "language": self.language or "",
                "language_found": not self.forced_language,
                "live_at": self.live_at, "cues": [c for c in self.cues if c["seq"] > since]}


def poll_job(models_dir, key, url, model_name, language, since=0):
    """The job for this key, started when there is none (or it ended), and its captions."""
    with jobs_lock:
        for old_key in [k for k, j in jobs.items() if k != key and (j.stop.is_set() or j.idle())]:
            jobs.pop(old_key).stop.set()
        job = jobs.get(key)
        if job is None or job.stop.is_set() or job.idle() or job.url != url or job.model_name != model_name:
            if job is not None:
                job.stop.set()
            job = jobs[key] = Job(key, url, model_name, language, models_dir)
            job.start()
        job.touched = time.monotonic()
    return job.answer(since)


def stop_job(key):
    with jobs_lock:
        job = jobs.pop(key, None)
    if job is not None:
        job.stop.set()
    return job is not None


def jobs_now():
    with jobs_lock:
        return [{k: v for k, v in job.answer(10 ** 12).items() if k != "cues"}
                for job in jobs.values() if not job.finished() and not job.idle()]


# ── Translation: Opus-MT (fork/subtitles.md §9 step 4) ──────────────────────────

# The original Marian weights are linked from each pair's model card; CTranslate2 converts them
# without PyTorch, and SentencePiece (source.spm / target.spm in the same zip) cuts the text
OPUS_CARD = "https://huggingface.co/Helsinki-NLP/opus-mt-{pair}/raw/main/README.md"
translators = {}  # pair -> (Translator, source spm, target spm)
translate_lock = threading.Lock()
missing_pairs = set()


def _opus_dir(models_dir, pair):
    return os.path.join(models_dir, "opus-mt", pair)


def opus_pair(models_dir, pair):
    """The pair's translator, fetched and converted the first time; None when there is no
    such pair (or no CTranslate2 / SentencePiece here)."""
    if pair in translators:
        return translators[pair]
    if pair in missing_pairs:
        return None
    try:
        import ctranslate2
        import sentencepiece
        from ctranslate2.converters import OpusMTConverter
    except ImportError:
        return None
    folder = _opus_dir(models_dir, pair)
    converted = os.path.join(folder, "ct2")
    if not os.path.isdir(converted):
        import re
        import zipfile

        try:
            with urllib.request.urlopen(OPUS_CARD.format(pair=pair), timeout=20) as answer:
                card = answer.read().decode("utf-8", "replace")
        except OSError:
            missing_pairs.add(pair)
            return None
        link = re.search(r"https://object\.pouta\.csc\.fi/[^)\s]+\.zip", card)
        if not link or ">>id<<" in card:  # multi-language targets need a token; not these
            missing_pairs.add(pair)
            return None
        os.makedirs(folder, exist_ok=True)
        archive = os.path.join(folder, "model.zip")
        urllib.request.urlretrieve(link.group(0), archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(os.path.join(folder, "marian"))
        OpusMTConverter(os.path.join(folder, "marian")).convert(converted + ".tmp", quantization="int8", force=True)
        for name in ("source.spm", "target.spm"):
            shutil.copy(os.path.join(folder, "marian", name), os.path.join(folder, name))
        os.rename(converted + ".tmp", converted)
        shutil.rmtree(os.path.join(folder, "marian"), ignore_errors=True)
        os.remove(archive)
    translators[pair] = (
        ctranslate2.Translator(converted, device="cpu", inter_threads=1),
        sentencepiece.SentencePieceProcessor(model_file=os.path.join(folder, "source.spm")),
        sentencepiece.SentencePieceProcessor(model_file=os.path.join(folder, "target.spm")),
    )
    return translators[pair]


def _run_pair(found, lines):
    model, source, target = found
    out = model.translate_batch([source.encode(line, out_type=str) for line in lines], beam_size=2)
    return [target.decode(o.hypotheses[0]) for o in out]


def translate_lines(models_dir, lines, source, target):
    """Lines in `source` (two letters) into `target`: the direct pair, else through English."""
    if not lines or source == target:
        return {"lines": list(lines), "via": []}
    with translate_lock:
        direct = opus_pair(models_dir, f"{source}-{target}")
        if direct is not None:
            return {"lines": _run_pair(direct, lines), "via": [f"{source}-{target}"]}
        if "en" not in (source, target):
            first, second = opus_pair(models_dir, f"{source}-en"), opus_pair(models_dir, f"en-{target}")
            if first is not None and second is not None:
                return {"lines": _run_pair(second, _run_pair(first, lines)), "via": [f"{source}-en", f"en-{target}"]}
    return {"error": f"no Opus-MT model from {source} to {target}"}


def can_translate():
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
        return True
    except ImportError:
        return False


# ── HTTP ───────────────────────────────────────────────────────────────────────

def make_handler(models_dir, token):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self):
            if token and self.headers.get("X-Worker-Token") != token:
                self._send(403, {"error": "wrong or missing X-Worker-Token"})
                return False
            return True

        def log_message(self, *args):
            pass

        def do_GET(self):
            if not self._allowed():
                return
            if urlparse(self.path).path == "/status":
                return self._send(200, {"machine": state["machine"], "models": state["models"],
                                        "catalogue": MODELS, "version": VERSION, "device": device(),
                                        "cuda_failed": state.get("cuda_failed", ""), "jobs": jobs_now(),
                                        "translate": can_translate()})
            if urlparse(self.path).path == "/jobs":
                return self._send(200, {"jobs": jobs_now()})
            self._send(404, {"error": "unknown"})

        def do_POST(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            name = query.get("model", "")
            if url.path == "/look":
                return self._send(200, look(models_dir))
            if url.path == "/jobs/poll":
                # {key, url, model, language, since}: the job's captions since `since`,
                # starting it when it is not running
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if body.get("model") not in MODELS:
                        return self._send(400, {"error": f"unknown model: {body.get('model')}"})
                    if not str(body.get("url") or "").startswith(("http://", "https://")):
                        return self._send(400, {"error": "a stream URL, please"})
                    return self._send(200, poll_job(models_dir, str(body["key"]), body["url"], body["model"],
                                                    body.get("language"), int(body.get("since") or 0)))
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": str(e)})
            if url.path == "/jobs/stop":
                return self._send(200, {"stopped": stop_job(query.get("key", ""))})
            if url.path == "/translate":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    answer = translate_lines(models_dir, [str(x) for x in body.get("lines") or []],
                                             str(body.get("source") or ""), str(body.get("target") or ""))
                except Exception as e:
                    return self._send(500, {"error": f"translation failed: {e}"})
                return self._send(200 if "lines" in answer else 404, answer)
            if url.path in ("/download", "/benchmark", "/transcribe") and name not in MODELS:
                return self._send(400, {"error": f"unknown model: {name}"})
            if url.path == "/download":
                threading.Thread(target=download, args=(models_dir, name), daemon=True).start()
                return self._send(202, {"downloading": name})
            if url.path == "/benchmark":
                threading.Thread(target=benchmark, args=(models_dir, name), daemon=True).start()
                return self._send(202, {"benchmarking": name})
            if url.path == "/transcribe":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    audio = body_to_audio(self.rfile.read(length))
                    result = transcribe(models_dir, name, audio, query.get("language"),
                                        float(query.get("offset") or 0))
                except Exception as e:
                    return self._send(500, {"error": str(e)})
                return self._send(200, result)
            self._send(404, {"error": "unknown"})

    return Handler


def cuda_libraries():
    """Where NVIDIA's pip packages (nvidia-cublas-cu12, nvidia-cudnn-cu12) put their libraries,
    "" when they are not installed. CTranslate2 finds them only on LD_LIBRARY_PATH."""
    found = []
    for name in ("nvidia.cublas.lib", "nvidia.cudnn.lib"):
        try:
            module = __import__(name, fromlist=["_"])
            found += list(getattr(module, "__path__", []))[:1]
        except ImportError:
            pass
    return ":".join(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dispatch More caption worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9725)
    parser.add_argument("--models", default="/data/models/captions")
    parser.add_argument("--token", default=os.environ.get("CAPTIONS_WORKER_TOKEN", ""))
    args = parser.parse_args(argv)
    libs = cuda_libraries()
    if libs and argv is None and not os.environ.get("LD_LIBRARY_PATH", "").startswith(libs):
        # Started again with them on the path: the loader reads it only when a process starts
        os.environ["LD_LIBRARY_PATH"] = ":".join(p for p in (libs, os.environ.get("LD_LIBRARY_PATH")) if p)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    os.makedirs(args.models, exist_ok=True)
    look(args.models)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.models, args.token))
    print(f"Dispatch More caption worker on {args.host}:{args.port}, models in {args.models}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
