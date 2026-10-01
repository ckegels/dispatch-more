# arrTV ↔ Dispatch More: what arrTV tells the server, and what the server does with it

For the developer of **arrTV** (the AerioTV-Android fork). This describes what arrTV sends so
that a **Dispatch More** server (a fork of Dispatcharr 0.31: device and channel-change headers from v197, error reports from v198, quality away from home from v206 (FHD as a choice from v209), stutter from v207, what it can decode and a stream of its own from v208, faster failover from v212, the number of other streams from v213, choosing a channel's guide from v216 (a longer list and Load more from v217), a socket message when a guide changes from v219, commercial breaks in recordings from v238, subtitles found by Stream Check from v243 and the caption worker from v244, catch-up sessions kept alive from v246; latest release v246) knows which
device a request comes from, and which channel it is leaving. Everything here is extra: a stock
Dispatcharr server ignores it, and arrTV must work exactly as today when the server does not
announce support.

## Why

Dispatch More has features that act per device. The main one is **Force Close on Identified
Traffic**: when a device starts a channel, the server closes the other channels *that device*
still holds open. Many IPTV accounts allow a single connection, so this is what makes channel
switching work on them. The server used to work out "which device" from the IP address and the
login. That fails whenever several devices share an address: a VPN, a router doing NAT, a
reverse proxy.

**Real case:** a SHIELD running arrTV and a Mac running Chrome, both on the admin login, both
reached the server through a VPN as `192.168.65.3`. To the server they were one device, so each
channel start on one closed the stream on the other.

The app knows which device it is, so it can say so. It can also say which channel it is
leaving, so the server does not have to guess that either.

## Where arrTV stands (2026-09-28)

What the server offers and what arrTV does with it, as of Dispatch More **v218** (v217 is the
one installed on the user's server) and arrTV **0.5.9-arr.63** (a test build; the published
one is arr.62). Every arrTV optimization has a switch of its own in the app (Settings →
General → **arrTV optimizations**, all on by default); switched off, that part does nothing:

| Server feature | Server since | In arrTV | This document |
|---|---|---|---|
| Support check, device id and name | v197 | built | §1–§3 |
| Channel change (`X-Dispatch-Previous-Channel`) | v197 | built | §4 |
| Multiview session | v197 | built | §5 |
| Problem reports | v198 | built | §7 |
| Quality away from home | v206 | nothing to build (User-Agent or device header) | §8.1 |
| What the device can decode (`X-Dispatch-Max-Video`) | v208 | built (arr.40), switch "Only start streams this TV can play" | §8.2 |
| A stream of its own (409 on `change_stream`) | v208 | built (arr.40): a 409 reopens instead of walking | §8.3 |
| Stutter report (`POST /api/core/app-stall/`) | v207 | built (arr.37), switch "Tell the server when the picture stutters" | §8.4 |
| Reopen when a swap inside the connection freezes the picture | — (app only) | built (arr.40), switch "Reopen a frozen stream" | §8.6 |
| Faster failover when arrTV starts a channel | v212 | nothing to build (device header); the app's own waits are shorter too, switch "Faster switch to the next stream" | §8.7 |
| How many other streams a channel has (`X-Dispatch-Alternatives`) | v213 (two tiers from v214) | built (arr.43): the picture wait follows it, switch "Wait less when a channel has more streams", the seconds editable | §8.8 |
| Wrong guide? Choose another (`/api/core/app-guide/`) | v216, v217 (widening, Load more) | built (arr.57, Load more arr.60) | §7a |
| A guide changed: `channels_changed` with `"guide": true` on the socket | v219 | built (arr.64): the lineup is read again and the guide window fetched | §7a |

The TV report below was taken when the user's server was still on v205; it is on v217 now.
Install the latest release before testing any of §8.

**Why §8.2 and §8.6 matter first** (measured on the user's Chromecast with Google TV HD,
1.4 GB RAM, Android 14, arrTV arr.36): RTL ZWEI's first stream is 4K. Every tune began on it,
bytes arrived at full rate and nothing ever became playable (bufferedPosition 0). After 15 s
arr.36's new `[UNPLAYABLE]` rule walked with `change_stream` (the server swaps the upstream
behind the open connection); the next stream was unplayable too, the one after it (AVC 1080p,
E-AC-3 decoded in software) rendered **one frame** and then stalled with data still arriving
("ingest quiet 0ms") -- "Reconnecting" for over two minutes, nothing retried. Both the
`[UNPLAYABLE]` rule and the 50 s cold-start `[NO-DATA]` net require `!videoFrameRendered`, so
that one frame switched every rescue off. With `X-Dispatch-Max-Video: 1080` the server never
starts that device on the 4K stream (§8.2); §8.6 covers the freeze itself.

## 1. Detect support

```
GET /api/core/capabilities/
Authorization: Bearer <access token>      (or X-API-Key: <key>, as for the rest of the API)
```

Any logged-in user may call it (not only admins). On a stock Dispatcharr this returns **404**:
send none of the headers below. On Dispatch More:

