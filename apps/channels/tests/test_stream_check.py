"""Stream Check (channels.stream_check): looking at every stream on a channel for a picture.

What matters most here is what it must never do: count a check cut short for a viewer
against a stream, go past a provider's connection limit or keep a connection, run while
someone is watching, touch a channel's fallback, or lose where a parked stream was.
"""

import fnmatch
import http.server
import shutil
import subprocess
import threading
import time
from unittest import mock, skipUnless

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import channel_manager, stream_check
from apps.channels.models import Channel, ChannelGroup, ChannelStream, Stream
from apps.m3u.models import M3UAccount, M3UAccountProfile


class FakeRedis:
    """Enough Redis for a run: strings with expiry and nx, counters, hashes, key patterns."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        with self.lock:
            if nx and key in self.data:
                return None
            self.data[key] = str(value)
            return True

    def exists(self, *keys):
        return sum(1 for key in keys if key in self.data)

    def delete(self, *keys):
        with self.lock:
            for key in keys:
                self.data.pop(key, None)

    def expire(self, key, seconds):
        return key in self.data

    def incr(self, key):
        with self.lock:
            self.data[key] = str(int(self.data.get(key) or 0) + 1)
            return int(self.data[key])

    def decr(self, key):
        with self.lock:
            self.data[key] = str(int(self.data.get(key) or 0) - 1)
            return int(self.data[key])

    def hset(self, key, field, value):
        with self.lock:
            self.data.setdefault(key, {})[field] = value

    def hgetall(self, key):
        value = self.data.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def hdel(self, key, *fields):
        for field in fields:
            (self.data.get(key) or {}).pop(field, None)

    def scan_iter(self, match="*", count=None):
        return iter([key for key in list(self.data) if fnmatch.fnmatch(key, match)])


def _video(seconds=2):
    """A few seconds of real MPEG-TS, 320x240, made by ffmpeg."""
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={seconds}",
            "-c:v", "mpeg2video", "-f", "mpegts", "pipe:1",
        ],
        capture_output=True, check=True,
    ).stdout


class _Provider(http.server.BaseHTTPRequestHandler):
    """A provider: one path per way a stream can be."""

    video = b""

    def log_message(self, *args):
        pass

    def _send(self, body, kind="video/mp2t", status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/live.ts":
            self._send(self.video)
        elif self.path == "/gone.ts":
            self._send(b"not found", "text/plain", 404)
        elif self.path == "/page.ts":
            self._send(b"<!DOCTYPE html><html>Your subscription has expired</html>" + b" " * 20000, "text/html")
        elif self.path == "/silent.ts":
            # Answers, then sends nothing at all
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            self.wfile.flush()
            time.sleep(5)
        elif self.path == "/master.m3u8":
            self._send(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvariant.m3u8\n", "application/vnd.apple.mpegurl")
        elif self.path == "/variant.m3u8":
            self._send(b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nold.ts\n#EXTINF:2,\nlive.ts\n", "application/vnd.apple.mpegurl")
        elif self.path.startswith("/player_api.php"):
            # An Xtream Codes provider asked about a login: this one knows only user/pass
            good = "username=user&password=pass" in self.path
            body = b'{"user_info": {"auth": 1, "status": "Active", "active_cons": "0", "max_connections": "1"}}' if good else b'{"user_info": {"auth": 0}}'
            self._send(body, "application/json")
        elif self.path == "/empty.m3u8":
            self._send(b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n", "application/vnd.apple.mpegurl")
        else:
            self._send(b"", status=404)


@skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg and ffprobe")
class ProbeTests(TestCase):
    """Against a real HTTP server, since how a provider fails is the whole point."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _Provider.video = _video()
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def test_a_stream_that_plays(self):
        found = stream_check.probe(f"{self.base}/live.ts", timeout=5)
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["resolution"], "320x240")

    def test_a_stream_the_provider_no_longer_has(self):
        found = stream_check.probe(f"{self.base}/gone.ts", timeout=5)
        self.assertFalse(found["ok"])
        self.assertIn("404", found["reason"])

    def test_a_page_where_the_video_should_be(self):
        """A provider that is done with an account often answers 200 with a page."""
        found = stream_check.probe(f"{self.base}/page.ts", timeout=5)
        self.assertFalse(found["ok"])
        self.assertIn("page", found["reason"])

    def test_a_stream_that_answers_and_sends_nothing(self):
        found = stream_check.probe(f"{self.base}/silent.ts", timeout=3)
        self.assertFalse(found["ok"])
        self.assertTrue(found["reason"])

    def test_a_playlist_is_followed_to_its_video(self):
        found = stream_check.probe(f"{self.base}/master.m3u8", timeout=5)
        self.assertTrue(found["ok"], found)

    def test_a_playlist_with_nothing_in_it(self):
        found = stream_check.probe(f"{self.base}/empty.m3u8", timeout=5)
        self.assertFalse(found["ok"])

    def test_nothing_listening(self):
        found = stream_check.probe("http://127.0.0.1:9/live.ts", timeout=3)
        self.assertFalse(found["ok"])

    def test_an_xtream_login_is_asked_about(self):
        account = M3UAccount.objects.create(
            name="XC", account_type="XC", server_url=self.base, username="user", password="pass", is_active=True
        )
        login = account.profiles.first() or M3UAccountProfile.objects.create(
            m3u_account=account, name="default", is_default=True, max_streams=1
        )
        self.assertIsNone(stream_check._login_problem(account, login, ""))
        account.password = "wrong"
        account.save()
        self.assertEqual(stream_check._login_problem(account, login, ""), "the provider refused the login")

    def test_a_check_cut_short_says_nothing(self):
        with self.assertRaises(stream_check.Stopped):
            stream_check.probe(f"{self.base}/live.ts", timeout=5, should_stop=lambda: True)


