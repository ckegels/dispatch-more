import time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

CHANNEL_ID = "11111111-2222-3333-4444-555555555555"


def make_generator():
    from apps.proxy.live_proxy.output.ts.generator import StreamGenerator

    generator = StreamGenerator.__new__(StreamGenerator)
    generator.channel_id = CHANNEL_ID
    generator.channel_name = "Channel"
    generator.client_id = "client_1"
    generator.client_ip = "127.0.0.1"
    generator.client_user_agent = "test"
    generator.user = None
    generator.bytes_sent = 0
    generator.stream_start_time = time.time()
    return generator


def make_proxy_server(total_clients):
    client_manager = MagicMock()
    client_manager.clients = {}
    client_manager.get_client_count.return_value = 0
    client_manager.get_total_client_count.return_value = total_clients
    proxy_server = MagicMock()
    proxy_server.client_managers = {CHANNEL_ID: client_manager}
    return proxy_server


class LastClientReleasesStreamTests(SimpleTestCase):
    def cleanup(self, total_clients, shutdown_delay):
        generator = make_generator()
        module = "apps.proxy.live_proxy.output.ts.generator"
        with patch(f"{module}.ProxyServer.get_instance", return_value=make_proxy_server(total_clients)), \
                patch(f"{module}.ConfigHelper.channel_shutdown_delay", return_value=shutdown_delay), \
                patch(f"{module}.log_system_event"), \
                patch(f"{module}.logger") as logger, \
                patch("apps.proxy.live_proxy.url_utils.release_worker_stream", return_value=True) as release:
            generator._cleanup()
        return release, logger

    def test_last_client_releases_the_stream_without_a_shutdown_delay(self):
        release, logger = self.cleanup(total_clients=1, shutdown_delay=0)
        release.assert_called_once_with(CHANNEL_ID)
        logger.error.assert_not_called()

    def test_a_remaining_client_keeps_the_stream(self):
        release, _ = self.cleanup(total_clients=2, shutdown_delay=0)
        release.assert_not_called()

    def test_a_shutdown_delay_leaves_the_release_to_the_coordinated_stop(self):
        release, _ = self.cleanup(total_clients=1, shutdown_delay=5)
        release.assert_not_called()


class ChunksGoneTests(SimpleTestCase):
    """
    The buffer's head is ahead but none of its chunks exist any more (a channel being torn
    down). The jump to near the head was taken again and again with nothing to read, at once:
    33,090 warnings in one hour on 2026-11-22, a client looping for seconds on end.
    """

    def run_loop(self, rounds, oldest=None):
        from apps.proxy.live_proxy.output.ts import generator as module

        generator = make_generator()
        generator.local_index = 0
        generator.empty_reads = 0
        generator.consecutive_empty = 0
        generator.last_yield_time = time.time()
        generator.stream_manager = None
        generator.buffer = MagicMock()
        generator.buffer.index = 55
        generator.buffer.get_optimized_client_data.return_value = ([], None)
        generator.buffer.find_oldest_available_chunk.return_value = oldest
        checks = iter([True] * rounds + [False])
        sleeps = []
        with patch.object(module.StreamGenerator, "_check_resources", side_effect=lambda: next(checks)), \
                patch.object(module.StreamGenerator, "_is_timeout", return_value=False), \
                patch(f"{module.__name__}.ConfigHelper.initial_behind_chunks", return_value=4), \
                patch(f"{module.__name__}.gevent.sleep", side_effect=sleeps.append), \
                patch(f"{module.__name__}.logger") as logger:
            list(generator._stream_data_generator())
        return generator, sleeps, logger

    def test_one_jump_then_it_waits(self):
        generator, sleeps, logger = self.run_loop(rounds=6)
        self.assertEqual(generator.local_index, 51)
        # The first round jumps, every other one waits
        self.assertEqual(len(sleeps), 5)
        self.assertEqual(logger.warning.call_count, 1)

    def test_an_oldest_chunk_that_is_not_ahead_is_no_jump(self):
        generator, sleeps, _logger = self.run_loop(rounds=4, oldest=0)
        self.assertEqual(generator.local_index, 51)
        self.assertEqual(len(sleeps), 3)

    def test_a_chunk_further_on_is_still_jumped_to_at_once(self):
        generator, sleeps, logger = self.run_loop(rounds=1, oldest=30)
        self.assertEqual(generator.local_index, 30)
        self.assertEqual(sleeps, [])