```json
{
  "build": "Dispatch More v197",
  "version": "0.31.0",
  "app_integration": 1,
  "devices": true,
  "multiview": true,
  "switch_hints": false,
  "reports": false,
  "report_url": "/api/core/app-reports/",
  "outside_max_quality": "HD",
  "stall_switch": false,
  "stall_url": "/api/core/app-stall/",
  "own_stream": false,
  "headers": {
    "device": "X-Dispatch-Device",
    "device_name": "X-Dispatch-Device-Name",
    "multiview": "X-Dispatch-Multiview",
    "previous": "X-Dispatch-Previous-Channel",
    "max_video": "X-Dispatch-Max-Video"
  },
  "query_parameters": {
    "device": "dm_device",
    "device_name": "dm_device_name",
    "multiview": "dm_multiview",
    "previous": "dm_previous",
    "max_video": "dm_max_video"
  }
}
```

- `devices`, `multiview`, `switch_hints` and `reports` are the server admin's switches
  (Settings → Streaming → **arrTV**: "Recognise each arrTV device", "Close the previous
  channel when arrTV changes channel", "Take problem reports from arrTV"). **All are off
  by default.**
  Sending the headers while they are off is harmless: they are ignored.
- Ask once when a Dispatcharr playlist is added or refreshed, and cache the answer with the
  playlist (next to `DispatcharrCapability`). Re-ask on app start. It is cheap.
- `app_integration` is the version of this contract. Only rely on it if it is `>= 1`.

Simplest correct behaviour: **if the capabilities call answers 200 with `app_integration >= 1`,
always send the device headers on that server**, whatever `devices` says. The server decides
whether to use them.

## 2. The device ID (`X-Dispatch-Device`)

- Generate **once per install**: a random UUID v4, e.g. `3f2a9c1e-0b7d-4e21-9a55-0f1c2d3e4f50`.
  Store it in app-private storage (DataStore/SharedPreferences).
- Format the server accepts: `^[A-Za-z0-9-]{8,64}$`. A UUID fits. Anything else is ignored.
- **Must never be shared between devices.** arrTV has Google Drive sync (`DriveSyncManager`):
  **exclude the device ID from sync and from any backup/restore**, or two TVs restored from one
  backup become one device again. Also exclude it from Android Auto Backup
  (`android:fullBackupContent` / `dataExtractionRules`).
- Do not derive it from hardware IDs (ANDROID_ID, MAC). A random UUID is enough and reveals
  nothing.
- The server combines it with the login: device X on login A and device X on login B are two
  different viewers. A device can only ever affect streams opened with its own login.

`X-Dispatch-Device-Name` is what the device is called in the server's Diagnostics ("admin ·
Living room SHIELD"). Use the Android device name (`Settings.Global.DEVICE_NAME`, falling back
to `Build.MODEL`), or a name the user can set in arrTV's settings. At most 80 characters.
Optional, but please send it: it is what makes the server's logs readable.

## 3. Where to send the headers

On **every request to the Dispatcharr server that plays or opens a live stream**, and it is
fine (simplest) to send them on every request to that server:

| Request | Examples |
|---|---|
| Live stream | `/proxy/ts/stream/<channel uuid>`, `/live/<user>/<pass>/<channel id>[.ts\|.m3u8]` |
| Anything else on that server | API calls, EPG, logos: harmless, and keeps one code path |

Two places in arrTV make these requests:

1. **The API client** (`core/network/DispatcharrClient.kt`, Ktor). Add the headers as defaults
   on the client, for example:

   ```kotlin
   install(DefaultRequest) {
       if (appIntegration) {
           header("X-Dispatch-Device", deviceId)
           header("X-Dispatch-Device-Name", deviceName)
       }
   }
   ```

2. **The player's HTTP data source** (`core/playback/AerioExoPlayerHolder.kt`, Media3). This
   is the one that matters: the stream request *is* the viewer. Set the headers on the
   `DataSource.Factory` used for live playback, per tune, because the multiview and
   previous-channel values change per request:

   ```kotlin
   val headers = buildMap {
       put("X-Dispatch-Device", deviceId)
       put("X-Dispatch-Device-Name", deviceName)
       multiviewSession?.let { put("X-Dispatch-Multiview", it) }
       leavingChannel?.let { put("X-Dispatch-Previous-Channel", it) }
   }
   DefaultHttpDataSource.Factory()            // or OkHttpDataSource.Factory(okHttp)
       .setDefaultRequestProperties(headers)
   ```

   If the same factory instance is reused across tunes, update its request properties before
   each tune (`setDefaultRequestProperties` replaces them), or create a factory per tune.

**When headers cannot be set** (a Chromecast playing the URL itself, an external player), put
the same values in the stream URL as query parameters. Dispatcharr ignores parameters it does
not know, so this is safe on stock too:

```
/proxy/ts/stream/<uuid>?dm_device=3f2a9c1e-...&dm_device_name=Living%20room%20SHIELD
```

arrTV's own Cast path (`core/cast/hlsproxy/CastHlsProxyServer`) fetches the stream on the phone
and serves it to the Cast device. There the phone makes the request, so use headers. Use the
**phone's** device ID: the phone is what holds the connection.

## 4. Changing channel: `X-Dispatch-Previous-Channel`

Server switch: `switch_hints` (off by default).

When the user changes from channel A to channel B **in the same player**, send on the request
for B:

```
X-Dispatch-Previous-Channel: <channel A>
```

The server then closes A immediately (if this device is its only viewer and it is not being
recorded) and holds its connection slot for B. On single-connection accounts, the difference is
between a quick switch and a refusal or a stall while A's connection times out.

