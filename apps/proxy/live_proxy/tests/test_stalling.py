"""A stream that keeps stalling is left for the channel's next stream (live_proxy.stalling)."""

from unittest import mock

from django.test import TestCase

from apps.proxy.live_proxy import stalling
from apps.proxy.live_proxy.tests.test_health_reconnect import _make_manager


class StallCounterTests(TestCase):
    def test_the_third_stall_within_five_minutes(self):
        counter = stalling.StallCounter()
        self.assertFalse(counter.stalled(0))
        self.assertFalse(counter.stalled(40))
        self.assertTrue(counter.stalled(80))

    def test_stalls_further_apart_do_not_add_up(self):
        counter = stalling.StallCounter()
        self.assertFalse(counter.stalled(0))
        self.assertFalse(counter.stalled(200))
        # The first is more than five minutes before this one
        self.assertFalse(counter.stalled(400))
        self.assertTrue(counter.stalled(450))


class RealStreamsTests(TestCase):
    def setUp(self):
        from apps.channels.models import Stream
        from apps.m3u.models import M3UAccount

        provider = M3UAccount.objects.create(name="TiviBridge", account_type="XC", server_url="http://a")
        self.hd = Stream.objects.create(name="CANAL+ HD", url="http://x/1", m3u_account=provider)
        self.fhd = Stream.objects.create(name="CANAL+ FHD", url="http://x/2", m3u_account=provider)
        self.fallback = Stream.objects.create(name="Could Not Dispatch", url="http://local/f", is_custom=True)

    def entries(self, *streams):
        return [{"stream_id": s.id, "profile_id": 1} for s in streams]

    def test_never_the_fallback(self):
        self.assertEqual(
            [e["stream_id"] for e in stalling.real_streams(self.entries(self.fallback, self.hd, self.fhd))],
            [self.hd.id, self.fhd.id],
        )

    def test_the_first_real_stream_not_tried(self):
        with mock.patch(
            "apps.proxy.live_proxy.url_utils.get_alternate_streams",
            return_value=self.entries(self.hd, self.fallback, self.fhd),
        ):
            self.assertEqual(stalling.real_alternative("ch", 1, {self.hd.id}), self.fhd.id)
            self.assertIsNone(stalling.real_alternative("ch", 1, {self.hd.id, self.fhd.id}))


class _Buffer:
    index = 50
    redis_client = None


class LeaveStallingTests(TestCase):
    def manager(self):
        sm = _make_manager(buffer=_Buffer())
        sm._stalls = stalling.StallCounter()
        sm._leaving_stalling = False
        sm.last_health_action_time = 0
        return sm

    def stall_three_times(self, sm, alternative=7, on=True):
        with mock.patch.object(stalling, "enabled", return_value=on), \
                mock.patch.object(stalling, "real_alternative", return_value=alternative), \
                mock.patch("apps.proxy.live_proxy.health.record_event"):
            for at in (0, 30, 60):
                sm._note_stall(at)

    def test_the_third_stall_moves_the_channel(self):
        sm = self.manager()
        self.stall_three_times(sm)
        self.assertTrue(sm.needs_stream_switch)
        self.assertTrue(sm._leaving_stalling)

    def test_off_it_stays_as_in_dispatcharr(self):
        sm = self.manager()
        self.stall_three_times(sm, on=False)
        self.assertFalse(sm.needs_stream_switch)

    def test_nothing_else_to_move_to_it_stays(self):
        sm = self.manager()
        self.stall_three_times(sm, alternative=None)
        self.assertFalse(sm.needs_stream_switch)

    def test_at_the_start_the_failover_decides(self):
        sm = self.manager()
        sm.buffer.index = 0
        self.stall_three_times(sm)
        self.assertFalse(sm.needs_stream_switch)
        sm.buffer.index = 50

    def test_only_to_a_real_stream_not_tried(self):
        sm = self.manager()
        sm.alternates = [{"stream_id": 200, "profile_id": 1}, {"stream_id": 300, "profile_id": 1}]
        with mock.patch("apps.proxy.live_proxy.input.manager.get_alternate_streams", return_value=sm.alternates), \
                mock.patch.object(stalling, "real_streams", return_value=[]):
            self.assertFalse(sm._try_next_stream(real_only=True))
