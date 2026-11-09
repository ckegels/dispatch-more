"""/api/epg/programs/ streamed, or paged when a page is asked for (ProgramViewSet.list).

Stock builds every programme of every guide into one answer and ignores `page_size` and
`tvg_id`. On 2026-11-09 one `?page_size=1000` from arrTV took a web worker from 207 MB to
7.9 GB in 50 seconds and the server with it. Without parameters the answer must stay byte for
byte stock's; with them it is one bounded page.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.epg.api_views import ProgramViewSet
from apps.epg.models import EPGData, EPGSource, ProgramData
from apps.epg.serializers import ProgramDataSerializer

URL = "/api/epg/programs/"


class ProgramListTests(TestCase):
    def setUp(self):
        patcher = mock.patch("celery.app.task.Task.apply_async")
        patcher.start()
        self.addCleanup(patcher.stop)
        admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.client = APIClient()
        self.client.force_authenticate(user=admin)
        source = EPGSource.objects.create(name="EPGShare FR", source_type="xmltv")
        self.tf1 = EPGData.objects.create(tvg_id="TF1.fr", name="TF1", epg_source=source)
        self.arte = EPGData.objects.create(tvg_id="Arte.fr", name="Arte", epg_source=source)
        self.now = timezone.now().replace(microsecond=0)

    def programme(self, epg, hours, title, **properties):
        start = self.now + timedelta(hours=hours)
        return ProgramData.objects.create(
            epg=epg, tvg_id=epg.tvg_id, title=title, start_time=start,
            end_time=start + timedelta(hours=1), custom_properties=properties or None,
        )

    def test_without_parameters_the_bytes_are_stocks_across_several_batches(self):
        for i in range(7):
            self.programme(self.tf1 if i % 2 else self.arte, i, f"Journal {i} \"édition\" ✓", new=i == 3)
        expected = JSONRenderer().render(
            ProgramDataSerializer(ProgramData.objects.select_related("epg").order_by("id"), many=True).data
        )
        with mock.patch.object(ProgramViewSet, "STREAM_BATCH", 3):
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming, "the whole list was built in memory")
        self.assertEqual(b"".join(response.streaming_content), expected)

    def test_an_empty_guide_is_an_empty_list(self):
        response = self.client.get(URL)
        self.assertEqual(b"".join(response.streaming_content), b"[]")

    def test_a_page_asked_for_is_one_page_in_start_order_with_a_next(self):
        """arrTV's bulk fallback: ?page_size=1000, following `next`."""
        for hours in (5, 1, 3, 2, 4):
            self.programme(self.tf1, hours, f"at {hours}")
        response = self.client.get(URL, {"page_size": 2})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(getattr(response, "streaming", False))
        page = response.json()
        self.assertEqual([p["title"] for p in page["results"]], ["at 1", "at 2"])
        self.assertIsNone(page["previous"])
        self.assertIn("page=2", page["next"])
        self.assertIn("page_size=2", page["next"])

        second = self.client.get(page["next"]).json()
        self.assertEqual([p["title"] for p in second["results"]], ["at 3", "at 4"])
        self.assertNotIn("page=", second["previous"] or "x")
        third = self.client.get(second["next"]).json()
        self.assertEqual([p["title"] for p in third["results"]], ["at 5"])
        self.assertIsNone(third["next"], "a next page past the last")

    def test_a_page_without_a_guide_is_what_is_still_to_come(self):
        """
        arrTV's guide when its grid fails: current-programs plus ten pages of these. From the
        oldest, ten pages would be the guide's history and nothing to come.
        """
        self.programme(self.tf1, -30, "a day ago")
        self.programme(self.tf1, -2, "over")
        running = self.programme(self.tf1, -0.5, "running")
        self.programme(self.tf1, 2, "next")
        page = self.client.get(URL, {"page_size": 1000}).json()
        self.assertEqual([p["title"] for p in page["results"]], ["running", "next"])
        self.assertEqual(page["results"][0]["id"], running.id)

    def test_one_guide_keeps_its_past(self):
        """arrTV's recording genre lookup looks for a programme that has ended."""
        self.programme(self.tf1, -30, "a day ago")
        self.programme(self.tf1, 2, "next")
        page = self.client.get(URL, {"tvg_id": "TF1.fr", "page_size": 200}).json()
        self.assertEqual([p["title"] for p in page["results"]], ["a day ago", "next"])

    def test_without_parameters_the_past_is_there_as_stock_has_it(self):
        self.programme(self.tf1, -30, "a day ago")
        body = b"".join(self.client.get(URL).streaming_content)
        self.assertIn(b"a day ago", body)

    def test_a_page_is_never_bigger_than_the_most_a_page_holds(self):
        for hours in range(5):
            self.programme(self.tf1, hours, f"at {hours}")
        with mock.patch.object(ProgramViewSet, "MAX_PAGE_SIZE", 3):
            page = self.client.get(URL, {"page_size": 1000000}).json()
        self.assertEqual(len(page["results"]), 3)
        self.assertIsNotNone(page["next"])

    def test_only_the_guide_asked_for(self):
        """arrTV's recording genre lookup: ?tvg_id=<id>&page_size=200."""
        self.programme(self.tf1, 1, "TF1 news")
        self.programme(self.arte, 2, "Arte doc")
        page = self.client.get(URL, {"tvg_id": "Arte.fr", "page_size": 200}).json()
        self.assertEqual([p["title"] for p in page["results"]], ["Arte doc"])
        self.assertIsNone(page["next"])
        # ...and without a page, the streamed list keeps to it too
        response = self.client.get(URL, {"tvg_id": "TF1.fr"})
        self.assertIn(b"TF1 news", b"".join(response.streaming_content))

    def test_numbers_that_cannot_be_read_are_the_defaults(self):
        self.programme(self.tf1, 1, "only")
        response = self.client.get(URL, {"page_size": "lots", "page": "-3"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["title"] for p in response.json()["results"]], ["only"])

    def test_one_programme_is_as_stock_has_it(self):
        programme = self.programme(self.tf1, 1, "detail")
        response = self.client.get(f"{URL}{programme.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "detail")
