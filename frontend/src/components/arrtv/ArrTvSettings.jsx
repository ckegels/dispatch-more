import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Collapse,
  Group,
  Loader,
  MultiSelect,
  NumberInput,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core';
import { ChevronRight } from 'lucide-react';
import API from '../../api';
import useVideoStore from '../../store/useVideoStore';
import useSettingsStore from '../../store/settings';
import { getShowVideoUrl } from '../../utils/cards/RecordingCardUtils';
import useEPGsStore from '../../store/epgs';
import AppReports from '../diagnostics/AppReports';
import { copy } from '../diagnostics/copyText';

// arrTV is the Android / Google TV player built to work with this server. What it may tell
// the server, and what it has reported. No other app sends what these switches read, so for
// every other player they change nothing; all of them are off until switched on here.
// The contract for arrTV's developer is fork/arrTV-integration.md.

const Setting = ({ label, description, checked, disabled, onChange }) => (
  <Switch
    size="sm"
    label={label}
    description={description}
    checked={!!checked}
    disabled={disabled}
    onChange={(e) => onChange(e.currentTarget.checked)}
  />
);

const WHERE = { home: 'at home', away: 'away from home', any: '' };

// The devices that stuttered their way down to a lower quality, each startable at its best
// again: a device held to HD because of one bad evening should not stay there for a day
const HeldDevices = () => {
  const [held, setHeld] = useState(null);

  useEffect(() => {
    API.getArrTvHeld()
      .then((given) => setHeld(given?.held || []))
      .catch(() => setHeld([]));
  }, []);

  const forget = async (device, where) => {
    try {
      const given = await API.forgetArrTvHeld(device, where);
      setHeld(given?.held || []);
    } catch {
      // Left as it is: the list still says what is held
    }
  };

  if (!held?.length) return null;
  return (
    <Stack gap={4} mt="xs">
      {held.map((h) => (
        <Group key={`${h.device}-${h.where}`} gap="xs" wrap="nowrap">
          <Text size="xs">
            {h.name}: starts within {h.quality}
            {WHERE[h.where] ? ` ${WHERE[h.where]}` : ''}
            {h.until
              ? `, until ${new Date(h.until * 1000).toLocaleString()}`
              : ''}
          </Text>
          <Button
            size="compact-xs"
            variant="subtle"
            onClick={() => forget(h.device, h.where)}
          >
            Forget
          </Button>
        </Group>
      ))}
    </Stack>
  );
};

const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

// A section that opens on a click: the lists below the settings grow long, and the
// settings are what the page is mostly opened for
export const Fold = ({ title, note, children, defaultOpen = false }) => {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Box>
      <UnstyledButton
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        style={{ display: 'flex', alignItems: 'center', gap: 6 }}
      >
        <ChevronRight
          size={14}
          style={{
            transform: open ? 'rotate(90deg)' : 'none',
            transition: 'transform 120ms',
          }}
        />
        <Text size="xs" fw={700} tt="uppercase" c="dimmed">
          {title}
        </Text>
        {note && (
          <Text size="xs" c="dimmed">
            {note}
          </Text>
        )}
      </UnstyledButton>
      <Collapse in={open}>
        <Box mt="xs">{children}</Box>
      </Collapse>
    </Box>
  );
};

const clock = (iso) =>
  iso
    ? new Date(iso).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      })
    : '';

// "Zeit im Bild (17:30–17:50)", or why there is nothing to compare
const onNow = (now, holds) => {
  if (now) return `${now.title} (${clock(now.start)}–${clock(now.end)})`;
  if (holds === 0) return 'holds no programmes';
  if (holds == null) return '';
  return 'nothing on right now';
};

// The programmes loaded ahead of time for the guides arrTV offers, and "Load now". Asked
// again every few seconds while it loads, so the line moves and ends; a preload that died
// part way (a restart) says so and can be started again, rather than "Loading" for a day
// with the button greyed out.
const PRELOAD_POLL_MS = 5000;

