"""Driving iptv-org/epg from Dispatcharr (apps.channels.epg_grabber).

The grabber is not rewritten here and nothing about it is changed: what these are about is
everything that was being done by hand around it -- running it on a timer, not twice at
once, checking what came out before it replaces a working guide, and handing it over.
"""

import os
import tempfile
import time
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels import epg_grabber
from apps.epg.models import EPGSource

GUIDE = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="waiq"><display-name>Alabama Public TV (WAIQ)</display-name></channel>
  <channel id="kqed"><display-name>PBS (KQED)</display-name></channel>
  <programme channel="waiq" start="20260923080000 +0000" stop="20260923090000 +0000">
    <title>Sesame Street</title>
  </programme>
  <programme channel="kqed" start="20260923080000 +0000" stop="20260923090000 +0000">
    <title>Nova</title>
  </programme>
</tv>
"""

NOTHING_MUCH = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="waiq"><display-name>Alabama Public TV (WAIQ)</display-name></channel>
</tv>
"""


class FakeRedis:
    """Enough Redis for a grab to say how it is going."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    def exists(self, *keys):
        return sum(1 for key in keys if key in self.data)

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


class _Setup(TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        # What a checked-out, installed grabber looks like
        open(os.path.join(self.folder, "package.json"), "w").write("{}")
        os.makedirs(os.path.join(self.folder, "sites", "tvpassport.com"), exist_ok=True)
        os.makedirs(os.path.join(self.folder, "node_modules"), exist_ok=True)
        open(
            os.path.join(self.folder, "sites", "tvpassport.com", "tvpassport.com.channels.xml"), "w"
        ).write('<channels><channel site="tvpassport.com" site_id="x/1">A</channel></channels>')
        self.output = os.path.join(self.folder, "data", "pbs.xmltv")
        self.redis = FakeRedis()

    def _settings(self, **overrides):
        job = {
            "id": "pbs", "name": "PBS (TV Passport)", "enabled": True,
            "channels": os.path.join(self.folder, "sites", "tvpassport.com", "tvpassport.com.channels.xml"),
            "days": 3, "output": self.output,
        }
        job.update(overrides.pop("job", {}))
        return epg_grabber.save_settings({
            "enabled": True, "folder": self.folder, "jobs": [job], **overrides
        })

    def _wrote(self, text, code=0, lines=()):
        """A stand-in grabber: says a few lines, writes what it was told to, and stops."""
        def fake(argv, cwd=None, **kwargs):
            where = next(one for one in argv if one.startswith("--output=")).split("=", 1)[1]
            os.makedirs(os.path.dirname(where), exist_ok=True)
            if text is not None:
                open(where, "w").write(text)
            return _FakeProcess(lines or ["[1/2] tvpassport.com - one", "[2/2] tvpassport.com - two"], code)

        return fake


class _FakeProcess:
    def __init__(self, lines, code):
        self.stdout = iter(lines)
        self._code = code
        self.pid = os.getpid()

    def wait(self, timeout=None):
        return self._code

    def terminate(self):
        pass


class SettingsTests(_Setup):
    def test_it_is_off_and_empty_until_it_is_set_up(self):
        settings = epg_grabber.load_settings()
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["jobs"], [])
        # The command the project's own README uses, three dashes and all
        self.assertEqual(settings["command"], ["npm", "run", "grab", "---"])

    def test_a_command_can_be_given_as_a_line_and_is_kept_as_words(self):
        # No shell runs it, so what is typed is read once, here, and never again
        saved = epg_grabber.save_settings({"command": "npm run grab ---", "jobs": []})
        self.assertEqual(saved["command"], ["npm", "run", "grab", "---"])

    def test_a_guide_has_to_say_what_to_grab_and_where_to_put_it(self):
        with self.assertRaises(ValueError):
            epg_grabber.save_settings({"jobs": [{"name": "PBS", "output": "/tmp/x.xml"}]})
        with self.assertRaises(ValueError):
            epg_grabber.save_settings({"jobs": [{"name": "PBS", "sites": "tvpassport.com"}]})

    def test_a_guide_is_a_channel_list_or_whole_sites_never_both(self):
        # A channel list already says which site each of its channels is on, so naming
        # sites as well says two different things and the grabber takes one or the other
        with self.assertRaises(ValueError) as refused:
            epg_grabber.save_settings({"jobs": [{
                "name": "PBS", "channels": "/tmp/pbs.xml", "sites": "tvpassport.com",
                "output": "/tmp/pbs.xmltv",
            }]})
        self.assertIn("not both", str(refused.exception))

    def test_a_list_from_anywhere_is_allowed_not_only_the_ones_it_found(self):
        # One somebody made lives wherever they put it
        saved = epg_grabber.save_settings({"jobs": [{
            "name": "PBS", "channels": "/home/me/lists/pbs.xml", "output": "/tmp/pbs.xmltv",
        }]})
        self.assertEqual(saved["jobs"][0]["channels"], "/home/me/lists/pbs.xml")

    def test_what_is_set_becomes_what_somebody_would_have_typed(self):
        settings = self._settings(job={
            "days": 3, "lang": "en", "timeout_ms": 45000, "delay_ms": 250,
            "max_connections": 4, "gzip": True,
        })
        argv = epg_grabber._argv(settings["jobs"][0], settings)

        self.assertEqual(argv[:4], ["npm", "run", "grab", "---"])
        self.assertIn(f"--output={self.output}.part", argv)
        self.assertIn("--days=3", argv)
        self.assertIn("--lang=en", argv)
        self.assertIn("--timeout=45000", argv)
        self.assertIn("--delay=250", argv)
        self.assertIn("--maxConnections=4", argv)
        self.assertIn("--gzip", argv)

    def test_and_what_is_not_set_is_left_to_the_grabbers_own_default(self):
        # A site's own number of days is usually the number of days that site has
        settings = self._settings(job={"days": 0, "timeout_ms": 0, "max_connections": 0})
        argv = epg_grabber._argv(settings["jobs"][0], settings)
        self.assertFalse([one for one in argv if one.startswith(("--days", "--timeout", "--maxConnections"))])


class WhatIsThereTests(_Setup):
    def test_it_says_when_the_grabber_is_where_it_should_be(self):
        with mock.patch("shutil.which", return_value="/usr/bin/npm"):
            found = epg_grabber.look_at_it(self._settings())
        self.assertTrue(found["ok"], found["why"])
        self.assertEqual(found["sites"], 1)

    def test_and_says_which_part_is_missing_when_it_is_not(self):
        settings = self._settings(folder="/nowhere/at/all")
        self.assertIn("not there", epg_grabber.look_at_it(settings)["why"])

        os.rename(os.path.join(self.folder, "node_modules"), os.path.join(self.folder, "was_there"))
        self.assertIn("npm install", epg_grabber.look_at_it(self._settings())["why"])

    def test_the_channel_lists_it_has_are_offered(self):
        found = epg_grabber.channel_files(self._settings())
        self.assertEqual([one["site"] for one in found], ["tvpassport.com"])
        self.assertEqual(found[0]["channels"], 1)


PASSPORT = """<?xml version="1.0" encoding="UTF-8"?>
<channels>
  <channel site="tvpassport.com" site_id="alabama-public-tv--pbs-waiq/5149" lang="en" xmltv_id="">Alabama Public TV - PBS (WAIQ) Montgomery, AL</channel>
  <channel site="tvpassport.com" site_id="pbs-kqed/1" lang="en" xmltv_id="">PBS (KQED) San Francisco, CA</channel>
  <channel site="tvpassport.com" site_id="pbs-kqed-radio/2" lang="en" xmltv_id="">PBS Radio (KQED) San Francisco, CA</channel>
  <channel site="tvpassport.com" site_id="cnn/3" lang="en" xmltv_id="">CNN</channel>
