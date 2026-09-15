"""Tests for probation slots during live channel switches."""

import fnmatch
import time
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.channels.models import Channel, ChannelStream, Stream
from apps.m3u.connection_pool import (
    pool_has_capacity_for_profile,
    profile_connections_key,
    release_profile_slot,
    reserve_profile_slot,
)
from apps.m3u.models import M3UAccount, M3UAccountProfile, ServerGroup
from apps.m3u.serializers import M3UAccountSerializer
from apps.proxy.live_proxy import probation
from apps.proxy.live_proxy.constants import ChannelMetadataField, ChannelState
from apps.proxy.live_proxy.redis_keys import RedisKeys


class FakeRedis:
    """Minimal in-memory Redis (decode_responses=True semantics)."""

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.sets = {}

    def get(self, key):
        value = self.strings.get(key)
        return None if value is None else str(value)

    def set(self, key, value, nx=False, ex=None):
        if nx and self.exists(key):
            return None
        self.strings[key] = str(value)
        return True

    def setex(self, key, ttl, value):
        self.strings[key] = str(value)

    def delete(self, *keys):
        removed = 0
        for key in keys:
            for store in (self.strings, self.hashes, self.sets):
                if store.pop(key, None) is not None:
                    removed += 1
        return removed

    def exists(self, key):
        return int(key in self.strings or key in self.hashes or key in self.sets)

    def expire(self, key, ttl):
        return True

    def incr(self, key):
        value = int(self.strings.get(key) or 0) + 1
        self.strings[key] = str(value)
        return value

    def decr(self, key):
        value = int(self.strings.get(key) or 0) - 1
        self.strings[key] = str(value)
        return value

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        bucket = self.hashes.setdefault(key, {})
        if field is not None:
            bucket[field] = str(value)
        for f, v in (mapping or {}).items():
            bucket[f] = str(v)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, amount=1):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, 0)) + amount)
        return int(bucket[field])

    def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        return sum(1 for field in fields if bucket.pop(field, None) is not None)

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(str(m) for m in members)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def srem(self, key, *members):
        bucket = self.sets.get(key, set())
        return sum(1 for m in members if str(m) in bucket and not bucket.discard(str(m)))

    def scan_iter(self, match="*", count=None):
        keys = set(self.strings) | set(self.hashes) | set(self.sets)
        return [k for k in keys if fnmatch.fnmatchcase(k, match)]


def _reset_in_use_cache(test):
    """probation.in_use() is cached in Redis, which test database rollbacks do not reset."""
    probation.forget_in_use()
    test.addCleanup(probation.forget_in_use)


def _make_account(name, max_streams=1, probation_enabled=False, probation_seconds=None):
    custom_properties = {"probation_enabled": probation_enabled}
    if probation_seconds is not None:
        custom_properties["probation_seconds"] = probation_seconds
    account = M3UAccount.objects.create(
        name=name,
        account_type="XC",
        username=f"{name}-user",
        password="pass",
        max_streams=max_streams,
        custom_properties=custom_properties,
    )
    profile = M3UAccountProfile.objects.get(m3u_account=account, is_default=True)
    return account, profile


class ReserveExtraCapacityTests(TestCase):
    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        _account, self.profile = _make_account("extra-capacity")

    def test_extra_capacity_allows_one_slot_past_limit(self):
        self.assertTrue(reserve_profile_slot(self.profile, self.redis)[0])
        self.assertFalse(reserve_profile_slot(self.profile, self.redis)[0])

        reserved, count, _reason = reserve_profile_slot(
            self.profile, self.redis, extra_capacity=1
        )
        self.assertTrue(reserved)
        self.assertEqual(count, 2)

        self.assertFalse(
            reserve_profile_slot(self.profile, self.redis, extra_capacity=1)[0]
        )
        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "2")


