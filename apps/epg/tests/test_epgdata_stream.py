"""/api/epg/epgdata/ written out batch by batch (EPGDataViewSet.list).

Every call used to build the whole table in Python first: about 130 MB for a 15 MB answer on
the user's server, kept by the uWSGI worker for good, and requests piling up in one worker took
it to 6 GB (2026-10-20). The streamed answer must be byte for byte what DRF sent, and must not
need the whole table in memory.
"""

import gc
import json
import tracemalloc
from unittest import mock

from django.test import TestCase
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.epg.api_views import EPGDataViewSet
from apps.epg.models import EPGData, EPGSource
from apps.epg.serializers import EPGDataSerializer

URL = "/api/epg/epgdata/"


class EPGDataStreamTests(TestCase):
    def setUp(self):
        patcher = mock.patch("celery.app.task.Task.apply_async")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.source = EPGSource.objects.create(name="EPGShare DE", source_type="xmltv")

    def drf_bytes(self):
        """What DRF's own list rendered for the whole table."""
        return JSONRenderer().render(EPGDataSerializer(EPGData.objects.all(), many=True).data)

    def streamed(self, **headers):
        response = self.client.get(URL, **headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming, "the list was not streamed")
        return b"".join(response.streaming_content), response

    def test_an_empty_table_is_an_empty_list(self):
        body, response = self.streamed()
        self.assertEqual(body, b"[]")
        self.assertEqual(response["Content-Type"], "application/json")

    def test_the_bytes_are_drfs_across_several_batches(self):
        EPGData.objects.create(tvg_id="DasErste.de", name="Das Erste", icon_url="http://x/1.png", epg_source=self.source)
        EPGData.objects.create(tvg_id=None, name="Ö1 \"Klassik\" ✓", icon_url=None, epg_source=None)
        EPGData.objects.create(tvg_id="odd.de", name="line sep para", epg_source=self.source)
        for i in range(7):
            EPGData.objects.create(tvg_id=f"x{i}.fr", name=f"Chaîne {i}", epg_source=self.source)
        with mock.patch.object(EPGDataViewSet, "STREAM_BATCH", 3):
            body, _ = self.streamed()
        self.assertEqual(body, self.drf_bytes())
        self.assertEqual(len(json.loads(body)), 10)

    def test_exactly_one_batch_and_a_partial_one(self):
        for i in range(4):
            EPGData.objects.create(tvg_id=f"y{i}", name=f"Y {i}", epg_source=self.source)
        with mock.patch.object(EPGDataViewSet, "STREAM_BATCH", 4):
            self.assertEqual(self.streamed()[0], self.drf_bytes())
        with mock.patch.object(EPGDataViewSet, "STREAM_BATCH", 3):
            self.assertEqual(self.streamed()[0], self.drf_bytes())

    def test_the_browsable_api_and_an_indented_answer_keep_drfs_path(self):
        EPGData.objects.create(tvg_id="a", name="A", epg_source=self.source)
        html = self.client.get(URL, HTTP_ACCEPT="text/html")
        self.assertEqual(html.status_code, 200)
        self.assertFalse(html.streaming)
        indented = self.client.get(URL, HTTP_ACCEPT="application/json; indent=2")
        self.assertFalse(indented.streaming)
        self.assertEqual(json.loads(indented.content), json.loads(self.drf_bytes()))

    def test_a_login_is_still_needed(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))

    def peak(self, work):
        """Peak Python memory of work() alone, with earlier garbage collected first."""
        gc.collect()
        tracemalloc.start()
        work()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return peak

    def test_the_whole_table_is_never_in_memory_at_once(self):
        EPGData.objects.bulk_create(
            [EPGData(tvg_id=f"tvg{i}.example", name=f"Channel number {i}", icon_url=f"http://logos/{i}.png",
                     epg_source=self.source) for i in range(40_000)]
        )

        def stream():
            for _ in self.client.get(URL).streaming_content:
                pass

        drf, streamed = self.peak(self.drf_bytes), self.peak(stream)
        print(f"\n40,000 rows: DRF peak {drf / 1e6:.1f} MB, streamed peak {streamed / 1e6:.1f} MB")
        # DRF holds every row at once (~1 MB per 1,000 rows); streamed holds one batch of 2,000.
        # Measured flat at 8.3 MB from 80,000 to 160,000 rows (a 16.6 MB answer, the user's
        # size), so at 40,000 the gap is already wide whatever the garbage collector's timing.
        self.assertLess(streamed, drf / 3)
        self.assertLess(streamed, 20_000_000)