class WhenTests(TestCase):
    def test_a_window_over_midnight(self):
        from datetime import datetime

        night = {"window_from": "23:00", "window_to": "06:00"}
        self.assertTrue(stream_check.in_window(night, datetime(2026, 1, 1, 2, 0)))
        self.assertTrue(stream_check.in_window(night, datetime(2026, 1, 1, 23, 30)))
        self.assertFalse(stream_check.in_window(night, datetime(2026, 1, 1, 12, 0)))
        self.assertTrue(stream_check.in_window({"window_from": "", "window_to": ""}))

    def test_nothing_runs_by_itself_while_it_is_off(self):
        self.assertFalse(stream_check.due(stream_check.load_settings(), FakeRedis()))
        self.assertTrue(stream_check.due({**stream_check.load_settings(), "enabled": True}, FakeRedis()))

    def test_broken_takes_more_than_one_bad_run(self):
        settings = {"broken_after": 2}
        self.assertEqual(stream_check.state_of({"ok": False, "failures": 1}, settings), "failing")
        self.assertEqual(stream_check.state_of({"ok": False, "failures": 2}, settings), "broken")
        self.assertEqual(stream_check.state_of({"ok": True, "failures": 0}, settings), "ok")

    def test_settings_are_checked(self):
        with self.assertRaises(ValueError):
            stream_check.save_settings({"window_from": "25:00"})
        saved = stream_check.save_settings({"enabled": True, "timeout_seconds": 999})
        self.assertEqual(saved["timeout_seconds"], 60)

    def test_a_viewer_is_only_made_way_for_while_a_run_is_going(self):
        redis = FakeRedis()
        stream_check.make_way(redis)
        self.assertFalse(redis.exists(stream_check.YIELD_KEY))
        redis.set(stream_check.RUN_KEY, "1")
        stream_check.make_way(redis)
        self.assertTrue(redis.exists(stream_check.YIELD_KEY))


class _Setup(TestCase):
    def setUp(self):
        self.a = M3UAccount.objects.create(name="Provider A", account_type="STD", server_url="http://a", is_active=True)
        self.b = M3UAccount.objects.create(name="Provider B", account_type="STD", server_url="http://b", is_active=True)
        for account in (self.a, self.b):
            M3UAccountProfile.objects.filter(m3u_account=account).delete()
            M3UAccountProfile.objects.create(m3u_account=account, name="default", is_default=True, max_streams=1)
        self.group = ChannelGroup.objects.create(name="┃AT┃ AUSTRIA")
        self.fallback = Stream.objects.create(name="could not dispatch", url="http://local/fallback", is_custom=True)
        self.orf1 = Channel.objects.create(name="┃AT┃ ORF 1", channel_number=1, channel_group=self.group)
        self.first = self._stream("ORF 1 A", self.a)
        self.second = self._stream("ORF 1 B", self.b)
        self.third = self._stream("ORF 1 A2", self.a)
        self._attach(self.orf1, [self.first, self.second, self.third, self.fallback])

    def _stream(self, name, account):
        return Stream.objects.create(name=name, url=f"http://{account.name.replace(' ', '')}/{name.replace(' ', '')}", m3u_account=account)

    def _attach(self, channel, streams):
        for order, stream in enumerate(streams):
            ChannelStream.objects.create(channel=channel, stream=stream, order=order)

    def _order(self, channel):
        return list(ChannelStream.objects.filter(channel=channel).order_by("order").values_list("stream__name", flat=True))