@patch("apps.channels.models.Channel._pick_channel_to_preempt", return_value=None)
class GetStreamProbationTests(TestCase):
    IP = "192.168.1.20"

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account_a, self.profile_a = _make_account("provider-a", probation_enabled=True)
        self.account_b, self.profile_b = _make_account(
            "provider-b", probation_enabled=True, probation_seconds=25
        )
        self.stream_a = Stream.objects.create(
            name="News A", url="http://a.example/live/u/p/1.ts", m3u_account=self.account_a
        )
        self.stream_b = Stream.objects.create(
            name="News B", url="http://b.example/live/u/p/1.ts", m3u_account=self.account_b
        )
        self.channel = Channel.objects.create(channel_number=900, name="News")
        ChannelStream.objects.create(channel=self.channel, stream=self.stream_a, order=0)
        ChannelStream.objects.create(channel=self.channel, stream=self.stream_b, order=1)

        patcher = patch("apps.channels.models.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.stream_profile = MagicMock()
        self.stream_profile.is_redirect.return_value = False
        patcher = patch.object(Channel, "get_stream_profile", return_value=self.stream_profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fill_both(self):
        self.redis.set(profile_connections_key(self.profile_a.id), 1)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

    def _set_props(self, account, **props):
        account.custom_properties = {**account.custom_properties, **props}
        account.save()

    def _watching(self, profile, ip=IP, user_id="0", device_id=None, channel_uuid="old-channel"):
        self.redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile.id},
        )
        self.redis.sadd(RedisKeys.clients(channel_uuid), "client_1")
        client = {"ip_address": ip, "user_agent": "TiviMate/5.1", "user_id": user_id}
        if device_id:
            client[probation.DEVICE_ID_FIELD] = device_id
        self.redis.hset(RedisKeys.client_metadata(channel_uuid, "client_1"), mapping=client)

    def _deadline_seconds(self):
        record = self.redis.hgetall(probation.probation_key(self.channel.uuid))
        return round(float(record["deadline"]) - float(record["started_at"]))

    def _assert_probation_on(self, result, stream, profile):
        self.assertEqual(result, (stream.id, profile.id, None, True))
        self.assertEqual(self.redis.get(profile_connections_key(profile.id)), "2")
        record = self.redis.hgetall(probation.probation_key(self.channel.uuid))
        self.assertEqual(record["profile_id"], str(profile.id))

    def _assert_limit_error(self, result):
        stream_id, _profile_id, error, _reserved = result
        self.assertIsNone(stream_id)
        self.assertIn("maximum connection limits", error)
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "1")
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_b.id)), "1")

    def test_user_switching_starts_on_probation_on_its_profile(self, _preempt):
        self._fill_both()
        self._watching(self.profile_b, user_id="7")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=7))

        self._assert_probation_on(result, self.stream_b, self.profile_b)
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "1")
        self.assertEqual(self._deadline_seconds(), 25)

    def test_device_switching_starts_on_probation(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self._assert_probation_on(result, self.stream_a, self.profile_a)
        self.assertEqual(self._deadline_seconds(), probation.DEFAULT_PROBATION_SECONDS)

    def test_other_device_behind_same_ip_gets_limit_error(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")

        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="phone"))
        )

    def test_other_user_behind_same_ip_gets_limit_error(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, user_id="7")

        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=8))
        )

    def test_same_user_from_other_ip_gets_limit_error(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, ip="10.0.0.5", user_id="7")

        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=7))
        )

    def test_anonymous_viewer_needs_account_opt_in(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a)

        self._assert_limit_error(self.channel.get_stream(viewer=probation.Viewer(self.IP)))

        self._set_props(self.account_a, probation_allow_anonymous=True)
        result = self.channel.get_stream(viewer=probation.Viewer(self.IP))

        self._assert_probation_on(result, self.stream_a, self.profile_a)

    def test_anonymous_viewer_does_not_match_identified_client(self, _preempt):
        self._set_props(self.account_a, probation_allow_anonymous=True)
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")

        self._assert_limit_error(self.channel.get_stream(viewer=probation.Viewer(self.IP)))

    def test_retries_log_not_used_once(self, _preempt):
        probation._not_used_logged.clear()
        self.addCleanup(probation._not_used_logged.clear)
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")
        viewer = probation.Viewer(self.IP, device_id="phone")

        with self.assertLogs("live_proxy", level="INFO") as logs:
            for _ in range(3):
                self._assert_limit_error(self.channel.get_stream(viewer=viewer))

        not_used = [line for line in logs.output if "Probation: not used" in line]
        self.assertEqual(len(not_used), 1, logs.output)

    def test_no_viewer_never_uses_probation(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a)

        self._assert_limit_error(self.channel.get_stream())

    def test_disabled_accounts_do_no_probation_work_and_log_nothing(self, _preempt):
        self._set_props(self.account_a, probation_enabled=False)
        self._set_props(self.account_b, probation_enabled=False)
        self._fill_both()
        self._watching(self.profile_a, user_id="7")

        with patch.object(probation, "find_profile_ids_watched_by") as mock_find, \
                patch.object(probation, "log_not_used") as mock_log_not_used, \
                patch("apps.channels.models.logger") as mock_logger:
            for viewer in (probation.Viewer(self.IP, user_id=7), probation.Viewer(self.IP)):
                self._assert_limit_error(self.channel.get_stream(viewer=viewer))

        mock_find.assert_not_called()
        mock_log_not_used.assert_not_called()
        self.stream_profile.is_redirect.assert_not_called()
        logged = [str(call) for call in mock_logger.info.call_args_list]
        self.assertFalse([line for line in logged if "Probation" in line], logged)

    def test_watching_on_account_without_probation_gets_limit_error(self, _preempt):
        self._set_props(self.account_a, probation_enabled=False)
        self._fill_both()
        self._watching(self.profile_a, user_id="7")

        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=7))
        )

    def test_redirect_profiles_never_use_probation(self, _preempt):
        self.stream_profile.is_redirect.return_value = True
        self._fill_both()
        self._watching(self.profile_a, user_id="7")

        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=7))
        )

    def test_free_capacity_is_used_without_probation(self, _preempt):
        self.redis.set(profile_connections_key(self.profile_a.id), 1)
        self._watching(self.profile_a, user_id="7")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=7))

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))

    # ── Stay On Same Account ────────────────────────────────────────────────

    def _count(self, profile):
        return self.redis.get(profile_connections_key(profile.id))

    def test_sticky_uses_overlap_on_watched_account_even_if_other_is_free(self, _preempt):
        self._set_props(self.account_b, probation_account_preference="same")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self._watching(self.profile_b, device_id="tv1")

        with self.assertLogs("live_proxy", level="INFO") as logs:
            result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertEqual(self._count(self.profile_b), "2")
        self.assertIsNone(self._count(self.profile_a))
        self.assertTrue(probation.has_pending_probation(self.redis, self.channel.uuid))
        self.assertTrue(any("stay on same account" in line for line in logs.output))

    def test_sticky_uses_free_slot_on_watched_account(self, _preempt):
        self._set_props(self.account_b, probation_account_preference="same")
        self.profile_b.max_streams = 2
        self.profile_b.save()
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self._watching(self.profile_b, user_id="7")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, user_id=7))

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertEqual(self._count(self.profile_b), "2")
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))

    def test_sticky_uses_account_viewer_just_left(self, _preempt):
        self._set_props(self.account_b, probation_account_preference="same")
        viewer = probation.Viewer(self.IP, device_id="tv1")
        probation.remember_viewer_profile(self.redis, viewer, self.profile_b, self.account_b)

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertEqual(self._count(self.profile_b), "1")

    def test_sticky_never_overlaps_account_viewer_only_left(self, _preempt):
        self._set_props(self.account_b, probation_account_preference="same")
        viewer = probation.Viewer(self.IP, device_id="tv1")
        probation.remember_viewer_profile(self.redis, viewer, self.profile_b, self.account_b)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)  # someone else took it

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result, (self.stream_a.id, self.profile_a.id, None, True))
        self.assertEqual(self._count(self.profile_b), "1")

    def test_sticky_disabled_keeps_stock_order(self, _preempt):
        self._watching(self.profile_b, device_id="tv1")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(result, (self.stream_a.id, self.profile_a.id, None, True))
        self.assertIsNone(self.redis.get(probation._last_profile_key(probation.Viewer(self.IP, device_id="tv1"))))

    def test_sticky_anonymous_needs_account_opt_in(self, _preempt):
        self._set_props(self.account_b, probation_account_preference="same")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self._watching(self.profile_b)
        viewer = probation.Viewer(self.IP)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

        self.redis.set(profile_connections_key(self.profile_a.id), 0)
        self.redis.delete(f"channel_stream:{self.channel.id}")
        self._set_props(self.account_b, probation_allow_anonymous=True)
        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_b.id)

    def test_sticky_ignores_redirect_profiles(self, _preempt):
        self.stream_profile.is_redirect.return_value = True
        self._set_props(self.account_b, probation_account_preference="same")
        self._watching(self.profile_b, device_id="tv1")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(result[1], self.profile_a.id)

    def test_assignment_is_remembered_only_on_sticky_accounts(self, _preempt):
        viewer = probation.Viewer(self.IP, device_id="tv1")

        self.channel.get_stream(viewer=viewer)
        self.assertIsNone(self.redis.get(probation._last_profile_key(viewer)))

        self.redis.delete(f"channel_stream:{self.channel.id}")
        self.redis.set(profile_connections_key(self.profile_a.id), 0)
        self._set_props(self.account_a, probation_account_preference="same")
        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)
        self.assertEqual(self.redis.get(probation._last_profile_key(viewer)), str(self.profile_a.id))

    # ── Use another account ─────────────────────────────────────────────────

    def test_alternate_starts_on_other_account_after_old_one_was_released(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, device_id="tv1")
        probation.remember_viewer_profile(self.redis, viewer, self.profile_a, self.account_a)

        with self.assertLogs("live_proxy", level="INFO") as logs:
            result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertIsNone(self._count(self.profile_a))
        self.assertTrue(any("use another account" in line for line in logs.output))

    def test_order_preference_returns_to_the_same_account(self, _preempt):
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

    def test_alternate_releases_the_hold_it_leaves_behind(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result[1], self.profile_b.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    def test_alternate_falls_back_to_own_held_slot_when_others_are_full(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    def test_alternate_falls_back_to_overlap_when_others_are_full(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self._count(self.profile_a), "2")

    def _add_fallback_stream(self):
        """Like the could-not-dispatch plugin: a custom stream on an unlimited account, last."""
        # Overlap enabled on purpose: custom streams must be skipped even then
        account, _profile = _make_account("custom-fallback", max_streams=0, probation_enabled=True)
        stream = Stream.objects.create(
            name="Fallback slate", url="http://127.0.0.1:9999/slate.ts", m3u_account=account, is_custom=True
        )
        ChannelStream.objects.create(channel=self.channel, stream=stream, order=99)
        return stream

    def test_alternate_never_picks_a_custom_fallback_stream(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        fallback = self._add_fallback_stream()
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

        result = self.channel.get_stream(viewer=viewer)

        self.assertNotEqual(result[0], fallback.id)
        self.assertEqual(result[1], self.profile_a.id)

    def test_alternate_ignores_accounts_without_overlap(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        self._set_props(self.account_b, probation_enabled=False)
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

    def test_alternate_depends_on_the_account_being_left(self, _preempt):
        # Only account B alternates; the viewer leaves account A, which follows channel order
        self._set_props(self.account_b, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

    def test_alternate_only_applies_to_the_viewer_that_left(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        probation.remember_viewer_profile(
            self.redis, probation.Viewer(self.IP, device_id="tv1"), self.profile_a, self.account_a
        )

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="phone"))

        self.assertEqual(result[1], self.profile_a.id)

    # ── Held slots ──────────────────────────────────────────────────────────

    def _hold(self, profile, viewer, seconds=10):
        self.redis.hset(
            probation._held_slots_key(profile.id),
            probation._viewer_key(viewer),
            str(time.time() + seconds),
        )

    def test_held_slot_is_taken_for_other_viewers(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, device_id="tv1"))

        result = self.channel.get_stream(viewer=probation.Viewer("192.168.1.99", device_id="phone"))

        self.assertEqual(result[1], self.profile_b.id)
        self.assertEqual(self._count(self.profile_a), "0")

    def test_held_slot_is_taken_for_requests_without_viewer(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(self.channel.get_stream()[1], self.profile_b.id)

    def test_viewer_gets_its_held_slot_back(self, _preempt):
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self._hold(self.profile_a, viewer)

        with self.assertLogs("live_proxy", level="INFO") as logs:
            result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})
        self.assertTrue(any("took its held slot" in line for line in logs.output))

    def test_expired_hold_is_ignored(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, device_id="tv1"), seconds=-1)

        result = self.channel.get_stream(viewer=probation.Viewer("192.168.1.99", device_id="phone"))

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    def test_holds_are_ignored_on_accounts_without_overlap(self, _preempt):
        self._set_props(self.account_a, probation_enabled=False)
        self._hold(self.profile_a, probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(self.channel.get_stream()[1], self.profile_a.id)

    def test_release_holds_slot_for_its_single_viewer(self, _preempt):
        self._watching(self.profile_a, device_id="tv1", channel_uuid=str(self.channel.uuid))
        self.redis.set(f"channel_stream:{self.channel.id}", self.stream_a.id)
        self.redis.set(f"stream_profile:{self.stream_a.id}", self.profile_a.id)
        self.redis.set(profile_connections_key(self.profile_a.id), 1)

        with self.assertLogs("live_proxy", level="INFO"):
            self.assertTrue(self.channel.release_stream())

        self.assertEqual(self._count(self.profile_a), "0")
        held = self.redis.hgetall(probation._held_slots_key(self.profile_a.id))
        self.assertEqual(list(held), [probation._viewer_key(probation.Viewer(self.IP, device_id="tv1"))])

    def test_no_hold_without_identity_multiple_viewers_or_no_hold_marker(self, _preempt):
        uuid = str(self.channel.uuid)

        def release_with(**setup):
            self.redis = FakeRedis()
            for client_id, device_id in setup.get("clients", []):
                self.redis.hset(RedisKeys.channel_metadata(uuid), mapping={ChannelMetadataField.M3U_PROFILE: self.profile_a.id})
                self.redis.sadd(RedisKeys.clients(uuid), client_id)
                self.redis.hset(
                    RedisKeys.client_metadata(uuid, client_id),
                    mapping={"ip_address": self.IP, "user_id": "0", "device_id": device_id or ""},
                )
            if setup.get("no_hold"):
                self.redis.setex(probation.NO_HOLD_KEY.format(channel_uuid=uuid), 30, "1")
            probation.hold_slot_for_viewer(self.redis, uuid, self.profile_a.id)
            return self.redis.hgetall(probation._held_slots_key(self.profile_a.id))

        self.assertEqual(release_with(clients=[("c1", None)]), {})  # anonymous, not allowed
        self.assertEqual(release_with(clients=[("c1", "tv1"), ("c2", "tv2")]), {})
        self.assertEqual(release_with(clients=[("c1", "tv1")], no_hold=True), {})
        self._set_props(self.account_a, probation_enabled=False)
        self.assertEqual(release_with(clients=[("c1", "tv1")]), {})

    def test_only_one_probation_slot_per_profile(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, user_id="7")
        viewer = probation.Viewer(self.IP, user_id=7)

        _stream_id, profile_id, _error, _reserved = self.channel.get_stream(viewer=viewer)
        self.assertEqual(profile_id, self.profile_a.id)

        other = Channel.objects.create(channel_number=901, name="Sports")
        ChannelStream.objects.create(channel=other, stream=self.stream_a, order=0)
        ChannelStream.objects.create(channel=other, stream=self.stream_b, order=1)
        stream_id, _profile_id, error, _reserved = other.get_stream(viewer=viewer)

        self.assertIsNone(stream_id)
        self.assertIn("maximum connection limits", error)
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "2")

    def test_holds_apply_to_every_way_of_getting_a_slot(self, _preempt):
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._hold(self.profile_a, viewer)

        # Failover, stream changes, plugins, VOD and timeshift use these checks
        self.assertFalse(pool_has_capacity_for_profile(self.profile_a, self.redis))
        self.assertFalse(reserve_profile_slot(self.profile_a, self.redis)[0])
        # Stream previews
        self.assertIsNone(self.stream_a.get_stream()[0])
        self.assertEqual(self._count(self.profile_a), "0")
        # The viewer the slot is held for is not blocked
        self.assertTrue(pool_has_capacity_for_profile(self.profile_a, self.redis, viewer))

    def test_recording_never_uses_the_overlap_or_account_preferences(self, _preempt):
        for account in (self.account_a, self.account_b):
            self._set_props(
                account, probation_allow_anonymous=True, probation_account_preference="same"
            )
        self._fill_both()
        # A recording already runs on profile A
        self._watching(self.profile_a, ip="127.0.0.1")
        self.redis.hset(
            RedisKeys.client_metadata("old-channel", "client_1"),
            "user_agent",
            "Dispatcharr-DVR/recording-1",
        )

        # A second recording is not a channel switch
        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer("127.0.0.1", recording=True))
        )
        # and an anonymous player on the same address is not watching the recording
        with self.assertLogs("live_proxy", level="INFO"):
            self._assert_limit_error(self.channel.get_stream(viewer=probation.Viewer("127.0.0.1")))

    def test_recording_ignores_held_slots(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, device_id="tv1"))

        result = self.channel.get_stream(viewer=probation.Viewer("127.0.0.1", recording=True))

        self.assertEqual(result[1], self.profile_a.id)

    def test_no_hold_when_stopped_on_purpose_or_recording(self, _preempt):
        uuid = str(self.channel.uuid)
        viewer = probation.Viewer(self.IP, device_id="tv1")

        def release_with(setup):
            self.redis = FakeRedis()
            self._watching(self.profile_a, device_id="tv1", channel_uuid=uuid)
            setup(self.redis)
            probation.hold_slot_for_viewer(self.redis, uuid, self.profile_a.id)
            return self.redis.hgetall(probation._held_slots_key(self.profile_a.id))

        # Stopped from the dashboard, channel deleted, stream removed by an M3U refresh
        self.assertEqual(
            release_with(lambda r: r.setex(RedisKeys.channel_stopping(uuid), 60, "true")), {}
        )
        self.assertEqual(
            release_with(
                lambda r: r.hset(
                    RedisKeys.channel_metadata(uuid), ChannelMetadataField.STATE, ChannelState.STOPPING
                )
            ),
            {},
        )
        # Client disconnected from the dashboard
        self.assertEqual(
            release_with(lambda r: r.setex(RedisKeys.client_stop(uuid, "client_1"), 30, "true")), {}
        )
        # Only client is a recording
        self.assertEqual(
            release_with(
                lambda r: r.hset(
                    RedisKeys.client_metadata(uuid, "client_1"), "user_agent", "Dispatcharr-DVR/recording-3"
                )
            ),
            {},
        )
        # A viewer leaving still gets its hold
        with self.assertLogs("live_proxy", level="INFO"):
            self.assertEqual(list(release_with(lambda r: None)), [probation._viewer_key(viewer)])

    def _idle_channel(self, profile, viewers, channel_uuid="idle-channel"):
        """A channel whose clients all left; it waits out the Channel Shutdown Delay."""
        self.redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile.id, ChannelMetadataField.STATE: "active"},
        )
        self.redis.setex(RedisKeys.last_client_disconnect(channel_uuid), 60, str(time.time()))
        for viewer in viewers:
            self.redis.sadd(
                probation.CHANNEL_VIEWERS_KEY.format(channel_uuid=channel_uuid),
                probation._viewer_key(viewer),
            )

    def test_channel_waiting_for_shutdown_delay_counts_for_its_viewer(self, _preempt):
        viewer = probation.Viewer(self.IP, device_id="tv1")
        self._fill_both()
        self._idle_channel(self.profile_a, [viewer])

        result = self.channel.get_stream(viewer=viewer)

        self._assert_probation_on(result, self.stream_a, self.profile_a)

    def test_channel_waiting_for_shutdown_delay_only_counts_for_its_only_viewer(self, _preempt):
        viewer = probation.Viewer(self.IP, device_id="tv1")
        other = probation.Viewer(self.IP, device_id="tv2")

        self._idle_channel(self.profile_a, [viewer, other])
        self.assertEqual(probation.find_profile_ids_watched_by(self.redis, viewer), [])

        self.redis = FakeRedis()
        self._idle_channel(self.profile_a, [viewer])
        self.assertEqual(probation.find_profile_ids_watched_by(self.redis, other), [])
        self.assertEqual(
            probation.find_profile_ids_watched_by(self.redis, probation.Viewer(self.IP, recording=True)), []
        )
        self.assertEqual(probation.find_profile_ids_watched_by(self.redis, viewer), [self.profile_a.id])
        # Not once it is being stopped
        self.redis.setex(RedisKeys.channel_stopping("idle-channel"), 60, "true")
        self.assertEqual(probation.find_profile_ids_watched_by(self.redis, viewer), [])

    def _add_custom_stream(self, order, name="Fallback slate"):
        custom_account, _profile = _make_account(f"custom-{order}", max_streams=0)
        stream = Stream.objects.create(
            name=name, url="http://127.0.0.1:9999/slate.ts", m3u_account=custom_account, is_custom=True
        )
        ChannelStream.objects.create(channel=self.channel, stream=stream, order=order)
        return stream

    def test_overlap_comes_before_a_custom_fallback_stream(self, _preempt):
        slate = self._add_custom_stream(order=99)
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self._assert_probation_on(result, self.stream_a, self.profile_a)
        self.assertNotEqual(result[0], slate.id)

    def test_custom_fallback_stream_is_used_when_the_overlap_does_not_apply(self, _preempt):
        slate = self._add_custom_stream(order=99)
        self._fill_both()
        self._watching(self.profile_a, device_id="tv1")

        # Another viewer, and a request without viewer, get the fallback as before
        with self.assertLogs("live_proxy", level="INFO"):
            other = self.channel.get_stream(viewer=probation.Viewer("192.168.1.99", device_id="phone"))
        self.assertEqual(other[0], slate.id)
        self.assertEqual(self.channel.get_stream()[0], slate.id)
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))

    def test_custom_stream_first_in_order_is_still_used_first(self, _preempt):
        ChannelStream.objects.filter(channel=self.channel).update(order=5)
        custom = self._add_custom_stream(order=0, name="My own stream")
        self._watching(self.profile_a, device_id="tv1")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, device_id="tv1"))

        self.assertEqual(result[0], custom.id)