**"Previous" is the channel that is actually playing, not the last one asked for.** When the
user zaps quickly A → B → C, the server may drop the request for B unanswered (Dispatch
More's surfing delay: a request a newer one has replaced is never sent to the provider). B
then never plays, and A is still the channel open. So the request for C must say
`Previous-Channel: A`, the channel the player is really leaving. If it said B, nothing would be
closed and A would stay open until its connection ends. In code: remember the channel of the
stream that last *started playing* (first frames), not of the last tune request, and send that.

Channel A may be given as:
- its **UUID**, the id in `/proxy/ts/stream/<uuid>` (Direct Connect), or
- its **number id**, the `<channel id>` in `/live/<user>/<pass>/<channel id>` (Xtream).

Use whichever the app used to open A. Format: letters, digits, dashes, at most 80.

**Send it only for a real channel change by the user:**

| Situation | Send previous? |
|---|---|
| User zaps A → B (up/down, number entry, guide, list) | **yes**, A |
| First channel after opening the app / player | no |
| Reconnect / retry of the same channel (network blip, `StreamEndVerifier`, 503 retry) | **no**: A == B, and the server ignores it anyway |
| Failover inside one channel (`LiveStreamFailover`, `change_stream`) | **no**: same channel, and `change_stream` is not a new stream request |
| Leaving the player to the guide/home | no header (there is no new request); just close the connection |
| Catch-up / timeshift / VOD | no (catch-up sessions: from v246 a re-minted session stays alive while it streams, and position reports reach it -- `fork/HANDOVER.md` §5.7c; arrTV keeps reporting after the first "no active playback" 404 from arr.73) |

The server never closes a channel another viewer is also on, never one being recorded, and
never another device's, even if the header names it. A wrong value costs nothing.

## 5. Multiview: `X-Dispatch-Multiview`

Server switch: `devices` (Multiview comes with device identity).

Without this, Force Close would treat each new tile as "the device changed channel" and close
the tile before it. With it:

- When Multiview opens, generate a **session id** (a short random string, format
  `^[A-Za-z0-9-]{1,64}$`, e.g. a UUID) and keep it for as long as that Multiview stays open.
- Send `X-Dispatch-Multiview: <session id>` on **every tile's** stream request (MultiviewScreen,
  per tile).
- A request carrying a session id **closes nothing** of this device. That includes the channel
  that was playing full screen when Multiview was entered, which becomes a tile.
- When a tile **changes channel**, send `X-Dispatch-Previous-Channel` with that tile's old
  channel, as in §4. That is how a tile's old channel is released.
- When a tile is **removed**, close its connection (release the player). Nothing to send.
- When Multiview **closes** and one tile goes full screen, the next ordinary request (no session
  header) lets Force Close tidy up the device's leftover channels as usual.

## 6. Leaving a channel

Close the HTTP connection (release the ExoPlayer / data source) when the user leaves playback.
Dispatcharr frees the slot when the connection closes. Do not keep a connection open in the
background "for a fast return". On single-connection accounts that blocks every other device.

(Dispatcharr's `/proxy/ts/stop_client/` endpoint is admin-only and needs a server-side client
id the app never sees. Do not use it.)

## 7. Error reports

Server switch: `reports` (off by default). Only offer "Report a problem" when capabilities
say `"reports": true`.

**Where in arrTV:** holding OK while a channel plays opens the player's options; **"Send a
report to the server"** is the first entry there. It asks "What went wrong?" with a list of
choices, not a text field (typing with a remote is slow): Stream doesn't load, Picture
stutters or freezes, Sound problem, Wrong or missing guide, Wrong channel or picture,
Something else. One press sends it: the choice's words as `what`, its code as
`extra.problem` (`no_load`, `stutter`, `sound`, `guide`, `wrong_channel`, `other`). Then
"Sent" with the report id from the answer, or the error message.

**Kept until deleted.** The server keeps every report, one database row each, with no count
or age limit, until an admin deletes it (one at a time, or all at once) on Settings → arrTV.

**The request:**

```
POST /api/core/app-reports/
Authorization: Bearer <access token>          (the playlist's login; any user level)
X-Dispatch-Device: <device id>                (as everywhere, see §2)
Content-Type: application/json
```

```json
{
  "device_id": "3f2a9c1e-0b7d-4e21-9a55-0f1c2d3e4f50",
  "device_name": "Living room SHIELD",
  "channel_uuid": "0f1e2d3c-...",
  "channel_id": 1234,
  "what": "Picture froze after two minutes, sound kept going",
  "happened_at": "2026-09-27T20:14:05Z",
  "app": {
    "name": "arrTV", "version": "1.4.0", "build": 140,
    "android": "11", "model": "SHIELD Android TV", "manufacturer": "NVIDIA",
    "decoder": "c2.nvidia.hevc.decoder"
  },
  "player": {
    "state": "BUFFERING",
    "error": "Source error: HttpDataSourceException: Response code: 503",
    "error_code": 2004,
    "url": "http://server:9191/proxy/ts/stream/0f1e2d3c-...",
    "position_ms": 128000, "buffered_ms": 0,
    "video": "1920x1080 h264 25fps", "audio": "aac 2ch",
    "bitrate_kbps": 5400, "dropped_frames": 12, "stalls": 3,
    "stall_seconds": 14.5, "first_byte_ms": 820, "failover_steps": 1
  },
  "network": { "type": "ethernet", "vpn": true, "down_kbps": 48000 },
  "extra": { "multiview_tiles": 1, "cast": false },
  "log": "<the player's own log for the last minutes, newest last>"
}
```

