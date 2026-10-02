"""Dispatch More's helpers (the rewind recorder, the caption worker) never keep a channel open:
the shutdown checks count people only (server.ProxyServer._people_count)."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.proxy.live_proxy.redis_keys import RedisKeys
from apps.proxy.live_proxy.server import ProxyServer


class FakeRedis:
    def __init__(self, agents):
        self.agents = agents

    def smembers(self, key):
        return set(self.agents)

    def pipeline(self, transaction=False):
        redis, calls = self, []

        class Pipe:
            def hget(self, key, field):
                calls.append(key.split(":clients:")[-1])

            def execute(self):
                return [redis.agents[c].encode() for c in calls]

        return Pipe()


def people(agents):
    fake = SimpleNamespace(redis_client=FakeRedis(agents))
    return ProxyServer._people_count(fake, "c1", len(agents))


class PeopleCountTests(SimpleTestCase):
    def test_the_recorder_alone_is_nobody(self):
        self.assertEqual(people({"a": "DispatchMore-Rewind/1"}), 0)
        self.assertEqual(people({"a": "DispatchMore-Rewind/1", "b": "DispatchMore-Captions/1"}), 0)

    def test_people_count(self):
        self.assertEqual(people({"a": "DispatchMore-Rewind/1", "b": "AerioTV/0.5.9-arr.91"}), 1)

    def test_nothing_to_count(self):
        self.assertEqual(ProxyServer._people_count(SimpleNamespace(redis_client=None), "c1", 0), 0)

    def test_the_client_key_is_the_channels(self):
        self.assertTrue(RedisKeys.client_metadata("c1", "a").endswith(":clients:a"))