class ResolveProbationTests(TestCase):
    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account_a, self.profile_a = _make_account("resolve-a")
        self.account_b, self.profile_b = _make_account("resolve-b")
        self.stream_a = Stream.objects.create(
            name="Movies A", url="http://a.example/live/u/p/2.ts", m3u_account=self.account_a
        )
        self.stream_b = Stream.objects.create(
            name="Movies B", url="http://b.example/live/u/p/2.ts", m3u_account=self.account_b
        )
        self.channel = Channel.objects.create(channel_number=950, name="Movies")
        ChannelStream.objects.create(channel=self.channel, stream=self.stream_a, order=0)
        ChannelStream.objects.create(channel=self.channel, stream=self.stream_b, order=1)
        self.uuid = str(self.channel.uuid)

        # Channel is on probation: profile A holds the old stream plus this one.
        self.redis.set(profile_connections_key(self.profile_a.id), 2)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self.redis.set(f"channel_stream:{self.channel.id}", self.stream_a.id)
        self.redis.set(f"stream_profile:{self.stream_a.id}", self.profile_a.id)
        probation.mark_probation(self.redis, self.channel, self.stream_a.id, self.profile_a.id, seconds=10)
        self.deadline = float(self.redis.hgetall(probation.probation_key(self.uuid))["deadline"])

    def test_pending_while_over_limit_inside_window(self):
        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline - 5)

        self.assertEqual(outcome, probation.PENDING)
        self.assertTrue(probation.has_pending_probation(self.redis, self.uuid))

    def test_confirmed_when_another_stream_on_profile_ends(self):
        self.redis.set(profile_connections_key(self.profile_a.id), 1)

        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline - 5)

        self.assertEqual(outcome, probation.CONFIRMED)
        self.assertFalse(probation.has_pending_probation(self.redis, self.uuid))

    def test_ended_when_channel_released(self):
        self.redis.delete(f"channel_stream:{self.channel.id}")

        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1)

        self.assertEqual(outcome, probation.ENDED)
        self.assertFalse(probation.has_pending_probation(self.redis, self.uuid))

    def test_gone_without_record(self):
        self.redis.delete(probation.probation_key(self.uuid))

        self.assertEqual(probation.resolve_probation(self.uuid, self.redis), probation.GONE)

    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.change_stream_url")
    @patch("apps.proxy.live_proxy.url_utils.get_stream_info_for_switch")
    def test_migrates_to_profile_that_freed_up(self, mock_info, mock_change, mock_stop):
        self.redis.set(profile_connections_key(self.profile_b.id), 0)
        mock_info.return_value = {
            "url": "http://b.example/live/u/p/2.ts",
            "user_agent": "UA",
            "stream_name": "Movies B",
        }
        mock_change.return_value = {"status": "success", "success": True}

        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1)

        self.assertEqual(outcome, probation.MIGRATED)
        mock_info.assert_called_once_with(self.uuid, self.stream_b.id, self.profile_b.id)
        mock_change.assert_called_once_with(
            self.uuid, "http://b.example/live/u/p/2.ts", "UA", self.stream_b.id, self.profile_b.id, "Movies B"
        )
        mock_stop.assert_not_called()
        self.assertFalse(probation.has_pending_probation(self.redis, self.uuid))

    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.change_stream_url")
    @patch("apps.proxy.live_proxy.url_utils.get_stream_info_for_switch")
    def test_custom_fallback_stream_is_the_last_resort(self, mock_info, mock_change, mock_stop):
        fallback_account, _fallback_profile = _make_account("custom-fallback", max_streams=0)
        fallback = Stream.objects.create(
            name="Fallback slate", url="http://127.0.0.1:9999/slate.ts", m3u_account=fallback_account, is_custom=True
        )
        # Even placed before a provider stream, the fallback is only used when nothing else is free
        ChannelStream.objects.filter(channel=self.channel, stream=self.stream_b).update(order=5)
        ChannelStream.objects.create(channel=self.channel, stream=fallback, order=1)
        mock_info.side_effect = lambda _uuid, stream_id, _profile_id: {"url": f"http://{stream_id}", "user_agent": "UA"}
        mock_change.return_value = {"status": "success", "success": True}

        self.redis.set(profile_connections_key(self.profile_b.id), 0)
        self.assertEqual(
            probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1), probation.MIGRATED
        )
        self.assertEqual(mock_change.call_args.args[3], self.stream_b.id)

        # No provider profile free: the fallback, instead of stopping the channel
        mock_change.reset_mock()
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        probation.mark_probation(self.redis, self.channel, self.stream_a.id, self.profile_a.id, seconds=10)
        self.assertEqual(
            probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 11), probation.MIGRATED
        )
        self.assertEqual(mock_change.call_args.args[3], fallback.id)
        mock_stop.assert_not_called()

    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.change_stream_url")
    def test_failed_stop_is_retried_then_given_up(self, mock_change, mock_stop):
        mock_stop.side_effect = RuntimeError("redis went away")

        for _attempt in range(probation.MAX_RESOLVE_ATTEMPTS):
            with self.assertRaises(RuntimeError):
                probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1)
            # Kept, so a worker can try again
            self.assertTrue(probation.has_pending_probation(self.redis, self.uuid))
            self.assertIn(self.uuid, self.redis.smembers(probation.PENDING_PROBATIONS_KEY))

        with self.assertLogs("live_proxy", level="ERROR"):
            outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1)
        self.assertEqual(outcome, probation.FAILED)
        self.assertFalse(probation.has_pending_probation(self.redis, self.uuid))
        self.assertEqual(self.redis.smembers(probation.PENDING_PROBATIONS_KEY), set())

    @patch("apps.proxy.live_proxy.probation.gevent.spawn")
    def test_probation_without_a_running_monitor_is_resumed_by_one_worker(self, mock_spawn):
        # Just marked: the monitor is about to start, nobody takes it over
        probation.recover_unmonitored_probations(self.redis)
        mock_spawn.assert_not_called()

        # The worker running the monitor restarted: its lease ran out
        self.redis.delete(probation._monitor_lease_key(self.uuid))
        with self.assertLogs("live_proxy", level="WARNING"):
            probation.recover_unmonitored_probations(self.redis)
        mock_spawn.assert_called_once()
        self.assertEqual(mock_spawn.call_args.args[:2], (probation._monitor, self.uuid))
        token = mock_spawn.call_args.args[2]
        self.assertEqual(self.redis.get(probation._monitor_lease_key(self.uuid)), token)

        # Another worker sees the new lease and leaves it alone
        probation.recover_unmonitored_probations(self.redis)
        mock_spawn.assert_called_once()

        # A record that expired is forgotten
        self.redis.delete(probation.probation_key(self.uuid), probation._monitor_lease_key(self.uuid))
        probation.recover_unmonitored_probations(self.redis)
        mock_spawn.assert_called_once()
        self.assertEqual(self.redis.smembers(probation.PENDING_PROBATIONS_KEY), set())

    @patch("apps.proxy.live_proxy.probation.resolve_probation", return_value=probation.PENDING)
    def test_monitor_stops_when_another_worker_took_over(self, mock_resolve):
        self.redis.set(probation._monitor_lease_key(self.uuid), "other-worker")

        with patch("core.utils.RedisClient.get_client", return_value=self.redis), patch(
            "django.db.close_old_connections"
        ), self.assertLogs("live_proxy", level="INFO"):
            probation._monitor(self.uuid, "my-token")

        mock_resolve.assert_not_called()

    @patch("apps.proxy.live_proxy.probation.gevent.sleep")
    def test_monitor_renews_its_lease_while_pending(self, _sleep):
        outcomes = iter([probation.PENDING, probation.CONFIRMED])
        leases = []

        def resolve(channel_uuid, redis_client):
            leases.append(redis_client.get(probation._monitor_lease_key(channel_uuid)))
            return next(outcomes)

        with patch("core.utils.RedisClient.get_client", return_value=self.redis), patch(
            "django.db.close_old_connections"
        ), patch.object(probation, "resolve_probation", side_effect=resolve), patch(
            "apps.proxy.live_proxy.probation.gevent.spawn", side_effect=lambda fn, *args: fn(*args)
        ), self.assertLogs("live_proxy", level="INFO"):
            probation.start_probation_monitor(self.uuid, self.redis)

        # The lease mark_probation() set while starting is replaced by this monitor's token
        self.assertEqual(len(set(leases)), 1)
        self.assertEqual(len(leases), 2)
        self.assertNotEqual(leases[0], "starting")

    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.change_stream_url")
    def test_stops_probation_channel_when_no_capacity(self, mock_change, mock_stop):
        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1)

        self.assertEqual(outcome, probation.STOPPED)
        mock_change.assert_not_called()
        mock_stop.assert_called_once_with(self.uuid)
        self.assertFalse(probation.has_pending_probation(self.redis, self.uuid))

    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.change_stream_url")
    @patch("apps.proxy.live_proxy.url_utils.get_stream_info_for_switch")
    def test_stops_when_migration_fails(self, mock_info, mock_change, mock_stop):
        self.redis.set(profile_connections_key(self.profile_b.id), 0)
        mock_info.return_value = {"url": "http://b", "user_agent": "UA"}
        mock_change.return_value = {"status": "success", "success": False}

        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline + 1)

        self.assertEqual(outcome, probation.STOPPED)
        mock_stop.assert_called_once_with(self.uuid)

    def _old_channel(self, number, clients=(), disconnected=True):
        """The channel this switch came from, on profile A."""
        channel = Channel.objects.create(channel_number=number, name=f"Old {number}")
        stream = Stream.objects.create(
            name=f"Old {number}", url=f"http://a.example/live/u/p/{number}.ts", m3u_account=self.account_a
        )
        uuid = str(channel.uuid)
        self.redis.hset(
            RedisKeys.channel_metadata(uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: self.profile_a.id, ChannelMetadataField.STATE: "active"},
        )
        self.redis.set(f"channel_stream:{channel.id}", stream.id)
        self.redis.set(f"stream_profile:{stream.id}", self.profile_a.id)
        for client_id in clients:
            self.redis.sadd(RedisKeys.clients(uuid), client_id)
        if disconnected:
            self.redis.setex(RedisKeys.last_client_disconnect(uuid), 60, str(time.time()))
        return uuid

    @patch("apps.proxy.live_proxy.probation.gevent.spawn")
    def test_channel_left_during_shutdown_delay_is_stopped_and_switch_confirmed(self, mock_spawn):
        old_uuid = self._old_channel(951)

        with patch("apps.channels.models.RedisClient.get_client", return_value=self.redis):
            with self.assertLogs("live_proxy", level="INFO") as logs:
                outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline - 9)

        self.assertEqual(outcome, probation.CONFIRMED)
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "1")
        mock_spawn.assert_called_once_with(probation._stop_channel, old_uuid)
        self.assertTrue(any("Channel Shutdown Delay" in line for line in logs.output))
        # Nobody left it during a switch, so its slot is not held
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    @patch("apps.proxy.live_proxy.probation.gevent.spawn")
    def test_channels_with_viewers_or_still_starting_are_not_stopped(self, mock_spawn):
        self._old_channel(952, clients=["c1"])
        self._old_channel(953, disconnected=False)
        # Restarted within a minute: a disconnect time from its earlier run is still there
        restarted = self._old_channel(954)
        self.redis.hset(
            RedisKeys.channel_metadata(restarted), ChannelMetadataField.STATE, ChannelState.CONNECTING
        )

        outcome = probation.resolve_probation(self.uuid, self.redis, now=self.deadline - 5)

        self.assertEqual(outcome, probation.PENDING)
        mock_spawn.assert_not_called()


