"""A stream opened by its hash is still logged as a system event (Dispatch More)."""

import uuid

from django.test import TestCase

from core.models import SystemEvent
from core.utils import log_system_event


class StreamHashEventTests(TestCase):
    def test_a_stream_hash_is_kept_in_the_details(self):
        hash_ = "7e2656f1d036850051f38870a4b65d215695830cb61729fe095d5689db4aea21"
        log_system_event("channel_start", channel_id=hash_, channel_name="VRT 1 HD")
        event = SystemEvent.objects.get(event_type="channel_start")
        self.assertIsNone(event.channel_id)
        self.assertEqual(event.details["stream_hash"], hash_)

    def test_a_channel_uuid_as_before(self):
        channel = uuid.uuid4()
        log_system_event("channel_stop", channel_id=channel, channel_name="C")
        self.assertEqual(str(SystemEvent.objects.get(event_type="channel_stop").channel_id), str(channel))
