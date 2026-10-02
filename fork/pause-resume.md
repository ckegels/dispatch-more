# Pause for as long as you like: resuming where it was paused

Planned 2026-10-02, against release v247 and arrTV arr.87. **Server rewind's server side (§4.3) is
built (v248):** `apps/proxy/live_proxy/rewind.py`, `rewind_views.py`, the arrTV settings page;
arrTV's side and the fixes of §4.2 are next.

## 1. The problem

The user (2026-10-02): pausing live TV and coming back 20 minutes later plays **live**, not from
the pause. It should continue from where it was paused, however long the pause.

## 2. How pause works today

- **Live TV**: arrTV's Live Rewind (`core/timeshift/TimeshiftController`) records the channel into
  a ring on the device while it plays -- 30 minutes deep by default (Settings -> Player -> Live
  Rewind depth, `liveRewindDepthMinutes`), fed by its own connection to the live proxy (the
  "filler"). Pause plays from that ring; resume continues in it.
- **Look back**: a catch-up session on the server; a pause keeps it alive by arrTV's position
  reports every 20 s (fixed in v246 / arr.73, `fork/HANDOVER.md` §5.7c).
- The Shield's settings (checked over adb): screensaver on, screen-off after 2 h.

A 20-minute pause fits a 30-minute ring, so the ring's length alone does not explain it.

## 3. What can send a resume to live (to confirm first)

