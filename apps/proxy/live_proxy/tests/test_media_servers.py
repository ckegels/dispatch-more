"""Media servers: storing one, reading what it plays, and adding that to a channel start."""

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
        "Dvr": [{"key": "32", "lineupTitle": "Belgium", "Device": [{"key": "22"}]}]
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

        def after_adding(url, **_kwargs):
            if "/media/grabbers/devices" in url:
                return fake_response(added)
            if "/livetv/dvrs" in url:
                return fake_response(in_a_dvr)
            return plex(url)

        with patch("apps.proxy.live_proxy.media_servers.requests.get") as get, patch(
            "apps.proxy.live_proxy.media_servers.requests.post"
        ) as post:
            get.side_effect = after_adding
            post.return_value = fake_response({})
            response = self.client_api.post(
                "/proxy/media-servers/tuners/",
                {
                    "server": "a1",
                    "channel_profile": "austria",
                    "base_url": "http://192.168.2.142:9191",
                    "tuner_count": 2,
                    "language": "nld",
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
        self.assertEqual(
            dvr_call["lineup"],
            "lineup://tv.plex.providers.epg.xmltv/"
            "http%3A%2F%2F192.168.2.142%3A9191%2Foutput%2Fepg%2Faustria#austria",
        )
        self.assertEqual(dvr_call["language"], "nld")
        # And it is scanned and its guide loaded, so it is ready to watch
        self.assertIn("http://192.168.2.141:32400/media/grabbers/devices/40/scan", calls)

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
        # A DVR holds a guide per tuner, so this tuner's guide is added next to the others
        self.assertEqual(
            puts["http://192.168.2.141:32400/livetv/dvrs/32/lineups"]["lineup"],
            "lineup://tv.plex.providers.epg.xmltv/"
            "http%3A%2F%2F192.168.2.142%3A9191%2Foutput%2Fepg%2Faustria#austria",
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
            get.side_effect = plex_with_tuners
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
        self.assertEqual(response.json()["dvrs"], [{"id": "32", "title": "Belgium"}])

    def test_a_tuner_of_ours_put_into_a_dvr_takes_its_guide_with_it(self):
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

        puts = {call.args[0]: call.kwargs.get("params", {}) for call in put.call_args_list}
        self.assertIn(
            "austria",
            puts["http://192.168.2.141:32400/livetv/dvrs/32/lineups"]["lineup"],
        )

    def test_putting_a_tuner_in_a_dvr_needs_a_dvr(self):
        response = self.client_api.post(
            "/proxy/media-servers/tuners/",
            {"server": "a1", "action": "attach", "id": "1"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a DVR", response.json()["error"])

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
            media_servers.device_watching(self.redis, "channel-1"), "server|mkk9dgqsm9p8"
        )
        # And the client on that channel is that viewer, so holds and Stop Skipped apply
        client = self.redis.hgetall(RedisKeys.client_metadata("channel-1", "c1"))
        self.assertEqual(client["server_device"], "server|mkk9dgqsm9p8")
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