class ParkTests(_Setup):
    def test_a_parked_stream_comes_off_its_channels_and_the_rest_close_up(self):
        stream_check.park(self.second.id, "The provider answered HTTP 404")
        self.assertEqual(self._order(self.orf1), ["ORF 1 A", "ORF 1 A2", "could not dispatch"])
        orders = list(ChannelStream.objects.filter(channel=self.orf1).order_by("order").values_list("order", flat=True))
        self.assertEqual(orders, [0, 1, 2])
        self.assertIn(self.second.id, stream_check.parked_ids())

    def test_put_back_where_it_was_before_the_fallback(self):
        stream_check.park(self.second.id)
        stream_check.restore(self.second.id)
        self.assertEqual(self._order(self.orf1), ["ORF 1 A", "ORF 1 B", "ORF 1 A2", "could not dispatch"])
        self.assertNotIn(self.second.id, stream_check.parked_ids())

    def test_put_back_before_the_fallback_even_when_the_channel_shrank(self):
        stream_check.park(self.third.id)
        ChannelStream.objects.filter(channel=self.orf1, stream=self.second).delete()
        stream_check.restore(self.third.id)
        self.assertEqual(self._order(self.orf1), ["ORF 1 A", "ORF 1 A2", "could not dispatch"])

    def test_a_channel_deleted_meanwhile_is_passed_over(self):
        other = Channel.objects.create(name="┃AT┃ ORF 1 copy", channel_number=2, channel_group=self.group)
        self._attach(other, [self.second])
        stream_check.park(self.second.id)
        other.delete()
        self.assertEqual(stream_check.restore(self.second.id), 1)

    def test_forgotten_it_stays_off(self):
        stream_check.park(self.second.id)
        stream_check.forget(self.second.id)
        self.assertNotIn("ORF 1 B", self._order(self.orf1))
        self.assertNotIn(self.second.id, stream_check.parked_ids())

    def test_removed_from_one_channel_only(self):
        other = Channel.objects.create(name="┃AT┃ ORF 1 copy", channel_number=2, channel_group=self.group)
        self._attach(other, [self.second])
        stream_check.remove(self.second.id, self.orf1.id)
        self.assertNotIn("ORF 1 B", self._order(self.orf1))
        self.assertEqual(self._order(other), ["ORF 1 B"])
        # The stream itself is the provider's and stays
        self.assertTrue(Stream.objects.filter(id=self.second.id).exists())

    def test_the_fallback_is_never_parked_or_removed(self):
        with self.assertRaises(ValueError):
            stream_check.park(self.fallback.id)
        with self.assertRaises(ValueError):
            stream_check.remove(self.fallback.id)
        self.assertIn("could not dispatch", self._order(self.orf1))

    def test_the_merge_leaves_parked_streams_alone(self):
        """It would otherwise put them straight back on the channel they came off."""
        orf = self._stream("┃AT┃ ORF 1 HD", self.b)
        stream_check.park(orf.id)
        plan = channel_manager.build_plan({**channel_manager.DEFAULTS})
        row = next(r for r in plan["rows"] if r["key"] == f"ch:{self.orf1.id}")
        self.assertNotIn("┃AT┃ ORF 1 HD", [s["name"] for s in row["streams"]])


def _answers(by_name):
    """A probe that answers from a table, by the stream's URL."""

    def fake(url, user_agent="", timeout=12, should_stop=lambda: False):
        outcome = by_name.get(url.rsplit("/", 1)[-1], True)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(should_stop)
        return {"ok": outcome, "reason": "" if outcome else "The provider answered HTTP 404",
                "resolution": "1920x1080" if outcome else "", "codec": "h264", "bytes": 1, "seconds": 0.1}

    return fake