const GuidePreload = () => {
  const [state, setState] = useState(null);
  const working = state?.state === 'working';

  useEffect(() => {
    API.getArrTvGuidePreload()
      .then(setState)
      .catch(() => setState(null));
  }, []);

  useEffect(() => {
    if (!working) return undefined;
    const timer = setInterval(() => {
      API.getArrTvGuidePreload()
        .then(setState)
        .catch(() => {});
    }, PRELOAD_POLL_MS);
    return () => clearInterval(timer);
  }, [working]);

  const loadNow = async () => {
    try {
      setState(await API.startArrTvGuidePreload());
    } catch {
      // Left as it is: the line still says what is loaded
    }
  };

  if (!state) return null;
  const loaded =
    `Programmes loaded for ${state.kept} guide${state.kept === 1 ? '' : 's'}` +
    (state.preloaded_at ? `, last ${when(state.preloaded_at)}` : '');
  return (
    <Group gap="xs" mt="xs" wrap="nowrap">
      <Text size="xs" c={state.state === 'stopped' ? 'orange' : 'dimmed'}>
        {working
          ? `Loading: ${state.stage || 'working'}${state.total ? ` (${state.done} of ${state.total} channels)` : ''}`
          : state.state === 'stopped'
            ? `The last loading stopped before it finished. ${loaded}`
            : loaded}
      </Text>
      <Button
        size="compact-xs"
        variant="subtle"
        onClick={loadNow}
        disabled={working}
      >
        Load now
      </Button>
    </Group>
  );
};

const byWhom = (by) =>
  `${by?.username || 'someone'} on ${by?.device_name || by?.device || 'an unknown device'}` +
  (by?.ip ? ` (${by.ip})` : '');

// The guide changes made from arrTV, newest first. Anyone may change a guide from the
// player, so an admin has to be able to see what was changed and either put it back or keep
// it. Each change is a card of its own: on one line with the buttons, the buttons were
// squeezed until "Put back" read "Put bac".
const GuideChanges = () => {
  const [changes, setChanges] = useState(null);
  const [problem, setProblem] = useState(null);
  const showVideo = useVideoStore((s) => s.showVideo);
  const envMode = useSettingsStore((s) => s.environment?.env_mode);

  useEffect(() => {
    API.getArrTvGuideChanges()
      .then((given) => setChanges(given?.changes || []))
      .catch(() => setChanges([]));
  }, []);

  const act = async (doing, channel) => {
    setProblem(null);
    try {
      const given = await doing(channel);
      setChanges(given?.changes || []);
    } catch (e) {
      // Said on the card rather than swallowed: a change that was not undone must not
      // look as though it was
      setProblem({ channel, text: e?.body?.error || 'That did not work' });
      API.getArrTvGuideChanges()
        .then((given) => setChanges(given?.changes || []))
        .catch(() => {});
    }
  };

  if (!changes) return null;
  return (
    <Box mt="md">
      <Fold
        title="Guide changes made in arrTV"
        note={changes.length ? `(${changes.length})` : '(none)'}
      >
        <Text size="xs" c="dimmed" mb="xs">
          <b>Put back</b> returns the channel to the guide it had before the
          change. <b>Keep</b> says the change was right and takes it off this
          list; the channel stays on that guide.
        </Text>
        {!changes.length && (
          <Text size="xs" c="dimmed">
            No channel has been put on another guide from arrTV.
          </Text>
        )}
        <Stack gap="xs">
          {changes.map((change) => (
            <Box
              key={change.channel}
              p="xs"
              style={{ border: '1px solid #3f3f46', borderRadius: 6 }}
            >
              <Text size="sm" fw={600}>
                {change.channel_name || `Channel ${change.channel}`}
              </Text>
              <Text size="xs">
                Now on: {change.guide_name || 'no guide'}
                {change.epg
                  ? ` — ${onNow(change.guide_now, change.guide_holds)}`
                  : ''}
              </Text>
              <Text size="xs">
                Before: {change.was_name || 'no guide'}
                {change.was
                  ? ` — ${onNow(change.was_now, change.was_holds)}`
                  : ''}
              </Text>
              <Text size="xs" c="dimmed">
                Changed by {byWhom(change.by)}, {when(change.at)}
              </Text>
              {(change.confirmed || []).map((c) => (
                <Text key={`${c.at}-${c.device || c.ip}`} size="xs" c="dimmed">
                  Confirmed as right by {byWhom(c)}, {when(c.at)}
                </Text>
              ))}
              {change.earlier > 0 && (
                <Text size="xs" c="dimmed">
                  {change.earlier} earlier change
                  {change.earlier === 1 ? '' : 's'} in arrTV: putting this back
                  brings the one before it back to this list.
                </Text>
              )}
              {change.in_force === false && (
                <Text size="xs" c="orange">
                  The guide was changed again since, outside arrTV. Putting this
                  back would undo that change, so only taking it off the list is
                  offered.
                </Text>
              )}
              {problem?.channel === change.channel && (
                <Text size="xs" c="red">
                  {problem.text}
                </Text>
              )}
              <Group gap="xs" mt={6}>
                {change.channel_uuid && (
                  <Button
                    size="xs"
                    variant="subtle"
                    style={{ flexShrink: 0 }}
                    onClick={() =>
                      showVideo(
                        getShowVideoUrl({ uuid: change.channel_uuid }, envMode),
                        'live',
                        {
                          name: change.channel_name,
                          channelId: change.channel,
                        }
                      )
                    }
                  >
                    Watch
                  </Button>
                )}
                {change.in_force !== false && (
                  <Button
                    size="xs"
                    variant="light"
                    style={{ flexShrink: 0 }}
                    onClick={() => act(API.putBackArrTvGuide, change.channel)}
                  >
                    Put back
                  </Button>
                )}
                <Button
                  size="xs"
                  variant="default"
                  style={{ flexShrink: 0 }}
                  onClick={() => act(API.keepArrTvGuide, change.channel)}
                >
                  {change.in_force === false ? 'Take off the list' : 'Keep'}
                </Button>
              </Group>
            </Box>
          ))}
        </Stack>
      </Fold>
    </Box>
  );
};

