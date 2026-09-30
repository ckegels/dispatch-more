"""A channel another worker owns is not taken over (Dispatch More: redis-py's None for a taken
NX key was read as a Redis failure, and the worker assumed ownership)."""

from unittest import mock

from django.test import SimpleTestCase

from apps.proxy.live_proxy.server import ProxyServer


class OwnershipTests(SimpleTestCase):
    def server(self, set_answer, owner):
        server = ProxyServer.__new__(ProxyServer)
        server.worker_id = "worker-b"
        server.redis_client = mock.Mock()
        server.redis_client.set.return_value = set_answer
        server.redis_client.get.return_value = owner
        return server

    def test_owned_by_another_worker(self):
        self.assertFalse(self.server(None, "worker-a").try_acquire_ownership("chan"))

    def test_free(self):
        self.assertTrue(self.server(True, None).try_acquire_ownership("chan"))

    def test_already_ours(self):
        self.assertTrue(self.server(None, "worker-b").try_acquire_ownership("chan"))

    def test_redis_down_still_assumes_ownership(self):
        server = self.server(None, None)
        server.redis_client.set.side_effect = RuntimeError("down")
        self.assertTrue(server.try_acquire_ownership("chan"), "as stock: nothing else to go on")
