"""The Logs tab (core.log_center): every log, wherever Dispatcharr writes it, readable in one place."""

import io
import os
import tempfile
import zipfile
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from core import log_center

# As the journal of a real installation has them
JOURNAL = """\
2026-09-19T09:21:38+0200 dispatcharr start-celery.sh[246822]: 2026-09-19 07:21:38,343 INFO apps.channels.stream_check Stream Check: batch ended (waiting), next in 1207 s
2026-09-19T09:22:40+0200 dispatcharr start-uwsgi.sh[205973]: 2026-09-19 07:22:40,100 ERROR django.request Internal Server Error: /proxy/diagnostics/
2026-09-19T09:22:40+0200 dispatcharr start-uwsgi.sh[205973]: Traceback (most recent call last):
2026-09-19T09:22:40+0200 dispatcharr start-uwsgi.sh[205973]:   File "/opt/dispatcharr/apps/proxy/live_proxy/diagnostics_views.py", line 86, in _channel_names
2026-09-19T09:22:40+0200 dispatcharr start-uwsgi.sh[205973]: ValueError: badly formed hexadecimal UUID string
2026-09-19T09:23:00+0200 dispatcharr start-uwsgi.sh[205973]: 2026-09-19 07:23:00,001 WARNING apps.m3u.tasks M3U refresh slow for TiviBridge
2026-09-19T09:23:05+0200 dispatcharr start-uwsgi.sh[205973]: 2026-09-19 07:23:05,001 INFO live_proxy.server Channel d4b5 has 1 clients, state: active
2026-09-19T09:23:06+0200 dispatcharr start-uwsgi.sh[205973]: Traceback (most recent call last):
2026-09-19T09:23:06+0200 dispatcharr start-uwsgi.sh[205973]: RuntimeError: a stray one
"""


class RecordTests(TestCase):
    def test_a_traceback_stays_with_the_error_that_reported_it(self):
        found = log_center.records(JOURNAL)
        error = next(r for r in found if "Internal Server Error" in r["text"])
        self.assertEqual(error["level"], "ERROR")
        self.assertIn("badly formed hexadecimal UUID string", error["text"])
        self.assertEqual(error["service"], "start-uwsgi.sh")

    def test_a_stray_traceback_is_an_error_of_its_own(self):
        found = log_center.records(JOURNAL)
        stray = [r for r in found if "a stray one" in r["text"]]
        self.assertEqual(len(stray), 1)
        self.assertEqual(stray[0]["level"], "ERROR")
        self.assertNotIn("a stray one", next(r for r in found if "1 clients" in r["text"])["text"])

    def test_narrowing_by_level_topic_and_text(self):
        found = log_center.records(JOURNAL)
        keep = lambda **kw: [r for r in found if log_center._keep(r, kw.get("level", "ALL"), kw.get("topic", ""), kw.get("text", ""))]
        self.assertEqual(len(keep(level="ERROR")), 2)
        self.assertEqual(len(keep(level="WARNING")), 3)
        self.assertEqual([r["level"] for r in keep(topic="stream_check")], ["INFO"])
        self.assertEqual(len(keep(topic="m3u")), 1)
        self.assertEqual(len(keep(text="tivibridge")), 1)


@override_settings(LOG_FILE_DIR="")
class ReadTests(TestCase):
    def _journal(self):
        return [
            mock.patch.object(log_center, "_systemd_units", return_value=["dispatcharr", "dispatcharr-celery"]),
            mock.patch.object(log_center, "_raw_journal", return_value=JOURNAL),
        ]

    def test_the_services_of_a_linux_install_are_its_sources(self):
        with self._journal()[0]:
            labels = [s["label"] for s in log_center.sources()]
        self.assertIn("Background tasks (Celery): refreshes, Stream Check", labels)

    def test_reading_asks_the_journal_for_the_chosen_services_and_time(self):
        units, raw = self._journal()
        with units, raw as journal:
            found = log_center.read(["journal:dispatcharr-celery"], since="15m", level="INFO")
        # ...and for a sensible number of lines rather than everything it has
        journal.assert_called_once_with(["dispatcharr-celery"], 15, log_center.PAGE_LINES)
        self.assertEqual(found["total"], len(found["records"]))

    def test_the_journal_is_never_read_whole(self):
        """
        "Everything" over a journal months old is gigabytes, read into one string in the
        web worker -- and with Follow on, every five seconds. The newest lines are asked
        for instead, far more than anybody reads but not the lot.
        """
        units, raw = self._journal()
        with units, raw as journal:
            log_center.read(since="all")
            page_lines = journal.call_args.args[2]
            log_center.download(since="all")
            download_lines = journal.call_args.args[2]

        self.assertEqual(page_lines, log_center.PAGE_LINES)
        self.assertEqual(download_lines, log_center.DOWNLOAD_LINES)
        self.assertGreater(log_center.DOWNLOAD_LINES, log_center.PAGE_LINES)

    def test_the_page_is_cut_the_download_is_not(self):
        units, raw = self._journal()
        with units, raw, mock.patch.object(log_center, "PAGE_RECORDS", 2):
            page = log_center.read(since="all")
            everything = log_center.download(since="all").decode()
        self.assertTrue(page["cut"])
        self.assertEqual(len(page["records"]), 2)
        self.assertIn("a stray one", everything)
        self.assertIn("Stream Check: batch ended", everything)