// A whole number, saved when the field is left (not on every key): a wait in seconds, or
// the days of guide kept
// What server rewind keeps now (live_proxy/rewind.py), read when the section opens
const RewindUsage = () => {
  const [usage, setUsage] = useState(null);
  useEffect(() => {
    API.getRewindUsage()
      .then(setUsage)
      .catch(() => setUsage(null));
  }, []);
  if (!usage) return null;
  const gb = (b) => `${(b / 1024 ** 3).toFixed(1)} GB`;
  return (
    <Text size="xs" c="dimmed" mt={4}>
      {usage.channels.length
        ? `Recording now: ${usage.channels.filter((c) => c.recording).length} channel(s), ${gb(usage.bytes)} of ${gb(usage.budget_bytes)} in ${usage.folder}.`
        : `Nothing recorded now. Room for ${gb(usage.budget_bytes)} in ${usage.folder}.`}
    </Text>
  );
};

const Seconds = ({
  label,
  description,
  value,
  disabled,
  onSave,
  min = 1,
  max = 60,
  suffix = ' s',
  w = 220,
}) => {
  const [typed, setTyped] = useState(value);
  useEffect(() => setTyped(value), [value]);
  return (
    <NumberInput
      size="xs"
      w={w}
      label={label}
      description={description}
      value={typed}
      min={min}
      max={max}
      suffix={suffix}
      allowDecimal={false}
      disabled={disabled}
      onChange={setTyped}
      onBlur={() => {
        if (typed !== value && typed !== '') onSave(Number(typed));
      }}
    />
  );
};