- Send **either** `channel_uuid` (Direct Connect) **or** `channel_id` (Xtream), whichever
  the app used to play it. Everything else is optional: send what you have. The more you
  send, the less guessing on the other side.
- `player`, `network`, `app` and `extra` may hold any keys. They are kept as sent (at most 60
  keys each, text up to 4,000 characters per value). `log` is kept up to its last 100,000
  characters: send the tail of the player log (`DebugLogger`), not the whole file.
- `happened_at` is when it went wrong (ISO 8601, UTC). The user presses "report" a moment
  later, and the server uses the time it received the report for its own side.
- **Logins and passwords are removed by the server** (Xtream `/live/<user>/<pass>/…` paths,
  `password=`/`token=` parameters). Still, don't send the playlist password on purpose.

**The answer:** `201 {"id": "a1b2c3d4e5f6"}`. `403` when the server does not take reports
(switch off): tell the user "This server does not take reports". Nothing is retried.

**What the server adds by itself**, so the app need not: the channel with its streams in
order, each stream's provider and what Stream Check last found; the channel's live readings
and what happened to it (reconnects, stream switches, errors); how it started, phase by
phase; this device's channel switches and Force Close; and the server's log lines about the
channel over the last 30 minutes. The admin reads it all on Settings → arrTV → Problem reports,
and copies it whole to pass on.

## 7a. Wrong guide? Choose another (server v216, v217)

Server switch: `guide_choice` (off by default), on Settings → arrTV. Capabilities say
`"guide_choice": true` and `"guide_choice_url": "/api/core/app-guide/"`. It does **not** need
`devices`: a device that does not say who it is can still use it (the record then says
"unknown device"). The whole design, with the reasons, is `fork/arrTV-guide-choice.md`.

**Where in arrTV:** the player's options (hold OK), directly **under "Send a report to the
server"**: **"Wrong guide? Choose another"**, only when capabilities say `guide_choice`. The
report and its "Wrong or missing guide" choice stay as they are; the two are independent.

**The list:** `GET /api/core/app-guide/?channel=<uuid or id>` with the usual `Authorization`
and `X-Dispatch-Device` / `-Name` headers.

```json
{
  "channel": {"uuid": "0f1e…", "id": 1234, "name": "┃AT┃ ORF 1", "number": 101},
  "current": {
    "epg_id": 5501, "name": "ORF1.at", "source": "EPGShare AT",
    "now":  {"title": "Zeit im Bild", "start": "2026-09-27T17:30:00Z", "end": "2026-09-27T17:50:00Z"},
    "next": {"title": "Wetter", "start": "2026-09-27T17:50:00Z"}
  },
  "guides": [
    {
      "epg_id": 88213, "name": "ORF 1 HD", "tvg_id": "ORF1HD.de", "score": 91,
      "source": {"id": 3, "name": "EPGShare DE"},
      "now":  {"title": "Zeit im Bild", "start": "…", "end": "…"},
      "next": {"title": "Wetter", "start": "…"}
    }
  ],
  "reading": false,
  "more": true
}
```

- `guides`: at most **20**, best match first across every source (the Guides tab's own
  ranking), **only guides with a programme on now**, the current guide never among them.
  `next` may be `null`.
- `current`: the channel's guide, shown apart as what is being replaced; `null` for a channel
  on no guide; its `now` is `null` when it holds nothing ("Guide now: no information").
- `reading: true`: some candidates had never been read and are being read now. Show "Looking
  for more guides…" and ask again every 5 seconds while it stays true, for at most a minute.
- v217: the list widens until it holds 20 (any confidence, then without the Guides tab's
  matching limits, then a plain name search), so `score` goes down the list and may be `0`
  for a search result.
- `more` (v217): `GET …&shown=<epg ids, comma-separated>` gives the next page, those left
  out, looking deeper down. Show "Load more" while `more` is true.
- 403: switched off (hide the entry until capabilities are read again). 404: this login may
  not watch that channel, or it is gone.

