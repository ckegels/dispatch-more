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
from apps.proxy.live_proxy import app_devices, media_servers, probation

# Waits that exist to be kind to a real media server, and cost only time here. Set once,
# in the module the others import, so every test file gets them.
#
# DEVICE_WAIT_SECONDS is the one that matters: a request a server cannot place waits for it
# to catch up, and there are hundreds of those in here.
media_servers.DEVICE_WAIT_SECONDS = 0
media_servers.SCAN_SECONDS = 0
media_servers.MOVE_SETTLE = 0
# Some of these reach a server that is not there on purpose; waiting for it to not answer
# proves nothing
media_servers.REQUEST_TIMEOUT = 0.01
from apps.proxy.live_proxy.constants import ChannelMetadataField, ChannelState
from apps.proxy.live_proxy.redis_keys import RedisKeys


class FakeRedis:
    """Minimal in-memory Redis (decode_responses=True semantics)."""

    def __init__(self):
        self.strings = {}
        self.hashes = {}
        self.sets = {}
        self.zsets = {}
        self.lists = {}
        # Keys with an expiry, and the seconds they were given (nothing expires by itself here)
        self.expiring = set()
        self.ttls = {}

    def get(self, key):
        value = self.strings.get(key)
        return None if value is None else str(value)

    def set(self, key, value, nx=False, ex=None):
        if nx and self.exists(key):
            return None
        self.strings[key] = str(value)
        if ex is None:
            self.expiring.discard(key)
            self.ttls.pop(key, None)
        else:
            self.expiring.add(key)
            self.ttls[key] = ex
        return True

    def setex(self, key, ttl, value):
        self.strings[key] = str(value)
        self.expiring.add(key)
        self.ttls[key] = ttl

    def delete(self, *keys):
        removed = 0
        for key in keys:
            for store in (self.strings, self.hashes, self.sets, self.zsets, self.lists):
                if store.pop(key, None) is not None:
                    removed += 1
        return removed

    def exists(self, key):
        return int(
            key in self.strings
            or key in self.hashes
            or key in self.sets
            or key in self.zsets
            or key in self.lists
        )

    def expire(self, key, ttl):
        if self.exists(key):
            self.expiring.add(key)
            self.ttls[key] = ttl
        return True

    def ttl(self, key):
        if not self.exists(key):
            return -2
        return self.ttls.get(key, 30) if key in self.expiring else -1

    def lpush(self, key, *values):
        items = self.lists.setdefault(key, [])
        for value in values:
            items.insert(0, str(value))
        return len(items)

    def ltrim(self, key, start, stop):
        items = self.lists.get(key, [])
        self.lists[key] = items[start : stop + 1] if stop >= 0 else items[start:]
        return True

    def lrange(self, key, start, stop):
        items = self.lists.get(key, [])
        return items[start : stop + 1] if stop >= 0 else items[start:]

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

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

    def hsetnx(self, key, field, value):
        fields = self.hashes.setdefault(key, {})
        if field in fields:
            return 0
        fields[field] = str(value)
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, amount=1):
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = str(int(bucket.get(field, 0)) + amount)
        return int(bucket[field])

    def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        return sum(1 for field in fields if bucket.pop(field, None) is not None)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update({str(k): float(v) for k, v in mapping.items()})

    def zrevrange(self, key, start, stop):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda item: -item[1])
        return [member for member, _score in members][start : (None if stop == -1 else stop + 1)]

    def zremrangebyrank(self, key, start, stop):
        members = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        for member, _score in members[start : (None if stop == -1 else stop + 1)]:
            self.zsets[key].pop(member, None)

    def zremrangebyscore(self, key, minimum, maximum):
        minimum = float("-inf") if minimum == "-inf" else float(minimum)
        for member, score in list(self.zsets.get(key, {}).items()):
            if minimum <= score <= float(maximum):
                self.zsets[key].pop(member, None)

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


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        return [getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.calls]


def _reset_in_use_cache(test):
    """probation.in_use() is cached in Redis, which test database rollbacks do not reset."""
    probation.forget_in_use()
    test.addCleanup(probation.forget_in_use)


def _profiles_watched_by(redis_client, viewer):
    """The profiles a viewer is watching on, which is how the overlap recognises it."""
    return list(probation.watched_channels_by(redis_client, viewer))


