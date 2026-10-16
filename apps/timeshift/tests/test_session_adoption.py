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


class SeekSessionTests(SimpleTestCase):
    """
    Dispatch More: arrTV mints an API session for every seek (its archive stream cannot be
    seeked in). Seen on the real server (2026-10-16): one such session's requests were served
    by three different old sessions' pools, each at another position, and the pool it got
    estimated a byte offset in its own archive -- a forward press played the same part again
    or jumped to a random time.
    """

    def setUp(self):
        self.redis = FakeRedis()
        self.old, self.new = "QXBold", "LSsNew"
        self.redis.hset(TimeshiftRedisKeys.api_session(self.new),
                        mapping={"user_id": "3", "channel_uuid": "u", "start": "x"})
        self.redis.hset(views._pool_key(self.old), mapping={
            "user_id": "3", "profile_id": "12", "media_id": "8_2026-10-16-20-00",
            "final_url": "http://cdn/archive-opened-at-20-00.ts", "archive_anchor_ts": "2026-10-16:20-00",
        })

    def test_every_request_of_a_session_stays_on_the_pool_it_was_handed(self):
        self.assertIsNone(views._held_pool(self.redis, self.new, 3))
        sessions.adopt(self.new, self.old, redis_client=self.redis)
        self.assertEqual(views._held_pool(self.redis, self.new, 3), self.old)
        # Not another viewer's pool, nor one that is gone
        self.assertIsNone(views._held_pool(self.redis, self.new, 4))
        self.redis.delete(views._pool_key(self.old))
        self.assertIsNone(views._held_pool(self.redis, self.new, 3))

    def test_a_seek_is_served_to_the_second_in_the_archive_the_pool_has_open(self):
        """
        Not by opening a new archive: provider URLs carry the minute only, so a +30 s press
        from the start of a minute asked for that same minute again (the Shield, 2026-10-16).
        """
        sessions.adopt(self.new, self.old, redis_client=self.redis)
        self.redis.hset(views._pool_key(self.old), mapping={
            "content_length": "600000000", "archive_duration_secs": "3600",
            "archive_anchor_ts": "2026-10-16T20:00:00Z",
        })
        entry = views._get_pool_entry(self.redis, views._held_pool(self.redis, self.new, 3))
        scrub = views._resolve_session_archive_scrub(entry, "2026-10-16T20:00:31Z")
        self.assertEqual(scrub["kind"], "scrub")
        self.assertEqual(entry.get("final_url"), "http://cdn/archive-opened-at-20-00.ts")
        # 31 s of an hour's 600 MB, on a TS packet boundary
        self.assertEqual(scrub["byte_offset"], (int(31 / 3600 * 600000000) // 188) * 188)

    def test_a_whole_minute_opens_the_providers_archive_at_that_minute(self):
        """arrTV moves a minute a press: the provider serves that minute exactly."""
        self.assertTrue(views._open_at_its_minute(
            self.redis, self.new, self.old, "8_2026-10-16T20-12-00Z", "2026-10-16T20:12:00Z"))
        self.assertIsNone(self.redis.hget(views._pool_key(self.old), "final_url"))

    def test_a_start_with_seconds_or_the_pools_own_or_an_xtream_one_keeps_the_open_archive(self):
        url = "http://cdn/archive-opened-at-20-00.ts"
        self.assertFalse(views._open_at_its_minute(
            self.redis, self.new, self.old, "8_2026-10-16T20-12-31Z", "2026-10-16T20:12:31Z"))
        self.assertFalse(views._open_at_its_minute(
            self.redis, self.new, self.old, "8_2026-10-16-20-00", "2026-10-16:20-00"))
        self.assertFalse(views._open_at_its_minute(
            self.redis, "xtream-301-session", self.old, "8_x", "2026-10-16T20:12:00Z"))
        self.assertEqual(self.redis.hget(views._pool_key(self.old), "final_url"), url)
