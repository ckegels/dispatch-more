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

    def zadd(self, key, mapping):
        with self.lock:
            self.data.setdefault(key, {}).update(mapping)

    def _scores(self, key, low, high):
        high = float("inf") if high == "+inf" else float(high)
        return sorted(
            (score, member) for member, score in (self.data.get(key) or {}).items() if float(low) <= score <= high
        )

    def zcount(self, key, low, high):
        return len(self._scores(key, low, high))

    def zrangebyscore(self, key, low, high, start=None, num=None, withscores=False):
        found = self._scores(key, low, high)
        if start is not None:
            found = found[start:start + num]
        return [(member, score) for score, member in found] if withscores else [member for _, member in found]

    def zremrangebyscore(self, key, low, high):
        for score, member in self._scores(key, low, high):
            self.data[key].pop(member, None)

    def scan_iter(self, match="*", count=None):
        return iter([key for key in list(self.data) if fnmatch.fnmatch(key, match)])


def _video(seconds=2, source="testsrc=size=320x240:rate=25"):
    """A few seconds of real MPEG-TS, made by ffmpeg: moving by default."""
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"{source}:duration={seconds}",
            "-c:v", "mpeg2video", "-f", "mpegts", "pipe:1",
        ],
        capture_output=True, check=True,
    ).stdout