class CollectorFileTests(TestCase):
    def test_dockers_collector_files_are_read_oldest_first(self):
        folder = tempfile.mkdtemp()
        open(os.path.join(folder, "dispatcharr.log.1"), "w").write("2026-09-19 07:00:00,000 INFO x older\n")
        open(os.path.join(folder, "dispatcharr.log"), "w").write("2026-09-19 08:00:00,000 ERROR x newer\n")
        with override_settings(LOG_FILE_DIR=folder), mock.patch.object(log_center, "_systemd_units", return_value=[]):
            self.assertEqual([s["id"] for s in log_center.sources()], ["file:dispatcharr.log"])
            found = log_center.read(since="all")
        self.assertEqual([r["text"].split()[-1] for r in found["records"]], ["older", "newer"])


    def test_a_files_time_is_read_as_the_server_writes_it(self):
        """
        The collector writes the server's own time with nothing to say which it is. Read
        as UTC, "the last fifteen minutes" of a file kept the wrong quarter of an hour on
        any machine that is not on UTC -- an hour or more of lines either missing or
        wrongly kept.
        """
        import time as clock
        from datetime import datetime, timedelta

        # A machine behind UTC, which is where reading its time as UTC throws away the
        # lines somebody is looking for rather than keeping a few too many
        was = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        clock.tzset()

        def put_it_back():
            if was is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = was
            clock.tzset()

        self.addCleanup(put_it_back)

        folder = tempfile.mkdtemp()
        just_now = datetime.now().astimezone() - timedelta(minutes=2)
        long_ago = datetime.now().astimezone() - timedelta(hours=6)
        with open(os.path.join(folder, "dispatcharr.log"), "w") as handle:
            handle.write(f"{long_ago.strftime('%Y-%m-%d %H:%M:%S')},000 INFO x older\n")
            handle.write(f"{just_now.strftime('%Y-%m-%d %H:%M:%S')},000 INFO x newer\n")

        with override_settings(LOG_FILE_DIR=folder), mock.patch.object(
            log_center, "_systemd_units", return_value=[]
        ):
            found = log_center.read(since="15m")

        self.assertEqual([r["text"].split()[-1] for r in found["records"]], ["newer"])


class ViewTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=User.objects.create_user(username="admin", password="x", user_level=10))
        for patcher in (
            mock.patch.object(log_center, "_systemd_units", return_value=["dispatcharr"]),
            mock.patch.object(log_center, "_raw_journal", return_value=JOURNAL),
            mock.patch.object(log_center, "journal_readable", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_tab_reads_with_what_it_asks_for(self):
        data = self.api.get("/api/core/log-center/read/?level=error&since=24h").json()
        self.assertEqual(len(data["records"]), 2)

    def test_what_cannot_be_asked_for_is_ignored(self):
        data = self.api.get("/api/core/log-center/read/?level=nonsense&since=forever&topic=nope").json()
        self.assertEqual(data["total"], len(log_center.records(JOURNAL)))

    def test_the_whole_log_downloads_as_a_file(self):
        response = self.api.get("/api/core/log-center/download/?since=all")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(b"a stray one", response.content)

    def test_the_bundle_has_every_log_and_what_this_is(self):
        response = self.api.get("/api/core/log-center/bundle/")
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        self.assertIn("about.json", names)
        self.assertIn("logs/dispatcharr.log", names)

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(user=User.objects.create_user(username="v", password="x", user_level=0))
        self.assertEqual(viewer.get("/api/core/log-center/read/").status_code, 403)
