# Channel Switch Overlap

Status: prototype on branch `feature/probation-slots` (not submitted upstream).
Internal name in code: *probation slots* (`apps/proxy/live_proxy/probation.py`).

## Problem

Many IPTV providers allow one stream per account, but tolerate a second connection
for a few seconds while a player switches channels. Dispatcharr cannot express that:

- **Max Streams = 1:** when every account is busy, a channel switch waits for the old
  stream's slot to be released (up to 3 s in `stream_ts`), and fails with
  "All active M3U profiles have reached maximum connection limits" if it is not
  released in time. The old slot is only released once Dispatcharr notices the old
  client has disconnected.
- **Max Streams = 2:** the second slot is not reserved for switches. A different viewer
  takes it for as long as they watch, and the provider cuts one of the streams.

## Goal

When every account is full, let a viewer who is already watching switch channels
immediately, using one temporary extra slot on the account they are on, without
ever stopping a stream that was already playing.

## Behavior

### When the overlap is used

All of the following must be true:

1. The request is a live viewer request through the TS proxy (not a Redirect stream
   profile, not failover, not VOD or timeshift, not a DVR recording).
2. Every M3U profile the channel can use is at its limit.
3. The requesting viewer is **already watching** on one of those profiles (see
   *Recognising the same viewer*).
4. That profile's M3U account has **Allow Channel Switch Overlap** enabled.
5. The profile does not already have an overlap slot in use (one extra slot per profile).

Otherwise Dispatcharr behaves exactly as it does today.

### What happens next

The new stream starts immediately on a temporary slot (`max_streams + 1`) and is
checked every 0.5 s until the account's **Overlap Window** expires:

| Outcome | Condition | Action |
|---|---|---|
| Confirmed | A stream on that profile ended, or only channels nobody watches keep it over its limit (see *Channel Shutdown Delay*) | Nothing: the stream continues normally |
| Ended | The new channel itself stopped, or failover moved it | Nothing |
| Moved | Window expired, profile still over its limit, another profile of the channel has a free slot | The channel switches to that stream/profile (short hiccup); custom streams (such as a fallback slate) only when no provider profile is free |
| Stopped | Window expired, still over its limit, no free slot anywhere | **Only the new channel** is stopped |

Streams that were already playing are never stopped by this feature.

### Custom and fallback streams

Plugins such as could-not-dispatch attach a custom stream at the end of every channel: a
slate on the built-in `custom` account, which has no connection limit. Dispatcharr would pick
it as soon as the streams before it are full, so a channel switch would land on the slate.

- `Channel.get_stream()` tries the overlap before the first custom stream whose earlier
  streams are all full, and again after the last stream. A custom stream placed before the
  provider streams is still used first, as before.
- When the overlap does not apply (another viewer, no request viewer, anonymous not
  allowed), the custom stream is used exactly as without this feature.
- When an overlap window expires, provider streams with a free slot are tried first; a custom
  stream is the last resort before stopping the channel. That is where the channel would
  have ended up without the overlap.
- "Use another account" never moves a viewer to a custom stream.

This does not depend on the plugin: it applies to any custom stream, and nothing changes
until a channel actually has one.

### When the worker running the check restarts

The check (monitor) runs as a greenlet in the uWSGI worker that started the channel. It
renews a lease (`live:probation:monitor:<channel uuid>`, 20 s) on every check, and every
pending channel is listed in `live:probation:pending`. Every worker's proxy cleanup loop
calls `recover_unmonitored_probations()`: a probation whose lease ran out is taken over by
the first worker that claims the lease, so the extra connection does not stay open when a
worker restarts. A monitor that finds its lease taken over stops.

Moving or stopping the channel keeps the record until it succeeded. When it raises, the next
takeover tries again, at most three times; after that the record is dropped with an error in
the log.

### Stop Skipped Channels (optional)

Fast channel surfing (several channels within seconds) fills every slot: Dispatcharr
only releases a channel once it notices the player left, and each account has a single
overlap slot. With **Stop Skipped Channels** enabled on the account, an identified viewer's
(user or device ID, plus IP) *skipped* channels are stopped:

- After its new channel got a slot (normally the overlap slot, so switching is not delayed).
  The overlap slot is then free again for the next switch.
- Before retrying, if not even the overlap slot was free; the released slots are then held
  for this viewer (see *Held slots*).

The skipped channel's slot is released immediately; the rest of the stop, which waits for
the provider connection to close, runs in the background.

A channel counts as skipped when all of these are true:

