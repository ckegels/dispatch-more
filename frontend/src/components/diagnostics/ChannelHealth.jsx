import React from 'react';
import { Badge, Stack, Table, Text } from '@mantine/core';

// What happened to a channel, coloured by whether it helped or gave up.
export const HEALTH_COLORS = {
  'kept alive': 'teal',
  'gave up': 'red',
};

export const HEALTH_MEANINGS = [
  [
    'kept alive',
    'The provider closed a connection that had been working, and Stream Recovery treated it as a rotation instead of a failure. Without it, three of these would have given up on the channel.',
  ],
  [
    'gave up',
    'The same channel has been kept alive too often in an hour: a stream that drops this much is not rotating, it is failing, so Dispatcharr goes back to trying the next stream.',
  ],
];

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

const ChannelHealth = ({ events }) => {
  if (!events || events.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        Nothing to report. This fills in when a provider closes a connection
        that was working, and needs Stream Recovery switched on under Media
        Servers.
      </Text>
    );
  }

  return (
    <Stack gap="sm">
      <Table.ScrollContainer minWidth={620} type="native">
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
              <Table.Th w="22%">Channel</Table.Th>
              <Table.Th w={130}>What happened</Table.Th>
              <Table.Th>Why</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {events.map((event, index) => (
              <Table.Tr key={`${event.time}-${index}`}>
                <Table.Td c="dimmed">{shortTime(event.time)}</Table.Td>
                <Table.Td style={{ wordBreak: 'break-word' }}>
                  {event.channel}
                </Table.Td>
                <Table.Td>
                  <Badge
                    size="sm"
                    variant="light"
                    color={HEALTH_COLORS[event.action] || 'gray'}
                  >
                    {event.action}
                  </Badge>
                </Table.Td>
                <Table.Td c="dimmed">{event.detail}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      <Text size="xs" c="dimmed">
        Kept for a day. A provider closing connections is normal; what matters
        is whether it costs you the channel.
      </Text>
    </Stack>
  );
};

export default ChannelHealth;
