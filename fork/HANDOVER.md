# Handover: the "probation-slots" fork of Dispatcharr

Everything someone continuing this work needs to know: what the fork is, the rules it is
built under, how it is tested and installed, every feature and why it is the way it is,
what was measured on the real installation, the mistakes made and what they taught, and
what is still open.

Written 2026-09-19, at patch v98 (commit `f0ceb68a`). The commit messages on the branch
are the detailed record of each change (`git log bcbb68c4..HEAD`); this file is the map.
The design of the first feature is in `docs/channel-switch-overlap.md`.

---

## 1. What this is

A fork of [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr), an IPTV proxy/manager
(Django 5 + DRF + Celery + Redis + Postgres backend, React + Mantine 8 frontend), kept by
one user for their own installation.

- **Branch:** `feature/probation-slots`
- **Based on:** upstream **v0.31.0**, commit `bcbb68c4`. Every patch is the full diff from
  there.
- **Delivered as:** a patch file, `~/probation-slots-vNN.patch`, installed on the user's
  server with `fork/scripts/install-probation.sh`. Latest: **v98**.
- **Shows itself as:** `v0.31.0+mod` in the sidebar and About (see §5.9).

The user's setup (matters for almost every decision):

- About **1360 channels**, grouped by country (`┃AT┃ AUSTRIA`, `┃UK┃ NEWS`…), names with a
  country box (`┃AT┃ ORF 1`).
- **Every channel ends in a custom "Could Not Dispatch" stream** (from the could-not-dispatch
  plugin): a fallback screen shown when every real stream fails. It must stay last and never
  be removed (see §3).
- Two accounts at one Xtream Codes provider, named **TiviBridge** and **TiviBridge2**
  (server `line.one-zone.cc`; despite the name, not a local bridge), each effectively
  **1 connection**.
- **Plex** and **Jellyfin** watch through Dispatcharr's HDHomeRun tuners.
- The server: a **Debian LXC at `192.168.2.142`** (Dispatcharr in `/opt/dispatcharr`,
  installed with upstream's `debian_install.sh`, virtualenv at `/opt/dispatcharr/.venv`, UI
  on port 9191). Services: `dispatcharr` (uWSGI, gevent, 4 workers), `dispatcharr-celery`,
  `dispatcharr-celerybeat`, `dispatcharr-daphne`. Server clock is UTC.

---

## 2. Rules the user set (keep them)

1. **Never push anything to the official repo.** The `upstream` remote's push URL is set to
   `no_push_to_official_repo`. No PRs there without the user's explicit say-so.
2. **With a feature switched off, Dispatcharr must behave exactly like stock.** "I do not
   want to be the reason other people their systems break." New features default to off;
   hooks into stock code paths must cost nothing when off (typically one cheap check).
3. **No migrations.** Fork state lives in `CoreSettings` rows (JSON), M3U account
   `custom_properties`, and Redis. The install is a plain file patch with no `migrate` step,
   and uninstalling leaves the database readable by stock.
4. **No fake provider** for testing ("I can test it myself with my providers").
5. **Comments explain *why*, in plain sentences,** in the style already used throughout the
   fork. Commit messages are written the same way, and describe the change for a reader.
6. **Never disturb a viewer.** Anything opening provider connections in the background must
   never cost someone their stream (§5.8 — this was broken once and the user was rightly
   upset).