- The requesting viewer is its only client (shared channels are never stopped).
- The viewer joined it within the account's Overlap Window (the earliest join counts,
  so reconnecting to a channel watched for a while does not qualify).
- Its account has both Allow Channel Switch Overlap and Stop Skipped Channels enabled.
- It is not the channel being requested.

Anonymous viewers never trigger it. Surfing A → B → C → D → E stops B, C and D as the
next channel is requested; A (watched longer) closes on its own and E is confirmed.

**Returning to a channel that is still being stopped.** Stopping takes a moment (the provider
connection has to close). A request for that channel in the meantime would get Dispatcharr's
`503 {"error": "Channel is stopping, retry shortly"}`, which players such as TiviMate show as
an error ("unrecognized format"), or would join the channel just before the stop ends it.
While `live:probation:stopping:<uuid>` is set (skipped channels, and idle channels stopped by
the overlap), `stream_ts` waits for the stop to finish (at most 5 s) and then starts the
channel again. The marker is removed when the background stop is done. Stops started any
other way (dashboard, deletes, refreshes) keep the normal 503.

Not supported (warnings in the explanation popup): a single player intentionally opening two
channels within the window (multiview / picture-in-picture) closes the first one, and two
devices sharing one login can stop each other's channels.

### Held slots

Many players close the old stream just before requesting the next channel. A request that
is already waiting for a slot (another viewer) would take the released slot in that gap and
the switch would fail. When a channel on an account with the overlap enabled releases its
slot and had exactly one viewer (identified, or anonymous where allowed), the slot is held
for that viewer for the overlap window (`live:probation:held:<profile id>`).

The hold is enforced in `reserve_profile_slot()` and `pool_has_capacity_for_profile()`, so
every way of getting a slot treats it as taken: other viewers, automatic failover, manual
stream changes, plugins, VOD, timeshift and stream previews. The viewer it is held for
(passed as `viewer=`) takes it with its next request. DVR recordings ignore holds, because
a scheduled recording must start on time. For accounts in a Server Group the shared login
counter is held as well (`live:probation:held_login:<login counter key>`), since other
accounts using the same login count against it too.

No hold is created when:

- the overlap stopped the channel because it was not a switch, or a skipped channel is
  stopped after its viewer already has its new slot;
- the channel is being stopped on purpose (from the dashboard, deleted, or its stream
  removed by an M3U refresh), or its client was disconnected from the dashboard;
- the only client is a DVR recording;
- the channel had no clients left when it released its slot (Channel Shutdown Delay above
  0: the old channel is then handled as below).

### Channel Shutdown Delay

With a delay above 0, a channel whose last client left keeps its slot until the delay has
passed. The old channel of a switch would then keep the profile over its limit, and a player
that closes the old stream first would no longer count as watching.

- Every viewer that joins a channel is remembered in `live:probation:viewers:<channel uuid>`.
  A channel waiting out the delay (no clients, `last_client_disconnect_time` set, no stop
  under way) counts as watched by a viewer when that viewer was its only one.
- While an overlap is pending and its profile is over its limit, channels on that profile
  that are waiting out the delay are stopped at once (slot released immediately, stop in the
  background). Nobody watches them, and the provider does not stay over its limit.
  The switch is then confirmed.

Channels that still have clients, or have not had a client yet (still starting), are never
stopped this way.

### DVR recordings

Recordings request channels through the proxy as `Dispatcharr-DVR/recording-<id>` from the
Dispatcharr host. They are recognised by that User-Agent and never use the overlap, account
preferences or held slots, are never matched as a viewer (not even by an anonymous player on
the same address), and a channel with a recording as its only client is not held. Recordings
on Redirect channels send the provider's User-Agent and are not recognised, but Redirect
channels do not use the overlap.

### While the feature is not used

`probation.in_use()` answers whether any active account has the overlap enabled. It is cached
in Django's (Redis) cache for 60 seconds and cleared whenever an M3U account is saved or
deleted. Every entry point checks it first, so with the overlap off on every account no
device IDs are added, nothing is stored in Redis, no slot is held and no extra database
queries run. A database error during this check counts as "not in use".

### When Switching Channels (account preference)

`probation_account_preference` chooses the account (M3U profile) for an identified viewer's
next channel. The profile the viewer is *leaving* is the one it is watching on, was last
assigned within 60 seconds (`live:probation:last_profile:<viewer>`), or has a held slot on.

- **Follow channel order** (`order`, default): normal selection by stream order. Because many
  players close the old stream first, this often returns to the same account.
