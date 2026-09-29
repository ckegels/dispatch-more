"""
The live show groups. Every minute (tasks.show_groups_tick) each group that is on is brought in
line with the plan: a copy of each channel that should be in it now is switched on in the Show
Groups profile, and switched off when its shows are over. The plugin's live loop, for many
groups, and run by Celery instead of a thread in every web worker.

What it promises, as the plugin did:
- A channel is never taken out of a group while someone watches it, nor for viewer_grace
  minutes after.
- Only its own copies are ever changed: they live in channel groups it made and are listed in
  groups.json. It refuses a group name that is already a channel group of yours.
- Each copy keeps its number (from first_number up) and plays its source's streams, in order.
- Live off switches every copy off (they are kept, so their numbers stay); a group switched off
  or deleted while live is on has its copies and its channel group removed once nobody
  watches them. "Remove everything" removes all of it.
"""

import json
import logging
import os
import time
import uuid as uuidlib
from datetime import datetime, timedelta

from django.utils import timezone

from . import plan as plans
from . import store, themes

logger = logging.getLogger(__name__)

STATE = "groups.json"
ACTIVITY = "activity.log"
LOOKUP_STATUS = "lookups-status.json"
PASS_KEY = "show-groups:pass"
REBUILD_KEY = "show-groups:rebuild"
LOOKUP_KEY = "show-groups:looking-up"
# The plugin's record of its copies, and the lock its passes took (see take_over)
PLUGIN_KEY = "show_groups"
PLUGIN_STATE = "live.json"
PLUGIN_PASS_KEY = "show_groups:pass"
RETRY_UNKNOWN = timedelta(days=30)
ALWAYS = "always"


class Refused(Exception):
    """Something Show Groups will not do, said in words the owner can act on."""


# ---- its own record ------------------------------------------------------------------------------

