# How one provider behaves when Stream Check opens streams one after another: what it answers,
# and how long it goes on counting a connection after it is closed. Prints no login.
# Run only while nobody watches through this provider.
import logging
import time

logging.getLogger("apps.m3u.credentials").setLevel(logging.WARNING)  # it logs the login

from apps.channels import stream_check
from apps.channels.models import Stream

ACCOUNT = "TiviBridge"
STREAMS = 3

streams = list(
    Stream.objects.filter(m3u_account__name=ACCOUNT, channels__isnull=False, is_custom=False)
    .select_related("m3u_account").distinct()[:STREAMS]
)
if not streams:
    raise SystemExit(f"No {ACCOUNT} stream on a channel")
account = streams[0].m3u_account
login = account.profiles.filter(is_active=True).order_by("-is_default").first()
agent = account.get_user_agent_string() or ""
print(f"{ACCOUNT}: {account.account_type}, Dispatcharr allows {login.max_streams or 'unlimited'} connection(s)")


def counted():
    if account.account_type != "XC":
        return "?"
    try:
        info = stream_check._xc_user_info(account, login, agent)
        return f"{info.get('active_cons')}/{info.get('max_connections')}"
    except Exception as e:
        return f"could not ask ({type(e).__name__})"


for number, stream in enumerate(streams, 1):
    print(f"\n#{number} {stream.name}")
    print(f"  provider counts before: {counted()}")
    started = time.monotonic()
    found = stream_check.probe(stream_check._url_for(stream, login), agent, 12)
    print(f"  check: {'plays' if found['ok'] else 'failed'} in {found['seconds']} s"
          f" | {found.get('reason') or ''} {found.get('resolution') or ''} {'(refused)' if found.get('refused') else ''}")
    closed = time.monotonic()
    for _ in range(30):
        now = counted()
        print(f"  {time.monotonic() - closed:5.1f} s after closing, provider counts: {now}")
        if now.startswith("0/") or now == "?":
            break
        time.sleep(2)
    time.sleep(3)