7. **Defaults that reproduce what the user already trusts** (the Channel Manager's defaults
   are DispatcharrUtils', §5.7); anything smarter is opt-in.
8. Every change: tests, full suites green, patch built and checked against stock (§4).

---

## 3. Things that must stay true

- **The fallback stream stays last.** Anything writing `ChannelStream` keeps custom
  streams, keeps them last, inserts new streams before them, and deletes only streams
  explicitly marked for removal. Test with a fallback on the channel.
- **Threads never touch the database** in Stream Check: the batch reads everything before
  starting the per-provider threads and writes after they end (a thread's DB connection is
  separate; in tests it cannot even see the test data, and writes from it escape the test
  transaction).
- **A refusal is only the provider's when another stream of the same provider is refused
  too.** Otherwise the refused stream is dead (§5.8, the Euronews case).
- **The install scripts reset every file any patch version ever touched**, not just the
  current patch's (§4.3).

---

## 4. Working on it

### 4.1 Building a patch

```bash
git diff bcbb68c4..HEAD -- . ':(exclude)CLAUDE.md' ':(exclude)fork' > ~/probation-slots-vNN.patch
git checkout -q bcbb68c4 && git apply --check ~/probation-slots-vNN.patch && echo APPLIES_CLEANLY
git checkout -q feature/probation-slots
```

`CLAUDE.md` and `fork/` are kept out of the patch: they are for people working on the fork,
not for the server.

### 4.2 Installing on the server

```bash
scp ~/probation-slots-vNN.patch root@192.168.2.142:/root/
ssh root@192.168.2.142 'bash /root/install-probation.sh /root/probation-slots-vNN.patch'
```

When `fork/scripts/install-probation.sh` or `uninstall-probation.sh` changes, copy it to
`/root/` as well.

### 4.3 The scripts in `fork/scripts/`

| Script | What it does |
|---|---|
| `install-probation.sh` | Resets every file any patch version ever touched (the embedded `EVER` list plus `/root/.probation-slots-files` from the last install) to stock v0.31.0 from GitHub, applies the patch, `npm run build`, stops the 4 services, clears leftover connection slots in Redis, starts them. Makes a tar backup of the files first. |
| `uninstall-probation.sh` | The same reset without applying anything: back to stock v0.31.0 (tested byte-for-byte). Settings stay in the database; stock ignores them. The About box tells users to run it before reporting a problem. |
| `dispatcharr-shell.sh` | Runs `manage.py` with the service's environment. **Needed**: the Debian install keeps the DB password in the systemd unit's `Environment=` lines, so a plain `manage.py shell` over SSH fails with "password authentication failed for user dispatch". Use: `bash /root/dispatcharr-shell.sh shell < script.py` |
| `check-channel.py` | `NAME=euronews bash dispatcharr-shell.sh shell < check-channel.py`: every stream of every channel matching NAME — what Stream Check has on record, a fresh check, a 10 s recording judged for black/frozen/silent. Viewer-safe. |
| `probe-provider.py`, `probe-pace.py`, `probe-recovery.py`, `compare-stream-url.py` | The measurements of §6.4. |
| `verify-probation.sh` | Read-only verification of the tuner, guide, fork endpoints, Redis, Plex and Jellyfin (`DISP_PASS=… PLEX_TOKEN=… bash verify-probation.sh`). |

When regenerating the install script's `EVER` list, use
`git log --name-only --pretty=format: bcbb68c4..HEAD | sort -u | grep -v '^fork/' | grep -v '^CLAUDE.md$'`.

### 4.4 Tests

A local environment was set up in a session scratch directory (not in the repo): a Python
venv with the backend requirements, Postgres on **127.0.0.1:55432** (via the `pgserver`
pip package), and a node venv for the frontend. Recreate the equivalent, then:

```bash
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 DJANGO_SECRET_KEY=x DISPATCHARR_ENV=aio \
  DISPATCHARR_LOG_LEVEL=WARNING python manage.py test --noinput            # full: ~65 s
# one module:  ... manage.py test --keepdb apps.channels.tests.test_stream_check
cd frontend && npx vitest run      # ~6500 tests
npx eslint <files>                 # api.js has 14 errors that are stock's own
```

- **Baseline:** the full backend run always ends `FAILED (errors=26)` — all 26 are
  `PermissionError: '/data'` from stock tests that expect Docker paths. Compare the list of
  errors against a saved baseline, not the count alone. Use `grep -a` on the log.
- Tests use in-test fake Redis classes (no Redis needed); Stream Check's `FakeRedis`
  supports strings, counters, hashes, sorted sets and `scan_iter`.
- Probe tests use a real local HTTP server and real ffmpeg-made video (skipped without
  ffmpeg/ffprobe).
- Frontend tests render real Mantine and the real `CustomTable`; Mantine `Select` needs
  `Element.prototype.scrollIntoView = vi.fn()` in jsdom.

---

## 5. The features

All backend fork code is in `apps/proxy/live_proxy/` and `apps/channels/`; frontend in
`frontend/src/components/{mediaservers,diagnostics,tables,forms}` and `frontend/src/pages`.

### 5.1 Channel Switch Overlap ("probation slots") — `apps/proxy/live_proxy/probation.py`

The original feature. The user's provider allows 1 stream per account but tolerates 2
briefly. With `max_streams=1` switching channels is slow (the new request waits for the old
slot, up to 3 s, then 503); with 2, another viewer can take the second slot and the provider
kills a stream.

Design (approved 2026-09-13; full detail in `docs/channel-switch-overlap.md`): when all
profiles are full, a viewer switching channels starts the new channel on a temporary
"overlap" slot past the limit; when the old stream ends within the window it is confirmed,
otherwise resolved. Per M3U account in `custom_properties` (`probation_enabled`,
`probation_seconds`, …), exposed in the M3U form. Grew: Stop Skipped Channels (a surfing
viewer's momentary channels closed first), held slots during a switch, "When Switching
Channels" (follow order / same account / another account), LAN Subnets for recognising
devices, a settings page and switch log. Identity: IP + Dispatcharr user + device, and for
media servers the device the server reports (`bind_device`, with `wait_for_device`,
`device_a_moment_ago`, … as fallbacks). The user rejected identity-based "grace" and
"sticky accounts".

Upstream issues: #1694 (the user's own "probation-slots" request), #1600 (configurable retry
budget, a simpler related idea). CONTRIBUTING.md: PRs target `dev`, need an agreed issue.

### 5.2 Media Servers (Plex, Jellyfin) — `media_servers.py`, `media_server_views.py`, `media_server_tuner_views.py`

A settings tab that talks to Plex and Jellyfin: who is watching which channel (sessions,
matched to Dispatcharr channels by name/programme — Plex names the programme, so the EPG
says which channel), stop a session (never a recording), manage the HDHomeRun tuners on the
server (add, move to a new address, tuner count, channel enabling, guide), and reload their
guides after an EPG refresh.

Plex facts (measured, contradict the forums — see §6.1): one DVR per server in practice,
one guide (lineup) **per channel source**, the exact API sequence Plex's own settings use,
`PUT devices/{id}?uri=` is ignored (moving a tuner = add new + remove old, slowly: a quick
sequence crashed Plex with SEGV once), channel scanning is async (poll for the channels
before enabling them or you get "(69) 0 enabled").

Cached logos (`?cachedlogos=`) default to **off** for media servers: the server hands the
logo URL to the player, and a private address fails off the network.

### 5.3 Stream Recovery — `recovery.py`

A provider closing a working connection (rotation) is not treated as a failure, within a
budget per hour; "Maximum retry attempts" was lowered on the user's setup (987 → 5). On for
media servers.

### 5.4 Diagnostics and Channel health — `diagnostics_views.py`, `health.py`, `timing.py`

Settings → Streaming → Diagnostics: channel starts timed phase by phase (with what the media
server did after the handover), channel switches (the overlap's log), and Channel health:
**Running now** as a card per channel (provider/account, stream, picture/sound, what is
arriving, last switch, error, every viewer), **What happened** (recovery events), and
**Stopped** channels with their last minutes of readings. Readings are sampled by the proxy's
cleanup loop (`health.sweep`, one worker at a time via a Redis lock, `scan_iter` not KEYS).
Speed only exists when ffmpeg runs (a stream profile other than Proxy). Every section is read
on its own: one bad record is logged and left out rather than 500-ing the page (it did,
because switch records sometimes carry a channel id or "None" instead of a UUID).

### 5.5 Find Logos — `apps/channels/logo_library.py`, `logo_library_views.py`, `LogoLibraryTable.jsx`

A tab of the Logo Manager: each channel's logo next to suggestions from public collections
(tv-logo/tv-logos, iptv-org), the user's own added collections, their playlists' and every
guide's icons — collections first, guides last. Search every logo by hand, use a link or a
file. Matching uses `match_key` (accents folded, "+"/"&" as words, box removed).

### 5.6 Channel Manager: Merge — `apps/channels/channel_manager.py`, `ChannelManagerTable.jsx`

Merges the same channel from every provider and quality into one, with a before/after table
and a Watch button per stream. **Defaults reproduce DispatcharrUtils** (kpirnie's tool, and
the user's own `merge_group.py`): the whole name, only a quality at the end removed, case
ignored, no tvg-id (providers share tvg-ids between different channels — it merged Krone into
Euronews and every CBS station into one), no country guessing, every same-named channel gets
the stream. Loose matching etc. are levers. Streams can be reordered on the page.
`DEFAULTS_VERSION` resets saved settings when defaults change. A backup warning with a link
to Settings → Backup & Restore sits above both tabs.

### 5.7 Channel Manager: Stream Check — `apps/channels/stream_check.py` (+ `stream_check_views.py`, `StreamCheckTable.jsx`, `StreamCheckSettings.jsx`, `ProviderLimits.jsx`)

The big one; its module docstring is the specification. Finds the streams on channels that
no longer play. Summary of how it works now:

- **Rounds in batches.** A round (every `every_hours`, optional night window, or "Check all
  now") is worked through in ~4-minute Celery batches that queue the next, so M3U/EPG
  refreshes are not starved (the Debian install has one Celery worker pool). A tick every 5
  minutes (`stream_check_tick` in `CELERY_BEAT_SCHEDULE`) starts due rounds, rechecks, and
  resumes a paused one; a paused round retries after 60 s, or when a resting provider is due.
- **Per provider, never on one in use.** A provider = every account sharing a server host, a
  login (credential fingerprint) or a server group, keyed by its server name. One check at a
  time per provider, all providers in parallel. Before every stream and while reading: anyone
  watching through that provider (live channels via `stream_profile:*`, VOD, counters) → it
  is left alone; Xtream providers are also asked `active_cons` (at most every 30 s). A viewer
  starting on it drops the check at once; a viewer finding the provider full calls
  `make_way` (hook in `live_proxy/views.py`) and gets the connection. `only_when_idle` (off)
  checks nothing while anything plays.
- **Opens the stream exactly as the proxy does** (`url_utils._resolve_live_stream_url`: for
  Xtream, current credentials + stream id, not the saved URL), with the account's User-Agent,
  taking a connection slot like a viewer (`reserve_profile_slot`), 3 s apart per provider.
- **A picture, not an answer.** Reads the stream, follows HLS to a segment, ffprobe for
  video; with the picture check (on, 6 s) ffmpeg `blackdetect`/`freezedetect` and a 16×9 grey
  fingerprint of one frame.
- **Failure kinds:** `dead` (does not play), `refused` (the provider refuses this stream while
  giving others — e.g. HTTP 407 for Euronews HD), `black`, `frozen`, `placeholder` (the same
  frozen frame on ≥3 channels of one provider: its "no stream" card). Broken after
  `broken_after` failures in a row. **Only `dead` may be autoparked**; the rest show as
  "needs you".
- **Accounts first:** expired logins (exp_date), Xtream login refused/not active → account
  left for the round; the first 5 streams of an account all failing → provider down, not
  counted.
- **Provider limits learned** (`_Budget`): a refused stream is compared with a stream that
  played on that provider (or the next in line). Both refused → the provider is at its limit:
  count/span recorded, provider left completely alone, retried after 30 s…60 min until it
  plays, then limit = 80 % of the count per (span + block). Shown/editable/forgettable in
  settings; limits set by hand kept; `LIMITS_VERSION` drops old learned ones.
- **Rechecks:** failing streams again every `recheck_hours` (3) or after each successful
  playlist refresh of their provider (hook in `apps/m3u/tasks.py`, recorded in
  `stream-check-recheck`). **Autopark** (off): `dead` `autopark_after` (3) checks in a row →
  parked; autoparked streams go back by themselves when they play.
- **Park** = off every channel, remembered with position, still checked, put back before the
  fallback. The Merge leaves parked streams out. Remove = off the channel only (the stream is
  the provider's). The fallback is never checked, parked or removed.
- **Stored:** CoreSettings `stream-check` (settings, `SETTINGS_VERSION`), `stream-check-results`,
  `stream-check-parked`, `stream-check-providers` (limits), `stream-check-recheck`; Redis
  `stream-check:*` (round, progress, run lock, stop, make-way, queued, live results, opens).
  Times are sent to the page as ISO moments and shown in the viewer's zone (server is UTC).

### 5.8 Misc

- **Modified-build label:** `version.py` has `__build__ = "mod"`; `/api/core/version/` returns
  it; sidebar/About show `v0.31.0+mod`. `__version__` stays `0.31.0` (it is in the default
  User-Agent sent to providers and compared by the update check).
- **About box** says: not official Dispatcharr, uninstall with `bash /root/uninstall-probation.sh`
  and try stock first, do not report on the official GitHub/Discord.
- **Phone layout:** our tables scroll inside their own box with wrapping toolbars instead of
  forcing the page 900 px wide; Diagnostics tabs stack on narrow screens.

---

## 6. Measured on the real installation (do not re-derive)

1. **Plex:** one guide per channel source, the add-channel-source API sequence (in §5.2 and
   the commit "What the server's own settings do, taken from its log"), a server accepts
   more DVRs through the API than its settings show (only one is usable). An earlier 404 on
   `PUT /livetv/dvrs/{id}/lineups` was a deleted DVR id, not a missing endpoint.
2. **Plex start-up glitch:** Dispatcharr hands over the first video in 1.2 s (keyframe in the
   first chunk). The ~5 s glitch after that is Plex's transcoder starting cold; a bigger
   initial burst did not help. The lever is a native client (direct play), not Dispatcharr.
3. **Stream Recovery:** with rotation not counted, retry ratio fell from 23 % to 4 %.
4. **TiviBridge / `line.one-zone.cc`:** a closed connection is off its `active_cons` 0.1 s
   later; three checks in a row all play. A run at 16/min stopped at stream 34 with HTTP 407 —
   which was most likely **a dead channel**, not a rate limit: the provider answers dead
   channels (e.g. `┃AT┃ EURONEWS HD`, `┃USA┃ EURONEWS HD`) with 407 every time, while playing
   everything around them. Later runs learned "limits" of ~600 streams (483 per 101 min and
   454 per 186 min kept) with a known-good stream refused too; plausible but not independently
   confirmed.
5. **The Debian install's DB password** is only in the systemd unit (see `dispatcharr-shell.sh`).

---

## 7. Mistakes made, and what they taught

- **Stream Check disturbed viewers** (v84/v85): it only trusted Dispatcharr's counters per
  login, missing one login under two accounts and people in other apps. → provider-level
  isolation, asking the provider, `make_way`.
- **Every 407 treated as "provider full"** (v86–v95) hid dead channels forever. → compare with
  another stream of the provider; refused-while-others-play is the stream's failure.
- **A limit "learned" from one dead channel.** → learn only when a known-good stream is refused
  too; `LIMITS_VERSION` discarded the old ones.
- **Install script only reset the current patch's files** → `/output/m3u` 500'd for a while
  (an old `apps/output/views.py` stayed). → reset the union of everything ever touched.
- **Channel Manager defaults smarter than DispatcharrUtils** merged the wrong channels. →
  DispatcharrUtils parity by default.
- **Plex:** flip-flopped on one guide vs one DVR before measuring; an endpoint was wrongly
  declared missing from a 404; a fast tuner move crashed Plex. → measure first, move slowly.
- **Times in the server's zone** on the page looked two hours off. → send moments, format in
  the browser.

---

## 8. Open / possible next

- Stream Check has not yet completed a full real round on v97/v98 with the picture check;
  watch the first one (runtime is ~6× longer per stream with it).
- Viewer channel opens are not counted against a learned provider limit (a 20 % margin covers
  normal zapping).
- An animated "no stream" card is not recognised (would need OCR or known-card fingerprints).
- Plex recording detection (Jellyfin has timers; Plex: `GET /livetv/dvrs/{id}/recordings`
  while recording, not yet looked at).
- Channel Manager phase 2 ideas: fuzzy matching, East/West, rule sets per group, run after M3U
  refresh, undo.
- Offered earlier, not built: stream reliability ranking, fd-leak check (#1674), channel-death
  notifications.

---

## 9. Where the rest of the history is

- `git log bcbb68c4..HEAD` — 89+ commits, each message a full explanation.
- `docs/channel-switch-overlap.md` — the overlap's design.
- The original conversations with Claude Code are kept on the user's machine under
  `~/.claude/projects/-home-ckegels-Documents-github-dispatcharr-tsi/` (`*.jsonl`), with
  Claude's memory notes in its `memory/` folder. They are not in the repo.