</channels>
"""


class MakeListTests(_Setup):
    """
    The grep that was being done by hand, done where the rest of it is -- and giving back
    a document rather than a heap of lines, which is what "Text data outside of root node"
    means when grep's output is handed to the grabber.
    """

    def setUp(self):
        super().setUp()
        self.big = os.path.join(self.folder, "sites", "tvpassport.com", "tvpassport.com.channels.xml")
        open(self.big, "w").write(PASSPORT)
        self.into = os.path.join(self.folder, "data", "pbs.channels.xml")

    def test_it_says_what_would_be_kept_before_anything_is_written(self):
        found = epg_grabber.make_list(self.big, "PBS")
        self.assertEqual((found["kept"], found["of"]), (3, 4))
        self.assertIn("PBS (KQED) San Francisco, CA", found["sample"])
        self.assertFalse(found["written"])
        self.assertFalse(os.path.exists(self.into))

    def test_case_does_not_matter_and_the_site_id_counts_too(self):
        # grep -i saw the whole line, which is the name and the ids
        self.assertEqual(epg_grabber.make_list(self.big, "pbs")["kept"], 3)
        self.assertEqual(epg_grabber.make_list(self.big, "kqed")["kept"], 2)

    def test_what_is_left_out_is_left_out(self):
        found = epg_grabber.make_list(self.big, "PBS", leaving_out="radio")
        self.assertEqual(found["kept"], 2)

    def test_what_is_written_is_a_document_the_grabber_can_read(self):
        from lxml import etree

        found = epg_grabber.make_list(self.big, "PBS", into=self.into, write=True)

        self.assertTrue(found["written"])
        # Read back the way the grabber reads it: a heap of <channel> lines is not XML,
        # and that is the error the hand-made list gave
        tree = etree.parse(self.into)
        self.assertEqual(tree.getroot().tag, "channels")
        self.assertEqual(len(tree.getroot()), 3)
        # ...with everything the grabber needs kept on each of them
        first = tree.getroot()[0]
        self.assertEqual(first.get("site"), "tvpassport.com")
        self.assertTrue(first.get("site_id"))

    def test_and_it_is_then_one_of_the_lists_on_offer(self):
        epg_grabber.make_list(self.big, "PBS", into=self.into, write=True)
        self.assertIn(self.into, [one["path"] for one in epg_grabber.channel_files(self._settings())])

    def test_nothing_matching_is_not_a_list(self):
        with self.assertRaises(ValueError):
            epg_grabber.make_list(self.big, "nothing at all", into=self.into, write=True)

    def test_and_it_never_writes_over_what_it_was_made_from(self):
        with self.assertRaises(ValueError) as refused:
            epg_grabber.make_list(self.big, "PBS", into=self.big, write=True)
        self.assertIn("write over", str(refused.exception))


class RunTests(_Setup):
    def _run(self, fake, **overrides):
        settings = self._settings(**overrides)
        with mock.patch("subprocess.Popen", side_effect=fake):
            return epg_grabber.run(self.redis, only=None), settings

    def test_a_good_grab_replaces_the_guide_and_says_what_it_holds(self):
        answer, _ = self._run(self._wrote(GUIDE))

        (job,) = answer["jobs"]
        self.assertTrue(job["ok"], job.get("why"))
        self.assertEqual((job["channels"], job["programmes"]), (2, 2))
        self.assertTrue(os.path.exists(self.output))
        # ...and nothing is left behind beside it
        self.assertFalse(os.path.exists(f"{self.output}.part"))

    def test_a_grab_that_came_back_with_nothing_much_keeps_the_guide_you_had(self):
        os.makedirs(os.path.dirname(self.output), exist_ok=True)
        open(self.output, "w").write(GUIDE)

        answer, _ = self._run(self._wrote(NOTHING_MUCH))

        (job,) = answer["jobs"]
        self.assertFalse(job["ok"])
        self.assertIn("keeping the guide you had", job["why"])
        # The good one is exactly where it was
        self.assertIn("Sesame Street", open(self.output).read())

    def test_and_so_does_one_that_stopped_part_way(self):
        os.makedirs(os.path.dirname(self.output), exist_ok=True)
        open(self.output, "w").write(GUIDE)

        answer, _ = self._run(self._wrote(NOTHING_MUCH, code=1))

        (job,) = answer["jobs"]
        self.assertFalse(job["ok"])
        self.assertIn("code 1", job["why"])
        self.assertIn("Sesame Street", open(self.output).read())

    def test_it_says_how_far_through_it_is(self):
        self._run(self._wrote(GUIDE, lines=["[1/4539] one", "[1204/4539] two"]))
        said = epg_grabber.progress(self.redis)
        self.assertEqual((said["done"], said["total"]), (1204, 4539))
        self.assertEqual(said["state"], "done")

    def test_two_at_once_is_refused(self):
        self.redis.set(epg_grabber.RUNNING_KEY, "1")
        answer, _ = self._run(self._wrote(GUIDE))
        self.assertIn("already running", answer["error"])

    def test_how_the_last_run_went_is_kept_with_the_guide(self):
        self._run(self._wrote(GUIDE))
        (job,) = epg_grabber.load_settings()["jobs"]
        self.assertTrue(job["last"]["ok"])
        self.assertEqual(job["last"]["programmes"], 2)

    def test_the_guide_is_handed_to_dispatcharr_to_read(self):
        source = EPGSource.objects.create(name="PBS", source_type="xmltv", is_active=True)
        with mock.patch("apps.epg.tasks.refresh_epg_data.delay") as asked:
            answer, _ = self._run(self._wrote(GUIDE), job={"epg_source": source.id})

        source.refresh_from_db()
        self.assertEqual(source.file_path, self.output)
        asked.assert_called_once_with(source.id, force=True)
        self.assertEqual(answer["jobs"][0]["read_by"], "PBS")

    def test_but_not_when_what_came_back_was_no_good(self):
        source = EPGSource.objects.create(name="PBS", source_type="xmltv", is_active=True)
        with mock.patch("apps.epg.tasks.refresh_epg_data.delay") as asked:
            self._run(self._wrote(NOTHING_MUCH), job={"epg_source": source.id})
        asked.assert_not_called()


class HangingTests(_Setup):
    """
    A grabber that stops saying anything. Waiting for its next line is not the same as
    waiting for ever: whether to carry on has to be askable while nothing is arriving,
    which is exactly when it matters -- so these three are asked on a timer of their own,
    not when the next line happens to turn up.

    Everything here is measured in fractions of a second, which is what the floors under
    the settings are patched down to. os.killpg is what stops the grabber and everything
    it started; the stand-in has no process group of its own to kill.
    """

    def _quiet(self):
        def fake(argv, cwd=None, **kwargs):
            return _QuietProcess()

        return fake

    def _grab(self, **overrides):
        settings = {**self._settings(), **overrides}
        with mock.patch("subprocess.Popen", side_effect=self._quiet()), \
                mock.patch("os.killpg") as killed, \
                mock.patch.object(epg_grabber, "LEAST_SILENCE", 0.05), \
                mock.patch.object(epg_grabber, "LEAST_RUN", 0.05), \
                mock.patch.object(epg_grabber, "LOOK_EVERY", 0.02):
            how = epg_grabber._grab(self.redis, settings["jobs"][0], settings)
        return how, killed

    def test_one_that_says_nothing_is_stopped(self):
        how, killed = self._grab(silent_for_minutes=0.001, give_up_after_minutes=600)
        self.assertFalse(how["ok"])
        self.assertIn("said nothing", how["why"])
        killed.assert_called()

    def test_and_one_that_is_asked_to_stop(self):
        self.redis.set(epg_grabber.STOP_KEY, "1")
        how, killed = self._grab(silent_for_minutes=600, give_up_after_minutes=600)
        self.assertFalse(how["ok"])
        self.assertIn("asked to stop", how["why"])
        killed.assert_called()

    def test_a_long_one_keeps_saying_it_is_still_going(self):
        """
        A grab may be allowed to run longer than its lock lives, and a lock that quietly
        expires under a running grab is how a second one starts on top of the first.
        """
        self.redis.set(epg_grabber.RUNNING_KEY, "1")
        held = []
        self.redis.expire = lambda key, seconds: held.append((key, seconds))

        with mock.patch.object(epg_grabber, "HOLD_EVERY", 0.0):
            self._grab(silent_for_minutes=600, give_up_after_minutes=0.001)

        self.assertIn((epg_grabber.RUNNING_KEY, epg_grabber.RUNNING_TTL), held)

    def test_and_one_that_runs_longer_than_it_is_allowed_to(self):
        how, killed = self._grab(silent_for_minutes=600, give_up_after_minutes=0.001)
        self.assertFalse(how["ok"])
        self.assertIn("longer than it is allowed to", how["why"])
        killed.assert_called()


class _QuietProcess:
    """One that starts, says nothing at all, and never ends by itself."""

    def __init__(self):
        self.stdout = _NeverEnds()
        self.pid = os.getpid()

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class _NeverEnds:
    """
    Stdout that says nothing at all, which is what a grabber that has hung looks like.

    It blocks, because that is the point: read straight, the wait for the next line is
    the wait for ever, and nothing else can be asked meanwhile. It gives up after a few
    seconds so that a regression fails the suite rather than hanging it.
    """

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(5)
        raise StopIteration


class WhenTests(_Setup):
    def test_nothing_happens_while_it_is_off(self):
        settings = self._settings(enabled=False)
        self.assertFalse(epg_grabber.due(settings, self.redis))

    def test_nor_outside_the_hours_it_was_given(self):
        from datetime import datetime

        settings = self._settings(window_from="01:00", window_to="05:00")
        self.assertTrue(epg_grabber.due(settings, self.redis, now=datetime(2026, 9, 23, 2, 0)))
        self.assertFalse(epg_grabber.due(settings, self.redis, now=datetime(2026, 9, 23, 12, 0)))

    def test_and_a_window_over_midnight_is_the_night(self):
        from datetime import datetime

        settings = self._settings(window_from="23:00", window_to="06:00")
        for hour in (23, 0, 5):
            self.assertTrue(epg_grabber.in_window(settings, datetime(2026, 9, 23, hour, 30)))
        self.assertFalse(epg_grabber.in_window(settings, datetime(2026, 9, 23, 12, 0)))

    def test_it_is_due_again_once_the_hours_have_passed(self):
        settings = self._settings(every_hours=12)
        self.assertTrue(epg_grabber.due(settings, self.redis))

        with mock.patch("subprocess.Popen", side_effect=self._wrote(GUIDE)):
            epg_grabber.run(self.redis)
        self.assertFalse(epg_grabber.due(epg_grabber.load_settings(), self.redis))


class ViewTests(_Setup):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )

    def test_the_tab_is_told_what_is_there(self):
        self._settings()
        with mock.patch("shutil.which", return_value="/usr/bin/npm"):
            data = self.api.get("/api/channels/epg-grabber/").json()
        self.assertTrue(data["install"]["ok"])
        self.assertEqual(data["settings"]["jobs"][0]["name"], "PBS (TV Passport)")
        self.assertEqual(len(data["channel_files"]), 1)

    def test_a_grab_is_refused_while_one_is_running(self):
        self._settings()
        with mock.patch.object(epg_grabber, "is_running", return_value=True):
            answer = self.api.post("/api/channels/epg-grabber/run/", {}, format="json")
        self.assertEqual(answer.status_code, 409)

    def test_and_when_the_grabber_is_not_where_it_should_be(self):
        self._settings(folder="/nowhere/at/all")
        answer = self.api.post("/api/channels/epg-grabber/run/", {}, format="json")
        self.assertEqual(answer.status_code, 400)
        self.assertIn("not there", answer.json()["error"])

    def test_an_epg_source_for_a_guide_is_made_from_here(self):
        answer = self.api.post(
            "/api/channels/epg-grabber/source/",
            {"name": "PBS (TV Passport)", "output": self.output}, format="json",
        )
        self.assertEqual(answer.status_code, 200)
        source = EPGSource.objects.get(name="PBS (TV Passport)")
        # A file and no URL: Dispatcharr reads it off disk, so no web server in between
        self.assertEqual(source.file_path, self.output)
        self.assertFalse(source.url)

    def test_a_channel_list_is_made_from_the_page(self):
        big = os.path.join(self.folder, "sites", "tvpassport.com", "tvpassport.com.channels.xml")
        open(big, "w").write(PASSPORT)
        into = os.path.join(self.folder, "data", "pbs.channels.xml")

        shown = self.api.post(
            "/api/channels/epg-grabber/channel-list/",
            {"from": big, "keep": "PBS", "leave_out": "radio"}, format="json",
        ).json()
        self.assertEqual(shown["kept"], 2)
        self.assertFalse(shown["written"])

        made = self.api.post(
            "/api/channels/epg-grabber/channel-list/",
            {"from": big, "keep": "PBS", "leave_out": "radio", "into": into, "apply": True},
            format="json",
        ).json()
        self.assertTrue(made["written"])
        self.assertTrue(os.path.exists(into))

    def test_and_says_why_when_it_cannot_be(self):
        answer = self.api.post(
            "/api/channels/epg-grabber/channel-list/",
            {"from": "/nowhere.xml", "keep": "PBS"}, format="json",
        )
        self.assertEqual(answer.status_code, 400)
        self.assertIn("not there", answer.json()["error"])

    def test_only_an_admin(self):
        viewer = APIClient()
        viewer.force_authenticate(
            user=User.objects.create_user(username="v", password="x", user_level=0)
        )
        self.assertEqual(viewer.get("/api/channels/epg-grabber/").status_code, 403)