def _make_account(name, max_streams=1, probation_enabled=False, probation_seconds=None, lan=True):
    custom_properties = {"probation_enabled": probation_enabled}
    if probation_enabled and lan:
        # Test players are on 192.168.x: recognised by IP address and app (LAN Subnets)
        custom_properties["probation_lan_subnets"] = ["192.168.0.0/16"]
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

    def _watching(
        self,
        profile,
        ip=IP,
        user_id="0",
        user_agent=None,
        channel_uuid="old-channel",
        server_device=None,
    ):
        self.redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile.id},
        )
        self.redis.sadd(RedisKeys.clients(channel_uuid), "client_1")
        # No User-Agent means no app, like a media server: the viewer in the test has none either
        client = {"ip_address": ip, "user_id": user_id, "user_agent": user_agent or ""}
        if server_device:
            client["server_device"] = server_device
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
        self._watching(self.profile_a, user_agent="TiviMate")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

        self._assert_probation_on(result, self.stream_a, self.profile_a)
        self.assertEqual(self._deadline_seconds(), probation.DEFAULT_PROBATION_SECONDS)

    def test_other_device_behind_same_ip_gets_limit_error(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, user_agent="TiviMate")

        self._assert_limit_error(
            self.channel.get_stream(viewer=probation.Viewer(self.IP, app="Smarters"))
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

    def test_a_viewer_that_cannot_be_told_apart_gets_nothing(self, _preempt):
        # No app in the User-Agent (a media server that could not say who is asking), so no
        # account recognises it: an extra connection could go to the wrong person entirely.
        self._fill_both()
        self._watching(self.profile_a)

        with self.assertLogs("live_proxy", level="INFO") as logs:
            self._assert_limit_error(self.channel.get_stream(viewer=probation.Viewer(self.IP)))
        self.assertTrue(
            any("no media server said which of its devices" in line for line in logs.output)
        )

    def test_a_media_server_that_named_its_device_is_told_apart(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, server_device="server|apple-tv")

        result = self.channel.get_stream(
            viewer=probation.Viewer(self.IP, server_device="server|apple-tv")
        )

        self._assert_probation_on(result, self.stream_a, self.profile_a)

    def test_anonymous_viewer_does_not_match_identified_client(self, _preempt):
        self._fill_both()
        self._watching(self.profile_a, user_agent="TiviMate")

        self._assert_limit_error(self.channel.get_stream(viewer=probation.Viewer(self.IP)))

    def test_retries_log_not_used_once(self, _preempt):
        probation._not_used_logged.clear()
        self.addCleanup(probation._not_used_logged.clear)
        self._fill_both()
        self._watching(self.profile_a, user_agent="TiviMate")
        viewer = probation.Viewer(self.IP, app="Smarters")

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

        with patch.object(probation, "watched_channels_by") as mock_find, \
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
        self._watching(self.profile_b, user_agent="TiviMate")

        with self.assertLogs("live_proxy", level="INFO") as logs:
            result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

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
        viewer = probation.Viewer(self.IP, app="TiviMate")
        probation.remember_viewer_profile(self.redis, viewer, self.profile_b, self.account_b)

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertEqual(self._count(self.profile_b), "1")

    def test_sticky_never_overlaps_account_viewer_only_left(self, _preempt):
        self._set_props(self.account_b, probation_account_preference="same")
        viewer = probation.Viewer(self.IP, app="TiviMate")
        probation.remember_viewer_profile(self.redis, viewer, self.profile_b, self.account_b)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)  # someone else took it

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result, (self.stream_a.id, self.profile_a.id, None, True))
        self.assertEqual(self._count(self.profile_b), "1")

    def test_sticky_disabled_keeps_stock_order(self, _preempt):
        self._watching(self.profile_b, user_agent="TiviMate")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

        self.assertEqual(result, (self.stream_a.id, self.profile_a.id, None, True))
        self.assertIsNone(self.redis.get(probation._last_profile_key(probation._viewer_key(probation.Viewer(self.IP, app="TiviMate")))))

    def test_stay_on_same_account_needs_a_viewer_it_can_tell_apart(self, _preempt):
        """A viewer with no login, no app and no media server device is nobody in particular."""
        self._set_props(self.account_b, probation_account_preference="same")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self._watching(self.profile_b)
        viewer = probation.Viewer(self.IP)

        # It follows the channel's order instead of the account it "was" on
        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

        # The same viewer, recognised by a media server, does stay on its account
        self.redis.set(profile_connections_key(self.profile_a.id), 0)
        self.redis.delete(f"channel_stream:{self.channel.id}")
        self._watching(self.profile_b, server_device="server|apple-tv")
        self.assertEqual(
            self.channel.get_stream(
                viewer=probation.Viewer(self.IP, server_device="server|apple-tv")
            )[1],
            self.profile_b.id,
        )

    def test_sticky_ignores_redirect_profiles(self, _preempt):
        self.stream_profile.is_redirect.return_value = True
        self._set_props(self.account_b, probation_account_preference="same")
        self._watching(self.profile_b, user_agent="TiviMate")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

        self.assertEqual(result[1], self.profile_a.id)

    def test_assignment_is_remembered_only_on_sticky_accounts(self, _preempt):
        viewer = probation.Viewer(self.IP, app="TiviMate")

        self.channel.get_stream(viewer=viewer)
        self.assertIsNone(self.redis.get(probation._last_profile_key(probation._viewer_key(viewer))))

        self.redis.delete(f"channel_stream:{self.channel.id}")
        self.redis.set(profile_connections_key(self.profile_a.id), 0)
        self._set_props(self.account_a, probation_account_preference="same")
        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)
        self.assertEqual(self.redis.get(probation._last_profile_key(probation._viewer_key(viewer))), str(self.profile_a.id))

    # ── Use another account ─────────────────────────────────────────────────

    def test_alternate_starts_on_other_account_after_old_one_was_released(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, app="TiviMate")
        probation.remember_viewer_profile(self.redis, viewer, self.profile_a, self.account_a)

        with self.assertLogs("live_proxy", level="INFO") as logs:
            result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertIsNone(self._count(self.profile_a))
        self.assertTrue(any("use another account" in line for line in logs.output))

    def test_order_preference_returns_to_the_same_account(self, _preempt):
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._hold(self.profile_a, viewer)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

    def test_alternate_releases_the_hold_it_leaves_behind(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._hold(self.profile_a, viewer)

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result[1], self.profile_b.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    def test_alternate_falls_back_to_own_held_slot_when_others_are_full(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._hold(self.profile_a, viewer)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

        result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    def test_alternate_falls_back_to_overlap_when_others_are_full(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        self._fill_both()
        self._watching(self.profile_a, user_agent="TiviMate")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

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
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._hold(self.profile_a, viewer)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)

        result = self.channel.get_stream(viewer=viewer)

        self.assertNotEqual(result[0], fallback.id)
        self.assertEqual(result[1], self.profile_a.id)

    def test_alternate_ignores_accounts_without_overlap(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        self._set_props(self.account_b, probation_enabled=False)
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._hold(self.profile_a, viewer)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

    def test_alternate_depends_on_the_account_being_left(self, _preempt):
        # Only account B alternates; the viewer leaves account A, which follows channel order
        self._set_props(self.account_b, probation_account_preference="alternate")
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._hold(self.profile_a, viewer)

        self.assertEqual(self.channel.get_stream(viewer=viewer)[1], self.profile_a.id)

    def test_alternate_only_applies_to_the_viewer_that_left(self, _preempt):
        self._set_props(self.account_a, probation_account_preference="alternate")
        probation.remember_viewer_profile(
            self.redis, probation.Viewer(self.IP, app="TiviMate"), self.profile_a, self.account_a
        )

        # Another device on the network: the remembered account does not apply to it
        result = self.channel.get_stream(viewer=probation.Viewer("192.168.1.55", app="Smarters"))

        self.assertEqual(result[1], self.profile_a.id)

    # ── Held slots ──────────────────────────────────────────────────────────

    def _hold(self, profile, viewer, account=None, seconds=10):
        self.redis.hset(
            probation._held_slots_key(profile.id),
            probation.identity_key(viewer, account or self.account_a),
            str(time.time() + seconds),
        )

    def test_held_slot_is_taken_for_other_viewers(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, app="TiviMate"))

        result = self.channel.get_stream(viewer=probation.Viewer("192.168.1.99", app="Smarters"))

        self.assertEqual(result[1], self.profile_b.id)
        self.assertEqual(self._count(self.profile_a), "0")

    def test_held_slot_is_taken_for_requests_without_viewer(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, app="TiviMate"))

        self.assertEqual(self.channel.get_stream()[1], self.profile_b.id)

    def test_viewer_gets_its_held_slot_back(self, _preempt):
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        self._hold(self.profile_a, viewer)

        with self.assertLogs("live_proxy", level="INFO") as logs:
            result = self.channel.get_stream(viewer=viewer)

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})
        self.assertTrue(any("took its held slot" in line for line in logs.output))

    def test_expired_hold_is_ignored(self, _preempt):
        self._hold(self.profile_a, probation.Viewer(self.IP, app="TiviMate"), seconds=-1)

        result = self.channel.get_stream(viewer=probation.Viewer("192.168.1.99", app="Smarters"))

        self.assertEqual(result[1], self.profile_a.id)
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

    def test_holds_are_ignored_on_accounts_without_overlap(self, _preempt):
        self._set_props(self.account_a, probation_enabled=False)
        self._hold(self.profile_a, probation.Viewer(self.IP, app="TiviMate"))

        self.assertEqual(self.channel.get_stream()[1], self.profile_a.id)

    def test_release_holds_slot_for_its_single_viewer(self, _preempt):
        self._watching(self.profile_a, user_agent="TiviMate", channel_uuid=str(self.channel.uuid))
        self.redis.set(f"channel_stream:{self.channel.id}", self.stream_a.id)
        self.redis.set(f"stream_profile:{self.stream_a.id}", self.profile_a.id)
        self.redis.set(profile_connections_key(self.profile_a.id), 1)

        with self.assertLogs("live_proxy", level="INFO"):
            self.assertTrue(self.channel.release_stream())

        self.assertEqual(self._count(self.profile_a), "0")
        held = self.redis.hgetall(probation._held_slots_key(self.profile_a.id))
        self.assertEqual(
            list(held),
            [probation.identity_key(probation.Viewer(self.IP, app="TiviMate"), self.account_a)],
        )

    def test_no_hold_without_identity_multiple_viewers_or_no_hold_marker(self, _preempt):
        uuid = str(self.channel.uuid)

        def release_with(**setup):
            self.redis = FakeRedis()
            for client_id, device_id in setup.get("clients", []):
                self.redis.hset(RedisKeys.channel_metadata(uuid), mapping={ChannelMetadataField.M3U_PROFILE: self.profile_a.id})
                self.redis.sadd(RedisKeys.clients(uuid), client_id)
                self.redis.hset(
                    RedisKeys.client_metadata(uuid, client_id),
                    mapping={"ip_address": self.IP, "user_id": "0", "user_agent": device_id or ""},
                )
            if setup.get("no_hold"):
                self.redis.setex(probation.NO_HOLD_KEY.format(channel_uuid=uuid), 30, "1")
            probation.hold_slot_for_viewer(self.redis, uuid, self.profile_a.id)
            return self.redis.hgetall(probation._held_slots_key(self.profile_a.id))

        self.assertEqual(release_with(clients=[("c1", None)]), {})  # anonymous, not allowed
        self.assertEqual(release_with(clients=[("c1", "TiviMate"), ("c2", "Kodi")]), {})
        self.assertEqual(release_with(clients=[("c1", "TiviMate")], no_hold=True), {})
        self._set_props(self.account_a, probation_enabled=False)
        self.assertEqual(release_with(clients=[("c1", "TiviMate")]), {})

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
        viewer = probation.Viewer(self.IP, app="TiviMate")
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
                account, probation_account_preference="same"
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
        self._hold(self.profile_a, probation.Viewer(self.IP, app="TiviMate"))

        result = self.channel.get_stream(viewer=probation.Viewer("127.0.0.1", recording=True))

        self.assertEqual(result[1], self.profile_a.id)

    def test_no_hold_when_stopped_on_purpose_or_recording(self, _preempt):
        uuid = str(self.channel.uuid)
        viewer = probation.Viewer(self.IP, app="TiviMate")

        def release_with(setup):
            self.redis = FakeRedis()
            self._watching(self.profile_a, user_agent="TiviMate", channel_uuid=uuid)
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
            self.assertEqual(
                list(release_with(lambda r: None)),
                [probation.identity_key(viewer, self.account_a)],
            )

    # ── How a device is recognised ──────────────────────────────────────────

    def _track_lan(self, account, subnets=("192.168.1.0/24",)):
        self._set_props(account, probation_lan_subnets=list(subnets))

    def _player(self, user_agent="TiviMate/5.1.6 (Android 12)", ip=IP, user_id=None):
        return probation.Viewer(ip, user_id=user_id, app=probation.app_name(user_agent))

    def _watching_player(self, profile, user_agent="TiviMate/5.1.6 (Android 12)", ip=IP, user_id="0"):
        self._watching(profile, ip=ip, user_id=user_id)
        self.redis.hset(RedisKeys.client_metadata("old-channel", "client_1"), "user_agent", user_agent)

    def test_lan_player_is_recognised_without_a_login(self, _preempt):
        for account in (self.account_a, self.account_b):
            self._set_props(account, probation_lan_subnets=[])
        self._fill_both()
        self._watching_player(self.profile_a)
        player = self._player()

        # Without LAN Device Tracking a player without a login is anonymous
        with self.assertLogs("live_proxy", level="INFO"):
            self._assert_limit_error(self.channel.get_stream(viewer=player))

        self._track_lan(self.account_a)
        self.assertEqual(_profiles_watched_by(self.redis, player), [self.profile_a.id])
        self._assert_probation_on(self.channel.get_stream(viewer=player), self.stream_a, self.profile_a)

    def test_another_app_or_address_is_another_device(self, _preempt):
        self._track_lan(self.account_a)
        self._watching_player(self.profile_a)

        # Another app on the same device, and another device on the network
        for other in (
            self._player(user_agent="Kodi/21.0 (Linux; Android 12)"),
            self._player(ip="192.168.1.30"),
        ):
            self.assertEqual(_profiles_watched_by(self.redis, other), [])
        # The same app after an update is the same device
        updated = self._player(user_agent="TiviMate/5.2.0 (Android 12)")
        self.assertEqual(_profiles_watched_by(self.redis, updated), [self.profile_a.id])

    def test_outside_the_lan_subnets_a_login_is_needed(self, _preempt):
        self._track_lan(self.account_a)
        remote = "77.100.5.9"
        self._watching_player(self.profile_a, ip=remote, user_id="7")

        # Same address and app, but no login: anonymous, so not the same viewer
        self.assertEqual(
            _profiles_watched_by(self.redis, self._player(ip=remote)), []
        )
        # With its own login it is recognised
        self.assertEqual(
            _profiles_watched_by(self.redis, self._player(ip=remote, user_id=7)),
            [self.profile_a.id],
        )

    def test_media_servers_and_recordings_are_never_lan_devices(self, _preempt):
        self._track_lan(self.account_a)
        self._fill_both()
        self._watching_player(self.profile_a, user_agent="Jellyfin-Server/10.10.7")
        media_server = self._player(user_agent="Jellyfin-Server/10.10.7")

        # Anonymous: matched by IP only, so the account has to allow that
        self.assertFalse(probation.is_identified(media_server, self.account_a))
        with self.assertLogs("live_proxy", level="INFO"):
            self._assert_limit_error(self.channel.get_stream(viewer=media_server))

        # A recording never takes part at all
        recording = probation.Viewer(self.IP, recording=True)
        self.assertFalse(probation.is_identified(recording, self.account_a))
        self.assertEqual(_profiles_watched_by(self.redis, recording), [])

    def test_lan_player_keeps_its_held_slot(self, _preempt):
        self._track_lan(self.account_a)
        self.redis.set(profile_connections_key(self.profile_b.id), 1)
        uuid = str(self.channel.uuid)
        self.redis.hset(RedisKeys.channel_metadata(uuid), mapping={ChannelMetadataField.M3U_PROFILE: self.profile_a.id})
        self.redis.sadd(RedisKeys.clients(uuid), "c1")
        self.redis.hset(
            RedisKeys.client_metadata(uuid, "c1"),
            mapping={"ip_address": self.IP, "user_id": "0", "user_agent": "TiviMate/5.1.6 (Android 12)"},
        )
        with self.assertLogs("live_proxy", level="INFO"):
            probation.hold_slot_for_viewer(self.redis, uuid, self.profile_a.id)
        self.redis.delete(RedisKeys.clients(uuid))

        # Another device on the network is kept out
        self.assertFalse(
            pool_has_capacity_for_profile(self.profile_a, self.redis, self._player(ip="192.168.1.30"))
        )
        # The player itself gets its slot back
        other_channel = Channel.objects.create(channel_number=905, name="Next")
        ChannelStream.objects.create(channel=other_channel, stream=self.stream_a, order=0)
        with self.assertLogs("live_proxy", level="INFO") as logs:
            self.assertEqual(other_channel.get_stream(viewer=self._player())[1], self.profile_a.id)
        self.assertTrue(any("took its held slot" in line for line in logs.output))
        self.assertEqual(self.redis.hgetall(probation._held_slots_key(self.profile_a.id)), {})

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
                probation._viewer_member(viewer),
            )

    def test_channel_waiting_for_shutdown_delay_counts_for_its_viewer(self, _preempt):
        viewer = probation.Viewer(self.IP, app="TiviMate")
        self._fill_both()
        self._idle_channel(self.profile_a, [viewer])

        result = self.channel.get_stream(viewer=viewer)

        self._assert_probation_on(result, self.stream_a, self.profile_a)

    def test_channel_waiting_for_shutdown_delay_only_counts_for_its_only_viewer(self, _preempt):
        viewer = probation.Viewer(self.IP, app="TiviMate")
        other = probation.Viewer(self.IP, app="Kodi")

        self._idle_channel(self.profile_a, [viewer, other])
        self.assertEqual(_profiles_watched_by(self.redis, viewer), [])

        self.redis = FakeRedis()
        self._idle_channel(self.profile_a, [viewer])
        self.assertEqual(_profiles_watched_by(self.redis, other), [])
        self.assertEqual(
            _profiles_watched_by(self.redis, probation.Viewer(self.IP, recording=True)), []
        )
        self.assertEqual(_profiles_watched_by(self.redis, viewer), [self.profile_a.id])
        # Not once it is being stopped
        self.redis.setex(RedisKeys.channel_stopping("idle-channel"), 60, "true")
        self.assertEqual(_profiles_watched_by(self.redis, viewer), [])

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
        self._watching(self.profile_a, user_agent="TiviMate")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

        self._assert_probation_on(result, self.stream_a, self.profile_a)
        self.assertNotEqual(result[0], slate.id)

    def test_custom_fallback_stream_is_used_when_the_overlap_does_not_apply(self, _preempt):
        slate = self._add_custom_stream(order=99)
        self._fill_both()
        self._watching(self.profile_a, user_agent="TiviMate")

        # Another viewer, and a request without viewer, get the fallback as before
        with self.assertLogs("live_proxy", level="INFO"):
            other = self.channel.get_stream(viewer=probation.Viewer("192.168.1.99", app="Smarters"))
        self.assertEqual(other[0], slate.id)
        self.assertEqual(self.channel.get_stream()[0], slate.id)
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))

    def test_custom_stream_first_in_order_is_still_used_first(self, _preempt):
        ChannelStream.objects.filter(channel=self.channel).update(order=5)
        custom = self._add_custom_stream(order=0, name="My own stream")
        self._watching(self.profile_a, user_agent="TiviMate")

        result = self.channel.get_stream(viewer=probation.Viewer(self.IP, app="TiviMate"))

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
                custom_properties={
                    "probation_enabled": True,
                    "probation_lan_subnets": ["192.168.0.0/16"],
                },
            )
            self.profiles.append(M3UAccountProfile.objects.get(m3u_account=account, is_default=True))
        self.viewer = probation.Viewer("192.168.1.20", app="TiviMate")

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
            mapping={"ip_address": self.viewer.ip, "user_id": "0", "user_agent": self.viewer.app},
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
        viewer = probation.Viewer(self.IP, app="TiviMate")
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
            mapping={"ip_address": self.IP, "user_id": "0", "user_agent": "TiviMate", "connected_at": str(time.time())},
        )
        before = (dict(redis.strings), {k: dict(v) for k, v in redis.hashes.items()}, dict(redis.sets))
        viewer = probation.Viewer(self.IP, app="TiviMate")
        # Both switches are cached once a minute, like a running server has them
        probation.in_use()
        probation.skipping_in_use()

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
        self.viewer = probation.Viewer(self.IP, app="TiviMate")

        # The background stop closes DB connections when done; keep the test transaction open
        patchers = [
            patch("django.db.close_old_connections"),
            patch("core.utils.RedisClient.get_client", side_effect=lambda: self.redis),
            patch(
                "apps.proxy.live_proxy.server.ProxyServer.get_instance",
                return_value=MagicMock(_stopping_channels=set()),
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _set_props(self, account, **props):
        account.custom_properties = {**account.custom_properties, **props}
        account.save()

    def _channel(self, channel_uuid, profile=None, clients=()):
        """clients: (client_id, ip, user_id, user_agent, seconds_ago[, media server device])"""
        profile = profile or self.profile
        self.redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile.id},
        )
        for client_id, ip, user_id, device_id, seconds_ago, *server in clients:
            self.redis.sadd(RedisKeys.clients(channel_uuid), client_id)
            client = {
                "ip_address": ip,
                "user_id": user_id,
                "connected_at": str(self.NOW - seconds_ago),
            }
            if device_id:
                client["user_agent"] = device_id
            if server:
                client["server_device"] = server[0]
            self.redis.hset(RedisKeys.client_metadata(channel_uuid, client_id), mapping=client)

    def _stop(self, viewer=None, requested="channel-new", certain=None):
        return probation.stop_skipped_channels(
            self.redis, viewer or self.viewer, requested, now=self.NOW, certain=certain
        )

    def test_switching_closes_every_other_channel_of_the_player(self, mock_stop, _mock_spawn):
        self._channel("watched", clients=[("c1", self.IP, "0", "TiviMate", 600)])
        self._channel("skipped-1", clients=[("c2", self.IP, "0", "TiviMate", 2)])
        self._channel("skipped-2", clients=[("c3", self.IP, "0", "TiviMate", 1)])

        with self.assertLogs("live_proxy", level="INFO") as logs:
            stopped = self._stop()

        self.assertEqual(sorted(stopped), ["skipped-1", "skipped-2", "watched"])
        self.assertEqual(
            sorted(c.args[0] for c in mock_stop.call_args_list), ["skipped-1", "skipped-2", "watched"]
        )
        self.assertTrue(any("Force close: stopping channel" in line for line in logs.output))

    def test_a_media_server_viewer_is_settled_once_its_server_names_them(
        self, mock_stop, _mock_spawn
    ):
        """
        The request could not say who this was, so the work is done when the server can.

        A media server asks for the stream before it registers what it is playing, so at the
        moment of the request there is nothing to go on. Waiting for it is a race that is
        lost often enough to matter; this runs afterwards, when the answer is certain, and
        stops the channel the viewer left so its slot goes back.
        """
        device = "server|shield-1"
        self._channel("left-behind", clients=[("c1", self.IP, "0", None, 2)])
        # This one runs after the fact, off the clock rather than at a moment given to it
        self.redis.hset(
            RedisKeys.client_metadata("left-behind", "c1"),
            mapping={"server_device": device, "connected_at": str(time.time() - 2)},
        )

        stopped = probation.settle_media_server_start(
            self.redis, "channel-new", device, previous_channel="left-behind"
        )

        self.assertEqual(stopped, ["left-behind"])
        mock_stop.assert_called_once_with("left-behind")
        # And it is on the page as a switch, not as a viewer nobody could place
        switched = [
            event
            for event in probation.recent_events(self.redis)
            if event["action"] == "switched"
        ]
        self.assertEqual(len(switched), 1, probation.recent_events(self.redis))
        self.assertEqual(switched[0]["from_channel"], "left-behind")
        self.assertIn("once its server said who", switched[0]["result"])

    def test_settling_a_start_nobody_left_a_channel_for_does_nothing(
        self, mock_stop, _mock_spawn
    ):
        self.assertEqual(
            probation.settle_media_server_start(self.redis, "channel-new", "server|x"), []
        )
        mock_stop.assert_not_called()
        self.assertEqual(probation.recent_events(self.redis), [])

    def test_a_channel_a_media_server_is_recording_is_never_stopped(
        self, mock_stop, _mock_spawn
    ):
        """
        A recording is a channel nobody is watching, which is what a skipped one looks like.

        The recording pulls its stream while a viewer is streaming, so it can be taken for
        that viewer's, and a switch a moment later would free "their" old channel. Losing a
        recording is the worst thing this could do, so the server is asked.
        """
        self.redis.hset(
            RedisKeys.channel_metadata("being-recorded"),
            mapping={ChannelMetadataField.CHANNEL_NAME: "┃FR┃ TFX"},
        )
        self._channel("being-recorded", clients=[("c1", self.IP, "0", "TiviMate", 2)])

        with patch(
            "apps.proxy.live_proxy.media_servers.channels_being_recorded"
        ) as recording:
            recording.return_value = {"TFX"}
            stopped = self._stop()

        self.assertEqual(stopped, [])
        mock_stop.assert_not_called()

    def test_a_channel_nothing_is_recording_is_still_stopped(self, mock_stop, _mock_spawn):
        """The guard only holds back what is really being recorded."""
        self.redis.hset(
            RedisKeys.channel_metadata("just-skipped"),
            mapping={ChannelMetadataField.CHANNEL_NAME: "┃FR┃ TFX"},
        )
        self._channel("just-skipped", clients=[("c1", self.IP, "0", "TiviMate", 2)])

        with patch(
            "apps.proxy.live_proxy.media_servers.channels_being_recorded"
        ) as recording:
            recording.return_value = {"┃BE┃ Een"}
            stopped = self._stop()

        self.assertEqual(stopped, ["just-skipped"])

    def test_requested_channel_is_never_stopped(self, mock_stop, _mock_spawn):
        self._channel("channel-new", clients=[("c1", self.IP, "0", "TiviMate", 1)])

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_shared_channel_is_never_stopped(self, mock_stop, _mock_spawn):
        self._channel(
            "shared",
            clients=[("c1", self.IP, "0", "TiviMate", 1), ("c2", "192.168.1.30", "0", "Kodi", 300)],
        )

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_a_channel_watched_for_long_is_closed_too(self, mock_stop, _mock_spawn):
        """Force close: every viewer is one device, so a channel it left is closed, however long it was on."""
        self._channel(
            "watched",
            clients=[("c1", self.IP, "0", "TiviMate", 600), ("c2", self.IP, "0", "TiviMate", 1)],
        )

        self.assertEqual(self._stop(), ["watched"])

    def test_other_device_or_user_channels_are_never_stopped(self, mock_stop, _mock_spawn):
        self._channel("other-device", clients=[("c1", self.IP, "0", "Smarters", 1)])
        self._channel("other-user", clients=[("c2", self.IP, "8", None, 1)])
        self._channel("other-ip", clients=[("c3", "10.0.0.9", "0", "TiviMate", 1)])

        self.assertEqual(self._stop(), [])
        self.assertEqual(self._stop(viewer=probation.Viewer(self.IP, user_id=7)), [])
        mock_stop.assert_not_called()

    def test_anonymous_viewer_never_stops_anything(self, mock_stop, _mock_spawn):
        self._channel("anonymous", clients=[("c1", self.IP, "0", None, 1)])

        with patch.object(probation, "any_account_stops_skipped_channels") as mock_any:
            self.assertEqual(self._stop(viewer=probation.Viewer(self.IP)), [])

        mock_any.assert_not_called()
        mock_stop.assert_not_called()

    def test_account_settings_are_required(self, mock_stop, _mock_spawn):
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])

        self._set_props(self.account, probation_stop_skipped=False)
        with patch.object(self.redis, "scan_iter", wraps=self.redis.scan_iter) as mock_scan:
            self.assertEqual(self._stop(), [])
        mock_scan.assert_not_called()

    def test_stopping_skipped_channels_does_not_need_the_overlap(self, mock_stop, _mock_spawn):
        """Its own feature: it asks no extra slot of the provider, so any account may use it."""
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])
        self._set_props(self.account, probation_stop_skipped=True, probation_enabled=False)
        probation.forget_in_use()
        self.assertEqual(self._stop(), ["skipped"])

    def test_a_guessed_media_server_player_only_closes_what_it_just_opened(self, mock_stop, _mock_spawn):
        """A guess must not close a channel someone else has watched for an hour."""
        player = probation.Viewer(self.IP, server_device="server|living-room")
        self._channel("just-opened", clients=[("c1", self.IP, "0", None, 2, "server|living-room")])
        self._channel("long-ago", clients=[("c2", self.IP, "0", None, 600, "server|living-room")])
        self.assertEqual(self._stop(viewer=player), ["just-opened"])
        # Once the server has said who it is, the rest goes too
        self.assertIn("long-ago", self._stop(viewer=player, certain=True))

    # ── Apps that say which device they are (app_devices) ──

    def _device(self, device, user_id=1, multiview=None):
        return probation.Viewer(
            "192.168.65.3", user_id=user_id, app="arrTV",
            server_device=app_devices.device_key(user_id, device), multiview=multiview,
        )

    def _device_channel(self, uuid, viewer, seconds_ago=5, client="c"):
        self._channel(uuid, clients=[(f"{client}-{uuid}", viewer.ip, str(viewer.user_id), "arrTV", seconds_ago, viewer.server_device)])
        if viewer.multiview:
            self.redis.hset(RedisKeys.client_metadata(uuid, f"{client}-{uuid}"), "multiview", viewer.multiview)

    def test_two_devices_on_one_login_behind_one_address_are_two_viewers(self, mock_stop, _mock_spawn):
        """
        On the real installation a SHIELD and a Mac on the admin login both came through the
        VPN as 192.168.65.3, and each channel start closed the other's stream.
        """
        shield, mac = self._device("shield-0001"), self._device("macbook-0001")
        self._device_channel("on-the-tv", shield, seconds_ago=600)
        self._device_channel("on-the-mac", mac, seconds_ago=600)
        self.assertEqual(self._stop(viewer=mac), ["on-the-mac"])

    def test_a_declared_device_is_certain_not_a_guess(self, mock_stop, _mock_spawn):
        """The media server's ten-second window is for a guess; a device that said so is not one."""
        shield = self._device("shield-0001")
        self._device_channel("an-hour-ago", shield, seconds_ago=3600)
        self.assertEqual(self._stop(viewer=shield), ["an-hour-ago"])

    def test_a_multiview_tile_closes_nothing_of_its_device(self, mock_stop, _mock_spawn):
        """
        Not the other tiles, and not the channel the device was on full screen before
        multiview opened -- that one started before the session and carries none of it.
        """
        tile = self._device("shield-0001", multiview="mv-1")
        self._device_channel("tile-1", tile)
        self._device_channel("full-screen-before", self._device("shield-0001"))
        self.assertEqual(self._stop(viewer=tile), [])
        # A request outside multiview afterwards closes them as usual
        self.assertEqual(sorted(self._stop(viewer=self._device("shield-0001"))), ["full-screen-before", "tile-1"])

    def test_the_app_says_which_channel_it_is_leaving(self, mock_stop, _mock_spawn):
        shield, mac = self._device("shield-0001"), self._device("macbook-0001")
        self._device_channel("leaving", shield)
        self._device_channel("the-macs", mac)
        with self.assertLogs("live_proxy", level="INFO") as logs:
            self.assertTrue(probation.leave_previous_channel(self.redis, shield, "leaving", "new"))
        self.assertTrue(any("App switch: closing channel leaving" in line for line in logs.output))
        # Not another device's channel, whatever the app says
        self.assertFalse(probation.leave_previous_channel(self.redis, shield, "the-macs", "new"))
        # Not one somebody else is on as well
        self._device_channel("shared", shield)
        self._channel("shared", clients=[("x1", "192.168.2.5", "2", "TiviMate", 30)])
        self.assertFalse(probation.leave_previous_channel(self.redis, shield, "shared", "new"))
        # Not for a player that did not say which device it is
        self._device_channel("guessed", shield)
        self.assertFalse(probation.leave_previous_channel(self.redis, self.viewer, "guessed", "new"))

    def test_only_accounts_with_the_option_are_affected(self, mock_stop, _mock_spawn):
        _other_account, other_profile = _make_account("no-stop", probation_enabled=True)
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])
        self._channel("other", profile=other_profile, clients=[("c2", self.IP, "0", "TiviMate", 1)])

        self.assertEqual(self._stop(), ["skipped"])

    def test_stop_runs_in_background(self, mock_stop, mock_spawn):
        mock_spawn.side_effect = None
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])

        self.assertEqual(self._stop(), ["skipped"])

        mock_stop.assert_not_called()
        mock_spawn.assert_called_once_with(probation._stop_channel, "skipped")

    def test_channel_already_being_stopped_is_not_stopped_again(self, mock_stop, mock_spawn):
        # The background stop is still running when the next channel is requested
        mock_spawn.side_effect = None
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])

        self.assertEqual(self._stop(requested="channel-c"), ["skipped"])
        self.assertEqual(self._stop(requested="channel-d"), [])

        mock_spawn.assert_called_once_with(probation._stop_channel, "skipped")

    def test_lan_player_without_a_login_stops_its_skipped_channels(self, mock_stop, _mock_spawn):
        player = probation.Viewer(self.IP, app=probation.app_name("TiviMate/5.1.6 (Android 12)"))
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate/5.1.6 (Android 12)", 2)])

        # Without LAN Device Tracking a player without a login is anonymous
        self._set_props(self.account, probation_lan_subnets=[])
        self.assertEqual(self._stop(viewer=player), [])

        self._set_props(self.account, probation_lan_subnets=["192.168.1.0/24"])
        with self.assertLogs("live_proxy", level="INFO"):
            self.assertEqual(self._stop(viewer=player), ["skipped"])

    def test_channel_dispatcharr_is_already_stopping_is_left_alone(self, mock_stop, _mock_spawn):
        # The player left and Dispatcharr is stopping it: a second stop would leave a
        # stopping marker behind that blocks the channel for a minute
        self._channel("leaving", clients=[("c1", self.IP, "0", "TiviMate", 1)])
        self.redis.setex(RedisKeys.channel_stopping("leaving"), 60, "true")
        self._channel("closing", clients=[("c2", self.IP, "0", "TiviMate", 1)])
        self.redis.hset(RedisKeys.channel_metadata("closing"), ChannelMetadataField.STATE, ChannelState.STOPPING)

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_background_stop_is_skipped_when_dispatcharr_got_there_first(self, mock_stop, _mock_spawn):
        def run_stop(setup):
            mock_stop.reset_mock()
            self.redis = FakeRedis()
            self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])
            self.redis.setex(probation._skipped_stopping_key("skipped"), 30, "1")
            stopping_here = setup(self.redis) or set()
            with patch(
                "apps.proxy.live_proxy.server.ProxyServer.get_instance",
                return_value=MagicMock(_stopping_channels=stopping_here),
            ), self.assertLogs("live_proxy", level="INFO"):
                probation._stop_channel("skipped")
            # Waiting requests may go ahead either way
            self.assertFalse(self.redis.exists(probation._skipped_stopping_key("skipped")))
            return mock_stop.called

        # Dispatcharr marked it stopping, already removed it, or is stopping it in this worker
        self.assertFalse(run_stop(lambda r: r.setex(RedisKeys.channel_stopping("skipped"), 60, "true")))
        self.assertFalse(run_stop(lambda r: r.delete(RedisKeys.channel_metadata("skipped")) and None))
        self.assertFalse(run_stop(lambda r: {"skipped"}))
        # Otherwise it is stopped
        mock_stop.reset_mock()
        self.redis = FakeRedis()
        self._channel("skipped", clients=[("c1", self.IP, "0", "TiviMate", 1)])
        probation._stop_channel("skipped")
        mock_stop.assert_called_once_with("skipped")

    def _real_skipped_channel(self):
        stream = Stream.objects.create(
            name="Skipped", url="http://a.example/live/u/p/9.ts", m3u_account=self.account
        )
        channel = Channel.objects.create(channel_number=990, name="Skipped")
        ChannelStream.objects.create(channel=channel, stream=stream, order=0)
        uuid = str(channel.uuid)
        self._channel(uuid, clients=[("c1", self.IP, "0", "TiviMate", 1)])
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
            [probation.identity_key(self.viewer, self.account)],
        )

    def test_there_is_no_window_for_an_identified_player(self, mock_stop, _mock_spawn):
        self._channel("inside", clients=[("c1", self.IP, "0", "TiviMate", 9)])
        self._channel("outside", clients=[("c2", self.IP, "0", "TiviMate", 3600)])

        self.assertEqual(sorted(self._stop()), ["inside", "outside"])


