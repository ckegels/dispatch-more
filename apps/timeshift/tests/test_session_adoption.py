"""Dispatch More: an API catch-up session served under another session's pool (a re-mint for
a seek, or a restart from the same device, fingerprint-matched) stays alive while that pool
streams, and its position reports reach the stats entry that is streaming (sessions.adopt).

Seen on a server (2026-09-30): the new session was matched to the old one's pool, the
heartbeats refreshed only the old id, and every seek or resume more than ten minutes in was
refused while the reports were answered "no active playback"."""

import fnmatch

from django.test import SimpleTestCase

from apps.timeshift import sessions, stats, views
from apps.timeshift.redis_keys import TimeshiftRedisKeys


class _Pipe:
    def __init__(self, redis):
        self.redis, self.calls = redis, []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return call

    def execute(self):
        return [getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.calls]


class FakeRedis:
    def __init__(self):
        self.data, self.ttl = {}, {}

    def pipeline(self, transaction=False):
        return _Pipe(self)

    def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.data.setdefault(key, {})
        if mapping:
            bucket.update({k: str(v) for k, v in mapping.items()})
        if field is not None:
            bucket[field] = str(value)

    def hget(self, key, field):
        return (self.data.get(key) or {}).get(field)

    def hgetall(self, key):
        value = self.data.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def hdel(self, key, field):
        (self.data.get(key) or {}).pop(field, None)

    def sadd(self, key, *members):
        self.data.setdefault(key, set()).update(members)

    def smembers(self, key):
        return set(self.data.get(key) or set())

    def set(self, key, value, ex=None):
        self.data[key] = str(value)
        self.ttl[key] = ex

    def get(self, key):
        value = self.data.get(key)
        return value if isinstance(value, str) else None

    def expire(self, key, seconds):
        if key in self.data:
            self.ttl[key] = seconds

    def exists(self, key):
        return int(bool(self.data.get(key)))

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)
        return 1

    def scan(self, cursor, match="*", count=100):
        return 0, [k for k in self.data if fnmatch.fnmatch(k, match)]

    def expire_now(self, key):
        """What Redis does when a TTL runs out."""
        self.data.pop(key, None)


class AdoptedSessionTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.old, self.new = "ErLtOld", "ZadZNew"
        # The player's new API session; the old one was revoked by the app already
        self.redis.hset(TimeshiftRedisKeys.api_session(self.new),
                        mapping={"user_id": "3", "channel_uuid": "u", "start": "x"})

    def test_the_pools_heartbeat_keeps_the_adopted_session_alive(self):
        self.assertTrue(sessions.adopt(self.new, self.old, redis_client=self.redis))
        self.redis.ttl.clear()
        views._refresh_active_session_redis_ttl(self.redis, self.old)
        self.assertEqual(self.redis.ttl.get(TimeshiftRedisKeys.api_session(self.new)),
                         sessions.SESSION_IDLE_TTL_SECONDS)

    def test_nothing_is_linked_without_an_api_session_or_for_itself(self):
        self.assertFalse(sessions.adopt("no-such", self.old, redis_client=self.redis))
        self.assertFalse(sessions.adopt(self.new, self.new, redis_client=self.redis))
        self.assertEqual(sessions.aliases_of(self.old, redis_client=self.redis), [])

    def test_position_reports_reach_the_entry_that_streams(self):
        sessions.adopt(self.new, self.old, redis_client=self.redis)
        client_key = TimeshiftRedisKeys.client_metadata("7_ch", self.old)
        self.redis.hset(client_key, mapping={"user_id": "3"})
        self.redis.ttl.clear()
        self.assertTrue(stats.update_catchup_session_position(
            self.new, position_secs=600, paused=True, user_id=3, redis_client=self.redis))
        self.assertEqual(self.redis.hget(client_key, "paused"), "1")
        self.assertEqual(self.redis.hget(client_key, "playback_base_secs"), "600.0")
        # The player's own session is the one kept alive
        self.assertEqual(self.redis.ttl.get(TimeshiftRedisKeys.api_session(self.new)),
                         sessions.SESSION_IDLE_TTL_SECONDS)

    def test_a_report_with_no_playback_at_all_is_still_refused(self):
        self.assertFalse(stats.update_catchup_session_position(
            self.new, position_secs=1, redis_client=self.redis))

    def test_the_adopted_session_ends_with_the_pools_viewing(self):
        sessions.adopt(self.new, self.old, redis_client=self.redis)
        views._finalize_playback_session_auth(self.redis, self.old)
        self.assertFalse(self.redis.exists(TimeshiftRedisKeys.api_session(self.new)))