class RunTests(_Setup):
    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()
        stream_check.save_settings({"gap_seconds": 0, "broken_after": 2})

    def _run(self, answers, only=None, batch_seconds=None):
        """A whole round, batch after batch, as the tasks chain them; (last ending, probe)."""
        with mock.patch.object(stream_check, "probe", side_effect=_answers(answers)) as probe:
            if only is None:
                stream_check.start_round(self.redis, force=True)
            for _ in range(20):
                ended = stream_check.run(self.redis, only=only, batch_seconds=batch_seconds)
                if ended not in ("more", "waiting"):
                    break
        return {"ended": ended, **stream_check.progress(self.redis)}, probe

    def test_every_stream_on_a_channel_is_looked_at_but_not_the_fallback(self):
        final, probe = self._run({"ORF1B": False})
        self.assertEqual(final["done"], 3)
        self.assertEqual(probe.call_count, 3)
        results = stream_check.load_results()["streams"]
        self.assertTrue(results[str(self.first.id)]["ok"])
        self.assertEqual(results[str(self.second.id)]["state"], "failing")
        self.assertNotIn(str(self.fallback.id), results)

    def test_broken_after_failing_runs_in_a_row(self):
        self._run({"ORF1B": False})
        self._run({"ORF1B": False})
        self.assertEqual(stream_check.load_results()["streams"][str(self.second.id)]["state"], "broken")
        self._run({"ORF1B": True})
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual((record["state"], record["failures"], record["history"][:3]), ("ok", 0, [1, 0, 0]))

    def test_every_connection_taken_is_given_back(self):
        self._run({"ORF1B": False})
        for account in (self.a, self.b):
            profile = account.profiles.get()
            self.assertEqual(int(self.redis.get(f"profile_connections:{profile.id}") or 0), 0)
        self.assertFalse(self.redis.exists(stream_check.RUN_KEY))

    def _watching(self, profile):
        """A viewer on a login, as the proxy leaves it in Redis."""
        self.redis.set("live:channel:abc:metadata", "1")
        self.redis.set("stream_profile:999", profile.id)
        self.redis.set(f"profile_connections:{profile.id}", 1)

    def _done_watching(self, profile):
        self.redis.delete("live:channel:abc:metadata", "stream_profile:999")
        self.redis.set(f"profile_connections:{profile.id}", 0)

    def test_a_provider_someone_is_using_waits_while_the_others_go_on(self):
        login = self.a.profiles.get()
        self._watching(login)
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "waiting")
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})
        # Not a single connection of the viewer's login was taken
        self.assertEqual(int(self.redis.get(f"profile_connections:{login.id}")), 1)
        self.assertEqual(stream_check.progress(self.redis)["accounts"][str(self.a.id)]["status"], "in use")

        # The round waits for it, and carries on once they are done
        self.assertTrue(stream_check.is_running(self.redis))
        self._done_watching(login)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "done")
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1A", "ORF1A2"})

    def test_another_login_of_the_same_provider_is_used_when_one_is_in_use(self):
        default = self.a.profiles.get()
        M3UAccountProfile.objects.create(m3u_account=self.a, name="second login", is_default=False, max_streams=1)
        self._watching(default)
        final, probe = self._run({})
        self.assertEqual(final["ended"], "done")
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(int(self.redis.get(f"profile_connections:{default.id}")), 1)

    def test_a_count_left_behind_with_nothing_playing_is_not_a_viewer(self):
        """A stream that ended badly can leave its count; trusting it would skip the login for good."""
        login = self.a.profiles.get()
        login.max_streams = 2
        login.save()
        self.redis.set(f"profile_connections:{login.id}", 1)
        final, probe = self._run({})
        self.assertEqual(probe.call_count, 3)

    def test_a_viewer_coming_onto_the_login_being_checked_with_stops_the_check(self):
        login = self.b.profiles.get()
        calls = {"n": 0}

        def viewer_arrives(should_stop):
            calls["n"] += 1
            if calls["n"] == 1:
                self._watching(login)
                time.sleep(0.6)  # past what the usage remembers
                self.assertTrue(should_stop())
                raise stream_check.Stopped()
            return {"ok": True, "reason": "", "resolution": "", "codec": "", "bytes": 1, "seconds": 0.1}

        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": viewer_arrives})):
            self.assertEqual(stream_check.run(self.redis), "waiting")
            self.assertNotIn(str(self.second.id), stream_check.load_results()["streams"])
            self._done_watching(login)
            self.assertEqual(stream_check.run(self.redis), "done")
        self.assertTrue(stream_check.load_results()["streams"][str(self.second.id)]["ok"])

    def _as_xc(self, account):
        account.account_type = "XC"
        account.username, account.password = "user", "pass"
        account.save()

    def test_a_provider_that_refuses_the_login_is_left_and_nothing_counts_against_it(self):
        self._as_xc(self.a)
        with mock.patch.object(stream_check, "_xc_user_info", return_value={"auth": 0}):
            final, probe = self._run({})
        self.assertEqual(final["ended"], "done")
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})
        results = stream_check.load_results()
        self.assertNotIn(str(self.first.id), results["streams"])
        self.assertEqual(results["last_run"]["unavailable"], [{"name": "Provider A", "reason": "the provider refused the login"}])

    def test_a_login_the_provider_says_is_active_is_used(self):
        self._as_xc(self.a)
        with mock.patch.object(stream_check, "_xc_user_info", return_value={"auth": 1, "status": "Active"}):
            final, probe = self._run({})
        self.assertEqual(probe.call_count, 3)

    def test_a_provider_that_does_not_answer_is_left(self):
        import requests

        self._as_xc(self.a)
        with mock.patch.object(stream_check, "_xc_user_info", side_effect=requests.exceptions.ConnectionError()):
            final, probe = self._run({})
        self.assertIn("did not answer", stream_check.load_results()["last_run"]["unavailable"][0]["reason"])

    def test_an_expired_login_is_left(self):
        from datetime import datetime, timedelta, timezone

        login = self.a.profiles.get()
        login.exp_date = datetime.now(timezone.utc) - timedelta(days=2)
        login.save()
        final, probe = self._run({})
        self.assertIn("expired", stream_check.load_results()["last_run"]["unavailable"][0]["reason"])

    def test_a_provider_whose_first_streams_all_fail_is_down_not_its_streams(self):
        stream_check.save_settings({"account_failures": 2})
        final, probe = self._run({"ORF1A": False, "ORF1A2": False})
        results = stream_check.load_results()
        self.assertEqual(results["last_run"]["unavailable"][0]["name"], "Provider A")
        for stream in (self.first, self.third):
            record = results["streams"][str(stream.id)]
            self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "unchecked")

    def test_failures_after_something_played_are_the_streams(self):
        stream_check.save_settings({"account_failures": 2})
        final, probe = self._run({"ORF1A": True, "ORF1A2": False})
        record = stream_check.load_results()["streams"][str(self.third.id)]
        self.assertEqual(record["state"], "failing")
        self.assertEqual(stream_check.load_results()["last_run"]["unavailable"], [])

    def test_a_round_goes_in_batches(self):
        """Each ends after its time, with the next queued; all of them look at every stream once."""
        final, probe = self._run({}, batch_seconds=0.001)
        self.assertEqual(final["ended"], "done")
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(final["done"], 3)
        self.assertFalse(stream_check.is_running(self.redis))

    def test_stopped_between_batches(self):
        stream_check.start_round(self.redis, force=True)
        self.assertTrue(stream_check.request_stop(self.redis))
        self.assertFalse(stream_check.is_running(self.redis))
        self.assertTrue(stream_check.load_results()["last_run"]["stopped"])

    def test_a_check_cut_short_for_a_viewer_does_not_count_against_the_stream(self):
        calls = {"n": 0}

        def cut_once(should_stop):
            calls["n"] += 1
            if calls["n"] == 1:
                raise stream_check.Stopped()
            return {"ok": True, "reason": "", "resolution": "", "codec": "", "bytes": 1, "seconds": 0.1}

        self._run({"ORF1B": cut_once})
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual((record["ok"], record["failures"]), (True, 0))

    def test_a_batch_already_going_is_not_started_twice(self):
        stream_check.start_round(self.redis, force=True)
        self.redis.set(stream_check.RUN_KEY, "1")
        self.assertEqual(stream_check.run(self.redis), "already running")
        self.assertIsNone(stream_check.start_round(self.redis, force=True))

    def test_a_parked_stream_is_looked_at_again_and_can_come_back_by_itself(self):
        stream_check.park(self.second.id)
        stream_check.save_settings({"restore_recovered": True})
        self._run({"ORF1B": True})
        self.assertIn("ORF 1 B", self._order(self.orf1))

    def test_one_stream_can_be_looked_at_alone(self):
        final, probe = self._run({}, only=[self.second.id])
        self.assertEqual(probe.call_count, 1)

    def test_the_page_shows_the_channel_with_every_stream(self):
        self._run({"ORF1B": False})
        self._run({"ORF1B": False})
        found = stream_check.issues(self.redis)
        (row,) = found["rows"]
        self.assertEqual((row["broken"], row["working"], row["dead"]), (1, 2, False))
        self.assertEqual([s["state"] for s in row["streams"]], ["ok", "broken", "ok", "fallback"])


