# Dispatcharr fork: probation-slots

A personal fork of Dispatcharr on branch `feature/probation-slots`, based on **v0.33.0**
(`2fa13a67`, `fork/patcher/BASE`; merged into `feature/probation-slots` on 2026-11-01, not pushed yet --
before 2026-11-01 v0.31.0, `bcbb68c4`), delivered as releases of the patcher. **Read `fork/HANDOVER.md` before changing
anything**: it holds the features, the reasoning, what was measured on the real server, and
the mistakes not to repeat.

## Rules

- **Never push to the official repo** (`upstream`; its push URL is disabled on purpose). No
  PRs there without the user's explicit say-so.
- **Off means stock.** With a fork feature switched off, Dispatcharr must behave exactly like
  the Dispatcharr version it is built on. New features default to off; hooks into stock code
  cost nothing when off.
- **Install and uninstall must leave Dispatcharr's own files exactly as they were**, also when
  Dispatcharr was updated under a release in between (handover-2026-11-01 §3): test-patcher.sh's
  U and P scenarios, `fork/patcher/test-real.sh` and `fork/patcher/test-docker.sh` check it.
- **No migrations.** State lives in `CoreSettings` JSON rows, M3U account `custom_properties`
  and Redis.
- **No fake provider** for testing; the user tests with real providers.
- **Never disturb a viewer.** Background work that opens provider connections (Stream Check)
  must never cost anyone their stream, and never touches a provider someone is using.
- **Never flag a stream for something that is not the stream's.** No connection, a timeout, a
  server error, a refusal while others play, a fade: each is looked at again in the same run
  before it counts, or never counted. A working channel called broken is the worst bug this
  fork can have — it has happened, more than once (handover §7).
- **Every channel ends in a custom fallback stream** ("Could Not Dispatch"): keep it last,
  never remove it, insert new streams before it.
- **Channel Manager recognition is one choice, "How hard to look"**: Thorough (every rule and
  Dispatcharr's language model) by default -- the user's decision, 2026-09-29 -- with Exact
  still reproducing DispatcharrUtils. Other Lineup defaults still follow DispatcharrUtils.
- **Run the server tests on a test database of your own** (`POSTGRES_DB=dispatcharr_claude`):
  another session in this repository shares the default one, and two runs at once break each
  other (missing tables, deadlocks). Redis must be running (handover-2026-11-01 §4). Baseline on
  0.33: 28 errors, 26 `/data` + 2 `posix_spawn` setsid, the same as stock 0.33 here.
- **Show Groups' copies are not the Channel Manager's**: anything that lists channels to change
  leaves out `show_groups.live.copy_group_ids()` (handover §7b).
- **Comments and commit messages explain *why*, in plain sentences**, like the existing fork
  code. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Every change

1. Tests for it; full backend suite (baseline: `FAILED (errors=28)`: 26 `/data`
   PermissionErrors, 2 `posix_spawn` setsid) and frontend `npx vitest run` green, and
   `npm run build` (a merge once passed every test and did not build).
2. Commit. Ship it as a release of the patcher (the way it is installed now):

   ```bash
   fork/patcher/release.sh vNN             # builds the overlay and runs fork/patcher/test-patcher.sh
   fork/patcher/release.sh vNN --publish   # and makes the GitHub release (gh is logged in as ckegels)
   ```

   Releases run from v99; v275 is the latest (v253-v257 and v259 were test builds, never published; v275 is the last for
   Dispatcharr 0.31.0, v276 will be the first for 0.33.0). Users install one with
   `curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash`,
   or the same inside `docker exec` for Docker; `... | sudo bash -s -- --uninstall` takes it off with the
   newest release's uninstaller.

   The user installs a release with `sudo bash dispatch-more/install.sh` (Linux/LXC) or the
   Docker entrypoint; see README.md. The old patch file still works for the user's own server
   while it is on the patch-based install:

   ```bash
   git diff $(cat fork/patcher/BASE)..HEAD -- . ':(exclude)CLAUDE.md' ':(exclude)fork' ':(exclude)README.md' ':(exclude).github' > ~/probation-slots-vNN.patch
   ```

3. **Keep every md file up to date after each edit** (the user, 2026-10-01): `fork/HANDOVER.md`
   (and its "kept current to" line), `README.md`, this file, `fork/subtitles.md`,
   `fork/arrTV-integration.md`, `docs/channel-switch-overlap.md` and any design file the
   change touches -- in the same commit as the change, not afterwards.
4. Never let the overlay or a patch carry `CLAUDE.md`, `README.md`, `fork/` or `.github/`: they
   are for the repository, not for servers.

## Where things are

- `fork/HANDOVER.md` — the full context. `fork/patcher/` — how it is built, installed,
  uninstalled and tested (§4 of the handover). `fork/scripts/` — the old patch-based install
  scripts and the server-side diagnostics (`dispatcharr-shell.sh` runs `manage.py` over SSH).
- The public name is **Dispatch More** (a working name, in `version.py` `__build__` and the
  patcher's `NAME`/`SLUG`). Public repository: **https://github.com/ckegels/dispatch-more**, a
  GitHub fork of Dispatcharr, remote `origin`, default branch `feature/probation-slots`.
  Releases go there with `fork/patcher/release.sh vNN --publish` (it names the fork in every gh
  command). Only the `fork-*` workflow is enabled there; upstream's are disabled on purpose.
- `docs/channel-switch-overlap.md` — design of the first feature.
- Fork backend: `apps/proxy/live_proxy/` (probation, media servers, health,
  diagnostics with the memory recorder -- memory_recorder.py runs apart as a service of its own
  (`fork/patcher/memory-recorder.sh`), inflight.py, memory_history.py -- and arrTV: app_devices, app_reports, app_stalls, app_own_streams,
  app_alternatives, app_guides, app_one_device, app_links, app_sync) and `apps/channels/` (logo_library, channel_manager,
  pairings, guide_manager, guide_layout, epg_grabber, stream_check, show_groups/,
  subtitles, captions/ -- the caption worker runs apart, in its own venv or container).
- `fork/handover-2026-10-02.md` — **the work in progress (2026-10-02): read it first**; where
  things stand, what is half done, v244-v249 and arrTV arr.73-arr.89 in detail.
- `fork/subtitles.md` — subtitles, captions and translation: design and what is built.
- `fork/lookback-priority.md`, `fork/pause-resume.md` — designs (2026-10-02); both server sides are
  built (v248: `apps/timeshift/priority.py`, `apps/proxy/live_proxy/rewind.py`).
- `fork/arrTV-integration.md` — the contract with arrTV (the user's Android TV app);
  `fork/arrTV-guide-choice.md` and `fork/picture-check.md` — designs.
- `git log bcbb68c4..HEAD` — every change, explained in its message (the 0.33.0 merge is
  `a1749363`).
- `fork/handover-2026-11-01.md` — **the move to Dispatcharr 0.33.0: read it first**; what upstream
  changed that the fork had, how each was settled, upgrading across Dispatcharr versions, what is
  open.
