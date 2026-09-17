"""A provider closing a working connection must not count towards giving up on a channel.

Providers rotate connections as normal behaviour. Counting each rotation as a failure meant
three of them inside the retry window abandoned a channel that was playing perfectly, so the
longer someone watched the likelier the stream was to die (thousands of "Server closed
connection" lines, hundreds of "Maximum retry attempts (3) reached" on a real setup).
"""

import time
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.proxy.live_proxy import recovery
from apps.proxy.live_proxy.input.manager import StreamManager
from apps.proxy.live_proxy.redis_keys import RedisKeys

from .test_probation import FakeRedis


def _manager(stable_threshold=30, window=1800, max_retries=3):
    """A StreamManager with only the failure accounting set up."""
    manager = StreamManager.__new__(StreamManager)
    manager.channel_id = "channel-1"
    manager.retry_count = 0
    manager._last_failure_time = None
    manager._retry_window_seconds = window
    manager._stable_connection_threshold = stable_threshold
    manager.max_retries = max_retries
    manager.connection_start_time = 0
    manager.last_data_time = 0
    manager.buffer = MagicMock(redis_client=FakeRedis())
    return manager


class ReconnectBudgetTests(TestCase):
    """With Stream Recovery on; off, none of this happens and the count just goes up."""

    def setUp(self):
        from django.core.cache import cache

        cache.delete(recovery.SETTINGS_CACHE_KEY)
        self.addCleanup(cache.delete, recovery.SETTINGS_CACHE_KEY)
        recovery.save_settings({**recovery.DEFAULTS, "enabled": True, "scope": "all"})
        cache.delete(recovery.SETTINGS_CACHE_KEY)
    def test_a_rotation_after_stable_playback_never_adds_up(self):
        manager = _manager()
        now = time.time()

        # Three rotations of a channel that played for ten minutes each time
        for rotation in range(3):
            manager.connection_start_time = now + rotation * 600
            manager.last_data_time = manager.connection_start_time + 600
            self.assertEqual(manager._record_connection_failure(), 1)

        # Before this, the third one reached max_retries and the channel was given up
        self.assertLess(manager.retry_count, manager.max_retries)

    def test_a_stream_that_never_works_still_runs_out_of_retries(self):
        manager = _manager()
        now = time.time()
        manager.connection_start_time = now
        # The last data came from an earlier connection: this one sent nothing
        manager.last_data_time = now - 5

        self.assertEqual(manager._record_connection_failure(), 1)
        self.assertEqual(manager._record_connection_failure(), 2)
        self.assertEqual(manager._record_connection_failure(), 3)

    def test_a_connection_that_barely_worked_is_not_called_stable(self):
        manager = _manager(stable_threshold=30)
        now = time.time()
        manager.connection_start_time = now
        manager.last_data_time = now + 5  # five seconds of data, then gone

        self.assertEqual(manager._record_connection_failure(), 1)
        manager.connection_start_time = now + 5
        manager.last_data_time = now + 10
        self.assertEqual(manager._record_connection_failure(), 2)

    def test_nothing_changes_while_stream_recovery_is_off(self):
        from django.core.cache import cache

        recovery.save_settings({**recovery.DEFAULTS, "enabled": False})
        cache.delete(recovery.SETTINGS_CACHE_KEY)

        manager = _manager()
        now = time.time()
        for rotation in range(3):
            manager.connection_start_time = now + rotation * 600
            manager.last_data_time = manager.connection_start_time + 600
            count = manager._record_connection_failure()
        # Exactly Dispatcharr's own behaviour: three rotations and the stream is given up
        self.assertEqual(count, manager.max_retries)

    def test_the_long_quiet_window_still_clears_the_count(self):
        manager = _manager(window=60)
        manager.connection_start_time = time.time()
        manager.last_data_time = 0

        self.assertEqual(manager._record_connection_failure(), 1)
        manager._last_failure_time = time.time() - 120  # two minutes ago
        self.assertEqual(manager._record_connection_failure(), 1)

    def test_it_says_so_in_the_log_when_it_forgives_a_rotation(self):
        manager = _manager()
        now = time.time()
        manager.connection_start_time = now
        manager.last_data_time = now - 1
        manager._record_connection_failure()  # a real failure first

        manager.connection_start_time = now
        manager.last_data_time = now + 600
        with patch("apps.proxy.live_proxy.input.manager.logger") as log:
            manager._record_connection_failure()
        self.assertIn("had been working", log.info.call_args.args[0])
