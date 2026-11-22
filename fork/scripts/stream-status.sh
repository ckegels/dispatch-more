#!/bin/bash
# How each provider's streams are doing, read-only: run inside the Dispatcharr LXC (or on the
# Proxmox host with `pct exec 145 -- bash -s < stream-status.sh`). Changes nothing.
#
# Per M3U account: on or off, whether the live proxy has it marked as refusing every connection
# right now (refusing.py; only with Channel Switch Overlap on), what Stream Check's last look at
# its streams found (states, the newest stream that worked, the most common reasons), and how
# often the last 24 hours of the log mention it refusing (HTTP 456/403/401/429).
set -euo pipefail
cd /opt/dispatcharr
set -a
[ -f .env ] && . ./.env
set +a
while IFS= read -r line; do
  [ -n "$line" ] && export "$line"
done < <(systemctl show dispatcharr -p Environment --value | tr ' ' '\n')
PY=.venv/bin/python
[ -x "$PY" ] || PY=env/bin/python
echo "Dispatcharr: $(curl -s localhost:9191/api/core/version/)"
echo "Server time: $(date '+%Y-%m-%d %H:%M %Z')"
echo
REFUSALS="$(journalctl -u 'dispatcharr*' --since '24 hours ago' --no-pager 2>/dev/null \
  | grep -E ' (456|403|401|429)\b' | grep -oE 'account[^0-9]{0,12}[0-9]+|m3u_account[_ ]id[=: ]+[0-9]+' \
  | grep -oE '[0-9]+$' | sort | uniq -c | awk '{print $2"="$1}' | paste -sd, - || true)"
export REFUSALS
"$PY" manage.py shell 2>/dev/null <<'PY'
import os
from collections import Counter, defaultdict

from apps.channels import stream_check
from apps.channels.models import Stream
from apps.m3u.models import M3UAccount
from apps.proxy.live_proxy import refusing
from core.utils import RedisClient

redis_client = RedisClient.get_client()
marked = refusing.marked_accounts(redis_client)
settings = stream_check.load_settings()
stored = stream_check.load_results()
results = stored["streams"]
ids = [int(i) for i in results if str(i).isdigit()]
account_of = dict(Stream.objects.filter(id__in=ids).values_list("id", "m3u_account_id"))
refusals = dict(
    pair.split("=") for pair in (os.environ.get("REFUSALS") or "").split(",") if "=" in pair
)

states = defaultdict(Counter)
reasons = defaultdict(Counter)
newest_ok = defaultdict(str)
newest_check = defaultdict(str)
for stream_id, record in results.items():
    account = account_of.get(int(stream_id)) if str(stream_id).isdigit() else None
    state = stream_check.state_of(record, settings)
    states[account][state] += 1
    if state != "ok":
        reasons[account][(record.get("reason") or record.get("kind") or "?")[:60]] += 1
    newest_ok[account] = max(newest_ok[account], record.get("last_ok") or "")
    newest_check[account] = max(newest_check[account], record.get("checked_at") or "")

last_run = stored.get("last_run") or {}
print(f"Stream Check: {'on' if settings.get('enabled') else 'off'}; last run {last_run.get('finished_at') or last_run.get('started_at') or '?'}")
print()
for account in M3UAccount.objects.order_by("name"):
    counts = dict(states.get(account.id, {}))
    print(f"[{account.id}] {account.name}: {'active' if account.is_active else 'OFF'}"
          f"{' -- MARKED refusing every connection now' if account.id in marked else ''}")
    print(f"    streams on channels: {Stream.objects.filter(m3u_account=account, channels__isnull=False).distinct().count()}"
          f"; refusals in the log, 24 h: {refusals.get(str(account.id), 0)}")
    if counts:
        print(f"    Stream Check: {counts}; newest check {newest_check[account.id] or '-'};"
              f" newest stream that worked {newest_ok[account.id] or 'never'}")
        for reason, n in reasons[account.id].most_common(3):
            print(f"      {n} x {reason}")
    else:
        print("    Stream Check: none of its streams looked at")
PY
