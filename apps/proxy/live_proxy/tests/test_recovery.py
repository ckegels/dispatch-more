"""Stream Recovery: forgiving the close of a connection that had been working."""

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import recovery
from apps.proxy.live_proxy.redis_keys import RedisKeys

from .test_probation import FakeRedis


def _forget_settings():
    from django.core.cache import cache

    cache.delete(recovery.SETTINGS_CACHE_KEY)


class SettingsTests(TestCase):
    def setUp(self):
        _forget_settings()
        self.addCleanup(_forget_settings)

    def test_it_is_on_for_media_servers_and_nothing_else(self):
        self.assertEqual(recovery.settings(), recovery.DEFAULTS)
        # A media server holds a channel open for hours: that is the case this is for
        self.assertTrue(recovery.settings()["enabled"])
        # Everything else behaves as it always did until the scope is widened
        self.assertEqual(recovery.settings()["scope"], "media_servers")

    def test_what_is_saved_is_what_is_read_back(self):
        recovery.save_settings({**recovery.DEFAULTS, "enabled": True, "stable_seconds": 90})
        _forget_settings()
        self.assertTrue(recovery.settings()["enabled"])
        self.assertEqual(recovery.settings()["stable_seconds"], 90)

    def test_a_broken_settings_read_leaves_dispatcharr_as_it_was(self):
        with patch("core.models.CoreSettings.objects") as objects:
            objects.filter.side_effect = RuntimeError("no database")
            self.assertEqual(recovery.settings(), recovery.DEFAULTS)


class ForgivenessTests(TestCase):
    """Whether a close is forgiven, and why not when it is not."""

    CHANNEL = "channel-1"

    def setUp(self):
        _forget_settings()
        self.addCleanup(_forget_settings)
        self.redis = FakeRedis()

    def _on(self, **changes):
        recovery.save_settings({**recovery.DEFAULTS, "enabled": True, **changes})
        _forget_settings()

    def _media_server_is_watching(self):
        self.redis.sadd(RedisKeys.clients(self.CHANNEL), "c1")
        self.redis.hset(
            RedisKeys.client_metadata(self.CHANNEL, "c1"),
            mapping={"ip_address": "192.168.2.141", "user_agent": "Lavf/61.7.100"},
        )

    def _someone_else_is_watching(self):
        self.redis.sadd(RedisKeys.clients(self.CHANNEL), "c1")
        self.redis.hset(
            RedisKeys.client_metadata(self.CHANNEL, "c1"),
            mapping={"ip_address": "192.168.2.50", "user_agent": "TiviMate/5.1.6"},
        )

    def test_nothing_happens_while_it_is_off(self):
        recovery.save_settings({**recovery.DEFAULTS, "enabled": False})
        _forget_settings()
        self._media_server_is_watching()

        self.assertFalse(recovery.forgive_disconnect(self.redis, self.CHANNEL, 600))
        self.assertEqual(recovery.recent_events(self.redis), [])

    def test_a_connection_that_had_been_working_is_forgiven(self):
        self._on()
        self._media_server_is_watching()

        self.assertTrue(recovery.forgive_disconnect(self.redis, self.CHANNEL, 600))

        (event,) = recovery.recent_events(self.redis)
        self.assertEqual(event["action"], "kept alive")
        self.assertIn("600s", event["detail"])

    def test_a_connection_that_barely_worked_is_not(self):
        self._on(stable_seconds=30)
        self._media_server_is_watching()
        self.assertFalse(recovery.forgive_disconnect(self.redis, self.CHANNEL, 5))

    def test_only_the_channels_a_media_server_is_watching_by_default(self):
        self._on()
        self._someone_else_is_watching()
        self.assertFalse(recovery.forgive_disconnect(self.redis, self.CHANNEL, 600))

        # Widened to everything, the same channel is forgiven
        self._on(scope="all")
        self.assertTrue(recovery.forgive_disconnect(self.redis, self.CHANNEL, 600))

    def test_a_channel_that_drops_too_often_is_failing_not_rotating(self):
        self._on(max_per_hour=3)
        self._media_server_is_watching()

        for _ in range(3):
            self.assertTrue(recovery.forgive_disconnect(self.redis, self.CHANNEL, 600))
        self.assertFalse(recovery.forgive_disconnect(self.redis, self.CHANNEL, 600))

        self.assertEqual(recovery.recent_events(self.redis)[0]["action"], "gave up")

    def test_a_broken_redis_changes_nothing(self):
        self._on(scope="all")

        class Broken:
            def __getattr__(self, _name):
                def explode(*_args, **_kwargs):
                    raise RuntimeError("redis is down")

                return explode

        self.assertFalse(recovery.forgive_disconnect(Broken(), self.CHANNEL, 600))