const ArrTvSettings = () => {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(null);
  // The home networks as typed, saved when the field is left rather than on every key
  const [networks, setNetworks] = useState('');
  const [networksError, setNetworksError] = useState(null);
  // The guide sources arrTV may offer: every active one that is not a dummy (a dummy makes
  // its programmes up from the channel's name, so it says nothing about which channel it is)
  const epgs = useEPGsStore((s) => s.epgs);
  const guideSources = Object.values(epgs || {})
    .filter((epg) => epg.source_type !== 'dummy' && epg.is_active !== false)
    .map((epg) => ({ value: String(epg.id), label: epg.name }));

  useEffect(() => {
    API.getArrTvSettings()
      .then((given) => {
        setSettings(given);
        setNetworks(given?.home_networks || '');
      })
      .catch(() => setError('Could not read the arrTV settings.'));
  }, []);

  const change = async (changes) => {
    try {
      const saved = await API.saveArrTvSettings({ ...settings, ...changes });
      setSettings(saved);
      setNetworks(saved?.home_networks || '');
      setError(null);
      setNetworksError(null);
    } catch (e) {
      // A network that is not one is said on its field; anything else on the page
      if ('home_networks' in changes && e?.body?.error)
        setNetworksError(e.body.error);
      else setError('Could not change the arrTV settings.');
    }
  };

  const copyToClipboard = async (text) => {
    const ok = await copy(text);
    setCopied(ok ? 'Copied' : 'Could not copy: select the text by hand');
    setTimeout(() => setCopied(null), 2500);
  };

  if (error) return <Alert color="red">{error}</Alert>;
  if (!settings) return <Loader size="sm" />;

  return (
    <Stack gap="lg">
      <Text size="sm" c="dimmed">
        arrTV is a player for Android and Google TV made to work with this
        server. It can tell the server which device it is, which channel it is
        leaving, and what went wrong. Only arrTV sends this: for any other
        player these switches change nothing.
      </Text>

      <Stack gap="md">
        <Setting
          label="Recognise each arrTV device"
          description="Every arrTV device counts as itself, with its login, wherever it connects from. Two devices on one login behind a VPN or a router are otherwise one device to the server, and Force Close stops one's channel when the other starts one. Multiview tiles of one device are never closed for each other, and devices are shown by the name arrTV gives them."
          checked={settings.devices}
          onChange={(on) => change({ devices: on })}
        />
        <Box pl="xl">
          <Setting
            label="Close the previous channel when arrTV changes channel"
            description={
              settings.devices
                ? 'arrTV says which channel it is leaving, and that channel is closed the moment the new one is asked for, its connection kept for the new one. On an account with one connection, a quick switch instead of a refusal. Never a channel somebody else is watching, nor one being recorded.'
                : 'Needs "Recognise each arrTV device": only a device that said who it is can say which channel is its own.'
            }
            checked={settings.switch_hints}
            disabled={!settings.devices}
            onChange={(on) => change({ switch_hints: on })}
          />
        </Box>
        <Box pl="xl">
          <Setting
            label="Change stream when arrTV stutters"
            description={
              settings.devices
                ? 'arrTV says the moment its picture stops to wait, and the channel moves to its next stream at once, the way it does when a stream fails: never to a better one, never to the fallback, and nothing when there is no other. Stuttering again goes down in quality, and that device starts channels within it for a day (at home and away apart). Never a channel someone else is watching without trouble. Nothing counts against a stream.'
                : 'Needs "Recognise each arrTV device": only a device that said who it is can say it is the one stuttering.'
            }
            checked={settings.stall_switch}
            disabled={!settings.devices}
            onChange={(on) => change({ stall_switch: on })}
          />
          {settings.devices && settings.stall_switch && <HeldDevices />}
        </Box>
        <Box pl="xl">
          <Setting
            label="Faster failover when arrTV starts a channel"
            description={
              settings.devices
                ? 'A stream that connects and sends nothing is left after a few seconds and one check, instead of the start grace (Settings → Streaming) and three checks, at every step of the way while there is another stream to go to: a dead stream no longer costs a viewer a minute. Nowhere to go is never hurried -- no other stream the device can play, or none on a provider with a connection free (someone else watching), and never a stream of its own -- since leaving could only end on the fallback. Only for channels an arrTV device starts; a source that needs longer to lock (a tuner) would be left too soon.'
                : 'Needs "Recognise each arrTV device": only a device that said who it is is given the shorter wait.'
            }
            checked={settings.fast_failover}
            disabled={!settings.devices}
            onChange={(on) => change({ fast_failover: on })}
          />
          {settings.devices && settings.fast_failover && (
            <Group mt="xs" gap="md" align="flex-start">
              <Seconds
                label="Wait"
                description="With one other stream left to go to"
                value={settings.fast_grace}
                onSave={(s) => change({ fast_grace: s })}
              />
              <Seconds
                label="Wait with two or more"
                description="While at least two other streams are left to go to"
                value={settings.fast_grace_many}
                onSave={(s) => change({ fast_grace_many: s })}
              />
            </Group>
          )}
        </Box>
        <Box pl="xl">
          <Setting
            label="Tell arrTV how many other streams a channel has"
            description={
              settings.devices
                ? "With each stream, arrTV is told how many of the channel's other streams it could be moved to now: ones it can play, on an account with a connection free, never the fallback. arrTV gives up on a slow stream sooner where there are several, and waits a little longer where one is left. (Faster failover counts them itself, whether or not arrTV is told.)"
                : 'Needs "Recognise each arrTV device": only a device that said who it is can be counted for.'
            }
            checked={settings.alternatives}
            disabled={!settings.devices}
            onChange={(on) => change({ alternatives: on })}
          />
        </Box>
        <Box>
          <Text size="sm" fw={500}>
            Away from home
          </Text>
          <Text size="xs" c="dimmed" mb="xs">
            An arrTV device outside your home networks — on the VPN, or on a
            phone connection — starts a channel on a stream no better than this,
            where the channel has one: an FHD stream stutters where an HD one
            plays. A channel with nothing within it still plays its best. A
            channel someone is already watching plays the stream it is on; only
            the device that starts a channel chooses.
          </Text>
          <Group align="flex-start" gap="md" wrap="wrap">
            <TextInput
              size="xs"
              label="Home networks"
              description="Comma separated, for example 192.168.2.0/24. Empty is no limit anywhere."
              placeholder="192.168.2.0/24"
              value={networks}
              error={networksError}
              onChange={(e) => setNetworks(e.currentTarget.value)}
              onBlur={() => {
                if (networks !== (settings.home_networks || ''))
                  change({ home_networks: networks });
              }}
              style={{ flex: '1 1 280px' }}
            />
            <Select
              size="xs"
              label="Away from home, at most"
              allowDeselect={false}
              value={settings.outside_max_quality || ''}
              onChange={(value) => change({ outside_max_quality: value || '' })}
              data={[
                // 4K is "No limit": nothing is better than it
                { value: '', label: 'No limit (4K)' },
                { value: 'FHD', label: 'FHD (1080p)' },
                { value: 'HD', label: 'HD (720p)' },
                { value: 'SD', label: 'SD (576p)' },
              ]}
              style={{ width: 180 }}
            />
          </Group>
        </Box>
        <Setting
          label="A stream of its own when the channel's is too much for it"
          description="Someone at home watches a channel in 4K, and a device that cannot play it (away from home, a Chromecast HD that says it cannot decode 4K, or one held to a lower quality above) asks for the same channel: it gets another of that channel's streams within what it can play, from another provider with a connection free, and the others keep theirs. It opens one more provider connection, only where one is free; with none, the device joins the channel as before."
          checked={settings.own_stream}
          onChange={(on) => change({ own_stream: on })}
        />
        <Box>
          <Setting
            label="Let arrTV change a channel's guide"
            description="Whoever is watching can pick another guide for the channel from the player (“Wrong guide? Choose another”), from the guides that could be that channel and have something on now. The change is for every viewer and for Plex and Jellyfin, and is recorded with the user, device and address that made it. The best guides for every channel have their programmes loaded ahead of time so the list has something to show."
            checked={settings.guide_choice}
            onChange={(on) => change({ guide_choice: on })}
          />
          {settings.guide_choice && (
            <Box pl="xl">
              <MultiSelect
                size="xs"
                mt="xs"
                maw={520}
                label="Sources to offer"
                description="Empty is every active source that is not a dummy"
                placeholder="Every source"
                data={guideSources}
                value={String(settings.guide_choice_sources || '')
                  .split(',')
                  .filter(Boolean)}
                onChange={(ids) =>
                  change({ guide_choice_sources: ids.join(',') })
                }
                clearable
                searchable
              />
              <GuidePreload />
              <GuideChanges />
            </Box>
          )}
        </Box>
        <Seconds
          label="Keep the guide's past"
          w={420}
          min={0}
          max={7}
          suffix=" days"
          description="Each guide refresh throws away what has already been on, and most guide files start at today, so the TV cannot scroll back even to this morning. Set 3 to keep three days of finished programmes (0 keeps none, as Dispatcharr does). arrTV shows them once its login has “EPG previous days” set to the same number (Users → edit the user). The past fills in from the next refresh on."
          value={settings.keep_past_days ?? 0}
          onSave={(days) => change({ keep_past_days: days })}
        />
        <Setting
          label="Server rewind"
          description="While a TV watches a channel, the server keeps a recording of it, so arrTV can pause for as long as it likes and rewind without using the TV's own storage. Read from the channel the TV already plays: no extra provider connection while someone watches; a paused TV alone keeps its channel open. Off: nothing is recorded and arrTV uses its own buffer."
          checked={settings.rewind}
          onChange={(on) => change({ rewind: on })}
        />
        {settings.rewind && (
          <Box pl="md">
            <Group gap="md" align="flex-start">
              <Seconds
                label="Always kept"
                min={5}
                max={240}
                suffix=" min"
                description="How far back a TV can rewind."
                value={settings.rewind_minutes ?? 60}
                onSave={(v) => change({ rewind_minutes: v })}
              />
              <Seconds
                label="Longest pause"
                min={15}
                max={1440}
                suffix=" min"
                description="Kept from a TV's pause on."
                value={settings.rewind_max_pause_minutes ?? 240}
                onSave={(v) => change({ rewind_max_pause_minutes: v })}
              />
              <Seconds
                label="Disk budget"
                min={1}
                max={4000}
                suffix=" GB"
                description="All channels together; never more than the disk can spare."
                value={settings.rewind_budget_gb ?? 20}
                onSave={(v) => change({ rewind_budget_gb: v })}
              />
            </Group>
            <RewindUsage />
          </Box>
        )}
        <Setting
          label="Look back may move another viewer"
          description="When the only provider with a programme's look back is in use by someone's live channel, that channel is moved to another of its streams (checked to play, moved back if not) so the look back can start. When it cannot, the look-back viewer is told it is unavailable due to current viewing priorities and may try again after 5 minutes, or as soon as the provider is free."
          checked={settings.look_back_priority}
          onChange={(on) => change({ look_back_priority: on })}
        />
        {settings.look_back_priority && (
          <Box pl="md">
            <Setting
              label="Tell the viewer who was moved"
              description="Their arrTV shows a short note that their channel moved to another stream for someone else."
              checked={settings.look_back_priority_notify}
              onChange={(on) => change({ look_back_priority_notify: on })}
            />
          </Box>
        )}
        <Setting
          label="Take problem reports from arrTV"
          description="Someone with a problem on a channel sends a report from arrTV's player settings. It arrives below with what arrTV saw and what the server knew about that channel at that moment: its streams and providers, its readings, how it started, the channel switches and the log. Logins and passwords are taken out."
          checked={settings.reports}
          onChange={(on) => change({ reports: on })}
        />
      </Stack>

      <Fold title="Problem reports">
        {copied && (
          <Text size="xs" c="dimmed" mb="xs">
            {copied}
          </Text>
        )}
        <AppReports enabled={settings.reports} onCopy={copyToClipboard} />
      </Fold>
    </Stack>
  );
};

export default ArrTvSettings;