**The screen:** "Guide now: ORF1.at — Zeit im Bild (17:30–17:50)" at the top, then one row per
guide: **the programme on now in large type** (what is compared with the picture), under it the
guide's name, its source and when the programme ends. The video keeps playing beside or behind
it. One press on a row chooses it; Back closes without changing anything. While `reading` is
true the list is asked for again every 5 s (at most a minute) and rows are added without moving
the focus; a **Load more** row at the bottom asks for the next page while `more` is true, and
the focus goes to the first new row. Only when nothing is being read and nothing more can be
had: "No other guide has anything on for this channel right now." (arr.60; in the info bar
style's options row the entry is labelled "Wrong guide?".) The channel's name is under the title
(arr.65), and **"Could not find the guide"** beside Close sends a problem report (§7) with
`extra.problem: "guide"` and `extra.from: "guide list: could not find the guide"` -- the same
as the report menu's "Wrong or missing guide" -- so an admin sees it among the reports.

**Choosing:** `POST /api/core/app-guide/` with `{"channel": "<uuid>", "epg_id": 88213}`.

- **200** `{"ok": true, "channel": {…}, "guide": {"epg_id", "name", "source", "now", "next"}}`:
  arrTV closes the list at the press and sends this in the background, then shows "Guide
  changed to ORF 1 HD" (or the refusal's message) as a short notice. Then reload that channel's programmes after
  about 5 seconds (the server reads the new guide in the background after the save). The
  guide the channel is already on is answered 200 too: it means "this one is right".
- **409**: the guide has nothing on any more; show the message and ask for the list again.
- **403** / **404**: as for the list.

**Every app hears of it** (v219): the server sends Dispatcharr's socket message
`{"type": "update", "data": {"type": "channels_changed", "source": "guides", "channels": [uuids],
"guide": true}}` when a guide is put on a channel (here or on the Guides tab), and again with
`"source": "guide read"` once that guide's programmes have been read. A channel's programmes
are keyed by its guide's tvg-id, learnt from the lineup, so the app reads the lineup again
first, then the guide window (arr.64 also does this itself 2 s, 30 s and 90 s after its own
choice, for servers before v219).

**Show Groups** (v231, Channel Manager → Show Groups) sends the same message with
`"source": "show_groups"`, `"profile": "<its profile>"` and the copies that joined or left,
whenever channels come into or go out of a show group (Cooking, Travel, ...). The copies are
ordinary channels in the "Show Groups" profile, in a channel group per show group, hidden from
output while out of their group; an app reads the lineup again on the message, as arr.64 does.
The Show Groups plugin sent the same message, so nothing changes for the app.

The change is for every viewer (and Plex and Jellyfin), and the server records it with the
login, the device id and name, and the address it came from; an admin sees it on Settings →
arrTV ("Guide changes", with Put back and Keep) and on the Guides tab. From v220 choosing the
guide a channel is already on ("this one is right") is recorded as a confirmation and keeps
the change it confirms, so it can still be put back.

**The guide's past** (server v220, nothing for the app to send). With "Keep the guide's past"
set on Settings → arrTV (`keep_past_days`, 0–7), a guide refresh no longer deletes what has
already been on: the finished programmes of those days stay. The app gets them the way any
player asks for the past from Dispatcharr: the login's "EPG previous days" (set on the user
in Dispatcharr), or `prev_days=<days>` on the XMLTV address. The past fills in from the next
refresh on; nothing from before the setting was on comes back.

## 8. Picture quality

Four server features about getting each device a stream it can play, and two things the app
should know. §8.2, §8.4 and §8.6 are what arrTV still has to build.

### 8.1 Quality away from home (server v206)

Nothing to build for this. The server admin sets **home networks** and **"Away from home, at
most"** FHD (1080p, from v209), HD (720p) or SD; "No limit" is 4K. An arrTV request from outside the home networks (the VPN, a phone
connection) then starts a channel on a stream within that quality, where the channel has
one. `outside_max_quality` in the capabilities says it is on ("" is off), in case the app
wants to show it.

What the app must keep doing, because it is how the server recognises arrTV: send the device
header (§2), **or** keep the User-Agent as it is now, `AerioTV/<version>-arr. (Android; …)`.
A User-Agent without `-arr` and without the device header is taken for another app and gets no
limit.

### 8.2 What the device can decode: `X-Dispatch-Max-Video` (server v208)

Server switch: `devices` (read with the other device headers). Send, with the device headers,
the tallest picture this device can decode, as a height: `1080` on a Chromecast with Google TV
HD, `2160` on a 4K device (which limits nothing). Work it out once per install from
`MediaCodecList`: the largest supported height of the `video/hevc` and `video/avc` decoders
(`VideoCapabilities.getSupportedHeights().upper`), and round down to 2160 / 1080 / 720 / 576.
`FHD`, `HD` or `SD` are accepted too.

What the server does with it:
- A channel this device **starts** begins on a stream it can decode, in the channel's order;
  better ones go last (before "Could Not Dispatch"), so a channel with nothing else still
  tries its best. The 4K stream of RTL ZWEI is then never where a Chromecast HD starts.
- The channel's **failover** keeps to it too: it does not fail over onto the 4K stream.
- A channel **someone else is already watching** in a quality the device cannot use: with the
  server's `own_stream` switch on, the device gets another stream of that channel to itself,
  from a provider with a connection free (§8.3). Off, it joins what is playing.

### 8.3 A stream of its own (server v208)

Nothing to build: the request is the usual `/proxy/ts/stream/<channel uuid>` (or `/live/…`) and
the server decides. With `own_stream` on, an arrTV device whose limit (decoding, away from
home, or a stutter it was held to) is below what the channel is playing for others is served
another of the channel's streams, run on its own. What the app should know:
- `X-Dispatch-Previous-Channel` and the stutter report keep naming the **channel**; the server
  applies them to the device's own stream.