class ServerGroupHeldSlotTests(TestCase):
    """Held slots also cover the login that accounts in a Server Group share."""

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        group = ServerGroup.objects.create(name="shared-login")
        self.profiles = []
        for name in ("login-a", "login-b"):
            account = M3UAccount.objects.create(
                name=name,
                account_type="XC",
                username="user",
                password="pass",
                server_url="http://xc.example.com",
                server_group=group,
                max_streams=1,
                custom_properties={"probation_enabled": True},
            )
            self.profiles.append(M3UAccountProfile.objects.get(m3u_account=account, is_default=True))
        self.viewer = probation.Viewer("192.168.1.20", device_id="tv1")

    def _login_holds(self):
        return {
            key: value
            for key, value in self.redis.hashes.items()
            if key.startswith("live:probation:held_login:") and value
        }

    def test_hold_covers_the_shared_login(self):
        profile_a, profile_b = self.profiles
        # The viewer's channel on account A ends during a switch
        self.assertTrue(reserve_profile_slot(profile_a, self.redis)[0])
        uuid = "old-channel"
        self.redis.sadd(RedisKeys.clients(uuid), "c1")
        self.redis.hset(
            RedisKeys.client_metadata(uuid, "c1"),
            mapping={"ip_address": self.viewer.ip, "user_id": "0", "device_id": "tv1"},
        )
        with self.assertLogs("live_proxy", level="INFO"):
            probation.hold_slot_for_viewer(self.redis, uuid, profile_a.id)
        release_profile_slot(profile_a.id, self.redis)
        self.assertEqual(len(self._login_holds()), 1)

        # Account B has a free profile slot but shares the login: the held slot stays taken
        self.assertEqual(reserve_profile_slot(profile_b, self.redis)[::2], (False, "credential_full"))
        self.assertFalse(pool_has_capacity_for_profile(profile_b, self.redis))

        # The viewer gets it, which uses up both holds
        self.assertTrue(reserve_profile_slot(profile_b, self.redis, viewer=self.viewer)[0])
        with self.assertLogs("live_proxy", level="INFO"):
            probation.take_held_slot(self.redis, profile_b, self.viewer)
        self.assertEqual(self._login_holds(), {})
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(profile_a.id)), {})