def load_state():
    """groups: group id -> {"group_id", "copies": {source channel id: {"id", "uuid"}}},
    profile_id, watched: copy id -> when a viewer was last seen, refused: group id -> why."""
    try:
        with open(store.path_of(STATE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("groups", {})
    data.setdefault("watched", {})
    data.setdefault("refused", {})
    for entry in data["groups"].values():
        entry.setdefault("copies", {})
    return data


def save_state(state):
    store.write_text(STATE, json.dumps(state, indent=1, sort_keys=True))


def own_copy_ids(state):
    return {int(c["id"]) for entry in state["groups"].values() for c in entry["copies"].values()}


def _zone():
    try:
        from zoneinfo import ZoneInfo

        from core.models import CoreSettings

        return ZoneInfo(CoreSettings.get_system_time_zone())
    except Exception:
        from datetime import timezone as tz

        return tz.utc


def record(line):
    """One line per join and leave, with the reason. The last 2000 lines are kept."""
    path = store.path_of(ACTIVITY)
    stamp = timezone.now().astimezone(_zone()).strftime("%Y-%m-%d %H:%M")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()[-1999:]
    except OSError:
        lines = []
    lines.append(f"{stamp}  {line}")
    store.write_text(ACTIVITY, "\n".join(lines) + "\n")


def activity(most=80):
    try:
        with open(store.path_of(ACTIVITY), encoding="utf-8") as fh:
            return fh.read().splitlines()[-most:]
    except OSError:
        return []


def _redis():
    try:
        from core.utils import RedisClient

        return RedisClient.get_client()
    except Exception:
        return None


def viewers(uuid):
    """How many watch the copy now. None when Redis cannot be asked: then nobody may be taken
    off it."""
    try:
        from apps.proxy.live_proxy.redis_keys import RedisKeys

        client = _redis()
        if client is None:
            return None
        return int(client.scard(RedisKeys.clients(str(uuid))))
    except Exception:
        return None


class _Pass:
    """Only one pass changes channels at a time; the plugin's passes took the same kind of lock,
    so a take-over also waits for one of its passes to end."""

    def __init__(self, *keys):
        self.keys = keys or (PASS_KEY,)
        self.token = f"{os.getpid()}:{uuidlib.uuid4().hex}"
        self.client = None
        self.held = []

    def __enter__(self):
        self.client = _redis()
        if self.client is None:
            return self
        try:
            for key in self.keys:
                if not self.client.set(key, self.token, nx=True, ex=300):
                    raise Refused("Show Groups is being updated right now; try again in a minute.")
                self.held.append(key)
        except Refused:
            self.__exit__(None, None, None)
            raise
        except Exception:
            self.client = None
        return self

    def __exit__(self, *exc):
        for key in self.held:
            try:
                value = self.client.get(key)
                if (value.decode() if isinstance(value, bytes) else value) == self.token:
                    self.client.delete(key)
            except Exception:
                pass
        self.held = []
        return False


# ---- Dispatcharr objects ---------------------------------------------------------------------------

def ensure_profile(settings, state):
    """The profile the copies are switched on in. Created empty: Dispatcharr would otherwise
    switch every existing channel on in it."""
    from apps.channels.models import ChannelProfile

    name = settings.get("profile_name") or "Show Groups"
    profile = ChannelProfile.objects.filter(id=state.get("profile_id") or 0).first()
    if profile is None:
        profile = ChannelProfile.objects.filter(name=name).first()
    if profile is None:
        profile = ChannelProfile(name=name)
        profile._start_empty = True
        profile.save()
        record(f'created the channel profile "{name}" (empty)')
    elif profile.name != name and not ChannelProfile.objects.filter(name=name).exists():
        profile.name = name
        profile.save(update_fields=["name"])
    state["profile_id"] = profile.id
    return profile


def ensure_group(entry, name, ours):
    """The channel group a show group's copies live in, known by its id so renaming the show
    group renames it."""
    from apps.channels.models import Channel, ChannelGroup

    group = ChannelGroup.objects.filter(id=entry.get("group_id") or 0).first()
    if group is not None and group.name != name:
        if ChannelGroup.objects.filter(name=name).exists():
            raise Refused(f'A channel group called "{name}" already exists; give this show group '
                          "another name.")
        group.name = name
        group.save(update_fields=["name"])
    if group is None:
        if ChannelGroup.objects.filter(name=name).exists():
            raise Refused(f'You already have a channel group called "{name}"; give this show '
                          'group another name (say "' + name + ' now").')
        group = ChannelGroup.objects.create(name=name)
        entry["group_id"] = group.id
    strangers = Channel.objects.filter(channel_group=group).exclude(id__in=ours)
    if strangers.exists():
        raise Refused(f'The channel group "{name}" holds {strangers.count()} channels Show Groups '
                      "did not make; give this show group another name.")
    return group


def _own_copies(entry, group):
    """The copies that still exist and are still in the group. One deleted or moved elsewhere is
    forgotten, and never touched again."""
    from apps.channels.models import Channel

    ids = {int(c["id"]) for c in entry["copies"].values()}
    present = {c.id: c for c in Channel.objects.filter(id__in=ids, channel_group=group)}
    for source_id, copy in list(entry["copies"].items()):
        if int(copy["id"]) not in present:
            del entry["copies"][source_id]
    return present


def sync_streams(source, copy):
    """The copy plays the source's streams, in the same order (its fallback still last)."""
    from apps.channels.models import ChannelStream

    wanted = list(ChannelStream.objects.filter(channel=source).order_by("order", "id")
                  .values_list("stream_id", flat=True))
    have = list(ChannelStream.objects.filter(channel=copy).order_by("order", "id")
                .values_list("stream_id", flat=True))
    if wanted == have:
        return False
    ChannelStream.objects.filter(channel=copy).delete()
    ChannelStream.objects.bulk_create([ChannelStream(channel=copy, stream_id=stream_id, order=order)
                                       for order, stream_id in enumerate(wanted)])
    return True


def _mend_catchup(pairs):
    """Every copy offers catch-up exactly when its source does. Dispatcharr works these two
    fields out in a ChannelStream signal that bulk_create does not send and the delete in
    sync_streams sends too early, so they are put back from the source every pass.
    pairs: source id -> copy id."""
    from apps.channels.models import Channel

    rows = {r["id"]: (r["is_catchup"], r["catchup_days"]) for r in Channel.objects.filter(
        id__in=list(pairs) + list(pairs.values())).values("id", "is_catchup", "catchup_days")}
    changed = 0
    for source_id, copy_id in pairs.items():
        want, have = rows.get(source_id), rows.get(copy_id)
        if want is not None and have is not None and want != have:
            Channel.objects.filter(id=copy_id).update(is_catchup=want[0], catchup_days=want[1])
            changed += 1
    return changed


def make_copy(source_id, group, profile, first_number):
    """A copy of the source channel as the viewer sees it (its effective name, guide and logo),
    with a number of its own that it keeps."""
    from apps.channels.managers import with_effective_values
    from apps.channels.models import Channel, ChannelProfileMembership

    source = with_effective_values(Channel.objects.filter(id=source_id)).first()
    if source is None:
        return None
    copy = Channel.objects.create(
        name=source.effective_name,
        channel_number=Channel.get_next_available_channel_number(starting_from=first_number),
        channel_group=group,
        epg_data_id=source.effective_epg_data_id,
        logo_id=source.effective_logo_id,
        tvg_id=source.effective_tvg_id,
        tvc_guide_stationid=source.effective_tvc_guide_stationid,
        stream_profile_id=source.effective_stream_profile_id,
        user_level=source.user_level,
        is_adult=source.is_adult,
        hidden_from_output=True,  # shown once it is switched on
    )
    sync_streams(source, copy)
    _mend_catchup({source.id: copy.id})
    ChannelProfileMembership.objects.create(channel_profile=profile, channel=copy, enabled=False)
    return copy


def resync_streams(state):
    """After an M3U refresh the sources may have other streams: the copies follow, except one
    someone is watching."""
    from apps.channels.models import Channel

    changed = 0
    for entry in state["groups"].values():
        for source_id, copy_entry in entry["copies"].items():
            source = Channel.objects.filter(id=int(source_id)).first()
            copy = Channel.objects.filter(id=int(copy_entry["id"])).first()
            if source is not None and copy is not None and viewers(copy.uuid) == 0:
                changed += sync_streams(source, copy)
    return changed


def _hide_the_rest(profile, copy_ids):
    """A copy out of its group is hidden from every output, not only switched off in the
    profile: an app reading the playlist without a profile lists every channel."""
    from apps.channels.models import Channel, ChannelProfileMembership

    on = set(ChannelProfileMembership.objects.filter(
        channel_profile=profile, channel_id__in=list(copy_ids), enabled=True)
        .values_list("channel_id", flat=True))
    shown = Channel.objects.filter(id__in=on, hidden_from_output=True).update(hidden_from_output=False)
    hidden = Channel.objects.filter(id__in=set(copy_ids) - on, hidden_from_output=False).update(
        hidden_from_output=True)
    return shown + hidden


def announce(settings, channel_uuids, changes):
    """Tell the apps on Dispatcharr's socket that channels changed, so arrTV reloads its
    channel list within seconds instead of at its next periodic check."""
    try:
        from core.utils import send_websocket_update

        send_websocket_update("updates", "update", {
            "type": "channels_changed", "source": "show_groups",
            "profile": settings.get("profile_name") or "Show Groups",
            "channels": channel_uuids, "changes": changes,
        })
    except Exception as e:
        logger.warning(f"Show Groups: could not tell apps the channels changed: {e}")


# ---- the plugin ------------------------------------------------------------------------------------

def plugin():
    """The Show Groups plugin, when it is installed: {"enabled", "live", "group"}."""
    try:
        from apps.plugins.models import PluginConfig

        config = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
    except Exception:
        return None
    if config is None:
        return None
    settings = config.settings or {}
    return {"enabled": bool(config.enabled),
            "live": str(settings.get("live")).lower() in ("true", "1", "yes", "on"),
            "group": str(settings.get("group_name") or "Cooking")}


def take_over():
    """Take the plugin's group over: its copies (with their numbers), its profile, its words
    and pins, and switch the plugin off. Nothing is copied again and no channel moves."""
    from apps.channels.models import ChannelGroup
    from apps.plugins.models import PluginConfig

    from .matching import fold

    config = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
    if config is None:
        raise Refused("The Show Groups plugin is not installed.")
    with _Pass(PASS_KEY, PLUGIN_PASS_KEY):
        old = dict(config.settings or {})
        was_live = str(old.get("live")).lower() in ("true", "1", "yes", "on")
        # Switched off first, so the plugin's loop does nothing more while this runs
        config.enabled = False
        config.settings = {**old, "live": False}
        config.save(update_fields=["enabled", "settings"])

        settings, groups = themes.load_settings(), themes.load_groups()
        name = str(old.get("group_name") or "Cooking").strip() or "Cooking"
        mine = next((g for g in groups if fold(g["name"]) == fold(name)), None)
        if mine is None:
            mine = {**themes.GROUP_FIELDS, "id": themes.new_id(name, {g["id"] for g in groups}),
                    "name": name, "preset": False}
            groups.append(mine)
        for key in ("category_words", "title_words", "title_exclusions", "disqualifiers",
                    "always", "never"):
            if key in old:
                mine[key] = str(old[key] or "")
        for key in ("use_title_words", "use_disqualifiers", "one_per_airing"):
            if key in old:
                mine[key] = str(old[key]).lower() in ("true", "1", "yes", "on")
        mine["on"] = mine["on"] or was_live
        for key in ("profile_name", "first_number", "viewer_grace", "announce_changes",
                    "join_ahead", "leave_after", "linger", "min_length", "wikipedia_languages",
                    "tmdb_key", "online_lookups"):
            if key in old and old[key] not in (None, ""):
                settings[key] = old[key]
        wanted = {fold(n).strip() for n in str(old.get("source_groups") or "").split(",") if n.strip()}
        if wanted:
            settings["source_groups"] = [g.id for g in ChannelGroup.objects.all()
                                         if fold(g.name).strip() in wanted]
        settings["live"] = bool(settings.get("live") or was_live)
        themes.save(settings, groups)

        taken = 0
        try:
            with open(store.path_of(PLUGIN_STATE), encoding="utf-8") as fh:
                theirs = json.load(fh)
        except (OSError, ValueError):
            theirs = {}
        state = load_state()
        if theirs.get("copies"):
            entry = state["groups"].setdefault(mine["id"], {"copies": {}})
            entry["group_id"] = theirs.get("group_id") or entry.get("group_id")
            entry["copies"].update(theirs["copies"])
            state["watched"].update(theirs.get("watched") or {})
            taken = len(theirs["copies"])
        if theirs.get("profile_id"):
            state["profile_id"] = theirs["profile_id"]
        save_state(state)
        # Out of the plugin's reach: were it switched on again, it would start afresh instead of
        # switching these copies off under Show Groups
        if os.path.exists(store.path_of(PLUGIN_STATE)):
            os.replace(store.path_of(PLUGIN_STATE), store.path_of(PLUGIN_STATE + ".taken-over"))
        record(f'took over the plugin\'s group "{name}" with {taken} copies; the plugin is switched off')
    ask_rebuild()
    return {"group": mine["id"], "copies": taken}


# ---- one pass --------------------------------------------------------------------------------------

def tick(settings, groups, plan, now=None):
    """Bring every group in line with the plan. Returns (joined, left, held for viewers)."""
    with _Pass():
        return _tick(settings, groups, plan, now)


def _tick(settings, groups, plan, now=None):
    from apps.channels.models import Channel, ChannelGroup, ChannelProfileMembership

    now = now or timezone.now()
    state = load_state()
    live = bool(settings.get("live"))
    by_id = {g["id"]: g for g in groups}
    working = {g["id"] for g in themes.active(settings, groups)}
    if not working and not own_copy_ids(state):
        return [], [], []  # off and nothing made: nothing is touched at all
    running = plugin()
    if working and running and running["enabled"] and running["live"]:
        raise Refused("The Show Groups plugin is still live. Take its group over, or switch it "
                      "off, before Show Groups goes live here.")

    profile = ensure_profile(settings, state)
    first_number = float(settings.get("first_number") or 20000)
    grace = timedelta(minutes=float(settings.get("viewer_grace") or 0))
    ours = own_copy_ids(state)
    enabled = set(ChannelProfileMembership.objects.filter(
        channel_profile=profile, channel_id__in=list(ours), enabled=True)
        .values_list("channel_id", flat=True))

    joined, left, held, moved = [], [], [], []
    zone = _zone()
    for gid in sorted(set(state["groups"]) | working):
        group = by_id.get(gid)
        entry = state["groups"].setdefault(gid, {"copies": {}})
        wanted = {}
        if gid in working:
            wanted = dict(plans.stays_at(plan, gid, now))
            for source_id in group.get("permanent") or ():
                if source_id not in ours:
                    wanted.setdefault(str(source_id), ALWAYS)
        if not wanted and not entry["copies"] and not entry.get("group_id"):
            state["groups"].pop(gid, None)
            continue
        name = (group or {}).get("name") or entry.get("name") or gid
        try:
            channel_group = ensure_group(entry, name, ours)
        except Refused as why:
            if state["refused"].get(gid) != str(why):
                record(f"{name}: {why}")
            state["refused"][gid] = str(why)
            continue
        state["refused"].pop(gid, None)
        entry["name"] = name
        copies = _own_copies(entry, channel_group)

        for source_id, stay in wanted.items():
            copy_entry = entry["copies"].get(source_id)
            copy = copies.get(int(copy_entry["id"])) if copy_entry else None
            if copy is None:
                copy = make_copy(int(source_id), channel_group, profile, first_number)
                if copy is None:
                    continue
                entry["copies"][source_id] = {"id": copy.id, "uuid": str(copy.uuid)}
                copies[copy.id] = copy
                ours.add(copy.id)
            if copy.id in enabled:
                continue
            ChannelProfileMembership.objects.update_or_create(
                channel_profile=profile, channel=copy, defaults={"enabled": True})
            enabled.add(copy.id)
            moved.append(str(copy.uuid))
            if stay == ALWAYS:
                why = "always in the group"
            else:
                airing = plans.showing(stay, now)
                start = datetime.fromisoformat(airing["start"]).astimezone(zone)
                end = datetime.fromisoformat(airing["end"]).astimezone(zone)
                leaves = datetime.fromisoformat(stay["leaves"]).astimezone(zone)
                why = (f"{airing['title']} {start:%H:%M}-{end:%H:%M}, {airing['why'].split(':')[0]};"
                       f" leaves {leaves:%a %H:%M} as the guide stands now")
            record(f"{name}: joined  {copy.name} (#{copy.channel_number:g}): {why}")
            joined.append(copy.name)

        wanted_ids = {int(entry["copies"][s]["id"]) for s in wanted if s in entry["copies"]}
        for copy_id in sorted((enabled & set(copies)) - wanted_ids):
            copy = copies[copy_id]
            watching = viewers(copy.uuid)
            if watching is None or watching > 0:
                if str(copy_id) not in state["watched"]:
                    record(f"{name}: stays   {copy.name} (#{copy.channel_number:g}): "
                           f"{'watched by ' + str(watching) if watching else 'viewers unknown'}")
                state["watched"][str(copy_id)] = now.isoformat()
                held.append(copy.name)
                continue
            last_seen = state["watched"].get(str(copy_id))
            if last_seen and now - datetime.fromisoformat(last_seen) < grace:
                held.append(copy.name)
                continue
            state["watched"].pop(str(copy_id), None)
            ChannelProfileMembership.objects.filter(channel_profile=profile, channel=copy).update(enabled=False)
            enabled.discard(copy_id)
            moved.append(str(copy.uuid))
            why = ("Show Groups is switched off" if not live
                   else "the group is switched off" if gid not in working
                   else "no show follows within the hour")
            record(f"{name}: left    {copy.name} (#{copy.channel_number:g}): {why}")
            left.append(copy.name)

        # A group switched off (or deleted) while live is on is cleared away once its copies
        # are out and unwatched. With live off everything is kept, numbers and all.
        if live and gid not in working:
            for copy_id, copy in list(copies.items()):
                if copy_id in enabled:
                    continue
                source = next(s for s, c in entry["copies"].items() if int(c["id"]) == copy_id)
                copy.delete()
                del entry["copies"][source]
                ours.discard(copy_id)
            if not entry["copies"]:
                if not Channel.objects.filter(channel_group=channel_group).exists():
                    channel_group.delete()
                    record(f'{name}: removed its channel group; the group is switched off')
                state["groups"].pop(gid, None)

    for copy_id in list(state["watched"]):
        if int(copy_id) not in enabled:
            state["watched"].pop(copy_id, None)
    save_state(state)
    pairs = {int(s): int(c["id"]) for e in state["groups"].values() for s, c in e["copies"].items()}
    flipped = _hide_the_rest(profile, ours) + _mend_catchup(pairs)
    if joined or left or flipped:
        from apps.output.streaming_chunk_cache import invalidate_epg_chunk_cache

        invalidate_epg_chunk_cache()
        if settings.get("announce_changes", True):
            announce(settings, moved, len(joined) + len(left) + flipped)
    return joined, left, held


def remove_all(settings):
    """Delete every copy, then every channel group Show Groups made and its profile. Only with
    live off, or the next minute fills the groups again. A copy someone watches is kept (press
    again later). The online answers are kept: they are knowledge, not channels."""
    from apps.channels.models import Channel, ChannelGroup, ChannelProfile, ChannelProfileMembership

    if settings.get("live"):
        raise Refused("Switch Show Groups off first, or the groups fill again within a minute.")
    with _Pass():
        state = load_state()
        deleted, kept, removed = [], [], []
        for gid, entry in list(state["groups"].items()):
            for source_id, copy_entry in list(entry["copies"].items()):
                copy = Channel.objects.filter(id=int(copy_entry["id"])).first()
                if copy is None or copy.channel_group_id != entry.get("group_id"):
                    del entry["copies"][source_id]
                    continue
                watching = viewers(copy.uuid)
                if watching is None or watching > 0:
                    kept.append(copy.name)
                    continue
                deleted.append(copy.name)
                copy.delete()
                del entry["copies"][source_id]
            if not entry["copies"]:
                group = ChannelGroup.objects.filter(id=entry.get("group_id") or 0).first()
                if group is not None and not Channel.objects.filter(channel_group=group).exists():
                    removed.append(f'the channel group "{group.name}"')
                    group.delete()
                state["groups"].pop(gid, None)
        if not state["groups"]:
            profile = ChannelProfile.objects.filter(id=state.get("profile_id") or 0).first()
            if profile is not None and not ChannelProfileMembership.objects.filter(
                    channel_profile=profile).exists():
                removed.append(f'the channel profile "{profile.name}"')
                profile.delete()
            state = {}
        save_state(state)
        record(f"removed {len(deleted)} copies" + (", " + ", ".join(removed) if removed else "")
               + (f"; kept {len(kept)} someone is watching" if kept else ""))
    if deleted:
        from apps.output.streaming_chunk_cache import invalidate_epg_chunk_cache

        invalidate_epg_chunk_cache()
    return deleted, kept, removed


# ---- the plan and the minute ----------------------------------------------------------------------

def ask_rebuild():
    """Work the plan out again at the next minute (after a guide refresh, or new answers)."""
    client = _redis()
    if client is not None:
        try:
            client.set(REBUILD_KEY, "1", ex=3600)
        except Exception:
            pass


def work_out(settings, groups, now=None):
    """The plan made now, for every group that is on (also with live off: that is a preview),
    and kept."""
    state = load_state()
    resync_streams(state)
    made = plans.compute(settings, groups, now, exclude_ids=own_copy_ids(state))
    plans.save(made)
    return made


def run_once(now=None):
    """One minute: the plan made again when it is old or asked for, then the pass. Nothing at
    all while Show Groups is off and nothing was ever made."""
    settings, groups = themes.load_settings(), themes.load_groups()
    working = themes.active(settings, groups)
    if not working and not own_copy_ids(load_state()):
        return "off"
    plan = plans.load()
    rebuild = False
    client = _redis()
    if client is not None:
        try:
            rebuild = bool(client.delete(REBUILD_KEY))
        except Exception:
            pass
    if working and (rebuild or plans.stale(plan, settings, groups, now)):
        plan = work_out(settings, groups, now)
    joined, left, held = tick(settings, groups, plan, now)
    return f"{len(joined)} joined, {len(left)} left, {len(held)} held for viewers"


def lookup_queue(plan, titles, sources, now):
    """The titles worth asking about, busiest first, with the sources not asked yet (or asked
    over RETRY_UNKNOWN ago without an answer). [(written title, key, [sources])]"""
    queue = []
    for _, written, key in (plan or {}).get("unknown") or []:
        answers = titles.get(key, {})
        missing = []
        for source in sources:
            answer = answers.get(source)
            if answer is None:
                missing.append(source)
            elif not answer.get("genres"):
                try:
                    if now - datetime.fromisoformat(answer.get("asked")) > RETRY_UNKNOWN:
                        missing.append(source)
                except (TypeError, ValueError):
                    missing.append(source)
        if missing:
            queue.append((written, key, missing))
    return queue


def look_up(settings, budget=60, now=None):
    """Ask the online databases about titles no guide knows for at most budget seconds, and keep
    every answer (an empty one too, so nothing is asked twice). Returns (asked, waiting)."""
    from . import lookups

    now = now or timezone.now()
    plan = plans.load()
    sources = lookups.enabled_sources(settings)
    queue = lookup_queue(plan, store.load_lookups(), sources, now)
    began = time.monotonic()
    new = {}
    for written, key, missing in queue:
        if time.monotonic() - began >= budget:
            break
        for source in missing:
            try:
                answer = lookups.ask(source, written, settings)
            except Exception:
                logger.exception("Show Groups: asking %s about %r failed", source, written)
                answer = None
            new.setdefault(key, {})[source] = {**(answer or {}), "asked": now.isoformat()}
    titles = store.merge_lookups(new) if new else store.load_lookups()
    waiting = len(queue) - len(new)
    store.write_text(LOOKUP_STATUS, json.dumps({
        "at": now.isoformat(), "asked_now": len(new), "waiting": waiting,
        "asked": len(titles), "answered": len(store.online_answers(titles)),
        "sources": list(sources)}))
    if new:
        ask_rebuild()
    return len(new), waiting


def lookup_status():
    try:
        with open(store.path_of(LOOKUP_STATUS), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