class _Provider(http.server.BaseHTTPRequestHandler):
    """A provider: one path per way a stream can be."""

    video = b""
    long_video = b""
    black = b""
    still = b""
    quiet_then_moving = b""

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
        elif self.path == "/moving.ts":
            self._send(self.long_video)
        elif self.path == "/black.ts":
            self._send(self.black)
        elif self.path == "/still.ts":
            self._send(self.still)
        elif self.path == "/still.m3u8":
            self._send(b"#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXTINF:8,\nstill.ts\n", "application/vnd.apple.mpegurl")
        elif self.path == "/burst.ts":
            # All of it at once, then nothing for a long while: a provider's burst
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            self.wfile.write(self.long_video)
            self.wfile.flush()
            time.sleep(6)
        elif self.path == "/pauses.ts":
            # A second of video, then the data stops for longer than a read waits
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            self.wfile.write(self.long_video[: len(self.long_video) // 2])
            self.wfile.flush()
            time.sleep(1.5)
        elif self.path == "/quiet-then-moving.ts":
            self._send(self.quiet_then_moving)
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
        elif self.path == "/auth.ts":
            self._send(b"<html><body><h1>Proxy Authentication Required</h1></body></html>", "text/html", 407)
        elif self.path == "/full.m3u8":
            self._send(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nfull-variant.m3u8\n", "application/vnd.apple.mpegurl")
        elif self.path == "/full-variant.m3u8":
            self._send(b"too many connections", "text/plain", 458)
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
        _Provider.long_video = _video(8)
        _Provider.black = _video(8, "color=c=black:size=320x240:rate=25")
        _Provider.still = _video(8, "color=c=0x3050a0:size=320x240:rate=25")
        # Six seconds of a still scene -- a news desk, a slide -- and then it moves
        _Provider.quiet_then_moving = subprocess.run(
            ["ffmpeg", "-v", "error",
             "-f", "lavfi", "-i", "color=c=0x3050a0:size=320x240:rate=25:duration=6",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
             "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
             "-c:v", "mpeg2video", "-f", "mpegts", "pipe:1"],
            capture_output=True, check=True,
        ).stdout
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

    def test_a_pause_part_way_keeps_what_came(self):
        """What made WELT FHD 'could not connect': a pause while reading threw the video away."""
        with mock.patch.object(stream_check, "READ_PAUSE_SECONDS", 0.5):
            found = stream_check.probe(f"{self.base}/pauses.ts", timeout=12)
        self.assertTrue(found["ok"], found)

    def test_a_burst_then_a_pause_is_judged_at_once_not_waited_out(self):
        """What made checks slow: waiting out a provider's pause after a burst of ten seconds."""
        started = time.monotonic()
        found = stream_check.probe(f"{self.base}/burst.ts", timeout=12, picture_seconds=6)
        self.assertTrue(found["ok"], found)
        self.assertTrue(found.get("picture_looked"))
        self.assertLess(time.monotonic() - started, 4)

    def test_an_answer_with_nothing_after_it_is_a_stall_to_look_at_again(self):
        with mock.patch.object(stream_check, "READ_PAUSE_SECONDS", 0.5):
            found = stream_check.probe(f"{self.base}/silent.ts", timeout=12)
        self.assertFalse(found["ok"])
        self.assertTrue(found["transient"])
        self.assertIn("stopped sending", found["reason"])
        self.assertNotEqual(found["kind"], stream_check.UNREACHABLE)

    def test_a_few_black_seconds_in_a_long_look_are_a_fade_not_a_black_channel(self):
        """What flagged "Black picture (3 of 29 s)": three seconds anywhere, however long the look."""
        with mock.patch.object(stream_check, "_picture", return_value={
            "length": 29.0, "black": 3.0, "frozen": 0.0, "frame": "ab",
        }):
            found = stream_check.probe(f"{self.base}/moving.ts", timeout=12, picture_seconds=6)
        self.assertTrue(found["ok"], found)
        with mock.patch.object(stream_check, "_picture", return_value={
            "length": 29.0, "black": 27.0, "frozen": 27.0, "frame": "ab",
        }):
            found = stream_check.probe(f"{self.base}/moving.ts", timeout=12, picture_seconds=6)
        self.assertEqual(found["kind"], stream_check.BLACK)

    def test_a_moving_picture_plays(self):
        found = stream_check.probe(f"{self.base}/moving.ts", timeout=12, picture_seconds=6)
        self.assertTrue(found["ok"], found)

    def test_a_black_picture_is_its_own_failure(self):
        found = stream_check.probe(f"{self.base}/black.ts", timeout=12, picture_seconds=6)
        self.assertFalse(found["ok"])
        self.assertEqual(found["kind"], stream_check.BLACK)
        self.assertIn("Black picture", found["reason"])

    def test_a_picture_that_does_not_move_is_its_own_failure(self):
        found = stream_check.probe(f"{self.base}/still.ts", timeout=12, picture_seconds=6)
        self.assertFalse(found["ok"])
        self.assertEqual(found["kind"], stream_check.FROZEN)
        self.assertTrue(found["frozen_frame"])

    def test_a_still_picture_is_watched_longer_before_it_is_called_frozen(self):
        found = stream_check.probe(f"{self.base}/still.ts", timeout=12, picture_seconds=6, frozen_confirm_seconds=6)
        self.assertEqual(found["kind"], stream_check.FROZEN)
        self.assertIn("watched to be sure", found["reason"])

    def test_a_still_hls_stream_is_watched_longer_segment_by_segment(self):
        found = stream_check.probe(f"{self.base}/still.m3u8", timeout=12, picture_seconds=6, frozen_confirm_seconds=6)
        self.assertEqual(found["kind"], stream_check.FROZEN)

    def test_a_picture_still_for_a_while_and_then_moving_plays(self):
        """What false frozen pictures were: a scene that is quiet for the first seconds looked at."""
        found = stream_check.probe(f"{self.base}/quiet-then-moving.ts", timeout=12, picture_seconds=6, frozen_confirm_seconds=6)
        self.assertTrue(found["ok"], found)

    def test_without_the_picture_check_a_still_picture_plays(self):
        self.assertTrue(stream_check.probe(f"{self.base}/still.ts", timeout=12)["ok"])

    def test_an_error_page_is_told_by_its_title(self):
        page = (b'<!DOCTYPE html><html><head><title>one-zone.cc | 502: Bad gateway</title>'
                b'<meta charset="UTF-8" /><meta http-equiv="X-UA-Compatible" content="IE=E')
        self.assertEqual(stream_check._said(page), "one-zone.cc | 502: Bad gateway")
        self.assertEqual(stream_check._said(b"Forbidden <b>here</b> <meta a="), "Forbidden here")

    def test_a_connection_error_is_told_in_words_without_the_login_in_it(self):
        import requests

        cases = {
            "Temporary failure in name resolution": "could not be looked up",
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))":
                "closed it without answering",
            "Connection reset by peer": "cut the connection off",
        }
        for text, words in cases.items():
            error = requests.exceptions.ConnectionError(f"HTTPConnectionPool: /live/user/secret/1.ts ({text})")
            said = stream_check._why_no_connection(error)
            self.assertIn(words, said)
            self.assertNotIn("secret", said)

    def test_no_connection_is_unreachable_not_dead(self):
        import socket

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        found = stream_check.probe(f"http://127.0.0.1:{port}/live.ts", timeout=5)
        self.assertFalse(found["ok"])
        self.assertEqual(found["kind"], stream_check.UNREACHABLE)
        # Which way it failed, in words, and never the address
        self.assertIn("refused the connection", found["reason"])
        self.assertNotIn("127.0.0.1", found["reason"])

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

    def test_what_a_provider_says_with_an_error_is_kept(self):
        found = stream_check.probe(f"{self.base}/auth.ts", timeout=5)
        self.assertTrue(found["refused"])
        self.assertEqual(found["reason"], "The provider answered HTTP 407: Proxy Authentication Required")

    def test_a_playlist_whose_provider_is_full_is_a_refusal_not_a_failure(self):
        found = stream_check.probe(f"{self.base}/full.m3u8", timeout=5)
        self.assertFalse(found["ok"])
        self.assertTrue(found["refused"])
        self.assertIn("458", found["reason"])

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


class LettingGoTests(TestCase):
    """
    How quickly a check that is reading notices it has been asked to let go. A viewer
    starting a channel waits three seconds for a connection and then goes to the fallback
    stream, so a check that takes ten to notice has already cost them their channel --
    which is what happened.
    """

    def _pieces(self, *put):
        import queue

        held = queue.Queue()
        for one in put:
            held.put(one)
        return held

    def test_a_piece_that_is_there_comes_straight_back(self):
        self.assertEqual(
            stream_check._next_piece(self._pieces(b"video"), 10, lambda: False), b"video"
        )

    def test_nothing_in_the_time_given_is_still_nothing(self):
        import queue

        started = time.monotonic()
        with self.assertRaises(queue.Empty):
            stream_check._next_piece(self._pieces(), 0.5, lambda: False)
        # The wait itself is unchanged: it waits the time it was given
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_but_being_asked_to_let_go_is_noticed_at_once(self):
        # The wait is ten seconds, as it is for a provider that sends in bursts; the
        # check must still let go inside a fraction of one
        asked = {"at": time.monotonic() + 0.3}
        started = time.monotonic()
        with self.assertRaises(stream_check.Stopped):
            stream_check._next_piece(
                self._pieces(), 10, lambda: time.monotonic() >= asked["at"]
            )
        self.assertLess(time.monotonic() - started, 1.0)

    def test_and_a_read_with_nothing_coming_lets_go_too(self):
        asked = {"yet": False}

        def should_stop():
            was, asked["yet"] = asked["yet"], True
            return was

        started = time.monotonic()
        with self.assertRaises(stream_check.Stopped):
            stream_check._next_piece(self._pieces(), 10, should_stop)
        self.assertLess(time.monotonic() - started, 1.0)


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


class PlaylistTests(_Setup):
    """
    A stream its provider has stopped listing. Dispatcharr marks those on every refresh
    (Stream.is_stale) and deletes them after the account's stale days; the check can read
    the same mark and spend no connection on them.
    """

    def _stale(self, *streams):
        for stream in streams:
            stream.is_stale = True
            stream.save(update_fields=["is_stale"])

    def _more_of(self, account, how_many):
        """Streams of the account that are still listed, so it is not a failed refresh."""
        return [self._stream(f"filler {account.id} {n}", account) for n in range(how_many)]

    def test_a_stream_the_provider_dropped_is_the_providers_own_word(self):
        self._more_of(self.a, 10)
        self._stale(self.first)
        self.assertEqual(
            stream_check.unlisted_in(self.a.id, [self.first, self.third]), {self.first.id}
        )

    def test_an_account_nobody_ever_refreshed_says_nothing(self):
        # Nothing marked, because nothing has ever looked
        self.assertEqual(
            stream_check.unlisted_in(self.a.id, [self.first, self.third]), set()
        )

    def test_a_refresh_that_went_wrong_is_not_a_provider_dropping_everything(self):
        # A provider drops channels a few at a time; one that dropped its whole playlist
        # overnight is a refresh that failed, and acting on it would empty every channel
        others = self._more_of(self.a, 8)
        self._stale(self.first, self.third, *others)
        self.assertEqual(
            stream_check.unlisted_in(self.a.id, [self.first, self.third]), set()
        )

    def test_it_is_broken_at_once_and_not_failing(self):
        # Not a stream that failed once and may pass next time
        record = {"ok": False, "kind": stream_check.UNLISTED, "failures": 1}
        self.assertEqual(stream_check.state_of(record, {"broken_after": 3}), "broken")

    def test_and_it_is_as_sure_as_this_gets(self):
        score, why = stream_check.confidence_of(
            {"ok": False, "kind": stream_check.UNLISTED, "failures": 1, "history": [0]}
        )
        self.assertGreaterEqual(score, 75)
        self.assertIn("does not list this stream any more", " ".join(why))
        # ...and "it may never have been right" says nothing about a stream since dropped
        self.assertNotIn("never been seen working", " ".join(why))


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

    def _hidden(self, channel):
        channel.refresh_from_db()
        return channel.hidden_from_output

    def test_a_channel_with_every_real_stream_parked_is_hidden_the_fallback_not_counting(self):
        for stream in (self.first, self.second):
            stream_check.park(stream.id)
        self.assertFalse(self._hidden(self.orf1))
        stream_check.park(self.third.id)
        self.assertTrue(self._hidden(self.orf1))
        self.assertEqual(self._order(self.orf1), ["could not dispatch"])
        self.assertIn(str(self.orf1.id), stream_check.hidden_channels())

    def test_shown_again_when_a_stream_of_it_is_put_back(self):
        for stream in (self.first, self.second, self.third):
            stream_check.park(stream.id)
        stream_check.restore(self.second.id)
        self.assertFalse(self._hidden(self.orf1))
        self.assertEqual(stream_check.hidden_channels(), {})

    def test_hiding_keeps_the_channel_number(self):
        """A save would set off stock's compact numbering, which gives a hidden channel's number away."""
        for stream in (self.first, self.second, self.third):
            stream_check.park(stream.id)
        self.orf1.refresh_from_db()
        self.assertEqual(self.orf1.channel_number, 1)

    def test_a_channel_a_person_hid_is_never_shown_again_by_this(self):
        Channel.objects.filter(id=self.orf1.id).update(hidden_from_output=True)
        for stream in (self.first, self.second, self.third):
            stream_check.park(stream.id)
        self.assertEqual(stream_check.hidden_channels(), {})
        stream_check.restore(self.first.id)
        self.assertTrue(self._hidden(self.orf1))

    def test_with_hiding_off_the_channel_stays(self):
        stream_check.save_settings({"hide_emptied_channels": False})
        for stream in (self.first, self.second, self.third):
            stream_check.park(stream.id)
        self.assertFalse(self._hidden(self.orf1))

    def test_the_page_lists_the_channels_hidden_this_way(self):
        from unittest import mock as _mock

        for stream in (self.first, self.second, self.third):
            stream_check.park(stream.id)
        redis = _mock.MagicMock()
        redis.hgetall.return_value = {}
        found = stream_check.issues(redis)
        self.assertEqual([c["name"] for c in found["hidden_channels"]], ["┃AT┃ ORF 1"])

    def test_the_merge_leaves_parked_streams_alone(self):
        """It would otherwise put them straight back on the channel they came off."""
        orf = self._stream("┃AT┃ ORF 1 HD", self.b)
        stream_check.park(orf.id)
        plan = channel_manager.build_plan({**channel_manager.DEFAULTS})
        row = next(r for r in plan["rows"] if r["key"] == f"ch:{self.orf1.id}")
        self.assertNotIn("┃AT┃ ORF 1 HD", [s["name"] for s in row["streams"]])


def _answers(by_name):
    """A probe that answers from a table, by the stream's URL."""

    def fake(url, user_agent="", timeout=12, should_stop=lambda: False, picture_seconds=0, frozen_confirm_seconds=0):
        outcome = by_name.get(url.rsplit("/", 1)[-1], True)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(should_stop)
        return {"ok": outcome, "reason": "" if outcome else "The provider answered HTTP 404",
                "resolution": "1920x1080" if outcome else "", "codec": "h264", "bytes": 1, "seconds": 0.1}

    return fake


@mock.patch.object(stream_check, "REFUSAL_PAUSE", 0.01)
@mock.patch.object(stream_check, "PROVIDER_ASK_EVERY", 0.01)
@mock.patch.object(stream_check, "PROVIDER_LINGER", 0.2)
class RunTests(_Setup):
    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()
        # Most of these are about checking alongside viewers, which is not the default
        stream_check.save_settings({"gap_seconds": 0, "broken_after": 2, "only_when_idle": False})

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

    def test_how_sure_it_is_and_why(self):
        """
        A number that can be read back as the sentences it was made of. Nothing short of
        failing run after run gets near a hundred: the one thing this must never do is
        call a working channel broken.
        """
        one_look = {"ok": False, "kind": "black", "failures": 1, "history": [0], "last_ok": "y"}
        sure, why = stream_check.confidence_of(one_look)
        self.assertLess(sure, 20)
        self.assertTrue(any("fade" in one for one in why))

        # ...the same fault, seen again later in the run and again the next run
        twice = {**one_look, "failures": 2, "confirmed": True, "history": [0, 0]}
        surer, why = stream_check.confidence_of(twice)
        self.assertGreater(surer, sure + 30)
        self.assertTrue(any("looked at again" in one for one in why))

        # ...and the same channel playing from somewhere else says it is this stream
        with_sibling, why = stream_check.confidence_of(twice, [{"ok": True}])
        self.assertGreater(with_sibling, surer)
        self.assertTrue(any("another provider" in one for one in why))

    def test_a_stream_gone_from_the_playlist_costs_no_connection(self):
        """
        The provider has already said it is gone, in its own playlist. Opening it would
        take a connection from a viewer and could learn nothing better.
        """
        # Enough still listed that this is a provider dropping one, not a failed refresh
        for n in range(10):
            self._stream(f"still there {n}", self.a)
        self.first.is_stale = True
        self.first.save(update_fields=["is_stale"])

        found, probe = self._run({"*": {"ok": True}})

        looked_at = [call.args[0] for call in probe.call_args_list]
        self.assertNotIn(self.first.url, looked_at)
        # ...and the others were looked at as usual
        self.assertIn(self.second.url, looked_at)

        record = stream_check.load_results()["streams"][str(self.first.id)]
        self.assertEqual(record["kind"], stream_check.UNLISTED)
        self.assertEqual(record["state"], "broken")

    def test_unless_the_playlist_is_not_to_be_believed(self):
        stream_check.save_settings({"trust_the_playlist": False})
        for n in range(10):
            self._stream(f"still there {n}", self.a)
        self.first.is_stale = True
        self.first.save(update_fields=["is_stale"])

        _, probe = self._run({"*": {"ok": True}})

        self.assertIn(
            self.first.url, [call.args[0] for call in probe.call_args_list]
        )

    def test_nothing_that_is_still_being_judged_has_a_confidence(self):
        for record in (
            {"ok": True, "failures": 0},
            {"ok": False, "skipped": True},
            {"ok": False, "suspect": {"kind": "black"}},
            {},
        ):
            self.assertEqual(stream_check.confidence_of(record), (0, []))

    def test_a_stream_never_seen_working_is_less_sure_not_more(self):
        # It may never have been right, which is not the same as having stopped working
        failing = {"ok": False, "kind": "dead", "failures": 3, "history": [0, 0, 0]}
        seen_working, _ = stream_check.confidence_of({**failing, "last_ok": "yesterday"})
        never, _ = stream_check.confidence_of(failing)
        self.assertLess(never, seen_working)

    def test_the_same_channel_from_another_provider_goes_next(self):
        """
        A channel with one copy broken is either a channel that is gone everywhere or one
        provider being bad, and which of those it is decides what you do about it. So the
        other copies jump the queue rather than waiting for their provider's turn to come
        round -- which is what left one stream saying broken and its sibling untested.
        """
        left = [self.first, self.second, self.third]
        urgent = set()
        # Nothing urgent: in the order they came
        self.assertIs(stream_check._pick_next(left, urgent), self.first)
        # The third is a copy of a channel that has just failed elsewhere
        urgent.add(self.third.id)
        self.assertIs(stream_check._pick_next(left, urgent), self.third)
        # ...and once taken it is not urgent any more, so the order goes back to normal
        self.assertEqual(urgent, set())
        self.assertIs(stream_check._pick_next(left, urgent), self.first)
        self.assertIsNone(stream_check._pick_next([], urgent))

    def test_which_streams_are_a_channels_other_copies(self):
        by_provider = {"a": [self.first, self.third], "b": [self.second]}
        siblings = stream_check._siblings_of(by_provider)
        # The fallback is on the channel too, and is nobody's copy of anything, but it is
        # not being looked at so it is not in the map to begin with
        self.assertEqual(siblings[self.first.id], {self.second.id, self.third.id, self.fallback.id})
        self.assertEqual(siblings[self.second.id], {self.first.id, self.third.id, self.fallback.id})

    def test_nothing_is_parked_or_removed_until_you_say_how_sure_is_sure_enough(self):
        self.assertEqual(stream_check.DEFAULTS["park_above"], 0)
        self.assertEqual(stream_check.DEFAULTS["remove_above"], 0)
        for _ in range(4):
            self._run({"ORF1B": False})
        self.assertEqual(stream_check.load_parked(), {})
        self.assertIn("ORF 1 B", self._order(self.orf1))

    def test_a_stream_it_is_sure_enough_about_is_parked(self):
        stream_check.save_settings({"park_above": 40})
        for _ in range(3):
            self._run({"ORF1B": False})
        parked = stream_check.load_parked()
        self.assertIn(str(self.second.id), parked)
        self.assertIn("% sure", parked[str(self.second.id)]["reason"])
        self.assertNotIn("ORF 1 B", self._order(self.orf1))

    def test_and_removed_only_above_a_bar_of_its_own(self):
        # Parking is the kind one and comes back by itself; removing does not, so it is
        # asked for separately and wants a higher bar
        stream_check.save_settings({"remove_above": 40, "park_above": 90})
        for _ in range(3):
            self._run({"ORF1B": False})
        self.assertEqual(stream_check.load_parked(), {})
        self.assertNotIn("ORF 1 B", self._order(self.orf1))

    def test_a_stream_that_plays_is_never_acted_on_however_low_the_bar(self):
        stream_check.save_settings({"remove_above": 1, "park_above": 1})
        for _ in range(3):
            self._run({})
        self.assertEqual(stream_check.load_parked(), {})
        self.assertEqual(
            self._order(self.orf1), ["ORF 1 A", "ORF 1 B", "ORF 1 A2", "could not dispatch"]
        )

    def test_a_failure_puts_the_other_copies_at_the_front(self):
        # Through a whole run: the copy that failed and its siblings all end up checked
        final, probe = self._run({"ORF1A": False})
        self.assertEqual(final["done"], 3)
        results = stream_check.load_results()["streams"]
        self.assertEqual(results[str(self.first.id)]["state"], "failing")
        self.assertTrue(results[str(self.second.id)]["ok"])
        self.assertTrue(results[str(self.third.id)]["ok"])

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

    def _status(self, name):
        """How a provider is getting on, by the name the page shows."""
        return next(e for e in stream_check.progress(self.redis)["accounts"].values() if name in e["name"])

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
        self.assertEqual(self._status("Provider A")["status"], "in use")

        # The round waits for it, and carries on once they are done
        self.assertTrue(stream_check.is_running(self.redis))
        self._done_watching(login)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "done")
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1A", "ORF1A2"})

    def test_a_provider_someone_watches_through_one_login_is_left_alone_on_every_login(self):
        default = self.a.profiles.get()
        M3UAccountProfile.objects.create(m3u_account=self.a, name="second login", is_default=False, max_streams=1)
        self._watching(default)
        final, probe = self._run({})
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})

    def test_two_accounts_on_one_server_are_one_provider(self):
        """A viewer on either leaves both alone, and they are never checked at the same time."""
        twin = M3UAccount.objects.create(name="Provider A backup", account_type="STD", server_url="http://a:8080/x", is_active=True)
        M3UAccountProfile.objects.filter(m3u_account=twin).delete()
        twin_login = M3UAccountProfile.objects.create(m3u_account=twin, name="default", is_default=True, max_streams=1)
        self._attach(Channel.objects.create(name="┃AT┃ ORF 3", channel_number=3, channel_group=self.group), [self._stream("ORF 3 A", twin)])
        self._watching(twin_login)
        final, probe = self._run({})
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})

        self._done_watching(twin_login)
        at_once, most = {"now": 0}, {"n": 0}

        def one_at_a_time(should_stop):
            at_once["now"] += 1
            most["n"] = max(most["n"], at_once["now"])
            time.sleep(0.05)
            at_once["now"] -= 1
            return {"ok": True, "reason": "", "resolution": "", "codec": "", "bytes": 1, "seconds": 0.1}

        stream_check.clear_results()
        self._run({"ORF1A": one_at_a_time, "ORF1A2": one_at_a_time, "ORF3A": one_at_a_time})
        self.assertEqual(most["n"], 1)

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

    def test_by_default_nothing_is_checked_while_anything_plays(self):
        """On any provider: a provider can know logins Dispatcharr thinks are separate."""
        stream_check.save_settings({"only_when_idle": True})
        self._watching(self.b.profiles.get())
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "waiting")
        probe.assert_not_called()

    def test_by_default_a_viewer_starting_anywhere_stops_the_check(self):
        stream_check.save_settings({"only_when_idle": True})
        other = M3UAccount.objects.create(name="Provider C", account_type="STD", server_url="http://c", is_active=True)
        M3UAccountProfile.objects.filter(m3u_account=other).delete()
        elsewhere = M3UAccountProfile.objects.create(m3u_account=other, name="default", is_default=True, max_streams=1)

        def viewer_starts_elsewhere(should_stop):
            self._watching(elsewhere)
            time.sleep(0.6)
            if should_stop():
                raise stream_check.Stopped()
            return {"ok": True, "reason": "", "resolution": "", "codec": "", "bytes": 1, "seconds": 0.1}

        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": viewer_starts_elsewhere})):
            self.assertEqual(stream_check.run(self.redis), "waiting")
        self.assertNotIn(str(self.second.id), stream_check.load_results()["streams"])

    def test_one_login_under_two_accounts_is_one_login(self):
        """A viewer on it through either account is a viewer on it: the check must not join them."""
        self._as_xc(self.a)
        twin = M3UAccount.objects.create(
            name="Provider A (VOD)", account_type="XC", server_url="http://ProviderA", username="user",
            password="pass", is_active=True,
        )
        M3UAccountProfile.objects.filter(m3u_account=twin).delete()
        twin_login = M3UAccountProfile.objects.create(m3u_account=twin, name="default", is_default=True, max_streams=1)
        self._watching(twin_login)
        with mock.patch.object(stream_check, "_xc_user_info", return_value={"auth": 1, "status": "Active", "active_cons": 0}):
            final, probe = self._run({})
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})

    def test_a_login_the_provider_says_is_in_use_is_not_checked_with(self):
        """Someone on it in another app: only the provider can see them."""
        self._as_xc(self.a)
        stream_check.start_round(self.redis, force=True)
        busy = {"auth": 1, "status": "Active", "active_cons": "1", "max_connections": "2"}
        with mock.patch.object(stream_check, "_xc_user_info", return_value=busy), \
                mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "waiting")
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})
        self.assertEqual(self._status("Provider A")["status"], "in use")

    def test_the_provider_is_not_asked_before_every_stream(self):
        """Asking is a request too: a provider limiting requests per minute counts them."""
        self._as_xc(self.a)
        asked = []

        def provider(account, profile, agent):
            asked.append(1)
            return {"auth": 1, "status": "Active", "active_cons": 0}

        with mock.patch.object(stream_check, "PROVIDER_ASK_EVERY", 30), \
                mock.patch.object(stream_check, "_xc_user_info", side_effect=provider):
            final, probe = self._run({})
        self.assertEqual(probe.call_count, 3)
        # Once to see the login works, once for how busy it is: not once per stream more
        self.assertEqual(len(asked), 2)

    def test_the_check_just_closed_is_waited_out_not_taken_for_a_viewer(self):
        """A provider can go on counting a closed connection for a while."""
        self._as_xc(self.a)
        answers = iter([0] + [1, 1, 0] + [0] * 20)

        def provider(account, profile, agent):
            return {"auth": 1, "status": "Active", "active_cons": next(answers)}

        with mock.patch.object(stream_check, "_xc_user_info", side_effect=provider):
            final, probe = self._run({})
        self.assertEqual(final["ended"], "done")
        self.assertEqual(probe.call_count, 3)

    @staticmethod
    def _refusal(should_stop=None):
        return {"ok": False, "reason": "The provider answered HTTP 407: Proxy Authentication Required",
                "refused": True, "resolution": "", "codec": "", "bytes": 0, "seconds": 0.1}

    @staticmethod
    def _plays(should_stop=None):
        return {"ok": True, "reason": "", "resolution": "", "codec": "", "bytes": 1, "seconds": 0.1}

    def test_an_xtream_stream_is_opened_the_way_the_proxy_opens_it(self):
        """With the login as it is now, not the address kept from the last refresh."""
        self._as_xc(self.a)
        self.first.url = "http://old-address/live/olduser/oldpass/1.ts"
        self.first.stream_id = 1234
        self.first.save()
        login = self.a.profiles.get()
        url = stream_check._url_for(Stream.objects.select_related("m3u_account").get(id=self.first.id), login)
        self.assertIn("/live/user/pass/1234.ts", url)
        self.assertNotIn("olduser", url)

    def test_the_saved_off_from_before_goes_back_to_on(self):
        """
        Checking only while nothing plays is the default again (version 4), after a viewer
        changing channel onto a provider a check was using lost their channel. A setting
        saved before that takes the new default; what else was saved is kept.
        """
        stream_check._store(stream_check.SETTINGS_KEY, "Stream Check", {"only_when_idle": False, "every_hours": 12, "gap_seconds": 1})
        loaded = stream_check.load_settings()
        self.assertTrue(loaded["only_when_idle"])
        self.assertEqual(loaded["gap_seconds"], 3)
        self.assertEqual(loaded["every_hours"], 12)
        # ...and choosing it for yourself is kept
        stream_check.save_settings({"only_when_idle": False})
        self.assertFalse(stream_check.load_settings()["only_when_idle"])

    def _limit(self):
        return stream_check.load_limits().get("server:a", {})

    def test_a_provider_that_stops_giving_streams_has_its_limit_learned_and_rests(self):
        """Refused after a few, whatever the stream: the provider's limit, not the streams'."""
        opened = []

        def two_then_refused(should_stop):
            opened.append(1)
            return self._plays() if len(opened) <= 1 else self._refusal()

        final, probe = self._run({"ORF1A": two_then_refused, "ORF1A2": two_then_refused})
        limit = self._limit()
        self.assertTrue(limit["blocked_since"])
        self.assertIn("hit its limit after", self._status("Provider A")["reason"])
        # The refused stream is not counted against, and nothing else was tried while resting
        self.assertNotIn(str(self.third.id), stream_check.load_results()["streams"])
        self.assertTrue(stream_check.is_running(self.redis))
        self.assertGreaterEqual(stream_check.progress(self.redis)["resume_in"], stream_check.RETRY_WAITING)

    def test_once_it_answers_again_the_limit_is_kept_with_room_to_spare(self):
        state = {"blocked_since": time.time() - 600, "count": 10, "span": 120, "step": 3,
                 "next_try": time.time() - 1, "good_stream": self.first.id, "v": stream_check.LIMITS_VERSION}
        stream_check._change_limits(lambda limits: limits.update({"server:a": {**state, "name": "Provider A"}}))
        final, probe = self._run({})
        limit = self._limit()
        self.assertNotIn("blocked_since", limit)
        self.assertEqual(limit["limit"], 8)
        self.assertGreaterEqual(limit["window"], 720)
        self.assertEqual(final["ended"], "done")

    def test_a_resting_provider_is_not_asked_anything(self):
        self._as_xc(self.a)
        state = {"blocked_since": time.time(), "count": 34, "span": 120, "step": 0, "next_try": time.time() + 300,
                 "v": stream_check.LIMITS_VERSION}
        stream_check._change_limits(lambda limits: limits.update({"server:a": {**state, "name": "Provider A"}}))
        with mock.patch.object(stream_check, "_xc_user_info") as asked:
            final, probe = self._run({})
        asked.assert_not_called()
        self.assertEqual({c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list}, {"ORF1B"})
        self.assertEqual(self._status("Provider A")["status"], "resting")

    def test_one_stream_the_provider_will_not_give_is_told_from_a_limit(self):
        """A stream that just played there still plays: it is that one stream, not the provider."""
        stream_check._change_limits(lambda limits: limits.update({"server:a": {"name": "Provider A", "good_stream": self.first.id}}))
        final, probe = self._run({"ORF1A2": self._refusal})
        record = stream_check.load_results()["streams"][str(self.third.id)]
        # A failure of the stream like any other, so rechecks and autopark see it
        self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "failing")
        self.assertIn("while the provider plays its other streams", record["reason"])
        self.assertNotIn("blocked_since", self._limit())
        self.assertEqual(final["ended"], "done")

    def test_a_dead_channel_refused_first_is_told_from_a_limit_by_the_next_stream(self):
        """What hid a dead Euronews: nothing played there yet, and the next stream decides."""
        final, probe = self._run({"ORF1A": self._refusal})
        record = stream_check.load_results()["streams"][str(self.first.id)]
        self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "failing")
        self.assertTrue(stream_check.load_results()["streams"][str(self.third.id)]["ok"])
        self.assertNotIn("limit", self._limit())
        self.assertEqual(final["ended"], "done")
        # The next stream was looked at once, not again
        called = [c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list]
        self.assertEqual(called.count("ORF1A2"), 1)

    def test_a_limit_learned_before_this_is_not_used(self):
        """It may have been nothing but a dead channel."""
        stream_check._change_limits(lambda limits: limits.update({"server:a": {
            "name": "Provider A", "limit": 1, "window": 3600, "how": "learned", "good_stream": self.first.id,
        }}))
        self.assertEqual(self._limit(), {"name": "Provider A", "good_stream": self.first.id})
        final, probe = self._run({})
        self.assertEqual(probe.call_count, 3)

    def test_a_limit_set_by_hand_is_kept_to(self):
        stream_check.set_limit("server:a", "Provider A", limit=1, window_minutes=10)
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "waiting")
        called = [c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list]
        self.assertEqual(called.count("ORF1A") + called.count("ORF1A2"), 1)
        self.assertIn("allows 1 streams every 10 min (set by hand)", self._status("Provider A")["reason"])

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


