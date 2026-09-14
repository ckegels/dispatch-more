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
   profile, not failover, not VOD or timeshift).
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
| Confirmed | A stream on that profile ended; the profile is within its limit again | Nothing: the stream continues normally |
| Ended | The new channel itself stopped, or failover moved it | Nothing |
| Moved | Window expired, profile still over its limit, another profile of the channel has a free slot | The channel switches to that stream/profile (short hiccup) |
| Stopped | Window expired, still over its limit, no free slot anywhere | **Only the new channel** is stopped |

Streams that were already playing are never stopped by this feature.

### Stop Skipped Channels (optional)

Fast channel surfing (several channels within seconds) fills every slot: Dispatcharr
only releases a channel once it notices the player left, and each account has a single
overlap slot. With **Stop Skipped Channels** enabled on the account, a new request from
an identified viewer (user or device ID, plus IP) first stops that viewer's *skipped*
channels, so their slots are free for the channel it wants now.

A channel counts as skipped when all of these are true:

- The requesting viewer is its only client (shared channels are never stopped).
- The viewer joined it within the account's Overlap Window (the earliest join counts,
  so reconnecting to a channel watched for a while does not qualify).
- Its account has both Allow Channel Switch Overlap and Stop Skipped Channels enabled.
- It is not the channel being requested.

Anonymous viewers never trigger it. Surfing A → B → C → D → E stops B, C and D as the
next channel is requested; A (watched longer) closes on its own and E is confirmed.

Known risks: a single player intentionally opening two channels within the window
(multiview / picture-in-picture), and two devices sharing one Xtream login behind one IP.

### Stay On Same Account (optional)

With **Stay On Same Account** enabled, a viewer's next channel prefers the account
(M3U profile) the viewer is watching on, or was last assigned within 60 seconds
(`live:probation:last_profile:<viewer>`), ahead of the channel's normal stream order:

- A free slot on that profile is used first.
- If that profile is full and the viewer is watching on it (its own old stream is still
  closing), the overlap slot is used instead of moving to another account, even when
  another account has a free slot.
- A profile the viewer only left is never overlapped (someone else may hold it).
- Otherwise normal selection continues.

Anonymous viewers need Allow Anonymous Connections; several of them behind one IP can
then be kept on one account, and a new stream that is not a switch is moved to a free
account when the window expires.

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
- HDHomeRun lineups get no device ID: they are always read by one media server
  (Plex, Jellyfin, Emby) on behalf of all its viewers.
- For the same reason, M3U playlists requested by a media server get no device ID. They
  are recognised by User-Agent (`jellyfin`, `emby` or `plex`, case-insensitive; defaults
  are `Jellyfin-Server/<version>`, `Emby/<version>`, `PlexMediaServer/<version>`). Stream
  requests from such a User-Agent also ignore any device ID. A custom User-Agent configured
  in the media server's tuner settings is not recognised.
- Native Xtream players (`player_api.php`) build stream links themselves, so they
  are identified by user only.

## Settings

Per M3U account (stored in `M3UAccount.custom_properties`, no migration):

| Setting | Key | Default |
|---|---|---|
| Allow Channel Switch Overlap | `probation_enabled` | off |
| Overlap Window (seconds, 1–120) | `probation_seconds` | 10 |
| Stop Skipped Channels | `probation_stop_skipped` | off |
| Stay On Same Account | `probation_sticky` | off |
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
- Redirect stream profiles, VOD, timeshift, automatic failover.

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
| `apps/proxy/live_proxy/probation.py` | Viewer identity, account settings, probation record, resolution (confirm/move/stop), background monitor |
| `apps/channels/models.py` | `Channel.get_stream(viewer=…)` grants the overlap slot when every profile is full |
| `apps/m3u/connection_pool.py` | `reserve_profile_slot(..., extra_capacity=0)` |
| `apps/proxy/live_proxy/url_utils.py` | Pass the viewer to `get_stream` |
| `apps/proxy/live_proxy/views.py` | Build the viewer from the request, record the client's device ID, stop skipped channels, start the monitor |
| `apps/output/views.py` | Automatic device ID in M3U stream links |
| `apps/m3u/serializers.py`, `frontend/src/components/forms/M3U.jsx` | Per-account settings |

Redis keys: `live:probation:<channel uuid>` (probation record, TTL window + 120 s);
`device_id` field on `live:channel:<uuid>:clients:<client id>`.

Logging: every decision logs a line starting with `Probation:`. "Not used" reasons are
logged once per channel and viewer per 10 seconds, because `stream_ts` retries slot
selection several times per second while all profiles are full.

## Open questions

1. How much of today's switch delay is the slot wait versus provider start-up
   (measure: new request → old client `Disconnected after` → `Successfully obtained stream`).
2. How long common providers tolerate the extra connection.
3. Whether any popular players key favourites or EPG on stream URLs (affects the
   random per-download device ID).
