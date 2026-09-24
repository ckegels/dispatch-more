"""Reading the programmes of the guides someone is choosing between, in one pass.

Dispatcharr reads a guide's programmes only once a channel uses it, and its own task
streams the whole XMLTV to pick out one tvg_id. So the guide window's entries hold
nothing, and reading them one at a time would read the same file once each. This is the
one pass that reads them together (apps.channels.tasks.read_guide_programmes).
"""

import os
import tempfile

from django.test import TestCase

from apps.channels.tasks import read_guide_programmes
from apps.epg.models import EPGData, EPGSource, ProgramData

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <programme channel="ORF1.at" start="20260920080000 +0000" stop="20260920090000 +0000">
    <title>Zeit im Bild</title>
    <desc>The news</desc>
  </programme>
  <programme channel="ORF1.at" start="20260920090000 +0000" stop="20260920100000 +0000">
    <title>Bundesland heute</title>
  </programme>
  <programme channel="ORF2.at" start="20260920080000 +0000" stop="20260920090000 +0000">
    <title>Something else</title>
    <sub-title>An episode</sub-title>
  </programme>
  <programme channel="NOBODY.at" start="20260920080000 +0000" stop="20260920090000 +0000">
    <title>Not asked for</title>
  </programme>
</tv>
"""


class ReadGuideProgrammesTests(TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False)
        handle.write(XMLTV)
        handle.close()
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))
        self.source = EPGSource.objects.create(
            name="Austria", source_type="xmltv", file_path=self.path
        )
        self.one = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.source)
        self.two = EPGData.objects.create(tvg_id="ORF2.at", name="ORF 2", epg_source=self.source)

    def _read(self, *entries):
        return read_guide_programmes({str(self.source.id): [e.id for e in entries]})

    def test_what_was_found_is_written_down_once_not_once_per_guide(self):
        """
        The record holds every guide ever read. Writing it back inside the loop is that
        whole record read and written again for each guide.
        """
        from unittest.mock import patch

        from apps.channels import channel_manager

        with patch.object(
            channel_manager, "note_read", wraps=channel_manager.note_read
        ) as noting:
            self._read(self.one, self.two)
        self.assertEqual(noting.call_count, 1, "written once, for all of them")
        self.assertEqual(noting.call_args[0][0], {self.one.id: 2, self.two.id: 1})

    def test_every_guide_asked_for_is_read_in_the_one_pass(self):
        self._read(self.one, self.two)
        self.assertEqual(
            sorted(ProgramData.objects.filter(epg=self.one).values_list("title", flat=True)),
            ["Bundesland heute", "Zeit im Bild"],
        )
        (other,) = ProgramData.objects.filter(epg=self.two)
        self.assertEqual(other.title, "Something else")
        self.assertEqual(other.sub_title, "An episode")

    def test_a_guide_not_asked_for_is_left_alone(self):
        self._read(self.one)
        self.assertEqual(ProgramData.objects.filter(epg=self.two).count(), 0)
        # and nothing is kept for a channel in the file nobody asked about
        self.assertEqual(ProgramData.objects.exclude(epg=self.one).count(), 0)

    def test_the_programmes_come_out_as_dispatcharrs_own_parse_makes_them(self):
        self._read(self.one)
        news = ProgramData.objects.get(epg=self.one, title="Zeit im Bild")
        self.assertEqual(news.description, "The news")
        self.assertEqual(news.tvg_id, "ORF1.at")
        self.assertEqual(news.end_time - news.start_time, __import__("datetime").timedelta(hours=1))

    def test_reading_again_replaces_what_was_there_rather_than_doubling_it(self):
        self._read(self.one)
        self._read(self.one)
        self.assertEqual(ProgramData.objects.filter(epg=self.one).count(), 2)

    def test_a_guide_with_nothing_in_the_file_really_is_empty(self):
        nothing = EPGData.objects.create(tvg_id="GONE.at", name="Gone", epg_source=self.source)
        self._read(nothing)
        self.assertEqual(ProgramData.objects.filter(epg=nothing).count(), 0)

    def test_a_guide_a_channel_is_on_keeps_what_it_has_when_the_file_has_nothing(self):
        """
        Reading a guide is how somebody chooses between guides, and it answers by replacing
        what the guide holds. For a guide nobody uses that is the whole point; for one a
        channel is on, a file that says nothing about it would leave that channel with no
        programmes at all until the next refresh -- to answer a question nobody asked about
        it. What is there stays.
        """
        from apps.channels.models import Channel
        from django.utils import timezone
        from datetime import timedelta

        gone = EPGData.objects.create(tvg_id="GONE.at", name="Gone", epg_source=self.source)
        moment = timezone.now()
        ProgramData.objects.create(
            epg=gone, title="What it is showing now",
            start_time=moment, end_time=moment + timedelta(hours=1),
        )
        Channel.objects.create(name="Gone TV", channel_number=9, epg_data=gone)

        self._read(gone)

        self.assertEqual(ProgramData.objects.filter(epg=gone).count(), 1)

    def test_but_one_nobody_uses_is_emptied_as_before(self):
        nothing = EPGData.objects.create(tvg_id="GONE.at", name="Gone", epg_source=self.source)
        from django.utils import timezone
        from datetime import timedelta

        moment = timezone.now()
        ProgramData.objects.create(
            epg=nothing, title="Stale", start_time=moment, end_time=moment + timedelta(hours=1)
        )
        self._read(nothing)
        self.assertEqual(ProgramData.objects.filter(epg=nothing).count(), 0)

    def test_it_says_where_it_has_got_to_as_it_reads(self):
        from unittest.mock import patch

        from apps.channels import channel_manager
        from apps.channels.tests.test_guide_manager import FakeRedis

        fake = FakeRedis()
        with patch("apps.channels.channel_manager._reading_redis", return_value=fake):
            self._read(self.one, self.two)
            state = channel_manager.reading_state()
        # A pass of a big guide file is minutes, and a spinner that says nothing looks
        # exactly like one that has jammed
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["done"], 2)
        self.assertEqual(state["total"], 2)

    def test_a_missing_file_is_handed_over_rather_than_given_up_on(self):
        """
        This used to answer "Read 0 guide(s)" and stop, which is what these two tests
        asked for -- and it is exactly the complaint: the button reports that it has read
        the guides and not one of them has anything. Dispatcharr's own task fetches the
        file when it is missing, so the guides go to it instead.
        """
        from unittest.mock import patch

        os.remove(self.path)
        with patch("apps.epg.tasks.parse_programs_for_tvg_id.delay") as asked:
            answer = self._read(self.one)
        asked.assert_called_once_with(self.one.id, force=True)
        self.assertIn("still to come", answer)
        self.assertEqual(ProgramData.objects.count(), 0)

    def test_a_source_being_refreshed_is_waited_for_rather_than_left(self):
        # The file is rewritten by a refresh, so it cannot be read now -- which is a
        # reason to come back, not a reason to stop
        from unittest.mock import patch

        with patch("core.utils.is_task_lock_held", return_value=True), patch(
            "apps.channels.tasks.read_guide_programmes.apply_async"
        ) as again:
            answer = self._read(self.one)
        self.assertTrue(again.called)
        self.assertIn("still to come", answer)
        self.assertEqual(ProgramData.objects.count(), 0)


class KeptAfterReadingTests(TestCase):
    """
    Dispatcharr deletes the programmes of every guide no channel uses each time a source
    refreshes -- every guide being suggested. A read from the Guides page lasted until the
    next refresh, and the page went back to "nothing on" for what it had just read.
    """

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        self.source = EPGSource.objects.create(name="xmltvfr.fr", source_type="xmltv")
        self.read = EPGData.objects.create(tvg_id="TF1.fr", name="TF1", epg_source=self.source)
        self.not_read = EPGData.objects.create(tvg_id="M6.fr", name="M6", epg_source=self.source)
        moment = timezone.now()
        for guide in (self.read, self.not_read):
            ProgramData.objects.create(
                epg=guide, title="Le journal",
                start_time=moment - timedelta(minutes=10), end_time=moment + timedelta(minutes=20),
            )

    def _refresh_cleans_up(self):
        from apps.epg.tasks import _delete_orphaned_epg_programs

        _delete_orphaned_epg_programs(self.source)
        return set(ProgramData.objects.values_list("epg__name", flat=True))

    def test_a_guide_read_from_the_page_keeps_its_programmes_through_a_refresh(self):
        from apps.channels import channel_manager

        channel_manager.note_read({self.read.id: 1})
        self.assertEqual(self._refresh_cleans_up(), {"TF1"})

    def test_nothing_is_kept_for_anybody_who_has_not_read_one(self):
        # Exactly stock
        self.assertEqual(self._refresh_cleans_up(), set())

    def test_a_read_that_found_nothing_keeps_nothing(self):
        from apps.channels import channel_manager

        channel_manager.note_read({self.read.id: 0})
        self.assertEqual(self._refresh_cleans_up(), set())

    def test_and_a_read_days_old_is_cleared_as_usual(self):
        from datetime import timedelta
        from unittest.mock import patch

        from django.utils import timezone

        from apps.channels import channel_manager

        long_ago = timezone.now() - timedelta(days=channel_manager.READ_KEPT_DAYS, hours=1)
        with patch("django.utils.timezone.now", return_value=long_ago):
            channel_manager.note_read({self.read.id: 1})
        self.assertEqual(self._refresh_cleans_up(), set())
