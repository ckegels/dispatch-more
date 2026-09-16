import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Badge, Group, Loader, Stack, Table, Text } from '@mantine/core';
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

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

const OverlapActivity = ({ active }) => {
  const [activity, setActivity] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      setActivity(await API.getOverlapActivity());
      setError(null);
    } catch {
      setError('Could not load the activity.');
    }
  }, []);

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
          No channel switches in the last {activity.kept_minutes} minutes.
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
              <Table.Th w={150}>What happened</Table.Th>
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
                <Table.Td>
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

      <Text size="xs" c="dimmed">
        The last {activity.events.length} switches, kept for{' '}
        {activity.kept_minutes} minutes. Refreshes every {REFRESH_MS / 1000}{' '}
        seconds.
      </Text>
    </Stack>
  );
};

export default OverlapActivity;
