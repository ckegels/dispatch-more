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
2026-09-19T09:24:00+0200 dispatcharr start-celery.sh[246822]: 2026-09-19 07:24:00,001 INFO plugins.recipes Cooked 3 channels
2026-09-19T09:24:01+0200 dispatcharr start-celery.sh[246822]: 2026-09-19 07:24:01,001 WARNING _dispatcharr_plugin_recipes.plugin Could not read the recipe book
2026-09-19T09:24:02+0200 dispatcharr start-celery.sh[246822]: 2026-09-19 07:24:02,001 INFO plugins.tuner-tools Nothing about recipes here
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
        # ...and the warning a plugin wrote, which the fixture gained with the plugins
        self.assertEqual(len(keep(level="WARNING")), 4)
        self.assertEqual([r["level"] for r in keep(topic="stream_check")], ["INFO"])
        self.assertEqual(len(keep(topic="m3u")), 1)
        self.assertEqual(len(keep(text="tivibridge")), 1)


class WhoWroteItTests(TestCase):
    """
    Every line Dispatcharr writes says which logger wrote it -- the format is "{asctime}
    {levelname} {name} {message}" -- so that is what a line is found by, rather than
    whether the words happen to appear somewhere in it.
    """

    def _find(self, words):
        return next(r for r in log_center.records(JOURNAL) if words in r["text"])

    def test_a_line_is_read_apart_into_what_wrote_it_and_what_it_said(self):
        record = self._find("batch ended")
        self.assertEqual(record["logger"], "apps.channels.stream_check")
        self.assertEqual(record["level"], "INFO")
        # The message, without the date and the level in front of it twice over
        self.assertTrue(record["text"].startswith("Stream Check: batch ended"))
        self.assertEqual(record["service"], "start-celery.sh")

    def test_a_line_nobody_formatted_is_still_a_line(self):
        stray = self._find("a stray one")
        self.assertEqual(stray["logger"], "")
        self.assertEqual(stray["level"], "ERROR")

    def test_a_part_is_what_its_loggers_wrote(self):
        # The words "M3U" are in this one, but it was the guide reader that wrote it
        made_up = {"logger": "apps.epg.tasks", "text": "Read the M3U account's guide", "level": "INFO"}
        self.assertTrue(log_center._keep(made_up, "ALL", "epg", ""))
        self.assertFalse(log_center._keep(made_up, "ALL", "m3u", ""))

    def test_and_the_words_are_for_the_lines_that_say_nothing(self):
        uwsgi = {"logger": "", "text": "spawned uWSGI worker for the M3U refresh", "level": ""}
        self.assertTrue(log_center._keep(uwsgi, "ALL", "m3u", ""))

    def test_the_channel_managers_own_tabs_can_be_asked_for(self):
        # They had nowhere of their own at all: everything they write was "everything else"
        found = {"logger": "apps.channels.guide_manager", "text": "Guides: done", "level": "INFO"}
        self.assertTrue(log_center._keep(found, "ALL", "channel_manager", ""))


class PluginLogTests(TestCase):
    """One plugin's lines, out of everything every plugin writes."""

    def _kept(self, plugin):
        return [
            r["text"] for r in log_center.records(JOURNAL)
            if log_center._keep(r, "ALL", "", "", plugin)
        ]

    def test_a_plugins_lines_are_its_own(self):
        self.assertEqual(
            self._kept("recipes"),
            ["Cooked 3 channels", "Could not read the recipe book"],
        )

    def test_including_the_ones_it_wrote_through_its_own_module(self):
        # A plugin doing logging.getLogger(__name__) comes out as the loader's name for it
        by_module = next(
            r for r in log_center.records(JOURNAL) if "recipe book" in r["text"]
        )
        self.assertEqual(by_module["logger"], "_dispatcharr_plugin_recipes.plugin")
        self.assertTrue(log_center.from_plugin(by_module, "recipes"))

    def test_and_never_another_plugins_line_that_mentions_it(self):
        # By the logger, never by the name appearing in a line: a plugin called "Cooking"
        # would otherwise own every line about a cooking channel
        others = next(r for r in log_center.records(JOURNAL) if "Nothing about recipes" in r["text"])
        self.assertFalse(log_center.from_plugin(others, "recipes"))
        self.assertEqual(self._kept("tuner-tools"), ["Nothing about recipes here"])

    def test_every_plugin_at_once_is_a_part_of_its_own(self):
        both = [
            r["text"] for r in log_center.records(JOURNAL)
            if log_center._keep(r, "ALL", "plugins", "")
        ]
        self.assertEqual(
            both,
            ["Cooked 3 channels", "Could not read the recipe book", "Nothing about recipes here"],
        )

    def test_the_plugins_installed_are_offered(self):
        from apps.plugins.models import PluginConfig

        PluginConfig.objects.create(key="recipes", name="Recipe Channels")
        self.assertEqual(
            log_center.plugins(), [{"key": "recipes", "name": "Recipe Channels"}]
        )


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

    def test_the_tab_is_told_the_parts_and_the_plugins_this_install_has(self):
        from apps.plugins.models import PluginConfig

        PluginConfig.objects.create(key="recipes", name="Recipe Channels")
        data = self.api.get("/api/core/log-center/").json()

        parts = {one["value"]: one["label"] for one in data["topics"]}
        self.assertEqual(parts["stream_check"], "Stream Check")
        self.assertIn("channel_manager", parts)
        self.assertEqual(data["plugins"], [{"key": "recipes", "name": "Recipe Channels"}])

    def test_one_plugins_lines_can_be_asked_for(self):
        data = self.api.get("/api/core/log-center/read/?plugin=recipes&since=24h").json()
        self.assertEqual(
            [r["text"] for r in data["records"]],
            ["Cooked 3 channels", "Could not read the recipe book"],
        )

    def test_the_bundle_says_what_plugins_are_installed(self):
        from apps.plugins.models import PluginConfig

        PluginConfig.objects.create(
            key="recipes", name="Recipe Channels", version="1.2.0", enabled=True,
            settings={"api_key": "not for sharing"},
        )
        answer = self.api.get("/api/core/log-center/bundle/")
        with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
            about = archive.read("about.json").decode()

        self.assertIn("Recipe Channels", about)
        self.assertIn("1.2.0", about)
        # Never their settings: that is where a plugin's keys and passwords are
        self.assertNotIn("not for sharing", about)

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
