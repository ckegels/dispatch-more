"""The memory recorder, its history and the in-flight record (memory_recorder.py,
memory_history.py, inflight.py). Why it exists: fork/HANDOVER.md §5.4c, "8 GB and a frozen
container" -- the next time the server fills up, what led to it must already be on disk, also
after a reboot or a crash."""

import gzip
import json
import os
import shutil
import tempfile
import time
import zipfile
from unittest import mock

from django.http import StreamingHttpResponse, HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import inflight, memory_history, memory_recorder as rec


class Folders:
    def make_folders(self):
        self.dir = tempfile.mkdtemp(prefix="memhist-")
        self.inflight_dir = tempfile.mkdtemp(prefix="inflight-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(shutil.rmtree, self.inflight_dir, True)
        patcher = mock.patch.dict(os.environ, {"MEMORY_HISTORY_DIR": self.dir, "MEMORY_INFLIGHT_DIR": self.inflight_dir})
        patcher.start()
        self.addCleanup(patcher.stop)


def record(t, boot="aaaaaaaa", procs=None, cg=1000, redis=None, **extra):
    """A sample as the recorder writes it."""
    return {
        "t": t, "boot": boot, "every": 30,
        "sys": {"total": 8192, "available": 8192 - cg, "swap_total": 1024, "swap_free": 1024},
        "cg": {"current": cg, "max": 8192, "anon": cg - 100},
        "psi": {"some": [0.0, 0.0], "full": [0.0, 0.0]},
        "redis": redis if redis is not None else {"ms": 1.0, "used": 3},
        "procs": procs if procs is not None else [[100, 1, "Web worker (uWSGI)", 150, 140, 0, "S", 7, 1.0, t - 60, "uwsgi"]],
        "others": [], "inflight": {}, **extra,
    }


class RecorderTests(Folders, SimpleTestCase):
    def setUp(self):
        self.make_folders()

    def lines(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".jsonl"):
                with open(os.path.join(self.dir, name)) as fh:
                    out += [json.loads(line) for line in fh]
        return out

    def test_a_sample_has_every_part_and_this_process(self):
        sample = rec.sample()
        for key in ("t", "boot", "sys", "psi", "redis", "inflight", "procs", "others"):
            self.assertIn(key, sample)
        self.assertGreater(sample["sys"]["total"], 0)
        # The test runner is "manage.py test": a Dispatcharr process
        self.assertTrue(any(p[0] == os.getpid() for p in sample["procs"]))
        mine = next(p for p in sample["procs"] if p[0] == os.getpid())
        self.assertGreater(mine[3], 0)
        self.assertEqual(len(mine), 11)

    def test_off_writes_nothing_and_on_writes_a_start(self):
        rec.run(self.dir, once=True)
        self.assertEqual(self.lines(), [])
        open(os.path.join(self.dir, rec.FLAG), "w").close()
        rec.run(self.dir, once=True)
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["start"])
        self.assertEqual(lines[0]["every"], rec.INTERVAL)

    def test_one_failing_part_never_costs_the_sample(self):
        with mock.patch.object(rec, "redis", side_effect=RuntimeError("boom")):
            sample = rec.sample()
        self.assertIn("boom", sample["redis"]["error"])
        self.assertIn("sys", sample)

    def test_redis_that_does_not_answer_is_recorded_not_raised(self):
        with mock.patch.object(rec, "REDIS", ("127.0.0.1", 1)):
            answer = rec.redis()
        self.assertIn("error", answer)

    def test_finished_days_are_gzipped_and_old_ones_dropped(self):
        old = time.time() - 20 * 86400
        yesterday = time.time() - 86400
        for t in (old, yesterday, time.time()):
            rec.write(self.dir, record(int(t)))
        rec.tidy(self.dir, rec._day(time.time()))
        names = sorted(os.listdir(self.dir))
        self.assertEqual(names, [rec._day(yesterday) + ".jsonl.gz", rec._day(time.time()) + ".jsonl"])
        with gzip.open(os.path.join(self.dir, names[0]), "rt") as fh:
            self.assertEqual(json.loads(fh.readline())["t"], int(yesterday))

    def test_over_the_size_cap_the_oldest_day_goes(self):
        for days_ago in (3, 2, 1):
            rec.write(self.dir, record(int(time.time() - days_ago * 86400)))
        with mock.patch.object(rec, "KEEP_BYTES", 1):
            rec.tidy(self.dir, rec._day(time.time()))
        self.assertEqual(len(os.listdir(self.dir)), 1)

    def test_something_alarming_samples_faster(self):
        calm = record(0, cg=1000)
        self.assertFalse(rec._alarming(record(30, cg=1100), calm))
        self.assertTrue(rec._alarming(record(30, cg=1400), calm))
        self.assertTrue(rec._alarming(record(30, redis={"ms": 900.0}), calm))
        self.assertTrue(rec._alarming(record(30, redis={"error": "timeout"}), calm))
        stuck = [[1, 0, "Web worker (uWSGI)", 900, None, 0, "D", 7, 1.0, 0, "uwsgi"]]
        self.assertTrue(rec._alarming(record(30, procs=stuck), calm))
        self.assertFalse(rec._alarming(calm, None))


