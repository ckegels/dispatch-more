"""Captions made from the sound (fork/subtitles.md §5b): what fits a server, the settings, the
request for the root installer, and the worker's own HTTP service."""

import io
import json
import tempfile
import threading
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.channels.captions import manager, worker

CARD_12GB = {"gpus": [{"name": "RTX 3060", "memory_mb": 12288, "free_mb": 11000}],
             "cpu": {"cores": 12, "arch": "x86_64", "avx2": True}}
CARD_6GB = {"gpus": [{"name": "GTX 1660", "memory_mb": 6144}], "cpu": {"cores": 4, "arch": "x86_64"}}
CARD_2GB = {"gpus": [{"name": "GT 1030", "memory_mb": 2048}], "cpu": {"cores": 4, "arch": "x86_64"}}
CPU_8 = {"gpus": [], "cpu": {"cores": 8, "arch": "x86_64", "avx2": True}}
CPU_4 = {"gpus": [], "cpu": {"cores": 4, "arch": "x86_64", "avx2": False}}
CPU_2 = {"gpus": [], "cpu": {"cores": 2, "arch": "x86_64", "avx2": True}}
PI = {"gpus": [], "cpu": {"cores": 4, "arch": "aarch64"}}


def measured(**channels):
    return {name: {"benchmark": {"channels": n, "rtf": 0.6 / max(n, 0.5), "device": "cpu"}}
            for name, n in channels.items()}


class ProposalTests(SimpleTestCase):
    def test_the_hardware_table(self):
        self.assertEqual(
            [(manager.guess(m)["model"], manager.guess(m)["translation"])
             for m in (CARD_12GB, CARD_6GB, CARD_2GB, CPU_8, CPU_4, CPU_2, PI)],
            [("large-v3-turbo", "ollama"), ("large-v3-turbo", "opus-mt"), ("small", ""),
             ("small", "opus-mt"), ("base", ""), ("tiny", ""), ("tiny", "")],
        )

    def test_a_guess_until_measured(self):
        proposal = manager.propose(CPU_8, {}, {"channels_at_once": 2})
        self.assertEqual((proposal["model"], proposal["measured"]), ("small", False))

    def test_measured_the_largest_that_keeps_up_with_room_to_spare(self):
        models = measured(tiny=9, base=5, small=3, medium=1)
        self.assertEqual(manager.propose(CPU_8, models, {"channels_at_once": 2, "quality": "balanced"})["model"], "small")
        self.assertEqual(manager.propose(CPU_8, models, {"channels_at_once": 3, "quality": "balanced"})["model"], "base")
        self.assertEqual(manager.propose(CPU_8, models, {"channels_at_once": 1, "quality": "best"})["model"], "medium")
        self.assertEqual(manager.propose(CPU_8, models, {"channels_at_once": 1, "quality": "channels"})["model"], "tiny")

    def test_nothing_keeps_up(self):
        proposal = manager.propose(CPU_4, measured(base=1, small=0), {"channels_at_once": 4})
        self.assertEqual(proposal["model"], "base")
        self.assertIn("fewer channels", proposal["why"])

    def test_a_better_model_the_hardware_suggests_is_to_be_measured_next(self):
        proposal = manager.propose(CARD_12GB, measured(small=8), {"channels_at_once": 2})
        self.assertEqual((proposal["model"], proposal["measure_next"]), ("small", "large-v3-turbo"))


class CaptionSettingsTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="admin", password="x", user_level=10)
        self.client = APIClient()
        self.client.force_authenticate(user)
        self.state = tempfile.mkdtemp()
        self.record = {"layout": "systemd", "release": "v244",
                       "captions_request": f"{self.state}/requests/captions",
                       "captions_status": f"{self.state}/captions-status.json"}
        patcher = mock.patch.object(manager, "_record", lambda: self.record)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ask", "ollama"):
            p = mock.patch.object(manager, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)

    def test_off_by_default_and_nothing_installed(self):
        page = self.client.get("/api/channels/captions/").json()
        self.assertFalse(page["settings"]["enabled"])
        self.assertFalse(page["worker"]["running"])
        self.assertEqual(page["worker_url"], "http://127.0.0.1:9725")
        self.assertTrue(page["install"]["can_request"])
        self.assertIsNone(page["docker"])
        self.assertEqual([m["name"] for m in page["models"]][:3], ["tiny", "base", "small"])
        self.assertIn("model", page["proposal"])

    def test_settings_are_kept_and_checked(self):
        self.client.put("/api/channels/captions/", {"enabled": True, "channels_at_once": 99, "quality": "x",
                                                    "model": "huge", "token": "secret"}, format="json")
        settings = manager.load()
        self.assertEqual((settings["enabled"], settings["channels_at_once"], settings["quality"], settings["model"],
                          settings["token"]), (True, 20, "balanced", "", "secret"))
        page = self.client.put("/api/channels/captions/", {"token": "••••••••"}, format="json").json()
        self.assertEqual(manager.load()["token"], "secret", "the masked token sent back leaves it as it was")
        self.assertEqual(page["settings"]["token"], "••••••••")

    def test_install_leaves_a_request_for_the_root_watcher(self):
        page = self.client.post("/api/channels/captions/action/", {"action": "install"}, format="json").json()
        request = json.loads(Path(self.record["captions_request"]).read_text())
        self.assertEqual((request["action"], request["by"]), ("install", "admin"))
        self.assertTrue(page["install"]["requested"])
        Path(self.record["captions_status"]).write_text('{"state": "installing", "step": "Installing faster-whisper"}')
        self.assertEqual(manager.install_state()["step"], "Installing faster-whisper")

    def test_docker_cannot_install_from_the_page_but_gets_a_container(self):
        self.record["layout"] = "docker"
        answer = self.client.post("/api/channels/captions/action/", {"action": "install"}, format="json")
        self.assertEqual(answer.status_code, 400)
        self.assertFalse(Path(self.record["captions_request"]).exists())
        page = self.client.get("/api/channels/captions/").json()
        self.assertEqual(page["worker_url"], "http://dispatch-more-captions:9725")
        self.assertIn("/v244/apps/channels/captions/worker.py", page["docker"]["cpu"])
        self.assertNotIn("nvidia", page["docker"]["cpu"])
        self.assertIn("driver: nvidia", page["docker"]["gpu"])

    def test_the_worker_not_answering_is_said(self):
        answer = self.client.post("/api/channels/captions/action/", {"action": "benchmark", "model": "tiny"}, format="json")
        self.assertEqual((answer.status_code, answer.json()["error"]), (400, "The caption worker does not answer."))
        answer = self.client.post("/api/channels/captions/action/", {"action": "download", "model": "huge"}, format="json")
        self.assertEqual(answer.status_code, 400)


class DockerComposeTests(SimpleTestCase):
    def test_the_compose_text_is_yaml_with_a_shell_command(self):
        import yaml

        with mock.patch.object(manager, "_record", lambda: {"release": "v244"}):
            for gpu in (False, True):
                service = yaml.safe_load("services:\n" + manager.docker_compose(gpu))["services"]["dispatch-more-captions"]
                command = service["command"]
                self.assertTrue(command.startswith('sh -c "pip install -q faster-whisper'))
                self.assertIn("python /worker.py --host 0.0.0.0 --models /models", command)
                self.assertEqual("nvidia-cudnn-cu12" in command, gpu)
                self.assertEqual("deploy" in service, gpu)


class WorkerServiceTests(SimpleTestCase):
    """The worker's HTTP side, without faster-whisper: what it says about the machine, and
    that it refuses what it does not know."""

    def setUp(self):
        self.models = tempfile.mkdtemp()
        worker.look(self.models)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), worker.make_handler(self.models, "t0ken"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, path, method="GET", token="t0ken", body=None):
        req = Request(self.base + path, data=body, method=method, headers={"X-Worker-Token": token})
        try:
            with urlopen(req, timeout=5) as answer:
                return answer.status, json.loads(answer.read())
        except Exception as e:  # HTTPError
            return e.code, json.loads(e.read())

    def test_status_says_what_the_machine_has(self):
        code, body = self.call("/status")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(body["machine"]["cpu"]["cores"], 1)
        self.assertIn("total_mb", body["machine"]["memory"])
        self.assertEqual(set(body["models"]), set(worker.MODELS))
        self.assertFalse(body["models"]["tiny"]["downloaded"])

    def test_the_token_is_asked_for(self):
        self.assertEqual(self.call("/status", token="wrong")[0], 403)

    def test_an_unknown_model_is_refused(self):
        self.assertEqual(self.call("/download?model=huge", method="POST")[0], 400)

    def test_a_downloaded_model_is_seen(self):
        Path(self.models, "base").mkdir()
        Path(self.models, "base", "model.bin").write_bytes(b"x")
        self.call("/look", method="POST")
        self.assertTrue(self.call("/status")[1]["models"]["base"]["downloaded"])

    def test_wav_and_pcm_are_both_read(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy")
        pcm = (b"\x00\x10" * 1600)
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        self.assertEqual(len(worker.body_to_audio(out.getvalue())), 1600)
        self.assertEqual(len(worker.body_to_audio(pcm)), 1600)
