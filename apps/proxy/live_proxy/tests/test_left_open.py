"""Closing a Jellyfin connection left open after everybody stopped watching (left_open.py).

The rule this file guards: only a connection Jellyfin holds for nobody is closed, and only
when that is known -- never one somebody watches or Jellyfin records, never one whose server
cannot be read or names something that cannot be placed, never another viewer's.
"""

import fnmatch
import time
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.channels.models import Channel, ChannelOverride
from apps.proxy.live_proxy import left_open, media_servers

JELLYFIN_IP = "192.168.2.50"
PLEX_IP = "192.168.2.141"


class FakeRedis:
    """The calls left_open makes, on dicts."""

    def __init__(self):
        self.values = {}
        self.sets = {}
        self.hashes = {}
        self.lists = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.hashes.pop(key, None)

    def expire(self, key, ttl):
        return True

    def scan_iter(self, pattern):
        return [key for key in list(self.sets) if fnmatch.fnmatch(key, pattern)]

    def smembers(self, key):
        return set(self.sets.get(key, ()))

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {field: value})

    def lpush(self, key, *values):
        self.lists.setdefault(key, [])[:0] = list(values)

    def ltrim(self, key, start, stop):
        pass

    def pipeline(self, transaction=True):
        return self

    def execute(self):
        return []


def fake_response(body, status=200):
    response = MagicMock(status_code=status, ok=status < 400)
    response.json.return_value = body
    if status >= 400:
        response.raise_for_status.side_effect = Exception(f"{status}")
    return response


def watching(channel_name, paused=False):
    return {
        "Id": f"s-{channel_name}",
        "UserName": "ckegels",
        "DeviceName": "Shield",
        "NowPlayingItem": {"Name": channel_name, "Type": "TvChannel"},
        "PlayState": {"IsPaused": paused},
    }


