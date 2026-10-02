"""Server rewind (live_proxy/rewind.py): what is kept, the playlist made from disk, the budget,
one recorder per channel, and the API."""

import json
import os
import tempfile
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.channels.captions import is_caption_client
from apps.proxy.live_proxy import rewind

CONF = {"enabled": True, "minutes": 60, "max_pause_minutes": 240, "budget_gb": 20}


class FakeRedis:
    def __init__(self):
        self.data = {}

    def hset(self, key, field, value):
        self.data.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return dict(self.data.get(key) or {})

    def hget(self, key, field):
        return (self.data.get(key) or {}).get(field)

    def hdel(self, key, field):
        (self.data.get(key) or {}).pop(field, None)

    def expire(self, key, seconds):
        pass

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


def write_part(folder, part, start_ms, durations, names=None):
    """A recorder's own playlist and its segment files, as ffmpeg writes them."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    wall = start_ms
    for i, d in enumerate(durations):
        name = (names or [])[i] if names else f"p{part}-{i:06d}.ts"
        lines += [f"#EXT-X-PROGRAM-DATE-TIME:{rewind._iso(wall)}", f"#EXTINF:{d:.3f},", name]
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(b"\x47" * 188 * 10)
        wall += int(d * 1000)
    with open(os.path.join(folder, f"part-{part}.m3u8"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


class _Folder(SimpleTestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"DISPATCHARR_REWIND_DIR": self.base})
        env.start()
        self.addCleanup(env.stop)
        conf = mock.patch.object(rewind, "settings", lambda: dict(CONF))
        conf.start()
        self.addCleanup(conf.stop)
        rewind._recorders.clear()
        self.addCleanup(rewind._recorders.clear)
        self.uuid = "6f4c3991-f5e6-474e-8e00-a970a654c1d1"
        self.folder = rewind.channel_dir(self.uuid)
        os.makedirs(self.folder)


class KeptTests(SimpleTestCase):
    def test_the_last_hour_and_a_paused_tv_within_the_longest_pause(self):
        now = 10_000_000.0
        now_ms = int(now * 1000)
        self.assertEqual(rewind.keep_from_ms(CONF, {}, now), now_ms - 60 * 60_000)
        paused = {"tv": {"paused_at": now_ms - 3 * 3600_000}}
        self.assertEqual(rewind.keep_from_ms(CONF, paused, now), now_ms - 3 * 3600_000 - 30_000)
        long_ago = {"tv": {"paused_at": now_ms - 9 * 3600_000}}
        self.assertEqual(rewind.keep_from_ms(CONF, long_ago, now), now_ms - 240 * 60_000)

    def test_the_recorder_is_not_a_viewer(self):
        self.assertTrue(is_caption_client(rewind.USER_AGENT))
        self.assertFalse(is_caption_client("AerioTV/0.5.9"))


class PlaylistTests(_Folder):
    def test_made_from_disk_with_wall_times_and_a_mark_between_parts(self):
        write_part(self.folder, 1, 1_790_000_000_000, [6.0, 6.0])
        write_part(self.folder, 2, 1_790_000_020_000, [6.0])
        text = rewind.playlist(self.uuid)
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", text)
        self.assertEqual(text.count("#EXT-X-DISCONTINUITY"), 1)
        self.assertLess(text.index("p1-000001.ts"), text.index("#EXT-X-DISCONTINUITY"))
        self.assertIn("#EXT-X-PROGRAM-DATE-TIME:2026-09-21", text)
        window = rewind.window(self.uuid)
        self.assertEqual((window["tail_wall_ms"], window["head_wall_ms"]), (1_790_000_000_000, 1_790_000_026_000))

    def test_a_segment_gone_from_disk_is_left_out(self):
        write_part(self.folder, 1, 1_790_000_000_000, [6.0, 6.0, 6.0])
        os.remove(os.path.join(self.folder, "p1-000000.ts"))
        names = [s[3] for s in rewind.segments(self.uuid)]
        self.assertEqual(names, ["p1-000001.ts", "p1-000002.ts"])
        self.assertEqual(rewind.segments(self.uuid)[0][1], 1_790_000_006_000, "its own wall time, not the first's")

    def test_trimmed_before_the_moment_to_keep(self):
        write_part(self.folder, 1, 1_790_000_000_000, [6.0] * 5)
        rewind.trim(self.uuid, 1_790_000_013_000)
        self.assertEqual([s[3] for s in rewind.segments(self.uuid)], ["p1-000002.ts", "p1-000003.ts", "p1-000004.ts"])

    def test_only_segment_names_are_served(self):
        write_part(self.folder, 1, 1_790_000_000_000, [6.0])
        self.assertIsNotNone(rewind.segment_path(self.uuid, "p1-000000.ts"))
        self.assertIsNone(rewind.segment_path(self.uuid, "../../etc/passwd"))
        self.assertIsNone(rewind.segment_path(self.uuid, "part-1.m3u8"))


class BudgetTests(_Folder):
    def test_over_the_budget_the_oldest_unpaused_minutes_go_first(self):
        other = "11111111-2222-3333-4444-555555555555"
        os.makedirs(rewind.channel_dir(other))
        write_part(self.folder, 1, 1_790_000_000_000, [6.0] * 4)  # older, but someone is paused on it
        write_part(rewind.channel_dir(other), 1, 1_790_000_100_000, [6.0] * 4)
        redis = FakeRedis()
        redis.hset(rewind._viewers_key(self.uuid), "1:tv",
                   json.dumps({"until": time.time() + 60, "paused_at": 1_790_000_000_000}))
        seg = 188 * 10
        with mock.patch.object(rewind, "budget_bytes", lambda conf: 6 * seg + 400):
            removed = rewind.enforce_budget(CONF, redis)
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(len(rewind.segments(self.uuid)), 4, "the paused channel kept")
        self.assertLess(len(rewind.segments(other)), 4)


class WatchTests(_Folder):
    def test_one_recorder_per_channel_across_workers(self):
        redis = FakeRedis()
        with mock.patch.object(rewind.Recorder, "start") as start, \
             mock.patch.object(rewind, "_people_on", return_value=True), \
             mock.patch.object(rewind, "channel_running", return_value=True):
            rewind.watch(self.uuid, "1:tv", redis_client=redis)
            rewind.watch(self.uuid, "2:tv", redis_client=redis)
            self.assertEqual(start.call_count, 1)
            rewind._recorders.clear()
            # Another worker: the lease is held, nothing started
            rewind.ensure_recorder(self.uuid, redis)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(set(rewind.viewers(self.uuid, redis)), {"1:tv", "2:tv"})
        rewind.leave(self.uuid, "1:tv", redis)
        self.assertEqual(set(rewind.viewers(self.uuid, redis)), {"2:tv"})

    def test_a_keep_alive_alone_never_holds_a_channel(self):
        # 2026-10-02: the TV watched a stream of its own, or had left; the recorder kept the
        # channel and its provider connection open for nobody
        redis = FakeRedis()
        with mock.patch.object(rewind.Recorder, "start") as start, \
             mock.patch.object(rewind, "_people_on", return_value=False), \
             mock.patch.object(rewind, "channel_running", return_value=True):
            rewind.watch(self.uuid, "1:tv", redis_client=redis)
            start.assert_not_called()
            self.assertFalse(rewind.needed(self.uuid, rewind.viewers(self.uuid, redis), redis))
            # Paused, the TV plays the recording itself: it is kept
            rewind.watch(self.uuid, "1:tv", paused_at_ms=1_790_000_000_000, redis_client=redis)
            start.assert_called_once()
        self.assertFalse(rewind.needed(self.uuid, {}, redis))

    def test_never_starts_a_channel_that_does_not_run(self):
        # Its own request to a stopped channel started it again for nobody (2026-10-02)
        redis = FakeRedis()
        with mock.patch.object(rewind.Recorder, "start") as start, \
             mock.patch.object(rewind, "_people_on", return_value=True):
            for state in (None, "error", "stopping", "stopped"):
                redis.data.pop("live:channel:%s:metadata" % self.uuid, None)
                if state:
                    redis.hset("live:channel:%s:metadata" % self.uuid, "state", state)
                rewind.watch(self.uuid, "1:tv", paused_at_ms=1_790_000_000_000, redis_client=redis)
            start.assert_not_called()
            redis.hset("live:channel:%s:metadata" % self.uuid, "state", "active")
            rewind.watch(self.uuid, "1:tv", redis_client=redis)
            start.assert_called_once()

    def test_off_records_nothing(self):
        with mock.patch.object(rewind, "settings", lambda: {**CONF, "enabled": False}), \
             mock.patch.object(rewind, "ensure_recorder") as ensure:
            self.assertEqual(rewind.watch(self.uuid, "1:tv", redis_client=FakeRedis()), {"enabled": False})
        ensure.assert_not_called()


class ViewTests(TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        for p in (mock.patch.dict(os.environ, {"DISPATCHARR_REWIND_DIR": self.base}),
                  mock.patch.object(rewind, "settings", lambda: dict(CONF)),
                  mock.patch.object(rewind, "_redis", lambda: self.redis)):
            p.start()
            self.addCleanup(p.stop)
        self.redis = FakeRedis()
        rewind._recorders.clear()
        self.addCleanup(rewind._recorders.clear)
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="tv", password="x", user_level=1))
        self.uuid = "6f4c3991-f5e6-474e-8e00-a970a654c1d1"

    def test_watch_window_playlist_and_segment(self):
        with mock.patch.object(rewind.Recorder, "start"):
            answer = self.client.post(f"/api/channels/rewind/{self.uuid}/", {"viewer": "shield", "paused_at": 123},
                                      format="json").json()
        self.assertEqual((answer["enabled"], answer["recording"]), (True, False))
        entry = json.loads(self.redis.hgetall(rewind._viewers_key(self.uuid))[f"{self.client.handler._force_user.id}:shield"])
        self.assertEqual(entry["paused_at"], 123)
        self.assertEqual(self.client.get(f"/proxy/ts/rewind/{self.uuid}/index.m3u8").status_code, 404)
        folder = rewind.channel_dir(self.uuid)
        os.makedirs(folder, exist_ok=True)
        write_part(folder, 1, 1_790_000_000_000, [6.0])
        playlist = self.client.get(f"/proxy/ts/rewind/{self.uuid}/index.m3u8")
        self.assertEqual(playlist.status_code, 200)
        self.assertIn(b"p1-000000.ts", b"".join(playlist.streaming_content) if playlist.streaming else playlist.content)
        segment = self.client.get(f"/proxy/ts/rewind/{self.uuid}/p1-000000.ts")
        self.assertEqual((segment.status_code, segment["Content-Type"]), (200, "video/mp2t"))
        self.assertEqual(self.client.delete(f"/api/channels/rewind/{self.uuid}/", {"viewer": "shield"}, format="json").json(),
                         {"left": True})
