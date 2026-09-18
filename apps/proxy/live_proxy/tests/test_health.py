"""Reading what a channel is doing, and keeping what it ended on (live_proxy.health).

A channel that stops takes its metadata with it, so afterwards there is no way back to what
it was doing. These cover the sampling that makes that recoverable, and that it is a
recording and nothing more: it never decides anything about a stream.
"""

import json

from django.test import TestCase

from apps.proxy.live_proxy import health


class FakeRedis:
    """Enough Redis for the sampler: hashes, sets, lists and key patterns."""

    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.lists = {}
        self.strings = {}
        self.expiries = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        target = self.hashes.setdefault(key, {})
        if mapping:
            target.update({k: str(v) for k, v in mapping.items()})
        if field is not None:
            target[field] = str(value)

    def scard(self, key):
        return len(self.sets.get(key, ()))

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)

    def lpush(self, key, *values):
        for value in values:
            self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, stop):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if stop == -1 else items[start:stop + 1]

    def lrange(self, key, start, stop):
        items = self.lists.get(key, [])
        return items[start:] if stop == -1 else items[start:stop + 1]

    def expire(self, key, seconds):
        self.expiries[key] = seconds

    def set(self, key, value, nx=False, ex=None):
        """Only the nx form is used, to keep one worker reading at a time."""
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    def delete(self, key):
        self.lists.pop(key, None)
        self.hashes.pop(key, None)

    def keys(self, pattern):
        import fnmatch

        everything = list(self.hashes) + list(self.lists)
        return [key for key in everything if fnmatch.fnmatch(key, pattern)]

    def scan_iter(self, match=None, count=None):
        return iter(self.keys(match or "*"))


