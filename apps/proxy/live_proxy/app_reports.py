"""Error reports sent by a player app (arrTV), with the server's own view of the moment.

Someone watching a channel that stutters, stops or never starts holds OK in arrTV's player
and sends a report from its options. The app sends what it saw -- the player's state and
error, what it measured, its own log -- and the server adds everything it knows about that
channel at that moment: which stream and provider it was on, the channel's readings and
what happened to it, how it started, the switches this device made, what Stream Check has
on its streams, and the server's log lines about it. Together they are the whole story in one
place, instead of a screenshot and a guess at the time.

Off unless switched on ("reports" in app_devices' settings): a stock server has no such page.
Each report is a CoreSettings row of its own (ROW_PREFIX + its id), kept until an admin
deletes it: they are meant to be read later, by somebody else, and a report about something
that happens once a month is worth most when it is still there next month. One row each and
not one row for all: a report can carry a hundred thousand characters of log, so a single
row would be rewritten whole for every new report, and two arriving together would each
write back a list without the other. Reports kept the old way, the last 50 in one row
(OLD_KEY, to v203), are moved into rows of their own the first time the list is read.

The contract for the app is in fork/arrTV-integration.md.
"""

import json
import logging
import re
import secrets
import time

from . import app_devices

logger = logging.getLogger("live_proxy")

ROW_PREFIX = "app-report-"
OLD_KEY = "app-reports"
# A report's id as receive() makes it; anything else asked for is no report
_REPORT_ID = re.compile(r"^[0-9a-f]{12}$")
# What one report may be: a player log is useful, a whole day of one is not
MAX_TEXT = 4_000
MAX_LOG = 100_000
MAX_FIELDS = 60
LOG_MINUTES = "30m"
LOG_LINES = 400

# Credentials in anything a report carries: Xtream links (/live/<user>/<pass>/...), and
# password= or token= in a query. A report is meant to be passed on.
_XC_LINK = re.compile(r"/(live|movie|series|timeshift)/([^/\s]+)/([^/\s]+)/", re.IGNORECASE)
_SECRET_PARAM = re.compile(r"((?:password|pass|token|api_key|apikey)=)[^&\s\"']+", re.IGNORECASE)


def enabled():
    return bool(app_devices.load_settings().get("reports"))


def _redact(text):
    text = _XC_LINK.sub(lambda m: f"/{m.group(1)}/<user>/<password>/", str(text))
    return _SECRET_PARAM.sub(r"\1<hidden>", text)


def _clean(value, depth=0):
    """What the app sent, bounded and without credentials: nested at most a few levels."""
    if depth > 4:
        return None
    if isinstance(value, dict):
        return {
            str(k)[:80]: _clean(v, depth + 1) for k, v in list(value.items())[:MAX_FIELDS]
        }
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value[:MAX_FIELDS]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact(value)[:MAX_TEXT]


def _channel(given):
    """The channel the report is about, from its UUID or its number id; None if unknown."""
    from apps.channels.models import Channel

    given = str(given or "").strip()
    if not given:
        return None
    try:
        if given.isdigit():
            return Channel.objects.filter(id=int(given)).first()
        return Channel.objects.filter(uuid=given).first()
    except Exception:
        return None


def _about(items, needle):
    """The entries of a list that mention this channel, whatever the field is called."""
    if not needle:
        return []
    out = []
    for item in items or ():
        try:
            if needle in json.dumps(item, default=str):
                out.append(item)
        except Exception:
            continue
    return out


def _section(what, read, default):
    try:
        return read()
    except Exception as e:
        logger.debug(f"App report: could not read {what}: {e}")
        return default


def server_view(channel, viewer_key="", user_id=None):
    """Everything the server knows about this channel now, for a report."""
    from core.utils import RedisClient

    from . import health, probation, timing

    redis_client = RedisClient.get_client()
    uuid = str(channel.uuid) if channel else ""
    view = {"at": time.time()}
    if channel:
        view["channel"] = _section("the channel", lambda: _channel_card(channel), {})
    if redis_client and uuid:
        view["running"] = _section("running", lambda: _about(health.running_now(redis_client), uuid), [])
        view["events"] = _section("events", lambda: _about(health.recent_events(redis_client), uuid), [])
        view["stopped"] = _section(
            "stopped", lambda: _about(health.stopped_lately(redis_client), uuid), []
        )
        view["starts"] = _section("starts", lambda: _about(timing.recent_starts(redis_client), uuid), [])
    if redis_client:
        view["switches"] = _section(
            "switches",
            lambda: [
                e for e in probation.recent_events(redis_client)
                if (viewer_key and e.get("server_device") == viewer_key)
                or (user_id is not None and str(e.get("user_id")) == str(user_id))
                or (uuid and uuid in json.dumps(e, default=str))
            ][:60],
            [],
        )
    if uuid:
        view["log"] = _section("the log", lambda: _log_lines(uuid, channel), [])
    return view


def _channel_card(channel):
    """The channel and its streams in the order they are tried, with what Stream Check found."""
    from apps.channels import stream_check

    results = _section("stream check", lambda: stream_check.load_results()["streams"], {})
    streams = []
    for link in channel.channelstream_set.select_related("stream__m3u_account").order_by("order"):
        stream = link.stream
        check = results.get(str(stream.id)) or {}
        streams.append({
            "id": stream.id,
            "name": stream.name,
            "provider": stream.m3u_account.name if stream.m3u_account_id else "custom",
            "custom": stream.is_custom,
            "stale": stream.is_stale,
            "check": check.get("state") or ("not checked" if not check else ""),
            "checked_at": check.get("checked_at"),
            "stats": stream.stream_stats or {},
        })
    return {
        "id": channel.id,
        "uuid": str(channel.uuid),
        "name": channel.name,
        "number": channel.channel_number,
        "group": channel.channel_group.name if channel.channel_group_id else "",
        "streams": streams,
    }


