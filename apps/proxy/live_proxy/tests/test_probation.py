"""Tests for probation slots during live channel switches."""

import fnmatch
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from apps.channels.models import Channel, ChannelStream, Stream
from apps.m3u.connection_pool import profile_connections_key, reserve_profile_slot
from apps.m3u.models import M3UAccount, M3UAccountProfile
from apps.m3u.serializers import M3UAccountSerializer
from apps.proxy.live_proxy import probation
from apps.proxy.live_proxy.constants import ChannelMetadataField
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

    def set(self, key, value):
        self.strings[key] = str(value)

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

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(str(m) for m in members)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def scan_iter(self, match="*", count=None):
        keys = set(self.strings) | set(self.hashes) | set(self.sets)
        return [k for k in keys if fnmatch.fnmatchcase(k, match)]


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
    VIEWER_IP = "192.168.1.20"

    def setUp(self):
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

    def _disable(self, account):
        account.custom_properties = {**account.custom_properties, "probation_enabled": False}
        account.save()

    def _watching(self, channel_uuid, profile, client_ip):
        self.redis.hset(
            RedisKeys.channel_metadata(channel_uuid),
            mapping={ChannelMetadataField.M3U_PROFILE: profile.id},
        )
        self.redis.sadd(RedisKeys.clients(channel_uuid), "client_1")
        self.redis.hset(
            RedisKeys.client_metadata(channel_uuid, "client_1"),
            mapping={"ip_address": client_ip, "user_agent": "TiviMate/5.1", "user_id": "0"},
        )

    def _deadline_seconds(self):
        record = self.redis.hgetall(probation.probation_key(self.channel.uuid))
        return round(float(record["deadline"]) - float(record["started_at"]))

    def _assert_limit_error(self, result):
        stream_id, _profile_id, error, _reserved = result
        self.assertIsNone(stream_id)
        self.assertIn("maximum connection limits", error)
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "1")
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_b.id)), "1")

    def test_switching_viewer_starts_on_probation_on_its_profile(self, _preempt):
        self._fill_both()
        self._watching("old-channel", self.profile_b, self.VIEWER_IP)

        result = self.channel.get_stream(viewer_ip=self.VIEWER_IP)

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "1")
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_b.id)), "2")
        self.assertEqual(self.redis.get(f"channel_stream:{self.channel.id}"), str(self.stream_b.id))
        record = self.redis.hgetall(probation.probation_key(self.channel.uuid))
        self.assertEqual(record["profile_id"], str(self.profile_b.id))
        self.assertEqual(self._deadline_seconds(), 25)

    def test_default_window_when_account_has_no_seconds(self, _preempt):
        self._fill_both()
        self._watching("old-channel", self.profile_a, self.VIEWER_IP)

        _stream_id, profile_id, _error, _reserved = self.channel.get_stream(viewer_ip=self.VIEWER_IP)

        self.assertEqual(profile_id, self.profile_a.id)
        self.assertEqual(self._deadline_seconds(), probation.DEFAULT_PROBATION_SECONDS)

    def test_new_ip_gets_limit_error(self, _preempt):
        self._fill_both()
        self._watching("old-channel", self.profile_b, "192.168.1.99")

        self._assert_limit_error(self.channel.get_stream(viewer_ip=self.VIEWER_IP))

    def test_no_viewer_ip_never_uses_probation(self, _preempt):
        self._fill_both()
        self._watching("old-channel", self.profile_a, self.VIEWER_IP)

        self._assert_limit_error(self.channel.get_stream())

    def test_watching_on_account_without_probation_gets_limit_error(self, _preempt):
        self._disable(self.account_a)
        self._fill_both()
        self._watching("old-channel", self.profile_a, self.VIEWER_IP)

        self._assert_limit_error(self.channel.get_stream(viewer_ip=self.VIEWER_IP))

    def test_redirect_profiles_never_use_probation(self, _preempt):
        self.stream_profile.is_redirect.return_value = True
        self._fill_both()
        self._watching("old-channel", self.profile_a, self.VIEWER_IP)

        self._assert_limit_error(self.channel.get_stream(viewer_ip=self.VIEWER_IP))

    def test_free_capacity_is_used_without_probation(self, _preempt):
        self.redis.set(profile_connections_key(self.profile_a.id), 1)
        self._watching("old-channel", self.profile_a, self.VIEWER_IP)

        result = self.channel.get_stream(viewer_ip=self.VIEWER_IP)

        self.assertEqual(result, (self.stream_b.id, self.profile_b.id, None, True))
        self.assertFalse(probation.has_pending_probation(self.redis, self.channel.uuid))

    def test_only_one_probation_slot_per_profile(self, _preempt):
        self._fill_both()
        self._watching("old-channel", self.profile_a, self.VIEWER_IP)

        _stream_id, profile_id, _error, _reserved = self.channel.get_stream(viewer_ip=self.VIEWER_IP)
        self.assertEqual(profile_id, self.profile_a.id)

        other = Channel.objects.create(channel_number=901, name="Sports")
        ChannelStream.objects.create(channel=other, stream=self.stream_a, order=0)
        ChannelStream.objects.create(channel=other, stream=self.stream_b, order=1)
        stream_id, _profile_id, error, _reserved = other.get_stream(viewer_ip=self.VIEWER_IP)

        self.assertIsNone(stream_id)
        self.assertIn("maximum connection limits", error)
        self.assertEqual(self.redis.get(profile_connections_key(self.profile_a.id)), "2")


class ResolveProbationTests(TestCase):
    def setUp(self):
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


class WatchedProfilesTests(SimpleTestCase):
    def test_finds_profiles_watched_by_ip(self):
        redis = FakeRedis()
        for channel_uuid, profile_id, ip in (
            ("ch-1", 42, "10.0.0.2"),
            ("ch-2", 43, "10.0.0.9"),
            ("ch-3", 44, "10.0.0.2"),
        ):
            redis.hset(
                RedisKeys.channel_metadata(channel_uuid),
                mapping={ChannelMetadataField.M3U_PROFILE: profile_id},
            )
            redis.sadd(RedisKeys.clients(channel_uuid), "c1")
            redis.hset(
                RedisKeys.client_metadata(channel_uuid, "c1"),
                mapping={"ip_address": ip, "user_agent": "Kodi/21", "user_id": "0"},
            )

        self.assertEqual(
            sorted(probation.find_profile_ids_watched_by_ip(redis, "10.0.0.2")), [42, 44]
        )
        self.assertEqual(probation.find_profile_ids_watched_by_ip(redis, "10.0.0.7"), [])
        self.assertEqual(probation.find_profile_ids_watched_by_ip(redis, None), [])


class AccountProbationSettingsTests(TestCase):
    def test_account_settings_defaults_and_bounds(self):
        account = MagicMock(custom_properties={})
        self.assertFalse(probation.account_allows_probation(account))
        self.assertEqual(probation.account_probation_seconds(account), 10)

        account.custom_properties = {"probation_enabled": True, "probation_seconds": 500}
        self.assertTrue(probation.account_allows_probation(account))
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
            data={"probation_enabled": True, "probation_seconds": 30},
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

    def test_serializer_rejects_out_of_range_window(self):
        account, _profile = _make_account("serializer-bounds")
        for seconds in (0, 121):
            serializer = M3UAccountSerializer(
                account, data={"probation_seconds": seconds}, partial=True
            )
            self.assertFalse(serializer.is_valid())