class TickTests(_Setup):
    """The every-five-minutes task: what makes rounds happen by themselves."""

    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()
        patcher = mock.patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_off_it_does_nothing(self):
        from apps.channels.tasks import stream_check_tick

        with mock.patch.object(stream_check, "probe") as probe:
            self.assertEqual(stream_check_tick(), "off")
        probe.assert_not_called()
        self.assertFalse(stream_check.is_running(self.redis))

    def test_on_it_begins_a_round_and_queues_the_next_batch(self):
        from apps.channels.tasks import run_stream_check, stream_check_tick

        stream_check.save_settings({"enabled": True, "gap_seconds": 0})
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})), \
                mock.patch.object(stream_check, "BATCH_SECONDS", 0.001), \
                mock.patch.object(run_stream_check, "apply_async") as queued:
            self.assertEqual(stream_check_tick(), "more")
        queued.assert_called_once_with(countdown=1)

    def test_a_waiting_round_is_carried_on_even_when_turned_off(self):
        """One started from the page runs to its end whatever the setting."""
        from apps.channels.tasks import stream_check_tick

        stream_check.save_settings({"gap_seconds": 0})
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})):
            self.assertEqual(stream_check_tick(), "done")


class ViewTests(_Setup):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(user=User.objects.create_user(username="admin", password="x", user_level=10))
        self.redis = FakeRedis()
        patcher = mock.patch("apps.channels.stream_check_views.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_overview(self):
        data = self.api.get("/api/channels/stream-check/").json()
        self.assertEqual(data["rows"], [])
        self.assertFalse(data["settings"]["enabled"])
        self.assertIn("┃AT┃ AUSTRIA", [g["name"] for g in data["channel_groups"]])

    def test_park_and_put_back(self):
        response = self.api.post("/api/channels/stream-check/action/", {"action": "park", "stream_id": self.second.id}, format="json")
        self.assertEqual(response.json()["channels"], 1)
        parked = self.api.get("/api/channels/stream-check/").json()["parked"]
        self.assertEqual([p["name"] for p in parked], ["ORF 1 B"])
        self.assertEqual(parked[0]["from"][0]["name"], "┃AT┃ ORF 1")
        self.api.post("/api/channels/stream-check/action/", {"action": "restore", "stream_id": self.second.id}, format="json")
        self.assertIn("ORF 1 B", self._order(self.orf1))

    def test_the_fallback_cannot_be_removed_from_here(self):
        response = self.api.post("/api/channels/stream-check/action/", {"action": "remove", "stream_id": self.fallback.id}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_run_is_not_started_twice(self):
        self.redis.set(stream_check.RUN_KEY, "1")
        self.assertEqual(self.api.post("/api/channels/stream-check/run/", {}, format="json").status_code, 409)

    def test_a_run_is_started_in_the_background(self):
        with mock.patch("apps.channels.tasks.run_stream_check.delay") as delay:
            self.api.post("/api/channels/stream-check/run/", {"only": [self.second.id]}, format="json")
        delay.assert_called_once_with(only=[self.second.id])

    def test_check_all_begins_a_round(self):
        with mock.patch("apps.channels.tasks.run_stream_check.delay") as delay:
            response = self.api.post("/api/channels/stream-check/run/", {}, format="json")
        self.assertEqual(response.json()["streams"], 3)
        delay.assert_called_once_with()
        self.assertTrue(self.api.get("/api/channels/stream-check/").json()["running"])

    def test_bad_settings_are_refused(self):
        response = self.api.put("/api/channels/stream-check/settings/", {"settings": {"window_to": "nope"}}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(user=User.objects.create_user(username="v", password="x", user_level=0))
        self.assertEqual(viewer.get("/api/channels/stream-check/").status_code, 403)
