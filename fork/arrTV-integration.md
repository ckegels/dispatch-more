# arrTV ↔ Dispatch More: telling the server which device you are

For the developer of **arrTV** (the AerioTV-Android fork). This describes what arrTV sends so
that a **Dispatch More** server (a fork of Dispatcharr 0.31, v197 and later) knows which
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
  "headers": {
    "device": "X-Dispatch-Device",
    "device_name": "X-Dispatch-Device-Name",
    "multiview": "X-Dispatch-Multiview",
    "previous": "X-Dispatch-Previous-Channel"
  },
  "query_parameters": {
    "device": "dm_device",
    "device_name": "dm_device_name",
    "multiview": "dm_multiview",
    "previous": "dm_previous"
  }
}
```

- `devices`, `multiview` and `switch_hints` are the server admin's switches (Settings →
  Diagnostics → Channel switches → "Apps that say who they are"). **Both are off by default.**
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
| Catch-up / timeshift / VOD | no |

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

## 7. Behaviour matrix

| Server | Capabilities | What arrTV does | Result |
|---|---|---|---|
| Stock Dispatcharr | 404 | sends nothing new | exactly as today |
| Dispatch More, switches off | 200, `devices: false` | sends headers | ignored: exactly as today |
| Dispatch More, `devices` on | 200, `devices: true` | sends headers | each device is its own viewer, Multiview safe, names in Diagnostics |
| Dispatch More, `switch_hints` on | 200, `switch_hints: true` | also sends previous on zaps | old channel closed at once, faster switching |

## 8. Testing without the app

With both switches on in Diagnostics → Channel switches, from two terminals with the same login:

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

## 9. Checklist

- [ ] Capabilities call on playlist add/refresh and app start; cached per playlist.
- [ ] Device UUID made once, stored privately, excluded from Drive sync and Android backup.
- [ ] Device name sent (system name or user-set).
- [ ] Headers on the Ktor client and on the Media3 data source for live playback.
- [ ] Query parameters instead of headers wherever a URL is handed to something else to play.
- [ ] `X-Dispatch-Previous-Channel` only on a user channel change, with the id form the app
      used to open the old channel.
- [ ] Multiview: one session id per open Multiview, on every tile's request; tile channel
      change sends previous.
- [ ] Connection closed when playback is left.
- [ ] Nothing changes against a stock server (capabilities 404).

Questions about the server side: the implementation is `apps/proxy/live_proxy/app_devices.py`
and `leave_previous_channel` / `viewer_from_request` in `apps/proxy/live_proxy/probation.py`
of https://github.com/ckegels/dispatch-more.
