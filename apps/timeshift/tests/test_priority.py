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
        found = [(holding_channel(), 12, 15, [(ALT, ALT_PROFILE)])]
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
        self.found = [(holding_channel(), 12, 15, [(ALT, ALT_PROFILE)])]

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

