import React from 'react';
import { Badge, Box, Group, Stack, Table, Text, Tooltip } from '@mantine/core';

// Each phase of a channel start keeps its colour everywhere, so the bars can be compared
// between rows at a glance.
export const PHASE_COLORS = {
  slot: '#4dabf7',
  'provider connected': '#9775fa',
  'first byte': '#38d9a9',
  'first keyframe': '#ffa94d',
  'first byte to player': '#748ffc',
};

export const PHASE_MEANINGS = [
  ['slot', 'Dispatcharr picked a stream and took a connection slot for it.'],
  [
    'provider connected',
    'The provider accepted the connection. A long wait here is the provider, not Dispatcharr.',
  ],
  [
    'first byte',
    'The first data arrived from the provider. Long means the provider connected but sent nothing yet.',
  ],
  [
    'first keyframe',
    'The first picture a player can start from. Long means the channel started in the middle of a group of pictures, and everything before it cannot be shown. This is what usually makes Plex look slow.',
  ],
  [
    'first byte to player',
    'The first video left Dispatcharr for the player. After this, how fast it appears is up to the player.',
  ],
];

const SLOW_START = 3.0;

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

// One bar per start, split into the phases, each as wide as it took
const PhaseBar = ({ phases, total }) => (
  <Group gap={2} wrap="nowrap" style={{ width: '100%' }}>
    {phases.map((phase) => (
      <Tooltip
        key={phase.label}
        label={`${phase.label}: ${phase.took.toFixed(2)}s (at ${phase.at.toFixed(2)}s)`}
        withArrow
      >
        <Box
          style={{
            width: `${total > 0 ? (phase.took / total) * 100 : 0}%`,
            minWidth: 2,
            height: 10,
            borderRadius: 2,
            background: PHASE_COLORS[phase.label] || '#868e96',
          }}
        />
      </Tooltip>
    ))}
  </Group>
);

const ChannelStarts = ({ starts }) => {
  if (!starts || starts.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No channels have started recently.
      </Text>
    );
  }

  return (
    <Stack gap="sm">
      <Group gap="md" wrap="wrap">
        {PHASE_MEANINGS.map(([label]) => (
          <Group key={label} gap={6} wrap="nowrap">
            <Box
              style={{
                width: 10,
                height: 10,
                borderRadius: 2,
                background: PHASE_COLORS[label],
              }}
            />
            <Text size="xs" c="dimmed">
              {label}
            </Text>
          </Group>
        ))}
      </Group>

      <Table
        striped
        highlightOnHover
        withTableBorder
        verticalSpacing={6}
        fz="sm"
      >
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={90}>Time</Table.Th>
            <Table.Th>Channel</Table.Th>
            <Table.Th w={130}>Player</Table.Th>
            <Table.Th w={70}>Took</Table.Th>
            <Table.Th>Where the time went</Table.Th>
            <Table.Th w={170}>Slowest step</Table.Th>
            <Table.Th w={200}>After the handover</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {starts.map((start) => (
            <Table.Tr key={`${start.time}-${start.channel}`}>
              <Table.Td c="dimmed">{shortTime(start.time)}</Table.Td>
              <Table.Td>{start.channel}</Table.Td>
              <Table.Td c="dimmed">{start.client}</Table.Td>
              <Table.Td>
                <Text
                  size="sm"
                  fw={600}
                  c={start.total >= SLOW_START ? 'orange' : undefined}
                >
                  {start.total.toFixed(1)}s
                </Text>
              </Table.Td>
              <Table.Td>
                <PhaseBar phases={start.phases} total={start.total} />
              </Table.Td>
              <Table.Td c="dimmed" style={{ whiteSpace: 'nowrap' }}>
                {start.slowest}
              </Table.Td>
              {/* Only filled in for a media server that is configured (see Media Servers) */}
              <Table.Td>
                {start.server_buffering > 0 ? (
                  <Group gap={6} wrap="wrap">
                    <Text
                      size="sm"
                      c={start.server_buffering >= 3 ? 'orange' : 'dimmed'}
                    >
                      playing after {start.server_buffering.toFixed(1)}s
                    </Text>
                    {start.server_decision && (
                      <Badge
                        size="sm"
                        variant="light"
                        color={
                          start.server_decision === 'direct play'
                            ? 'teal'
                            : 'orange'
                        }
                      >
                        {start.server_decision}
                        {start.server_speed ? ` ${start.server_speed}×` : ''}
                      </Badge>
                    )}
                  </Group>
                ) : (
                  <Text size="sm" c="dimmed">
                    —
                  </Text>
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>

      <Text size="xs" c="dimmed">
        Hover a bar to see how long that step took. The steps are measured in
        Dispatcharr; what a player does with the video afterwards is not
        included.
      </Text>
    </Stack>
  );
};

export default ChannelStarts;
