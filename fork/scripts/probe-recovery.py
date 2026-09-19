# After a provider refuses (HTTP 407 after ~34 channel opens), how long until it answers
# again? Tries one stream every 15 seconds for up to an hour and prints when it plays again,
# with what the provider's account info says meanwhile. Prints no login.
import logging
import time

logging.getLogger("apps.m3u.credentials").setLevel(logging.WARNING)  # it logs the login

from apps.channels import stream_check
from apps.channels.models import Stream

ACCOUNT = "TiviBridge"
stream = (Stream.objects.filter(m3u_account__name=ACCOUNT, channels__isnull=False, is_custom=False)
          .select_related("m3u_account").first())
account = stream.m3u_account
login = account.profiles.filter(is_active=True).order_by("-is_default").first()
agent = account.get_user_agent_string() or ""

started = time.monotonic()
while time.monotonic() - started < 3600:
    found = stream_check.probe(stream_check._url_for(stream, login), agent, 12)
    try:
        info = stream_check._xc_user_info(account, login, agent)
        said = f"login {info.get('status')}, auth {info.get('auth')}, open {info.get('active_cons')}/{info.get('max_connections')}"
    except Exception as e:
        said = f"account info unavailable ({type(e).__name__})"
    waited = (time.monotonic() - started) / 60
    print(f"{waited:5.1f} min | {'plays' if found['ok'] else found['reason']} | {said}", flush=True)
    if found["ok"]:
        print(f"\nAnswering again after {waited:.1f} minutes.")
        break
    time.sleep(15)
else:
    print("\nStill refusing after an hour.")
