import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Modal,
  Select,
  Stack,
  Table,
  Text,
} from '@mantine/core';
import API from '../../api';

const REFRESH_MS = 5000;

// A switch is coloured by what the feature did with it, so the table reads at a glance.
const ACTION_COLORS = {
  'overlap slot': 'blue',
  'held slot': 'teal',
  'another account': 'grape',
  'same account': 'grape',
  'stopped skipped channel': 'orange',
  'skipped while surfing': 'yellow',
  'provider refused': 'red',
  'not used': 'gray',
};

// What each label in the "What happened" column means, shown in the legend popup.
const ACTION_MEANINGS = [
  [
    'overlap slot',
    'Every account was at its limit, so this viewer got one extra connection for a few seconds. The result says how it ended.',
  ],
  [
    'held slot',
    'The slot of the channel it just left was kept for it during the switch, so no other viewer could take it in between.',
  ],
  [
    'same account',
    'Its next channel was started on the account it was already watching on ("When Switching Channels").',
  ],
  [
    'another account',
    'Its next channel was started on a different account that still had a free slot.',
  ],
  [
    'stopped skipped channel',
    'A channel it had only watched for a moment was closed, so its slot was free for the new channel.',
  ],
  [
    'skipped while surfing',
    'It moved on to another channel during the Surfing Delay, so this one was never requested from the provider.',
  ],
  [
    'provider refused',
    'The provider closed a new connection before sending any video, usually because the account is full. Dispatcharr waited longer before trying again.',
  ],
  [
    'not used',
    'The overlap did not apply here. The result says why, for example a viewer it cannot tell apart.',
  ],
];

const KEEP_LABELS = {
  1800: '30 minutes',
  7200: '2 hours',
  21600: '6 hours',
  86400: '24 hours',
};

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

const OverlapActivity = ({ active }) => {
  const [activity, setActivity] = useState(null);
  const [error, setError] = useState(null);
  const [legendOpen, setLegendOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      setActivity(await API.getOverlapActivity());
      setError(null);
    } catch {
      setError('Could not load the activity.');
    }
  }, []);

  const changeRetention = async (value) => {
    try {
      setActivity(await API.setOverlapRetention(Number(value)));
      setError(null);
    } catch {
      setError('Could not change how long switches are kept.');
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
  if (!activity.enabled) {
    return (
      <Alert color="gray">
        Channel Switch Overlap is not enabled on any M3U account, so there is
        nothing to show. Enable it on an account to use it.
      </Alert>
    );
  }

  const keptLabel = KEEP_LABELS[activity.keep_seconds] || '30 minutes';

  return (
    <Stack gap="md">
      <Stack gap={4}>
        {activity.accounts.map((account) => (
          <Group
            key={`${account.account}-${account.profile}`}
            gap="xs"
            wrap="wrap"
          >
            <Text size="sm" fw={600}>
              {account.account}
            </Text>
            <Text size="sm" c="dimmed">
              {account.in_use}/{account.max_streams || '∞'} in use
            </Text>
            {account.held_slots > 0 && (
              <Badge size="sm" color="teal" variant="light">
                {account.held_slots} held
              </Badge>
            )}
            {account.stop_skipped && (
              <Badge size="sm" color="orange" variant="light">
                stops skipped channels
              </Badge>
            )}
            {account.lan_subnets.length > 0 && (
              <Text size="xs" c="dimmed">
                LAN: {account.lan_subnets.join(', ')}
              </Text>
            )}
          </Group>
        ))}
      </Stack>

      {activity.events.length === 0 ? (
        <Text size="sm" c="dimmed">
          No channel switches in the last {keptLabel}.
        </Text>
      ) : (
        <Table
          striped
          highlightOnHover
          withTableBorder
          verticalSpacing={4}
          fz="sm"
        >
          <Table.Thead>
            <Table.Tr>
              <Table.Th w={90}>Time</Table.Th>
              <Table.Th>Viewer</Table.Th>
              <Table.Th>Channel</Table.Th>
              <Table.Th w={210}>What happened</Table.Th>
              <Table.Th>Result</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {activity.events.map((event) => (
              <Table.Tr key={`${event.time}-${event.viewer}-${event.channel}`}>
                <Table.Td c="dimmed">{shortTime(event.time)}</Table.Td>
                <Table.Td>{event.viewer}</Table.Td>
                <Table.Td>
                  {event.from_channel ? `${event.from_channel} → ` : ''}
                  {event.channel || '—'}
                </Table.Td>
                {/* Wide enough for the longest label, so it never wraps or is cut off */}
                <Table.Td style={{ whiteSpace: 'nowrap' }}>
                  <Badge
                    size="sm"
                    variant="light"
                    color={ACTION_COLORS[event.action] || 'gray'}
                  >
                    {event.action}
                  </Badge>
                </Table.Td>
                <Table.Td c="dimmed">{event.result}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}

      <Group gap="xs" align="center">
        <Text size="xs" c="dimmed">
          Keep switches for
        </Text>
        <Select
          size="xs"
          w={130}
          aria-label="Keep switches for"
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
      </Group>

      <Modal
        opened={legendOpen}
        onClose={() => setLegendOpen(false)}
        title="What this page shows"
        size="lg"
        centered
      >
        <Stack gap="sm">
          <Text size="sm">
            Every line is one decision Channel Switch Overlap made: who was
            switching, from which channel to which, what the feature did, and
            how it ended. Only switches on accounts with the overlap enabled
            appear here.
          </Text>
          <Text size="sm">
            A viewer is shown by its login, or by its address and app when it is
            recognised on one of the account&apos;s LAN Subnets.
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
            Recording happens in Dispatcharr itself, so this page does not have
            to be open. The last {activity.events.length} switches are kept, for
            as long as you set above, and nothing is recorded while the overlap
            is switched off everywhere.
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

export default OverlapActivity;
