"""The Diagnostics page's data (live_proxy.diagnostics_views).

The page is where someone goes when something is wrong, so what matters most is that it
opens: a record half written, or one part that cannot be read, leaves that part out rather
than answering 500 for all of it.
"""

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import diagnostics_views


class DiagnosticsPageTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=User.objects.create_user(username="admin", password="x", user_level=10))
        # An empty Redis: every list, hash and key pattern comes back with nothing
        self.redis = mock.MagicMock()
        self.redis.zrevrange.return_value = []
        self.redis.lrange.return_value = []
        self.redis.hgetall.return_value = {}
        self.redis.scan_iter.return_value = iter([])
        self.redis.get.return_value = None
        patcher = mock.patch.object(diagnostics_views.RedisClient, "get_client", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self):
        return self.api.get("/proxy/diagnostics/")

    def test_it_opens_on_an_empty_installation(self):
        response = self._get()
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(response.json()["starts"], [])

    def test_a_channel_start_half_written_is_read_as_far_as_it_goes(self):
        records = [
            {"time": "1758230000.5", "channel": "ORF 1", "total": "", "server_buffering": "x"},
            {"channel": "ORF 2"},
        ]
        with mock.patch.object(diagnostics_views.timing, "recent_starts", return_value=records):
            response = self._get()
        self.assertEqual(response.status_code, 200)
        starts = response.json()["starts"]
        self.assertEqual([s["channel"] for s in starts], ["ORF 1", "ORF 2"])
        self.assertEqual(starts[0]["total"], 0.0)

    def test_one_part_that_cannot_be_read_leaves_the_rest(self):
        with mock.patch.object(diagnostics_views.health, "running_now", side_effect=RuntimeError("bad")):
            response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["running"], [])

    def test_a_viewer_kept_by_number_is_named(self):
        self.assertEqual(diagnostics_views._usernames([7, "x", None, "abc"]), {})
