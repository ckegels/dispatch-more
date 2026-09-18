"""Media servers: storing one, reading what it plays, and adding that to a channel start."""

import time
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.proxy.live_proxy import media_servers, timing

from .test_probation import FakeRedis, _make_account, _reset_in_use_cache

IDENTITY = {"MediaContainer": {"friendlyName": "Home Plex", "version": "1.41.0"}}
SESSION = {
    "MediaContainer": {
        "size": 1,
        "Metadata": [
            {
                "title": "ZIB",
                "live": "1",
                "addedAt": 1789580681,
                "User": {"title": "Ckegels"},
                "Player": {
                    "title": "Chrome",
                    "product": "Plex Web",
                    "machineIdentifier": "mkk9dgqsm9p8",
                    "state": "playing",
                },
                "viewOffset": 4000,
                "TranscodeSession": {
                    "videoDecision": "transcode",
                    "audioDecision": "transcode",
                    "speed": 0.9,
                    "maxOffsetAvailable": 30.0,
                },
            }
        ],
    }
}


def fake_response(body, status=200):
    response = MagicMock(status_code=status, ok=status < 400)
    response.json.return_value = body
    return response


def plex(url, **_kwargs):
    """Answer like Plex does, by path, so the order of the calls does not matter."""
    return fake_response(SESSION if "/status/sessions" in url else IDENTITY)


class MediaServerTests(TestCase):
    def setUp(self):
        self.server = {"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "secret"}

    def test_a_reachable_server_with_a_working_token(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex
            self.assertEqual(
                media_servers.check(self.server),
                {"ok": True, "name": "Home Plex", "version": "1.41.0"},
            )

    def test_a_refused_token_and_an_unreachable_server(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.return_value = fake_response({}, status=401)
            self.assertFalse(media_servers.check(self.server)["ok"])
            self.assertIn("token", media_servers.check(self.server)["error"])

        import requests

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = requests.exceptions.ConnectionError()
            result = media_servers.check(self.server)
        self.assertFalse(result["ok"])
        self.assertIn("Could not reach", result["error"])

    def test_what_is_playing_says_whether_it_is_live_tv(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.return_value = fake_response(SESSION)
            (session,) = media_servers.sessions(self.server)
        # Only live TV comes through Dispatcharr; a film on the same server does not
        self.assertEqual(session["watching"], "live TV")
        self.assertEqual(session["server"], "Plex")

    def test_what_is_playing_is_read(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.return_value = fake_response(SESSION)
            (session,) = media_servers.sessions(self.server)

        self.assertEqual(session["user"], "Ckegels")
        self.assertEqual(session["player"], "Chrome")
        self.assertEqual(session["decision"], "transcode (video + audio)")
        self.assertEqual(session["speed"], 0.9)
        self.assertTrue(session["live"])

    def test_direct_play_is_told_apart_from_transcoding(self):
        self.assertEqual(media_servers._decision({}), "direct play")
        self.assertEqual(
            media_servers._decision({"videoDecision": "copy", "audioDecision": "copy"}),
            "direct play",
        )
        self.assertEqual(
            media_servers._decision({"videoDecision": "copy", "audioDecision": "transcode"}),
            "transcode (audio)",
        )

    def test_plex_is_recognised_by_its_address_and_by_ffmpeg(self):
        from apps.proxy.live_proxy import probation

        # Plex pulls a tuner channel with ffmpeg, which says nothing about who is watching
        self.assertTrue(probation.is_media_server("Lavf/61.7.100"))
        self.assertIsNone(probation.app_name("Lavf/61.7.100"))
        # And a request from a configured server is that server, whatever it calls itself
        media_servers.forget_hosts()
        self.addCleanup(media_servers.forget_hosts)
        media_servers.save_servers(
            [{"id": "a1", "url": "http://192.168.2.141:32400", "token": "t"}]
        )
        self.assertTrue(probation.is_media_server("SomePlayer/1.0", "192.168.2.141"))
        self.assertIsNone(probation.app_name("SomePlayer/1.0", "192.168.2.141"))
        # Another address on the same network is still an ordinary player
        self.assertFalse(probation.is_media_server("SomePlayer/1.0", "192.168.2.50"))
        self.assertEqual(probation.app_name("SomePlayer/1.0", "192.168.2.50"), "SomePlayer/")

    def test_the_token_never_leaves_the_server(self):
        public = media_servers.public(self.server)
        self.assertNotIn("token", public)
        self.assertTrue(public["has_token"])


class MediaServerViewTests(TestCase):
    """Adding, listing and removing servers, for admins only."""

    def setUp(self):
        self.client_api = APIClient()
        self.admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.client_api.force_authenticate(user=self.admin)

    def _post(self, **data):
        return self.client_api.post("/proxy/media-servers/", data, format="json")

    def test_a_server_is_only_saved_when_it_answers(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.return_value = fake_response({}, status=401)
            response = self._post(name="Plex", url="http://plex:32400", token="wrong")

        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.json()["error"])
        self.assertEqual(media_servers.load_servers(), [])

    def test_adding_listing_and_removing(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex
            response = self._post(name="Plex", url="http://plex:32400/", token="secret")
            self.assertEqual(response.status_code, 200)
            (row,) = response.json()["servers"]
            self.assertTrue(row["online"])
            self.assertTrue(row["has_token"])
            # The address is stored without its trailing slash, and the token never comes back
            self.assertEqual(row["url"], "http://plex:32400")
            self.assertNotIn("token", row)
            self.assertEqual(row["sessions"][0]["user"], "Ckegels")

            server_id = row["id"]
            listed = self.client_api.get("/proxy/media-servers/").json()["servers"]
            self.assertEqual(len(listed), 1)

        removed = self.client_api.delete(f"/proxy/media-servers/?id={server_id}")
        self.assertEqual(removed.json()["servers"], [])

    def test_editing_without_the_token_keeps_the_stored_one(self):
        media_servers.save_servers(
            [{"id": "a1", "kind": "plex", "name": "Plex", "url": "http://plex:32400", "token": "secret"}]
        )
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex
            response = self._post(id="a1", name="Living room", url="http://plex:32400", token="")

        self.assertEqual(response.status_code, 200)
        (stored,) = media_servers.load_servers()
        self.assertEqual(stored["token"], "secret")
        self.assertEqual(stored["name"], "Living room")

    def test_an_address_that_is_not_a_url_is_refused(self):
        response = self._post(name="Plex", url="plex:32400", token="secret")
        self.assertEqual(response.status_code, 400)
        self.assertIn("http://", response.json()["error"])

    def test_only_admins(self):
        viewer = APIClient()
        viewer.force_authenticate(
            user=User.objects.create_user(username="viewer", password="x", user_level=0)
        )
        self.assertEqual(viewer.get("/proxy/media-servers/").status_code, 403)
        self.assertEqual(viewer.post("/proxy/media-servers/", {}, format="json").status_code, 403)


@patch("django.db.close_old_connections")
class StartWatchingTests(TestCase):
    """What the media server did is added to the channel start once it is playing."""

    def setUp(self):
        self.redis = FakeRedis()

    def _start(self):
        self.redis.hset(
            timing.START_KEY.format(start_id="s1"),
            mapping={"time": "1", "channel": "ZIB", "total": "1.21"},
        )
        return "s1"

    def test_nothing_happens_without_a_media_server_or_for_another_player(self, _close):
        with patch.object(media_servers, "gevent") as fake_gevent:
            media_servers.watch_start(self.redis, self._start(), "TiviMate/5.1.6", 1.0)
            fake_gevent.spawn.assert_not_called()

            media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])
            media_servers.watch_start(self.redis, "s1", "TiviMate/5.1.6", 1.0)
            fake_gevent.spawn.assert_not_called()

            # A media server, with a server configured: now it is worth watching
            media_servers.watch_start(self.redis, "s1", "PlexMediaServer/1.41", 1.0)
            fake_gevent.spawn.assert_called_once()

            # And Plex's real User-Agent when it pulls a tuner channel
            fake_gevent.spawn.reset_mock()
            media_servers.watch_start(self.redis, "s1", "Lavf/61.7.100", 1.0)
            fake_gevent.spawn.assert_called_once()

    def test_a_playing_session_is_added_to_the_start(self, _close):
        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])
        started_at = 1789580681.0

        def advancing(_url, **_kwargs):
            """Each poll, the player is a little further into the stream: it is really playing."""
            advancing.offset += 1000
            item = {**SESSION["MediaContainer"]["Metadata"][0], "viewOffset": advancing.offset}
            return fake_response({"MediaContainer": {"Metadata": [item]}})

        advancing.offset = 0
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ):
            get.side_effect = advancing
            media_servers._watch(self.redis, start_id, started_at)

        record = self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        self.assertEqual(record["server_user"], "Ckegels")
        self.assertEqual(record["server_decision"], "transcode (video + audio)")
        self.assertEqual(record["server_speed"], "0.9")
        self.assertIn("server_buffering", record)
        # Every stage the server went through, so a slow start can be blamed on one of them
        self.assertIn("session opened=", record["server_phases"])
        self.assertIn("transcode started=", record["server_phases"])
        self.assertIn("playing=", record["server_phases"])

    def test_a_session_that_never_plays_is_recorded_as_such(self, _close):
        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])
        buffering = {
            "MediaContainer": {
                "Metadata": [
                    {
                        **SESSION["MediaContainer"]["Metadata"][0],
                        "Player": {
                            **SESSION["MediaContainer"]["Metadata"][0]["Player"],
                            "state": "buffering",
                        },
                    }
                ]
            }
        }
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ), patch("apps.proxy.live_proxy.media_servers.time") as fake_time:
            get.return_value = fake_response(buffering)
            # deadline, then (loop check, "now") twice, then a check past the deadline
            fake_time.time.side_effect = [0, 1, 2, 3, 4, 999]
            media_servers._watch(self.redis, start_id, started_at=1789580681.0)

        record = self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        self.assertEqual(record["server_gave_up"], "1")
        self.assertIn("session opened=", record["server_phases"])
        self.assertNotIn("playing=", record["server_phases"])

    def test_a_session_that_was_already_playing_is_not_used(self, _close):
        """A session from before the request is someone else's stream, not this start."""
        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ), patch("apps.proxy.live_proxy.media_servers.time") as fake_time:
            get.return_value = fake_response(SESSION)
            fake_time.time.side_effect = [0, 1, 999]
            # The session started a minute before this channel was requested
            media_servers._watch(self.redis, start_id, started_at=1789580681.0 + 60)

        record = self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        self.assertNotIn("server_user", record)
        self.assertNotIn("server_gave_up", record)

    def test_a_live_session_has_no_position_so_the_server_is_believed(self, _close):
        """Live sessions carry no viewOffset at all: without it, "playing" is all there is."""
        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])
        live = {
            "MediaContainer": {
                "Metadata": [
                    {
                        k: v
                        for k, v in SESSION["MediaContainer"]["Metadata"][0].items()
                        if k != "viewOffset"
                    }
                ]
            }
        }
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ):
            get.return_value = fake_response(live)
            media_servers._watch(self.redis, start_id, started_at=1789580681.0)

        record = self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        self.assertIn("playing=", record["server_phases"])
        self.assertNotIn("server_gave_up", record)
        # And the page is told this is the server's word, not the player's position
        self.assertEqual(record.get("server_playing_is_certain", ""), "")

    def test_the_position_has_to_move_before_it_counts_as_playing(self, _close):
        """With a position (recorded media), Plex saying "playing" is not enough."""
        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])
        still = {
            "MediaContainer": {
                "Metadata": [
                    {**SESSION["MediaContainer"]["Metadata"][0], "viewOffset": 4000}
                ]
            }
        }
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ), patch("apps.proxy.live_proxy.media_servers.time") as fake_time:
            # The same position every time: the player is stuck, not playing
            get.return_value = fake_response(still)
            fake_time.time.side_effect = [0, 1, 2, 3, 4, 999]
            media_servers._watch(self.redis, start_id, started_at=1789580681.0)

        record = self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        self.assertEqual(record["server_gave_up"], "1")
        self.assertNotIn("playing=", record["server_phases"])


    def test_a_session_of_another_stream_is_not_used(self, _close):
        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ), patch("apps.proxy.live_proxy.media_servers.time") as fake_time:
            get.return_value = fake_response(SESSION)
            # The watching window passes without a session that started at the same moment
            fake_time.time.side_effect = [0, 1, 2, 999]
            media_servers._watch(self.redis, start_id, started_at=500000.0)

        record = self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        self.assertNotIn("server_user", record)

    def test_an_unreachable_server_adds_nothing_and_never_raises(self, _close):
        import requests as real_requests

        start_id = self._start()
        media_servers.save_servers([{"id": "a1", "url": "http://plex:32400", "token": "t"}])

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ), patch("apps.proxy.live_proxy.media_servers.time") as fake_time:
            get.side_effect = real_requests.exceptions.ConnectionError()
            fake_time.time.side_effect = [0, 1, 999]
            media_servers._watch(self.redis, start_id, 1.0)

        self.assertNotIn(
            "server_user", self.redis.hgetall(timing.START_KEY.format(start_id=start_id))
        )


