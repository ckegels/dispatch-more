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


class RenamingTests(_Setup):
    def test_a_group_is_renamed_and_its_channels_are_left_alone(self):
        guide_layout.rename_group(self.austria.id, "┃AT┃ ÖSTERREICH")
        self.austria.refresh_from_db()
        self.one.refresh_from_db()
        self.assertEqual(self.austria.name, "┃AT┃ ÖSTERREICH")
        self.assertEqual(self.one.name, "┃AT┃ ORF 1")

    def test_a_name_another_group_already_has_is_refused(self):
        with self.assertRaises(ValueError):
            guide_layout.rename_group(self.austria.id, "┃AT┃ NEWS")

    def test_taking_something_out_of_every_name_in_a_group(self):
        names = {1: "┃DE┃ ARD", 2: "┃DE┃ ZDF", 3: "VIP | RTL"}
        self.assertEqual(
            guide_layout.renamed(names, "┃DE┃ "), {1: "ARD", 2: "ZDF"}
        )

    def test_what_is_taken_out_is_text_and_not_a_pattern(self):
        # Somebody typing "┃DE┃" or "VIP |" means those characters. A name full of
        # box-drawing and brackets is exactly what turns into a pattern nobody meant.
        names = {1: "A (B) C", 2: "A B C"}
        self.assertEqual(guide_layout.renamed(names, "(B)"), {1: "A C"})

    def test_the_space_left_behind_is_tidied_up(self):
        self.assertEqual(guide_layout.renamed({1: "┃DE┃ RTL HD"}, "HD"), {1: "┃DE┃ RTL"})

    def test_a_name_that_would_be_left_empty_is_not_changed(self):
        self.assertEqual(guide_layout.renamed({1: "VIP"}, "VIP"), {})

    def test_channels_are_renamed(self):
        self.assertEqual(
            guide_layout.rename_channels({self.one.id: "ORF Eins"})["renamed"], 1
        )
        self.one.refresh_from_db()
        self.assertEqual(self.one.name, "ORF Eins")


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

    def test_renaming_through_the_page(self):
        answer = self.api.post(
            "/api/channels/guide-layout/rename/",
            {"group": self.austria.id, "name": "┃AT┃ ÖSTERREICH"}, format="json",
        )
        self.assertEqual(answer.json()["name"], "┃AT┃ ÖSTERREICH")
        # A name another group has is a refusal, not a 500
        self.assertEqual(
            self.api.post(
                "/api/channels/guide-layout/rename/",
                {"group": self.austria.id, "name": "┃AT┃ NEWS"}, format="json",
            ).status_code,
            400,
        )

    def test_asking_what_taking_something_out_would_leave_before_doing_it(self):
        asked = self.api.post(
            "/api/channels/guide-layout/rename/",
            {"channels": [self.one.id, self.two.id], "take_off": "┃AT┃ "}, format="json",
        ).json()
        self.assertEqual(asked["names"][str(self.one.id)], "ORF 1")
        self.one.refresh_from_db()
        self.assertEqual(self.one.name, "┃AT┃ ORF 1")  # nothing written by asking

        self.api.post(
            "/api/channels/guide-layout/rename/",
            {"channels": [self.one.id, self.two.id], "take_off": "┃AT┃ ", "apply": True},
            format="json",
        )
        self.one.refresh_from_db()
        self.assertEqual(self.one.name, "ORF 1")

    def test_only_an_admin(self):
        plain = APIClient()
        plain.force_authenticate(
            user=User.objects.create_user(username="someone", password="x", user_level=1)
        )
        self.assertEqual(plain.get("/api/channels/guide-layout/").status_code, 403)


class BadInputTests(TestCase):
    """Three ways this answered with a 500 where it meant to say "no"."""

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )
        self.group = ChannelGroup.objects.create(name="g")
        self.channel = Channel.objects.create(
            name="A", channel_number=1, channel_group=self.group
        )

    def test_a_group_that_is_not_a_number(self):
        answer = self.client_api.get("/api/channels/guide-layout/?groups=abc")
        self.assertEqual(answer.status_code, 400)

    def test_a_moved_channel_that_is_not_a_number(self):
        answer = self.client_api.post(
            "/api/channels/guide-layout/arrange/",
            {"order": [self.channel.id], "moved": "abc"},
            format="json",
        )
        self.assertEqual(answer.status_code, 400)

    def test_and_it_says_what_other_groups_those_numbers_land_on(self):
        # Numbers are worked out inside one group, and pushing channels along can reach
        # the numbers of the group above without anybody being told
        other = ChannelGroup.objects.create(name="above")
        Channel.objects.create(name="Theirs", channel_number=2, channel_group=other)
        second = Channel.objects.create(
            name="B", channel_number=5, channel_group=self.group
        )
        answer = self.client_api.post(
            "/api/channels/guide-layout/arrange/",
            {"order": [self.channel.id, second.id], "start": 1, "step": 1},
            format="json",
        ).json()
        self.assertEqual([one["name"] for one in answer["in_the_way"]], ["Theirs"])


class SafetyTests(TestCase):
    """The ways this could go wrong quietly, and what it does instead."""

    def setUp(self):
        self.group = ChannelGroup.objects.create(name="┃AT┃ AUSTRIA")
        self.other = ChannelGroup.objects.create(name="┃DE┃ GERMANY")

    def _channel(self, name, number, group=None):
        return Channel.objects.create(
            name=name, channel_number=number, channel_group=group or self.group
        )

    def test_renumbering_is_all_of_it_or_none_of_it(self):
        # A group renumbered one channel at a time and stopped half way leaves an
        # arrangement nobody asked for, and no way to tell which half is which
        from unittest.mock import patch

        one = self._channel("A", 1)
        two = self._channel("B", 2)
        real_save = Channel.save

        def fail_on_the_second(self, *args, **kwargs):
            if self.id == two.id:
                raise RuntimeError("the database went away")
            return real_save(self, *args, **kwargs)

        with patch.object(Channel, "save", fail_on_the_second):
            with self.assertRaises(RuntimeError):
                guide_layout.apply({one.id: 100, two.id: 101})

        one.refresh_from_db()
        self.assertEqual(one.channel_number, 1, "the first one was rolled back too")

    def test_a_long_name_is_not_cut_in_half(self):
        # Channel.name holds 512; renaming used to cut at 255
        long_name = "N" * 400
        channel = self._channel("short", 1)
        guide_layout.rename_channels({channel.id: long_name})
        channel.refresh_from_db()
        self.assertEqual(len(channel.name), 400)

    def test_a_group_name_already_taken_says_so_rather_than_failing(self):
        with self.assertRaises(ValueError) as caught:
            guide_layout.rename_group(self.group.id, "┃DE┃ GERMANY")
        self.assertIn("already a group", str(caught.exception))
        # ...however it is capitalised
        with self.assertRaises(ValueError):
            guide_layout.rename_group(self.group.id, "┃de┃ germany")

    def test_a_group_name_is_not_shortened_at_all(self):
        # ChannelGroup.name is a TextField: there is no length to cut it to
        long_name = "G" * 400
        guide_layout.rename_group(self.group.id, long_name)
        self.group.refresh_from_db()
        self.assertEqual(len(self.group.name), 400)
