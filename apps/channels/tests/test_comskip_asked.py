"""A recording that asks for Comskip gets it, even with the server-wide switch off (Dispatch More)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.channels.models import Channel, Recording
from apps.channels.tasks import comskip_asked_for


class ComskipAskedTests(TestCase):
    def recording(self, props):
        channel = Channel.objects.create(name="C", channel_number=1)
        now = timezone.now()
        return Recording.objects.create(channel=channel, start_time=now, end_time=now + timedelta(hours=1),
                                        custom_properties=props)

    def test_arrtv_asks_with_true_or_the_word(self):
        self.assertTrue(comskip_asked_for(self.recording({"comskip": True}).id))
        self.assertTrue(comskip_asked_for(self.recording({"comskip": "True"}).id))

    def test_not_asked(self):
        self.assertFalse(comskip_asked_for(self.recording({}).id))
        self.assertFalse(comskip_asked_for(self.recording({"comskip": False}).id))
        self.assertFalse(comskip_asked_for(self.recording({"comskip": {"status": "completed"}}).id))
        self.assertFalse(comskip_asked_for(999999))