class LeftOpenTests(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.now = time.time()
        self.servers = [{
            "id": "j1", "kind": "jellyfin", "name": "Jellyfin", "token": "k",
            "url": f"http://{JELLYFIN_IP}:8096", "close_when_left": True,
        }]
        media_servers.save_servers(self.servers)
        self.addCleanup(media_servers.forget_hosts)
        self.sessions = []
        self.timers = {"Items": [], "TotalRecordCount": 0}
        self.sessions_status = 200
        self.timers_status = 200
        self.tfx = Channel.objects.create(name="┃FR┃ TFX", channel_number=1)
        self.arte = Channel.objects.create(name="Arte", channel_number=2)
        stop = patch("apps.proxy.live_proxy.services.channel_service.ChannelService.stop_client")
        self.stop = stop.start()
        self.addCleanup(stop.stop)
        get = patch("apps.proxy.live_proxy.media_servers.requests.get", side_effect=self._answer)
        self.get = get.start()
        self.addCleanup(get.stop)

    def _answer(self, url, **_kwargs):
        if "/LiveTv/Timers" in url:
            return fake_response(self.timers, self.timers_status)
        if "/Sessions" in url:
            return fake_response(self.sessions, self.sessions_status)
        return fake_response({})

    def connect(self, channel, client_id, agent="Jellyfin-Server/10.10.7", ip=JELLYFIN_IP,
                ago=600):
        key = f"live:channel:{channel.uuid}:clients"
        self.redis.sets.setdefault(key, set()).add(client_id)
        self.redis.hashes[f"{key}:{client_id}"] = {
            "user_agent": agent, "ip_address": ip,
            "connected_at": str(self.now - ago), "last_active": str(self.now),
        }

    def look(self, after=0):
        return left_open.look(self.redis, now=self.now + after)

    def closed(self):
        return [(str(call.args[0]), call.args[1]) for call in self.stop.call_args_list]

    def test_a_connection_nobody_is_behind_is_closed_after_the_grace(self):
        self.connect(self.tfx, "jf-1")
        self.look()
        self.assertEqual(self.closed(), [], "closed at the first look")
        self.look(after=left_open.GRACE_SECONDS - 1)
        self.assertEqual(self.closed(), [])
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-1")])
        # And it is said where the user looks for what happened to a channel
        self.assertTrue(any("left open" in event for event in self.redis.lists["live:recovery:events"]))

    def test_off_closes_nothing(self):
        self.servers[0]["close_when_left"] = False
        media_servers.save_servers(self.servers)
        self.connect(self.tfx, "jf-1")
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])
        self.assertEqual(self.get.call_count, 0, "it asked Jellyfin while switched off")

    def test_off_never_starts_the_look(self):
        self.servers[0]["close_when_left"] = False
        media_servers.save_servers(self.servers)
        with patch("apps.proxy.live_proxy.left_open.gevent.spawn") as spawn:
            left_open.sweep(self.redis)
        spawn.assert_not_called()

    def test_somebody_watching_keeps_it(self):
        """Jellyfin names the channel without the country; the names are compared bare."""
        self.connect(self.tfx, "jf-1")
        self.sessions = [watching("TFX")]
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_paused_is_watching(self):
        self.connect(self.tfx, "jf-1")
        self.sessions = [watching("TFX", paused=True)]
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_the_name_the_tuner_lineup_gives_is_recognised(self):
        """0.33: the lineup Jellyfin reads names a channel by its override, if it has one."""
        ChannelOverride.objects.create(channel=self.arte, name="ARTE HD")
        self.connect(self.arte, "jf-1")
        self.sessions = [watching("ARTE HD")]
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_a_recording_keeps_it(self):
        self.connect(self.tfx, "jf-1")
        self.timers = {"Items": [{"Status": "InProgress", "ChannelName": "TFX"}],
                       "TotalRecordCount": 1}
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_a_recording_still_to_come_does_not(self):
        self.connect(self.tfx, "jf-1")
        self.timers = {"Items": [{"Status": "New", "ChannelName": "TFX"}], "TotalRecordCount": 1}
        self.look()
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-1")])

    def test_a_server_that_cannot_be_read_keeps_everything(self):
        self.connect(self.tfx, "jf-1")
        self.look()
        self.sessions_status = 500
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [])
        # ...and when it answers again the grace starts over, from what is known
        self.sessions_status = 200
        self.look(after=left_open.GRACE_SECONDS + 30)
        self.assertEqual(self.closed(), [])
        self.look(after=2 * left_open.GRACE_SECONDS + 30)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-1")])

    def test_timers_that_cannot_be_read_keep_everything(self):
        """Without knowing what it records, a recording cannot be told from nobody."""
        self.connect(self.tfx, "jf-1")
        self.timers_status = 500
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_a_session_that_cannot_be_placed_keeps_everything(self):
        """Playing a name no connection is on: it may be one of them, written differently."""
        self.connect(self.tfx, "jf-1")
        self.sessions = [watching("Télé Fun Experience")]
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_a_live_session_without_a_channel_keeps_everything(self):
        self.connect(self.tfx, "jf-1")
        self.sessions = [{"Id": "s", "NowPlayingItem": {"Name": "News", "ChannelId": "abc"}}]
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_only_the_channel_nobody_watches_is_closed(self):
        """Jellyfin moved from TFX to Arte and left TFX open."""
        self.connect(self.tfx, "jf-old")
        self.connect(self.arte, "jf-new")
        self.sessions = [watching("Arte")]
        self.look()
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-old")])

    def test_a_film_is_not_live_tv_and_does_not_keep_a_channel(self):
        self.connect(self.tfx, "jf-1")
        self.sessions = [{"Id": "s", "NowPlayingItem": {"Name": "Heat", "Type": "Movie"}}]
        self.look()
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-1")])

    def test_a_young_connection_is_left_alone(self):
        """Jellyfin registers its session a moment after it opens the stream."""
        self.connect(self.tfx, "jf-1", ago=0)
        self.look()
        self.look(after=left_open.GRACE_SECONDS - 1)
        self.assertEqual(self.closed(), [])

    def test_watching_again_starts_the_grace_over(self):
        self.connect(self.tfx, "jf-1")
        self.look()
        self.sessions = [watching("TFX")]
        self.look(after=120)
        self.sessions = []
        self.look(after=150)
        self.look(after=150 + left_open.GRACE_SECONDS - 1)
        self.assertEqual(self.closed(), [])
        self.look(after=150 + left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-1")])

    def test_other_viewers_are_never_touched(self):
        """Another player, one on Jellyfin's own machine, Dispatcharr's own recording, a helper."""
        self.connect(self.tfx, "tivimate", agent="TiviMate/5.1", ip="192.168.2.20")
        self.connect(self.tfx, "vlc-on-the-jellyfin-box", agent="VLC/3.0.20", ip=JELLYFIN_IP)
        self.connect(self.tfx, "dvr", agent="Dispatcharr-DVR/1", ip=JELLYFIN_IP)
        self.connect(self.tfx, "rewind", agent="DispatchMore-Rewind/1", ip=JELLYFIN_IP)
        self.connect(self.tfx, "jf-1")
        self.look()
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-1")])

    def test_jellyfins_ffmpeg_is_jellyfin(self):
        self.connect(self.tfx, "jf-ffmpeg", agent="Lavf/61.7.100")
        self.look()
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.tfx.uuid), "jf-ffmpeg")])

    def test_on_a_machine_shared_with_plex_only_what_says_jellyfin(self):
        """Plex pulls with ffmpeg's User-Agent too, and its recordings cannot be seen."""
        self.servers[0]["url"] = f"http://{PLEX_IP}:8096"
        self.servers.append({"id": "p1", "kind": "plex", "name": "Plex", "token": "t",
                             "url": f"http://{PLEX_IP}:32400", "close_when_left": True})
        media_servers.save_servers(self.servers)
        self.connect(self.tfx, "plex-ffmpeg", agent="Lavf/60.3.100", ip=PLEX_IP)
        self.connect(self.arte, "jf-1", ip=PLEX_IP)
        self.look()
        self.look(after=left_open.GRACE_SECONDS)
        self.assertEqual(self.closed(), [(str(self.arte.uuid), "jf-1")])

    def test_plex_is_never_looked_at(self):
        self.servers = [{"id": "p1", "kind": "plex", "name": "Plex", "token": "t",
                         "url": f"http://{PLEX_IP}:32400", "close_when_left": True}]
        media_servers.save_servers(self.servers)
        self.connect(self.tfx, "plex-ffmpeg", agent="PlexMediaServer/1.41", ip=PLEX_IP)
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])

    def test_a_server_switched_off_is_never_looked_at(self):
        self.servers[0]["enabled"] = False
        media_servers.save_servers(self.servers)
        self.connect(self.tfx, "jf-1")
        self.look()
        self.look(after=3600)
        self.assertEqual(self.closed(), [])


