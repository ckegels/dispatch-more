"""A recording's health, checked when it ends (apps.channels.recording_health)."""

from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.channels import recording_health as health

VIDEO_AUDIO = [{"index": 0, "codec_type": "video"}, {"index": 1, "codec_type": "audio"}]


def packets(seconds, video_step=0.04, audio_step=0.023, audio_holes=(), video_holes=()):
    """Picture and sound packets for a recording of `seconds`, with holes ((at, length), ...)."""
    out = []
    for index, step, holes in ((0, video_step, video_holes), (1, audio_step, audio_holes)):
        t = 0.0
        while t < seconds:
            hole = next((h for h in holes if h[0] <= t < h[0] + h[1]), None)
            if hole:
                t = hole[0] + hole[1]
                continue
            out.append((index, t))
            t += step
    out.sort(key=lambda p: p[1])
    return out


class AnalyseTests(SimpleTestCase):
    def test_a_recording_that_runs_through_is_healthy(self):
        result = health.analyse(VIDEO_AUDIO, packets(600), 600.0, 600.0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["audio"]["gaps"], 0)

    def test_the_saturday_night_live_recording_is_damaged_and_says_where(self):
        """Sound missing for long stretches from 15 minutes in (a stalling failover stream)."""
        holes = [(928 + i * 20, 11) for i in range(60)]
        result = health.analyse(VIDEO_AUDIO, packets(2634, audio_holes=holes), 2634.4, 2616.0)
        self.assertEqual(result["status"], "damaged")
        self.assertEqual(result["audio"]["gaps"], 60)
        self.assertAlmostEqual(result["audio"]["first_gap_at_secs"], 928, delta=0.1)
        self.assertTrue(result["problems"][0].startswith("Sound missing for 11 min"))
        self.assertIn("60 holes, the first at 15:2", result["problems"][0])

    def test_a_few_seconds_in_a_long_match_are_minor(self):
        """The rugby: one 7 s hole near the end of 2:20."""
        holes = [(8259, 7)]
        result = health.analyse(VIDEO_AUDIO, packets(8419, audio_holes=holes, video_holes=holes), 8419.0, 8400.0)
        self.assertEqual(result["status"], "minor")
        self.assertIn("Picture missing for 7 s", result["problems"][0])

    def test_a_reconnect_of_a_second_or_two_is_no_damage(self):
        result = health.analyse(VIDEO_AUDIO, packets(600, audio_holes=[(300, 2)], video_holes=[(300, 2)]), 600.0, 600.0)
        self.assertEqual(result["status"], "ok")
        # Still said, so it can be found
        self.assertEqual(len(result["problems"]), 2)

    def test_no_picture_fails_and_no_sound_is_damage(self):
        self.assertEqual(health.analyse([{"index": 0, "codec_type": "audio"}], [(0, 0.0)], 600.0, 600.0)["status"], "failed")
        result = health.analyse([{"index": 0, "codec_type": "video"}], [(0, t / 10) for t in range(6000)], 600.0, 600.0)
        self.assertEqual(result["status"], "damaged")
        self.assertIn("No sound in the file", result["problems"])

    def test_too_short_is_damaged_and_not_half_is_failed(self):
        self.assertEqual(health.analyse(VIDEO_AUDIO, packets(500), 500.0, 600.0)["status"], "damaged")
        self.assertEqual(health.analyse(VIDEO_AUDIO, packets(200), 200.0, 600.0)["status"], "failed")
        # A minute short of two hours is within what a recording's edges take
        self.assertEqual(health.analyse(VIDEO_AUDIO, packets(30), 7140.0, 7200.0)["status"], "ok")

    def test_timestamps_going_back_are_damage(self):
        p = packets(100) + [(0, 50.0)]
        result = health.analyse(VIDEO_AUDIO, p, 100.0, 100.0)
        self.assertEqual(result["status"], "damaged")
        self.assertIn("Picture timestamps jump back 1 time", result["problems"][0])

    def test_clock(self):
        self.assertEqual(health.clock(928), "15:28")
        self.assertEqual(health.clock(3723), "1:02:03")


class CheckRecordingTests(TestCase):
    def setUp(self):
        from apps.channels.models import Channel, Recording

        self.channel = Channel.objects.create(name="CANAL+", channel_number=1)
        now = timezone.now()
        self.recording = Recording.objects.create(
            channel=self.channel, start_time=now - timedelta(minutes=44), end_time=now,
            custom_properties={"status": "completed", "file_path": "/data/recordings/x.mkv"},
        )

    @patch("apps.channels.recording_health.check_file")
    @patch("os.path.exists", return_value=True)
    def test_the_answer_is_kept_with_the_recording(self, _exists, check_file):
        check_file.return_value = {"status": "damaged", "problems": ["Sound missing for 21 min"]}
        with patch("core.utils.send_websocket_update"):
            health.check_recording(self.recording.id)
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.custom_properties["health"]["status"], "damaged")
        self.assertEqual(self.recording.custom_properties["status"], "completed")
        # The scheduled length goes with it
        self.assertAlmostEqual(check_file.call_args[0][1], 44 * 60, delta=1)

    def test_a_recording_without_its_file_failed(self):
        with patch("core.utils.send_websocket_update"):
            result = health.check_recording(self.recording.id)
        self.assertEqual(result["status"], "failed")

    def test_unreadable_file_failed(self):
        with patch("apps.channels.recording_health._probe_streams", side_effect=RuntimeError("bad")):
            self.assertEqual(health.check_file("/x.mkv", 60)["status"], "failed")

    def test_check_now_from_the_dvr_page_is_for_a_dvr_manager(self):
        from apps.accounts.models import User

        admin = User.objects.create_user(username="admin", password="x", user_level=10)
        client = APIClient()
        client.force_authenticate(user=admin)
        with patch("apps.channels.tasks.recording_health_check.delay") as queued:
            answer = client.post(f"/api/channels/recordings/{self.recording.id}/health/")
        self.assertEqual(answer.status_code, 200)
        queued.assert_called_once_with(self.recording.id)
        viewer = User.objects.create_user(username="tv", password="x", user_level=0)
        client.force_authenticate(user=viewer)
        self.assertIn(client.post(f"/api/channels/recordings/{self.recording.id}/health/").status_code, (401, 403))

    def test_off_by_default(self):
        from core.models import CoreSettings

        self.assertFalse(CoreSettings.get_dvr_health_check_enabled())