- **Stay on same account** (`same`): the profile being left is tried first: a free slot, or
  the overlap slot when the viewer is still watching on it, even when another account has
  a free slot. A profile the viewer only left is never overlapped (someone else may hold it).
- **Use another account** (`alternate`): a free slot on another profile of the channel is
  tried first, so the next channel does not wait for the provider to close the old
  connection. Only accounts that have the overlap enabled themselves are used, and never
  custom streams: fallback streams such as the could-not-dispatch plugin's slate live on the
  unlimited `custom` account and would otherwise be picked on every switch. A slot held on the profile being left is released. When no other profile has
  a free slot, normal selection continues (held slot, then overlap).

The preference of the account being left decides. Anonymous viewers need Allow Anonymous
Connections; several of them behind one IP can then be kept on one account, and a new
stream that is not a switch is moved to a free account when the window expires. Earlier
builds stored "Stay On Same Account" as `probation_sticky: true`, which still reads as `same`.

## Recognising the same viewer

A stream request carries a client IP, a User-Agent and whatever is in the URL. Players
do not send device IDs or keep cookies, so identity has to come from the URL.

For every request Dispatcharr determines a viewer identity:

| Field | Source |
|---|---|
| IP | `get_client_ip()` (honours trusted reverse proxies) |
| User | Xtream login (`/live/<user>/<pass>/…`), Xtream-style M3U (`get.php`), web player session |
| Device ID | `device_id` query parameter on stream links, written by Dispatcharr's M3U output |

A requesting viewer matches an existing client when **IP, user and device ID are all
equal** (a missing user or device ID only matches a missing one).

- **Identified** viewers have a user and/or a device ID.
- **Anonymous** viewers have neither (HDHomeRun, M3U without device ID). They are
  matched on IP alone, and only on accounts that enable
  **Allow Overlap For Anonymous Connections**.

### Automatic device ID

When any M3U account has the overlap enabled, every M3U playlist Dispatcharr writes
(`/output/m3u`, `/output/m3u/<channel profile>`, `get.php`) gets a random device ID
appended to each stream link, generated per download:

```
/proxy/ts/stream/<channel uuid>?device_id=7f3a9c01b2d4
/live/<user>/<pass>/<channel id>?device_id=7f3a9c01b2d4
```

- The ID is inserted after the 2-second playlist cache, so two devices never share one.
- A fixed ID can be requested with `?device_id=<name>` on the playlist URL
  (letters, digits, `-`, `_`, up to 64 characters). Useful for players that key
  favourites on stream URLs, or to avoid a new ID on every playlist refresh.
- **Warning (shown in the explanation popup):** stream links change on every playlist
  download, so players that remember favourites by stream link can lose them. This is
  accepted; a fixed `?device_id=<name>` avoids it.
- HDHomeRun lineups get no device ID: they are always read by one media server
  (Plex, Jellyfin, Emby) on behalf of all its viewers.
- For the same reason, M3U playlists requested by a media server get no device ID. They
  are recognised by User-Agent (`jellyfin`, `emby` or `plex`, case-insensitive; defaults
  are `Jellyfin-Server/<version>`, `Emby/<version>`, `PlexMediaServer/<version>`). Stream
  requests from such a User-Agent also ignore any device ID. A custom User-Agent configured
  in the media server's tuner settings is not recognised.
- Native Xtream players (`player_api.php`) build stream links themselves, so they
  are identified by user only.
- **Warning (shown in the explanation popup):** every device that logs in with a
  Dispatcharr username and password needs its own user. A login shared by several devices
  is not supported: a request can be taken for the other device's switch, and Stop Skipped
  Channels can stop the other device's channel. A Dispatcharr login carries no device
  information (one Xtream password per user, no sessions or device tokens), so this cannot
  be detected reliably.

## Settings

Per M3U account (stored in `M3UAccount.custom_properties`, no migration):

| Setting | Key | Default |
|---|---|---|
| Allow Channel Switch Overlap | `probation_enabled` | off |
| Overlap Window (seconds, 1–120) | `probation_seconds` | 10 |
| Stop Skipped Channels | `probation_stop_skipped` | off |
| When Switching Channels | `probation_account_preference` | `order` |
| Allow Anonymous Connections (IP match) | `probation_allow_anonymous` | off |

The form only shows the toggle until it is enabled (after confirming the explanation
popup); the other settings then appear below it. "What does this do?" reopens the
explanation.

Keep Max Streams at the provider's real limit and the window below how long the
provider tolerates the extra connection.