class NotUsedLoggingTests(SimpleTestCase):
    def setUp(self):
        _reset_in_use_cache(self)
        probation._not_used_logged.clear()
        self.addCleanup(probation._not_used_logged.clear)

    @patch("apps.proxy.live_proxy.probation.time.monotonic")
    def test_repeated_reason_is_logged_once_per_interval(self, mock_monotonic):
        viewer = probation.Viewer("10.0.0.2", app="TiviMate")
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

    def test_find_profiles_matches_ip_and_user(self):
        # Without an account (so without LAN Device Tracking) a viewer is IP + user
        redis = FakeRedis()
        self._add_client(redis, "ch-1", 41, "c1", ip_address="10.0.0.2", user_id="0")
        self._add_client(redis, "ch-2", 42, "c1", ip_address="10.0.0.2", user_id="7")
        self._add_client(redis, "ch-4", 44, "c1", ip_address="10.0.0.9", user_id="7")

        find = _profiles_watched_by
        self.assertEqual(find(redis, probation.Viewer("10.0.0.2")), [41])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.2", user_id=7)), [42])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.9", user_id=7)), [44])
        self.assertEqual(find(redis, probation.Viewer("10.0.0.7", user_id=7)), [])
        self.assertEqual(find(redis, None), [])

    def test_viewer_from_request(self):
        factory = RequestFactory()
        user = MagicMock(id=5)

        viewer = probation.viewer_from_request(
            factory.get("/proxy/ts/stream/x", HTTP_USER_AGENT="TiviMate/5.1.6 (Android 12)"),
            user,
            "10.0.0.2",
        )
        self.assertEqual(viewer, probation.Viewer("10.0.0.2", user_id=5))
        self.assertEqual(viewer.app, probation.app_name("TiviMate/5.1.6 (Android 12)"))
        # A login is enough on its own; without one an account has to recognise the address
        self.assertTrue(probation.is_identified(viewer))
        self.assertTrue(probation.may_be_identified(viewer))

        viewer = probation.viewer_from_request(factory.get("/proxy/ts/stream/x"), None, "10.0.0.2")
        self.assertEqual(viewer, probation.Viewer("10.0.0.2"))
        self.assertFalse(probation.is_identified(viewer))

        self.assertIsNone(probation.viewer_from_request(factory.get("/"), None, None))

    def test_recordings_are_recognised(self):
        request = RequestFactory().get(
            "/proxy/ts/stream/x", HTTP_USER_AGENT="Dispatcharr-DVR/recording-12"
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

    def test_media_server_stream_requests_stay_anonymous(self):
        request = RequestFactory().get(
            "/proxy/ts/stream/x", HTTP_USER_AGENT="Jellyfin-Server/10.10.7"
        )

        viewer = probation.viewer_from_request(request, None, "10.0.0.2")

        self.assertEqual(viewer, probation.Viewer("10.0.0.2"))
        self.assertIsNone(viewer.app)
        self.assertFalse(probation.may_be_identified(viewer))

    @patch.object(probation, "in_use", return_value=True)
    def test_record_client_viewer(self, _in_use):
        redis = FakeRedis()
        probation.record_client_viewer(redis, "ch-1", "c1", probation.Viewer("10.0.0.2", app="TiviMate"))
        probation.record_client_viewer(redis, "ch-1", "c2", probation.Viewer("10.0.0.3"))
        probation.record_client_viewer(redis, "ch-2", "c3", probation.Viewer("127.0.0.1", recording=True))

        self.assertEqual(
            redis.smembers(probation.CHANNEL_VIEWERS_KEY.format(channel_uuid="ch-1")),
            {"10.0.0.2|0|TiviMate|", "10.0.0.3|0||"},
        )
        # Recordings are not viewers
        self.assertEqual(redis.smembers(probation.CHANNEL_VIEWERS_KEY.format(channel_uuid="ch-2")), set())

    @patch.object(probation, "in_use", return_value=False)
    def test_record_client_viewer_does_nothing_while_not_in_use(self, _in_use):
        redis = FakeRedis()
        probation.record_client_viewer(redis, "ch-1", "c1", probation.Viewer("10.0.0.2", app="TiviMate"))
        self.assertEqual((redis.strings, redis.hashes, redis.sets), ({}, {}, {}))


class AccountProbationSettingsTests(TestCase):
    def test_account_settings_defaults_and_bounds(self):
        account = MagicMock(custom_properties={})
        self.assertFalse(probation.account_allows_probation(account))

        self.assertEqual(probation.account_probation_seconds(account), 10)

        self.assertFalse(probation.account_stops_skipped_channels(account))

        account.custom_properties = {
            "probation_enabled": True,
            "probation_seconds": 500,
            "probation_stop_skipped": True,
        }
        self.assertTrue(probation.account_allows_probation(account))

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

        self.assertEqual(probation.account_surf_delay_seconds(account), 0.5)
        for stored, seconds in ((0, 0), (1200, 1.2), (99999, 2.0), (-5, 0), ("bad", 0.5)):
            account.custom_properties = {"probation_surf_delay_ms": stored}
            self.assertEqual(probation.account_surf_delay_seconds(account), seconds)

    def test_serializer_round_trips_settings_in_custom_properties(self):
        account, _profile = _make_account("serializer-account")
        account.custom_properties = {**account.custom_properties, "enable_vod": True}
        account.save()

        serializer = M3UAccountSerializer(
            account,
            data={
                "probation_enabled": True,
                "probation_seconds": 30,
                    "probation_stop_skipped": True,
                "probation_surf_delay_ms": 800,
                "probation_account_preference": "alternate",
            },
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        account.refresh_from_db()
        self.assertTrue(account.custom_properties["probation_enabled"])
        self.assertEqual(account.custom_properties["probation_seconds"], 30)
        self.assertTrue(account.custom_properties["enable_vod"])

        data = M3UAccountSerializer(account).data
        self.assertTrue(data["probation_enabled"])
        self.assertEqual(data["probation_seconds"], 30)
        self.assertTrue(data["probation_stop_skipped"])
        self.assertEqual(data["probation_surf_delay_ms"], 800)
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


class StreamTsViewerTests(SimpleTestCase):
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
    def test_joining_client_is_recorded_as_a_viewer(self, _in_use, mock_proxy_cls, _network, mock_get_object, *_mocks):
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

        request = RequestFactory().get(
            f"/proxy/ts/stream/{channel_id}", HTTP_USER_AGENT="TiviMate/5.1.6 (Android 12)"
        )
        request.user = MagicMock(is_authenticated=False)
        stream_ts(request, channel_id)

        # The viewer is remembered for the channel (see CHANNEL_VIEWERS_KEY)
        self.assertEqual(
            redis.smembers(probation.CHANNEL_VIEWERS_KEY.format(channel_uuid=channel_id)),
            {f"127.0.0.1|0|{probation.app_name('TiviMate/5.1.6 (Android 12)')}|"},
        )


class StreamTsSkippedChannelOrderTests(SimpleTestCase):
    """stream_ts only stops skipped channels after trying for a slot (switching stays fast)."""

    CHANNEL_ID = "channel-uuid"
    OK = ("http://example/stream", "ua", False, "None", True, None, 42)
    FULL = (None, None, False, None, False, "All active M3U profiles have reached maximum connection limits", None)

    def _run(self, generate_results, expect_stream=True):
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

        request = RequestFactory().get(
            f"/proxy/ts/stream/{self.CHANNEL_ID}", HTTP_USER_AGENT="TiviMate/5.1.6 (Android 12)"
        )
        request.user = MagicMock(is_authenticated=False)
        response = stream_ts(request, self.CHANNEL_ID)
        if not expect_stream:
            return response
        self.assertIsInstance(response, StreamingHttpResponse)
        return calls

    def test_skipped_channels_are_stopped_after_the_slot_is_taken(self):
        self.assertEqual(self._run([self.OK]), ["get slot", "stop skipped (hold=False)"])

    def test_channel_passed_while_surfing_is_never_requested(self):
        with patch.object(probation, "skipped_while_surfing", return_value=True):
            response = self._run([self.OK], expect_stream=False)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.calls, [])

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

    def test_the_checks_are_told_to_let_go_before_a_slot_is_even_asked_for(self):
        """
        Somebody starting a channel is enough. It used to be asked only once the viewer
        had already been refused, which is no use at all when a check is holding the last
        connection of every provider the channel has: the viewer went to the fallback
        stream while the connections they needed were still being held.
        """
        with patch(
            "apps.channels.stream_check.make_way",
            side_effect=lambda _redis: self.calls.append("let go") or False,
        ):
            calls = self._run([self.OK])

        self.assertEqual(calls[0], "let go")
        self.assertEqual(calls, ["let go", "get slot", "stop skipped (hold=False)"])

    def test_a_viewer_waits_longer_when_a_check_is_holding_the_connections(self):
        """
        Three seconds is stock's guess at how long a provider might free one by itself.
        When a check has just been told to let go we know one is coming, and waiting for
        it beats sending somebody to the fallback stream.
        """
        with patch("apps.channels.stream_check.make_way", return_value=True):
            with self.assertLogs("live_proxy", level="INFO") as said:
                self._run([self.FULL, self.OK])
        self.assertTrue(
            any("waiting up to 12s for it to let go" in line for line in said.output),
            f"it kept stock's three seconds while a check held on: {said.output}",
        )

    def test_but_not_a_moment_longer_when_no_check_is_running(self):
        with patch("apps.channels.stream_check.make_way", return_value=False):
            with self.assertLogs("live_proxy", level="INFO") as said:
                self._run([self.FULL, self.OK])
        self.assertFalse(
            any("waiting up to" in line for line in said.output),
            "stock's wait is what a viewer gets when nothing of ours is at fault",
        )

    def test_and_a_check_that_cannot_be_asked_never_costs_the_viewer_the_channel(self):
        with patch(
            "apps.channels.stream_check.make_way", side_effect=RuntimeError("no Redis")
        ):
            calls = self._run([self.OK])

        self.assertEqual(calls, ["get slot", "stop skipped (hold=False)"])

    def test_not_even_when_it_is_asked_again_after_a_refusal(self):
        """
        The provider said no and the checks are asked a second time, in the retry. That one
        was not wrapped, so anything it raised came out as a failed channel -- the very
        thing the wrapping above it was put there to prevent.
        """
        with patch(
            "apps.channels.stream_check.make_way", side_effect=RuntimeError("no Redis")
        ):
            calls = self._run([self.FULL, self.OK])

        self.assertEqual(
            calls,
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
    def test_leftover_stopping_marker_is_removed(self, mock_sleep, _in_use, _teardown):
        # A second stop set the marker again after the channel was already cleaned up
        self.redis.setex(RedisKeys.channel_stopping(self.UUID), 60, "true")

        with self.assertLogs("live_proxy", level="INFO"):
            self.assertTrue(probation.wait_for_stop_to_finish(self.redis, self.UUID))

        self.assertFalse(self.redis.exists(RedisKeys.channel_stopping(self.UUID)))
        mock_sleep.assert_not_called()

    @patch("apps.proxy.live_proxy.probation.gevent.sleep")
    def test_marker_of_a_stop_that_is_still_running_is_kept(self, mock_sleep, _in_use, _teardown):
        self.redis.setex(RedisKeys.channel_stopping(self.UUID), 60, "true")
        self.redis.hset(RedisKeys.channel_metadata(self.UUID), ChannelMetadataField.STATE, ChannelState.STOPPING)

        self.assertFalse(probation.wait_for_stop_to_finish(self.redis, self.UUID))
        self.assertTrue(self.redis.exists(RedisKeys.channel_stopping(self.UUID)))

    @patch("apps.proxy.live_proxy.probation.gevent.sleep")
    def test_leftover_marker_is_not_touched_while_not_in_use(self, mock_sleep, in_use, _teardown):
        in_use.return_value = False
        self.redis.setex(RedisKeys.channel_stopping(self.UUID), 60, "true")

        self.assertFalse(probation.wait_for_stop_to_finish(self.redis, self.UUID))
        self.assertTrue(self.redis.exists(RedisKeys.channel_stopping(self.UUID)))

    def test_marker_left_behind_while_waiting_is_removed(self, _in_use, teardown):
        self._stopping()
        polls = []
        teardown.side_effect = lambda uuid: self.redis.exists(RedisKeys.channel_stopping(uuid)) > 0

        def sleep(_seconds):
            polls.append(1)
            if len(polls) == 1:
                # Our stop finished, but Dispatcharr's second stop left its marker behind
                self.redis.delete(probation._skipped_stopping_key(self.UUID))
                self.redis.setex(RedisKeys.channel_stopping(self.UUID), 60, "true")

        with patch("apps.proxy.live_proxy.probation.gevent.sleep", side_effect=sleep), self.assertLogs(
            "live_proxy", level="INFO"
        ):
            self.assertTrue(probation.wait_for_stop_to_finish(self.redis, self.UUID))

        self.assertEqual(len(polls), 1)
        self.assertFalse(self.redis.exists(RedisKeys.channel_stopping(self.UUID)))

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

    @patch("apps.proxy.live_proxy.server.ProxyServer.get_instance", return_value=MagicMock(_stopping_channels=set()))
    @patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
    def test_background_stop_clears_the_marker(self, mock_stop, _proxy, _in_use, _teardown):
        self._stopping()
        self.redis.hset(RedisKeys.channel_metadata(self.UUID), ChannelMetadataField.STATE, "active")
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


@patch("apps.proxy.live_proxy.probation.gevent.sleep")
class SurfingDelayTests(TestCase):
    """A channel a player passes while surfing is never requested from the provider."""

    IP = "192.168.1.20"

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, _profile = _make_account("surf-delay", probation_enabled=True, probation_seconds=10)
        self.channels = {}
        for number, name in ((1, "CNN"), (2, "Sky Sports"), (3, "Discovery")):
            stream = Stream.objects.create(
                name=name, url=f"http://a.example/live/u/p/{number}.ts", m3u_account=self.account
            )
            channel = Channel.objects.create(channel_number=800 + number, name=name)
            ChannelStream.objects.create(channel=channel, stream=stream, order=0)
            self.channels[name] = str(channel.uuid)
        self.proxy_server = MagicMock(redis_client=self.redis)
        self.proxy_server.check_if_channel_exists.return_value = False
        self.viewer = probation.Viewer(self.IP, app="TiviMate")

    def _key(self, viewer=None):
        # Per player, not per address: two apps on one device wait for themselves only
        return probation.LAST_REQUEST_KEY.format(
            viewer=probation._player_key(viewer or self.viewer)
        )

    def _requested(self, name, seconds_ago):
        self.redis.set(self._key(), f"{time.time() - seconds_ago}|earlier|{self.channels[name]}")

    def _request(self, name, viewer=None):
        return probation.skipped_while_surfing(self.proxy_server, viewer or self.viewer, self.channels[name])

    def test_first_channel_and_first_switch_start_at_once(self, mock_sleep):
        self.assertFalse(self._request("CNN"))
        # Watched CNN for a minute, then switched
        self._requested("CNN", seconds_ago=60)
        self.assertFalse(self._request("Sky Sports"))
        mock_sleep.assert_not_called()

    def test_switching_again_right_after_a_switch_waits(self, mock_sleep):
        self._requested("Sky Sports", seconds_ago=2)

        with self.assertLogs("live_proxy", level="INFO"):
            self.assertFalse(self._request("Discovery"))

        mock_sleep.assert_called_once_with(0.5)

    def test_channel_passed_during_the_delay_is_skipped(self, mock_sleep):
        self._requested("Sky Sports", seconds_ago=2)
        # The player moves on to CNN while the Discovery request waits
        mock_sleep.side_effect = lambda _s: self.redis.set(
            self._key(), f"{time.time()}|newer|{self.channels['CNN']}"
        )

        with self.assertLogs("live_proxy", level="INFO") as logs:
            self.assertTrue(self._request("Discovery"))

        self.assertTrue(any("not requesting it from the provider" in line for line in logs.output))

        # And it says so on the settings page, so a skipped channel is not a silent one
        ((event,),) = (probation.recent_events(self.redis),)
        self.assertEqual(event["action"], "skipped while surfing")
        self.assertEqual(event["channel"], self.channels["Discovery"])
        self.assertEqual(event["from_channel"], self.channels["Sky Sports"])
        self.assertEqual(event["result"], "moved on during the 0.5s delay")

    def test_no_delay_for_reconnects_running_channels_and_other_viewers(self, mock_sleep):
        self._requested("Discovery", seconds_ago=1)
        self.assertFalse(self._request("Discovery"))  # the player reconnecting

        self._requested("Sky Sports", seconds_ago=1)
        self.proxy_server.check_if_channel_exists.return_value = True
        self.assertFalse(self._request("Discovery"))  # already running
        self.proxy_server.check_if_channel_exists.return_value = False

        for viewer in (probation.Viewer(self.IP), probation.Viewer("127.0.0.1", recording=True)):
            self.redis.set(self._key(viewer), f"{time.time() - 1}|earlier|{self.channels['CNN']}")
            self.assertFalse(self._request("Discovery", viewer=viewer))
        mock_sleep.assert_not_called()

    def test_no_delay_when_the_account_does_not_ask_for_one(self, mock_sleep):
        self.account.custom_properties = {**self.account.custom_properties, "probation_surf_delay_ms": 0}
        self.account.save()
        self._requested("Sky Sports", seconds_ago=1)
        self.assertFalse(self._request("Discovery"))

        self.account.custom_properties = {"probation_enabled": False}
        self.account.save()
        self._requested("Sky Sports", seconds_ago=1)
        self.assertFalse(self._request("Discovery"))
        mock_sleep.assert_not_called()

    def test_nothing_is_stored_while_not_in_use(self, mock_sleep):
        with patch.object(probation, "in_use", return_value=False):
            self.assertFalse(self._request("CNN"))
        self.assertEqual((self.redis.strings, self.redis.hashes), ({}, {}))
        mock_sleep.assert_not_called()


class RefusalRetryDelayTests(TestCase):
    """A provider that closes new connections before sending data is not asked again at once."""

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, self.profile = _make_account("refusing", probation_enabled=True)
        self.stream = Stream.objects.create(
            name="Refused", url="http://a.example/live/u/p/7.ts", m3u_account=self.account
        )
        patcher = patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waits_longer_after_each_refusal(self):
        self.redis.set(f"stream_profile:{self.stream.id}", self.profile.id)

        with self.assertLogs("live_proxy", level="INFO") as logs:
            self.assertEqual(probation.refusal_retry_delay(self.stream.id, 1, True), 1.5)
            self.assertEqual(probation.refusal_retry_delay(self.stream.id, 2, True), 3.0)

        self.assertTrue(any("before sending any data" in line for line in logs.output))

    def test_uses_the_stream_account_without_a_profile_assignment(self):
        with self.assertLogs("live_proxy", level="INFO"):
            self.assertEqual(probation.refusal_retry_delay(self.stream.id, 1, True), 1.5)

    def test_normal_timing_otherwise(self):
        # Data arrived before the connection ended: not a refusal
        self.assertEqual(probation.refusal_retry_delay(self.stream.id, 1, False), 0)
        # Stream previews have no stream ID here
        self.assertEqual(probation.refusal_retry_delay(None, 1, True), 0)
        # Account without the overlap, while another account uses it
        _make_account("other-overlap-account", probation_enabled=True)
        self.account.custom_properties = {"probation_enabled": False}
        self.account.save()
        self.assertTrue(probation.in_use())
        self.assertEqual(probation.refusal_retry_delay(self.stream.id, 1, True), 0)
        # Overlap not used anywhere
        with patch.object(probation, "in_use", return_value=False):
            self.assertEqual(probation.refusal_retry_delay(self.stream.id, 1, True), 0)


class StreamManagerRefusalRetryTests(TestCase):
    """Dispatcharr's retry loop uses the longer wait only for refused connections."""

    def _run(self, sends_data):
        from apps.proxy.live_proxy.input.manager import StreamManager
        from apps.proxy.live_proxy.tests.test_health_reconnect import _make_manager

        sm = _make_manager(max_retries=3)
        sleeps = []

        def fake_establish():
            sm.connected = True
            return True

        def fake_process():
            if sends_data:
                sm.last_data_time += 1
            sm.connected = False

        def fake_try_next():
            sm.running = False
            return False

        def delay(stream_id, failures, refused):
            return 1.5 * failures if refused else 0

        with patch.object(StreamManager, "_monitor_health"), \
                patch.object(StreamManager, "_ensure_owner_or_stop", return_value=True), \
                patch.object(StreamManager, "_close_all_connections"), \
                patch.object(StreamManager, "_try_next_stream", side_effect=fake_try_next), \
                patch("apps.proxy.live_proxy.input.manager.close_old_connections"), \
                patch.object(sm, "_establish_http_connection", side_effect=fake_establish), \
                patch.object(sm, "_process_stream_data", side_effect=fake_process), \
                patch.object(sm, "_sleep_interruptible", side_effect=lambda s: sleeps.append(("long", s)) or True), \
                patch("apps.proxy.live_proxy.input.manager.gevent.sleep", side_effect=lambda s: sleeps.append(("short", s))), \
                patch("apps.proxy.live_proxy.input.manager.log_system_event"), \
                patch.object(probation, "refusal_retry_delay", side_effect=delay) as mock_delay:
            sm.run()
        return sleeps, mock_delay

    def test_refused_connections_wait_longer_between_attempts(self):
        sleeps, mock_delay = self._run(sends_data=False)

        self.assertEqual(sleeps, [("long", 1.5), ("long", 3.0)])
        self.assertEqual([c.args for c in mock_delay.call_args_list], [(100, 1, True), (100, 2, True)])

    def test_connections_that_sent_data_keep_the_normal_timing(self):
        sleeps, _mock_delay = self._run(sends_data=True)

        self.assertEqual([kind for kind, _seconds in sleeps], ["short", "short"])


class LanDeviceTrackingSettingsTests(TestCase):
    def setUp(self):
        _reset_in_use_cache(self)

    def test_app_name_ignores_versions(self):
        self.assertEqual(
            probation.app_name("TiviMate/5.1.6 (Android 12)"),
            probation.app_name("TiviMate/5.2.0 (Android 13)"),
        )
        self.assertNotEqual(probation.app_name("TiviMate/5.1.6"), probation.app_name("Kodi/21.0"))
        for no_app in (None, "", "Jellyfin-Server/10.10.7", "Dispatcharr-DVR/recording-4"):
            self.assertIsNone(probation.app_name(no_app))

    def test_parse_lan_subnets(self):
        self.assertEqual(
            probation.parse_lan_subnets("192.168.2.10/24, 10.0.0.0/8 192.168.2.0/24"),
            ["192.168.2.0/24", "10.0.0.0/8"],
        )
        self.assertEqual(probation.parse_lan_subnets(["192.168.1.5"]), ["192.168.1.5/32"])
        for bad in (["not-an-ip"], ["8.8.8.0/24"]):
            with self.assertRaises(ValueError):
                probation.parse_lan_subnets(bad)

    def test_lan_device_key(self):
        account = MagicMock(custom_properties={
            "probation_enabled": True,
            "probation_lan_subnets": ["192.168.2.0/24", "public-junk", "8.8.8.0/24"],
        })
        tv = probation.Viewer("192.168.2.232", app="TiviMate/")
        self.assertEqual(probation._lan_device_key(tv, account), "lan|192.168.2.232|0|TiviMate/")
        self.assertEqual(
            probation._lan_device_key(probation.Viewer("::ffff:192.168.2.232", app="TiviMate/"), account)
            is not None,
            True,
        )
        for no_key in (
            probation.Viewer("192.168.3.4", app="TiviMate/"),  # outside subnets
            probation.Viewer("8.8.8.8", app="TiviMate/"),  # public entry ignored
            probation.Viewer("192.168.2.232"),  # no app (media server)
            probation.Viewer("192.168.2.232", recording=True),
        ):
            self.assertIsNone(probation._lan_device_key(no_key, account))
        # No subnets means no LAN tracking at all
        account.custom_properties["probation_lan_subnets"] = []
        self.assertIsNone(probation._lan_device_key(tv, account))

    def test_suggested_lan_subnet(self):
        def suggestion(address):
            probation.suggested_lan_subnet.cache_clear()
            sock = MagicMock()
            sock.__enter__.return_value.getsockname.return_value = (address, 5000)
            with patch("apps.proxy.live_proxy.probation.socket.socket", return_value=sock):
                return probation.suggested_lan_subnet()

        self.addCleanup(probation.suggested_lan_subnet.cache_clear)
        self.assertEqual(suggestion("192.168.2.10"), "192.168.2.0/24")
        self.assertIsNone(suggestion("172.17.0.3"))  # Docker network
        self.assertIsNone(suggestion("81.2.3.4"))

    def test_serializer_stores_lan_settings(self):
        account, _profile = _make_account("lan-serializer", probation_enabled=True)
        serializer = M3UAccountSerializer(
            account,
            data={"probation_lan_subnets": "192.168.2.0/24,10.1.0.0/16"},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        account.refresh_from_db()
        self.assertEqual(account.custom_properties["probation_lan_subnets"], ["192.168.2.0/24", "10.1.0.0/16"])

        data = M3UAccountSerializer(account).data
        self.assertEqual(data["probation_lan_subnets"], ["192.168.2.0/24", "10.1.0.0/16"])
        self.assertIn("probation_lan_subnet_suggestion", data)

        invalid = M3UAccountSerializer(account, data={"probation_lan_subnets": ["8.8.8.0/24"]}, partial=True)
        self.assertFalse(invalid.is_valid())
        self.assertIn("probation_lan_subnets", invalid.errors)

    def test_lan_tracking_in_use_follows_account_changes(self):
        account, _profile = _make_account("lan-cache", probation_enabled=True, lan=False)
        self.assertFalse(probation.lan_tracking_in_use())
        account.custom_properties = {
            **account.custom_properties,
            "probation_lan_subnets": ["192.168.0.0/16"],
        }
        account.save()
        self.assertTrue(probation.lan_tracking_in_use())


class AbandonedSlotTests(TestCase):
    """Slots left behind by a restart, reboot or crash are released again."""

    NOW = 50_000.0

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, self.profile = _make_account("abandoned", probation_enabled=True)
        self.stream = Stream.objects.create(
            name="Left behind", url="http://a.example/live/u/p/473082.ts", m3u_account=self.account
        )
        self.channel = Channel.objects.create(channel_number=870, name="Left behind")
        ChannelStream.objects.create(channel=self.channel, stream=self.stream, order=0)

    def _assigned(self, channel=None, stream=None):
        channel = channel or self.channel
        stream = stream or self.stream
        self.redis.set(f"channel_stream:{channel.id}", stream.id)
        self.redis.set(f"stream_profile:{stream.id}", self.profile.id)
        self.redis.incr(profile_connections_key(self.profile.id))

    def _sweep(self, now):
        # The lock would keep a second sweep within SWEEP_INTERVAL from running
        self.redis.delete(probation.SWEEP_LOCK_KEY)
        return probation.release_abandoned_slots(self.redis, now=now)

    def test_slot_of_a_channel_that_stopped_without_cleanup_is_released(self):
        # State after the restart: assignment and counter left, no live metadata
        self._assigned()
        key = f"channel_stream:{self.channel.id}"

        self.assertEqual(self._sweep(self.NOW), [])  # first seen
        self.assertEqual(self._sweep(self.NOW + probation.ABANDONED_SLOT_GRACE - 1), [])
        with self.assertLogs("live_proxy", level="WARNING"):
            self.assertEqual(self._sweep(self.NOW + probation.ABANDONED_SLOT_GRACE), [key])

        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "0")
        self.assertIsNone(self.redis.get(key))
        self.assertIsNone(self.redis.get(f"stream_profile:{self.stream.id}"))
        self.assertEqual(self.redis.hgetall(probation.ABANDONED_SLOTS_KEY), {})

    def test_running_channels_keep_their_slot(self):
        self._assigned()
        metadata_key = RedisKeys.channel_metadata(str(self.channel.uuid))
        self.redis.hset(metadata_key, mapping={"state": "active"})
        self.redis.expire(metadata_key, 30)

        for offset in (0, 120, 600):
            self.assertEqual(self._sweep(self.NOW + offset), [])
        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "1")

    def test_channel_that_starts_running_again_is_forgotten(self):
        # Between reserving a slot and creating its metadata, a starting channel looks stopped
        self._assigned()
        self._sweep(self.NOW)
        self.redis.hset(RedisKeys.channel_metadata(str(self.channel.uuid)), mapping={"state": "active"})
        self.redis.expire(RedisKeys.channel_metadata(str(self.channel.uuid)), 30)
        self._sweep(self.NOW + 30)
        self.redis.delete(RedisKeys.channel_metadata(str(self.channel.uuid)))

        # The grace period starts over
        self.assertEqual(self._sweep(self.NOW + 70), [])
        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "1")

    def test_stream_preview_leftovers(self):
        Stream.objects.filter(id=self.stream.id).update(stream_hash="a83d7d20" * 8)
        # An ended preview keeps an empty assignment: removed, nothing released
        self.redis.set(f"channel_stream:{self.stream.id}", self.stream.id)
        self._sweep(self.NOW)
        with self.assertLogs("live_proxy", level="INFO"):
            self.assertEqual(self._sweep(self.NOW + 61), [])
        self.assertIsNone(self.redis.get(f"channel_stream:{self.stream.id}"))

        # A preview that is still playing keeps its slot
        self.redis.set(f"channel_stream:{self.stream.id}", self.stream.id)
        self.redis.set(f"stream_profile:{self.stream.id}", self.profile.id)
        self.redis.set(profile_connections_key(self.profile.id), 1)
        self.redis.hset(RedisKeys.channel_metadata("a83d7d20" * 8), mapping={"state": "active"})
        self.redis.expire(RedisKeys.channel_metadata("a83d7d20" * 8), 30)
        self._sweep(self.NOW + 100)
        self.assertEqual(self._sweep(self.NOW + 200), [])
        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "1")

    def test_stream_shared_with_a_running_channel_keeps_its_record(self):
        running = Channel.objects.create(channel_number=871, name="Same stream, running")
        ChannelStream.objects.create(channel=running, stream=self.stream, order=0)
        self._assigned()
        self._assigned(channel=running)
        metadata_key = RedisKeys.channel_metadata(str(running.uuid))
        self.redis.hset(metadata_key, mapping={"state": "active"})
        self.redis.expire(metadata_key, 30)

        self._sweep(self.NOW)
        with self.assertLogs("live_proxy", level="INFO"):
            self.assertEqual(self._sweep(self.NOW + 61), [])

        # Only the stopped channel's assignment goes; the running channel keeps the record
        self.assertIsNone(self.redis.get(f"channel_stream:{self.channel.id}"))
        self.assertEqual(self.redis.get(f"stream_profile:{self.stream.id}"), str(self.profile.id))
        self.assertEqual(self.redis.get(f"channel_stream:{running.id}"), str(self.stream.id))

    def test_only_one_worker_sweeps_per_interval(self):
        self._assigned()
        probation.release_abandoned_slots(self.redis, now=self.NOW)
        # Within the interval the lock is held: nothing is recorded or released
        self.redis.hdel(probation.ABANDONED_SLOTS_KEY, f"channel_stream:{self.channel.id}")
        self.assertEqual(probation.release_abandoned_slots(self.redis, now=self.NOW + 500), [])
        self.assertEqual(self.redis.hgetall(probation.ABANDONED_SLOTS_KEY), {})

    def test_works_without_the_overlap(self):
        self._assigned()
        self.account.custom_properties = {"probation_enabled": False}
        self.account.save()
        self.assertFalse(probation.in_use())
        self._sweep(self.NOW)
        with self.assertLogs("live_proxy", level="WARNING"):
            self.assertEqual(len(self._sweep(self.NOW + 61)), 1)
        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "0")

    def test_any_live_key_keeps_the_slot(self):
        # Metadata lost (for example a stalled owner) while the stream still writes its buffer
        self._assigned()
        uuid = str(self.channel.uuid)
        for live_key in (
            RedisKeys.buffer_chunk(uuid, 12),
            RedisKeys.clients(uuid),
            RedisKeys.channel_stopping(uuid),
            RedisKeys.last_client_disconnect(uuid),
        ):
            self.redis = FakeRedis()
            self._assigned()
            self.redis.setex(live_key, 60, "1")
            for offset in (0, 120, 600):
                self.assertEqual(self._sweep(self.NOW + offset), [], live_key)
            self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "1")

    def test_keys_without_an_expiry_do_not_keep_the_slot(self):
        # After an unclean stop the buffer index (it never expires) and metadata written
        # without TTL stay behind; they do not prove the channel still runs
        self._assigned()
        uuid = str(self.channel.uuid)
        self.redis.set(RedisKeys.buffer_index(uuid), 4812)
        self._sweep(self.NOW)
        with self.assertLogs("live_proxy", level="WARNING"):
            self.assertEqual(len(self._sweep(self.NOW + 61)), 1)
        self.assertEqual(self.redis.get(profile_connections_key(self.profile.id)), "0")

    def test_nothing_to_do_without_assignments(self):
        with patch.object(probation, "_live_channel_ids") as live_ids:
            self.assertEqual(self._sweep(self.NOW), [])
        live_ids.assert_not_called()