class TimersTests(TestCase):
    def test_jellyfin_answers_with_a_page_of_timers(self):
        """LiveTvController.GetTimers returns a QueryResult; read as a list it found nothing."""
        page = {"Items": [{"Status": "InProgress", "ChannelName": "TFX"},
                          {"Status": "Completed", "ChannelName": "Over"}],
                "TotalRecordCount": 2}
        self.assertEqual(media_servers.jellyfin_recording(page), {"TFX"})
        self.assertEqual(media_servers.jellyfin_recording([{"Status": "InProgress",
                                                            "ChannelName": "TFX"}]), {"TFX"})
        self.assertIsNone(media_servers.jellyfin_recording(None))
        self.assertIsNone(media_servers.jellyfin_recording({"error": "no"}))

    def test_what_the_overlap_protects_reads_the_page_too(self):
        server = {"id": "j1", "name": "Jellyfin", "url": "http://jf:8096", "token": "t",
                  "kind": "jellyfin"}
        page = {"Items": [{"Status": "InProgress", "ChannelName": "TFX"}], "TotalRecordCount": 1}
        with patch("apps.proxy.live_proxy.media_servers.requests.get",
                   return_value=fake_response(page)):
            self.assertEqual(media_servers.recording_channels(server), {"TFX"})


class SwitchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        admin = User.objects.create_user(username="admin", password="x", user_level=10)
        self.client.force_authenticate(admin)
        media_servers.save_servers([
            {"id": "j1", "kind": "jellyfin", "name": "Jellyfin", "token": "k",
             "url": "http://jf:8096"},
            {"id": "p1", "kind": "plex", "name": "Plex", "token": "t", "url": "http://plex:32400"},
        ])
        self.addCleanup(media_servers.forget_hosts)
        rows = patch("apps.proxy.live_proxy.media_server_views._server_rows", return_value=[])
        rows.start()
        self.addCleanup(rows.stop)

    def test_off_by_default_and_switched_per_jellyfin_server(self):
        self.assertFalse(media_servers.public(media_servers.load_servers()[0])["close_when_left"])
        response = self.client.post("/proxy/media-servers/",
                                    {"id": "j1", "close_when_left": True}, format="json")
        self.assertEqual(response.status_code, 200)
        jellyfin, plex = media_servers.load_servers()
        self.assertTrue(jellyfin["close_when_left"])
        self.assertNotIn("close_when_left", plex)
        # Nothing else about the server changed
        self.assertEqual(jellyfin["token"], "k")
        self.assertEqual(jellyfin["url"], "http://jf:8096")

    def test_never_on_plex(self):
        response = self.client.post("/proxy/media-servers/",
                                    {"id": "p1", "close_when_left": True}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("close_when_left", media_servers.load_servers()[1])
