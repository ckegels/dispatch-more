import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Group,
  Loader,
  MultiSelect,
  NumberInput,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
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
            {h.until ? `, until ${new Date(h.until * 1000).toLocaleString()}` : ''}
          </Text>
          <Button size="compact-xs" variant="subtle" onClick={() => forget(h.device, h.where)}>
            Forget
          </Button>
        </Group>
      ))}
    </Stack>
  );
};

const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

// The programmes loaded ahead of time for the guides arrTV offers, and "Load now"
const GuidePreload = () => {
  const [state, setState] = useState(null);

  useEffect(() => {
    API.getArrTvGuidePreload()
      .then(setState)
      .catch(() => setState(null));
  }, []);

  const loadNow = async () => {
    try {
      setState(await API.startArrTvGuidePreload());
    } catch {
      // Left as it is: the line still says what is loaded
    }
  };

  if (!state) return null;
  const working = state.state === 'working';
  return (
    <Group gap="xs" mt="xs" wrap="nowrap">
      <Text size="xs" c="dimmed">
        {working
          ? `Loading: ${state.stage || 'working'}${state.total ? ` (${state.done} of ${state.total} channels)` : ''}`
          : `Programmes loaded for ${state.kept} guide${state.kept === 1 ? '' : 's'}` +
            (state.preloaded_at ? `, last ${when(state.preloaded_at)}` : '')}
      </Text>
      <Button size="compact-xs" variant="subtle" onClick={loadNow} disabled={working}>
        Load now
      </Button>
    </Group>
  );
};

const byWhom = (by) =>
  `${by?.username || 'someone'} on ${by?.device_name || by?.device || 'an unknown device'}` +
  (by?.ip ? ` (${by.ip})` : '');

// The guide changes made from arrTV, newest first, each one undoable: anyone may change a
// guide, so an admin has to be able to see what was changed and put it back
const GuideChanges = () => {
  const [changes, setChanges] = useState(null);

  useEffect(() => {
    API.getArrTvGuideChanges()
      .then((given) => setChanges(given?.changes || []))
      .catch(() => setChanges([]));
  }, []);

  const putBack = async (channel) => {
    try {
      const given = await API.putBackArrTvGuide(channel);
      setChanges(given?.changes || []);
    } catch {
      // Left as it is: the list still says what was changed
    }
  };

  if (!changes) return null;
  return (
    <Box mt="xs">
      <Text size="xs" fw={700} tt="uppercase" c="dimmed" mb={4}>
        Guide changes
      </Text>
      {!changes.length && (
        <Text size="xs" c="dimmed">
          No channel has been put on another guide from arrTV.
        </Text>
      )}
      <Stack gap={4}>
        {changes.map((change) => (
          <Group key={change.channel} gap="xs" wrap="nowrap" align="flex-start">
            <Text size="xs">
              <b>{change.channel_name || `Channel ${change.channel}`}</b>: {change.guide_name || 'no guide'}
              {change.was_name ? `, was ${change.was_name}` : ', had no guide'} — by {byWhom(change.by)},{' '}
              {when(change.at)}
            </Text>
            <Button size="compact-xs" variant="subtle" onClick={() => putBack(change.channel)}>
              Put back
            </Button>
          </Group>
        ))}
      </Stack>
    </Box>
  );
};

// A wait in whole seconds, saved when the field is left (not on every key)
const Seconds = ({ label, description, value, disabled, onSave }) => {
  const [typed, setTyped] = useState(value);
  useEffect(() => setTyped(value), [value]);
  return (
    <NumberInput
      size="xs"
      w={220}
      label={label}
      description={description}
      value={typed}
      min={1}
      max={60}
      suffix=" s"
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
      if ('home_networks' in changes && e?.body?.error) setNetworksError(e.body.error);
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
        arrTV is a player for Android and Google TV made to work with this server. It can tell
        the server which device it is, which channel it is leaving, and what went wrong. Only
        arrTV sends this: for any other player these switches change nothing.
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
                ? "arrTV says the moment its picture stops to wait, and the channel moves to its next stream at once, the way it does when a stream fails: never to a better one, never to the fallback, and nothing when there is no other. Stuttering again goes down in quality, and that device starts channels within it for a day (at home and away apart). Never a channel someone else is watching without trouble. Nothing counts against a stream."
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
                ? "A stream that connects and sends nothing is left after 5 seconds and one check, instead of the start grace (Settings → Streaming) and three checks: a dead stream no longer costs a viewer a minute. Only for channels an arrTV device starts; IPTV answers within a second or two, a source that needs longer to lock (a tuner) would be left too soon."
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
                description="With one other stream to go to"
                value={settings.fast_grace}
                onSave={(s) => change({ fast_grace: s })}
              />
              <Seconds
                label="Wait with two or more"
                description={
                  settings.alternatives
                    ? 'When at least two other streams are usable now'
                    : 'Needs "Tell arrTV how many other streams a channel has"'
                }
                value={settings.fast_grace_many}
                disabled={!settings.alternatives}
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
                ? "With each stream, arrTV is told how many of the channel's other streams it could be moved to now: ones it can play, on an account with a connection free, never the fallback. arrTV gives up on a slow stream sooner where there are several, and waits a little longer where one is left; faster failover uses it too, and does not hurry a channel with none."
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
            An arrTV device outside your home networks — on the VPN, or on a phone connection —
            starts a channel on a stream no better than this, where the channel has one: an FHD
            stream stutters where an HD one plays. A channel with nothing within it still plays
            its best. A channel someone is already watching plays the stream it is on; only the
            device that starts a channel chooses.
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
                if (networks !== (settings.home_networks || '')) change({ home_networks: networks });
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
                onChange={(ids) => change({ guide_choice_sources: ids.join(',') })}
                clearable
                searchable
              />
              <GuidePreload />
              <GuideChanges />
            </Box>
          )}
        </Box>
        <Setting
          label="Take problem reports from arrTV"
          description="Someone with a problem on a channel sends a report from arrTV's player settings. It arrives below with what arrTV saw and what the server knew about that channel at that moment: its streams and providers, its readings, how it started, the channel switches and the log. Logins and passwords are taken out."
          checked={settings.reports}
          onChange={(on) => change({ reports: on })}
        />
      </Stack>

      <Box>
        <Text size="xs" fw={700} tt="uppercase" c="dimmed" mb="xs">
          Problem reports
        </Text>
        {copied && (
          <Text size="xs" c="dimmed" mb="xs">
            {copied}
          </Text>
        )}
        <AppReports enabled={settings.reports} onCopy={copyToClipboard} />
      </Box>
    </Stack>
  );
};

export default ArrTvSettings;
