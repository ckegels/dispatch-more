"""Stream Check writes what it reads about a stream onto it, as Dispatcharr's Stats page shows it."""

import json
import subprocess
from unittest import mock

from django.test import TestCase

from apps.channels import stream_check
from apps.channels.models import Stream


FFPROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
         "avg_frame_rate": "50/1", "r_frame_rate": "50/1", "pix_fmt": "yuv420p"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2,
         "channel_layout": "stereo"},
    ],
    "format": {"format_name": "mpegts"},
}


class StreamInfoTests(TestCase):
    def test_what_ffprobe_reads_in_dispatcharrs_shapes(self):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(FFPROBE).encode(), stderr=b"")
        with mock.patch("subprocess.run", return_value=done):
            found = stream_check._ffprobe(b"x")
        self.assertEqual(found["resolution"], "1280x720")
        self.assertEqual(found["details"], {
            "video_codec": "h264", "resolution": "1280x720", "source_fps": 50.0,
            "pixel_format": "yuv420p", "audio_codec": "aac", "sample_rate": 48000,
            "audio_channels": "stereo", "stream_type": "mpegts",
        })

    def test_channels_without_a_layout_and_an_unknown_rate(self):
        details = stream_check.stream_details(
            {"codec_name": "hevc", "width": 3840, "height": 2160, "avg_frame_rate": "0/0",
             "r_frame_rate": "25/1"},
            {"codec_name": "ac3", "channels": 6}, "")
        self.assertEqual(details["source_fps"], 25.0)
        self.assertEqual(details["audio_channels"], "5.1")
        self.assertNotIn("stream_type", details)

    def test_saved_on_the_stream_keeping_what_ffmpeg_measured(self):
        stream = Stream.objects.create(name="S", url="http://example.invalid/1",
                                       stream_stats={"ffmpeg_output_bitrate": 4200.5})
        self.assertTrue(stream_check.save_stream_info(stream.id, {"resolution": "1280x720", "video_codec": "h264"}))
        stream.refresh_from_db()
        self.assertEqual(stream.stream_stats, {"ffmpeg_output_bitrate": 4200.5, "resolution": "1280x720",
                                               "video_codec": "h264"})
        self.assertIsNotNone(stream.stream_stats_updated_at)

    def test_the_lineup_quality_now_measured(self):
        from apps.channels.channel_manager import quality_of

        self.assertEqual(quality_of("┃BE┃ VRT 1 4K", {"resolution": "1280x720"}), ("HD", 2, True))


class StatsCardTests(TestCase):
    """A live card without live measurements (the Proxy profile) shows what was saved."""

    def test_filled_from_the_saved_stream_info(self):
        from apps.proxy.live_proxy.channel_status import ChannelStatus

        stream = Stream.objects.create(name="S", url="http://example.invalid/2",
                                       stream_stats={"resolution": "1920x1080", "video_codec": "h264",
                                                     "audio_channels": "stereo"})
        info = {"stream_id": stream.id}
        ChannelStatus._fill_from_saved_stream_info(info)
        self.assertEqual((info["resolution"], info["video_codec"], info["audio_channels"]),
                         ("1920x1080", "h264", "stereo"))
        self.assertEqual(info["stream_info_from"], "saved")

    def test_live_measurements_win(self):
        from apps.proxy.live_proxy.channel_status import ChannelStatus

        stream = Stream.objects.create(name="S", url="http://example.invalid/3",
                                       stream_stats={"resolution": "1920x1080"})
        info = {"stream_id": stream.id, "resolution": "1280x720"}
        ChannelStatus._fill_from_saved_stream_info(info)
        self.assertEqual(info["resolution"], "1280x720")