class SamplingTests(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        health.save_settings(dict(health.DEFAULTS))
        self.addCleanup(lambda: health.save_settings(dict(health.DEFAULTS)))

    def _sweep(self):
        """One reading, as the next interval would take it: the lock is per interval."""
        self.redis.strings.pop(health.SWEEP_LOCK_KEY, None)
        return health.sweep(self.redis)

    def _a_running_channel(self, uuid="abc", speed="1.00", clients=2, bitrate="4500"):
        self.redis.hset(f"live:channel:{uuid}:metadata", mapping={
            "state": "active",
            "channel_name": "ORF 1",
            "init_time": "1000",
            "ffmpeg_speed": speed,
            "source_bitrate": bitrate,
            "ffmpeg_output_bitrate": "4400",
            "total_bytes": "999",
        })
        for number in range(clients):
            self.redis.sadd(f"live:channel:{uuid}:clients", f"c{number}")

    def test_a_reading_says_what_the_channel_is_doing(self):
        self._a_running_channel()

        reading = health.sample(self.redis, "abc")

        self.assertEqual(reading["state"], "active")
        self.assertEqual(reading["speed"], 1.0)
        self.assertEqual(reading["clients"], 2)
        self.assertEqual(reading["source_kbps"], 4500)

    def test_a_channel_that_is_not_running_reads_as_nothing(self):
        self.assertEqual(health.sample(self.redis, "gone"), {})

    def test_the_readings_are_kept_while_the_channel_runs(self):
        self._a_running_channel()

        self._sweep()
        self._sweep()

        samples = self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1)
        self.assertEqual(len(samples), 2)

    def test_only_so_many_readings_are_kept(self):
        self._a_running_channel()
        for _ in range(health.SAMPLES_KEPT + 5):
            self._sweep()

        samples = self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1)
        self.assertEqual(len(samples), health.SAMPLES_KEPT)

    def test_what_a_channel_ended_on_is_kept_after_it_stops(self):
        """The whole point: once the metadata is gone there is no way back to this."""
        self._a_running_channel(speed="0.62")
        self._sweep()

        # The channel stops: its metadata goes, as it does on a real one
        self.redis.delete("live:channel:abc:metadata")
        self._sweep()

        stopped = health.stopped_lately(self.redis)
        self.assertEqual(len(stopped), 1)
        # Called what it was called while it ran, not by its id
        self.assertEqual(stopped[0]["channel"], "ORF 1")
        self.assertEqual(stopped[0]["samples"][-1]["speed"], 0.62)
        # And the live readings are not left behind for a channel that is gone
        self.assertEqual(self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1), [])

    def test_a_channel_that_stopped_before_the_window_is_left_out(self):
        self._a_running_channel()
        self._sweep()
        self.redis.delete("live:channel:abc:metadata")
        self._sweep()

        # Asked for the last hour, when it stopped just now: still there
        self.assertEqual(len(health.stopped_lately(self.redis, 3600)), 1)

        # Made to look old
        record = json.loads(self.redis.lrange(health.STOPPED_KEY, 0, -1)[0])
        record["stopped_at"] = 1
        self.redis.lists[health.STOPPED_KEY] = [json.dumps(record)]
        self.assertEqual(health.stopped_lately(self.redis, 3600), [])

    def test_only_one_worker_reads_per_interval(self):
        """
        Every worker process runs the thread this is called from.

        Without a lock they all read the same channels in the same instant, which is four
        times the readings and a handover of a stopped channel racing with itself.
        """
        self._a_running_channel()

        self.assertEqual(health.sweep(self.redis), 1)
        self.assertEqual(health.sweep(self.redis), 0)  # another worker, same instant

        samples = self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1)
        self.assertEqual(len(samples), 1)

    def test_switched_off_it_reads_nothing(self):
        """Off means off: no readings taken and nothing written."""
        health.save_settings({"enabled": False})
        self._a_running_channel()

        self.assertEqual(health.sweep(self.redis), 0)
        self.assertEqual(self.redis.lists, {})

    def test_a_reading_that_cannot_be_taken_is_not_an_error(self):
        """A recording is worth less than the channel it is about."""

        class Broken(FakeRedis):
            def keys(self, pattern):
                raise RuntimeError("redis is having a moment")

        self.assertEqual(health.sweep(Broken()), 0)

    def test_what_a_channel_carries_is_worked_out_from_the_bytes(self):
        """
        ffmpeg's speed and bitrate are only written where a stream profile is running one.

        A channel proxied straight through has neither, so every column about how well it is
        going would be empty. The bytes are always counted, and how many arrived over a
        window of readings says whether data is still arriving and how much.
        """
        samples = health.with_rates([
            {"at": 100.0 + step * 5, "bytes": step * 2_500_000} for step in range(7)
        ])

        self.assertEqual(samples[0]["kbps"], 0.0)  # nothing to compare the first with
        # 2.5 MB every 5s is 4 Mbps, whichever window it is measured over
        self.assertEqual(samples[-1]["kbps"], 4000.0)
        self.assertEqual(samples[5]["kbps"], 4000.0)

    def test_a_counter_written_as_rarely_as_the_readings_still_reads_steadily(self):
        """
        The byte counter goes to Redis about as often as a reading is taken.

        Compared against the reading before it, the two beat against each other: an interval
        that catches no update reads as nothing arriving, which is what a channel that has
        stopped carrying looks like. Measured over a window, every one holds several.
        """
        # Nothing, then a lump, then nothing, then a lump: the same stream either way
        samples = health.with_rates([
            {"at": 100.0, "bytes": 0},
            {"at": 105.0, "bytes": 0},
            {"at": 110.0, "bytes": 5_000_000},
            {"at": 115.0, "bytes": 5_000_000},
            {"at": 120.0, "bytes": 10_000_000},
            {"at": 125.0, "bytes": 10_000_000},
        ])

        # 10 MB over 20s is 4 Mbps, and no reading in the window says the channel died
        self.assertEqual(samples[-1]["kbps"], 4000.0)
        self.assertGreater(min(s["kbps"] for s in samples[2:]), 0)

    def test_a_channel_that_stopped_carrying_anything_reads_as_zero(self):
        """Which is the difference between a slow stream and a stopped one."""
        samples = health.with_rates([
            {"at": 100.0 + step * 5, "bytes": 5_000_000} for step in range(6)
        ])
        self.assertEqual(samples[-1]["kbps"], 0.0)

    def test_a_counter_that_went_backwards_is_not_a_negative_rate(self):
        """The channel restarted behind it; nothing arrived that can be measured."""
        samples = health.with_rates([
            {"at": 100.0, "bytes": 5_000_000},
            {"at": 125.0, "bytes": 10},
        ])
        self.assertEqual(samples[1]["kbps"], 0.0)

    def test_what_is_running_is_reported_with_its_readings(self):
        self._a_running_channel()
        self._sweep()

        (channel,) = health.running_now(self.redis)
        self.assertEqual(channel["now"]["state"], "active")
        self.assertEqual(len(channel["samples"]), 1)
