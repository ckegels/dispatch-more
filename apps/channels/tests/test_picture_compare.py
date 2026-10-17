"""
Picture compare (picture_compare.py): real ffmpeg on made-up programmes -- moving scenes with
cuts and sound that rises and falls, the same programme seven seconds ahead at another size
with a logo in a corner, another programme, and a picture that never changes.
"""

import math
import os
import random
import shutil
import subprocess
import tempfile
from unittest import skipUnless

from django.test import SimpleTestCase, TestCase

from apps.channels import picture_compare as pc

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))
FPS = 10
W, H = 160, 90


def _scene_plan(seed, length):
    """[(start, end, rectangles)]: a cut every 2-5 s, each scene a few moving rectangles."""
    rnd = random.Random(seed)
    plan, t = [], 0.0
    while t < length:
        end = t + rnd.uniform(2, 5)
        rects = [
            (rnd.randrange(W), rnd.randrange(H), rnd.randrange(10, 60), rnd.randrange(8, 40),
             rnd.randrange(256), rnd.uniform(-15, 15), rnd.uniform(-8, 8))
            for _ in range(rnd.randrange(3, 7))
        ]
        plan.append((t, end, rnd.randrange(256), rects))
        t = end
    return plan


def _frame(plan, t, still=False):
    for start, end, background, rects in plan:
        if start <= t < end or still:
            break
    picture = bytearray([background]) * (W * H)
    since = 0 if still else t - start
    for x, y, w, h, shade, dx, dy in rects:
        left = int(x + dx * since) % W
        top = int(y + dy * since) % H
        for row in range(top, min(H, top + h)):
            picture[row * W + left:row * W + min(W, left + w)] = bytes([shade]) * (min(W, left + w) - left)
    return bytes(picture)


def _sound(seed, start, seconds):
    """A tone whose loudness changes every half second, the same for the same programme."""
    rnd = random.Random(seed * 7919)
    levels = [rnd.uniform(0.05, 1.0) for _ in range(int((start + seconds) * 2) + 2)]
    rate = 8000
    out = bytearray()
    for n in range(int(seconds * rate)):
        t = start + n / rate
        value = int(12000 * levels[int(t * 2)] * math.sin(2 * math.pi * 440 * t))
        out += value.to_bytes(2, "little", signed=True)
    return bytes(out)


def _programme(folder, name, seed, start=0.0, seconds=110, big=False, logo=False, still=False):
    plan = _scene_plan(seed, start + seconds + 5)
    video = os.path.join(folder, f"{name}.gray")
    with open(video, "wb") as f:
        for n in range(int(seconds * FPS)):
            f.write(_frame(plan, start + n / FPS, still))
    audio = os.path.join(folder, f"{name}.pcm")
    with open(audio, "wb") as f:
        f.write(_sound(seed, start, seconds))
    filters = []
    if big:
        filters.append("scale=320:180")
    if logo:
        filters.append("drawbox=x=iw-40:y=6:w=32:h=14:color=white:t=fill")
    out = os.path.join(folder, f"{name}.ts")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", str(FPS), "-i", video,
         "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", audio,
         *(["-vf", ",".join(filters)] if filters else []),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-f", "mpegts", out],
        check=True, timeout=120,
    )
    return out


@skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class CompareTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = tempfile.mkdtemp(prefix="picture-test-")
        f = cls.folder
        cls.prints = {
            "ref": pc.record(_programme(f, "ref", seed=1)),
            # The same programme from another provider: 7 s ahead, bigger, with its logo
            "ahead": pc.record(_programme(f, "ahead", seed=1, start=7, big=True, logo=True)),
            "other": pc.record(_programme(f, "other", seed=2)),
            "still": pc.record(_programme(f, "still", seed=3, still=True)),
        }

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)
        super().tearDownClass()

    def test_a_recording_is_two_pictures_a_second_with_the_sound_and_a_few_to_look_at(self):
        ref = self.prints["ref"]
        self.assertEqual(ref["error"], "")
        self.assertAlmostEqual(len(ref["frames"]), pc.RECORD_SECONDS * pc.FRAMES_PER_SECOND, delta=4)
        self.assertAlmostEqual(len(ref["loud"]), pc.RECORD_SECONDS * pc.LOUD_PER_SECOND, delta=10)
        self.assertGreaterEqual(len(ref["thumbs"]), pc.RECORD_SECONDS // pc.THUMB_EVERY)

    def test_the_same_programme_elsewhere_is_the_same_and_the_delay_is_found(self):
        found = pc.compare(self.prints["ref"], self.prints["ahead"])
        self.assertEqual(found["verdict"], pc.SAME, found)
        self.assertAlmostEqual(found["offset"], -7, delta=0.5)
        self.assertIn("7 s ahead", found["why"])
        self.assertGreater(found["sound"], 0.8)
        # ...and the other way round, it is behind
        back = pc.compare(self.prints["ahead"], self.prints["ref"])
        self.assertEqual(back["verdict"], pc.SAME, back)
        self.assertAlmostEqual(back["offset"], 7, delta=0.5)

    def test_another_programme_is_different(self):
        found = pc.compare(self.prints["ref"], self.prints["other"])
        self.assertEqual(found["verdict"], pc.DIFFERENT, found)

    def test_a_picture_that_never_changes_proves_nothing(self):
        found = pc.compare(self.prints["still"], self.prints["ref"])
        self.assertEqual(found["verdict"], pc.UNSURE)
        self.assertIn("hardly changes", found["why"])

    def test_the_picture_alike_with_other_sound_is_not_taken_for_the_same(self):
        dubbed = dict(self.prints["ahead"], loud=self.prints["other"]["loud"])
        found = pc.compare(self.prints["ref"], dubbed)
        self.assertEqual(found["verdict"], pc.UNSURE, found)
        self.assertIn("sound does not", found["why"])

    def test_a_stream_that_does_not_play_says_why(self):
        found = pc.record(os.path.join(self.folder, "nothing-here.ts"), seconds=5)
        self.assertEqual(found["frames"], [])
        self.assertTrue(found["error"])


class PlanTests(SimpleTestCase):
    """What is opened together: the reference, and one stream on every other login."""

    # TiviBridge and TiviBridge2: one provider, two logins; Digitalizard another
    providers = {14: "one-zone", 12: "one-zone", 15: "digitalizard"}
    logins = {14: [140], 12: [120], 15: [150]}

    def plan(self, streams):
        return pc.plan_sessions(
            [{"id": i, "name": n, "account_id": a} for i, n, a in streams],
            self.providers.get, self.logins,
        )

    def test_a_name_is_compared_once_per_provider_on_whichever_login_is_free(self):
        sessions, left_out = self.plan([
            (1, "┃FR┃ BFM TV HD", 12),
            (2, "┃FR┃ BFM TV FHD", 12),   # only on the reference's login here...
            (3, "┃FR┃ BFM TV HD", 14),    # the same as the reference: one provider
            (4, "┃FR┃ BFM TV FHD", 14),   # ...but TiviBridge carries it too
            (5, "FR| BFM TV HD", 15),
            (6, "FR| BFM TV FHD", 15),
        ])
        self.assertEqual(left_out, [])
        opened = [{login: s["id"] for login, s in taken.items()} for _, taken in sessions]
        # The reference's HD is never compared with its own copy; FHD goes through
        # TiviBridge, and Digitalizard's two take two sessions on its one login
        self.assertEqual(opened, [{140: 4, 150: 5}, {150: 6}])
        self.assertTrue(all(ref["id"] == 1 for ref, _ in sessions))

    def test_what_only_the_reference_login_carries_is_left_out(self):
        sessions, left_out = self.plan([(1, "A HD", 12), (2, "A FHD", 12)])
        self.assertEqual(sessions, [])
        self.assertEqual([s["id"] for s in left_out], [2])

    def test_one_stream_has_nothing_to_compare(self):
        self.assertEqual(self.plan([(1, "A", 12)]), ([], []))


class _FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def exists(self, key):
        return key in self.values

    def delete(self, key):
        self.values.pop(key, None)


def _prints(seed, start=0, seconds=90):
    """A made-up fingerprint: a programme's pictures change every couple of seconds."""
    rnd = random.Random(seed)
    scenes = [rnd.getrandbits(64) for _ in range(200)]
    rnd2 = random.Random(seed + 1000)
    levels = [rnd2.random() for _ in range(3000)]
    first = int(start * pc.FRAMES_PER_SECOND)
    frames = [scenes[(first + i) // 3] for i in range(seconds * pc.FRAMES_PER_SECOND)]
    first_loud = int(start * pc.LOUD_PER_SECOND)
    loud = [levels[first_loud + i] for i in range(seconds * pc.LOUD_PER_SECOND)]
    return {"frames": frames, "loud": loud, "thumbs": ["AAAA"] * 9, "error": ""}


class ChannelTests(TestCase):
    """A channel's streams compared with its first, through the logins a provider has."""

    def setUp(self):
        from apps.channels.models import Channel, ChannelStream, Stream
        from apps.m3u.models import M3UAccount, M3UAccountProfile

        def account(name, server):
            made = M3UAccount.objects.create(name=name, account_type="STD", server_url=server, is_active=True)
            M3UAccountProfile.objects.filter(m3u_account=made).delete()
            M3UAccountProfile.objects.create(m3u_account=made, name="default", is_default=True, max_streams=1)
            return made

        # TiviBridge and TiviBridge2 reach one server: one provider, two logins
        tb = account("TiviBridge", "http://line.azerty-live.cc/get.php")
        tb2 = account("TiviBridge2", "http://line.azerty-live.cc/get.php")
        dz = account("Digitalizard.com", "http://wwgssxrk.lzrdns3.com/get.php")
        self.channel = Channel.objects.create(name="┃FR┃ MTV", channel_number=1)

        def stream(name, owner, order, custom=False):
            made = Stream.objects.create(name=name, url=f"http://x/{name}", m3u_account=owner, is_custom=custom)
            ChannelStream.objects.create(channel=self.channel, stream=made, order=order)
            return made

        self.ref = stream("┃FR┃ MTV HD", tb2, 0)
        self.fhd = stream("┃FR┃ MTV FHD", tb, 1)
        self.wrong = stream("FR| MTV HD", dz, 2)
        stream("Could Not Dispatch", None, 3, custom=True)
        # What each stream shows: the FHD is the reference 4 s later; Digitalizard's is
        # another MTV altogether
        self.shows = {self.ref.id: _prints(1), self.fhd.id: _prints(1, start=-4), self.wrong.id: _prints(2)}
        self.redis = _FakeRedis()
        self.released = []
        self.busy = set()

    def run_it(self):
        from unittest import mock

        def take(logins, redis_client):
            return None if logins[0].m3u_account.name in self.busy else logins[0]

        def url_for(stream, profile):
            return f"stream:{stream.id}"

        def record(url, user_agent="", seconds=0, should_stop=None):
            return self.shows[int(url.split(":")[1])]

        with mock.patch("apps.channels.stream_check._take_connection", side_effect=take), \
                mock.patch("apps.channels.stream_check._url_for", side_effect=url_for), \
                mock.patch("apps.m3u.connection_pool.release_profile_slot",
                           side_effect=lambda pid, r: self.released.append(pid)), \
                mock.patch.object(pc, "record", side_effect=record):
            pc.compare_channel(self.channel.id, self.redis)
        return pc.load(self.redis, self.channel.id)

    def test_every_stream_against_the_first_in_one_session_and_the_wrong_one_said(self):
        state = self.run_it()
        self.assertEqual(state["state"], "done", state)
        self.assertEqual(state["sessions"], 1)
        self.assertEqual(state["reference"]["name"], "┃FR┃ MTV HD")
        found = {row["name"]: row for row in state["results"]}
        # The fallback is no stream of the channel's to compare
        self.assertEqual(set(found), {"┃FR┃ MTV FHD", "FR| MTV HD"})
        self.assertEqual(found["┃FR┃ MTV FHD"]["verdict"], pc.SAME)
        self.assertEqual(found["┃FR┃ MTV FHD"]["offset"], 4)
        self.assertTrue(found["┃FR┃ MTV FHD"]["same_provider"])
        self.assertEqual(found["FR| MTV HD"]["verdict"], pc.DIFFERENT)
        self.assertFalse(found["FR| MTV HD"]["same_provider"])
        self.assertTrue(found["FR| MTV HD"]["thumb_ref"])
        # Every connection taken is given back
        self.assertEqual(len(self.released), 3)

    def test_a_busy_login_is_said_and_the_rest_still_compared(self):
        self.busy = {"Digitalizard.com"}
        state = self.run_it()
        found = {row["name"]: row for row in state["results"]}
        self.assertEqual(found["FR| MTV HD"]["verdict"], pc.UNSURE)
        self.assertIn("in use", found["FR| MTV HD"]["why"])
        self.assertEqual(found["┃FR┃ MTV FHD"]["verdict"], pc.SAME)
        self.assertEqual(len(self.released), 2)

    def test_the_reference_busy_stops_it_and_says_so(self):
        self.busy = {"TiviBridge2"}
        state = self.run_it()
        self.assertIn("in use", state["error"])
        self.assertEqual(state["results"], [])

    def test_a_viewer_needing_a_connection_stops_it_and_it_is_given_back(self):
        from apps.channels import stream_check

        # The proxy asks the moment a viewer finds the provider full
        self.redis.set(pc.LOCK_KEY, "1")
        self.assertTrue(stream_check.make_way(self.redis))
        state = self.run_it()
        self.assertIn("a viewer needed the connection", state["error"])
        self.assertEqual(len(self.released), 3)
