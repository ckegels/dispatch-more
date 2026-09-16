import React from 'react';
import { Alert, Badge, Group, Stack, Table, Text } from '@mantine/core';

// A switch is coloured by what Channel Switch Overlap did with it, so the table reads at a
// glance. Also used by the legend.
export const ACTION_COLORS = {
  'overlap slot': 'blue',
  'held slot': 'teal',
  'another account': 'grape',
  'same account': 'grape',
  'stopped skipped channel': 'orange',
  'skipped while surfing': 'yellow',
  'provider refused': 'red',
  'not used': 'gray',
};

export const ACTION_MEANINGS = [
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

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

const ChannelSwitches = ({ activity }) => {
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
          No channel switches recorded yet.
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={720} type="native">
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
                <Table.Tr
                  key={`${event.time}-${event.viewer}-${event.channel}`}
                >
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
        </Table.ScrollContainer>
      )}
    </Stack>
  );
};

export default ChannelSwitches;