class FeatureNotInUseTests(TestCase):
    """With the overlap off on every account, nothing extra is queried, stored or held."""

    IP = "192.168.1.20"

    def setUp(self):
        _reset_in_use_cache(self)
        self.account, self.profile = _make_account("stock-a")
        self.stream = Stream.objects.create(
            name="Stock", url="http://a.example/live/u/p/9.ts", m3u_account=self.account
        )
        self.channel = Channel.objects.create(channel_number=990, name="Stock")
        ChannelStream.objects.create(channel=self.channel, stream=self.stream, order=0)
        self.stream_profile = MagicMock()
        self.stream_profile.is_redirect.return_value = False
        patcher = patch.object(Channel, "get_stream_profile", return_value=self.stream_profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get_stream(self, viewer, full):
        redis = FakeRedis()
        if full:
            redis.set(profile_connections_key(self.profile.id), 1)
        with patch("apps.channels.models.RedisClient.get_client", return_value=redis), patch(
            "apps.channels.models.Channel._pick_channel_to_preempt", return_value=None
        ):
            with CaptureQueriesContext(connection) as queries:
                result = self.channel.get_stream(viewer=viewer)
        return result, len(queries), redis.strings, redis.hashes, redis.sets

    def test_get_stream_with_a_viewer_does_the_same_work_as_without(self):
        probation.in_use()  # warm the cache like a running server
        viewer = probation.Viewer(self.IP, device_id="tv1")
        for full in (False, True):
            self.assertEqual(self._get_stream(viewer, full), self._get_stream(None, full))

    def test_in_use_is_cached_until_an_account_changes(self):
        self.assertFalse(probation.in_use())
        with self.assertNumQueries(0):
            self.assertFalse(probation.in_use())

        self.account.custom_properties = {"probation_enabled": True}
        self.account.save()
        self.assertTrue(probation.in_use())

        self.account.delete()
        self.assertFalse(probation.in_use())

    def test_release_and_skipped_channels_do_nothing(self):
        redis = FakeRedis()
        uuid = "old-channel"
        redis.hset(RedisKeys.channel_metadata(uuid), mapping={ChannelMetadataField.M3U_PROFILE: self.profile.id})
        redis.sadd(RedisKeys.clients(uuid), "c1")
        redis.hset(
            RedisKeys.client_metadata(uuid, "c1"),
            mapping={"ip_address": self.IP, "user_id": "0", "device_id": "tv1", "connected_at": str(time.time())},
        )
        before = (dict(redis.strings), {k: dict(v) for k, v in redis.hashes.items()}, dict(redis.sets))
        viewer = probation.Viewer(self.IP, device_id="tv1")
        probation.in_use()

        with self.assertNumQueries(0):
            probation.hold_slot_for_viewer(redis, uuid, self.profile.id)
            self.assertEqual(probation.stop_skipped_channels(redis, viewer, "new-channel"), [])
            probation.record_client_viewer(redis, uuid, "c2", viewer)
            probation.take_held_slot(redis, self.profile, viewer)

        self.assertEqual((redis.strings, redis.hashes, redis.sets), before)


@patch("apps.proxy.live_proxy.probation.gevent.spawn", side_effect=lambda fn, *args: fn(*args))
@patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
class StopSkippedChannelsTests(TestCase):
    NOW = 10_000.0
    IP = "192.168.1.20"

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, self.profile = _make_account(
            "surf-account", probation_enabled=True, probation_seconds=10
        )
        self._set_props(self.account, probation_stop_skipped=True)
        self.viewer = probation.Viewer(self.IP, device_id="tv1")

        # The background stop closes DB connections when done; keep the test transaction open
        patcher = patch("django.db.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set_props(self, account, **props):
        account.custom_properties = {**account.custom_properties, **props}
        account.save()

    def _channel(self, channel_uuid, profile=None, clients=()):
        """clients: (client_id, ip, user_id, device_id, seconds_ago)"""
        profile = profile or self.profile
        self.redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile.id},
        )
        for client_id, ip, user_id, device_id, seconds_ago in clients:
            self.redis.sadd(RedisKeys.clients(channel_uuid), client_id)
            client = {
                "ip_address": ip,
                "user_id": user_id,
                "connected_at": str(self.NOW - seconds_ago),
            }
            if device_id:
                client[probation.DEVICE_ID_FIELD] = device_id
            self.redis.hset(RedisKeys.client_metadata(channel_uuid, client_id), mapping=client)

    def _stop(self, viewer=None, requested="channel-new"):
        return probation.stop_skipped_channels(
            self.redis, viewer or self.viewer, requested, now=self.NOW
        )

    def test_surfing_stops_skipped_channels_but_keeps_watched_one(self, mock_stop, _mock_spawn):
        self._channel("watched", clients=[("c1", self.IP, "0", "tv1", 600)])
        self._channel("skipped-1", clients=[("c2", self.IP, "0", "tv1", 2)])
        self._channel("skipped-2", clients=[("c3", self.IP, "0", "tv1", 1)])

        with self.assertLogs("live_proxy", level="INFO") as logs:
            stopped = self._stop()

        self.assertEqual(sorted(stopped), ["skipped-1", "skipped-2"])
        self.assertEqual(sorted(c.args[0] for c in mock_stop.call_args_list), ["skipped-1", "skipped-2"])
        self.assertTrue(any("Probation: stopping skipped channel" in line for line in logs.output))

    def test_requested_channel_is_never_stopped(self, mock_stop, _mock_spawn):
        self._channel("channel-new", clients=[("c1", self.IP, "0", "tv1", 1)])

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_shared_channel_is_never_stopped(self, mock_stop, _mock_spawn):
        self._channel(
            "shared",
            clients=[("c1", self.IP, "0", "tv1", 1), ("c2", "192.168.1.30", "0", "tv2", 300)],
        )

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_reconnect_on_watched_channel_does_not_count_as_skipped(self, mock_stop, _mock_spawn):
        self._channel(
            "watched",
            clients=[("c1", self.IP, "0", "tv1", 600), ("c2", self.IP, "0", "tv1", 1)],
        )

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_other_device_or_user_channels_are_never_stopped(self, mock_stop, _mock_spawn):
        self._channel("other-device", clients=[("c1", self.IP, "0", "phone", 1)])
        self._channel("other-user", clients=[("c2", self.IP, "8", None, 1)])
        self._channel("other-ip", clients=[("c3", "10.0.0.9", "0", "tv1", 1)])

        self.assertEqual(self._stop(), [])
        self.assertEqual(self._stop(viewer=probation.Viewer(self.IP, user_id=7)), [])
        mock_stop.assert_not_called()

    def test_anonymous_viewer_never_stops_anything(self, mock_stop, _mock_spawn):
        self._set_props(self.account, probation_allow_anonymous=True)
        self._channel("anonymous", clients=[("c1", self.IP, "0", None, 1)])

        with patch.object(probation, "any_account_stops_skipped_channels") as mock_any:
            self.assertEqual(self._stop(viewer=probation.Viewer(self.IP)), [])

        mock_any.assert_not_called()
        mock_stop.assert_not_called()

    def test_account_settings_are_required(self, mock_stop, _mock_spawn):
        self._channel("skipped", clients=[("c1", self.IP, "0", "tv1", 1)])

        self._set_props(self.account, probation_stop_skipped=False)
        with patch.object(self.redis, "scan_iter", wraps=self.redis.scan_iter) as mock_scan:
            self.assertEqual(self._stop(), [])
        mock_scan.assert_not_called()

        self._set_props(self.account, probation_stop_skipped=True, probation_enabled=False)
        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_only_accounts_with_the_option_are_affected(self, mock_stop, _mock_spawn):
        _other_account, other_profile = _make_account("no-stop", probation_enabled=True)
        self._channel("skipped", clients=[("c1", self.IP, "0", "tv1", 1)])
        self._channel("other", profile=other_profile, clients=[("c2", self.IP, "0", "tv1", 1)])

        self.assertEqual(self._stop(), ["skipped"])

    def test_stop_runs_in_background(self, mock_stop, mock_spawn):
        mock_spawn.side_effect = None
        self._channel("skipped", clients=[("c1", self.IP, "0", "tv1", 1)])

        self.assertEqual(self._stop(), ["skipped"])

        mock_stop.assert_not_called()
        mock_spawn.assert_called_once_with(probation._stop_channel, "skipped")

    def test_channel_already_being_stopped_is_not_stopped_again(self, mock_stop, _mock_spawn):
        self._channel("skipped", clients=[("c1", self.IP, "0", "tv1", 1)])

        self.assertEqual(self._stop(requested="channel-c"), ["skipped"])
        self.assertEqual(self._stop(requested="channel-d"), [])

        mock_stop.assert_called_once_with("skipped")

    def _real_skipped_channel(self):
        stream = Stream.objects.create(
            name="Skipped", url="http://a.example/live/u/p/9.ts", m3u_account=self.account
        )
        channel = Channel.objects.create(channel_number=990, name="Skipped")
        ChannelStream.objects.create(channel=channel, stream=stream, order=0)
        uuid = str(channel.uuid)
        self._channel(uuid, clients=[("c1", self.IP, "0", "tv1", 1)])
        self.redis.set(f"channel_stream:{channel.id}", stream.id)
        self.redis.set(f"stream_profile:{stream.id}", self.profile.id)
        self.redis.set(profile_connections_key(self.profile.id), 2)
        patcher = patch("apps.channels.models.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)
        return uuid

    def test_skipped_slot_is_released_immediately_without_hold(self, mock_stop, mock_spawn):
        mock_spawn.side_effect = None
        uuid = self._real_skipped_channel()

        self.assertEqual(self._stop(), [uuid])

        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "1")
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile.id)), {})
        mock_spawn.assert_called_once_with(probation._stop_channel, uuid)

    def test_skipped_slot_is_held_when_viewer_has_no_slot_yet(self, mock_stop, mock_spawn):
        mock_spawn.side_effect = None
        uuid = self._real_skipped_channel()

        with self.assertLogs("live_proxy", level="INFO"):
            probation.stop_skipped_channels(
                self.redis, self.viewer, "channel-new", now=self.NOW, hold_slots=True
            )

        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "1")
        self.assertEqual(
            list(self.redis.hgetall(probation._held_slots_key(self.profile.id))),
            [probation._viewer_key(self.viewer)],
        )

    def test_window_limits_what_counts_as_skipped(self, mock_stop, _mock_spawn):
        self._channel("inside", clients=[("c1", self.IP, "0", "tv1", 9)])
        self._channel("outside", clients=[("c2", self.IP, "0", "tv1", 11)])

        self.assertEqual(self._stop(), ["inside"])