## Coverage

### Reliable

- Xtream players with one user per device, at home or outside.
- Same user on several devices at home (distinct LAN IPs), or at different locations.
- M3U players that download their own playlist, including several devices behind one
  public IP and several devices on the same channel profile.
- A new viewer when every account is full: no match, normal limit error.

### Best effort (a wrong guess stops only the new stream after the window)

- Native Xtream players sharing one user behind one public IP.
- Jellyfin / Plex / Emby and playlist middlemen (Threadfin, m3u4u): one IP and one
  playlist for all their viewers; requires the anonymous option.
- A switch right after a playlist refresh (new device ID): no match, normal switch.
- A device changing IP during a switch (Wi-Fi ↔ mobile): no match, normal switch.
- One identity watching on two accounts: the overlap may land on the wrong one and
  is moved after the window.

### Not covered

- A reverse proxy Dispatcharr does not trust (all clients share one IP).
- A genuinely extra stream when every account is full.
- Switching away from a channel another viewer is still watching (the old slot stays
  in use; the new channel is moved or stopped after the window).
- Slow provider start-up (the overlap removes the slot wait, not provider latency).
- Two simultaneous switches on one profile (one extra slot per profile).
- Redirect stream profiles, VOD, timeshift, automatic failover and DVR recordings never
  get an overlap slot (held slots do apply to all of them except recordings).

## Alternatives considered

| Approach | Why not (alone) |
|---|---|
| Max Streams = 2 | Second slot is taken by other viewers long-term |
| Channel shutdown delay = 0 | Shortens but does not remove the wait |
| Per-user stream limit + Terminate on Limit Exceeded (existing) | Hands a user's slot over on a switch, but needs one user per device and stops the wrong stream when a login is shared; no coverage for M3U/HDHomeRun |
| Handoff (stop the viewer's old channel immediately) | Never exceeds the provider limit, but a wrong identity match stops someone else's stream |
| Identify viewers by IP only | Cannot separate devices behind one router |

Other IPTV proxies checked (Threadfin, xTeVe, Tvheadend, StreamMaster, Channels DVR)
use hard per-source limits; kvaster/iptv-proxy identifies devices by per-user playlist
URLs and releases a user's old slot before acquiring the new one. None implement an
overlap window.

## Implementation

| File | Change |
|---|---|
| `apps/proxy/live_proxy/probation.py` | Viewer identity, account settings, overlap slot, probation record, resolution (confirm/move/stop), background monitor and its recovery |
| `apps/channels/models.py` | `Channel.get_stream(viewer=…)` calls the account preferences, and the overlap before custom streams and when every profile is full; holds a released slot |
| `apps/proxy/live_proxy/server.py` | The cleanup loop resumes overlap checks whose worker restarted |
| `apps/m3u/connection_pool.py` | `reserve_profile_slot(..., extra_capacity=0, viewer=None)`; held slots count as taken in reservations and capacity checks |
| `apps/proxy/live_proxy/url_utils.py` | Pass the viewer to `get_stream` |
| `apps/proxy/live_proxy/views.py` | Build the viewer from the request, record the client's viewer and device ID, stop skipped channels, start the monitor |
| `apps/output/views.py` | Automatic device ID in M3U stream links |
| `apps/m3u/serializers.py`, `frontend/src/components/forms/M3U.jsx` | Per-account settings |

Redis keys: `live:probation:<channel uuid>` (probation record, TTL window + 120 s);
`live:probation:pending` and `live:probation:monitor:<channel uuid>` (monitor recovery);
`live:probation:held:<profile id>` and `live:probation:held_login:<login counter key>`
(held slots); `live:probation:viewers:<channel uuid>` (viewers that joined, TTL 24 h,
removed on release); `live:probation:last_profile:<viewer>`, `live:probation:stopping:<uuid>`,
`live:probation:no_hold:<uuid>`; `device_id` field on `live:channel:<uuid>:clients:<client id>`.
Django cache: `live:probation:in_use`.

Logging: every decision logs a line starting with `Probation:`. "Not used" reasons are
logged once per channel and viewer per 10 seconds, because `stream_ts` retries slot
selection several times per second while all profiles are full.

## Open questions

1. How much of today's switch delay is the slot wait versus provider start-up
   (measure: new request → old client `Disconnected after` → `Successfully obtained stream`).
2. How long common providers tolerate the extra connection.
3. Whether any popular players key favourites or EPG on stream URLs (affects the
   random per-download device ID).
