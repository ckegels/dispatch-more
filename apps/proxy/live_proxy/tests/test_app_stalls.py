"""When arrTV stutters, its channel moves to another stream (apps.proxy.live_proxy.app_stalls)."""

import time
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import app_devices, app_stalls, probation
from apps.proxy.live_proxy.redis_keys import RedisKeys
from apps.proxy.live_proxy.tests.test_probation import FakeRedis

SHIELD = "app|1|shield-0001"
PHONE = "app|1|phone-0002"


class StutterTests(TestCase):
    def setUp(self):
        from apps.channels.models import Channel, ChannelStream, Stream
        from apps.m3u.models import M3UAccount

        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        app_devices.save_settings({"devices": True, "stall_switch": True})

        one = M3UAccount.objects.create(name="TiviBridge", account_type="XC", server_url="http://a")
        two = M3UAccount.objects.create(name="Digitalizard.com", account_type="XC", server_url="http://b")
        self.channel = Channel.objects.create(name="┃AT┃ ORF 1", channel_number=1)
        made = [
            ("┃AT┃ ORF 1 FHD", one), ("┃AT┃ ORF 1 4K", two), ("AT| ORF 1 FHD", two),
            ("┃AT┃ ORF 1 HD", one), ("┃AT┃ ORF 1 SD", two),
        ]
        self.streams = {
            name: Stream.objects.create(name=name, url=f"http://x/{i}", m3u_account=account)
            for i, (name, account) in enumerate(made)
        }
        self.fallback = Stream.objects.create(name="Could Not Dispatch", url="http://local/f", is_custom=True)
        self.order = list(self.streams.values()) + [self.fallback]
        for position, stream in enumerate(self.order):
            ChannelStream.objects.create(channel=self.channel, stream=stream, order=position)

        self.uuid = str(self.channel.uuid)
        self.redis = FakeRedis()
        self.on("┃AT┃ ORF 1 FHD", started=time.time() - 120)
        self.watching(SHIELD)

        # Every stream but the current one has a connection free, in the channel's order after it
        def alternates(channel_uuid, current_id=None, allowed_m3u_profiles=None):
            ids = [s.id for s in self.order]
            at = ids.index(current_id) if current_id in ids else -1
            rotated = ids[at + 1:] + ids[:at + 1]
            return [{"stream_id": i, "profile_id": 1, "name": ""} for i in rotated if i != current_id]

        self.alternates = mock.patch("apps.proxy.live_proxy.url_utils.get_alternate_streams", side_effect=alternates)
        self.alternates.start()
        self.addCleanup(self.alternates.stop)
        self.switched = []

        def change(channel_uuid, new_url=None, user_agent=None, target_stream_id=None, **_):
            self.switched.append(target_stream_id)
            self.on(target_stream_id, started=None)
            return {"success": True}

        patcher = mock.patch(
            "apps.proxy.live_proxy.services.channel_service.ChannelService.change_stream_url", side_effect=change
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        events = mock.patch("apps.proxy.live_proxy.health.record_event")
        self.events = events.start()
        self.addCleanup(events.stop)

    def on(self, stream, started=None):
        """The channel plays this stream; started is when it started (or switched)."""
        stream_id = stream if isinstance(stream, int) else self.streams[stream].id
        key = RedisKeys.channel_metadata(self.uuid)
        self.redis.hset(key, mapping={"stream_id": stream_id, "init_time": time.time() - 600})
        # A switch a while ago, unless it is said to be now
        self.redis.hset(key, "stream_switch_time", started if started else time.time() - 60)

    def watching(self, device, client="c1"):
        self.redis.sadd(RedisKeys.clients(self.uuid), client)
        self.redis.hset(
            RedisKeys.client_metadata(self.uuid, client),
            mapping={"ip_address": "192.168.65.3", "user_id": "1", "user_agent": "AerioTV/-arr.",
                     **({"server_device": device} if device else {})},
        )

    def viewer(self, device=SHIELD, ip="192.168.65.3"):
        return probation.Viewer(ip, user_id=1, app="AerioTV/-arr.", server_device=device)

    def stutter(self, device=SHIELD, **given):
        return app_stalls.report(self.redis, self.viewer(device), {"channel_uuid": self.uuid, **given})

    def name(self, stream_id):
        return next(s.name for s in self.order if s.id == stream_id)

    def settled(self):
        """Time passes: the stream switched to is past its first seconds."""
        key = RedisKeys.channel_metadata(self.uuid)
        self.redis.hset(key, "stream_switch_time", time.time() - 60)
        self.redis.delete(app_stalls.SWITCHING_KEY.format(channel_uuid=self.uuid))

    def test_a_stutter_moves_the_channel_to_its_next_stream_at_once(self):
        answer = self.stutter(stalls=1, feed_media_ratio=0.7)
        # The 4K after it is better, so skipped: the next FHD, from the other provider
        self.assertEqual(answer, {"action": "switched", "stream": "AT| ORF 1 FHD"})
        self.assertEqual([self.name(i) for i in self.switched], ["AT| ORF 1 FHD"])
        self.events.assert_called_once()
        self.assertIn("feed_media_ratio 0.7", self.events.call_args[0][3])

    def test_never_to_a_better_stream_nor_the_fallback(self):
        self.on("┃AT┃ ORF 1 SD")
        self.assertEqual(self.stutter()["action"], "none")
        self.assertEqual(self.switched, [])

    def test_nothing_else_to_move_to_is_nothing_done(self):
        from apps.channels.models import ChannelStream

        ChannelStream.objects.filter(channel=self.channel).exclude(
            stream__in=[self.streams["┃AT┃ ORF 1 FHD"], self.fallback]
        ).delete()
        self.order = [self.streams["┃AT┃ ORF 1 FHD"], self.fallback]
        answer = self.stutter()
        self.assertEqual(answer["action"], "none")
        self.assertIn("no other stream", answer["reason"])

    def test_the_first_seconds_of_a_stream_are_its_own(self):
        self.on("┃AT┃ ORF 1 FHD", started=time.time() - 3)
        self.assertEqual(self.stutter()["action"], "none")
        self.assertEqual(self.switched, [])
        # The stall a switch itself causes does not walk the channel on
        self.on("┃AT┃ ORF 1 FHD", started=time.time() - 60)
        self.stutter()
        self.assertEqual(self.stutter()["action"], "none")
        self.assertEqual(len(self.switched), 1)

    def test_a_second_stutter_goes_down_and_is_remembered_for_the_device(self):
        self.stutter()
        self.settled()
        answer = self.stutter()
        # Not back to the stream it left, and lower before the same quality
        self.assertEqual(answer["stream"], "┃AT┃ ORF 1 HD")
        self.assertEqual(app_stalls.held_quality(self.redis, self.viewer()), "HD")
        # Remembered apart for where the device is: with home networks set, away is away
        self.assertEqual(
            [(h["device"], h["where"], h["quality"]) for h in app_stalls.held_devices(self.redis)],
            [(SHIELD, "any", "HD")],
        )
        # The next channel this device starts begins within it
        with mock.patch("core.utils.RedisClient.get_client", return_value=self.redis):
            self.assertEqual(app_devices.quality_limit_for(self.viewer()), "HD")
            app_stalls.forget_held(self.redis, SHIELD)
            self.assertEqual(app_devices.quality_limit_for(self.viewer()), "")

    def test_held_at_home_and_away_apart(self):
        app_devices.save_settings({"home_networks": "192.168.2.0/24"})
        self.stutter()
        self.settled()
        self.stutter()
        self.assertEqual(app_stalls.held_quality(self.redis, self.viewer(ip="192.168.65.3")), "HD")
        self.assertEqual(app_stalls.held_quality(self.redis, self.viewer(ip="192.168.2.40")), "")

    def test_not_on_somebody_elses_good_picture(self):
        self.watching(None, client="plex")
        self.assertIn("Someone else", self.stutter()["reason"])
        self.assertEqual(self.switched, [])

    def test_everyone_on_it_stuttering_moves_it(self):
        self.watching(PHONE, client="c2")
        self.assertEqual(self.stutter()["action"], "none")
        self.assertEqual(self.stutter(PHONE)["action"], "switched")

    def test_only_for_a_device_watching_that_channel(self):
        self.assertEqual(self.stutter(PHONE)["action"], "none")
        self.assertEqual(app_stalls.report(self.redis, self.viewer(None), {"channel_uuid": self.uuid})["action"], "none")

    def test_the_channel_may_be_given_by_its_number_id(self):
        answer = app_stalls.report(self.redis, self.viewer(), {"channel_id": self.channel.id})
        self.assertEqual(answer["action"], "switched")

    def test_off_nothing_is_held_and_the_endpoint_refuses(self):
        self.stutter()
        self.settled()
        self.stutter()
        app_devices.save_settings({"stall_switch": False})
        self.assertEqual(app_stalls.held_quality(self.redis, self.viewer()), "")
        user = User.objects.create_user(username="tv", password="x", user_level=1)
        client = APIClient()
        client.force_authenticate(user=user)
        self.assertEqual(client.post("/api/core/app-stall/", {"channel_uuid": self.uuid}, format="json").status_code, 403)
        caps = client.get("/api/core/capabilities/").json()
        self.assertFalse(caps["stall_switch"])
        self.assertEqual(caps["stall_url"], "/api/core/app-stall/")

    def test_the_endpoint_answers_any_logged_in_user(self):
        user = User.objects.create_user(username="tv", password="x", user_level=1)
        self.redis.delete(RedisKeys.clients(self.uuid))
        self.watching(f"app|{user.id}|shield-0001")
        client = APIClient()
        client.force_authenticate(user=user)

        def ask(device):
            with mock.patch("core.utils.RedisClient.get_client", return_value=self.redis):
                return client.post(
                    "/api/core/app-stall/", {"channel_uuid": self.uuid}, format="json",
                    HTTP_X_DISPATCH_DEVICE=device,
                )

        # Another device on the same login is not the one watching
        self.assertEqual(ask("phone-0002").json()["action"], "none")
        answer = ask("shield-0001")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["action"], "switched")
