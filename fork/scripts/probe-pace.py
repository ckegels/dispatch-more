# Does a provider stop answering when streams are opened quickly one after another? Opens up
# to COUNT streams with GAP seconds between them, the way Stream Check does, and stops at the
# first refusal with what the provider said. Prints no login. Run only while nobody watches
# through this provider.
import logging
import os
import time

logging.getLogger("apps.m3u.credentials").setLevel(logging.WARNING)  # it logs the login

from apps.channels import stream_check
from apps.channels.models import Stream

ACCOUNT = os.environ.get("ACCOUNT", "TiviBridge")
GAP = float(os.environ.get("GAP", "1"))
COUNT = int(os.environ.get("COUNT", "60"))

streams = list(
    Stream.objects.filter(m3u_account__name=ACCOUNT, channels__isnull=False, is_custom=False)
    .select_related("m3u_account").distinct()[:COUNT]
)
account = streams[0].m3u_account
login = account.profiles.filter(is_active=True).order_by("-is_default").first()
agent = account.get_user_agent_string() or ""
print(f"{ACCOUNT}: {len(streams)} streams, {GAP} s apart")

started = time.monotonic()
for number, stream in enumerate(streams, 1):
    found = stream_check.probe(stream_check._url_for(stream, login), agent, 12)
    minutes = (time.monotonic() - started) / 60
    rate = number / minutes if minutes else 0
    print(f"#{number:3} {'plays ' if found['ok'] else 'FAILED'} {found['seconds']:4.1f} s"
          f" | {rate:4.0f}/min | {found.get('reason') or found.get('resolution') or ''}")
    if found.get("refused"):
        print(f"\nRefused after {number} streams in {minutes:.1f} min ({rate:.0f} a minute).")
        break
    time.sleep(GAP)
else:
    print(f"\nNo refusal: {len(streams)} streams in {(time.monotonic() - started) / 60:.1f} min.")