DEVICES = {
    "MediaContainer": {
        "Device": [
            {
                "key": "22",
                "uri": "http://192.168.2.142:9191/hdhr/austria",
                "title": "Austria",
                "model": "Dispatcharr HDHomeRun - austria",
                "status": "alive",
                "tuners": "2",
            },
            {
                "key": "1",
                "uri": "http://192.168.2.141:34400",
                "title": "A1 TV",
                "model": "Threadfin",
                "status": "dead",
                "tuners": "2",
            },
        ]
    }
}
DVRS = {
    "MediaContainer": {
        "Dvr": [
            {
                "key": "32",
                "lineupTitle": "Belgium",
                "Device": [{"key": "22", "title": "Austria"}],
                "Lineup": [{"title": "Austria"}],
            }
        ]
    }
}


def _added_uri(post):
    """The address a tuner was registered with (adding one also makes a DVR for it)."""
    for call in post.call_args_list:
        if call.args[0].endswith("/media/grabbers/devices"):
            return call.kwargs["params"]["uri"]
    raise AssertionError("No tuner was added")


def plex_with_tuners(url, **_kwargs):
    if "/media/grabbers/devices" in url:
        return fake_response(DEVICES)
    if "/livetv/dvrs" in url:
        return fake_response(DVRS)
    return plex(url)


# One of ours that is in no DVR, which is what "place" is for
SPARE = {
    "MediaContainer": {
        "Device": DEVICES["MediaContainer"]["Device"]
        + [
            {
                "key": "40",
                "uuid": "device://tv.plex.grabbers.hdhomerun/dispatcharr-hdhr-austria-t2",
                "uri": "http://192.168.2.142:9191/proxy/hdhr/austria/tuners/2",
                "title": "Austria",
                "status": "alive",
                "tuners": "2",
            }
        ]
    }
}


def plex_with_a_spare_tuner(url, **_kwargs):
    if "/media/grabbers/devices" in url:
        return fake_response(SPARE)
    if "/livetv/dvrs" in url:
        return fake_response(DVRS)
    return plex(url)


def plex_with_a_spare_tuner_and_no_dvr(url, **_kwargs):
    if "/media/grabbers/devices" in url:
        return fake_response(SPARE)
    if "/livetv/dvrs" in url:
        return fake_response({"MediaContainer": {"size": 0}})
    return plex(url)


def plex_without_a_dvr(url, **_kwargs):
    """A server whose tuners are registered but which has no DVR: nothing plays yet."""
    if "/media/grabbers/devices" in url:
        return fake_response(DEVICES)
    if "/livetv/dvrs" in url:
        return fake_response({"MediaContainer": {"size": 0}})
    return plex(url)