class HistoryTests(Folders, SimpleTestCase):
    def setUp(self):
        self.make_folders()
        self.now = int(time.time())

    def test_a_torn_last_line_is_skipped(self):
        rec.write(self.dir, record(self.now - 60))
        rec.write(self.dir, record(self.now - 30))
        with open(os.path.join(self.dir, rec._day(self.now - 30) + ".jsonl"), "a") as fh:
            fh.write('{"t": 17925')  # the power went out mid-line
        self.assertEqual(len(list(memory_history.records(self.now - 3600, self.now))), 2)

    def test_reads_across_a_gzipped_day(self):
        rec.write(self.dir, record(self.now - 86400))
        rec.tidy(self.dir, rec._day(self.now))
        rec.write(self.dir, record(self.now - 10))
        self.assertEqual(len(list(memory_history.records(self.now - 2 * 86400, self.now))), 2)

    def test_events_reboot_gap_worker_jump_redis_and_stuck(self):
        t = self.now - 3000
        worker = lambda pid, mb, state="S": [pid, 1, "Web worker (uWSGI)", mb, mb, 0, state, 7, 1.0, t, "uwsgi"]
        samples = [
            record(t, procs=[worker(100, 200)], start=True),
            record(t + 30, procs=[worker(100, 300)]),
            # a worker at 6 GB waits in D, Redis slows down, memory jumps
            record(t + 60, procs=[worker(100, 6000, "D")], cg=7000, redis={"ms": 2300.0}),
            # the worker is killed and replaced
            record(t + 90, procs=[worker(101, 150)], cg=2000),
            # nothing for 20 minutes, then the same boot
            record(t + 1290, procs=[worker(101, 150)], cg=2000),
            # a reboot
            record(t + 1900, boot="bbbbbbbb", procs=[worker(5, 140)], cg=900, start=True),
            # the recorder itself restarted 5 s later (a crash), the same boot
            record(t + 1935, boot="bbbbbbbb", procs=[worker(5, 140)], cg=900, start=True),
        ]
        for s in samples:
            rec.write(self.dir, s)
        kinds = [e["kind"] for e in memory_history.events_of(samples)]
        for kind in ("start", "jump", "redis", "stuck", "worker", "gap", "reboot"):
            self.assertIn(kind, kinds, kinds)
        self.assertIn("started again", memory_history.events_of(samples)[-1]["text"])
        worker_event = next(e for e in memory_history.events_of(samples) if e["kind"] == "worker")
        self.assertIn("6000 MB", worker_event["text"])

    def test_many_samples_become_at_most_POINTS_keeping_the_peaks(self):
        start = self.now - 3590  # inside the hour however long the test takes
        for i in range(400):
            rec.write(self.dir, record(start + i * 8, cg=7000 if i == 123 else 1000))
        with mock.patch.object(memory_history, "POINTS", 50):
            view = memory_history.overview(1)
        self.assertLessEqual(len(view["points"]), 51)
        self.assertEqual(max(p["container"] for p in view["points"]), 7000)
        self.assertEqual(view["samples"], 400)

    def test_the_moment_closest_to_a_time(self):
        for dt in (0, 30, 60):
            rec.write(self.dir, record(self.now - 300 + dt))
        self.assertEqual(memory_history.at(self.now - 268)["t"], self.now - 270)
        self.assertIsNone(memory_history.at(self.now - 100000))

    def test_the_archive_holds_the_raw_files(self):
        rec.write(self.dir, record(self.now))
        with zipfile.ZipFile(__import__("io").BytesIO(memory_history.archive())) as zf:
            self.assertEqual(zf.namelist(), [rec._day(self.now) + ".jsonl"])