class RecentSwitchesTests(TestCase):
    """What the Channel Switch Overlap page shows: the last switches and their result."""

    IP = "192.168.1.20"

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, self.profile = _make_account("events", probation_enabled=True)
        self.viewer = probation.Viewer(self.IP, app="TiviMate")

    def test_an_event_is_recorded_and_its_result_filled_in(self):
        event_id = probation.record_event(
            self.redis,
            self.viewer,
            "overlap slot",
            channel="channel-b",
            from_channel="channel-a",
            account=self.account.name,
            result="waiting",
        )
        probation.update_event(self.redis, event_id, result="confirmed after 1.3s")

        (event,) = probation.recent_events(self.redis)
        self.assertEqual(event["action"], "overlap slot")
        self.assertEqual(event["ip"], self.IP)
        self.assertEqual(event["app"], "TiviMate")
        self.assertEqual(event["from_channel"], "channel-a")
        self.assertEqual(event["channel"], "channel-b")
        self.assertEqual(event["result"], "confirmed after 1.3s")

    def test_an_ordinary_switch_is_reported_too(self):
        viewer = probation.Viewer(self.IP, app="TiviMate")

        # The first channel is not a switch
        probation.record_switch(self.redis, viewer, "channel-a")
        self.assertEqual(probation.recent_events(self.redis), [])

        probation.record_switch(self.redis, viewer, "channel-b")
        (event,) = probation.recent_events(self.redis)
        self.assertEqual(event["action"], "switched")
        self.assertEqual(event["from_channel"], "channel-a")
        self.assertEqual(event["channel"], "channel-b")
        self.assertEqual(event["result"], "a slot was free")

        # The same channel again is not a switch either
        probation.record_switch(self.redis, viewer, "channel-b")
        self.assertEqual(len(probation.recent_events(self.redis)), 1)

    def test_a_switch_the_overlap_reported_is_not_reported_twice(self):
        viewer = probation.Viewer(self.IP, app="TiviMate")
        probation.record_switch(self.redis, viewer, "channel-a")
        # The overlap has already said what it did with this one
        probation.record_event(
            self.redis, viewer, "overlap slot", channel="channel-b", result="waiting"
        )

        probation.record_switch(self.redis, viewer, "channel-b")

        actions = [event["action"] for event in probation.recent_events(self.redis)]
        self.assertEqual(actions, ["overlap slot"])

    def test_a_switch_by_a_viewer_it_cannot_tell_apart_says_why(self):
        """
        Which of the three ways of being identified failed, not just that one did.

        They fail for different reasons and need different things done about them, and the
        cost is a switch without its overlap and a channel left running, so the page saying
        only "cannot tell apart" leaves nothing to act on.
        """
        viewer = probation.Viewer(self.IP)  # no login, no app, no media server device
        probation.record_switch(self.redis, viewer, "channel-a")
        probation.record_switch(self.redis, viewer, "channel-b")

        (event,) = probation.recent_events(self.redis)
        self.assertEqual(event["action"], "not used")
        self.assertEqual(event["result"], "nothing in the request says which player it is")

    def test_the_reason_given_fits_what_was_missing(self):
        """
        The usual one is a media server: more than one of its players is streaming, so the
        request could belong to either and it says so rather than "cannot tell apart".
        """
        self.assertIn(
            "did not say which of its players",
            probation.why_not_identified(probation.Viewer(self.IP, app="Lavf/60.3.100")),
        )
        self.assertEqual(
            probation.why_not_identified(probation.Viewer(self.IP)),
            "nothing in the request says which player it is",
        )
        self.assertIn(
            "LAN subnets",
            probation.why_not_identified(probation.Viewer(self.IP, app="VLC")),
        )

    def test_only_the_last_switches_are_kept(self):
        for number in range(probation.EVENTS_KEPT + 5):
            probation.record_event(self.redis, self.viewer, "not used", channel=f"channel-{number}")

        events = probation.recent_events(self.redis)
        self.assertEqual(len(events), probation.EVENTS_KEPT)
        # Newest first, and older ones are dropped instead of piling up
        self.assertEqual(events[0]["channel"], f"channel-{probation.EVENTS_KEPT + 4}")
        self.assertEqual(len(self.redis.zsets[probation.EVENTS_KEY]), probation.EVENTS_KEPT)

    def test_events_without_a_viewer_and_missing_ones_are_handled(self):
        probation.record_event(self.redis, None, "provider refused", account=self.account.name)
        (event,) = probation.recent_events(self.redis)
        self.assertEqual(event["ip"], "")

        # An event that expired is skipped instead of breaking the list
        self.redis.delete(probation._event_key(self.redis.zrevrange(probation.EVENTS_KEY, 0, 0)[0]))
        self.assertEqual(probation.recent_events(self.redis), [])

    def test_a_switch_with_the_overlap_shows_up_with_its_result(self):
        # A real switch: the viewer watches channel A, every account is full, it starts B
        stream = Stream.objects.create(
            name="A", url="http://a.example/live/u/p/1.ts", m3u_account=self.account
        )
        channel_a = Channel.objects.create(channel_number=940, name="Channel A")
        channel_b = Channel.objects.create(channel_number=941, name="Channel B")
        ChannelStream.objects.create(channel=channel_b, stream=stream, order=0)
        self.redis.set(profile_connections_key(self.profile.id), 1)
        self.redis.hset(
            RedisKeys.channel_metadata(str(channel_a.uuid)),
            mapping={ChannelMetadataField.M3U_PROFILE: self.profile.id},
        )
        self.redis.sadd(RedisKeys.clients(str(channel_a.uuid)), "c1")
        self.redis.hset(
            RedisKeys.client_metadata(str(channel_a.uuid), "c1"),
            mapping={"ip_address": self.IP, "user_id": "0", "user_agent": "TiviMate"},
        )
        stream_profile = MagicMock()
        stream_profile.is_redirect.return_value = False

        with patch("apps.channels.models.RedisClient.get_client", return_value=self.redis), patch(
            "core.utils.RedisClient.get_client", return_value=self.redis
        ), patch.object(Channel, "get_stream_profile", return_value=stream_profile), patch(
            "apps.channels.models.Channel._pick_channel_to_preempt", return_value=None
        ):
            channel_b.get_stream(viewer=self.viewer)

        (event,) = probation.recent_events(self.redis)
        self.assertEqual(event["action"], "overlap slot")
        self.assertEqual(event["from_channel"], str(channel_a.uuid))
        self.assertEqual(event["channel"], str(channel_b.uuid))
        self.assertEqual(event["account"], self.account.name)
        self.assertEqual(event["result"], "waiting")


