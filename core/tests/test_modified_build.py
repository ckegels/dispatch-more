"""The modified build's page: what it is, and leaving the request to go back to stock."""

import json
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User


class ModifiedBuildTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=User.objects.create_user(username="admin", password="x", user_level=10))
        self.folder = Path(tempfile.mkdtemp())
        patcher = override_settings(BASE_DIR=self.folder)
        patcher.enable()
        self.addCleanup(patcher.disable)

    def _installed(self, layout="systemd", via=""):
        request = self.folder / "requests" / "uninstall"
        (self.folder / ".fork-install.json").write_text(json.dumps({
            "release": "v99", "for_dispatcharr": "0.31.0", "layout": layout, "via": via,
            "installed_at": "2026-09-19T10:00:00+00:00", "uninstall_request": str(request),
        }))
        return request

    def test_says_what_it_is(self):
        self._installed()
        data = self.api.get("/api/core/modified-build/").json()
        self.assertTrue(data["installed"])
        self.assertTrue(data["build"].startswith("Dispatch More"))
        self.assertEqual(data["record"]["release"], "v99")
        self.assertFalse(data["uninstall_requested"])

    def test_uninstalling_leaves_the_request_where_the_installer_looks(self):
        request = self._installed()
        answer = self.api.post("/api/core/modified-build/uninstall/").json()
        self.assertTrue(answer["requested"])
        self.assertTrue(request.exists())
        self.assertTrue(self.api.get("/api/core/modified-build/").json()["uninstall_requested"])

    def test_uninstalling_takes_the_builds_schedule_out_of_celery_beat(self):
        """Stock Dispatcharr would otherwise be sent a task it does not have, every 5 minutes."""
        from django_celery_beat.models import IntervalSchedule, PeriodicTask

        every = IntervalSchedule.objects.create(every=300, period=IntervalSchedule.SECONDS)
        PeriodicTask.objects.create(name="stream-check-tick", task="apps.channels.tasks.stream_check_tick", interval=every)
        PeriodicTask.objects.create(name="scan-files", task="core.tasks.scan_and_process_files", interval=every)
        self._installed()
        self.api.post("/api/core/modified-build/uninstall/")
        names = set(PeriodicTask.objects.values_list("name", flat=True))
        self.assertNotIn("stream-check-tick", names)
        self.assertIn("scan-files", names)

    def test_in_docker_it_says_to_restart_the_container(self):
        self._installed(layout="docker", via="entrypoint")
        answer = self.api.post("/api/core/modified-build/uninstall/").json()
        self.assertIn("Restart the container", answer["how"])

    def test_installed_by_hand_in_docker_it_says_to_recreate_it(self):
        self._installed(layout="docker", via="exec")
        answer = self.api.post("/api/core/modified-build/uninstall/").json()
        self.assertIn("force-recreate", answer["how"])

    def test_not_installed_by_the_installer_cannot_be_uninstalled_from_here(self):
        response = self.api.post("/api/core/modified-build/uninstall/")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.api.get("/api/core/modified-build/").json()["installed"])

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(user=User.objects.create_user(username="v", password="x", user_level=0))
        self.assertEqual(viewer.post("/api/core/modified-build/uninstall/").status_code, 403)