class InflightTests(Folders, SimpleTestCase):
    def setUp(self):
        self.make_folders()
        inflight._state.checked = 0.0
        inflight._state.active = {}
        inflight._state.done = {}
        self.addCleanup(setattr, inflight._state, "checked", 0.0)

    def switch_on(self):
        open(os.path.join(self.dir, rec.FLAG), "w").close()
        inflight._state.checked = 0.0

    def written(self):
        inflight._write()
        with open(os.path.join(self.inflight_dir, f"{os.getpid()}.json")) as fh:
            return json.load(fh)

    def test_paths_are_counted_without_their_ids(self):
        self.assertEqual(inflight.short_path("/proxy/ts/stream/531ccc52-06eb-44b4-b795-f88af657472a"),
                         "/proxy/ts/stream/<id>")
        self.assertEqual(inflight.short_path("/api/epg/programs/149180513/"), "/api/epg/programs/<id>/")

    def test_off_the_middleware_records_nothing(self):
        middleware = inflight.InflightMiddleware(lambda request: HttpResponse("ok"))
        middleware(RequestFactory().get("/api/epg/epgdata/"))
        self.assertEqual(inflight._state.active, {})
        self.assertEqual(inflight._state.done, {})

    def test_a_request_is_in_flight_until_it_answers(self):
        self.switch_on()
        seen = {}

        def view(request):
            seen["during"] = self.written()
            return HttpResponse("ok")

        inflight.InflightMiddleware(view)(RequestFactory().get("/api/epg/epgdata/", REMOTE_ADDR="192.168.65.7"))
        self.assertEqual(seen["during"]["n"], 1)
        self.assertEqual(seen["during"]["req"][0][:3], ["GET", "/api/epg/epgdata/", "192.168.65.7"])
        after = self.written()
        self.assertEqual(after["n"], 0)
        self.assertEqual(after["done"]["/api/epg/epgdata/"][0], 1)

    def test_a_stream_stays_in_flight_until_it_ends(self):
        self.switch_on()
        response = inflight.InflightMiddleware(
            lambda request: StreamingHttpResponse(iter([b"a", b"b"]))
        )(RequestFactory().get("/proxy/ts/stream/531ccc52-06eb-44b4-b795-f88af657472a"))
        self.assertEqual(self.written()["n"], 1)
        self.assertEqual(b"".join(response.streaming_content), b"ab")
        self.assertEqual(self.written()["n"], 0)

    def test_a_view_that_raises_is_no_longer_in_flight(self):
        self.switch_on()

        def broken(request):
            raise ValueError("no")

        with self.assertRaises(ValueError):
            inflight.InflightMiddleware(broken)(RequestFactory().get("/x/"))
        self.assertEqual(inflight._state.active, {})

    def test_celery_tasks_are_recorded_by_name(self):
        self.switch_on()
        task = mock.Mock()
        task.name = "apps.channels.tasks.stream_check"
        inflight.task_started(task_id="t1", task=task)
        self.assertEqual(self.written()["req"][0][:2], ["TASK", "apps.channels.tasks.stream_check"])
        inflight.task_finished(task_id="t1")
        self.assertEqual(self.written()["n"], 0)

    def test_the_recorder_reads_the_files_and_drops_dead_ones(self):
        self.switch_on()
        inflight.begin("k", "GET", "/api/x/", "1.2.3.4")
        self.written()
        with open(os.path.join(self.inflight_dir, "999999999.json"), "w") as fh:
            fh.write("{}")
        with mock.patch.object(rec, "INFLIGHT_DIR", self.inflight_dir):
            seen = rec.inflight()
        self.assertIn(str(os.getpid()), seen)
        self.assertNotIn("999999999", seen)
        self.assertFalse(os.path.exists(os.path.join(self.inflight_dir, "999999999.json")))
        inflight.end("k")


class HistoryViewTests(Folders, TestCase):
    def setUp(self):
        self.make_folders()
        patcher = mock.patch("celery.app.task.Task.apply_async")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_switching_on_and_off_from_the_tab(self):
        url = "/proxy/diagnostics/memory/history/"
        self.assertFalse(self.client.get(url).json()["on"])
        self.assertTrue(self.client.post(url, {"on": True}, format="json").json()["on"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, rec.FLAG)))
        self.assertFalse(self.client.post(url, {"on": False}, format="json").json()["on"])

    def test_points_moment_and_download(self):
        now = int(time.time())
        rec.write(self.dir, record(now - 60, start=True))
        rec.write(self.dir, record(now - 30))
        view = self.client.get("/proxy/diagnostics/memory/history/?hours=1").json()
        self.assertEqual(len(view["points"]), 2)
        self.assertEqual(view["events"][0]["kind"], "start")
        moment = self.client.get(f"/proxy/diagnostics/memory/history/moment/?t={now - 31}")
        self.assertEqual(moment.json()["t"], now - 30)
        self.assertEqual(self.client.get("/proxy/diagnostics/memory/history/moment/?t=1").status_code, 404)
        download = self.client.get("/proxy/diagnostics/memory/history/download/")
        self.assertEqual(download["Content-Type"], "application/zip")

    def test_only_admins(self):
        alice = User.objects.create_user(username="alice", password="x", user_level=1)
        client = APIClient()
        client.force_authenticate(user=alice)
        self.assertEqual(client.get("/proxy/diagnostics/memory/history/").status_code, 403)
        self.assertEqual(client.post("/proxy/diagnostics/memory/history/", {"on": True}, format="json").status_code, 403)
        self.assertFalse(os.path.exists(os.path.join(self.dir, rec.FLAG)))