class NotUsedLoggingTests(SimpleTestCase):
    def setUp(self):
        _reset_in_use_cache(self)
        probation._not_used_logged.clear()
        self.addCleanup(probation._not_used_logged.clear)

    @patch("apps.proxy.live_proxy.probation.time.monotonic")
    def test_repeated_reason_is_logged_once_per_interval(self, mock_monotonic):
        viewer = probation.Viewer("10.0.0.2", device_id="tv1")
        mock_monotonic.return_value = 100.0

        with self.assertLogs("live_proxy", level="INFO") as logs:
            for _ in range(14):
                probation.log_not_used("ch-1", viewer, "not watching")
            probation.log_not_used("ch-2", viewer, "not watching")
            probation.log_not_used("ch-1", probation.Viewer("10.0.0.3"), "not watching")
            mock_monotonic.return_value = 100.0 + probation.NOT_USED_LOG_INTERVAL
            probation.log_not_used("ch-1", viewer, "not watching")

        self.assertEqual(len(logs.output), 4)
        self.assertIn("Probation: not used for channel ch-1: not watching", logs.output[0])

    @patch("apps.proxy.live_proxy.probation.time.monotonic")
    def test_expired_entries_are_forgotten(self, mock_monotonic):
        mock_monotonic.return_value = 0.0
        with self.assertLogs("live_proxy", level="INFO"):
            probation.log_not_used("ch-1", probation.Viewer("10.0.0.2"), "reason")
            mock_monotonic.return_value = probation.NOT_USED_LOG_INTERVAL + 1.0
            probation.log_not_used("ch-2", probation.Viewer("10.0.0.2"), "reason")

        self.assertEqual(list(probation._not_used_logged), [("ch-2", probation.Viewer("10.0.0.2"), "reason")])


