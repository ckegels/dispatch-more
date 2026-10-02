# Pause for as long as you like: resuming where it was paused

Planned 2026-10-02, against release v247 and arrTV arr.87. Nothing here is built yet.

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
3. **The server's own ring** (§4.3) for channels without an archive, if 1 and 2 are not enough
   for the user.

The ring's growth while paused (§4.2) stays within the disk budget; on a full box it simply
stops growing and (1) or (2) takes over.

### 4.3 A server-side alternative (only if the device cannot hold it)

Dispatch More keeping a per-channel ring itself (on disk, minutes to hours, wall-time addressed),
which any player can resume from (`/proxy/ts/stream/<uuid>?at=<wall>`): the pause would survive the
device and the app entirely. Much bigger (disk on the server, per-channel recording while
watched); worth it only if §4.2 cannot cover the cases the user hits.

### 4.4 Look back

Check the same 20- and 30-minute pause on a look back (the session's position reports keep it
alive; the provider's archive connection may still be closed by the provider -- then resume
re-mints at the paused position, which `commitScrubCatchup` already does for a seek).

## 5. Switch

Resume-at-pause is what pause means; no switch for that. The longer ring while paused uses the
existing Live Rewind switch and depth.