1. **The filler stopped during the pause.** This provider's live streams show gaps of ~10 s
   without bytes every half minute (arrTV's `[FEED] gap 9801ms without bytes`); if the filler's
   connection dies or is closed, the ring's head freezes. On resume the player reaches the frozen
   head, errors, and `AerioExoPlayerHolder`'s timeshift error triage ("stalled at the frozen
   head ... must go LIVE") calls `goLive()` -- by design, and the reason the user sees live.
2. **The paused position fell off the ring** (depth set lower than the pause, or the ring counts
   wall time the filler spent reconnecting) -- triage re-enters at the ring's tail, or goes live
   after two tries.
3. **The app was stopped**: the screensaver or standby taking over while paused (keep-screen-on
   may be released while paused), the activity stopping, the player and the ring session released
   -- resume starts the channel afresh, live.
4. **The server closed the filler's channel** while nothing else watched it and the filler stopped
   reading.

## 4. Plan

### 4.1 Find out (first)

Reproduce on the Shield: live channel, pause, 20 and 30 minutes, resume -- with arrTV's log
recorded live from the start (`adb logcat --pid`, since the ring buffer drops arrTV's lines,
`.personal/CHANGES.md` notes) and the server's log for the channel. Which of §3 it is decides the
fix; likely more than one.

### 4.2 Fixes by cause

- **Filler died (1)**: the filler reconnects by itself with the ring kept (marked as a
  discontinuity, as channel flip-back already does), and the head keeps advancing; resume past a
  frozen head goes to the **paused position**, never live, as long as that position is in the
  ring -- `goLive()` only when the ring holds nothing at or after it.
- **Fell off the ring (2)**: the ring grows with the pause: while paused it keeps at least the
  pause length plus a margin, up to the disk budget (the depth setting is how far you can rewind,
  not how long you can pause); when the budget is reached, the oldest part goes, and the resume
  says so ("Paused too long: continuing from 1 h ago").
- **App stopped (3)**: while paused with a ring, the screen is kept on until the screensaver's own
  time (the user may still want it), and if the activity stops anyway, the ring session is kept
  (as `liveRewindKeepRecent` keeps recent channels) and the resume re-enters it at the paused
  wall time.
- **Server closed the channel (4)**: the filler is a normal client of the live proxy and reads
  continuously; if the server closes it, that is a server bug to fix there.

### 4.2b Longer than the TV can hold (the user, 2026-10-02: "might have been longer than 30 min")

The TV's ring is bounded by its disk: the Shield had **1.0 GB free** (12 GB, 92 % used) on
2026-10-02, and 30 minutes of HD is 1-2 GB -- on that box the free space, not the depth setting,
may be what ends a pause (so it is the first thing §4.1 checks: `enforceBudget` and the free
space while paused). Beyond what the ring can hold, in this order:

1. **Resume through look back.** When the channel has a provider archive (catch-up days > 0 on
   any of its streams -- 24KITCHEN, Food Network UK on Digitalizard, ...), resume at the paused
   wall time as a look-back session (`playCatchup` at that time, the programme's info from the
   guide), with the Live button to jump back to live. Any pause length within the archive (days)
   then resumes exactly where it stopped, and nothing has to be kept on the TV at all. The ring
   is still used while it holds the position (instant, no provider connection).
2. **No archive: resume from the oldest moment still held**, saying so on screen ("Paused 52
   min; the TV kept the last 30 -- continuing from 30 min ago"), never silently live.
3. **The server's own recording** (§4.3, server rewind) -- the user's choice (2026-10-02),
   since the TVs do not have the space.

The ring's growth while paused (§4.2) stays within the disk budget; on a full box it simply
stops growing and (1) or (2) takes over.

### 4.3 Server rewind: the server keeps the recording (the user, 2026-10-02: "devices don't have enough space")

The TV's disk is what ends a long pause (the Shield: 1 GB free). The server has the disk, already
has the stream, and can keep it for every TV at once.

**What it is.** While a channel is watched, Dispatch More records it to the server's disk as an
**HLS event stream**: `ffmpeg -c copy` (no re-encoding, a few % of one core) reading the channel
from its own proxy -- one more client of the channel the TV already plays, no provider
connection of its own, the way the caption worker reads -- writing 6-second segments, each
stamped with its wall time (`#EXT-X-PROGRAM-DATE-TIME`), into `/data/rewind/<channel>/`
(`DISPATCHARR_REWIND_DIR`; a Docker volume like `/data` already is). ffmpeg cuts on keyframes and
marks discontinuities (`#EXT-X-DISCONTINUITY`) itself, which is what the device's ring and the
look-back archives struggle with.

**How arrTV uses it.** Instead of its own ring, arrTV plays
`/proxy/ts/rewind/<uuid>/index.m3u8` when the user pauses or rewinds: ExoPlayer's HLS player
handles pause of any length, seeking anywhere in the window, the jump back to live and the
discontinuities -- all things it already does well for HLS. Resume after an hour is just
"unpause". The live picture stays on the normal stream until the user pauses or rewinds, so
zapping is not slowed down.

**When it records.**
- While any TV watches the channel (so rewinding into the minutes *before* the pause works too),
  keeping the last **N minutes** (setting, default 60).
- While a paused or rewinding viewer is behind live, it keeps recording and keeps everything from
  that viewer's position on, up to a **maximum pause** (setting, default 4 h).
- After the last viewer leaves (and no paused viewer needs it), it stops after a grace of a few
  minutes (a quick flip back still finds the recording), and the files go.
- One recording per channel, shared by every TV watching it.

**Disk.** About 1-1.5 GB per hour for an SD / 720p channel, 3-4 GB for 1080p, 6-8 GB for 4K. A
**disk budget** (setting, default 20 GB, never more than the disk's free space minus a margin):
over it, the oldest unused minutes of the least-watched channel go first; a channel a paused
viewer needs is the last to be trimmed. The arrTV settings page shows what is recorded and its
size.

**Provider connections.** None extra while someone watches. A **paused** viewer alone keeps the
channel open (the recorder is a client), so the provider's connection stays in use during the
pause -- exactly as the TV's own ring does today. It counts as "in use" for Stream Check and for
look-back priority (`fork/lookback-priority.md`: a paused rewind is not moved). Force Close and the
viewer checks pass the recorder by, as they do the caption worker (`is_caption_client`), and a
viewer changing channel stops the old channel's recording unless someone paused it.

**Works with the rest.**
- Channels with a provider archive: a pause longer than the maximum (or after a restart of the
  server) still resumes through look back (§4.2b 1).
- Captions (3b) can read the recording instead of the live stream, and translation can run a few
  seconds behind without the viewer seeing it.
- Docker and Linux alike: ffmpeg is in both, the folder is under `/data`.

**Switch.** "Server rewind" on the arrTV settings page (Dispatch More), with the minutes, the
maximum pause and the disk budget; arrTV uses it when the server offers it and falls back to its
own ring otherwise (and when switched off on the TV). Off = stock: nothing is recorded.

**Built in v248 (server side).**
- `rewind.watch(uuid, viewer, paused_at)` from `POST /api/channels/rewind/<uuid>/` (any signed-in
  user; viewer = `<user id>:<device>`, kept 45 s): starts the recorder in this worker unless the
  lease `rewind:owner:<uuid>` is held (one recorder per channel across workers). `DELETE` leaves,
  `GET` gives the window `{enabled, recording, tail_wall_ms, head_wall_ms, playlist}`.
- `Recorder`: ffmpeg (`-c copy`, video and audio, `-f hls -hls_time 6`, `program_date_time`,
  numbered segments `p<part>-NNNNNN.ts` -- named by the second, the start burst overwrote them),
  errors to `recorder.log` (a pipe nobody reads fills and stalls ffmpeg), dies with its worker
  (`PR_SET_PDEATHSIG`); a restart is a new part. The supervisor (every 5 s): restarts ffmpeg while
  TVs watch (at most 20 times), stops it at once when none does (it holds the channel and its
  provider connection open), deletes the folder 5 min later, trims to `keep_from_ms` (the last
  `rewind_minutes`, or a paused TV's position minus 30 s, within `rewind_max_pause_minutes`), keeps
  the budget (`enforce_budget`: `rewind_budget_gb`, never more than free space minus 5 GB; oldest
  segments of channels nobody is paused on first) and its lease.
- `GET /proxy/ts/rewind/<uuid>/index.m3u8` (network access as for streams): an EVENT playlist built
  from the parts' own playlists, only segments still on disk, `#EXT-X-PROGRAM-DATE-TIME` each,
  `#EXT-X-DISCONTINUITY` between parts; segments served from `/proxy/ts/rewind/<uuid>/<name>`
  (names checked). `GET /api/channels/rewind/` (admin): what is recorded, for the settings page.
- Force Close passes the recorder by (User-Agent `DispatchMore-Rewind/1`,
  `captions.is_caption_client` now covers both helpers).
- Settings (`app_devices.DEFAULTS`): `rewind` (on), `rewind_minutes` 60 (5-240),
  `rewind_max_pause_minutes` 240 (15-1440), `rewind_budget_gb` 20 (1-4000); folder
  `DISPATCHARR_REWIND_DIR` (default `/data/rewind`), proxy `DISPATCHARR_INTERNAL_URL` (default
  `http://127.0.0.1:9191`).
- Checked with a real ffmpeg against a served sample: segments, parts, the playlist, stop and
  clean-up when the viewer goes. Tests: `apps/proxy/live_proxy/tests/test_rewind.py`.

**Order of work.** (1) The recorder and its housekeeping (start / stop / trim / budget), with the
HLS window served under `/proxy/ts/rewind/`; tests with a sample stream. (2) arrTV: pause and
rewind switch to the HLS window and back to live. (3) The settings page and the disk view.
(4) Captions reading from the recording (optional).

### 4.4 Look back

Check the same 20- and 30-minute pause on a look back (the session's position reports keep it
alive; the provider's archive connection may still be closed by the provider -- then resume
re-mints at the paused position, which `commitScrubCatchup` already does for a seek).

## 5. Switch

Resume-at-pause is what pause means; no switch for that. The longer ring while paused uses the
existing Live Rewind switch and depth.