class DiagnosticsViewTests(TestCase):
    """The Diagnostics page's data: accounts and readable switches, for admins only."""

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, self.profile = _make_account("activity", probation_enabled=True)
        patcher = patch("core.utils.RedisClient.get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self, admin=True):
        from apps.accounts.models import User
        from rest_framework.test import APIClient

        client = APIClient()
        user = User.objects.create_user(
            username=f"user-{'admin' if admin else 'viewer'}-{User.objects.count()}",
            password="x",
            user_level=10 if admin else 0,
        )
        client.force_authenticate(user=user)
        return client

    def _get(self, admin=True):
        return self._client(admin).get("/proxy/diagnostics/")

    def test_admins_see_accounts_and_switches(self):
        channel = Channel.objects.create(channel_number=942, name="Sky Sports")
        previous = Channel.objects.create(channel_number=943, name="CNN")
        self.redis.set(profile_connections_key(self.profile.id), 1)
        probation.record_event(
            self.redis,
            probation.Viewer("192.168.1.20", app="TiviMate"),
            "overlap slot",
            channel=channel.uuid,
            from_channel=previous.uuid,
            account=self.account.name,
            result="confirmed after 1.2s",
        )

        data = self._get().json()

        self.assertTrue(data["enabled"])
        self.assertEqual(
            data["accounts"],
            [{
                "account": self.account.name,
                "profile": self.profile.name,
                "in_use": 1,
                "max_streams": 1,
                "held_slots": 0,
                "lan_subnets": ["192.168.0.0/16"],
                "stop_skipped": False,
                "switch_preference": "order",
            }],
        )
        (event,) = data["events"]
        self.assertEqual(event["viewer"], "192.168.1.20 · TiviMate")
        self.assertEqual(event["from_channel"], "CNN")
        self.assertEqual(event["channel"], "Sky Sports")
        self.assertEqual(event["action"], "overlap slot")
        self.assertEqual(event["result"], "confirmed after 1.2s")

    def test_channel_starts_are_shown_with_their_phases(self):
        from apps.proxy.live_proxy import timing

        timing.start(self.redis, "channel-1", client="TiviMate/5.1.6 (Android 12)")
        now = time.time()
        self.redis.hset(timing._key("channel-1"), "requested", str(now))
        for phase, offset in (("slot", 0.1), ("first_keyframe", 3.9), ("first_byte_out", 4.0)):
            self.redis.hset(timing._key("channel-1"), phase, str(now + offset))
        timing.finish(self.redis, "channel-1", "CNN")

        (start,) = self._get().json()["starts"]

        self.assertEqual(start["channel"], "CNN")
        # The app, without its version numbers, like everywhere else
        self.assertEqual(start["client"], "TiviMate/ (Android )")
        self.assertAlmostEqual(start["total"], 4.0, places=2)
        self.assertEqual(start["slowest"], "first keyframe")
        # Each phase says how far in it was reached and how long it took on its own
        keyframe = next(p for p in start["phases"] if p["label"] == "first keyframe")
        self.assertAlmostEqual(keyframe["at"], 3.9, places=2)
        self.assertAlmostEqual(keyframe["took"], 3.8, places=2)

    def test_a_media_server_viewer_is_shown_by_who_is_watching(self):
        from apps.proxy.live_proxy import media_servers

        self.redis.setex(
            media_servers.DEVICE_NAME_KEY.format(device="server|apple-tv"), 60, "Ckegels · Chrome"
        )
        probation.record_event(
            self.redis,
            probation.Viewer("192.168.2.30", server_device="server|apple-tv"),
            "another account",
        )

        (event,) = self._get().json()["events"]
        # Not the media server's address, which is the same for everyone behind it
        self.assertEqual(event["viewer"], "Ckegels · Chrome")

    def test_a_login_is_shown_by_name(self):
        from apps.accounts.models import User

        viewer_user = User.objects.create_user(username="chris", password="x")
        probation.record_event(
            self.redis, probation.Viewer("10.0.0.5", user_id=viewer_user.id), "held slot"
        )

        (event,) = self._get().json()["events"]
        self.assertEqual(event["viewer"], "chris")

    def test_nothing_is_shown_while_the_overlap_is_off(self):
        self.account.custom_properties = {"probation_enabled": False}
        self.account.save()
        probation.record_event(self.redis, probation.Viewer("10.0.0.5"), "held slot")

        data = self._get().json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["events"], [])
        self.assertEqual(data["accounts"], [])

    def test_only_admins(self):
        self.assertEqual(self._get(admin=False).status_code, 403)
        self.assertEqual(
            self._client(admin=False)
            .post("/proxy/diagnostics/", {"keep_seconds": 7200}, format="json")
            .status_code,
            403,
        )

    def test_how_long_switches_are_kept_can_be_changed(self):
        self.assertEqual(self._get().json()["keep_seconds"], probation.EVENT_TTL)

        response = self._client().post(
            "/proxy/diagnostics/", {"keep_seconds": 7200}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        # The answer is the page itself, with the new setting
        self.assertEqual(response.json()["keep_seconds"], 7200)
        self.assertEqual(self._get().json()["keep_seconds"], 7200)
        self.assertEqual(self._get().json()["keep_choices"], list(probation.EVENT_TTL_CHOICES))

        # A switch recorded now lives that long
        probation.record_event(self.redis, probation.Viewer("10.0.0.5"), "held slot")
        event_id = probation._as_str(self.redis.zrevrange(probation.EVENTS_KEY, 0, 0)[0])
        self.assertEqual(self.redis.ttl(probation._event_key(event_id)), 7200)

    def test_an_unknown_retention_is_refused(self):
        for bad in (99, "soon", None):
            response = self._client().post(
                "/proxy/diagnostics/", {"keep_seconds": bad}, format="json"
            )
            self.assertEqual(response.status_code, 400, bad)
        self.assertEqual(self._get().json()["keep_seconds"], probation.EVENT_TTL)



class NeverBreaksAStreamTests(TestCase):
    """The parts a stream request reaches must fail quietly, not fail the request."""

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, self.profile = _make_account("guarded", probation_enabled=True)
        self.channel = Channel.objects.create(channel_number=990, name="Guarded")

    def test_the_three_entry_points_give_up_instead_of_raising(self):
        """get_stream() calls these directly, so an error here would be a failed stream."""
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("redis is down")
        broken.hgetall.side_effect = RuntimeError("redis is down")
        broken.scan_iter.side_effect = RuntimeError("redis is down")
        viewer = probation.Viewer("192.168.1.20", app="TiviMate")

        # Whatever they were part way through, they answer "no slot" rather than raising
        self.assertIsNone(probation.reserve_sticky_slot(self.channel, broken, viewer))
        self.assertIsNone(probation.reserve_alternate_slot(self.channel, broken, viewer))
        with self.assertLogs("live_proxy", level="ERROR") as logs:
            self.assertIsNone(
                probation.reserve_overlap_slot(self.channel, broken, viewer, [(None, None)])
            )
        self.assertTrue(
            any("could not give a viewer the overlap slot" in line for line in logs.output)
        )

    def test_saying_why_the_overlap_was_not_used_cannot_break_a_reservation(self):
        """It runs on every capacity check, and asks for a Redis client to record it."""
        with patch("core.utils.RedisClient.get_client", side_effect=RuntimeError("no redis")):
            with self.assertLogs("live_proxy", level="INFO"):
                probation.log_not_used(self.channel.uuid, probation.Viewer("10.0.0.2"), "why")

    def test_a_hold_on_a_shared_login_also_ends_when_the_account_is_switched_off(self):
        viewer = probation.Viewer("10.0.0.2", app="TiviMate")
        other = probation.Viewer("10.0.0.3", app="Kodi")
        self.redis.hset(
            probation._held_login_slots_key("group-1"),
            probation.identity_key(other, self.account),
            f"{time.time() + 30}|{self.profile.id}",
        )

        self.assertEqual(
            probation.slots_held_for_others(
                self.redis, self.profile, viewer, credential_key="group-1"
            ),
            1,
        )

        # Switching the overlap off releases the hold on both counters, not just its own
        self.account.custom_properties = {"probation_enabled": False}
        self.account.save()
        probation.forget_in_use()
        self.assertEqual(
            probation.slots_held_for_others(
                self.redis, self.profile, viewer, credential_key="group-1"
            ),
            0,
        )

    def test_the_surfing_delay_is_per_player_not_per_address(self):
        """Two apps on one device, or one login on two, must not wait for each other."""
        tivimate = probation.Viewer("10.0.0.2", app="TiviMate")
        kodi = probation.Viewer("10.0.0.2", app="Kodi")
        self.assertNotEqual(probation._player_key(tivimate), probation._player_key(kodi))
        # And a media server device is its own player too
        plex_one = probation.Viewer("10.0.0.5", server_device="server|apple-tv")
        plex_two = probation.Viewer("10.0.0.5", server_device="server|living-room")
        self.assertNotEqual(probation._player_key(plex_one), probation._player_key(plex_two))