class ViewerIdentityTests(SimpleTestCase):
    def _add_client(self, redis, channel_uuid, profile_id, client_id, **client):
        redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile_id},
        )
        redis.sadd(RedisKeys.clients(channel_uuid), client_id)
        redis.hset(RedisKeys.client_metadata(channel_uuid, client_id), mapping=client)

    def test_find_profiles_matches_ip_user_and_device(self):
        redis = FakeRedis()
        self._add_client(redis, "ch-1", 41, "c1", ip_address="10.0.0.2", user_id="0")
        self._add_client(redis, "ch-2", 42, "c1", ip_address="10.0.0.2", user_id="7")
        self._add_client(
            redis, "ch-3", 43, "c1", ip_address="10.0.0.2", user_id="0", device_id="tv1"
        )
        self._add_client(redis, "ch-4", 44, "c1", ip_address="10.0.0.9", user_id="7")

        find = probation.find_profile_ids_watched_by
        self.assertEqual(find(redis, probation.Viewer("10.0.0.2")), [41])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.2", user_id=7)), [42])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.2", device_id="tv1")), [43])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.9", user_id=7)), [44])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.7", user_id=7)), [])
        self.assertEqual(find(redis, None), [])

    def test_viewer_from_request(self):
        factory = RequestFactory()
        user = MagicMock(id=5)

        viewer = probation.viewer_from_request(
            factory.get("/proxy/ts/stream/x", {"device_id": "living-room_1"}), user, "10.0.0.2"
        )
        self.assertEqual(viewer, probation.Viewer("10.0.0.2", user_id=5, device_id="living-room_1"))
        self.assertTrue(viewer.identified)

        viewer = probation.viewer_from_request(factory.get("/proxy/ts/stream/x"), None, "10.0.0.2")
        self.assertEqual(viewer, probation.Viewer("10.0.0.2"))
        self.assertFalse(viewer.identified)

        self.assertIsNone(probation.viewer_from_request(factory.get("/"), None, None))

    def test_recordings_are_recognised(self):
        request = RequestFactory().get(
            "/proxy/ts/stream/x", {"device_id": "tv1"}, HTTP_USER_AGENT="Dispatcharr-DVR/recording-12"
        )
        viewer = probation.viewer_from_request(request, None, "127.0.0.1")

        self.assertEqual(viewer, probation.Viewer("127.0.0.1", recording=True))
        self.assertFalse(probation.is_viewer_request(viewer))
        self.assertFalse(probation.is_recording("TiviMate/5.1"))

    def test_media_server_detection(self):
        for user_agent in ("Jellyfin-Server/10.10.7", "Emby/4.8.10.0", "PlexMediaServer/1.41.0"):
            self.assertTrue(probation.is_media_server(user_agent), user_agent)
        for user_agent in (None, "", "TiviMate/5.1.6", "VLC/3.0.20 LibVLC/3.0.20", "Kodi/21.0"):
            self.assertFalse(probation.is_media_server(user_agent), user_agent)

    def test_fill_device_id(self):
        placeholder = probation.DEVICE_ID_PLACEHOLDER
        content = (
            f"http://x/proxy/ts/stream/a?device_id={placeholder}\n"
            f"http://x/proxy/ts/stream/b?output_format=mpegts&device_id={placeholder}\n"
        )

        self.assertEqual(
            probation.fill_device_id(content, "tv1"),
            "http://x/proxy/ts/stream/a?device_id=tv1\n"
            "http://x/proxy/ts/stream/b?output_format=mpegts&device_id=tv1\n",
        )
        self.assertEqual(
            probation.fill_device_id(content, None),
            "http://x/proxy/ts/stream/a\nhttp://x/proxy/ts/stream/b?output_format=mpegts\n",
        )

    def test_media_server_stream_requests_stay_anonymous(self):
        request = RequestFactory().get(
            "/proxy/ts/stream/x", {"device_id": "tv1"}, HTTP_USER_AGENT="Jellyfin-Server/10.10.7"
        )

        viewer = probation.viewer_from_request(request, None, "10.0.0.2")

        self.assertEqual(viewer, probation.Viewer("10.0.0.2"))
        self.assertFalse(viewer.identified)

    def test_normalize_device_id(self):
        self.assertEqual(probation.normalize_device_id("tv-1_A"), "tv-1_A")
        for bad in (None, "", "has space", "a/b", "x" * 65, probation.DEVICE_ID_PLACEHOLDER + "!"):
            self.assertIsNone(probation.normalize_device_id(bad))
        self.assertEqual(len(probation.new_device_id()), 12)
        self.assertNotEqual(probation.new_device_id(), probation.new_device_id())

    @patch.object(probation, "in_use", return_value=True)
    def test_record_client_viewer(self, _in_use):
        redis = FakeRedis()
        probation.record_client_viewer(redis, "ch-1", "c1", probation.Viewer("10.0.0.2", device_id="tv1"))
        probation.record_client_viewer(redis, "ch-1", "c2", probation.Viewer("10.0.0.3"))
        probation.record_client_viewer(redis, "ch-2", "c3", probation.Viewer("127.0.0.1", recording=True))

        self.assertEqual(
            redis.hget(RedisKeys.client_metadata("ch-1", "c1"), probation.DEVICE_ID_FIELD), "tv1"
        )
        self.assertIsNone(redis.hgetall(RedisKeys.client_metadata("ch-1", "c2")).get("device_id"))
        self.assertEqual(
            redis.smembers(probation.CHANNEL_VIEWERS_KEY.format(channel_uuid="ch-1")),
            {"10.0.0.2|0|tv1", "10.0.0.3|0|"},
        )
        # Recordings are not viewers
        self.assertEqual(redis.smembers(probation.CHANNEL_VIEWERS_KEY.format(channel_uuid="ch-2")), set())

    @patch.object(probation, "in_use", return_value=False)
    def test_record_client_viewer_does_nothing_while_not_in_use(self, _in_use):
        redis = FakeRedis()
        probation.record_client_viewer(redis, "ch-1", "c1", probation.Viewer("10.0.0.2", device_id="tv1"))
        self.assertEqual((redis.strings, redis.hashes, redis.sets), ({}, {}, {}))


