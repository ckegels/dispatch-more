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

VERSION = 1
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
                                        "cuda_failed": state.get("cuda_failed", "")})
            self._send(404, {"error": "unknown"})

        def do_POST(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            name = query.get("model", "")
            if url.path == "/look":
                return self._send(200, look(models_dir))
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