- `POST /proxy/ts/change_stream/<uuid>` (LiveStreamFailover's walk) is refused with **409**
  while the device is on a stream of its own: changing the channel would change it for the
  others on it. Treat 409 like any refused step; the stream it is on is within what it can play.
- A stutter report (§8.4) while on a stream of its own is answered
  `{"action": "none", "reason": "This device plays this channel on a stream of its own."}`:
  the server does not yet move a device's own stream. If it stutters there, reopening the
  connection (§8.6) gets the device a stream of its own again, chosen afresh.
- When the device's own stream ends (the provider drops it), there is no server-side
  failover for it: the connection closes, and arrTV's normal reconnect asks for the channel
  again and is given a stream afresh.
- It opens one more provider connection, and only where one is free: on a single-connection
  account already in use the device joins the channel as before, 4K and all.

### 8.4 Stutter: tell the server the moment the picture stops (server v207)

Server switch: `stall_switch` (needs `devices`; capabilities say `"stall_switch": true` and
`"stall_url": "/api/core/app-stall/"`). Only the device knows it is stuttering: the server sees
what the provider sends, not how fast the device receives it. When arrTV says so, the server
moves the channel to its next stream **at once, in place** (the same swap a failover does, so
the player keeps its connection and does nothing).

**When to send:** on every rebuffer of a live stream **after its first frame**, which is
exactly `PlaybackTrace.onBuffering` / the `onStall` hook. Not before the first frame (that is
the start), not for catch-up/VOD, not when the connection itself failed (that is the existing
retry ladder). Send it straight away; do not wait to see whether it recovers. Fire and forget,
off the main thread, one request per stall.

```
POST /api/core/app-stall/
Authorization: Bearer <access token>          (the playlist's login; any user level)
X-Dispatch-Device: <device id>                (required: the server matches it to the stream)
Content-Type: application/json
```

```json
{
  "channel_uuid": "0f1e2d3c-...",
  "stalls": 3,
  "feed_media_ratio": 0.72,
  "worst_gap_ms": 4100,
  "bandwidth_kbps": 3800,
  "dropped_frames": 0
}
```

Either `channel_uuid` or `channel_id`, as in §4. The rest is optional and only written into the
server's log and Channel health so a person can see why it switched: `stalls` since the tune,
`feedMediaRatio()`, `worstGapMs()`, `bitrateEstimateBps / 1000`, `droppedTotal`.

**The answer** (200): `{"action": "switched", "stream": "┃AT┃ ORF 1 HD"}` or
`{"action": "none", "reason": "…"}`. Nothing to do with either: after a switch the picture
continues on the same connection -- except when the new stream's codec differs and the
picture freezes, which is §8.6. `403` while the switch is off: stop sending for that server
until the next capabilities call.

What the server does, so the app need not:
- Same quality or lower, never better, never the "Could Not Dispatch" fallback, in the
  channel's own order. No other stream: nothing.
- The first 10 s after a start or a switch are ignored: the swap itself makes the player
  wait, and that is not stutter. So a stall right after a switch is expected and harmless.
- A stream left for stuttering is not gone back to for 10 minutes.
- Stuttering again goes down in quality, and **that device starts every channel within
  that quality for 24 hours** (learned at home and away apart). An admin can forget it.
- Never on a channel someone else watches without trouble: only when this device is alone
  on it, or every other viewer is an arrTV device that stuttered in the last 30 s.
- `LiveStreamFailover` stays as it is: it handles silence (no bytes); this handles a picture
  that plays badly.
- Where the `onStall` hook fires today (arr.36) is the right place: it already only counts
  a rebuffer after the first frame, and it has every number the report carries.

### 8.5 How the server judges a stream's quality (nothing to build)

So the app knows why a limit sometimes lets a 1080p stream through. The server reads a
stream's **measured resolution** where one was recorded -- only when a stream played through
ffmpeg (a stream profile other than Proxy), so on the user's server almost never -- and
otherwise its **name**: `4K`/`UHD`/`2160p` is 4K, `FHD`/`1080p`/`1080i` is FHD, `HD`/`720p`
is HD, `SD`/`576p`/`480p` is SD. A name without any of those is *unknown* and is let
through, so a good stream is never buried for saying nothing. Providers write "HD" for
anything that is not SD, and German private channels (RTL, ProSieben, SAT.1) broadcast
1080i, so an "HD" stream is often 1080 in fact: with "at most HD" it passes. Observed by the
user: RTL ZWEI came out 720p, other channels still 1080p (and played fine).

The limits also only apply to a device the server counts as **away from home**: an address
outside the admin's home networks. The Chromecast was at `192.168.10.100`; with home networks
`192.168.2.0/24` it counts as away although it is in the house.

### 8.6 A stream swap inside the open connection can freeze the picture (arrTV to build)

Dispatcharr changes a running channel's stream *behind the same connection*: stock failover,
`POST /proxy/ts/change_stream/<uuid>` (LiveStreamFailover), and the stutter switch (§8.4) all
do it. The player keeps its connection and sees one MPEG-TS stream turn into another. When
the codec changes (HEVC 4K → AVC 1080p on the Chromecast), ExoPlayer rendered one frame and
then waited for good while data kept arriving. Not yet proven which part does it (PIDs,
timestamps, the decoder); what is proven is that nothing in arrTV recovered.

What to build:
- **After a server-side stream change** (a `change_stream` that answered 200, or a stutter
  report answered `"switched"`), watch the picture: if no new frame is rendered for a few
  seconds while bytes keep arriving, **re-prime the player** -- release it and open the same
  URL again. Open the new connection *before* dropping the old one, or the channel has no
  viewer for a moment: the server's shutdown delay is 0, the channel stops, and the new
  request starts it from its first stream. With §8.2 sent, that first stream is one the
  device can decode, so reopening cannot land on the 4K stream again.
- **A stall after the first frame must reach a rescue.** Today `[UNPLAYABLE]` and the
  `[NO-DATA]` net both need `!videoFrameRendered`; a "Reconnecting" that lasts minutes with
  bytes arriving should end in the same walk or reopen.
- The server reports what happened on its side in Channel health (Settings → Diagnostics →
  Channel health → What happened) and in a problem report (§7), which carries the channel's
  switches: send one when this happens.

### 8.7 Faster failover when arrTV starts a channel (server v212)

Server switch: `fast_failover` (needs `devices`; capabilities say `"fast_failover": true`).
Nothing for the app to send beyond the device header. A channel a declared arrTV device
starts is marked for two minutes; while its stream is connected and has sent **nothing**, the
stream manager leaves it after a few seconds and **one** health check, instead of the start
grace (Settings → Streaming, 60 s) and three. From v220 this holds at **every step** of the
walk, each stream's wait counted from its own connection: with two or more other streams left,
`fast_grace_many` (3 s); with one left, `fast_grace` (5 s); with none left, stock's grace. A
channel with nowhere to go is never hurried -- no other stream the device can play, none on a
provider with a connection free, or a stream of its own (§8.3), which has no failover. Once
data has come, stock rules apply.
Switching it off takes effect at once. IPTV answers within a second or two (the TVs learned
0.1–3 s); a source that needs longer to lock would be left too soon, which is why it is only
for arrTV and off by default.

The app shortens its own waits the same way (arr.40, switch "Faster switch to the next
stream"): 6 s (or 1.5× the channel's learned first byte, at most 12 s) before the first step
instead of 28 s, 5 s per later step instead of 12, 6 s for "data but nothing playable" instead
of 15. From arr.41 the app also has a **picture deadline** (switch "Leave a stream that is slow
to start"): no first frame within twice the channel's usual start time (8–20 s) walks to the
next stream whatever the bytes do -- a stream that connects slowly or trickles its data never
looks dead to the server. And when the capabilities say `fast_failover`, the app's first
moves wait at least 12 s so the server moves first: both moving at once would skip a stream.
The server's 5 s already includes connecting: a stream counts as connected when its reader
thread starts, before the provider answers.

### 8.8 How many other streams a channel could switch to: `X-Dispatch-Alternatives` (server v213, two tiers from v214)

Server switch: `alternatives` (needs `devices`; capabilities say `"alternatives": true`). Each
stream response to a declared arrTV device carries

```
X-Dispatch-Alternatives: 3
```

-- how many of the channel's **other** streams it could be moved to for this device **now**:
never the "Could Not Dispatch" fallback, an active account, within the device's quality limit
(what it decodes, away from home, a stutter limit), not playing on another channel, and on
the account the channel holds or on one with a connection free (the rules of a stream of its
own, `app_own_streams._pick`). A channel not running yet counts one less (one of them is where
it starts). No header: switched off, not a declared device, or a stream of its own.

What the server does with it itself: faster failover's wait is `fast_grace_many` (default 3 s)
with two or more left, `fast_grace` (default 5 s) with one, both editable under the switch,
worked out again at each step from what is left; a channel with **none** is not hurried
(leaving its stream could only end on the fallback). From v220 the server counts this for
faster failover whether or not the header is switched on.

What arrTV does (arr.43): the picture wait ("Leave a stream that is slow to start") is the
channel's usual start time × 1.5 with 2+ others and × 2 with 1, at least 3 s / 5 s, at most
10 s (two tiers: the user's channels have three streams at most) -- all editable in the app (Settings → General → Wait for a picture) -- and no walk
at all with 0. The count is taken less the streams the walk already left. A stream that sends
nothing is left to the server when its faster failover is on; the app's deadline takes the
ones whose data arrives without a picture, so the two never move at once.

## 9. Behaviour matrix

| Server | Capabilities | What arrTV does | Result |
|---|---|---|---|
| Stock Dispatcharr | 404 | sends nothing new | exactly as today |
| Dispatch More, switches off | 200, `devices: false` | sends headers | ignored: exactly as today |
| Dispatch More, `devices` on | 200, `devices: true` | sends headers | each device is its own viewer, Multiview safe, names in Diagnostics |
| Dispatch More, `switch_hints` on | 200, `switch_hints: true` | also sends previous on zaps | old channel closed at once, faster switching |
| Dispatch More, `reports` on | 200, `reports: true` | offers "Send a report" in the player settings | report on Settings → arrTV, with the server's view |
| Dispatch More, `stall_switch` on | 200, `stall_switch: true` | posts each rebuffer after the first frame | the channel moves to its next stream at once |
| Dispatch More, `devices` on, `X-Dispatch-Max-Video` sent | 200 | sends the header with the device headers | channels it starts, and their failover, keep to what it decodes |
| Dispatch More, `own_stream` on | 200, `own_stream: true` | nothing new; treats 409 on `change_stream` as a refused step | a device that cannot use what a channel plays for others gets another stream of it |

## 10. Testing without the app

With the first two switches on in Settings → arrTV, from two terminals with the same login:

```bash
# "device 1" starts channel A
curl -s -o /dev/null -H "X-API-Key: $KEY" \
  -H "X-Dispatch-Device: test-device-0001" -H "X-Dispatch-Device-Name: Test TV" \
  "$SERVER/proxy/ts/stream/$CHANNEL_A" &

# "device 2" on the same login and address starts channel B: A keeps playing
curl -s -o /dev/null -H "X-API-Key: $KEY" \
  -H "X-Dispatch-Device: test-device-0002" -H "X-Dispatch-Device-Name: Test Mac" \
  "$SERVER/proxy/ts/stream/$CHANNEL_B" &

# "device 1" zaps from A to C and says so: A is closed at once
curl -s -o /dev/null -H "X-API-Key: $KEY" \
  -H "X-Dispatch-Device: test-device-0001" -H "X-Dispatch-Previous-Channel: $CHANNEL_A" \
  "$SERVER/proxy/ts/stream/$CHANNEL_C" &
```

What to look for in the server log (Settings → Diagnostics → Logs):

```
App switch: closing channel <A> for Viewer(... server_device='app|<user id>|test-device-0001' ...)
```

and **no** `Force close: stopping channel <A>` caused by `test-device-0002`. The Channel
switches tab names the devices "admin · Test TV" and "admin · Test Mac".

A report, by hand:

```bash
curl -s -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "{\"channel_uuid\": \"$CHANNEL_A\", \"what\": \"test report\", \"player\": {\"state\": \"BUFFERING\"}}" \
  "$SERVER/api/core/app-reports/"
```

What a device can decode (§8.2), with `devices` on: start a channel whose first stream is 4K
as a device that says 1080, and the server log names the stream it chose instead:

```bash
curl -s -o /dev/null -H "X-API-Key: $KEY" -H "User-Agent: AerioTV/test-arr." \
  -H "X-Dispatch-Device: test-device-0001" -H "X-Dispatch-Max-Video: 1080" \
  "$SERVER/proxy/ts/stream/$CHANNEL_4K" &
```

A stutter (§8.4), with `stall_switch` on, while that device plays the channel and more than
10 s after it started:

```bash
curl -s -X POST -H "X-API-Key: $KEY" -H "X-Dispatch-Device: test-device-0001" \
  -H "Content-Type: application/json" -d "{\"channel_uuid\": \"$CHANNEL_4K\", \"stalls\": 1}" \
  "$SERVER/api/core/app-stall/"
# {"action": "switched", "stream": "..."}  or  {"action": "none", "reason": "..."}
```

Settings → Diagnostics → Channel health → What happened then says "switched stream (arrTV
stuttered)", or "own stream for a device" for §8.3.

## 11. Checklist

- [ ] Capabilities call on playlist add/refresh and app start; cached per playlist.
- [ ] Device UUID made once, stored privately, excluded from Drive sync and Android backup.
- [ ] Device name sent (system name or user-set).
- [ ] Headers on the Ktor client and on the Media3 data source for live playback.
- [ ] Query parameters instead of headers wherever a URL is handed to something else to play.
- [ ] `X-Dispatch-Previous-Channel` only on a user channel change, naming the channel that
      is actually playing (not the last one asked for), in the id form used to open it.
- [ ] Multiview: one session id per open Multiview, on every tile's request; tile channel
      change sends previous.
- [ ] Connection closed when playback is left.
- [ ] "Send a report" as the first entry of the hold-OK options, only when `reports: true`;
      the channel, the player's state and error, and the tail of the player log.
- [ ] `X-Dispatch-Max-Video` with the device headers: the tallest decodable height, from
      `MediaCodecList`, worked out once.
- [ ] Stutter: on each live rebuffer after the first frame, `POST /api/core/app-stall/` with the
      channel and the device header, only when `stall_switch: true`; nothing to do with the answer.
- [ ] `change_stream` answered 409 (the device is on a stream of its own): a refused step, not an
      error to show.
- [ ] After a server-side stream change, a picture frozen for a few seconds with bytes arriving:
      re-prime, the new connection opened before the old one is dropped.
- [ ] A stall after the first frame reaches a rescue (today only a stall before it does).
- [ ] Nothing changes against a stock server (capabilities 404).

Questions about the server side: the implementation is `apps/proxy/live_proxy/app_devices.py`,
`apps/proxy/live_proxy/app_reports.py`, `apps/proxy/live_proxy/app_stalls.py`, `apps/proxy/live_proxy/app_own_streams.py`
and `leave_previous_channel` / `viewer_from_request` in `apps/proxy/live_proxy/probation.py`
of https://github.com/ckegels/dispatch-more.


## Recordings: commercials (v238, arr.69)

arrTV sends `custom_properties.comskip = true` on `POST /api/channels/recordings/` when "Remove
commercials" is on (Settings -> DVR, on by default from arr.69). From v238 the server runs
Comskip after such a recording even when its own DVR Comskip switch is off; before, the flag
was ignored. Series rules have no such flag: the server's switch decides for them.

**Marked breaks** (v239): with the server's Comskip in "mark" mode a completed recording
carries `custom_properties.comskip = {"status": "completed", "mode": "mark", "breaks": [[300.0,
480.5], ...]}` (seconds from the start of the file). `GET /api/core/capabilities/` says
`commercial_breaks: {"installed": bool, "enabled": bool, "mode": "cut"|"mark", "marks": bool}`.
arrTV fetches `GET /api/channels/recordings/<id>/` when a recording starts playing.

## Subtitles (v243, v244, arr.72)

- **Broadcast subtitles** need nothing from the server: arrTV shows DVB, CEA-608/708 and, from
  arr.72, **teletext** subtitle pages (Settings -> Player -> Teletext Subtitles, on by default;
  its own TsExtractor reader for descriptor 0x56, `teletext/TeletextMedia3.kt`). Stream Check
  records per stream what it carries (`stream_stats.subtitles`), listed on the Subtitles tab.
- **Captions made from the sound** (`fork/subtitles.md`): v244 adds the caption worker and its
  installer on the server only. Nothing reaches arrTV yet; the delivery (an `app-captions`
  endpoint and socket messages carrying text with the stream's PTS, so arrTV can show them in
  step with the picture) is step 3b, designed in `fork/subtitles.md` §4.4 and §7.3.
