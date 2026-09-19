# Is TiviBridge's saved stream address the one the proxy opens? Prints hosts only, never a login.
from urllib.parse import urlparse
from apps.channels.models import Stream
from apps.proxy.live_proxy.url_utils import _resolve_live_stream_url

s = (Stream.objects.filter(m3u_account__name="TiviBridge", channels__isnull=False)
     .select_related("m3u_account").first())
if s is None:
    print("No TiviBridge stream on a channel found")
else:
    p = s.m3u_account.profiles.filter(is_active=True).order_by("-is_default").first()
    live = _resolve_live_stream_url(s, s.m3u_account, p) or ""
    host = lambda u: urlparse(u or "").netloc.split("@")[-1]
    path = lambda u: urlparse(u or "").path.rsplit("/", 1)[-1]
    print("account type:", s.m3u_account.account_type, "| stream id:", s.stream_id)
    print("saved host:", host(s.url), "| file:", path(s.url))
    print("proxy host:", host(live), "| file:", path(live))
    print("same address:", s.url == live)
