import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Group,
  Loader,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
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

const ArrTvSettings = () => {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(null);
  // The home networks as typed, saved when the field is left rather than on every key
  const [networks, setNetworks] = useState('');
  const [networksError, setNetworksError] = useState(null);

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
                { value: '', label: 'No limit' },
                { value: 'HD', label: 'HD (720p)' },
                { value: 'SD', label: 'SD' },
              ]}
              style={{ width: 180 }}
            />
          </Group>
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
