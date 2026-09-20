# Handover: the "probation-slots" fork of Dispatcharr

Everything someone continuing this work needs to know: what the fork is, the rules it is
built under, how it is tested and installed, every feature and why it is the way it is,
what was measured on the real installation, the mistakes made and what they taught, and
what is still open.

Written 2026-09-19, kept current to **release v131** (2026-09-20). The commit messages on the branch
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
- **Delivered as:** releases of the patcher, **Dispatch More vNN** (§4.4), public at
  https://github.com/ckegels/dispatch-more. One command installs one:
  `curl -fsSL .../releases/latest/download/quick-install.sh | sudo bash` (Linux/LXC), or the
  same inside `docker exec` (Docker keeps it over a restart, not over a recreate). A patch file
  (`~/probation-slots-vNN.patch`, `fork/scripts/install-probation.sh`) is still built for the
  user's own server while it is on that older way.
- **Shows itself as:** "v0.31.0 · patched" in the sidebar, "v0.31.0 · patched (Dispatch More
  vNN)" in About, a notice on every Settings page, and Settings → System → Modified build.

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

### 4.1 Building a patch (the older way, still used on the user's server until it moves to the patcher)

```bash
git diff bcbb68c4..HEAD -- . ':(exclude)CLAUDE.md' ':(exclude)fork' ':(exclude)README.md' ':(exclude).github' > ~/probation-slots-vNN.patch
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
| `trace-stream.py` | `NAME="WELT FHD" ACCOUNT=TiviBridge2 bash dispatcharr-shell.sh shell < trace-stream.py`: every step of opening one stream (DNS, TCP, each redirect hop, first bytes, the longest pause in the data, the provider's connection count before and after), then Stream Check's own check. How the burst in §6.6 was found. |
| `verify-probation.sh` | Read-only verification of the tuner, guide, fork endpoints, Redis, Plex and Jellyfin (`DISP_PASS=… PLEX_TOKEN=… bash verify-probation.sh`). |

When regenerating the install script's `EVER` list, use
`git log --name-only --pretty=format: bcbb68c4..HEAD | sort -u | grep -v '^fork/' | grep -v '^CLAUDE.md$'`.

### 4.4 The patcher (`fork/patcher/`) — how it is shipped now

The fork is published as **Dispatch More**, a public GitHub fork (AGPL-3.0, like Dispatcharr),
installed over an existing Dispatcharr by a patcher rather than a `.patch`:

- `build-overlay.sh vNN` — from a clean worktree of HEAD: stamps `__build__` with the release,
  builds the frontend, and packs every backend file the fork changes/adds/removes (not tests,
  frontend sources, `fork/`, `CLAUDE.md`, `README.md`, `.github/`), the built frontend, and a
  manifest with the **stock checksum** of every file it replaces.
- `patch.py` (called by `install.sh`/`uninstall.sh`) — refuses another Dispatcharr version
  (exit 3) or a file that is not stock (exit 4, `--force` overrides); keeps the originals in
  the state folder (`/var/lib/dispatch-more`, or `/data/dispatch-more` in Docker); upgrades by
  putting stock back first; writes `.fork-install.json` into the Dispatcharr folder for the page.
- `install.sh` — finds Dispatcharr (`/opt/dispatcharr` or `/app`, `--app` otherwise) and its
  own Python, installs a root-owned **systemd path unit** that carries out the page's Uninstall
  request, restarts the `dispatcharr*` services and clears leftover connection slots.
- `docker-entrypoint.sh` — the compose `entrypoint`: installs at every container start (so a new
  image keeps it, and an image of another version starts as stock), and carries out an
  uninstall request at the next start.
- `uninstall.sh` — puts every file back, removes added ones, restores the stock frontend, and
  takes the build's Celery beat entry (`stream-check-tick`) out of the database; the page's
  button does that last part itself (`core/modified_build.py`), since stock would otherwise log
  "unregistered task" every five minutes.
- `test-patcher.sh <release.tar.gz>` — 25 checks on real stock copies: install, again,
  refusals, `--force`, upgrade, uninstall byte-for-byte, no frontend before, and the Docker
  start script. `release.sh vNN [--publish]` builds, tests, and publishes.
- `.github/workflows/fork-upstream-check.yml` — daily: if Dispatcharr has a newer release than
  `fork/patcher/BASE`, tries the fork's changes on it and opens an issue saying whether they
  apply or which files conflict. (On 2026-09-19 they applied cleanly to upstream `dev`.) In the
  GitHub fork, disable upstream's own workflows (docker builds, releases) so only `fork-*` run.

Published 2026-09-19: **https://github.com/ckegels/dispatch-more** (public fork, remote
`origin`, default branch `feature/probation-slots`, `gh repo set-default` points at it). First
release **v100**. Upstream's workflows (tests, Docker builds, releases, PR checks) are disabled
in the fork so pushes do not build or publish images; only `fork-upstream-check` runs (checked:
it ran green on GitHub). Never push to `upstream`.

Moving the user's own server from the patch-based install to the patcher: run
`fork/scripts/uninstall-probation.sh` (back to stock), then the release's `install.sh`.

### 4.5 Tests

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

### 5.1 Channel Switch Overlap and Force Close — `apps/proxy/live_proxy/probation.py`

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

**Force Close on Identified Traffic** (`probation_stop_skipped`, the old "Stop Skipped
Channels") is a switch of its own since v104: when an identified viewer asks for a channel,
every other channel it is the only viewer of is closed at once, however long it was watched —
no window any more, because every viewer is one device. The overlap need not be on. Only a
media server's player named by a guess is held to `GUESSED_WINDOW_SECONDS` (10) until its
server says who it is (`settle_media_server_start`, `certain=True`). It has its own cached flag
(`skipping_in_use`), so the stream request checks it and not the overlap's; LAN Subnets serve
both (`account_identifies_viewers`). Switching it on warns: all traffic must be identified, one
login is one device, several devices behind one address look like one, multiview closes the
first channel.

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

### 5.4b Logs — `core/log_center.py`, `LogViewer.jsx` (Diagnostics → Logs)

Stock's Logs page shows the log collector's files, which only Docker writes; on Linux/LXC the
logs are in the systemd journal per `dispatcharr*` service. This tab reads either, narrows by
service, time, level, topic (Stream Check, proxy, overlap, M3U, EPG, media servers, VOD,
Celery) and text, keeps a traceback with the ERROR line that reported it, follows live, and
downloads the whole log or a diagnostics bundle (every log of 24 h + version). The web app's
user needs the `systemd-journal` group; the patcher's installer adds it and uninstall removes it.

### 5.5 Find Logos — `apps/channels/logo_library.py`, `logo_library_views.py`, `LogoLibraryTable.jsx`

A tab of the Logo Manager: each channel's logo next to suggestions from public collections
(tv-logo/tv-logos, iptv-org), the user's own added collections, their playlists' and every
guide's icons — collections first, guides last. Search every logo by hand, use a link or a
file. Matching uses `match_key` (accents folded, "+"/"&" as words, box removed).

### 5.6 Channel Manager: Lineup (was "Merge") — `apps/channels/channel_manager.py`, `ChannelManagerTable.jsx`

Merges the same channel from every provider and quality into one, with a before/after table
and a Watch button per stream. **Defaults reproduce DispatcharrUtils** (kpirnie's tool, and
the user's own `merge_group.py`): the whole name, only a quality at the end removed, case
ignored, no tvg-id (providers share tvg-ids between different channels — it merged Krone into
Euronews and every CBS station into one), no country guessing, every same-named channel gets
the stream. Loose matching etc. are levers. Streams can be reordered on the page.
`DEFAULTS_VERSION` resets saved settings when defaults change (`CHANGED_IN` keeps the rest). A
backup warning with a link to Settings → Backup & Restore sits above both tabs.

**Combining channels that are the same channel** (v125, `combine_duplicates`, **on** since
v126 at the user's word -- having one channel twice is what people come here to fix, and like
every other suggestion nothing happens until a row is ticked; `DEFAULTS_VERSION` 4 carries it
and the new ignore word to sets saved before). Until
then the Lineup only ever added streams to channels or made new ones -- it had never removed a
channel. With `several_matches` "all" (the DispatcharrUtils default) the same channel in two of
your groups got every matching stream **twice over**, one copy on each, which is duplication
rather than merging. On: `_duplicate_sets` finds channels sharing a match key whose countries
do not contradict (same country, or either unstated -- "┃AT┃ ORF 1" and "┃DE┃ ORF 1" are never
combined, whatever names they share; the whole fork is built on telling those apart). One
`combine` row per set: `_which_to_keep` keeps the lowest-numbered channel **already in the
group suggested for the set** (`_NewHomes.by_country`, or where most of them already are),
every member's streams go on it with the fallback still last, and the others are **deleted** on
apply. The group can be chosen on the row as a new channel's can, and choosing it changes which
channel is kept. The folded channels get no row of their own. `apply_plan` returns `combined`.
It is the only thing in the Channel Manager that deletes a channel, hence off by default, the
row naming every channel that goes, and the warning in the apply dialog.

**The mark on a recording is not part of a name** (v126): `⏺ʳᵉᶜ`, which providers put on a
stream they are recording, is in `ignore_tags` by default. `_tag_pattern` replaced the
whole-word guard for tags: that guard only means anything at an edge that is a letter or a
digit, and asking for a word boundary around `⏺ʳᵉᶜ` missed it the moment a provider wrote it up
against the name ("NPO 1⏺ʳᵉᶜ"). A bare word like RAW is still only taken whole.

**The group picker offers only groups you have channels in** (v127), from `channel_groups`
rather than `all_groups`: every group there is ran to hundreds on this setup, most of them a
provider's own names that no channel is in, and finding your own among them was the hard part.
The last entry of the list makes a new group (`API.addChannelGroup`) and chooses it for that
row, because that is where you look when none of them is the one you want.

That took two goes (v131). A group made on the page has **no channels in it yet**, so "groups
you have channels in" hid it the moment it was made -- the offered set is now groups with
channels **plus groups with neither channels nor streams**, an empty group being one somebody
made by hand while a provider's carries streams. And `API.addChannelGroup` swallows its error
and returns nothing at all, so a name already taken came back as "that group could not be
made"; a name that is already there is now chosen rather than refused, since making one is what
was asked for and having it is what was meant. (Annotations on ChannelGroup cannot be called
`channels` or `streams`: those are the relations.)

**The group is on the Before side too** (v125): you cannot judge "one channel in one group"
without seeing which groups they are in now.

**The name and the guide are set by hand on the row** (v117), in the After column: a text box
for the name -- what a new channel is called, or a rename of a channel you have -- and a window
of its own for the guide (v118; a dropdown was tried first and was the wrong shape, see below). Guide matching is in two places on purpose. The plan keeps `_Guides`, a plain
lookup (exact tvg-id, exact name) because it runs over every channel at once; it now reads the
sources in priority order and leaves out the ones switched off (an entry with no source at all
is kept: nobody switched it off). When a row's menu is opened, `guide_candidates` puts that one
channel through Dispatcharr's own matcher (`apps/channels/epg_matching.stream_fuzzy_epg_scan`,
fuzzy + `preferred-region`, no ML -- it has to answer while a menu is open), country box taken
off the name first. Only candidates at or above `MIN_GUIDE_SCORE` (40) are offered: without a
floor the three least unlike names in the file read as matches. Typing searches every guide by
name or tvg-id. Nothing is asked until the menu is opened -- with Expand all that would be a
question per row. Both travel on apply as `names` and `epgs` ({row key: guide id, or null for
"no guide" -- a choice of its own, and how a wrong match comes off), beside `groups` and
`drops`, worked out again rather than trusted from the page: a guide deleted since is applied
to nothing, a name of only spaces is no name. Conflict rows have no channel, so they have
neither.

**A match is judged, not measured** (v129, `judge_guide`, and the worst bug this matching has
had). Stock scores on `fuzz.ratio` over `normalize_name`, and `COMMON_EXTRANEOUS_WORDS`
contains **"east" and "west"**: "PBS East" and "PBS West" both come out as "pbs" and match at
**100 %**. The word that tells two channels apart was being deleted before the comparison. And
`fuzz.ratio` is character similarity, so where a broadcaster's name carries most of the letters
the one token that identifies the station barely counts: "PBS 12"/"PBS 13" scored 83, "Sky
Sports 1"/"Sky Sports 2" 92. Most PBS stations were being offered at over 90 as each other.

So: `guide_words` is the fork's own normalising, which keeps everything that says which
channel it is (nothing dropped but the country box, accents and punctuation) and reads a number
word as its number ("ORF Eins" is "orf 1"). `_identity_of` pulls out what picks one channel
from its siblings -- a number, a side (`SIDE_WORDS`), an American call sign -- and
**a contradiction ends it**: both saying a number and saying different ones is not the same
channel whatever the letters say, score 0. Then a tier rather than one number, taken from how
the epgmatcharr plugin reports its work: **CERTAIN** (the tvg-id agrees, or the same name with
the country agreeing), **LIKELY** (>= `LIKELY_SCORE` 80, country agreeing, nothing half-said),
**GUESS** (everything else). The Guides tab **suggests nothing from a guess** -- a guess is
still on the picker's list to be taken by hand, which is a different thing from putting it
forward as a change to make. `MIN_GUIDE_SCORE` went 40 → 55.

**A tvg-id is a provider's word, not proof** (v130). v129 returned CERTAIN 100 on a matching
tvg-id *before* the contradiction checks -- which is the mistake §7 already records, made
again: providers hand one id to channels that are not the same (a Krone stream carrying
Euronews', every CBS station) and leave an old id on a channel that was renamed, which is why
`match_tvg_id` is off by default in the Lineup. So the order is contradiction first (a
differing number, side or call sign refuses the match **whatever the id says**), then the id:
with a name that reads alike (>= `TVG_NEEDS_NAME` 55) it is CERTAIN, with a name that reads
nothing like it it is LIKELY and says so. That last case is a renamed channel as often as it is
a wrong id, and the two are indistinguishable from ids and names alone -- only what is on the
guide now separates them, which is why the page shows it.

**Every channel is on the list** (v129): a run keeps a row for each channel it looked at, with
`why` empty where there is nothing to suggest, and the tab's "Every channel" view shows them.
Nothing to suggest is not nothing to know -- a channel no guide fits is exactly the one
somebody goes looking for, and it was invisible in a list that only held suggestions. (A row's
`why` is the reason there is something to suggest; a candidate's `match_why` is why it is the
kind of match it is. Two questions, one word, so they have two names.)

**The country decides between guides of one name** (v122). `normalize_name` takes the country
box off before scoring, so "┃NL┃ DREAMWORKS" and a British "DreamWorks" are both "dreamworks"
and score a flat 100 -- the same channel from the wrong country, offered as a certainty. The
box is the surest thing there is about a name of this fork's, so `_by_country` adds
`SAME_COUNTRY` (10) for a guide from the country the channel says it is from and takes off
`OTHER_COUNTRY` (30) for one from a country it says it is not, with `COUNTRY_ALSO` for the
"uk"/"gb" split (playlists say UK, tvg-ids say .uk, the code is gb -- without it every British
channel is penalised against every British guide). A guide naming no country is judged on its
name alone: it may well be the right one. The matcher's own region bonus is switched off
(`region_code=None`) rather than added to this: its one preferred region is for a library where
every channel is from one place, and it reads ".uk" as a country that is not "gb". Three times
as many candidates are asked for as are shown, because a right-country one can sit below a pile
of wrongly-scored ones and has to be there to be lifted past them.

**What is on each guide, on the plan** (v122): `_fill_what_is_on` puts `now` and `programmes`
on every guide the plan names, both sides of every row, in two queries for the whole plan
rather than per row. A name is not enough to tell whether a guide is the right one; what is on
it at this moment is, and a guide holding nothing says so in orange.

**Narrowing to a group** (v122): `group_id` on every channel summary, and a group picker in the
toolbar listing only the groups the plan has something in. A new channel counts under the group
it would go into (or the one chosen for it on the page), so narrowing to a group shows what
would go into it as well as what is in it.

**The guide is shown on both sides** (v121): the Before column says which guide the channel is
on now, with its source, and the After column says the one it would come out with, in the same
words -- so the two lines read against each other. `_channel_summary` carries the source for
that (`epg_data__epg_source` is select_related with it). A card whose `in_use` the server has
not said anything about claims nothing either way: the guide a row was matched to is on the
window's list from the start, but the plan's summary does not know what it holds.

The window lives in `frontend/src/components/tables/GuidePicker.jsx` since v128 and is used
by both pages: on the Lineup it sets the guide a row would come out with, on the Guides tab it
changes what a suggestion suggests. The same decision, so the same window.

**Why the guide is a window and not a dropdown** (v118). The first try was a Mantine `Select`
and it was wrong twice over. It was wired with `searchValue` as the server query, so choosing
an entry made Mantine write that entry's label into the search box, which asked again for that
one name and emptied the list of everything else -- and Mantine filtered the list client-side
by the same value. Keep the typed search separate from the selection. Worse, a menu is the
wrong shape for the decision: two entries called "ORF 1" from two sources read the same on one
line. So each candidate is now a card in a modal, with the source, the tvg-id, the match, **how
many programmes it holds and what is on it at this moment** (`_what_they_carry`, two queries on
the index `ProgramData` already has for `epg + start_time + end_time`). That is what actually
settles it: the entry showing Zeit im Bild is the Austrian ORF 1, and one holding no programmes
is a name and nothing else, which no list of names can tell you. The guide the channel has is
passed as `current` and comes back first on every answer, whatever the search found, so what it
is now is always there to go back to -- and so it says what it holds like every other entry,
which the plan's own summary does not know.

**Holding nothing means two things, and the card must not guess** (v119). Dispatcharr reads a
guide's programmes only once something uses it: a source refresh takes every source's channel
list (`parse_channels_only`), but the programmes only for entries assigned to a channel
(`apps/channels/signals.py` `_queue_epg_program_refresh`, on assignment). So most entries on the
window held nothing and the card said "no programmes", which for a perfectly good guide is a
lie -- and it is the entries nobody has chosen yet that someone is trying to decide between.
`in_use` (does any channel use it) tells the two apart: used and empty is **"no programmes"**,
unused and empty is **"programmes not read yet"** with a button that reads them
(`load_programmes` → `POST channel-manager/guides/load/` → Dispatcharr's own
`parse_programs_for_tvg_id`, the task the assignment would have set off). It is a task, so the
page asks the list again every 2 s for 30 s and gives up rather than turning for ever. **One at
a time and only when asked**: the task reads the source's file for that one entry, and this
install has a single Celery worker -- a dozen of them set off because a window was opened would
hold up the M3U and EPG refreshes behind them. A dummy source is refused outright: it makes its
programmes up as they are asked for, so there is nothing to read.

**The reading says where it has got to** (v127). A pass of a big guide file is minutes, and a
spinner that says nothing is indistinguishable from one that has jammed.
`read_guide_programmes` writes a `guide-read:run` hash in Redis (`channel_manager.say_reading`
/ `reading_state`, `GET channel-manager/guides/reading/`): which source it is going through,
how many programmes of it have gone by (every 20 000), which guide it has just kept, and how
many of the wanted guides are done. Both places that read guides follow it -- the Lineup's
window and the Guides tab -- and they wait for as long as the task says it is still reading
rather than giving up after a fixed number of tries.

**Reading one guide costs the whole file** (v120, and the button did nothing before it).
`parse_programs_for_tvg_id` streams the source's XMLTV from beginning to end and keeps the
programmes whose `channel` is the one tvg_id asked for -- so reading one entry is as expensive
as reading the file, and a window's worth read one at a time reads it a dozen times. Hence
`apps/channels/tasks.read_guide_programmes`: **one pass of each source's file for every entry
wanted from it**, using Dispatcharr's own helpers for everything inside a programme
(`_open_xmltv_file`, `parse_xmltv_time`, `extract_custom_properties`,
`extract_season_episode_from_description`, `clear_element`) so the rows are the ones its own
parse would make, each guide's swapped in inside a transaction as its task does. A source being
refreshed is left alone (`is_task_lock_held('refresh_epg_data', source_id)` -- the file is being
rewritten). Schedules Direct is fetched rather than parsed, so it goes to Dispatcharr's task per
entry. The window has **Read all N** for the unread ones on the list, and the page asks the list
again every 3 s for three minutes before giving up.

**`force=True` is the whole point.** `parse_programs_for_tvg_id` returns without doing anything
when no channel uses the guide (`is_epg_mapped_to_channel`, `apps/epg/tasks.py`) -- and an
unused guide is the only kind this is ever asked about. v119 called it without `force`, so the
button did nothing at all, silently.

**New channels are suggested** (`create_new`, on; only suggested — nothing is made until a row
is ticked and applied). From the stream groups your channels already come from (`new_from`
"followed"; "all" is tens of thousands). Each gets a group (`_NewHomes`: where your channels
from the same stream group are → the country's usual group → the stream's own), the next free
number **after the last channel of that group** (never one taken), a logo from the collections
(`new_logo`) and the custom fallback most channels end in (`new_fallback`, `_usual_fallback`).
The group can be changed per row (`groups` on apply, renumbered there). A stream can be taken
out of a row (`drops` on apply: not added, or off the channel; never the fallback), and a
suggestion ignored (`channel-manager-ignored`: a new channel or conflict whole, for a channel
you have only those streams, so a stream added later is still suggested), with an Ignored view
and Clear ignored list. Expand all opens every row (`expandAll` on the shared table).

### 5.6b Channel Manager: Guides — `apps/channels/guide_manager.py` (+ `guide_manager_views.py`, `GuideManagerTable.jsx`)

The third tab. Dispatcharr matches a channel to a guide when asked and leaves it there,
which goes stale in ways nobody sees. This goes and looks, and suggests a change for three
different problems, each switchable on its own:

- **none** — the channel is on no guide and something fits.
- **empty** — the guide it is on holds no programmes and one that holds some fits. The one
  that matters most: an empty guide looks exactly like a working one everywhere in
  Dispatcharr except on the channel, where there is simply nothing on.
- **better** — something matches it better by a margin (`better_by`, 20). Deliberately a
  margin and not a nose: replacing a working guide because another scores one point higher
  is how a good setup gets churned for nothing. A channel on the British feed of a Dutch
  channel is this, and it is what the country scoring of v122 finds.

Scoring is the Channel Manager's (`_score_against` → `epg_matching.fuzzy_scan_epg_list`
plus `channel_manager._by_country`), over a catalogue held in memory rather than a query per
channel — over a thousand channels that difference is the whole run. **The guide a channel
is already on is scored by the same measure**, so "better" means better at being this
channel rather than a high number next to one nobody worked out.

**It says where it has got to** (v126). Reading the guide catalogue is most of a batch on a
setup with a lot of EPG and happens before a single channel is looked at, so the run writes a
`stage` to Redis before each heavy step, the channel it is on (`at`) every tenth channel rather
than once a batch, and `since` for the page's elapsed clock. A bar that only moves between
batches reads as a page that has stopped. (`since`, not `started`: `start()` answers with
`started`, and a run already going had its timestamp read as a yes.) Redis is reached through
one seam, `guide_manager.redis()`, so tests patch one place and the package's other tests
cannot leave a different client behind.

**Batched, like Stream Check** (`tasks.suggest_guides`, `BATCH_CHANNELS` 150, each batch
queues the next): one Celery worker, and a run holding it for two minutes would hold up
every M3U and EPG refresh behind it. Progress in Redis (`guide-manager:run`), a stop flag
(`guide-manager:stop`) honoured between batches. No `close_old_connections()` inside the
task — Celery's Django support already does it around every task, and by hand inside one it
closes the connection the caller is using (it broke the tests, which is how it was found).

**Unread is not empty, here either** (v124). The first cut suggested guides saying "holds
nothing" for most rows, because a suggested guide is by definition one no channel uses and
Dispatcharr has therefore never read (§5.6, the same trap). `guides_in_use` tells them apart:
a guide something uses and that holds nothing is empty and passed over
(`only_if_it_holds_something`); one nobody uses is **"not read yet"**, still offered, and read
from the page with the Lineup's one-pass-per-source reader
(`channel_manager.load_programmes`). A guide known to hold programmes is preferred to an
unread one, since it can be judged on the spot. Each row also says **what is on the guide the
channel is on now** (`instead_of_now`, `instead_of_source`) beside what is on the suggested
one, and carries the channel's `uuid` for a **watch button** -- a guide can have the right
name and the channel behind it be something else entirely.

**A suggestion can be disagreed with** (v128): every row has Change, opening the Lineup's own
guide window, and what is chosen there is what the row applies ("chosen" in place of a score).
Choosing it ticks the row. "No guide" is a choice of its own and comes back as null, so what
counts is whether a choice was made for that channel, not whether it has a value.

Applying saves each channel **one at a time with `update_fields`**, because that is what
Dispatcharr's own signal watches: it drops the guide cache and reads the new guide's
programmes. A queryset update would do neither. Waving a suggestion away is per guide, not
per channel: the guide comes off that channel's list and the next best is offered, so a
better source added later is still found. Settings and results in CoreSettings
(`guide-manager`, `guide-manager-suggestions`, `guide-manager-ignored`).

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
- **A picture, not an answer.** Reads the stream, follows HLS to a segment, ffprobe for video;
  with the picture check (on) ffmpeg `blackdetect`/`freezedetect` over `picture_seconds` (6) and
  a 16×9 grey fingerprint of one frame. The body is pumped by a thread (`_read`), so the wait is
  decided as it goes: `READ_PAUSE_SECONDS` (10) for the first piece, `PAUSE_ENDS_READ` (1.5) once
  something has come — a provider's burst is judged at once instead of waiting out its pause, and
  the socket is shut (`_hang_up`) before closing, or the close waits for that read. What came
  before a pause is always judged; nothing at all raises `_Stalled`.
- **Not every check looks at the picture.** A stream that played last time and whose picture was
  looked at within `picture_every_days` (3) gets the quick check; a failing one, a re-look and a
  check by hand always get the full look (`_picture_due`). This is most of a run's speed.
- **A picture fault is most of what was seen:** black or frozen for `PICTURE_FAULT_SHARE` (0.8)
  of the seconds read, and at least `BLACK_SECONDS` (3) / `FROZEN_SECONDS` (4). A burst can hold
  thirty seconds, in which three black ones are a fade.
- **Nothing is counted on one look.** A picture fault, a timeout, a server error (500/502/504/
  520-524) and no connection at all are suspicions: the same round opens the stream again
  `RELOOK_GAP` (90 s) later, up to `RELOOKS` (3) times (`_record`, `_relook`, `suspect` on the
  record, state "suspect"). Seen again → the failure it is; three clean looks → it plays; never
  reached at all → "not checked", counted nowhere. A round is not over while a re-look is due
  (`_relook_wait`), and a check by hand re-looks from the task.
- **Failure kinds:** `dead` (does not play), `refused` (the provider refuses this stream while
  giving others — e.g. HTTP 407 for Euronews HD), `black`, `frozen`, `placeholder` (the same
  frozen frame on ≥3 channels of one provider: its "no stream" card), `unreachable` (no
  connection at all: never counted, `_why_no_connection` says which way it failed). Broken after
  `broken_after` failures in a row. **Only `dead` may be autoparked** (`dead_streak`); the rest
  show as "needs you".
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
  fallback. A channel left with no real stream (only its fallback) is **hidden** from outputs
  (`hidden_from_output`, set by queryset update so stock's compact numbering keeps its number)
  and shown again when a stream is put back — only channels Stream Check hid itself
  (`stream-check-hidden`); on by default (`hide_emptied_channels`). The Merge leaves parked streams out. Remove = off the channel only (the stream is
  the provider's). The fallback is never checked, parked or removed.
- **Ignored and cleared:** a stream can be ignored (`stream-check-ignored`): off the list, not
  checked, nothing on its channels changed, with an Ignored view and Stop ignoring. Clear list
  forgets every result (a button on the page; blocked while a run is going).
- **The page says how long a run has left** ("about 2 h 40 min left, done around 23:10"), from
  pace samples in progress (`_note_pace`, `_eta`, `PACE_WINDOW` one hour, waits included).
- **Stored:** CoreSettings `stream-check` (settings, `SETTINGS_VERSION`), `stream-check-results`,
  `stream-check-parked`, `stream-check-providers` (limits), `stream-check-recheck`,
  `stream-check-ignored`, `stream-check-hidden`; Redis
  `stream-check:*` (round, progress, run lock, stop, make-way, queued, live results, opens).
  Times are sent to the page as ISO moments and shown in the viewer's zone (server is UTC).

### 5.8 Misc

- **Modified-build label:** `version.py` `__build__` ("Dispatch More dev", stamped "Dispatch
  More vNN" per release); `/api/core/version/` returns it. `__version__` stays `0.31.0` (it is
  in the default User-Agent sent to providers and compared by the update check).
- **Not-official warnings:** About, a notice on every Settings page, Settings → System →
  Modified build (what it is, where it comes from, the Uninstall button; `core/modified_build.py`),
  and the README. All say: try stock first, do not report on the official GitHub/Discord.
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
6. **TiviBridge2 / `line.azerty-live.cc`** (traced with `fork/scripts/trace-stream.py`,
   2026-09-19): `line.*` sits behind Cloudflare and answers a stream with a 302 to an edge
   server (192.142.x.x). DNS, connect and first video all inside 0.3 s, every try. Its edge
   **sends about 20 MB — ten seconds of video — in under a second, then nothing for 7–9 s**
   until real time catches up. That burst is why a five-second read timeout made working
   channels fail, why `_read` stops at a pause once something has come, and why a picture look
   can hold thirty seconds of video.
7. **`requests` reports a read timeout inside a body as `ConnectionError`** — which is why
   "could not connect to the provider" was shown for streams that had connected and sent video.
8. **Cloudflare 502s** ("one-zone.cc | 502: Bad gateway") happen now and then mid-run: the
   provider's own server behind Cloudflare, not the channel.

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
- **A failed connection counted as "does not play"** (to v109): a working SBS 6 4K went Broken
  after two moments the provider was unreachable, minutes before the server's disk filled.
  → `unreachable` is never counted.
- **A five-second read timeout threw away video already read** (to v111), on a provider that
  bursts. → keep what came, wait up to ten seconds, and never call that "could not connect".
- **Black/frozen measured as seconds anywhere** (to v115): "Black picture (3 of 29 s)" on a
  fade. → most of what was seen.
- **A button that did nothing, quietly** (v119): "Read them, to see" called Dispatcharr's
  per-guide task without `force`, and that task returns at once for a guide no channel uses --
  which is every guide the button is for. It logged one INFO line and looked like a slow task.
  → when reusing a stock task, read what it refuses to do before trusting it.
- **Trusting a tvg-id outright** (v129, fixed v130): the guide matching took a matching
  tvg-id as proof and answered before it had even looked for a contradiction -- the same
  mistake as the shared-tvg_id merge of §5.6, made again a year later in a new place. → an id
  a provider wrote is evidence; a name that contradicts it beats it.
- **A wrong station offered as a certainty** (to v129): stock's normalising drops "east" and
  "west" as extraneous, so "PBS East" and "PBS West" were one word and matched at 100 %, and
  character similarity let "PBS 12" and "PBS 13" reach 83. → never compare on a name with its
  distinguishing words removed, and let a contradiction end a match outright (§5.6b).
- **A dropdown that emptied itself** (v117): choosing a guide wrote its own label into the
  search box that asked the server, so every other candidate vanished. Never let a widget's
  search value double as the query. → a window with a card per candidate (§5.6).
- **The settings store dropped the build field** (to v102), so nothing on the page ever said it
  was a modified build, while every component test passed. → test through the store, not past it.
- **The install script kept a backup per install** in `/root`; ~100 of them helped fill a 20 GB
  disk until PostgreSQL stopped and every page 500'd. → keep the last three (the patcher keeps
  one set of originals). Check `df -h /` when something breaks oddly.
- **"Check again" looked like it did nothing** (to v114): the page asked once, before the check
  had started; Stop's signal ended a check by hand; a batch still going dropped it. → keep
  looking for five minutes, Stop ends rounds only, wait your turn.

---

## 8. Open / possible next

- Stream Check has not yet completed a full real round since the speed work (v113+); watch the
  pace and the estimate. The learned provider limits (483 per 101 min, 454 per 186 min) are the
  main brake and were learned under the old 407 handling: worth forgetting once to see whether
  they are learned again.
- A check by hand can only start when no batch is running (it waits up to five minutes).
- Viewer channel opens are not counted against a learned provider limit (a 20 % margin covers
  normal zapping).
- An animated "no stream" card is not recognised (would need OCR or known-card fingerprints).
- Plex recording detection (Jellyfin has timers; Plex: `GET /livetv/dvrs/{id}/recordings`
  while recording, not yet looked at).
- Channel Manager phase 2 ideas: fuzzy matching, East/West, rule sets per group, run after M3U
  refresh, undo.
- Offered earlier, not built: stream reliability ranking, fd-leak check (#1674), channel-death
  notifications, silent-audio and low-framerate checks (the IPTV Checker plugin has both),
  counting viewers' channel opens against a provider's learned limit, and a prebuilt Docker
  image (so Docker would need no patcher at all).

---

## 9. Where the rest of the history is

- `git log bcbb68c4..HEAD` — 110+ commits, each message a full explanation.
- `docs/channel-switch-overlap.md` — the overlap's design.
- The original conversations with Claude Code are kept on the user's machine under
  `~/.claude/projects/-home-ckegels-Documents-github-dispatcharr-tsi/` (`*.jsonl`), with
  Claude's memory notes in its `memory/` folder. They are not in the repo.
