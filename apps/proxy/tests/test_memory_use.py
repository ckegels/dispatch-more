"""Diagnostics → Memory: every Dispatcharr process and the memory it holds."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.proxy.live_proxy import memory


class MemoryUseTests(TestCase):
    def test_kinds_from_the_command_line(self):
        self.assertEqual(memory._kind(["celery", "-A", "dispatcharr", "beat"], "celery"),
                         "Celery beat (the scheduler)")
        self.assertEqual(memory._kind(["celery", "-A", "dispatcharr", "worker", "-Q", "dvr"], "celery"),
                         "Celery DVR worker (recordings)")
        self.assertEqual(memory._kind(["uwsgi", "--ini", "/app/docker/uwsgi.ini"], "uwsgi"), "Web worker (uWSGI)")
        self.assertIsNone(memory._kind(["bash"], "bash"))

    def test_the_page_reads_it_for_admins(self):
        admin = get_user_model().objects.create_user(username="admin", password="x", user_level=10)
        client = APIClient()
        client.force_authenticate(admin)
        with mock.patch.object(memory, "_kind", return_value="Web worker (uWSGI)"):
            found = client.get("/proxy/diagnostics/memory/").json()
        self.assertGreater(found["total_mb"], 0)
        # Every process here is called a web worker, and one whose parent is not uWSGI its master
        self.assertLessEqual({k["kind"] for k in found["kinds"]}, {"Web worker (uWSGI)", "uWSGI master"})
        self.assertIn("percent", found["system"])
