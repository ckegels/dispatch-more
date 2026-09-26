"""What a player app says about itself (apps.proxy.live_proxy.app_devices)."""

from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import app_devices, probation


class AppDevicesTests(TestCase):
    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        self.request = RequestFactory().get(
            "/proxy/ts/stream/abc",
            HTTP_X_DISPATCH_DEVICE="3f2a9c1e-0b7d-4e21-9a55-0f1c2d3e4f50",
            HTTP_X_DISPATCH_DEVICE_NAME="Living room SHIELD",
            HTTP_X_DISPATCH_MULTIVIEW="mv-7",
            HTTP_X_DISPATCH_PREVIOUS_CHANNEL="11111111-2222-3333-4444-555555555555",
            HTTP_USER_AGENT="arrTV/1.2 (Android; SHIELD Android TV)",
        )
        self.user = User.objects.create_user(username="admin", password="x", user_level=10)

    def test_off_means_nothing_is_read(self):
        """Off, as stock: the headers change nothing about who the viewer is."""
        self.assertEqual(app_devices.declared_device(self.request), "")
        self.assertEqual(app_devices.declared_previous_channel(self.request), "")
        viewer = probation.viewer_from_request(self.request, self.user, "192.168.65.3")
        self.assertIsNone(viewer.server_device)
        self.assertIsNone(viewer.multiview)

    def test_a_declared_device_is_the_viewer_with_its_login(self):
        app_devices.save_settings({"devices": True})
        viewer = probation.viewer_from_request(self.request, self.user, "192.168.65.3")
        self.assertEqual(viewer.server_device, f"app|{self.user.id}|3f2a9c1e-0b7d-4e21-9a55-0f1c2d3e4f50")
        self.assertEqual(viewer.multiview, "mv-7")
        self.assertTrue(probation.is_identified(viewer))
        # The switch hint is its own switch
        self.assertEqual(app_devices.declared_previous_channel(self.request), "")
        app_devices.save_settings({"switch_hints": True})
        self.assertEqual(app_devices.declared_previous_channel(self.request), "11111111-2222-3333-4444-555555555555")

    def test_the_channel_left_may_be_given_by_its_number_id(self):
        """What an Xtream link (/live/<user>/<pass>/<id>) gives the app to say."""
        from apps.channels.models import Channel

        app_devices.save_settings({"switch_hints": True})
        channel = Channel.objects.create(name="ORF 1", channel_number=1)
        request = RequestFactory().get("/", HTTP_X_DISPATCH_PREVIOUS_CHANNEL=str(channel.id))
        self.assertEqual(app_devices.declared_previous_channel(request), str(channel.uuid))
        request = RequestFactory().get("/", HTTP_X_DISPATCH_PREVIOUS_CHANNEL="999999")
        self.assertEqual(app_devices.declared_previous_channel(request), "")

    def test_the_same_by_query_parameter_for_what_cannot_set_headers(self):
        app_devices.save_settings({"devices": True})
        request = RequestFactory().get("/proxy/ts/stream/abc", {"dm_device": "cast-receiver-0001"})
        self.assertEqual(app_devices.declared_device(request), "cast-receiver-0001")

    def test_anything_but_an_identifier_is_ignored(self):
        app_devices.save_settings({"devices": True, "switch_hints": True})
        request = RequestFactory().get(
            "/", HTTP_X_DISPATCH_DEVICE="bad|value", HTTP_X_DISPATCH_PREVIOUS_CHANNEL="../x"
        )
        self.assertEqual(app_devices.declared_device(request), "")
        self.assertEqual(app_devices.declared_previous_channel(request), "")

    def test_capabilities_are_there_for_any_logged_in_user(self):
        viewer_user = User.objects.create_user(username="tv", password="x", user_level=0)
        client = APIClient()
        client.force_authenticate(user=viewer_user)
        answer = client.get("/api/core/capabilities/").json()
        self.assertEqual(answer["app_integration"], 1)
        self.assertFalse(answer["devices"])
        self.assertEqual(answer["headers"]["device"], "X-Dispatch-Device")
        self.assertEqual(answer["headers"]["previous"], "X-Dispatch-Previous-Channel")
        self.assertEqual(APIClient().get("/api/core/capabilities/").status_code, 401)

    def test_switched_on_from_the_diagnostics_page(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        answer = client.post(
            "/proxy/diagnostics/", {"app_integration": {"devices": True}}, format="json"
        ).json()
        self.assertEqual(answer["app_integration"], {"devices": True, "switch_hints": False})
