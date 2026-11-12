"""Try a provider account that refuses every connection last, until it sends data again.

A provider can go on publishing its playlist while refusing every stream: on 2026-11-09 and
again on 2026-11-12 both of the user's TiviBridge accounts answered each connection by closing
it before any data (HTTP 456), while their playlists refreshed fine. Their streams come first
in many channels, so every channel start asked them three times per stream, with
probation.refusal_retry_delay's 1.5 s and 3 s in between, before reaching the account that
works: 12 seconds of black at every start, measured on the Shield, and arrTV's own 12-second
wait ran out at the same moment and switched streams on top of the server's.

So an account whose connections were refused on REFUSED_STREAMS different streams, with no
data from it in between, is marked for MARK_SECONDS. While it is marked:

- its streams are tried after every other account's, still before the channel's custom streams
  at the end (the "Could Not Dispatch" fallback stays last): `last`, used by
  Channel.get_stream and by failover's alternates;
- a refused connection to it gives that stream up at once instead of after max_retries
  attempts: `note_refused` says so;
- "use another account" does not move a viewer to it (probation.reserve_alternate_slot).

Nothing is flagged and nothing is taken out: when the other accounts are full the marked one
is still tried, and the first data from any of its connections clears the mark
(`note_served`). After MARK_SECONDS it is tried in its place again.

Only accounts with Channel Switch Overlap on take part, like refusal_retry_delay: anything
else, and a server where no account uses the overlap, behaves exactly like stock. Costs one
Redis lookup per channel start and per connection when on, and none when off.
"""

import logging
import time

logger = logging.getLogger("live_proxy")

# Different streams of one account refused, with no data in between, before it is marked
REFUSED_STREAMS = 2
# How long a refused stream counts toward that
WINDOW_SECONDS = 600
# How long a mark lasts when nothing clears it
MARK_SECONDS = 600
# {account id: when the mark ends}
MARKS_KEY = "live:refusing:marks"
# Set of the stream ids of an account refused lately
REFUSED_KEY = "live:refusing:refused:{account_id}"
# Present while anything above may exist, so a connection that sends data costs one lookup
ANY_KEY = "live:refusing:any"


def _int(value):
    try:
        return int(value.decode() if isinstance(value, bytes) else value)
    except (TypeError, ValueError, AttributeError):
        return None


def marked_accounts(redis_client):
    """Ids of the accounts marked now."""
    if not redis_client:
        return set()
    try:
        from . import probation

        if not probation.in_use():
            return set()
        now = time.time()
        marked = set()
        for account_id, until in (redis_client.hgetall(MARKS_KEY) or {}).items():
            try:
                if float(until.decode() if isinstance(until, bytes) else until) > now:
                    marked.add(_int(account_id))
            except (TypeError, ValueError):
                pass
        marked.discard(None)
        return marked
    except Exception as e:
        logger.debug(f"Could not read the accounts refusing every connection: {e}")
        return set()


def last(redis_client, items, account_of, is_custom):
    """
    `items` (streams, or failover's alternates) with those on a marked account moved after
    the others, before the custom streams the list ends with. Otherwise in their order.
    Returns `items` itself when no account is marked.
    """
    marked = marked_accounts(redis_client)
    if not marked:
        return items
    try:
        items = list(items)
        end = len(items)
        while end > 0 and is_custom(items[end - 1]):
            end -= 1
        head, tail = items[:end], items[end:]
        late = [item for item in head if account_of(item) in marked]
        if not late:
            return items
        return [item for item in head if account_of(item) not in marked] + late + tail
    except Exception as e:
        logger.debug(f"Could not put the streams of a refusing account last: {e}")
        return items


def last_alternates(redis_client, alternates):
    """Failover's alternates ({"stream_id", ...}) through `last`."""
    if not alternates or not marked_accounts(redis_client):
        return alternates
    try:
        from apps.channels.models import Stream

        rows = {
            stream_id: (account_id, is_custom)
            for stream_id, account_id, is_custom in Stream.objects.filter(
                id__in=[a["stream_id"] for a in alternates]
            ).values_list("id", "m3u_account_id", "is_custom")
        }
        return last(
            redis_client,
            alternates,
            lambda a: rows.get(a["stream_id"], (None, False))[0],
            lambda a: rows.get(a["stream_id"], (None, False))[1],
        )
    except Exception as e:
        logger.debug(f"Could not order failover's streams by refusing accounts: {e}")
        return alternates


def note_refused(redis_client, stream_id):
    """
    A connection to `stream_id` was closed before any data. Counts it, marks the account once
    REFUSED_STREAMS of its streams were refused, and says whether the stream should be given
    up now (its account is marked) instead of being asked again.
    """
    if not redis_client or not stream_id:
        return False
    try:
        from . import probation

        account = probation.stream_account(stream_id)
        if account is None:
            return False
        key = REFUSED_KEY.format(account_id=account.id)
        redis_client.sadd(key, int(stream_id))
        redis_client.expire(key, WINDOW_SECONDS)
        redis_client.set(ANY_KEY, "1", ex=max(WINDOW_SECONDS, MARK_SECONDS))
        if account.id in marked_accounts(redis_client):
            return True
        refused = redis_client.scard(key) or 0
        if refused < REFUSED_STREAMS:
            return False
        redis_client.hset(MARKS_KEY, account.id, time.time() + MARK_SECONDS)
        redis_client.expire(MARKS_KEY, MARK_SECONDS)
        probation.record_event(
            redis_client,
            None,
            "provider refusing every connection",
            account=account.name,
            result=f"its streams are tried last for {MARK_SECONDS // 60} minutes, "
            f"or until it sends data again",
        )
        logger.warning(
            f"The provider of account {account.name} refused {refused} different streams "
            f"without sending any data; its streams are tried after the others' for "
            f"{MARK_SECONDS}s or until it sends data"
        )
        return True
    except Exception as e:
        logger.debug(f"Could not count a refused connection for stream {stream_id}: {e}")
        return False


def note_served(redis_client, stream_id):
    """The first data of a connection to `stream_id` arrived: its account works."""
    if not redis_client or not stream_id:
        return
    try:
        if not redis_client.exists(ANY_KEY):
            return
        from . import probation

        account = probation.stream_account(stream_id)
        if account is None:
            return
        redis_client.delete(REFUSED_KEY.format(account_id=account.id))
        if redis_client.hdel(MARKS_KEY, account.id):
            probation.record_event(
                redis_client,
                None,
                "provider sends again",
                account=account.name,
                result="its streams are tried in their place again",
            )
            logger.info(
                f"The provider of account {account.name} sent data again; its streams are "
                f"tried in their place again"
            )
    except Exception as e:
        logger.debug(f"Could not clear the refusals of stream {stream_id}: {e}")
