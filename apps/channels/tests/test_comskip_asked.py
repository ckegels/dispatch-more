"""A recording that asks for Comskip gets it, even with the server-wide switch off (Dispatch More)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import Channel, Recording
from apps.channels.tasks import comskip_asked_for


class ComskipAskedTests(TestCase):
    def recording(self, props):
        channel = Channel.objects.create(name="C", channel_number=1)
        now = timezone.now()
        return Recording.objects.create(channel=channel, start_time=now, end_time=now + timedelta(hours=1),
                                        custom_properties=props)

    def test_arrtv_asks_with_true_or_the_word(self):
        self.assertTrue(comskip_asked_for(self.recording({"comskip": True}).id))
        self.assertTrue(comskip_asked_for(self.recording({"comskip": "True"}).id))

    def test_not_asked(self):
        self.assertFalse(comskip_asked_for(self.recording({}).id))
        self.assertFalse(comskip_asked_for(self.recording({"comskip": False}).id))
        self.assertFalse(comskip_asked_for(self.recording({"comskip": {"status": "completed"}}).id))
        self.assertFalse(comskip_asked_for(999999))


class CommercialBreaksCapabilityTests(TestCase):
    def test_said_to_the_app(self):
        from unittest import mock

        from apps.proxy.live_proxy import app_devices
        from core.models import CoreSettings

        with mock.patch("shutil.which", return_value="/usr/bin/comskip"), \
                mock.patch.object(CoreSettings, "get_dvr_comskip_enabled", return_value=True), \
                mock.patch.object(CoreSettings, "get_dvr_comskip_mode", return_value="mark"):
            self.assertEqual(app_devices.commercial_breaks(),
                             {"installed": True, "enabled": True, "mode": "mark", "marks": True})
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(app_devices.commercial_breaks()["marks"], "no comskip here")
        self.assertIn("commercial_breaks", app_devices.capabilities())


class MarkedBreaksTests(TestCase):
    """In "mark" mode the breaks are kept on the recording for a player to skip."""

    def test_the_breaks_are_on_the_recording(self):
        import os
        import subprocess
        import tempfile
        from unittest import mock

        from apps.channels import tasks
        from core.models import CoreSettings

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        video = os.path.join(folder.name, "show.mkv")
        open(video, "wb").close()
        channel = Channel.objects.create(name="C", channel_number=1)
        now = timezone.now()
        rec = Recording.objects.create(channel=channel, start_time=now, end_time=now + timedelta(hours=1),
                                       custom_properties={"file_path": video})

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ffprobe":
                return subprocess.CompletedProcess(cmd, 0, stdout="1800.0\n", stderr="")
            with open(os.path.join(folder.name, "show.edl"), "w") as edl:
                edl.write("300.04\t480.52\t0\n1200.0\t1200.4\t0\n1500.0\t1680.0\t0\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/comskip"), \
                mock.patch("subprocess.run", side_effect=fake_run), \
                mock.patch.object(CoreSettings, "get_dvr_comskip_mode", return_value="mark"), \
                mock.patch.object(tasks, "send_websocket_update", create=True):
            tasks.comskip_process_recording(rec.id)
        rec.refresh_from_db()
        comskip = rec.custom_properties["comskip"]
        self.assertEqual(comskip["mode"], "mark")
        self.assertEqual(comskip["breaks"], [[300.0, 480.5], [1500.0, 1680.0]], "a blip under a second is not a break")
        self.assertTrue(os.path.exists(video), "marked, never cut")
