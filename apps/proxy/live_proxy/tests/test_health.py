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

    def delete(self, key):
        self.lists.pop(key, None)
        self.hashes.pop(key, None)

    def keys(self, pattern):
        import fnmatch

        everything = list(self.hashes) + list(self.lists)
        return [key for key in everything if fnmatch.fnmatch(key, pattern)]


class SamplingTests(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        health.save_settings(dict(health.DEFAULTS))
        self.addCleanup(lambda: health.save_settings(dict(health.DEFAULTS)))

    def _a_running_channel(self, uuid="abc", speed="1.00", clients=2, bitrate="4500"):
        self.redis.hset(f"live:channel:{uuid}:metadata", mapping={
            "state": "active",
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

        health.sweep(self.redis)
        health.sweep(self.redis)

        samples = self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1)
        self.assertEqual(len(samples), 2)

    def test_only_so_many_readings_are_kept(self):
        self._a_running_channel()
        for _ in range(health.SAMPLES_KEPT + 5):
            health.sweep(self.redis)

        samples = self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1)
        self.assertEqual(len(samples), health.SAMPLES_KEPT)

    def test_what_a_channel_ended_on_is_kept_after_it_stops(self):
        """The whole point: once the metadata is gone there is no way back to this."""
        self._a_running_channel(speed="0.62")
        health.sweep(self.redis)

        # The channel stops: its metadata goes, as it does on a real one
        self.redis.delete("live:channel:abc:metadata")
        health.sweep(self.redis)

        stopped = health.stopped_lately(self.redis)
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["samples"][-1]["speed"], 0.62)
        # And the live readings are not left behind for a channel that is gone
        self.assertEqual(self.redis.lrange(health.LIVE_KEY.format(channel_id="abc"), 0, -1), [])

    def test_a_channel_that_stopped_before_the_window_is_left_out(self):
        self._a_running_channel()
        health.sweep(self.redis)
        self.redis.delete("live:channel:abc:metadata")
        health.sweep(self.redis)

        # Asked for the last hour, when it stopped just now: still there
        self.assertEqual(len(health.stopped_lately(self.redis, 3600)), 1)

        # Made to look old
        record = json.loads(self.redis.lrange(health.STOPPED_KEY, 0, -1)[0])
        record["stopped_at"] = 1
        self.redis.lists[health.STOPPED_KEY] = [json.dumps(record)]
        self.assertEqual(health.stopped_lately(self.redis, 3600), [])

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

    def test_what_is_running_is_reported_with_its_readings(self):
        self._a_running_channel()
        health.sweep(self.redis)

        (channel,) = health.running_now(self.redis)
        self.assertEqual(channel["now"]["state"], "active")
        self.assertEqual(len(channel["samples"]), 1)