class TunerTests(TestCase):
    """Seeing, adding, syncing and removing the tuners on a media server."""

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin", password="x", user_level=10)
        )
        media_servers.forget_hosts()
        self.addCleanup(media_servers.forget_hosts)
        media_servers.save_servers(
            [{"id": "a1", "name": "Plex", "url": "http://192.168.2.141:32400", "token": "t"}]
        )

    def test_the_tuners_are_listed_with_what_is_wrong_with_them(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex_with_tuners
            tuners = self.client_api.get("/proxy/media-servers/tuners/?server=a1").json()["tuners"]

        austria, threadfin = tuners
        self.assertEqual(austria["title"], "Austria")
        self.assertEqual(austria["dvr_id"], "32")
        self.assertEqual(austria["state"], "alive")
        # A leftover: dead, and in no DVR at all
        self.assertEqual(threadfin["state"], "dead")
        self.assertEqual(threadfin["dvr_id"], "")

    def test_a_tuner_is_added_for_a_channel_profile(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="austria")
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "channel_profile": "austria", "output_profile_id": 3},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        uri = _added_uri(post)
        self.assertTrue(uri.endswith("/hdhr/austria/output_profile/3"), uri)

    def test_a_profile_is_built_from_groups_without_touching_the_others(self):
        from apps.channels.models import (
            Channel,
            ChannelGroup,
            ChannelProfile,
            ChannelProfileMembership,
        )

        wanted = ChannelGroup.objects.create(name="Austria")
        other = ChannelGroup.objects.create(name="Belgium")
        Channel.objects.create(channel_number=1, name="ORF 1", channel_group=wanted)
        Channel.objects.create(channel_number=2, name="Een", channel_group=other)
        existing = ChannelProfile.objects.create(name="Everything")
        before = ChannelProfileMembership.objects.filter(channel_profile=existing).count()

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "new_profile_name": "austria",
                    "group_ids": [wanted.id],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        profile = ChannelProfile.objects.get(name="plexmedia-austria")
        channels = ChannelProfileMembership.objects.filter(channel_profile=profile)
        self.assertEqual([m.channel.name for m in channels], ["ORF 1"])
        # The profile that was already there is untouched
        self.assertEqual(
            ChannelProfileMembership.objects.filter(channel_profile=existing).count(), before
        )
        self.assertTrue(_added_uri(post).endswith("/hdhr/plexmedia-austria"))

    def test_the_address_is_remembered_and_the_name_fits_in_a_url(self):
        """The guessed address can be wrong (a missing port), and names have spaces in them."""
        from apps.channels.models import Channel, ChannelGroup, ChannelProfile

        group = ChannelGroup.objects.create(name="France")
        Channel.objects.create(channel_number=7, name="TF1", channel_group=group)

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "base_url": "http://192.168.2.142:9191/",
                    # The prefix is not repeated, and the space cannot go in an address
                    "new_profile_name": "PlexMedia France",
                    "group_ids": [group.id],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(ChannelProfile.objects.filter(name="plexmedia-France").exists())
        self.assertEqual(
            _added_uri(post), "http://192.168.2.142:9191/hdhr/plexmedia-France"
        )

        # The address is kept, so the next tuner does not need it typed again
        (stored,) = media_servers.load_servers()
        self.assertEqual(stored["dispatcharr_url"], "http://192.168.2.142:9191")
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex_with_tuners
            listed = self.client_api.get("/proxy/media-servers/tuners/?server=a1").json()
        self.assertEqual(listed["base_url"], "http://192.168.2.142:9191")

    def test_a_profile_name_that_is_not_a_url_is_still_usable(self):
        """A profile that already exists keeps its name, so the address is escaped instead."""
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="My Channels")
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "channel_profile": "My Channels",
                 "base_url": "http://192.168.2.142:9191"},
                format="json",
            )

        self.assertEqual(_added_uri(post), "http://192.168.2.142:9191/hdhr/My%20Channels")

    def test_an_address_that_is_not_a_url_is_refused(self):
        response = self.client_api.post(
            "/proxy/media-servers/tuners/",
            {"server": "a1", "channel_profile": "austria", "base_url": "192.168.2.142:9191"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("http://", response.json()["error"])

    def test_a_tuner_count_puts_the_tuner_on_our_own_hdhomerun(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="austria")
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "channel_profile": "austria",
                    "base_url": "http://192.168.2.142:9191",
                    "tuner_count": 2,
                },
                format="json",
            )

        self.assertEqual(
            _added_uri(post), "http://192.168.2.142:9191/proxy/hdhr/austria/tuners/2"
        )

        # Without a number it stays on Dispatcharr's own endpoint, which counts for itself
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "channel_profile": "austria",
                 "base_url": "http://192.168.2.142:9191"},
                format="json",
            )
        self.assertEqual(_added_uri(post), "http://192.168.2.142:9191/hdhr/austria")

    def test_a_profile_built_for_a_refused_tuner_does_not_stay_behind(self):
        from apps.channels.models import Channel, ChannelGroup, ChannelProfile

        group = ChannelGroup.objects.create(name="France")
        Channel.objects.create(channel_number=8, name="TF1", channel_group=group)

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            # The media server refuses the address
            post.side_effect = Exception("nope")
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "new_profile_name": "france", "group_ids": [group.id]},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        # Otherwise the next try fails with "already exists" over a profile nobody asked for
        self.assertFalse(ChannelProfile.objects.filter(name="plexmedia-france").exists())

    def test_a_silly_tuner_count_is_refused(self):
        response = self.client_api.post(
            "/proxy/media-servers/tuners/",
            {"server": "a1", "channel_profile": "austria", "tuner_count": 500},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("between 1 and", response.json()["error"])

    def test_building_a_profile_needs_a_group_and_a_free_name(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="plexmedia-taken")
        for body, expected in (
            ({"new_profile_name": "austria", "group_ids": []}, "channel group"),
            ({"new_profile_name": "taken", "group_ids": [1]}, "already exists"),
            ({}, "Choose a channel profile"),
        ):
            response = self.client_api.post(
                "/proxy/media-servers/tuners/", {"server": "a1", **body}, format="json"
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn(expected, response.json()["error"])

    def test_sync_rescans_and_reloads_the_guide(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "sync", "id": "22", "dvr_id": "32"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        called = [call.args[0] for call in post.call_args_list]
        self.assertIn("http://192.168.2.141:32400/media/grabbers/devices/22/scan", called)
        self.assertIn("http://192.168.2.141:32400/livetv/dvrs/32/reloadGuide", called)

    def test_adding_a_tuner_makes_a_dvr_with_dispatcharrs_own_guide(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="austria")
        added = {
            "MediaContainer": {
                "Device": DEVICES["MediaContainer"]["Device"]
                + [
                    {
                        "key": "40",
                        "uuid": "device://tv.plex.grabbers.hdhomerun/dispatcharr-hdhr-austria-t2",
                        "uri": "http://192.168.2.142:9191/proxy/hdhr/austria/tuners/2",
                        "title": "Austria",
                        "status": "alive",
                        "tuners": "2",
                    }
                ]
            }
        }

        in_a_dvr = {
            "MediaContainer": {
                "Dvr": [
                    {"key": "33", "lineupTitle": "austria", "Device": [{"key": "40"}]}
                ]
            }
        }

        # The server has no DVR until one is made for this tuner, and has it afterwards
        made = []

        def after_adding(url, **_kwargs):
            if "/media/grabbers/devices" in url:
                return fake_response(added)
            if "/livetv/dvrs" in url:
                return fake_response(in_a_dvr if made else {"MediaContainer": {"size": 0}})
            return plex(url)

        def making_one(url, **_kwargs):
            if url.endswith("/livetv/dvrs"):
                made.append(True)
            return fake_response({})

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = after_adding
            post.side_effect = making_one
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "channel_profile": "austria",
                    "base_url": "http://192.168.2.142:9191",
                    "tuner_count": 2,
                    "language": "nl",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        calls = {call.args[0]: call.kwargs.get("params", {}) for call in post.call_args_list}
        dvr_call = calls["http://192.168.2.141:32400/livetv/dvrs"]
        self.assertEqual(
            dvr_call["device"],
            "device://tv.plex.grabbers.hdhomerun/dispatcharr-hdhr-austria-t2",
        )
        # The guide is Dispatcharr's own EPG for that channel profile
        # The guide points at the original logos, which is what usually shows up on a media server
        self.assertEqual(
            dvr_call["lineup"],
            "lineup://tv.plex.providers.epg.xmltv/"
            "http%3A%2F%2F192.168.2.142%3A9191%2Foutput%2Fepg%2Faustria%3Fcachedlogos%3Dfalse"
            "#austria",
        )
        # "nl" is what a person types; "dut" is what the server wants
        self.assertEqual(dvr_call["language"], "dut")
        # And it is scanned and its guide loaded, so it is ready to watch
        self.assertIn("http://192.168.2.141:32400/media/grabbers/devices/40/scan", calls)

    def test_a_language_someone_typed_becomes_the_one_the_server_wants(self):
        from apps.proxy.live_proxy.media_server_tuner_views import language_code

        # French is "fre" to a media server, which nobody would guess
        for typed in ("fr", "fra", "French", "FRE", " fr "):
            self.assertEqual(language_code(typed), "fre")
        self.assertEqual(language_code("nl"), "dut")
        self.assertEqual(language_code(""), "eng")
        # Anything unrecognised is passed on as it was typed rather than replaced
        self.assertEqual(language_code("zzz"), "zzz")

    def test_the_cached_logos_can_be_asked_for_after_all(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="austria")
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_without_a_dvr
            post.return_value = fake_response({})
            self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "channel_profile": "austria",
                    "base_url": "http://192.168.2.142:9191",
                    "skip_cached_logos": False,
                },
                format="json",
            )

        dvr_call = next(
            call.kwargs["params"]
            for call in post.call_args_list
            if call.args[0].endswith("/livetv/dvrs")
        )
        self.assertNotIn("cachedlogos", dvr_call["lineup"])

    def test_a_tuner_can_be_added_into_a_dvr_that_is_already_there(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="austria")
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post, patch("apps.proxy.live_proxy.media_servers.requests.put") as put:
            get.side_effect = plex_with_tuners
            post.return_value = fake_response({})
            put.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "channel_profile": "austria",
                    "base_url": "http://192.168.2.142:9191",
                    "dvr_id": "32",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        # No DVR is made: it goes into the one that was chosen
        self.assertNotIn(
            "http://192.168.2.141:32400/livetv/dvrs",
            [call.args[0] for call in post.call_args_list],
        )
        puts = {call.args[0]: call.kwargs.get("params", {}) for call in put.call_args_list}
        self.assertTrue(
            any("/livetv/dvrs/32/devices/" in url for url in puts), puts
        )
        # A DVR holds a lineup per channel source, so this tuner's guide goes in beside them
        self.assertEqual(
            puts["http://192.168.2.141:32400/livetv/dvrs/32/lineups"]["lineup"],
            "lineup://tv.plex.providers.epg.xmltv/"
            "http%3A%2F%2F192.168.2.142%3A9191%2Foutput%2Fepg%2Faustria%3Fcachedlogos%3Dfalse"
            "#austria",
        )

    def test_a_dvr_that_could_not_be_made_is_said_so_without_losing_the_tuner(self):
        from apps.channels.models import ChannelProfile

        ChannelProfile.objects.create(name="austria")

        def refuse_the_dvr(url, **kwargs):
            if url.endswith("/livetv/dvrs"):
                raise Exception("no")
            return fake_response({})

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            # No DVR to fall into, so one has to be made and this server will not
            get.side_effect = plex_without_a_dvr
            post.side_effect = refuse_the_dvr
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "channel_profile": "austria",
                 "base_url": "http://192.168.2.142:9191"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("would not make a DVR", response.json()["warning"])

    def test_a_tuner_outside_a_dvr_says_what_to_do_instead_of_failing(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "sync", "id": "1", "dvr_id": ""},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not in a DVR yet", response.json()["error"])
        # And nothing was asked of the server, because there was nothing to ask
        post.assert_not_called()

    def test_each_tuner_shows_its_own_guide_not_the_dvrs_first_one(self):
        """
        A DVR holds a guide per channel source but does not say which is whose.

        The tuners carry no lineup and the guides carry no tuner, so they are matched on the
        channel profile both name. Without that every tuner in a DVR showed the guide it
        happened to be made with, which read as all of them sharing one.
        """
        two_in_one_dvr = {
            "MediaContainer": {
                "Dvr": [
                    {
                        "key": "44",
                        "lineupTitle": "austria",
                        "lineup": "lineup://tv.plex.providers.epg.xmltv/"
                        "http%3A%2F%2Fd%3A9191%2Foutput%2Fepg%2Faustria#austria",
                        "Device": [
                            {"key": "22", "title": "Austria"},
                            {"key": "50", "title": "Belgium"},
                        ],
                        "Lineup": [
                            {
                                "id": "lineup://tv.plex.providers.epg.xmltv/"
                                "http%3A%2F%2Fd%3A9191%2Foutput%2Fepg%2Faustria#austria",
                                "title": "austria",
                            },
                            {
                                "id": "lineup://tv.plex.providers.epg.xmltv/"
                                "http%3A%2F%2Fd%3A9191%2Foutput%2Fepg%2Fbelgium#Belgium",
                                "title": "Belgium",
                            },
                        ],
                    }
                ]
            }
        }
        devices = {
            "MediaContainer": {
                "Device": [
                    {"key": "22", "title": "Austria", "uri": "http://d:9191/hdhr/austria"},
                    {"key": "50", "title": "Belgium", "uri": "http://d:9191/hdhr/belgium"},
                ]
            }
        }

        def server_with_two(url, **_kwargs):
            if "/media/grabbers/devices" in url:
                return fake_response(devices)
            if "/livetv/dvrs" in url:
                return fake_response(two_in_one_dvr)
            return plex(url)

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = server_with_two
            austria, belgium = media_servers.tuners(
                {"id": "a1", "url": "http://192.168.2.141:32400", "token": "t"}
            )

        self.assertEqual(austria["guide"], "http://d:9191/output/epg/austria")
        self.assertEqual(belgium["guide"], "http://d:9191/output/epg/belgium")

    def test_a_registered_tuner_can_be_pointed_somewhere_else(self):
        """Moving it keeps it in its DVR, with the channels already mapped against it."""
        moved = {
            "MediaContainer": {
                "Device": [
                    {
                        "key": "22",
                        "title": "Austria",
                        "uri": "http://192.168.2.50:9191/hdhr/austria",
                    }
                ]
            }
        }

        def after_the_move(url, **_kwargs):
            if "/media/grabbers/devices" in url:
                return fake_response(moved)
            if "/livetv/dvrs" in url:
                return fake_response(DVRS)
            return plex(url)

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.put"
        ) as put:
            get.side_effect = after_the_move
            put.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "action": "set_uri",
                    "id": "22",
                    "uri": "http://192.168.2.50:9191/hdhr/austria",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        call = next(
            call for call in put.call_args_list
            if call.args[0] == "http://192.168.2.141:32400/media/grabbers/devices/22"
        )
        self.assertEqual(
            call.kwargs["params"]["uri"], "http://192.168.2.50:9191/hdhr/austria"
        )

    def test_a_server_that_keeps_the_old_address_has_a_tuner_put_there_instead(self):
        """
        Plex takes the request and keeps the address it had, whatever it is asked.

        Measured, not assumed: the address is read back, and when it has not moved the
        change is made the only way that server allows, which is the way its own settings
        do it. A tuner at the new address, named, its guide added, attached where the old
        one was, and only then the old one removed.
        """
        after = {
            "MediaContainer": {
                "Device": DEVICES["MediaContainer"]["Device"]
                + [
                    {
                        "key": "77",
                        "uuid": "device://tv.plex.grabbers.hdhomerun/moved",
                        "uri": "http://192.168.2.142:9191/proxy/hdhr/austria/tuners/4",
                        "title": "Austria",
                        "status": "alive",
                    }
                ]
            }
        }

        def keeps_the_old_address(url, **_kwargs):
            if "/media/grabbers/devices" in url:
                return fake_response(after)
            if "/livetv/dvrs" in url:
                return fake_response(DVRS)
            return plex(url)

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post, patch("apps.proxy.live_proxy.media_servers.requests.put") as put, patch(
            "apps.proxy.live_proxy.media_servers.requests.delete"
        ) as delete, patch("apps.proxy.live_proxy.media_servers._settle"):
            get.side_effect = keeps_the_old_address
            post.return_value = fake_response({})
            put.return_value = fake_response({})
            delete.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "action": "set_uri",
                    "id": "22",
                    "uri": "http://192.168.2.142:9191/proxy/hdhr/austria/tuners/4",
                    "base_url": "http://192.168.2.142:9191",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        added = [call.args[0] for call in post.call_args_list]
        self.assertTrue(
            any("/media/grabbers/devices" in url for url in added), added
        )
        # Its guide went in and it was attached where the old one was
        put_urls = [call.args[0] for call in put.call_args_list]
        self.assertIn("http://192.168.2.141:32400/livetv/dvrs/32/lineups", put_urls)
        self.assertIn("http://192.168.2.141:32400/livetv/dvrs/32/devices/77", put_urls)
        # And only then the old one removed
        self.assertEqual(
            delete.call_args.args[0],
            "http://192.168.2.141:32400/media/grabbers/devices/22",
        )

    def test_a_second_move_is_refused_while_one_is_running(self):
        """
        Measured the hard way: a server told to do this twice at once crashed outright.

        It took its own rollback with it, leaving a tuner registered and in nothing. One at
        a time, and each step waits, because a server changing its tuners is fragile.
        """
        redis = FakeRedis()
        server = {"id": "a1", "url": "http://192.168.2.141:32400", "token": "t"}
        device = {"id": "22", "title": "Austria", "dvr_id": "32", "uuid": "old"}

        def while_the_first_is_running(*_args, **_kwargs):
            moved, why = media_servers.move_tuner(
                server, device, "http://second", redis_client=redis
            )
            self.assertFalse(moved)
            self.assertIn("Another tuner is being moved", why)
            return False  # and the first one gives up, having changed nothing

        with patch("apps.proxy.live_proxy.media_servers.add_tuner") as add:
            add.side_effect = while_the_first_is_running
            media_servers.move_tuner(
                server, device, "http://first", redis_client=redis
            )

        # And once it is over, the next one may go ahead
        with patch("apps.proxy.live_proxy.media_servers.add_tuner") as add:
            add.return_value = False
            _moved, why = media_servers.move_tuner(
                server, device, "http://third", redis_client=redis
            )
        self.assertNotIn("Another tuner is being moved", why)

    def test_a_moved_tuner_is_not_scanned_on_the_way(self):
        """A tuner scanned the moment it arrives answers 500, and the server is already busy."""
        server = {"id": "a1", "url": "http://192.168.2.141:32400", "token": "t"}
        device = {"id": "22", "title": "Austria", "dvr_id": "32", "uuid": "old"}
        with patch("apps.proxy.live_proxy.media_servers.add_tuner") as add, patch(
            "apps.proxy.live_proxy.media_servers.tuners"
        ) as listed, patch("apps.proxy.live_proxy.media_servers.name_device"), patch(
            "apps.proxy.live_proxy.media_servers.add_lineup"
        ), patch("apps.proxy.live_proxy.media_servers.attach_tuner") as attach, patch(
            "apps.proxy.live_proxy.media_servers.delete_tuner"
        ) as remove, patch(
            "apps.proxy.live_proxy.media_servers.sync_tuner"
        ) as scan, patch("apps.proxy.live_proxy.media_servers._settle"):
            add.return_value = True
            listed.return_value = [{"id": "77", "uri": "http://new"}]
            attach.return_value = True
            remove.return_value = True
            moved, why = media_servers.move_tuner(server, device, "http://new")

        self.assertTrue(moved)
        scan.assert_not_called()
        self.assertIn("Press Sync", why)

    def test_a_tuner_that_could_not_be_attached_does_not_stay_behind(self):
        """The server goes back as it was rather than keeping a tuner in no DVR."""
        server = {"id": "a1", "url": "http://192.168.2.141:32400", "token": "t"}
        device = {"id": "22", "title": "Austria", "dvr_id": "32", "uuid": "old"}
        with patch("apps.proxy.live_proxy.media_servers.add_tuner") as add, patch(
            "apps.proxy.live_proxy.media_servers.tuners"
        ) as listed, patch(
            "apps.proxy.live_proxy.media_servers.name_device"
        ), patch("apps.proxy.live_proxy.media_servers.add_lineup"), patch(
            "apps.proxy.live_proxy.media_servers.attach_tuner"
        ) as attach, patch(
            "apps.proxy.live_proxy.media_servers.delete_tuner"
        ) as remove, patch("apps.proxy.live_proxy.media_servers._settle"):
            add.return_value = True
            listed.return_value = [{"id": "77", "uri": "http://new"}]
            attach.return_value = False
            moved, why = media_servers.move_tuner(server, device, "http://new")

        self.assertFalse(moved)
        self.assertIn("back in its DVR", why)
        # The one that could not be used is gone; the original is untouched
        remove.assert_called_once_with(server, "77")

    def test_a_tuner_address_has_to_be_one(self):
        response = self.client_api.post(
            "/proxy/media-servers/tuners/",
            {"server": "a1", "action": "set_uri", "id": "22", "uri": "hdhr/austria"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("http://", response.json()["error"])

    def test_a_tuner_can_be_put_into_a_dvr(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.put"
        ) as put:
            get.side_effect = plex_with_tuners
            put.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "attach", "id": "1", "dvr_id": "32"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        called = [call.args[0] for call in put.call_args_list]
        self.assertIn("http://192.168.2.141:32400/livetv/dvrs/32/devices/1", called)
        # The DVRs are offered by name, so there is something to choose
        (dvr,) = response.json()["dvrs"]
        self.assertEqual(dvr["id"], "32")
        self.assertEqual(dvr["title"], "DVR")
        self.assertEqual(dvr["tuners"], ["Austria"])

    def test_a_tuner_of_ours_put_into_a_dvr_takes_its_guide_with_it(self):
        """
        A DVR holds a lineup per channel source, and this is the order the server uses.

        Measured from a server's own log while adding a channel source through its settings:
        the guide goes in with PUT /livetv/dvrs/{id}/lineups, the tuner is named and switched
        on, and only then is it put into the DVR.
        """
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.put"
        ) as put:
            get.side_effect = plex_with_tuners
            put.return_value = fake_response({})
            # Tuner 22 is ours: http://192.168.2.142:9191/hdhr/austria
            self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "attach", "id": "22", "dvr_id": "32"},
                format="json",
            )

        called = [call.args[0] for call in put.call_args_list]
        self.assertEqual(
            called,
            [
                "http://192.168.2.141:32400/livetv/dvrs/32/lineups",
                "http://192.168.2.141:32400/media/grabbers/devices/22",
                "http://192.168.2.141:32400/livetv/dvrs/32/devices/22",
            ],
        )
        puts = {call.args[0]: call.kwargs.get("params", {}) for call in put.call_args_list}
        self.assertIn(
            "austria", puts["http://192.168.2.141:32400/livetv/dvrs/32/lineups"]["lineup"]
        )
        # Named and switched on, or the server shows a blank row it will not use
        self.assertEqual(
            puts["http://192.168.2.141:32400/media/grabbers/devices/22"]["enabled"], 1
        )

    def test_a_tuner_goes_into_the_dvr_that_is_there(self):
        """
        There is nothing to choose. A server takes more DVRs through its API than its
        settings show, and a tuner alone in one of the extra ones is registered, invisible
        and unwatchable, so a tuner joins the DVR that exists.
        """
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post, patch("apps.proxy.live_proxy.media_servers.requests.put") as put:
            get.side_effect = plex_with_a_spare_tuner
            post.return_value = fake_response({})
            put.return_value = fake_response({})
            # Tuner 40 is ours and in no DVR; DVR 32 is the one the server has
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "place", "id": "40"},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        # No DVR was made: it went into the one that was there
        self.assertNotIn(
            "http://192.168.2.141:32400/livetv/dvrs",
            [call.args[0] for call in post.call_args_list],
        )
        called = [call.args[0] for call in put.call_args_list]
        # Its guide first, then named and switched on, then put in
        self.assertIn("http://192.168.2.141:32400/livetv/dvrs/32/lineups", called)
        self.assertIn("http://192.168.2.141:32400/media/grabbers/devices/40", called)
        self.assertIn("http://192.168.2.141:32400/livetv/dvrs/32/devices/40", called)

    def test_a_dvr_is_made_only_when_the_server_has_none(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post, patch("apps.proxy.live_proxy.media_servers.requests.put") as put:
            get.side_effect = plex_with_a_spare_tuner_and_no_dvr
            post.return_value = fake_response({})
            put.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "place", "id": "40"},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        made = next(
            call for call in post.call_args_list if call.args[0].endswith("/livetv/dvrs")
        )
        # With the guide for the channels that tuner serves
        self.assertIn("austria", made.kwargs["params"]["lineup"])

    def test_a_tuner_that_is_not_ours_has_no_guide_to_go_with_it(self):
        """Its channels are not Dispatcharr's, so there is nothing to list them from."""
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = plex_with_tuners
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {"server": "a1", "action": "place", "id": "1"},
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not one of Dispatcharr's", response.json()["error"])
        post.assert_not_called()

    def test_the_guide_on_a_dvr_can_be_changed(self):
        """Its tuners go back with it, or the server takes it as a DVR emptied of them."""
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.put"
        ) as put:
            get.side_effect = plex_with_tuners
            put.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "action": "set_guide",
                    "dvr_id": "32",
                    "guide_url": "http://192.168.2.142:9191/output/epg/france?cachedlogos=false",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        call = next(
            call for call in put.call_args_list
            if call.args[0] == "http://192.168.2.141:32400/livetv/dvrs/32"
        )
        self.assertIn("france", call.kwargs["params"]["lineup"])
        self.assertIn("device", call.kwargs["params"])

    def test_a_guide_address_has_to_be_one(self):
        response = self.client_api.post(
            "/proxy/media-servers/tuners/",
            {"server": "a1", "action": "set_guide", "dvr_id": "32", "guide_url": "epg/france"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("http://", response.json()["error"])

    def test_putting_a_tuner_in_a_dvr_needs_a_dvr(self):
        response = self.client_api.post(
            "/proxy/media-servers/tuners/",
            {"server": "a1", "action": "attach", "id": "1"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a DVR", response.json()["error"])

    def test_a_dvr_is_removed(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.delete"
        ) as delete:
            get.side_effect = plex_with_tuners
            delete.return_value = fake_response({})
            response = self.client_api.delete("/proxy/media-servers/tuners/?server=a1&dvr=32")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(delete.call_args.args[0], "http://192.168.2.141:32400/livetv/dvrs/32")

    def test_the_dvrs_say_what_is_in_them(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex_with_tuners
            data = self.client_api.get("/proxy/media-servers/tuners/?server=a1").json()

        (dvr,) = data["dvrs"]
        self.assertEqual(dvr["title"], "DVR")
        self.assertEqual(dvr["tuners"], ["Austria"])

    def test_a_tuner_is_removed(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.delete"
        ) as delete:
            get.side_effect = plex_with_tuners
            delete.return_value = fake_response({})
            response = self.client_api.delete("/proxy/media-servers/tuners/?server=a1&id=1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/media/grabbers/devices/1", delete.call_args.args[0])

    def test_a_server_that_is_switched_off_is_left_alone(self):
        media_servers.save_servers(
            [{"id": "a1", "name": "Plex", "url": "http://p:32400", "token": "t", "enabled": False}]
        )
        response = self.client_api.get("/proxy/media-servers/tuners/?server=a1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("switched off", response.json()["error"])

    def test_only_admins(self):
        viewer = APIClient()
        viewer.force_authenticate(
            user=User.objects.create_user(username="viewer2", password="x", user_level=0)
        )
        self.assertEqual(viewer.get("/proxy/media-servers/tuners/?server=a1").status_code, 403)


class OverlapForEitherServerTests(TestCase):
    """
    The whole chain from a server's sessions to a viewer the overlap can use.

    The tests below work from a cache written by hand, so an asymmetry between how the two
    kinds of server are read would not show up in them. This runs the real thing: ask the
    server what it is playing, cache it, and see whether a request arriving from it can be
    told apart. It has to work the same either way or the overlap is Plex-only.
    """

    def setUp(self):
        self.redis = FakeRedis()
        media_servers.forget_hosts()
        self.addCleanup(media_servers.forget_hosts)

    def _cache_from(self, server, answer):
        media_servers.save_servers([server])
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = answer
            media_servers.refresh_sessions(self.redis)

    def test_a_jellyfin_viewer_can_be_told_apart(self):
        self._cache_from(
            {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
             "kind": "jellyfin"},
            jellyfin,
        )

        live = [s for s in media_servers.cached_sessions(self.redis) if s["live"]]
        # Only the live channel counts: the film on the same server is not our business
        self.assertEqual([s["title"] for s in live], ["ORF 1"])
        self.assertEqual(live[0]["user"], "Chris")
        # And with one device on a live channel, a request from it belongs to that device
        self.assertEqual(media_servers.sole_device(self.redis), "shield-1")

    def test_a_jellyfin_viewer_watching_a_programme_is_still_watching_live_tv(self):
        """
        Started from the guide, the session is the programme with its channel beside it.

        Only counting items of type TvChannel leaves those viewers out of the overlap, which
        is the same as not being able to tell them apart at all.
        """
        from_the_guide = [
            {
                "Id": "s3",
                "UserName": "Chris",
                "DeviceId": "shield-1",
                "NowPlayingItem": {
                    "Name": "Zeit im Bild",
                    "Type": "Program",
                    "ChannelId": "abc-123",
                },
                "PlayState": {},
            }
        ]

        def jellyfin_from_the_guide(url, **kwargs):
            if "/Sessions" in url:
                return fake_response(from_the_guide)
            return jellyfin(url, **kwargs)

        self._cache_from(
            {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
             "kind": "jellyfin"},
            jellyfin_from_the_guide,
        )

        (session,) = media_servers.cached_sessions(self.redis)
        self.assertTrue(session["live"])
        self.assertEqual(session["watching"], "live TV")
        self.assertEqual(media_servers.sole_device(self.redis), "shield-1")

    def test_a_plex_viewer_can_be_told_apart(self):
        self._cache_from(
            {"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"},
            plex,
        )

        live = [s for s in media_servers.cached_sessions(self.redis) if s["live"]]
        self.assertTrue(live, media_servers.cached_sessions(self.redis))
        self.assertIsNotNone(media_servers.sole_device(self.redis))

    def test_both_kinds_say_who_is_watching(self):
        """The page lists people from this, so a server that reports nobody shows nobody."""
        for server, answer in (
            ({"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"}, plex),
            ({"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
              "kind": "jellyfin"}, jellyfin),
        ):
            with self.subTest(server=server["name"]):
                self._cache_from(server, answer)
                watching = media_servers.cached_sessions(self.redis)
                self.assertTrue(watching, f"{server['name']} reported nobody")
                self.assertTrue(
                    all(session.get("user") for session in watching),
                    f"{server['name']} did not say who: {watching}",
                )


class BindingOnEitherServerTests(TestCase):
    """
    The device is bound when the server names it, which is what the overlap really runs on.

    Everything tried while the request is in flight is a guess: the server is asked for the
    stream before it has a session to report. This is the part that is certain, so it has to
    work on both kinds of server or the overlap is Plex-only in the one way that matters.
    """

    def setUp(self):
        self.redis = FakeRedis()
        media_servers.forget_hosts()
        self.addCleanup(media_servers.forget_hosts)

    def test_plex_names_the_device_and_it_is_bound(self):
        server = {"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"}
        # Plex says when the session began, so the start it belongs to is the one at that
        # moment: matched on time, which is what it gives us
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex
            session = media_servers._session_for(server, 1789580681)

        self.assertIsNotNone(session, "Plex named no session for this start")
        media_servers.bind_device(self.redis, "channel-a", session)
        self.assertEqual(
            media_servers._as_str(
                self.redis.get(
                    media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid="channel-a")
                )
            ),
            f"server|{session['device_id']}",
        )

    def test_jellyfin_names_the_device_and_it_is_bound(self):
        """
        Jellyfin says when a session was last active, not when it began.

        Matched on that time, a stream that started an hour ago looks as new as this one,
        and a session whose last activity has not been refreshed looks like neither. How far
        into the stream the player is says it properly.
        """
        server = {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
                  "kind": "jellyfin"}
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = jellyfin
            session = media_servers._session_for(server, time.time())

        self.assertIsNotNone(session, "Jellyfin named no session for this start")
        self.assertEqual(session["device_id"], "shield-1")
        media_servers.bind_device(self.redis, "channel-a", session)
        self.assertEqual(
            media_servers._as_str(
                self.redis.get(
                    media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid="channel-a")
                )
            ),
            "server|shield-1",
        )

    def test_the_session_playing_our_channel_is_the_one_whatever_the_clocks_say(self):
        """
        What it is playing is a fact about the session; when it started is a guess.

        The times come from two clocks that do not agree, and mean different things on the
        two servers. The channel does not: Dispatcharr knows which one it handed over, and
        the server says which one each session is on.
        """
        two_watching = [
            {
                "Id": "s1",
                "UserName": "Someone else",
                "DeviceId": "living-room",
                "NowPlayingItem": {"Name": "ORF 1", "Type": "TvChannel"},
                "PlayState": {"PositionTicks": 0},
            },
            {
                "Id": "s2",
                "UserName": "Chris",
                "DeviceId": "shield-1",
                "NowPlayingItem": {"Name": "TFX", "Type": "TvChannel"},
                "PlayState": {"PositionTicks": 0},
            },
        ]
        server = {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
                  "kind": "jellyfin"}
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = lambda url, **kwargs: (
                fake_response(two_watching) if "/Sessions" in url else jellyfin(url, **kwargs)
            )
            # Both look equally new; only one is on the channel that was handed over
            session = media_servers._session_for(server, time.time(), "┃FR┃ TFX")

        self.assertIsNotNone(session)
        self.assertEqual(session["device_id"], "shield-1")

    def test_the_guide_says_what_is_on_a_channel_now(self):
        """What makes the programme usable: our own guide, which is where Plex got it."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.channels.models import Channel
        from apps.epg.models import EPGData, ProgramData

        epg = EPGData.objects.create(tvg_id="tfx", name="TFX")
        channel = Channel.objects.create(
            channel_number=1, name="┃FR┃ TFX", epg_data=epg
        )
        now = timezone.now()
        ProgramData.objects.create(
            epg=epg,
            title="Over already",
            start_time=now - timedelta(hours=2),
            end_time=now - timedelta(hours=1),
        )
        ProgramData.objects.create(
            epg=epg,
            title="Le banquet",
            start_time=now - timedelta(minutes=10),
            end_time=now + timedelta(minutes=50),
        )

        self.assertEqual(media_servers.programme_now(channel.uuid), "Le banquet")

    def test_a_channel_with_no_guide_simply_has_no_programme(self):
        """A fallback lost, not a failure: the times still place the session."""
        from apps.channels.models import Channel

        channel = Channel.objects.create(channel_number=2, name="No guide")
        self.assertEqual(media_servers.programme_now(channel.uuid), "")
        self.assertEqual(media_servers.programme_now("not-a-channel"), "")

    def test_plex_names_the_programme_and_the_guide_says_which_channel_that_is(self):
        """
        Taken from a real Plex live session: it does not say which channel it is on.

        The title is the programme, the type is what the programme is, and the fields that
        would carry a channel are empty. The programme name came from Dispatcharr's own
        guide, so the guide answers what the session does not.
        """
        as_plex_really_reports_it = {
            "MediaContainer": {
                "size": 1,
                "Metadata": [
                    {
                        "title": "Le banquet",
                        "type": "movie",
                        "live": "1",
                        "guid": "tv.plex.xmltv://movie/Le%20banquet",
                        "addedAt": 1789711777,
                        "User": {"title": "Ckegels"},
                        "Player": {
                            "title": "Chrome",
                            "machineIdentifier": "mkk9dgqsm9p8jxbatwh6373h",
                            "state": "buffering",
                        },
                    }
                ],
            }
        }
        server = {"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"}
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = lambda url, **kwargs: (
                fake_response(as_plex_really_reports_it)
                if "/status/sessions" in url
                else plex(url, **kwargs)
            )
            (session,) = media_servers.sessions(server)
            self.assertEqual(session["channel"], "", "Plex named a channel after all")

            # Matched on the programme our guide says is on the channel we handed over,
            # with the clock hours out to show it is not what decided this
            found = media_servers._session_for(
                server, 1, channel_name="┃FR┃ TFX", programme_name="Le banquet"
            )

        self.assertIsNotNone(found, "the programme did not place the session")
        self.assertEqual(found["device_id"], "mkk9dgqsm9p8jxbatwh6373h")

    def test_channel_names_are_compared_on_what_they_say_not_how_they_are_written(self):
        """Dispatcharr's names carry decoration a server drops, and a server may shorten."""
        self.assertTrue(media_servers._same_channel("TFX", "┃FR┃ TFX"))
        self.assertTrue(media_servers._same_channel("ORF 1 HD", "ORF1HD"))
        self.assertTrue(media_servers._same_channel("┃AT┃ ORF 1", "ORF 1"))
        self.assertTrue(media_servers._same_channel("[BE] Eén", "Eén"))
        # Not so loose that anything matches anything
        self.assertFalse(media_servers._same_channel("BBC One", "TFX"))
        self.assertFalse(media_servers._same_channel("", "TFX"))
        # A different channel whose name this one is the beginning of
        self.assertFalse(media_servers._same_channel("TF1", "TF1 Series Films"))

    def test_two_on_the_same_channel_cannot_be_told_apart_by_it(self):
        """With two there is no telling which asked, so it falls back rather than guessing."""
        both_on_it = [
            {
                "Id": f"s{number}",
                "DeviceId": device,
                "NowPlayingItem": {"Name": "TFX", "Type": "TvChannel"},
                "PlayState": {"PositionTicks": 3600 * 10_000_000},
            }
            for number, device in enumerate(("living-room", "shield-1"))
        ]
        server = {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
                  "kind": "jellyfin"}
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = lambda url, **kwargs: (
                fake_response(both_on_it) if "/Sessions" in url else jellyfin(url, **kwargs)
            )
            # Both are an hour in, so the fallback does not claim either
            self.assertIsNone(media_servers._session_for(server, time.time(), "TFX"))

    def test_a_jellyfin_session_already_well_into_a_stream_is_not_this_start(self):
        """Otherwise a channel someone else has been watching is bound to this one."""
        long_running = [
            {
                "Id": "s9",
                "UserName": "Someone else",
                "DeviceId": "living-room",
                "NowPlayingItem": {"Name": "ORF 1", "Type": "TvChannel"},
                # An hour in, and its last activity is a moment ago because it is playing
                "PlayState": {"PositionTicks": 3600 * 10_000_000},
                "LastActivityDate": "2026-09-17T20:15:00.0000000Z",
            }
        ]
        server = {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
                  "kind": "jellyfin"}
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = lambda url, **kwargs: (
                fake_response(long_running) if "/Sessions" in url else jellyfin(url, **kwargs)
            )
            self.assertIsNone(media_servers._session_for(server, time.time()))

    def test_binding_settles_the_overlap_on_either_server(self):
        """The binding is what stops the channel the viewer left; it is not just a name."""
        session = {"device_id": "shield-1", "user": "Chris", "player": "Shield"}
        with patch(
            "apps.proxy.live_proxy.probation.settle_media_server_start"
        ) as settle:
            media_servers.bind_device(self.redis, "channel-a", session)
            settle.assert_not_called()  # nothing to settle: it was on no channel before

            media_servers.bind_device(self.redis, "channel-b", session)
            settle.assert_called_once()
            self.assertEqual(settle.call_args.args[1], "channel-b")
            self.assertEqual(settle.call_args.args[2], "server|shield-1")
            self.assertEqual(settle.call_args.args[3], "channel-a")


class RecordingsAndGuidesTests(TestCase):
    """Two things that need the server asked rather than assumed."""

    def setUp(self):
        self.redis = FakeRedis()
        media_servers.forget_hosts()
        self.addCleanup(media_servers.forget_hosts)

    def test_jellyfin_says_what_it_is_recording(self):
        timers = [
            {"Status": "InProgress", "ChannelName": "TFX"},
            {"Status": "New", "ChannelName": "Not started yet"},
            {"Status": "Completed", "ChannelName": "Over"},
        ]
        server = {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
                  "kind": "jellyfin"}
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = lambda url, **kwargs: (
                fake_response(timers) if "/LiveTv/Timers" in url else jellyfin(url, **kwargs)
            )
            self.assertEqual(media_servers.recording_channels(server), {"TFX"})

    def test_plex_recordings_are_not_known_and_it_does_not_pretend(self):
        """No endpoint for this is known to work there, so nothing is claimed."""
        server = {"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"}
        self.assertEqual(media_servers.recording_channels(server), set())

    def test_what_is_being_recorded_is_asked_once_and_kept(self):
        """A stop is decided while a viewer waits; a slow server must not hold that up."""
        media_servers.save_servers(
            [{"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
              "kind": "jellyfin"}]
        )
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = lambda url, **kwargs: (
                fake_response([{"Status": "InProgress", "ChannelName": "TFX"}])
                if "/LiveTv/Timers" in url else jellyfin(url, **kwargs)
            )
            self.assertEqual(media_servers.channels_being_recorded(self.redis), {"TFX"})
            asked = get.call_count
            self.assertEqual(media_servers.channels_being_recorded(self.redis), {"TFX"})
            self.assertEqual(get.call_count, asked, "it asked the server twice")

    def test_every_guide_is_reloaded_when_ours_changes(self):
        """A server looks at ours on its own schedule, which is hours."""
        media_servers.save_servers([
            {"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"},
            {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
             "kind": "jellyfin"},
        ])
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = lambda url, **kwargs: (
                jellyfin(url, **kwargs) if ":8096" in url else plex_with_tuners(url, **kwargs)
            )
            post.return_value = fake_response({})
            media_servers.reload_every_guide()

        called = [call.args[0] for call in post.call_args_list]
        self.assertIn("http://plex:32400/livetv/dvrs/32/reloadGuide", called)
        self.assertTrue(
            any("/ScheduledTasks/Running/" in url for url in called),
            f"Jellyfin was not asked to refresh: {called}",
        )

    def test_a_server_that_is_off_is_not_asked(self):
        media_servers.save_servers(
            [{"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t",
              "enabled": False}]
        )
        with patch("apps.proxy.live_proxy.media_servers.requests.post") as post:
            media_servers.reload_every_guide()
        post.assert_not_called()

    def test_a_session_can_be_stopped(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.return_value = fake_response({})
            self.assertTrue(
                media_servers.stop_session(
                    {"id": "a1", "url": "http://plex:32400", "token": "t"}, "abc"
                )
            )
        self.assertIn("/status/sessions/terminate", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["params"]["sessionId"], "abc")

    def test_a_jellyfin_session_can_be_stopped(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.post") as post:
            post.return_value = fake_response({})
            self.assertTrue(
                media_servers.stop_session(
                    {"id": "j1", "url": "http://jf:8096", "token": "t", "kind": "jellyfin"},
                    "abc",
                )
            )
        self.assertEqual(post.call_args.args[0], "http://jf:8096/Sessions/abc/Playing/Stop")


class SoleDeviceTests(TestCase):
    """A media server asks on behalf of its viewers; sometimes it can say which one."""

    def setUp(self):
        self.redis = FakeRedis()
        media_servers.save_servers(
            [{"id": "a1", "name": "Plex", "url": "http://plex:32400", "token": "t"}]
        )

    def _sessions(self, *devices):
        import json

        self.redis.setex(
            media_servers.SESSIONS_KEY,
            10,
            json.dumps([{"device_id": d, "live": True} for d in devices]),
        )

    def test_one_device_streaming_is_that_device(self):
        self._sessions("apple-tv")
        self.assertEqual(media_servers.sole_device(self.redis), "apple-tv")

    def test_several_devices_cannot_be_told_apart(self):
        self._sessions("apple-tv", "living-room")
        self.assertIsNone(media_servers.sole_device(self.redis))

    def test_it_waits_a_moment_for_the_server_to_say_who_is_asking(self):
        """
        The server registers what it is playing after it has asked for the stream.

        Serving a viewer that cannot be told apart is worse than being a little slower: the
        overlap does not apply and the channel they just left keeps its slot.
        """
        answers = [[], [], [{"device_id": "shield-1", "live": True}]]

        def answering_late(_url, **_kwargs):
            return fake_response(answers.pop(0) if answers else [])

        media_servers.save_servers(
            [{"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
              "kind": "jellyfin"}]
        )
        with patch(
            "apps.proxy.live_proxy.media_servers._jellyfin_sessions"
        ) as jellyfin_sessions, patch(
            "apps.proxy.live_proxy.media_servers.gevent.sleep"
        ) as sleep:
            jellyfin_sessions.side_effect = lambda _server: (
                answers.pop(0) if answers else []
            )
            self.assertEqual(media_servers.wait_for_device(self.redis), "shield-1")
        # It waited rather than answering at once, and did not wait long
        self.assertLessEqual(sleep.call_count, 3)

    def test_it_gives_up_rather_than_holding_the_stream(self):
        """A server that is never going to answer must not hold a stream open forever."""
        media_servers.save_servers(
            [{"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
              "kind": "jellyfin"}]
        )
        with patch(
            "apps.proxy.live_proxy.media_servers._jellyfin_sessions"
        ) as jellyfin_sessions, patch(
            "apps.proxy.live_proxy.media_servers.gevent.sleep"
        ):
            jellyfin_sessions.return_value = []
            self.assertIsNone(media_servers.wait_for_device(self.redis, seconds=0))

    def test_whoever_was_watching_a_moment_ago_when_nobody_is_now(self):
        """
        The moment a channel starts, the server has no session to report yet.

        It is asked for the stream first and registers what it is playing after, so there is
        a window where it can say nothing about who is asking. Treating that as an unknown
        viewer costs them the overlap and leaves their old channel running, which is the
        "it works most of the time" this is for.
        """
        self.redis.hset(
            media_servers.RECENT_DEVICES_KEY, "shield-1", str(time.time() - 5)
        )
        self._sessions()  # nothing playing this instant

        self.assertIsNone(media_servers.sole_device(self.redis))
        self.assertEqual(media_servers.device_a_moment_ago(self.redis), "shield-1")

    def test_two_watching_a_moment_ago_is_still_no_answer(self):
        """Same rule as sole_device: with two there is no telling which of them this is."""
        for device in ("shield-1", "living-room"):
            self.redis.hset(
                media_servers.RECENT_DEVICES_KEY, device, str(time.time() - 5)
            )
        self.assertIsNone(media_servers.device_a_moment_ago(self.redis))

    def test_watching_long_enough_ago_does_not_count(self):
        self.redis.hset(
            media_servers.RECENT_DEVICES_KEY,
            "shield-1",
            str(time.time() - media_servers.RECENT_DEVICE_SECONDS - 60),
        )
        self.assertIsNone(media_servers.device_a_moment_ago(self.redis))

    def test_nothing_playing_and_no_cache_at_all(self):
        self._sessions()
        self.assertIsNone(media_servers.sole_device(self.redis))
        self.redis.delete(media_servers.SESSIONS_KEY)
        self.assertIsNone(media_servers.sole_device(self.redis))

    def test_the_sessions_are_refreshed_in_the_background_and_not_too_often(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex
            media_servers.refresh_sessions(self.redis)
            first = get.call_count
            self.assertGreater(first, 0)
            # Called again straight away it does nothing: the cleanup loop runs every second
            media_servers.refresh_sessions(self.redis)
            self.assertEqual(get.call_count, first)

        self.assertEqual(
            [s["user"] for s in media_servers.cached_sessions(self.redis)], ["Ckegels"]
        )

    def test_a_media_server_that_is_off_is_not_asked(self):
        media_servers.save_servers(
            [{"id": "a1", "url": "http://plex:32400", "token": "t", "enabled": False}]
        )
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            media_servers.refresh_sessions(self.redis)
            get.assert_not_called()


class MediaServerViewerTests(TestCase):
    """What a media server tells us makes its viewer one Dispatcharr can tell apart."""

    def setUp(self):
        _reset_in_use_cache(self)
        self.redis = FakeRedis()
        self.account, _profile = _make_account("plex", probation_enabled=True)
        media_servers.forget_hosts()
        self.addCleanup(media_servers.forget_hosts)
        media_servers.save_servers(
            [{"id": "a1", "url": "http://192.168.2.141:32400", "token": "t"}]
        )

    def _request(self, user_agent="Lavf/61.7.100", ip="192.168.2.141"):
        from django.test import RequestFactory

        from apps.proxy.live_proxy import probation

        request = RequestFactory().get("/proxy/ts/stream/x", HTTP_USER_AGENT=user_agent)
        return probation.viewer_from_request(request, None, ip, self.redis)

    def _sessions(self, *devices):
        import json

        self.redis.setex(
            media_servers.SESSIONS_KEY,
            10,
            json.dumps([{"device_id": d, "live": True} for d in devices]),
        )

    def test_the_only_device_watching_is_recognised(self):
        from apps.proxy.live_proxy import probation

        self._sessions("apple-tv")
        viewer = self._request()

        self.assertEqual(viewer.server_device, "server|apple-tv")
        self.assertTrue(probation.is_identified(viewer, self.account))
        self.assertEqual(probation.identity_key(viewer, self.account), "server|apple-tv")

    def test_several_devices_stay_anonymous(self):
        from apps.proxy.live_proxy import probation

        self._sessions("apple-tv", "living-room")
        viewer = self._request()

        self.assertIsNone(viewer.server_device)
        self.assertFalse(probation.is_identified(viewer, self.account))

    def test_an_ordinary_player_is_never_given_a_device(self):
        viewer = self._request(user_agent="TiviMate/5.1.6", ip="192.168.2.50")
        self.assertIsNone(viewer.server_device)

    def test_the_device_survives_as_the_channel_s_viewer(self):
        from apps.proxy.live_proxy import probation

        self._sessions("apple-tv")
        viewer = self._request()
        probation.record_client_viewer(self.redis, "channel-1", "c1", viewer)

        (member,) = self.redis.smembers(
            probation.CHANNEL_VIEWERS_KEY.format(channel_uuid="channel-1")
        )
        back = probation._viewer_from_member(member)
        self.assertEqual(back.server_device, "server|apple-tv")
        self.assertEqual(
            probation.identity_key(back, self.account),
            probation.identity_key(viewer, self.account),
        )


@patch("django.db.close_old_connections")
class WhoIsWatchingTests(TestCase):
    """The server says which device is watching which channel, a moment after it starts."""

    def setUp(self):
        self.redis = FakeRedis()
        media_servers.save_servers(
            [{"id": "a1", "url": "http://plex:32400", "token": "t"}]
        )
        self.redis.hset(
            timing.START_KEY.format(start_id="s1"), mapping={"time": "1", "channel": "ZIB"}
        )

    def _sessions(self, *devices):
        import json

        self.redis.setex(
            media_servers.SESSIONS_KEY,
            10,
            json.dumps([{"device_id": d, "live": True} for d in devices]),
        )

    def _channel_is_live(self, channel_uuid):
        from apps.proxy.live_proxy.redis_keys import RedisKeys

        self.redis.hset(RedisKeys.channel_metadata(channel_uuid), "state", "active")

    def test_the_channel_learns_who_is_watching_it(self, _close):
        from apps.proxy.live_proxy import probation
        from apps.proxy.live_proxy.redis_keys import RedisKeys

        self._channel_is_live("channel-1")
        self.redis.sadd(RedisKeys.clients("channel-1"), "c1")
        self.redis.hset(
            RedisKeys.client_metadata("channel-1", "c1"),
            mapping={"ip_address": "192.168.2.141", "user_id": "0"},
        )

        # A live session (no playback position), so the watching ends as soon as it plays
        live = {
            "MediaContainer": {
                "Metadata": [
                    {
                        k: v
                        for k, v in SESSION["MediaContainer"]["Metadata"][0].items()
                        if k != "viewOffset"
                    }
                ]
            }
        }
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch.object(
            media_servers.gevent, "sleep"
        ):
            get.return_value = fake_response(live)
            media_servers._watch(self.redis, "s1", 1789580681.0, channel_uuid="channel-1")

        self.assertEqual(
            self.redis.get(
                media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid="channel-1")
            ),
            "server|mkk9dgqsm9p8",
        )
        # And the client on that channel is that viewer, so holds and Stop Skipped apply
        client = self.redis.hgetall(RedisKeys.client_metadata("channel-1", "c1"))
        self.assertEqual(client["server_device"], "server|mkk9dgqsm9p8")
        # And the page can say who that is, instead of the media server's address
        self.assertEqual(
            media_servers.device_name(self.redis, "server|mkk9dgqsm9p8"),
            "Ckegels · Chrome (Plex)",
        )
        viewer = probation._client_viewer(client)
        self.assertEqual(viewer.server_device, "server|mkk9dgqsm9p8")

    def test_the_device_whose_channel_stopped_is_the_one_switching(self, _close):
        self._sessions("apple-tv", "living-room")
        # Both were watching; the living room one has stopped
        self.redis.setex(
            media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid="channel-1"), 60,
            "server|apple-tv",
        )
        self.redis.setex(
            media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid="channel-2"), 60,
            "server|living-room",
        )
        self._channel_is_live("channel-1")

        self.assertEqual(
            media_servers.switching_device(self.redis), "server|living-room"
        )

    def test_two_devices_switching_at_once_cannot_be_told_apart(self, _close):
        self._sessions("apple-tv", "living-room")
        for number, device in ((1, "apple-tv"), (2, "living-room")):
            self.redis.setex(
                media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid=f"channel-{number}"),
                60,
                f"server|{device}",
            )

        self.assertIsNone(media_servers.switching_device(self.redis))

    def test_a_device_that_stopped_watching_is_not_switching(self, _close):
        # Its channel is gone and so is its session: it went away, it did not switch
        self._sessions("apple-tv")
        self.redis.setex(
            media_servers.CHANNEL_DEVICE_KEY.format(channel_uuid="channel-2"), 60,
            "server|living-room",
        )
        self.assertIsNone(media_servers.switching_device(self.redis))


CHANNELS = {
    "MediaContainer": {
        "DeviceChannel": [
            {"identifier": "6420", "name": "ORF 1"},
            {"identifier": "6422", "name": "ATV"},
        ]
    }
}


class ChannelMapTests(TestCase):
    """A scan finds channels; they also have to be switched on before they appear."""

    def setUp(self):
        self.server = {"id": "a1", "url": "http://plex:32400", "token": "t"}

    def _plex(self, url, **_kwargs):
        if "/channels" in url:
            return fake_response(CHANNELS)
        return plex_with_tuners(url)

    def test_the_channels_are_switched_on_and_mapped_to_the_same_numbers(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.put"
        ) as put:
            get.side_effect = self._plex
            put.return_value = fake_response({})
            self.assertTrue(media_servers.enable_channels(self.server, "22"))

        params = put.call_args.kwargs["params"]
        # One value with commas: repeated, the server keeps the last and enables one channel
        self.assertEqual(params["channelsEnabled"], "6420,6422")
        # Dispatcharr's EPG uses the same numbers, so each channel maps to itself
        self.assertEqual(params["channelMapping[6420]"], "6420")
        self.assertEqual(params["channelMapping[6422]"], "6422")
        # Both maps, which is what the server's own settings send
        self.assertEqual(params["channelMappingByKey[6420]"], "6420")
        self.assertEqual(params["channelMappingByKey[6422]"], "6422")
        self.assertIn("/media/grabbers/devices/22/channelmap", put.call_args.args[0])

    def test_a_tuner_without_channels_is_left_alone(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.put"
        ) as put:
            get.return_value = fake_response({"MediaContainer": {}})
            self.assertFalse(media_servers.enable_channels(self.server, "22"))
            put.assert_not_called()

    def test_syncing_scans_enables_and_reloads(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post, patch("apps.proxy.live_proxy.media_servers.requests.put") as put:
            get.side_effect = self._plex
            post.return_value = fake_response({})
            put.return_value = fake_response({})
            self.assertTrue(media_servers.sync_tuner(self.server, "22", "32"))

        posted = [call.args[0] for call in post.call_args_list]
        self.assertIn("http://plex:32400/media/grabbers/devices/22/scan", posted)
        self.assertIn("http://plex:32400/livetv/dvrs/32/reloadGuide", posted)
        self.assertIn("/channelmap", put.call_args.args[0])


JELLYFIN_INFO = {"ServerName": "Home Jellyfin", "Version": "10.10.3"}
JELLYFIN_SESSIONS = [
    {
        "Id": "s1",
        "UserName": "Chris",
        "DeviceId": "shield-1",
        "DeviceName": "Shield",
        "Client": "Jellyfin Android TV",
        "LastActivityDate": "2026-09-17T20:15:00.0000000Z",
        "NowPlayingItem": {"Name": "ORF 1", "Type": "TvChannel"},
        "PlayState": {"IsPaused": False, "PositionTicks": 30000000},
        "TranscodingInfo": {"IsVideoDirect": False, "IsAudioDirect": True},
    },
    {
        "Id": "s2",
        "UserName": "Someone",
        "DeviceId": "laptop",
        "NowPlayingItem": {"Name": "A Film", "Type": "Movie"},
        "PlayState": {},
    },
]
JELLYFIN_CONFIG = {
    "TunerHosts": [
        {
            "Id": "abc123",
            "Url": "http://192.168.2.142:9191/hdhr/austria",
            "Type": "hdhomerun",
            "FriendlyName": "Austria",
            "TunerCount": 2,
        }
    ],
    "ListingProviders": [
        {
            "Id": "guide1",
            "Type": "xmltv",
            "Path": "http://192.168.2.142:9191/output/epg/austria",
            "EnableAllTuners": True,
        }
    ],
}


def jellyfin(url, **_kwargs):
    if "/System/Info" in url:
        return fake_response(JELLYFIN_INFO)
    if "/Sessions" in url:
        return fake_response(JELLYFIN_SESSIONS)
    if "/System/Configuration/livetv" in url:
        return fake_response(JELLYFIN_CONFIG)
    if "/ScheduledTasks" in url:
        return fake_response([{"Id": "task-1", "Key": "RefreshGuide"}])
    return fake_response({})


class JellyfinTests(TestCase):
    """Jellyfin, said in the same words as Plex: tuners, guides and who is watching."""

    def setUp(self):
        self.server = {
            "id": "j1",
            "kind": "jellyfin",
            "name": "Jellyfin",
            "url": "http://jf:8096",
            "token": "key",
        }

    def test_the_api_key_goes_in_the_headers_jellyfin_expects(self):
        headers = media_servers._headers(self.server)
        self.assertEqual(headers["X-Emby-Token"], "key")
        self.assertIn('MediaBrowser Token="key"', headers["Authorization"])
        # And a Plex server is unchanged
        self.assertEqual(
            media_servers._headers({"kind": "plex", "token": "t"})["X-Plex-Token"], "t"
        )

    def test_it_says_whether_it_answers_and_what_it_is(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = jellyfin
            self.assertEqual(
                media_servers.check(self.server),
                {"ok": True, "name": "Home Jellyfin", "version": "10.10.3"},
            )

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.return_value = fake_response({}, status=401)
            self.assertIn("API key", media_servers.check(self.server)["error"])

    def test_only_live_channels_count_as_watching(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = jellyfin
            sessions = media_servers.sessions(self.server)

        live = [session for session in sessions if session["live"]]
        (channel,) = live
        self.assertEqual(channel["user"], "Chris")
        self.assertEqual(channel["player"], "Shield")
        self.assertEqual(channel["device_id"], "shield-1")
        self.assertEqual(channel["decision"], "transcode (video)")
        self.assertEqual(channel["position"], 3.0)
        # The film is still a session, but not one the overlap cares about
        self.assertFalse(sessions[1]["live"])

    def test_its_tuners_and_guides_read_like_plex_s(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = jellyfin
            (tuner,) = media_servers.tuners(self.server, {"192.168.2.142"})
            (guide,) = media_servers.dvr_list(self.server)

        self.assertEqual(tuner["title"], "Austria")
        self.assertEqual(tuner["tuners"], 2)
        self.assertTrue(tuner["ours"])
        # A Jellyfin guide covers every tuner, so a tuner is never "in no DVR"
        self.assertEqual(tuner["dvr_id"], "guide")
        self.assertEqual(guide["tuners"], ["all tuners"])
        # And it is the address of that guide, not "none": the tuner has one, like on Plex
        self.assertEqual(tuner["guide"], "http://192.168.2.142:9191/output/epg/austria")
        self.assertEqual(guide["guide"], "http://192.168.2.142:9191/output/epg/austria")

    def test_a_tuner_is_added_as_an_hdhomerun_with_our_guide(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = jellyfin
            post.return_value = fake_response({})
            media_servers.add_tuner(
                self.server, "http://192.168.2.142:9191/hdhr/france", "france", 4
            )
            media_servers.create_dvr(
                self.server, "", "http://192.168.2.142:9191/output/epg/france", "france"
            )

        tuner_call, guide_call = post.call_args_list
        self.assertEqual(tuner_call.args[0], "http://jf:8096/LiveTv/TunerHosts")
        self.assertEqual(
            tuner_call.kwargs["json"],
            {
                "Type": "hdhomerun",
                "Url": "http://192.168.2.142:9191/hdhr/france",
                "FriendlyName": "france",
                "AllowHWTranscoding": True,
                "EnableStreamLooping": False,
                "TunerCount": 4,
            },
        )
        self.assertEqual(guide_call.args[0], "http://jf:8096/LiveTv/ListingProviders")
        self.assertEqual(guide_call.kwargs["json"]["Type"], "xmltv")
        self.assertEqual(
            guide_call.kwargs["json"]["Path"], "http://192.168.2.142:9191/output/epg/france"
        )
        self.assertTrue(guide_call.kwargs["json"]["EnableAllTuners"])

    def test_a_tuner_can_be_added_as_a_playlist_instead(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.post") as post:
            post.return_value = fake_response({})
            media_servers.add_tuner(
                self.server, "http://d:9191/output/m3u/france", "france", None, "m3u"
            )
        self.assertEqual(post.call_args.kwargs["json"]["Type"], "m3u")

    def test_what_each_session_is_watching_is_said_in_words(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = jellyfin
            channel, film = media_servers.sessions(self.server)
        self.assertEqual(channel["watching"], "live TV")
        self.assertEqual(film["watching"], "a film")

    def test_removing_a_tuner_and_a_guide(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.delete") as delete:
            delete.return_value = fake_response({})
            media_servers.delete_tuner(self.server, "abc123")
            self.assertEqual(delete.call_args.args[0], "http://jf:8096/LiveTv/TunerHosts")
            self.assertEqual(delete.call_args.kwargs["params"], {"id": "abc123"})

            media_servers.delete_dvr(self.server, "guide1")
            self.assertEqual(delete.call_args.args[0], "http://jf:8096/LiveTv/ListingProviders")

    def test_syncing_runs_the_guide_refresh_task(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = jellyfin
            post.return_value = fake_response({})
            self.assertTrue(media_servers.sync_tuner(self.server, "abc123", "guide"))

        # Jellyfin rescans while refreshing, and has no channel map to set
        self.assertEqual(post.call_args.args[0], "http://jf:8096/ScheduledTasks/Running/task-1")


class EditingAServerTests(TestCase):
    """Editing one must not quietly undo what was learned or switched about it."""

    def setUp(self):
        self.client_api = APIClient()
        self.client_api.force_authenticate(
            user=User.objects.create_user(username="admin3", password="x", user_level=10)
        )
        media_servers.save_servers([{
            "id": "a1",
            "kind": "plex",
            "name": "Plex",
            "url": "http://plex:32400",
            "token": "secret",
            "enabled": False,
            "dispatcharr_url": "http://192.168.2.142:9191",
        }])

    def test_renaming_keeps_the_address_it_learned_and_stays_switched_off(self):
        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get:
            get.side_effect = plex
            response = self.client_api.post(
                "/proxy/media-servers/",
                {"id": "a1", "name": "Living room", "url": "http://plex:32400"},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        (stored,) = media_servers.load_servers()
        self.assertEqual(stored["name"], "Living room")
        # Both were lost before: the tuner address had to be typed again, and a server
        # switched off came back on when anyone edited its name
        self.assertEqual(stored["dispatcharr_url"], "http://192.168.2.142:9191")
        self.assertFalse(stored["enabled"])
        self.assertEqual(stored["token"], "secret")