def _log_lines(uuid, channel):
    """The server's log about this channel over the last half hour, as lines."""
    from core import log_center

    lines = []
    for needle in {uuid, channel.name}:
        found = log_center.read(since=LOG_MINUTES, text=needle)
        lines.extend(log_center.as_line(r) for r in found.get("records", ()))
    return [_redact(line) for line in sorted(set(lines))][-LOG_LINES:]


def receive(data, request, user):
    """
    Take a report from an app, add the server's view to it, and keep it. Returns it.
    """
    data = data if isinstance(data, dict) else {}
    device = str(data.get("device_id") or app_devices._said(request, "device") or "")[:64]
    user_id = getattr(user, "id", None)
    viewer_key = app_devices.device_key(user_id, device) if device else ""
    channel = _channel(data.get("channel_uuid") or data.get("channel_id"))
    log = _redact(str(data.get("log") or ""))[-MAX_LOG:]
    report = {
        "id": secrets.token_hex(6),
        "received_at": time.time(),
        "user": getattr(user, "username", "") or "",
        "user_id": user_id,
        "device_id": device,
        "device_name": str(data.get("device_name") or app_devices._said(request, "device_name") or "")[:80],
        "address": request.META.get("REMOTE_ADDR", ""),
        "what": _redact(str(data.get("what") or ""))[:MAX_TEXT],
        "happened_at": data.get("happened_at"),
        "app": _clean(data.get("app") or {}),
        "channel_asked": _clean(data.get("channel_uuid") or data.get("channel_id") or ""),
        "player": _clean(data.get("player") or {}),
        "network": _clean(data.get("network") or {}),
        "extra": _clean(data.get("extra") or {}),
        "log": log,
        "server": server_view(channel, viewer_key, user_id),
    }
    _keep(report)
    logger.info(
        f"App report {report['id']} from {report['user'] or 'nobody'} "
        f"({report['device_name'] or device or report['address']}) about "
        f"{channel.name if channel else 'no channel'}: {report['what'][:120]}"
    )
    return report


def _rows():
    from core.models import CoreSettings

    return CoreSettings.objects.filter(key__startswith=ROW_PREFIX)


def _keep(report):
    from core.models import CoreSettings

    CoreSettings.objects.update_or_create(
        key=ROW_PREFIX + report["id"],
        defaults={"name": f"App report {report['id']}", "value": report},
    )


def _move_old_row():
    """
    Reports kept the old way, all in one row, each into a row of its own; then the old row
    goes. A report already moved is left as it is, so a move cut short is finished next time.
    """
    from django.db import transaction

    from core.models import CoreSettings

    old = CoreSettings.objects.filter(key=OLD_KEY).first()
    if not old:
        return
    with transaction.atomic():
        value = old.value if isinstance(old.value, dict) else {}
        for report in value.get("reports") or []:
            if isinstance(report, dict) and _REPORT_ID.match(str(report.get("id") or "")):
                CoreSettings.objects.get_or_create(
                    key=ROW_PREFIX + report["id"],
                    defaults={"name": f"App report {report['id']}", "value": report},
                )
        old.delete()
    logger.info("App reports: moved the reports kept in one row into a row each")


def get_report(report_id):
    """One whole report, or None."""
    if not _REPORT_ID.match(str(report_id or "")):
        return None
    _move_old_row()
    row = _rows().filter(key=ROW_PREFIX + report_id).first()
    return row.value if row and isinstance(row.value, dict) else None


def list_reports():
    """Every report, whole, newest first."""
    try:
        _move_old_row()
        reports = [r.value for r in _rows() if isinstance(r.value, dict)]
    except Exception as e:
        logger.debug(f"App reports: could not read them: {e}")
        return []
    return sorted(reports, key=lambda r: r.get("received_at") or 0, reverse=True)


def summaries():
    """
    Every report as the list shows it, newest first. Only the fields the list shows are
    read from the database: the logs stay where they are, however many reports there are.
    """
    try:
        _move_old_row()
        rows = _rows().values(
            "value__id",
            "value__received_at",
            "value__user",
            "value__device_name",
            "value__what",
            "value__channel_asked",
            "value__server__channel__name",
            "value__player__error",
        )
        found = [
            {
                "id": row["value__id"],
                "received_at": row["value__received_at"],
                "user": row["value__user"],
                "device_name": row["value__device_name"],
                "what": row["value__what"],
                "channel": row["value__server__channel__name"] or row["value__channel_asked"] or "",
                "error": row["value__player__error"] or "",
            }
            for row in rows
        ]
    except Exception as e:
        logger.debug(f"App reports: could not read them: {e}")
        return []
    return sorted(found, key=lambda r: r.get("received_at") or 0, reverse=True)


def delete(report_id=None):
    """One report, or every one of them without an id."""
    from core.models import CoreSettings

    if report_id:
        if _REPORT_ID.match(str(report_id)):
            _rows().filter(key=ROW_PREFIX + report_id).delete()
        return
    CoreSettings.objects.filter(key=OLD_KEY).delete()
    _rows().delete()
