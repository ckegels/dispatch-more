"""Look-back priority (apps/timeshift/priority.py): when to move a live viewer, the steps arrTV
shows, moving back when the new stream does not play, and the cooldown."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.timeshift import priority


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    def delete(self, key):
        self.data.pop(key, None)

    def exists(self, key):
        return key in self.data

    def ttl(self, key):
        return 120 if key in self.data else -2


USER = SimpleNamespace(id=7)
CHANNEL = SimpleNamespace(uuid="look-back-channel", name="UK Food Network")


def holding_channel():
    return SimpleNamespace(uuid="live-channel", name="BE 24KITCHEN")


ALT = SimpleNamespace(id=55, name="24KITCHEN (TiviBridge)")
ALT_PROFILE = SimpleNamespace(id=9)


class MakeRoomTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patch = mock.patch.object(priority, "settings", lambda: (True, True))
        patch.start()
        self.addCleanup(patch.stop)

    def test_off_is_stock(self):
        with mock.patch.object(priority, "settings", lambda: (False, False)), \
             mock.patch.object(priority, "has_room") as has_room:
            self.assertIsNone(priority.make_room(USER, CHANNEL, self.redis))
        has_room.assert_not_called()

    def test_a_free_slot_goes_on_as_stock_and_ends_the_cooldown(self):
        self.redis.set(priority.cooldown_key(7, CHANNEL.uuid), "1")
        with mock.patch.object(priority, "has_room", return_value=True):
            self.assertIsNone(priority.make_room(USER, CHANNEL, self.redis))
        self.assertFalse(self.redis.exists(priority.cooldown_key(7, CHANNEL.uuid)))

    def test_the_askers_own_channel_or_look_back_is_never_a_refusal(self):
        self.redis.set(priority.cooldown_key(7, CHANNEL.uuid), "1")
        with mock.patch.object(priority, "has_room", return_value=False), \
             mock.patch.object(priority, "others_holding", return_value=[]), \
             mock.patch.object(priority, "candidates") as candidates:
            self.assertIsNone(priority.make_room(USER, CHANNEL, self.redis))
        candidates.assert_not_called()
        self.assertFalse(self.redis.exists(priority.cooldown_key(7, CHANNEL.uuid)))

    def test_nobody_can_move_refuses_with_a_cooldown(self):
        with mock.patch.object(priority, "has_room", return_value=False), \
             mock.patch.object(priority, "others_holding", return_value=["x"]), \
             mock.patch.object(priority, "candidates", return_value=[]):
            outcome, body = priority.make_room(USER, CHANNEL, self.redis)
        self.assertEqual((outcome, body["reason"]), ("refused", "viewing_priorities"))
        self.assertEqual(body["retry_after"], priority.COOLDOWN_SECONDS)
        # Asked again while it runs: refused at once, nobody looked at
        with mock.patch.object(priority, "has_room", return_value=False), \
             mock.patch.object(priority, "others_holding", return_value=["x"]), \
             mock.patch.object(priority, "candidates") as candidates:
            outcome, body = priority.make_room(USER, CHANNEL, self.redis)
        candidates.assert_not_called()
        self.assertEqual(outcome, "refused")

    def test_someone_can_move(self):
        found = [(holding_channel(), 12, 15, [(ALT, ALT_PROFILE, [])])]
        with mock.patch.object(priority, "has_room", return_value=False), \
             mock.patch.object(priority, "others_holding", return_value=["x"]), \
             mock.patch.object(priority, "candidates", return_value=found):
            self.assertEqual(priority.make_room(USER, CHANNEL, self.redis), ("moving", found))


class MoveTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakeRedis()
        for patch in (mock.patch.object(priority, "settings", lambda: (True, False)),
                      mock.patch.object(priority, "_provider_name", lambda _id: "Digitalizard")):
            patch.start()
            self.addCleanup(patch.stop)
        self.found = [(holding_channel(), 12, 15, [(ALT, ALT_PROFILE, [])])]

    def steps(self):
        return [s["step"] for s in json.loads(self.redis.get(priority.room_key("s1")))["steps"]]

    def run_move(self, verified, room_after=True):
        old = SimpleNamespace(id=12, name="old")
        with mock.patch.object(priority, "_switch", return_value=True) as switch, \
             mock.patch.object(priority, "verify", side_effect=verified) as verify, \
             mock.patch.object(priority, "has_room", return_value=room_after), \
             mock.patch("apps.m3u.models.M3UAccountProfile.objects") as profiles, \
             mock.patch("apps.channels.models.Stream.objects") as streams:
            profiles.select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(id=15)
            streams.filter.return_value.first.return_value = old
            priority.write_step(self.redis, "s1", "found", provider="Digitalizard")
            final = priority.run_move("s1", 7, CHANNEL, self.found, self.redis, sleep=lambda s: None)
        return final, switch, verify

    def test_moved_verified_and_ready(self):
        final, switch, _verify = self.run_move([True])
        self.assertEqual(final["state"], "ready")
        self.assertEqual(self.steps(), ["found", "moving", "moved", "verified", "ready"])
        switch.assert_called_once_with("live-channel", ALT, ALT_PROFILE)
        self.assertTrue(self.redis.exists(priority.moved_key("live-channel")), "not moved again for 10 minutes")
        self.assertFalse(self.redis.exists(priority.cooldown_key(7, CHANNEL.uuid)))

    def test_not_playing_moves_them_back_and_refuses(self):
        final, switch, _verify = self.run_move([False, True])
        self.assertEqual((final["state"], final["text"]), ("refused", priority.REFUSED_TEXT))
        self.assertEqual(self.steps(), ["found", "moving", "moved", "reverting", "refused"])
        self.assertEqual(switch.call_args_list[1].args[0], "live-channel")
        self.assertEqual(switch.call_args_list[1].args[1].id, 12, "back to their own stream")
        self.assertTrue(self.redis.exists(priority.cooldown_key(7, CHANNEL.uuid)))


class VerifyTests(SimpleTestCase):
    def test_bytes_from_the_new_stream(self):
        from apps.proxy.live_proxy.redis_keys import RedisKeys

        state = {"index": 100}

        class Redis:
            def hget(self, key, field):
                return {"stream_id": b"55", "state": b"active"}.get(field)

            def get(self, key):
                state["index"] += 5
                return str(state["index"]).encode()

        self.assertTrue(priority.verify(Redis(), "c", 55, seconds=2, sleep=lambda s: None))
        self.assertFalse(priority.verify(Redis(), "c", 56, seconds=0.01, sleep=lambda s: None))
        self.assertTrue(RedisKeys.buffer_index("c"))


class OthersTests(SimpleTestCase):
    def test_only_other_peoples_viewing_counts(self):
        clients = {"mine": [{"user_id": "7", "user_agent": "AerioTV"}],
                   "theirs": [{"user_id": "4", "user_agent": "AerioTV"}],
                   "recorded": [{"user_id": "0", "user_agent": "Dispatcharr-DVR"}]}
        with mock.patch.object(priority, "_catchup_profiles", return_value=({1}, [SimpleNamespace(id=15)])), \
             mock.patch("apps.proxy.live_proxy.probation._active_channels",
                        return_value=[("mine", 15), ("theirs", 15), ("elsewhere", 99)]), \
             mock.patch("apps.proxy.live_proxy.probation._channel_clients",
                        side_effect=lambda r, uuid: iter(clients.get(uuid, []))), \
             mock.patch("apps.proxy.live_proxy.probation.is_recording", side_effect=lambda a: "DVR" in a):
            self.assertEqual(priority.others_holding(CHANNEL, 7, FakeRedis()), ["theirs"])



class AskerTests(SimpleTestCase):
    """2026-10-02: a Google TV Stick and the Shield on one login. The Stick asks for ┃DE┃ BON
    GUSTO's look back (archive on profiles 12 and 14); the Shield watches NJAM! on 12, a BRAVIA
    PBS on 14, the Stick itself Food Network CA on 15."""

    STICK, SHIELD = "app|1|stick", "app|1|shield"
    CLIENTS = {
        "njam": [{"user_id": "1", "user_agent": "AerioTV", "server_device": SHIELD}],
        "pbs": [{"user_id": "4", "user_agent": "AerioTV", "server_device": "app|4|bravia"}],
        "food": [{"user_id": "1", "user_agent": "AerioTV", "server_device": STICK}],
    }
    ACTIVE = [("njam", 12), ("pbs", 14), ("food", 15)]

    def patches(self):
        return (
            mock.patch("apps.proxy.live_proxy.probation._active_channels", return_value=self.ACTIVE),
            mock.patch("apps.proxy.live_proxy.probation._channel_clients",
                       side_effect=lambda r, uuid: iter(self.CLIENTS.get(uuid, []))),
            mock.patch("apps.proxy.live_proxy.probation.is_recording", return_value=False),
        )

    def test_another_tv_on_the_same_login_is_another_viewer(self):
        a, b, c = self.patches()
        with a, b, c:
            self.assertEqual(priority._viewers(FakeRedis(), "njam", priority.Asker(1, self.STICK))[0], 1)
            self.assertEqual(priority._viewers(FakeRedis(), "food", priority.Asker(1, self.STICK))[0], 0)
            # Without a device it is the login, as before
            self.assertEqual(priority._viewers(FakeRedis(), "njam", priority.Asker(1))[0], 0)

    def test_a_provider_only_the_asker_holds_is_room(self):
        a, b, c = self.patches()
        with a, b, c:
            self.assertEqual(priority.askers_own(FakeRedis(), 15, priority.Asker(1, self.STICK)), ["food"])
            self.assertEqual(priority.askers_own(FakeRedis(), 12, priority.Asker(1, self.STICK)), [])

    def test_the_asker_holding_an_archive_provider_leaves_it(self):
        a, b, c = self.patches()
        user = SimpleNamespace(id=1)
        with a, b, c, \
             mock.patch.object(priority, "settings", lambda: (True, False)), \
             mock.patch.object(priority, "has_room", return_value=False), \
             mock.patch.object(priority, "_catchup_profiles",
                               return_value=({12, 15}, [SimpleNamespace(id=12), SimpleNamespace(id=15)])), \
             mock.patch.object(priority, "close_askers_own") as close:
            self.assertIsNone(priority.make_room(user, CHANNEL, FakeRedis(), device=self.STICK))
        close.assert_called_once()
        self.assertEqual(close.call_args.args[1], ["food"])

    def test_the_move_closes_the_askers_own_channel_first(self):
        redis = FakeRedis()
        found = [(holding_channel(), 12, 12, [(ALT, ALT_PROFILE, ["food"])])]
        with mock.patch.object(priority, "settings", lambda: (True, False)), \
             mock.patch.object(priority, "_provider_name", lambda _id: "TiviBridge2"), \
             mock.patch.object(priority, "close_askers_own") as close, \
             mock.patch.object(priority, "_wait_for_room", return_value=True), \
             mock.patch.object(priority, "_switch", return_value=True) as switch, \
             mock.patch.object(priority, "verify", return_value=True), \
             mock.patch.object(priority, "has_room", return_value=True), \
             mock.patch("apps.m3u.models.M3UAccountProfile.objects"):
            priority.write_step(redis, "s2", "found", provider="TiviBridge2")
            final = priority.run_move("s2", 1, CHANNEL, found, redis, sleep=lambda s: None)
        self.assertEqual(final["state"], "ready")
        self.assertEqual(close.call_args.args[1], ["food"])
        switch.assert_called_once_with("live-channel", ALT, ALT_PROFILE)