class AccountProbationSettingsTests(TestCase):
    def test_account_settings_defaults_and_bounds(self):
        account = MagicMock(custom_properties={})
        self.assertFalse(probation.account_allows_probation(account))
        self.assertFalse(probation.account_allows_anonymous(account))
        self.assertEqual(probation.account_probation_seconds(account), 10)

        self.assertFalse(probation.account_stops_skipped_channels(account))

        account.custom_properties = {
            "probation_enabled": True,
            "probation_seconds": 500,
            "probation_allow_anonymous": True,
            "probation_stop_skipped": True,
        }
        self.assertTrue(probation.account_allows_probation(account))
        self.assertTrue(probation.account_allows_anonymous(account))
        self.assertTrue(probation.account_stops_skipped_channels(account))
        self.assertFalse(probation.account_keeps_viewers(account))
        self.assertEqual(probation.account_switch_preference(account), "order")
        account.custom_properties["probation_sticky"] = True  # earlier builds
        self.assertTrue(probation.account_keeps_viewers(account))
        account.custom_properties["probation_account_preference"] = "alternate"
        self.assertTrue(probation.account_alternates_viewers(account))
        self.assertFalse(probation.account_keeps_viewers(account))
        account.custom_properties["probation_account_preference"] = "sideways"
        self.assertTrue(probation.account_keeps_viewers(account))
        self.assertEqual(probation.account_probation_seconds(account), 120)

        account.custom_properties = {"probation_enabled": "yes", "probation_seconds": "bad"}
        self.assertFalse(probation.account_allows_probation(account))
        self.assertEqual(probation.account_probation_seconds(account), 10)

    def test_serializer_round_trips_settings_in_custom_properties(self):
        account, _profile = _make_account("serializer-account")
        account.custom_properties = {**account.custom_properties, "enable_vod": True}
        account.save()

        serializer = M3UAccountSerializer(
            account,
            data={
                "probation_enabled": True,
                "probation_seconds": 30,
                "probation_allow_anonymous": True,
                "probation_stop_skipped": True,
                "probation_account_preference": "alternate",
            },
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        account.refresh_from_db()
        self.assertTrue(account.custom_properties["probation_enabled"])
        self.assertEqual(account.custom_properties["probation_seconds"], 30)
        self.assertTrue(account.custom_properties["probation_allow_anonymous"])
        self.assertTrue(account.custom_properties["enable_vod"])

        data = M3UAccountSerializer(account).data
        self.assertTrue(data["probation_enabled"])
        self.assertEqual(data["probation_seconds"], 30)
        self.assertTrue(data["probation_allow_anonymous"])
        self.assertTrue(data["probation_stop_skipped"])
        self.assertEqual(data["probation_account_preference"], "alternate")

    def test_any_account_allows_probation(self):
        self.assertFalse(probation.any_account_allows_probation())
        _make_account("enabled-account", probation_enabled=True)
        self.assertTrue(probation.any_account_allows_probation())

    def test_serializer_create_without_settings_stores_nothing(self):
        serializer = M3UAccountSerializer(
            data={
                "name": "created-without-overlap",
                "account_type": "STD",
                "server_url": "http://example.com/playlist.m3u",
                "max_streams": 1,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        account = serializer.save()

        account.refresh_from_db()
        self.assertFalse([k for k in account.custom_properties if k.startswith("probation")])
        self.assertFalse(probation.account_allows_probation(account))

    def test_serializer_replaces_legacy_stay_on_same_account(self):
        account, _profile = _make_account("legacy-sticky")
        account.custom_properties = {**account.custom_properties, "probation_sticky": True}
        account.save()
        self.assertEqual(M3UAccountSerializer(account).data["probation_account_preference"], "same")

        serializer = M3UAccountSerializer(
            account, data={"probation_account_preference": "order"}, partial=True
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        account.refresh_from_db()
        self.assertNotIn("probation_sticky", account.custom_properties)
        self.assertEqual(account.custom_properties["probation_account_preference"], "order")
        self.assertFalse(
            M3UAccountSerializer(account, data={"probation_account_preference": "x"}, partial=True).is_valid()
        )

    def test_serializer_rejects_out_of_range_window(self):
        account, _profile = _make_account("serializer-bounds")
        for seconds in (0, 121):
            serializer = M3UAccountSerializer(
                account, data={"probation_seconds": seconds}, partial=True
            )
            self.assertFalse(serializer.is_valid())


class StreamTsDeviceIdTests(SimpleTestCase):
    @patch("apps.proxy.live_proxy.views.close_old_connections")
    @patch("apps.proxy.live_proxy.views.create_stream_generator", return_value=lambda: iter([b""]))
    @patch("apps.proxy.live_proxy.views._resolve_output_format", return_value="mpegts")
    @patch("apps.proxy.live_proxy.views._resolve_output_profile", return_value=None)
    @patch(
        "apps.proxy.live_proxy.views.ChannelService.is_channel_unavailable_for_new_clients",
        return_value=False,
    )
    @patch("apps.proxy.live_proxy.views.get_stream_object")
    @patch("apps.proxy.live_proxy.views.network_access_allowed", return_value=True)
    @patch("apps.proxy.live_proxy.views.ProxyServer")
    @patch.object(probation, "in_use", return_value=True)
    def test_joining_client_records_device_id(self, _in_use, mock_proxy_cls, _network, mock_get_object, *_mocks):
        channel_id = "channel-uuid"
        channel = MagicMock(id=1, uuid=channel_id)
        channel.name = "Test Channel"
        channel.get_stream_profile.return_value.is_redirect.return_value = False
        mock_get_object.return_value = channel

        redis = FakeRedis()
        redis.hset(RedisKeys.channel_metadata(channel_id), mapping={"state": "active"})
        client_manager = MagicMock()
        client_manager.add_client.return_value = 1
        proxy_server = MagicMock(
            redis_client=redis,
            stream_buffers={channel_id: MagicMock()},
            client_managers={channel_id: client_manager},
        )
        mock_proxy_cls.get_instance.return_value = proxy_server

        from apps.proxy.live_proxy.views import stream_ts

        request = RequestFactory().get(f"/proxy/ts/stream/{channel_id}", {"device_id": "tv1"})
        request.user = MagicMock(is_authenticated=False)
        stream_ts(request, channel_id)

        client_id = client_manager.add_client.call_args.args[0]
        self.assertEqual(
            redis.hget(RedisKeys.client_metadata(channel_id, client_id), probation.DEVICE_ID_FIELD),
            "tv1",
        )


class StreamTsSkippedChannelOrderTests(SimpleTestCase):
    """stream_ts only stops skipped channels after trying for a slot (switching stays fast)."""

    CHANNEL_ID = "channel-uuid"
    OK = ("http://example/stream", "ua", False, "None", True, None, 42)
    FULL = (None, None, False, None, False, "All active M3U profiles have reached maximum connection limits", None)

    def _run(self, generate_results):
        from django.http import StreamingHttpResponse
        import gevent.lock

        calls = []
        self.calls = calls
        results = iter(generate_results)

        def generate(*_args, **_kwargs):
            calls.append("get slot")
            return next(results)

        def stop_skipped(_redis, _viewer, channel_id, hold_slots=False):
            calls.append(f"stop skipped (hold={hold_slots})")
            return []

        channel = MagicMock(id=1, uuid=self.CHANNEL_ID)
        channel.name = "Test Channel"
        channel.get_stream_profile.return_value.is_redirect.return_value = False

        proxy_server = MagicMock()
        proxy_server.redis_client.exists.return_value = False
        proxy_server.redis_client.get.return_value = None
        proxy_server.redis_client.hgetall.return_value = {}
        proxy_server.check_if_channel_exists.return_value = False
        proxy_server.try_acquire_ownership.return_value = True
        proxy_server.am_i_owner.return_value = True
        proxy_server._channels_setting_up = set()
        proxy_server.stream_buffers = {self.CHANNEL_ID: MagicMock()}
        client_manager = MagicMock()
        client_manager.add_client.return_value = 1
        proxy_server.client_managers = {self.CHANNEL_ID: client_manager}
        proxy_server._get_channel_init_lock.return_value = gevent.lock.RLock()
        proxy_server._finish_channel_init_lock.side_effect = lambda _cid, held: held.release()

        patches = [
            patch("apps.proxy.live_proxy.views.ProxyServer.get_instance", return_value=proxy_server),
            patch("apps.proxy.live_proxy.views.network_access_allowed", return_value=True),
            patch("apps.proxy.live_proxy.views.get_stream_object", return_value=channel),
            patch(
                "apps.proxy.live_proxy.views.ChannelService.is_channel_unavailable_for_new_clients",
                return_value=False,
            ),
            patch("apps.proxy.live_proxy.views._resolve_output_profile", return_value=None),
            patch("apps.proxy.live_proxy.views._resolve_output_format", return_value="mpegts"),
            patch("apps.proxy.live_proxy.views.ChannelService.initialize_channel", return_value=True),
            patch("apps.proxy.live_proxy.views.generate_stream_url", side_effect=generate),
            patch("apps.proxy.live_proxy.views.create_stream_generator", return_value=lambda: iter([b""])),
            patch("apps.proxy.live_proxy.views.close_old_connections"),
            patch("apps.proxy.live_proxy.views.gevent.sleep"),
            patch.object(probation, "stop_skipped_channels", side_effect=stop_skipped),
            patch.object(probation, "in_use", return_value=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        from apps.proxy.live_proxy.views import stream_ts

        request = RequestFactory().get(f"/proxy/ts/stream/{self.CHANNEL_ID}", {"device_id": "tv1"})
        request.user = MagicMock(is_authenticated=False)
        response = stream_ts(request, self.CHANNEL_ID)
        self.assertIsInstance(response, StreamingHttpResponse)
        return calls

    def test_skipped_channels_are_stopped_after_the_slot_is_taken(self):
        self.assertEqual(self._run([self.OK]), ["get slot", "stop skipped (hold=False)"])

    def test_waits_for_a_skipped_channel_to_stop_before_looking_at_it(self):
        with patch.object(
            probation,
            "wait_for_stop_to_finish",
            side_effect=lambda _redis, channel_id: self.calls.append(f"wait for stop {channel_id}"),
        ):
            calls = self._run([self.OK])

        self.assertEqual(
            calls, [f"wait for stop {self.CHANNEL_ID}", "get slot", "stop skipped (hold=False)"]
        )

    def test_no_free_slot_stops_skipped_channels_first_with_hold(self):
        self.assertEqual(
            self._run([self.FULL, self.OK]),
            ["get slot", "stop skipped (hold=True)", "get slot", "stop skipped (hold=False)"],
        )


@patch("apps.proxy.live_proxy.services.channel_service.ChannelService.is_channel_teardown_active", return_value=False)
@patch.object(probation, "in_use", return_value=True)
class WaitForStopTests(SimpleTestCase):
    """A player returning to a channel that is still being stopped as skipped."""

    UUID = "skipped-channel"

    def setUp(self):
        self.redis = FakeRedis()

    def _stopping(self):
        self.redis.setex(probation._skipped_stopping_key(self.UUID), 30, "1")

    @patch("apps.proxy.live_proxy.probation.gevent.sleep")
    def test_does_nothing_for_other_channels(self, mock_sleep, _in_use, _teardown):
        self.assertFalse(probation.wait_for_stop_to_finish(self.redis, self.UUID))
        mock_sleep.assert_not_called()

    @patch("apps.proxy.live_proxy.probation.gevent.sleep")
    def test_does_nothing_while_not_in_use(self, mock_sleep, in_use, _teardown):
        in_use.return_value = False
        self._stopping()
        self.assertFalse(probation.wait_for_stop_to_finish(self.redis, self.UUID))
        mock_sleep.assert_not_called()

    def test_waits_until_the_stop_is_done(self, _in_use, teardown):
        self._stopping()
        polls = []

        def sleep(_seconds):
            polls.append(1)
            if len(polls) == 2:
                # The background stop finished its Redis cleanup; the provider connection closes
                self.redis.delete(probation._skipped_stopping_key(self.UUID))
                teardown.return_value = True
            elif len(polls) == 4:
                teardown.return_value = False

        with patch("apps.proxy.live_proxy.probation.gevent.sleep", side_effect=sleep), self.assertLogs(
            "live_proxy", level="INFO"
        ) as logs:
            self.assertTrue(probation.wait_for_stop_to_finish(self.redis, self.UUID))

        self.assertEqual(len(polls), 4)
        self.assertTrue(any("starting it again" in line for line in logs.output))

    @patch("apps.proxy.live_proxy.probation.gevent.sleep")
    @patch("apps.proxy.live_proxy.probation.time.monotonic")
    def test_gives_up_after_the_limit(self, mock_monotonic, mock_sleep, _in_use, _teardown):
        self._stopping()
        mock_monotonic.side_effect = [0.0, 1.0, probation.STOP_WAIT_SECONDS + 0.1]

        with self.assertLogs("live_proxy", level="WARNING"):
            self.assertTrue(probation.wait_for_stop_to_finish(self.redis, self.UUID))
        mock_sleep.assert_called_once()

    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    def test_background_stop_clears_the_marker(self, mock_stop, _in_use, _teardown):
        self._stopping()
        with patch("core.utils.RedisClient.get_client", return_value=self.redis), patch(
            "django.db.close_old_connections"
        ):
            probation._stop_channel(self.UUID)

        mock_stop.assert_called_once_with(self.UUID)
        self.assertFalse(self.redis.exists(probation._skipped_stopping_key(self.UUID)))

        # Also when the stop fails
        self._stopping()
        mock_stop.side_effect = RuntimeError("boom")
        with patch("core.utils.RedisClient.get_client", return_value=self.redis), patch(
            "django.db.close_old_connections"
        ), self.assertLogs("live_proxy", level="ERROR"):
            probation._stop_channel(self.UUID)
        self.assertFalse(self.redis.exists(probation._skipped_stopping_key(self.UUID)))

