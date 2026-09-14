"""Tests for probation slots during live channel switches."""

import fnmatch
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, TestCase

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
    IP = "192.168.1.20"

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


@patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_channel")
class StopSkippedChannelsTests(TestCase):
    NOW = 10_000.0
    IP = "192.168.1.20"

    def setUp(self):
        self.redis = FakeRedis()
        self.account, self.profile = _make_account(
            "surf-account", probation_enabled=True, probation_seconds=10
        )
        self._set_props(self.account, probation_stop_skipped=True)
        self.viewer = probation.Viewer(self.IP, device_id="tv1")

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

    def test_surfing_stops_skipped_channels_but_keeps_watched_one(self, mock_stop):
        self._channel("watched", clients=[("c1", self.IP, "0", "tv1", 600)])
        self._channel("skipped-1", clients=[("c2", self.IP, "0", "tv1", 2)])
        self._channel("skipped-2", clients=[("c3", self.IP, "0", "tv1", 1)])

        with self.assertLogs("live_proxy", level="INFO") as logs:
            stopped = self._stop()

        self.assertEqual(sorted(stopped), ["skipped-1", "skipped-2"])
        self.assertEqual(sorted(c.args[0] for c in mock_stop.call_args_list), ["skipped-1", "skipped-2"])
        self.assertTrue(any("Probation: stopping skipped channel" in line for line in logs.output))

    def test_requested_channel_is_never_stopped(self, mock_stop):
        self._channel("channel-new", clients=[("c1", self.IP, "0", "tv1", 1)])

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_shared_channel_is_never_stopped(self, mock_stop):
        self._channel(
            "shared",
            clients=[("c1", self.IP, "0", "tv1", 1), ("c2", "192.168.1.30", "0", "tv2", 300)],
        )

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_reconnect_on_watched_channel_does_not_count_as_skipped(self, mock_stop):
        self._channel(
            "watched",
            clients=[("c1", self.IP, "0", "tv1", 600), ("c2", self.IP, "0", "tv1", 1)],
        )

        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_other_device_or_user_channels_are_never_stopped(self, mock_stop):
        self._channel("other-device", clients=[("c1", self.IP, "0", "phone", 1)])
        self._channel("other-user", clients=[("c2", self.IP, "8", None, 1)])
        self._channel("other-ip", clients=[("c3", "10.0.0.9", "0", "tv1", 1)])

        self.assertEqual(self._stop(), [])
        self.assertEqual(self._stop(viewer=probation.Viewer(self.IP, user_id=7)), [])
        mock_stop.assert_not_called()

    def test_anonymous_viewer_never_stops_anything(self, mock_stop):
        self._set_props(self.account, probation_allow_anonymous=True)
        self._channel("anonymous", clients=[("c1", self.IP, "0", None, 1)])

        with patch.object(probation, "any_account_stops_skipped_channels") as mock_any:
            self.assertEqual(self._stop(viewer=probation.Viewer(self.IP)), [])

        mock_any.assert_not_called()
        mock_stop.assert_not_called()

    def test_account_settings_are_required(self, mock_stop):
        self._channel("skipped", clients=[("c1", self.IP, "0", "tv1", 1)])

        self._set_props(self.account, probation_stop_skipped=False)
        with patch.object(self.redis, "scan_iter", wraps=self.redis.scan_iter) as mock_scan:
            self.assertEqual(self._stop(), [])
        mock_scan.assert_not_called()

        self._set_props(self.account, probation_stop_skipped=True, probation_enabled=False)
        self.assertEqual(self._stop(), [])
        mock_stop.assert_not_called()

    def test_only_accounts_with_the_option_are_affected(self, mock_stop):
        _other_account, other_profile = _make_account("no-stop", probation_enabled=True)
        self._channel("skipped", clients=[("c1", self.IP, "0", "tv1", 1)])
        self._channel("other", profile=other_profile, clients=[("c2", self.IP, "0", "tv1", 1)])

        self.assertEqual(self._stop(), ["skipped"])

    def test_window_limits_what_counts_as_skipped(self, mock_stop):
        self._channel("inside", clients=[("c1", self.IP, "0", "tv1", 9)])
        self._channel("outside", clients=[("c2", self.IP, "0", "tv1", 11)])

        self.assertEqual(self._stop(), ["inside"])


class NotUsedLoggingTests(SimpleTestCase):
    def setUp(self):
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

    def test_normalize_device_id(self):
        self.assertEqual(probation.normalize_device_id("tv-1_A"), "tv-1_A")
        for bad in (None, "", "has space", "a/b", "x" * 65, probation.DEVICE_ID_PLACEHOLDER + "!"):
            self.assertIsNone(probation.normalize_device_id(bad))
        self.assertEqual(len(probation.new_device_id()), 12)
        self.assertNotEqual(probation.new_device_id(), probation.new_device_id())

    def test_record_client_device(self):
        redis = FakeRedis()
        probation.record_client_device(redis, "ch-1", "c1", "tv1")
        probation.record_client_device(redis, "ch-1", "c2", None)

        self.assertEqual(
            redis.hget(RedisKeys.client_metadata("ch-1", "c1"), probation.DEVICE_ID_FIELD), "tv1"
        )
        self.assertIsNone(redis.hgetall(RedisKeys.client_metadata("ch-1", "c2")).get("device_id"))


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
    def test_joining_client_records_device_id(self, mock_proxy_cls, _network, mock_get_object, *_mocks):
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
