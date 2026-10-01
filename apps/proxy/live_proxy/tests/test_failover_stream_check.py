"""Failover while Stream Check runs (failover_makes_way): a check holding the only free
connection lets go, and the provider being checked is tried last."""
from unittest.mock import patch

from django.test import TestCase

from apps.channels import stream_check
from apps.channels.tests.test_stream_check import FakeRedis, _Setup
from apps.proxy.live_proxy.tests.test_failover_retry_window import _make_manager


class FailoverWaitsForACheckTests(TestCase):
    def test_a_check_letting_go_gives_failover_its_stream(self):
        sm = _make_manager()
        tries = iter([False, True])
        with patch.object(sm, "_try_next_stream", side_effect=lambda: next(tries)), \
             patch.object(sm, "_wait_for_stream_check", return_value=True):
            self.assertTrue(sm._try_next_stream_with_cooldown())

    def test_what_the_failed_try_armed_is_undone(self):
        sm = _make_manager()

        def arms_then_fails():
            sm._failover_rotation_passes, sm._rotation_cooldown_until = 1, 9e12
            return False

        tries = iter([arms_then_fails, lambda: True])
        with patch.object(sm, "_try_next_stream", side_effect=lambda: next(tries)()), \
             patch.object(sm, "_wait_for_stream_check", return_value=True):
            self.assertTrue(sm._try_next_stream_with_cooldown())
        self.assertEqual((sm._failover_rotation_passes, sm._rotation_cooldown_until), (0, None))

    def test_no_check_running_changes_nothing(self):
        sm = _make_manager()
        with patch.object(sm, "_try_next_stream", return_value=False) as tried, \
             patch("apps.channels.stream_check.failover_may_wait", return_value=False):
            self.assertFalse(sm._try_next_stream_with_cooldown())
        self.assertEqual(tried.call_count, 1)

    def test_it_waits_until_a_stream_has_room(self):
        sm = _make_manager()
        answers = iter([[], [{"stream_id": 400, "profile_id": 1}]])
        with patch("apps.channels.stream_check.failover_may_wait", return_value=True), \
             patch("apps.proxy.live_proxy.input.manager.get_alternate_streams", side_effect=lambda *a: next(answers)), \
             patch.object(sm, "_sleep_interruptible", return_value=True):
            self.assertTrue(sm._wait_for_stream_check())

    def test_it_gives_up_after_its_time(self):
        sm = _make_manager()
        with patch("apps.channels.stream_check.failover_may_wait", return_value=True), \
             patch("apps.proxy.live_proxy.input.manager.get_alternate_streams", return_value=[]), \
             patch.object(sm, "_sleep_interruptible", return_value=True):
            self.assertFalse(sm._wait_for_stream_check(seconds=0.01))


class FailoverOrderTests(_Setup):
    def setUp(self):
        super().setUp()
        self.redis = FakeRedis()

    def test_the_provider_being_checked_is_tried_last(self):
        alternates = [{"stream_id": self.first.id}, {"stream_id": self.second.id}]
        self.assertEqual(stream_check.checked_last(self.redis, alternates), alternates)
        self.redis.set(stream_check.RUN_KEY, "1")
        self.redis.set(stream_check.CHECKING_KEY, f"[{self.a.id}]")
        self.assertEqual(stream_check.checked_last(self.redis, alternates),
                         [{"stream_id": self.second.id}, {"stream_id": self.first.id}])

    def test_failover_asks_the_check_to_let_go_only_when_on(self):
        self.assertFalse(stream_check.failover_may_wait(self.redis), "no check running")
        self.redis.set(stream_check.RUN_KEY, "1")
        self.assertTrue(stream_check.failover_may_wait(self.redis))
        self.assertTrue(self.redis.exists(stream_check.YIELD_KEY))
        self.redis.delete(stream_check.YIELD_KEY)
        stream_check.save_settings({"failover_makes_way": False})
        self.assertFalse(stream_check.failover_may_wait(self.redis))
        self.assertFalse(self.redis.exists(stream_check.YIELD_KEY))
