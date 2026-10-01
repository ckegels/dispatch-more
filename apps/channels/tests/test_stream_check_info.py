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
        details = dict(found["details"])
        self.assertIsNotNone(details.pop("subtitles_checked_at"))
        self.assertEqual(details, {
            "video_codec": "h264", "resolution": "1280x720", "source_fps": 50.0,
            "pixel_format": "yuv420p", "audio_codec": "aac", "sample_rate": 48000,
            "audio_channels": "stereo", "stream_type": "mpegts",
            "subtitles": [], "audio_languages": [],
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


class SubtitleInfoTests(TestCase):
    """Which subtitles a stream carries, read in the same check (fork/subtitles.md §2)."""

    def probe(self, streams, data=b"x"):
        answer = {"streams": streams, "format": {"format_name": "mpegts"}}
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(answer).encode(), stderr=b"")
        with mock.patch("subprocess.run", return_value=done):
            return stream_check._ffprobe(data)["details"]

    def test_teletext_dvb_and_the_audio_languages(self):
        details = self.probe([
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "dut"}},
            {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
            {"codec_type": "subtitle", "codec_name": "dvb_teletext", "tags": {"language": "dut,dut"}},
            {"codec_type": "subtitle", "codec_name": "dvb_subtitle", "tags": {"language": "eng"},
             "disposition": {"hearing_impaired": 1}},
        ])
        self.assertEqual(details["subtitles"], [
            {"kind": "teletext", "lang": "dut", "hearing_impaired": False},
            {"kind": "dvb", "lang": "eng", "hearing_impaired": True},
        ])
        self.assertEqual(details["audio_languages"], ["dut", "eng"])
        self.assertIn("subtitles_checked_at", details)

    def test_captions_in_the_picture_found_by_their_marker(self):
        video = [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}]
        marker = b"\x00\x00GA94\x03\xc1\xff"
        self.assertEqual(self.probe(video, b"..." + marker * 5)["subtitles"],
                         [{"kind": "cc", "lang": "", "hearing_impaired": False}])
        self.assertEqual(self.probe(video, b"..." + marker)["subtitles"], [], "one chance match is not captions")

    def test_none_found_is_an_empty_list_and_und_is_no_language(self):
        details = self.probe([
            {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
            {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "und"}},
        ])
        self.assertEqual(details["subtitles"], [])
        self.assertEqual(details["audio_languages"], [])

    def test_kept_on_the_stream(self):
        stream = Stream.objects.create(name="S", url="http://example.invalid/9")
        stream_check.save_stream_info(stream.id, {"subtitles": [], "audio_languages": ["deu"]})
        stream.refresh_from_db()
        self.assertEqual(stream.stream_stats["subtitles"], [], "looked and found none: kept")