class SettingsViewTests(TestCase):
    def setUp(self):
        _forget_settings()
        self.addCleanup(_forget_settings)
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin4", password="x", user_level=10)
        )

    def test_reading_and_changing_the_settings(self):
        self.assertTrue(
            self.client_api.get("/proxy/stream-recovery/").json()["settings"]["enabled"]
        )

        response = self.client_api.post(
            "/proxy/stream-recovery/",
            {"enabled": True, "stable_seconds": 120, "scope": "all", "max_per_hour": 20},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["settings"],
            {"enabled": True, "stable_seconds": 120, "scope": "all", "max_per_hour": 20},
        )

    def test_values_that_would_make_no_sense_are_refused(self):
        for body, expected in (
            ({"stable_seconds": 2}, "between 5 and 600"),
            ({"max_per_hour": 500}, "between 1 and 120"),
            ({"scope": "sometimes"}, "which channels"),
        ):
            response = self.client_api.post("/proxy/stream-recovery/", body, format="json")
            self.assertEqual(response.status_code, 400, body)
            self.assertIn(expected, response.json()["error"])

    def test_only_admins(self):
        viewer = APIClient()
        viewer.force_authenticate(
            user=User.objects.create_user(username="viewer4", password="x", user_level=0)
        )
        self.assertEqual(viewer.get("/proxy/stream-recovery/").status_code, 403)
        self.assertEqual(
            viewer.post("/proxy/stream-recovery/", {}, format="json").status_code, 403
        )


class FailoverEventsTests(TestCase):
    """The other half of the story: what happened when a stream could not be kept."""

    def setUp(self):
        self.redis = FakeRedis()

    def test_giving_up_on_a_stream_switching_and_running_out_are_all_reported(self):
        recovery.record_event(self.redis, "channel-1", "stream given up", "3 failed connections")
        recovery.record_event(self.redis, "channel-1", "stream switched", "now on stream 42")
        recovery.record_event(self.redis, "channel-1", "nothing left", "none of them worked")

        actions = [event["action"] for event in recovery.recent_events(self.redis)]
        # Newest first, so the page reads as the story ran
        self.assertEqual(actions, ["nothing left", "stream switched", "stream given up"])

    def test_only_the_last_events_are_kept(self):
        for number in range(recovery.EVENTS_KEPT + 10):
            recovery.record_event(self.redis, f"channel-{number}", "stream switched")

        self.assertEqual(len(recovery.recent_events(self.redis)), recovery.EVENTS_KEPT)

    def test_a_channel_name_that_cannot_be_looked_up_does_not_lose_the_event(self):
        with patch(
            "apps.proxy.live_proxy.utils.resolve_channel_display_name",
            side_effect=RuntimeError("no database"),
        ):
            recovery.record_event(self.redis, "channel-1", "stream switched")

        # What happened to the channel is worth more than what it is called
        (event,) = recovery.recent_events(self.redis)
        self.assertEqual(event["channel"], "channel-1")
        self.assertEqual(event["action"], "stream switched")
