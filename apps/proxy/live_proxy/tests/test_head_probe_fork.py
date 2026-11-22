"""A HEAD probe on a live stream URL does nothing on Dispatch More's side either.

Dispatcharr 0.34 answers HEAD on /proxy/ts/stream/<uuid> with the container and returns
before a client is registered or a stream opened (#1731: Jellyfin probes this way before it
shares one connection between its viewers). Everything Dispatch More does for a viewer --
recognising it, Force Close, the surfing delay, one device per login, watch together, the
device's own stream, the timing of the start -- comes after that point, so a probe must not
close anybody's channel or count as a channel change.
"""

from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase

VIEWS = "apps.proxy.live_proxy.views"
FORK_STEPS = (
    "probation.viewer_from_request",
    "probation.skipped_while_surfing",
    "probation.leave_previous_channel",
    "probation.stop_skipped_channels",
    "probation.record_switch",
    "probation.record_client_viewer",
    "app_one_device.check",
    "app_links.mirror",
    "app_own_streams.own_stream_for",
    "app_devices.declared_previous_channel",
    "app_devices.mark_fast_start",
    "timing.start",
)


@patch(f"{VIEWS}.close_old_connections")
@patch(f"{VIEWS}.network_access_allowed", return_value=True)
@patch(f"{VIEWS}.ProxyServer")
class HeadDoesNothingOfTheForksTests(SimpleTestCase):
    def _probe(self, **headers):
        from apps.proxy.live_proxy.views import stream_ts

        request = RequestFactory().head("/proxy/ts/stream/channel-uuid", **headers)
        request.user = MagicMock(is_authenticated=False)
        channel = MagicMock()
        channel.get_stream_profile.return_value.is_redirect.return_value = False
        mocks = {}
        patchers = [patch(f"{VIEWS}.get_stream_object", return_value=channel),
                    patch(f"{VIEWS}.CoreSettings.get_default_output_format", return_value="mpegts")]
        for step in FORK_STEPS:
            module, name = step.split(".")
            patchers.append(patch(f"apps.proxy.live_proxy.{module}.{name}"))
        started = [p.start() for p in patchers]
        try:
            response = stream_ts(request, "channel-uuid")
        finally:
            for p in patchers:
                p.stop()
        for step, mock in zip(FORK_STEPS, started[2:]):
            mocks[step] = mock
        return response, mocks

    def test_an_arrtv_device_probing_closes_nothing(self, proxy_server, *_):
        response, mocks = self._probe(
            HTTP_USER_AGENT="ArrTV/0.5.9-arr.117 (Android; SHIELD Android TV)",
            HTTP_X_DISPATCH_DEVICE="b7be66fa-9927-483b-85c1-9a1f5e119ad0",
            HTTP_X_DISPATCH_PREVIOUS_CHANNEL="another-channel",
        )
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "video/mp2t"))
        called = [step for step, mock in mocks.items() if mock.called]
        self.assertEqual(called, [], "a HEAD probe ran a viewer step")
        proxy_server.get_instance.assert_not_called()

    def test_jellyfin_probing_before_it_shares_a_stream(self, proxy_server, *_):
        response, mocks = self._probe(HTTP_USER_AGENT="Jellyfin-Server/10.11.0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([step for step, mock in mocks.items() if mock.called], [])
