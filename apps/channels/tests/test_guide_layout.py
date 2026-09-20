"""What order the channels come in, and on which numbers (apps.channels.guide_layout).

The rule that matters: channels keep the numbers they have. Dragging one between two
others gives it the place it was dropped in and pushes the others along only as far as it
has to. A lineup somebody built over months is not rearranged because one channel moved.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import guide_layout
from apps.channels.models import Channel, ChannelGroup


class NumberingTests(TestCase):
    def test_a_channel_dropped_between_two_others_takes_that_place(self):
        existing = {1: 1, 2: 2, 3: 3, 4: 4, 5: 7}
        # The one on 7 dragged to third
        numbers = guide_layout.numbers_for([1, 2, 5, 3, 4], existing, moved=5)
        self.assertEqual(numbers, {1: 1, 2: 2, 5: 3, 3: 4, 4: 5})

    def test_and_the_others_move_only_as_far_as_they_must(self):
        # A gap after the third means nothing past it has to move at all
        existing = {1: 1, 2: 2, 3: 3, 4: 20, 5: 7}
        numbers = guide_layout.numbers_for([1, 2, 5, 3, 4], existing, moved=5)
        self.assertEqual(numbers[3], 4)
        self.assertEqual(numbers[4], 20)

    def test_nothing_moves_at_all_when_the_order_is_already_right(self):
        existing = {1: 1, 2: 5, 3: 40}
        self.assertEqual(guide_layout.numbers_for([1, 2, 3], existing), existing)

    def test_a_channel_dropped_first_takes_the_groups_first_number(self):
        existing = {1: 100, 2: 101, 3: 140}
        numbers = guide_layout.numbers_for([3, 1, 2], existing, moved=3)
        self.assertEqual(numbers, {3: 100, 1: 101, 2: 102})

    def test_a_channel_from_another_group_takes_its_new_groups_numbers(self):
        # It brings its old number with it: 5 dropped at the top of a group starting at
        # 100 takes 100, not 5
        existing = {9: 5, 1: 100, 2: 101}
        numbers = guide_layout.numbers_for([9, 1, 2], existing, moved=9)
        self.assertEqual(numbers, {9: 100, 1: 101, 2: 102})

    def test_and_dropped_at_the_end_it_follows_the_last_one(self):
        existing = {9: 5, 1: 100, 2: 101}
        numbers = guide_layout.numbers_for([1, 2, 9], existing, moved=9)
        self.assertEqual(numbers[9], 102)

    def test_a_channel_dropped_into_an_empty_group_keeps_its_number(self):
        self.assertEqual(guide_layout.numbers_for([9], {9: 5}, moved=9), {9: 5})

    def test_a_channel_with_no_number_is_given_one(self):
        numbers = guide_layout.numbers_for([1, 2], {1: 5, 2: None})
        self.assertEqual(numbers, {1: 5, 2: 6})

    def test_renumbering_a_group_outright_is_a_different_thing(self):
        self.assertEqual(
            guide_layout.renumbered([3, 1, 2], 100, 10), {3: 100, 1: 110, 2: 120}
        )


class _Setup(TestCase):
    def setUp(self):
        self.austria = ChannelGroup.objects.create(name="┃AT┃ AUSTRIA")
        self.news = ChannelGroup.objects.create(name="┃AT┃ NEWS")
        self.one = self._channel("┃AT┃ ORF 1", 1, self.austria)
        self.two = self._channel("┃AT┃ ORF 2", 2, self.austria)
        self.news_one = self._channel("┃AT┃ NEWS 1", 50, self.news)

    def _channel(self, name, number, group):
        return Channel.objects.create(name=name, channel_number=number, channel_group=group)


class LayoutTests(_Setup):
    def test_the_lineup_comes_back_group_by_group_in_number_order(self):
        found = guide_layout.layout()
        self.assertEqual([g["name"] for g in found["groups"]], ["┃AT┃ AUSTRIA", "┃AT┃ NEWS"])
        austria = found["groups"][0]
        self.assertEqual([c["name"] for c in austria["channels"]], ["┃AT┃ ORF 1", "┃AT┃ ORF 2"])
        self.assertEqual((austria["first"], austria["last"]), (1, 2))

    def test_the_room_between_one_group_and_the_next_is_said(self):
        # Austria ends at 2, news starts at 50: room for 47 more
        self.assertEqual(guide_layout.layout()["groups"][0]["room_after"], 47)

    def test_two_channels_on_one_number_are_pointed_out(self):
        # A media server sees one flat lineup, so this is a problem across groups and
        # nothing in Dispatcharr says so
        self._channel("┃AT┃ SOMETHING", 1, self.news)
        found = guide_layout.layout()
        self.assertEqual(found["clashes"], [1])
        austria = found["groups"][0]
        self.assertTrue(austria["channels"][0]["clashes"])
        self.assertFalse(austria["channels"][1]["clashes"])

    def test_only_the_groups_asked_for(self):
        found = guide_layout.layout([self.news.id])
        self.assertEqual([g["name"] for g in found["groups"]], ["┃AT┃ NEWS"])


class ApplyTests(_Setup):
    def test_the_numbers_are_written(self):
        self.assertEqual(guide_layout.apply({self.one.id: 5, self.two.id: 6})["changed"], 2)
        self.one.refresh_from_db()
        self.assertEqual(self.one.channel_number, 5)

    def test_a_channel_can_be_moved_to_another_group_as_well(self):
        guide_layout.apply({self.two.id: 51}, {self.two.id: self.news.id})
        self.two.refresh_from_db()
        self.assertEqual((self.two.channel_group_id, self.two.channel_number), (self.news.id, 51))

    def test_a_number_that_is_already_right_is_not_written_again(self):
        self.assertEqual(guide_layout.apply({self.one.id: 1})["changed"], 0)

    def test_a_channel_gone_since_the_page_was_looked_at_is_left_alone(self):
        self.assertEqual(guide_layout.apply({9999: 5}), {"changed": 0})


class ViewTests(_Setup):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )

    def test_the_page(self):
        data = self.api.get("/api/channels/guide-layout/").json()
        self.assertEqual(len(data["groups"]), 2)

    def test_asking_what_an_arrangement_would_come_to(self):
        answer = self.api.post(
            "/api/channels/guide-layout/arrange/",
            {"order": [self.two.id, self.one.id], "moved": self.two.id},
            format="json",
        ).json()
        self.assertEqual(answer["numbers"][str(self.two.id)], 1)
        self.assertEqual(answer["numbers"][str(self.one.id)], 2)
        # Both of them change, which is what the page shows before applying
        self.assertEqual(sorted(answer["changing"]), sorted([str(self.one.id), str(self.two.id)]))

    def test_renumbering_a_group_from_a_number(self):
        answer = self.api.post(
            "/api/channels/guide-layout/arrange/",
            {"order": [self.one.id, self.two.id], "start": 100, "step": 10},
            format="json",
        ).json()
        self.assertEqual(answer["numbers"][str(self.two.id)], 110)

    def test_applying_through_the_page(self):
        answer = self.api.post(
            "/api/channels/guide-layout/apply/",
            {"numbers": {str(self.one.id): 9}},
            format="json",
        )
        self.assertEqual(answer.json()["changed"], 1)
        self.assertEqual(
            self.api.post("/api/channels/guide-layout/apply/", {}, format="json").status_code, 400
        )

    def test_only_an_admin(self):
        plain = APIClient()
        plain.force_authenticate(
            user=User.objects.create_user(username="someone", password="x", user_level=1)
        )
        self.assertEqual(plain.get("/api/channels/guide-layout/").status_code, 403)
