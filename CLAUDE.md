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
- **Every channel ends in a custom fallback stream** ("Could Not Dispatch"): keep it last,
  never remove it, insert new streams before it.
- **Channel Manager defaults reproduce DispatcharrUtils**; anything smarter is a lever.
- **Comments and commit messages explain *why*, in plain sentences**, like the existing fork
  code. Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Every change

1. Tests for it; full backend suite (baseline: `FAILED (errors=26)`, all `/data`
   PermissionErrors) and frontend `npx vitest run` green.
2. Commit, then build and check the patch:

   ```bash
   git diff bcbb68c4..HEAD -- . ':(exclude)CLAUDE.md' ':(exclude)fork' > ~/probation-slots-vNN.patch
   git checkout -q bcbb68c4 && git apply --check ~/probation-slots-vNN.patch && echo APPLIES_CLEANLY
   git checkout -q feature/probation-slots
   ```

3. Give the user the install commands:

   ```bash
   scp ~/probation-slots-vNN.patch root@192.168.2.142:/root/
   ssh root@192.168.2.142 'bash /root/install-probation.sh /root/probation-slots-vNN.patch'
   ```

## Where things are

- `fork/HANDOVER.md` — the full context. `fork/scripts/` — install/uninstall scripts and the
  server-side diagnostics (`dispatcharr-shell.sh` is needed to run `manage.py` over SSH).
- `docs/channel-switch-overlap.md` — design of the first feature.
- Fork backend: `apps/proxy/live_proxy/` (probation, media servers, health, recovery,
  diagnostics) and `apps/channels/` (logo_library, channel_manager, stream_check).
- `git log bcbb68c4..HEAD` — every change, explained in its message.
