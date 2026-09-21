# Dispatcharr fork: probation-slots

A personal fork of Dispatcharr v0.31.0 on branch `feature/probation-slots` (base commit
`bcbb68c4`), delivered to one server as a patch. **Read `fork/HANDOVER.md` before changing
anything**: it holds the features, the reasoning, what was measured on the real server, and
the mistakes not to repeat.

## Rules

- **Never push to the official repo** (`upstream`; its push URL is disabled on purpose). No
  PRs there without the user's explicit say-so.
- **Off means stock.** With a fork feature switched off, Dispatcharr must behave exactly like
  v0.31.0. New features default to off; hooks into stock code cost nothing when off.
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
- **Channel Manager defaults reproduce DispatcharrUtils**; anything smarter is a lever.
- **Comments and commit messages explain *why*, in plain sentences**, like the existing fork
  code. Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Every change

1. Tests for it; full backend suite (baseline: `FAILED (errors=26)`, all `/data`
   PermissionErrors) and frontend `npx vitest run` green.
2. Commit. Ship it as a release of the patcher (the way it is installed now):

   ```bash
   fork/patcher/release.sh vNN             # builds the overlay and runs fork/patcher/test-patcher.sh
   fork/patcher/release.sh vNN --publish   # and makes the GitHub release (gh is logged in as ckegels)
   ```

   Releases run from v99; v164 is the latest. Users install one with
   `curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash`,
   or the same inside `docker exec` for Docker.

   The user installs a release with `sudo bash dispatch-more/install.sh` (Linux/LXC) or the
   Docker entrypoint; see README.md. The old patch file still works for the user's own server
   while it is on the patch-based install:

   ```bash
   git diff bcbb68c4..HEAD -- . ':(exclude)CLAUDE.md' ':(exclude)fork' ':(exclude)README.md' ':(exclude).github' > ~/probation-slots-vNN.patch
   ```

3. Never let the overlay or a patch carry `CLAUDE.md`, `README.md`, `fork/` or `.github/`: they
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
  diagnostics) and `apps/channels/` (logo_library, channel_manager, guide_manager,
  guide_layout, stream_check).
- `git log bcbb68c4..HEAD` — every change, explained in its message.
