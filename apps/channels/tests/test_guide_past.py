"""A guide refresh keeps the last few days of what was on (apps.channels.guide_past).

Every refresh deleted a guide's programmes and put in what the file holds now, so a guide
never went back further than its file -- usually not even to this morning. With
keep_past_days set, the finished programmes of those days stay, and where the new file
covers the same time the file wins. Off, it is stock's delete.
"""

from datetime import timedelta
from unittest import mock

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from apps.epg import tasks as epg_tasks
from apps.epg.models import EPGData, EPGSource, ProgramData
from apps.proxy.live_proxy import app_devices


class KeepPastTests(TestCase):
    def setUp(self):
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        # Nothing queued for real (saving a guide's programmes signals nothing, but a
        # stray task waits on a broker for twenty seconds)
        patcher = mock.patch("celery.app.task.Task.apply_async")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = EPGSource.objects.create(name="EPGShare AT", source_type="xmltv")
        self.guide = EPGData.objects.create(tvg_id="ORF1.at", name="ORF 1", epg_source=self.source)
        self.quiet = EPGData.objects.create(tvg_id="ORF2.at", name="ORF 2", epg_source=self.source)
        # Both on a channel, as every guide a refresh reads is: the ones on none are stock's
        # orphans, emptied whatever is kept
        from apps.channels.models import Channel

        Channel.objects.create(name="┃AT┃ ORF 1", channel_number=1, epg_data=self.guide)
        Channel.objects.create(name="┃AT┃ ORF 2", channel_number=2, epg_data=self.quiet)
        self.now = timezone.now()

    def on(self, guide, title, hours_ago, hours=1):
        start = self.now - timedelta(hours=hours_ago)
        return ProgramData.objects.create(
            epg=guide, title=title, start_time=start, end_time=start + timedelta(hours=hours)
        )

    def new(self, guide, title, hours_ago, hours=1):
        start = self.now - timedelta(hours=hours_ago)
        return ProgramData(epg=guide, title=title, start_time=start, end_time=start + timedelta(hours=hours))

    def keep(self, days):
        app_devices.save_settings({"keep_past_days": days})

    def the_old_ones(self):
        """Yesterday, five days ago, this morning (the file carries this morning too), later."""
        self.on(self.guide, "Yesterday", hours_ago=24)
        self.on(self.guide, "Five days ago", hours_ago=24 * 5)
        self.on(self.guide, "This morning", hours_ago=3)
        self.on(self.guide, "Tonight, old listing", hours_ago=-3)
        self.on(self.quiet, "ORF 2 yesterday", hours_ago=24)
        self.on(self.quiet, "ORF 2 tonight", hours_ago=-3)

    def the_new_file(self):
        # The file starts four hours ago: this morning is in it, yesterday is not
        return [
            self.new(self.guide, "This morning, new listing", hours_ago=4, hours=2),
            self.new(self.guide, "Tonight", hours_ago=-3),
        ]

    def titles(self, guide):
        return sorted(ProgramData.objects.filter(epg=guide).values_list("title", flat=True))

    def swap_staged(self):
        epg_tasks._prepare_epg_program_staging_table()
        epg_tasks._flush_epg_program_staging_batch(self.the_new_file())
        with transaction.atomic():
            epg_tasks._swap_staged_epg_programs([self.guide.id, self.quiet.id], self.source)

    def swap_parsed(self):
        epg_tasks._swap_parsed_epg_programs([self.guide.id, self.quiet.id], self.source, self.the_new_file())

    def test_off_a_refresh_replaces_everything_as_stock_does(self):
        self.the_old_ones()
        self.swap_staged()
        self.assertEqual(self.titles(self.guide), ["This morning, new listing", "Tonight"])
        self.assertEqual(self.titles(self.quiet), [])

    def test_kept_the_days_asked_for_and_the_new_file_wins_where_they_overlap(self):
        self.keep(3)
        self.the_old_ones()
        self.swap_staged()
        self.assertEqual(
            self.titles(self.guide), ["This morning, new listing", "Tonight", "Yesterday"]
        )
        # A guide the file had nothing for keeps its past, and not its old future
        self.assertEqual(self.titles(self.quiet), ["ORF 2 yesterday"])

    def test_the_same_for_a_database_without_the_staging_table(self):
        self.keep(3)
        self.the_old_ones()
        self.swap_parsed()
        self.assertEqual(
            self.titles(self.guide), ["This morning, new listing", "Tonight", "Yesterday"]
        )
        self.assertEqual(self.titles(self.quiet), ["ORF 2 yesterday"])

    def test_one_guide_read_on_its_own_keeps_its_past_too(self):
        from apps.channels.guide_past import delete_replaced, firsts_of

        self.keep(3)
        self.the_old_ones()
        new = [p for p in self.the_new_file() if p.epg_id == self.guide.id]
        with transaction.atomic():
            delete_replaced(ProgramData.objects.filter(epg=self.guide), firsts_of(new))
            ProgramData.objects.bulk_create(new)
        self.assertEqual(
            self.titles(self.guide), ["This morning, new listing", "Tonight", "Yesterday"]
        )

    def test_the_days_are_whole_and_at_most_a_week(self):
        self.assertEqual(app_devices.save_settings({"keep_past_days": "30"})["keep_past_days"], 7)
        self.assertEqual(app_devices.save_settings({"keep_past_days": "-2"})["keep_past_days"], 0)
        self.assertEqual(app_devices.save_settings({"keep_past_days": "x"})["keep_past_days"], 0)
        self.assertEqual(app_devices.load_settings()["keep_past_days"], 0)
