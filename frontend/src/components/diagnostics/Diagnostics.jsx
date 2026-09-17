import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  Modal,
  Select,
  SegmentedControl,
  Stack,
  Table,
  Text,
} from '@mantine/core';
import { Copy } from 'lucide-react';
import API from '../../api';
import { allAsText, copy } from './copyText';
import ChannelStarts, {
  PHASE_COLORS,
  PHASE_MEANINGS,
  SERVER_PHASE_COLORS,
  SERVER_PHASE_MEANINGS,
} from './ChannelStarts';
import ChannelSwitches, {
  ACTION_COLORS,
  ACTION_MEANINGS,
} from './ChannelSwitches';
import ChannelHealth, { HEALTH_COLORS, HEALTH_MEANINGS } from './ChannelHealth';

const REFRESH_MS = 5000;

const KEEP_LABELS = {
  1800: '30 minutes',
  7200: '2 hours',
  21600: '6 hours',
  86400: '24 hours',
};

const Diagnostics = ({ active }) => {
  const [activity, setActivity] = useState(null);
  const [error, setError] = useState(null);
  const [legendOpen, setLegendOpen] = useState(false);
  const [tab, setTab] = useState('starts');
  const [copied, setCopied] = useState(null);

  // One place for every copy button: it says whether it worked, and forgets after a moment
  const copyToClipboard = async (text) => {
    const ok = await copy(text);
    setCopied(ok ? 'Copied' : 'Could not copy: select the text by hand');
    setTimeout(() => setCopied(null), 2500);
  };

  const load = useCallback(async () => {
    try {
      setActivity(await API.getDiagnostics());
      setError(null);
    } catch {
      setError('Could not load the diagnostics.');
    }
  }, []);

  const changeHealth = async (settings) => {
    try {
      setActivity(await API.setChannelHealth(settings));
      setError(null);
    } catch {
      setError('Could not change the channel health recording.');
    }
  };

  const changeRetention = async (value) => {
    try {
      setActivity(await API.setDiagnosticsRetention(Number(value)));
      setError(null);
    } catch {
      setError('Could not change how long activity is kept.');
    }
  };

  useEffect(() => {
    if (!active) return undefined;
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [active, load]);

  if (error) {
    return <Alert color="red">{error}</Alert>;
  }
  if (!activity) {
    return <Loader size="sm" />;
  }

  return (
    <Stack gap="md">
      <SegmentedControl
        value={tab}
        onChange={setTab}
        aria-label="What to show"
        data={[
          {
            value: 'starts',
            label: `Channel starts (${activity.starts.length})`,
          },
          {
            value: 'switches',
            label: `Channel switches (${activity.events.length})`,
          },
          {
            value: 'health',
            // What is running now, not what has gone wrong: the count that matters
            label: `Channel health (${(activity.running || []).length})`,
          },
        ]}
      />

      {tab === 'starts' && (
        <ChannelStarts starts={activity.starts} onCopy={copyToClipboard} />
      )}
      {tab === 'switches' && (
        <ChannelSwitches activity={activity} onCopy={copyToClipboard} />
      )}
      {tab === 'health' && (
        <ChannelHealth
          events={activity.health}
          running={activity.running}
          stopped={activity.stopped}
          settings={activity.channel_health}
          onSettings={changeHealth}
        />
      )}

      <Group gap="xs" align="center">
        <Text size="xs" c="dimmed">
          Keep for
        </Text>
        <Select
          size="xs"
          w={130}
          aria-label="Keep for"
          value={String(activity.keep_seconds)}
          onChange={changeRetention}
          data={(activity.keep_choices || []).map((seconds) => ({
            value: String(seconds),
            label: KEEP_LABELS[seconds] || `${seconds}s`,
          }))}
        />
        <Text size="xs" c="dimmed">
          Refreshes every {REFRESH_MS / 1000} seconds.
        </Text>
        <Button
          variant="subtle"
          size="compact-xs"
          onClick={() => setLegendOpen(true)}
        >
          What do these mean?
        </Button>
        <Button
          variant="subtle"
          size="compact-xs"
          leftSection={<Copy size={13} />}
          onClick={() => copyToClipboard(allAsText(activity, tab))}
        >
          Copy all as text
        </Button>
        {copied && (
          <Text size="xs" c="dimmed">
            {copied}
          </Text>
        )}
      </Group>

      <Modal
        opened={legendOpen}
        onClose={() => setLegendOpen(false)}
        title="What this page shows"
        size="lg"
        centered
      >
        <Stack gap="sm">
          <Text size="sm" fw={600}>
            Channel starts
          </Text>
          <Text size="sm">
            Every time a channel is started, each step is timed inside
            Dispatcharr. The bar shows where the time went, so a slow start can
            be blamed on the right step instead of guessed at.
          </Text>
          <Table verticalSpacing={4} fz="sm">
            <Table.Tbody>
              {PHASE_MEANINGS.map(([label, meaning]) => (
                <Table.Tr key={label}>
                  <Table.Td style={{ whiteSpace: 'nowrap' }}>
                    <Group gap={6} wrap="nowrap">
                      <Box
                        style={{
                          width: 10,
                          height: 10,
                          borderRadius: 2,
                          background: PHASE_COLORS[label],
                        }}
                      />
                      <Text size="sm">{label}</Text>
                    </Group>
                  </Table.Td>
                  <Table.Td>{meaning}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>

          <Text size="sm" fw={600} mt="sm">
            After the handover
          </Text>
          <Text size="sm">
            For a media server configured under Media Servers, what the server
            itself did once Dispatcharr handed the video over. The number is how
            long it took before the viewer saw anything.
          </Text>
          <Table verticalSpacing={4} fz="sm">
            <Table.Tbody>
              {SERVER_PHASE_MEANINGS.map(([label, meaning]) => (
                <Table.Tr key={label}>
                  <Table.Td style={{ whiteSpace: 'nowrap' }}>
                    <Group gap={6} wrap="nowrap">
                      <Box
                        style={{
                          width: 10,
                          height: 10,
                          borderRadius: 2,
                          background: SERVER_PHASE_COLORS[label],
                        }}
                      />
                      <Text size="sm">{label}</Text>
                    </Group>
                  </Table.Td>
                  <Table.Td>{meaning}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>

          <Text size="sm" fw={600} mt="sm">
            Channel health
          </Text>
          <Text size="sm">
            What happened to a channel itself, rather than to a viewer. A
            provider closing connections is normal; whether it costs you the
            channel is what this shows.
          </Text>
          <Table verticalSpacing={4} fz="sm">
            <Table.Tbody>
              {HEALTH_MEANINGS.map(([action, meaning]) => (
                <Table.Tr key={action}>
                  <Table.Td style={{ whiteSpace: 'nowrap' }}>
                    <Badge
                      size="sm"
                      variant="light"
                      color={HEALTH_COLORS[action]}
                    >
                      {action}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{meaning}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>

          <Text size="sm" fw={600} mt="sm">
            Channel switches
          </Text>
          <Text size="sm">
            What Channel Switch Overlap did: who was switching, from which
            channel to which, and how it ended. Only accounts with the overlap
            enabled appear here. A viewer is shown by its login, or by its
            address and app when it is recognised on one of the account&apos;s
            LAN Subnets.
          </Text>
          <Table verticalSpacing={4} fz="sm">
            <Table.Tbody>
              {ACTION_MEANINGS.map(([action, meaning]) => (
                <Table.Tr key={action}>
                  <Table.Td style={{ whiteSpace: 'nowrap' }}>
                    <Badge
                      size="sm"
                      variant="light"
                      color={ACTION_COLORS[action]}
                    >
                      {action}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{meaning}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>

          <Text size="xs" c="dimmed">
            Everything is recorded in Dispatcharr itself, so this page does not
            have to be open, and everything is thrown away after the time set
            below the table.
          </Text>
          <Group justify="flex-end">
            <Button variant="outline" onClick={() => setLegendOpen(false)}>
              Close
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
};

export default Diagnostics;