class ChainTests(_Setup):
    """Batches follow one another: never two chains, and a waiting round is tried again soon."""

    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()
        patcher = mock.patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        stream_check.save_settings({"gap_seconds": 0})

    def test_make_way_says_whether_a_run_was_going(self):
        """
        The viewer's path needs the answer, not just the signal. A provider whose only
        connection a check is holding refuses however it likes -- and the stock test that
        decides whether to wait only recognises one wording, so without this a viewer was
        told no, with no retry, while the connection it needed was about to be free.
        """
        self.assertFalse(stream_check.make_way(self.redis))
        self.redis.set(stream_check.RUN_KEY, "1")
        self.assertTrue(stream_check.make_way(self.redis))
        self.assertTrue(self.redis.exists(stream_check.YIELD_KEY))

    def test_a_round_waits_for_everything_to_stop_by_default(self):
        from apps.channels.tasks import run_stream_check

        stream_check.start_round(self.redis, force=True)
        self.redis.set("live:channel:abc:metadata", "1")
        with mock.patch.object(run_stream_check, "apply_async"), \
                mock.patch.object(stream_check, "probe") as probe:
            self.assertEqual(run_stream_check(), "waiting")
        probe.assert_not_called()
        self.assertIn("nothing is playing", stream_check.progress(self.redis)["message"])

    def test_a_waiting_round_is_tried_again_in_a_minute_once(self):
        from apps.channels.tasks import run_stream_check, stream_check_tick

        # With checks allowed beside viewers, the wait is per provider instead
        stream_check.save_settings({"only_when_idle": False})
        stream_check.start_round(self.redis, force=True)
        self.redis.set("live:channel:abc:metadata", "1")
        self.redis.set("stream_profile:1", self.a.profiles.get().id)
        self.redis.set("stream_profile:2", self.b.profiles.get().id)
        with mock.patch.object(run_stream_check, "apply_async") as queued, \
                mock.patch.object(stream_check, "probe") as probe:
            self.assertEqual(run_stream_check(), "waiting")
            queued.assert_called_once_with(countdown=stream_check.RETRY_WAITING)
            # The tick sees the next batch is queued and does not start another
            self.assertEqual(stream_check_tick(), "the next batch is queued")
        probe.assert_not_called()
        self.assertTrue(stream_check.progress(self.redis)["next_batch_at"])
        self.assertIn("someone is watching through it", stream_check.progress(self.redis)["message"])

    def test_a_stream_checked_by_hand_right_after_stop_is_checked(self):
        """Stop ends a round; the stop signal left set must not end a check asked for by hand."""
        from apps.channels.tasks import run_stream_check

        self.redis.set(stream_check.STOP_KEY, "1")
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe, \
                mock.patch.object(run_stream_check, "apply_async"):
            run_stream_check(only=[self.first.id])
        self.assertEqual(probe.call_count, 1)

    def test_a_check_by_hand_waits_its_turn_behind_a_batch(self):
        from apps.channels.tasks import run_stream_check

        self.redis.set(stream_check.RUN_KEY, "1")
        with mock.patch.object(run_stream_check, "apply_async") as queued:
            self.assertEqual(run_stream_check(only=[self.first.id]), "already running")
        queued.assert_called_once_with(kwargs={"only": [self.first.id], "looks": 0, "waits": 1}, countdown=5)

    def test_a_full_batch_queues_the_next_at_once(self):
        from apps.channels.tasks import run_stream_check

        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "BATCH_SECONDS", 0.001), \
                mock.patch.object(stream_check, "probe", side_effect=_answers({})), \
                mock.patch.object(run_stream_check, "apply_async") as queued:
            self.assertEqual(run_stream_check(), "more")
        queued.assert_called_once_with(countdown=1)

    def test_a_stopped_round_leaves_nothing_queued(self):
        stream_check.start_round(self.redis, force=True)
        self.redis.set(stream_check.QUEUED_KEY, "1")
        stream_check.request_stop(self.redis)
        self.assertFalse(self.redis.exists(stream_check.QUEUED_KEY))


