import React from 'react';
import {
  ActionIcon,
  Badge,
  Box,
  Group,
  Stack,
  Table,
  Text,
  Tooltip,
} from '@mantine/core';
import { Copy } from 'lucide-react';
import { startAsText } from './copyText';

// Each phase of a channel start keeps its colour everywhere, so the bars can be compared
// between rows at a glance.
export const PHASE_COLORS = {
  slot: '#4dabf7',
  'provider connected': '#9775fa',
  'first byte': '#38d9a9',
  'first keyframe': '#ffa94d',
  'first byte to player': '#748ffc',
};

// What the media server itself did, after Dispatcharr handed the video over
export const SERVER_PHASE_COLORS = {
  'session opened': '#4dabf7',
  'transcode started': '#ffa94d',
  'first video ready': '#38d9a9',
  playing: '#748ffc',
};

export const SERVER_PHASE_MEANINGS = [
  [
    'session opened',
    'The server noticed the channel and made a session for it. A long wait here is the server deciding what to do, before it even looks at the video.',
  ],
  [
    'transcode started',
    'It started converting the stream. Live TV in a browser is almost always converted; a native player usually plays it as it is.',
  ],
  [
    'first video ready',
    'The first converted video existed. The gap before this is the server analysing the stream and starting its encoder.',
  ],
  [
    'playing',
    'The player actually showed video. Everything before this is what a viewer sees as a black screen.',
  ],
];

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
const PhaseBar = ({ phases, total, colors = PHASE_COLORS }) => (
  <Group gap={2} wrap="nowrap" style={{ width: '100%' }}>
    {phases.map((phase) => (
      <Tooltip
        key={phase.label}
        label={`${phase.label}: ${phase.took.toFixed(1)}s (at ${phase.at.toFixed(1)}s)`}
        withArrow
      >
        <Box
          style={{
            width: `${total > 0 ? (phase.took / total) * 100 : 0}%`,
            minWidth: 2,
            height: 10,
            borderRadius: 2,
            background: colors[phase.label] || '#868e96',
          }}
        />
      </Tooltip>
    ))}
  </Group>
);

const ChannelStarts = ({ starts, onCopy }) => {
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

      <Table.ScrollContainer minWidth={760} type="native">
        <Table
          striped
          highlightOnHover
          withTableBorder
          verticalSpacing={6}
          fz="sm"
          layout="fixed"
        >
          <Table.Thead>
            <Table.Tr>
              <Table.Th w={80}>Time</Table.Th>
              <Table.Th w="16%">Channel</Table.Th>
              <Table.Th w={100}>Player</Table.Th>
              <Table.Th w={60}>Took</Table.Th>
              {/* The bar gets what is left: it is the reason for this table */}
              <Table.Th>Where the time went</Table.Th>
              <Table.Th w="28%">After the handover</Table.Th>
              <Table.Th w={40} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {starts.map((start) => (
              <Table.Tr key={`${start.time}-${start.channel}`}>
                <Table.Td c="dimmed">{shortTime(start.time)}</Table.Td>
                <Table.Td style={{ wordBreak: 'break-word' }}>
                  {start.channel}
                </Table.Td>
                <Table.Td
                  c="dimmed"
                  fz="xs"
                  style={{ wordBreak: 'break-word' }}
                >
                  {start.client}
                </Table.Td>
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
                  <Stack gap={2}>
                    <PhaseBar phases={start.phases} total={start.total} />
                    <Text size="xs" c="dimmed">
                      slowest: {start.slowest}
                    </Text>
                  </Stack>
                </Table.Td>
                {/* Only filled in for a media server that is configured (Media Servers) */}
                <Table.Td>
                  {start.server_phases.length > 0 ? (
                    <Stack gap={2}>
                      <Group gap={6} wrap="nowrap" justify="space-between">
                        <Text
                          size="sm"
                          fw={600}
                          c={
                            start.server_gave_up
                              ? 'red'
                              : start.server_buffering >= 3
                                ? 'orange'
                                : undefined
                          }
                        >
                          {start.server_gave_up
                            ? 'never played'
                            : `${start.server_buffering.toFixed(1)}s`}
                        </Text>
                        {start.server_decision && (
                          <Text
                            size="xs"
                            c={
                              start.server_decision === 'direct play'
                                ? 'teal'
                                : 'orange'
                            }
                            style={{ whiteSpace: 'nowrap' }}
                          >
                            {start.server_decision === 'direct play'
                              ? 'direct play'
                              : 'transcode'}
                            {start.server_speed
                              ? ` ${start.server_speed}×`
                              : ''}
                          </Text>
                        )}
                      </Group>
                      <PhaseBar
                        phases={start.server_phases}
                        total={
                          start.server_phases[start.server_phases.length - 1].at
                        }
                        colors={SERVER_PHASE_COLORS}
                      />
                      <Text size="xs" c="dimmed">
                        {start.server_phases
                          .map(
                            (phase) => `${phase.label} ${phase.at.toFixed(1)}s`
                          )
                          .join(' · ')}
                      </Text>
                      {start.server_title && (
                        // Which session this was matched to, so a wrong match is visible
                        <Text size="xs" c="dimmed" fs="italic">
                          {start.server_title}
                          {start.server_user ? ` · ${start.server_user}` : ''}
                        </Text>
                      )}
                    </Stack>
                  ) : (
                    <Text size="sm" c="dimmed">
                      —
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <Tooltip label="Copy this start as text" withArrow>
                    <ActionIcon
                      variant="subtle"
                      color="gray"
                      size="sm"
                      aria-label={`Copy the start of ${start.channel}`}
                      onClick={() => onCopy(startAsText(start))}
                    >
                      <Copy size={14} />
                    </ActionIcon>
                  </Tooltip>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>

      <Text size="xs" c="dimmed">
        Hover a bar to see how long that step took. The steps are measured in
        Dispatcharr; what a player does with the video afterwards is not
        included.
      </Text>
    </Stack>
  );
};

export default ChannelStarts;