class RecheckTests(_Setup):
    """A failing stream looked at again by itself, and parked by autopark if it stays dead."""

    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()
        patcher = mock.patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        stream_check.save_settings({"enabled": True, "gap_seconds": 0})

    def _results(self, **records):
        stream_check._store(stream_check.RESULTS_KEY, "x", {
            "streams": records,
            # A full run just finished, so only rechecks can be due
            "last_run": {"finished_at": stream_check._now(), "checked": 3, "total": 3},
        })

    def _ago(self, hours):
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="milliseconds")

    def _failed(self, hours_ago, failures=1):
        return {"ok": False, "reason": "The provider answered HTTP 404", "failures": failures, "checked_at": self._ago(hours_ago), "history": [0]}

    def test_a_failed_stream_is_due_again_after_the_hours_set(self):
        self._results(**{
            str(self.first.id): self._failed(4),
            str(self.second.id): self._failed(1),
            str(self.third.id): {"ok": True, "checked_at": self._ago(9)},
        })
        self.assertEqual(stream_check.rechecks_due(stream_check.load_settings()), [self.first.id])

    def test_after_a_playlist_refresh_its_providers_failing_streams_are_due(self):
        stream_check.save_settings({"recheck_mode": "refresh"})
        self._results(**{str(self.first.id): self._failed(0.1), str(self.second.id): self._failed(0.1)})
        self.assertEqual(stream_check.rechecks_due(stream_check.load_settings()), [])
        self.assertEqual(stream_check.after_playlist_refresh(self.a.id), 1)
        self.assertEqual(stream_check.rechecks_due(stream_check.load_settings()), [self.first.id])

    def test_a_refresh_does_nothing_while_stream_check_is_off(self):
        stream_check.save_settings({"enabled": False, "recheck_mode": "refresh"})
        self._results(**{str(self.first.id): self._failed(0.1)})
        self.assertEqual(stream_check.after_playlist_refresh(self.a.id), 0)

    def test_the_tick_rechecks_only_the_failing_ones_and_leaves_the_full_run_as_it_was(self):
        from apps.channels.tasks import stream_check_tick

        self._results(**{str(self.first.id): self._failed(4)})
        last_run = stream_check.load_results()["last_run"]
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check_tick(), "done")
        self.assertEqual([c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list], ["ORF1A"])
        self.assertTrue(stream_check.load_results()["streams"][str(self.first.id)]["ok"])
        self.assertEqual(stream_check.load_results()["last_run"], last_run)

    def test_autopark_parks_a_stream_dead_check_after_check_and_puts_it_back_when_it_plays(self):
        stream_check.save_settings({"autopark": True, "autopark_after": 2, "account_failures": 99})
        for _ in range(2):
            stream_check.start_round(self.redis, force=True)
            with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": False})):
                stream_check.run(self.redis)
        parked = stream_check.load_parked()
        self.assertTrue(parked[str(self.second.id)]["auto"])
        self.assertNotIn("ORF 1 B", self._order(self.orf1))

        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": True})):
            stream_check.run(self.redis)
        self.assertEqual(self._order(self.orf1), ["ORF 1 A", "ORF 1 B", "ORF 1 A2", "could not dispatch"])

    def _kind(self, kind, frame=""):
        def answer(should_stop):
            return {"ok": False, "kind": kind, "reason": kind, "frozen_frame": frame,
                    "resolution": "", "codec": "", "bytes": 1, "seconds": 0.1}
        return answer

    def test_autopark_never_parks_a_refused_black_or_frozen_stream(self):
        # One look decides here: what is tested is what follows a picture fault
        stream_check.save_settings({"relook_pictures": False})
        stream_check.save_settings({"autopark": True, "autopark_after": 2, "account_failures": 99})
        kinds = {"ORF1B": self._kind(stream_check.BLACK), "ORF1A2": self._kind(stream_check.REFUSED)}
        for _ in range(4):
            stream_check.start_round(self.redis, force=True)
            with mock.patch.object(stream_check, "probe", side_effect=_answers(kinds)):
                stream_check.run(self.redis)
        self.assertEqual(stream_check.load_parked(), {})
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual((record["kind"], stream_check.state_of(record, stream_check.load_settings())), ("black", "broken"))

    def test_a_different_failure_starts_the_count_to_autopark_again(self):
        stream_check.save_settings({"autopark": True, "autopark_after": 2, "account_failures": 99})
        for answer in (False, self._kind(stream_check.BLACK), False):
            stream_check.start_round(self.redis, force=True)
            with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": answer})):
                stream_check.run(self.redis)
        self.assertEqual(stream_check.load_parked(), {})
        self.assertEqual(stream_check.load_results()["streams"][str(self.second.id)]["dead_streak"], 1)

    def test_rechecks_include_every_kind_of_failure(self):
        stream_check._store(stream_check.RESULTS_KEY, "x", {"streams": {
            str(self.second.id): {"ok": False, "kind": "black", "failures": 1, "checked_at": "2000-01-01"},
        }, "last_run": {"finished_at": stream_check._now()}})
        self.assertEqual(stream_check.rechecks_due(stream_check.load_settings()), [self.second.id])

    def test_the_same_still_picture_on_three_channels_is_the_providers_card(self):
        # One look decides here: what is tested is what follows a picture fault
        stream_check.save_settings({"relook_pictures": False})
        extra = []
        for number, name in ((2, "ORF 2"), (3, "ORF 3")):
            channel = Channel.objects.create(name=f"┃AT┃ {name}", channel_number=number, channel_group=self.group)
            stream = self._stream(f"{name} B", self.b)
            self._attach(channel, [stream])
            extra.append(stream)
        card = self._kind(stream_check.FROZEN, frame="abc123")
        stream_check.save_settings({"account_failures": 99})
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": card, "ORF2B": card, "ORF3B": card})):
            stream_check.run(self.redis)
        results = stream_check.load_results()["streams"]
        self.assertEqual({results[str(s.id)]["kind"] for s in [self.second] + extra}, {stream_check.PLACEHOLDER})
        self.assertIn("no stream", results[str(self.second.id)]["reason"])

    def test_the_same_still_picture_on_two_channels_is_only_frozen(self):
        # One look decides here: what is tested is what follows a picture fault
        stream_check.save_settings({"relook_pictures": False})
        channel = Channel.objects.create(name="┃AT┃ ORF 2", channel_number=2, channel_group=self.group)
        stream = self._stream("ORF 2 B", self.b)
        self._attach(channel, [stream])
        card = self._kind(stream_check.FROZEN, frame="abc123")
        stream_check.save_settings({"account_failures": 99})
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": card, "ORF2B": card})):
            stream_check.run(self.redis)
        self.assertEqual(stream_check.load_results()["streams"][str(stream.id)]["kind"], stream_check.FROZEN)

    def _round(self, answers):
        stream_check.start_round(self.redis, force=True) if stream_check.current_round(self.redis) is None else None
        with mock.patch.object(stream_check, "probe", side_effect=_answers(answers)):
            return stream_check.run(self.redis)

    def _later(self):
        """Every re-look of this round due now, as if RELOOK_GAP had passed."""
        results = stream_check.load_results()
        for record in results["streams"].values():
            if record.get("suspect"):
                record["suspect"]["next_at"] = 0
        stream_check._store(stream_check.RESULTS_KEY, "x", results)

    def test_a_picture_fault_is_a_suspicion_until_seen_again(self):
        stream_check.save_settings({"account_failures": 99})
        black = self._kind(stream_check.BLACK)
        self.assertEqual(self._round({"ORF1B": black}), "waiting")
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "suspect")
        self.assertEqual(record.get("failures", 0), 0)
        self.assertTrue(stream_check.is_running(self.redis))

        self._later()
        self.assertEqual(self._round({"ORF1B": black}), "done")
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual((record["kind"], record["failures"]), ("black", 1))
        self.assertIn("again when looked at later", record["reason"])

    def test_three_clean_looks_and_it_plays(self):
        stream_check.save_settings({"account_failures": 99})
        self._round({"ORF1B": self._kind(stream_check.FROZEN)})
        for _ in range(stream_check.RELOOKS):
            self._later()
            ended = self._round({})
        self.assertEqual(ended, "done")
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertTrue(record["ok"])
        self.assertNotIn("suspect", record)

    def test_a_re_look_waits_its_turn(self):
        stream_check.save_settings({"account_failures": 99})
        self._round({"ORF1B": self._kind(stream_check.BLACK)})
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            self.assertEqual(stream_check.run(self.redis), "waiting")
        probe.assert_not_called()
        self.assertGreaterEqual(stream_check.progress(self.redis)["resume_in"], stream_check.RETRY_WAITING)

    def _unreachable(self, should_stop=None):
        return {"ok": False, "kind": stream_check.UNREACHABLE, "reason": "Could not connect to the provider",
                "resolution": "", "codec": "", "bytes": 0, "seconds": 0.1}

    def test_no_connection_is_never_counted_and_is_tried_again(self):
        """What made a working SBS 6 4K broken: two moments the provider could not be reached."""
        stream_check.save_settings({"account_failures": 99, "autopark": True, "autopark_after": 1})
        self._round({"ORF1B": self._unreachable})
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "suspect")
        self.assertEqual(record.get("failures", 0), 0)
        self._later()
        self.assertEqual(self._round({}), "done")
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertTrue(record["ok"])
        self.assertEqual(stream_check.load_parked(), {})

    def test_a_timeout_or_server_error_counts_only_when_it_happens_again(self):
        stream_check.save_settings({"account_failures": 99})

        def server_error(should_stop=None):
            return {"ok": False, "kind": "dead", "transient": True, "reason": "The provider answered HTTP 502",
                    "resolution": "", "codec": "", "bytes": 0, "seconds": 0.1}

        self._round({"ORF1B": server_error})
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "suspect")
        self._later()
        self._round({"ORF1B": server_error})
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual((record["kind"], record["failures"]), ("dead", 1))
        self.assertIn("again when looked at later", record["reason"])

    def test_never_reached_in_a_whole_run_is_not_checked_rather_than_dead(self):
        stream_check.save_settings({"account_failures": 99, "autopark": True, "autopark_after": 1})
        self._round({"ORF1B": self._unreachable})
        for _ in range(stream_check.RELOOKS):
            self._later()
            ended = self._round({"ORF1B": self._unreachable})
        self.assertEqual(ended, "done")
        record = stream_check.load_results()["streams"][str(self.second.id)]
        self.assertEqual(stream_check.state_of(record, stream_check.load_settings()), "unchecked")
        self.assertIn("not counted", record["refused"])
        self.assertEqual(record.get("failures", 0), 0)
        self.assertEqual(stream_check.load_parked(), {})

    def test_a_stream_that_played_gets_the_quick_check_until_its_picture_is_due(self):
        stream_check.save_settings({"account_failures": 99, "picture_check": True, "picture_every_days": 3})
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            stream_check.run(self.redis)
        # Never checked: every picture looked at
        self.assertTrue(all(c.kwargs.get("picture_seconds") for c in probe.call_args_list))
        results = stream_check.load_results()
        for record in results["streams"].values():
            record["picture_at"] = stream_check._now()
        stream_check._store(stream_check.RESULTS_KEY, "x", results)
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            stream_check.run(self.redis)
        # Played, and looked at today: the quick check
        self.assertFalse(any(c.kwargs.get("picture_seconds") for c in probe.call_args_list))

    def test_how_long_a_round_has_left_follows_its_pace(self):
        now = time.time()
        found = {"state": "running", "total": 1000, "done": 400, "samples": [[now - 1800, 100], [now, 400]]}
        # 300 streams in half an hour: 600 more take an hour
        self.assertAlmostEqual(stream_check._eta(found), 3600, delta=5)
        self.assertIsNone(stream_check._eta({**found, "samples": [[now, 400]]}))
        self.assertIsNone(stream_check._eta({**found, "state": "done"}))

    def test_an_ignored_stream_is_off_the_list_and_not_checked(self):
        stream_check.save_settings({"account_failures": 99})
        stream_check.ignore(self.second.id)
        stream_check.start_round(self.redis, force=True)
        with mock.patch.object(stream_check, "probe", side_effect=_answers({})) as probe:
            stream_check.run(self.redis)
        self.assertNotIn("ORF1B", [c.args[0].rsplit("/", 1)[-1] for c in probe.call_args_list])
        found = stream_check.issues(self.redis, "all")
        self.assertEqual([r["name"] for r in found["ignored"]], ["ORF 1 B"])
        stream_check.unignore(self.second.id)
        self.assertEqual(stream_check.load_ignored(), {})

    def test_without_autopark_nothing_is_parked(self):
        stream_check.save_settings({"autopark": False, "account_failures": 99})
        for _ in range(4):
            stream_check.start_round(self.redis, force=True)
            with mock.patch.object(stream_check, "probe", side_effect=_answers({"ORF1B": False})):
                stream_check.run(self.redis)
        self.assertEqual(stream_check.load_parked(), {})


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

    def test_every_stream_of_a_channel_can_be_parked_at_once(self):
        """
        A channel whose streams are all broken is dealt with a channel at a time. Doing it
        a stream at a time took the row out from under you: park the broken one and the
        channel had nothing broken left, so it left the list with the stream you had not
        got to yet still on it.
        """
        response = self.api.post(
            "/api/channels/stream-check/action/",
            {"action": "park", "stream_ids": [self.first.id, self.second.id, self.third.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(response.json()["streams"]),
            sorted([self.first.id, self.second.id, self.third.id]),
        )
        parked = self.api.get("/api/channels/stream-check/").json()["parked"]
        self.assertEqual(len(parked), 3)
        # ...and the fallback is still the only thing left on the channel
        self.assertEqual(self._order(self.orf1), ["could not dispatch"])

    def test_one_stream_that_cannot_be_done_does_not_stop_the_others(self):
        # Stopping half way through a channel is the worst of both
        response = self.api.post(
            "/api/channels/stream-check/action/",
            {"action": "park", "stream_ids": [self.fallback.id, self.second.id]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["streams"], [self.second.id])

    def test_a_channel_just_acted_on_stays_on_the_list(self):
        # Whatever the view would otherwise do with it, now that it has nothing broken
        data = self.api.get(
            f"/api/channels/stream-check/?show=broken&keep={self.orf1.id}"
        ).json()
        self.assertEqual([r["channel"]["id"] for r in data["rows"]], [self.orf1.id])
        # ...and without being asked for, it is not there
        self.assertEqual(self.api.get("/api/channels/stream-check/?show=broken").json()["rows"], [])

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

    def test_results_that_cannot_be_trusted_can_be_forgotten(self):
        stream_check._store(stream_check.RESULTS_KEY, "x", {"streams": {"1": {"ok": False}}, "last_run": {}})
        self.assertEqual(self.api.post("/api/channels/stream-check/clear/").status_code, 200)
        self.assertEqual(stream_check.load_results()["streams"], {})

    def test_a_providers_limit_is_shown_set_and_forgotten(self):
        rows = {r["name"]: r for r in self.api.get("/api/channels/stream-check/").json()["limits"]}
        self.assertEqual(rows["Provider A"]["said"], "no limit found yet")
        key = rows["Provider A"]["key"]

        self.api.put("/api/channels/stream-check/limits/", {"key": key, "name": "Provider A", "limit": 25, "window_minutes": 10}, format="json")
        rows = {r["name"]: r for r in self.api.get("/api/channels/stream-check/").json()["limits"]}
        self.assertEqual((rows["Provider A"]["limit"], rows["Provider A"]["window_minutes"]), (25, 10))
        self.assertEqual(rows["Provider A"]["how"], "set by hand")

        self.api.put("/api/channels/stream-check/limits/", {"key": key, "limit": None}, format="json")
        rows = {r["name"]: r for r in self.api.get("/api/channels/stream-check/").json()["limits"]}
        self.assertIsNone(rows["Provider A"]["limit"])

    def test_bad_settings_are_refused(self):
        response = self.api.put("/api/channels/stream-check/settings/", {"settings": {"window_to": "nope"}}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(user=User.objects.create_user(username="v", password="x", user_level=0))
        self.assertEqual(viewer.get("/api/channels/stream-check/").status_code, 403)
