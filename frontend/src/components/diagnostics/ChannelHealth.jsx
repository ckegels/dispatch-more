import React, { useState } from 'react';
import {
  Badge,
  Button,
  Group,
  Stack,
  Switch,
  Table,
  Text,
} from '@mantine/core';

// What happened to a channel, coloured by whether it helped or gave up.
export const HEALTH_COLORS = {
  'kept alive': 'teal',
  'gave up': 'orange',
  'stream given up': 'orange',
  'stream switched': 'blue',
  'nothing left': 'red',
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
  [
    'stream given up',
    'A stream failed to connect too many times in a row, so Dispatcharr left it for the next one on the channel. This is what Stream Recovery is meant to prevent when the stream was actually working.',
  ],
  [
    'stream switched',
    'The channel is now playing from another of its streams. Whoever was watching saw a moment of nothing while it changed over.',
  ],
  [
    'nothing left',
    'Every other stream on the channel was tried and none worked, so the channel stops. Either the provider is down, or the channel has only one stream that is failing.',
  ],
];

const shortTime = (seconds) =>
  new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

const forHowLong = (seconds) => {
  if (!seconds) return '—';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds)}s`;
};

const rate = (kbps) =>
  !kbps ? '—' : kbps >= 1000 ? `${(kbps / 1000).toFixed(1)} Mbps` : `${Math.round(kbps)} kbps`;

// Under 1.0 means ffmpeg is not keeping up with real time, which is what a stall looks
// like before it becomes one. Worth colouring rather than leaving to be noticed.
const speedColor = (speed) => {
  if (!speed) return 'gray';
  if (speed >= 0.98) return 'teal';
  if (speed >= 0.9) return 'yellow';
  return 'red';
};

const Speed = ({ speed }) =>
  speed ? (
    <Badge size="sm" variant="light" color={speedColor(speed)}>
      {speed.toFixed(2)}×
    </Badge>
  ) : (
    <Text size="xs" c="dimmed">
      —
    </Text>
  );

// The shape of a reading over the samples kept, drawn small enough to sit in a table cell.
const Trend = ({ samples, pick, height = 18, width = 90 }) => {
  const values = (samples || []).map(pick).filter((value) => value !== null);
  if (values.length < 2) return null;
  const top = Math.max(...values, 0.0001);
  const bottom = Math.min(...values, 0);
  const span = top - bottom || 1;
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - bottom) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg width={width} height={height} role="img" aria-label="trend">
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        opacity="0.7"
      />
    </svg>
  );
};

const RunningNow = ({ running }) => {
  if (!running || running.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No channel is running.
      </Text>
    );
  }
  return (
    <Table.ScrollContainer minWidth={760} type="native">
      <Table striped highlightOnHover withTableBorder verticalSpacing={4} fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w="24%">Channel</Table.Th>
            <Table.Th w={90}>State</Table.Th>
            <Table.Th w={80}>Playing</Table.Th>
            <Table.Th w={70}>Watching</Table.Th>
            <Table.Th w={90}>Speed</Table.Th>
            <Table.Th w={110}>Source</Table.Th>
            <Table.Th w={110}>Out</Table.Th>
            <Table.Th>Last minutes</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {running.map((channel) => (
            <Table.Tr key={channel.channel}>
              <Table.Td style={{ wordBreak: 'break-word' }}>
                {channel.channel}
              </Table.Td>
              <Table.Td>
                <Badge
                  size="sm"
                  variant="light"
                  color={channel.now.state === 'active' ? 'teal' : 'yellow'}
                >
                  {channel.now.state}
                </Badge>
              </Table.Td>
              <Table.Td c="dimmed">{forHowLong(channel.now.uptime)}</Table.Td>
              <Table.Td c="dimmed">{channel.now.clients}</Table.Td>
              <Table.Td>
                <Speed speed={channel.now.speed} />
              </Table.Td>
              <Table.Td c="dimmed">{rate(channel.now.source_kbps)}</Table.Td>
              <Table.Td c="dimmed">{rate(channel.now.output_kbps)}</Table.Td>
              <Table.Td>
                <Trend
                  samples={channel.samples}
                  pick={(sample) => sample.speed || null}
                />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
};

// A channel that has stopped, with the readings it ended on: the useful part is the shape
// of the last minutes, not the final row, which only ever says it stopped.
const Stopped = ({ stopped }) => {
  const [open, setOpen] = useState(null);

  if (!stopped || stopped.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No channel has stopped since the last restart.
      </Text>
    );
  }

  return (
    <Stack gap={4}>
      {stopped.map((channel) => {
        const last = channel.samples[channel.samples.length - 1] || {};
        const showing = open === channel.stopped_at;
        return (
          <Stack key={`${channel.channel}-${channel.stopped_at}`} gap={2}>
            <Group gap="xs" wrap="wrap">
              <Text size="sm" c="dimmed" w={80}>
                {shortTime(channel.stopped_at)}
              </Text>
              <Text size="sm">{channel.channel}</Text>
              <Text size="xs" c="dimmed">
                ran {forHowLong(last.uptime)} · ended with {last.clients ?? 0}{' '}
                watching
              </Text>
              <Speed speed={last.speed} />
              <Trend
                samples={channel.samples}
                pick={(sample) => sample.speed || null}
              />
              <Button
                size="compact-xs"
                variant="subtle"
                onClick={() => setOpen(showing ? null : channel.stopped_at)}
              >
                {showing ? 'Hide' : 'The last minutes'}
              </Button>
            </Group>
            {showing && (
              <Table fz="xs" verticalSpacing={2} withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th w={90}>Time</Table.Th>
                    <Table.Th w={90}>State</Table.Th>
                    <Table.Th w={80}>Speed</Table.Th>
                    <Table.Th w={110}>Source</Table.Th>
                    <Table.Th w={110}>Out</Table.Th>
                    <Table.Th>Watching</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {channel.samples.map((sample) => (
                    <Table.Tr key={sample.at}>
                      <Table.Td c="dimmed">{shortTime(sample.at)}</Table.Td>
                      <Table.Td c="dimmed">{sample.state}</Table.Td>
                      <Table.Td>
                        <Speed speed={sample.speed} />
                      </Table.Td>
                      <Table.Td c="dimmed">{rate(sample.source_kbps)}</Table.Td>
                      <Table.Td c="dimmed">{rate(sample.output_kbps)}</Table.Td>
                      <Table.Td c="dimmed">{sample.clients}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            )}
          </Stack>
        );
      })}
    </Stack>
  );
};

const Events = ({ events }) => {
  if (!events || events.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        Nothing has happened to a channel lately. This fills in when a stream
        fails, is swapped for another, or is kept alive through a provider
        closing a working connection.
      </Text>
    );
  }
  return (
    <Table.ScrollContainer minWidth={620} type="native">
      <Table striped highlightOnHover withTableBorder verticalSpacing={4} fz="sm">
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
  );
};

const ChannelHealth = ({ events, running, stopped, settings, onSettings }) => (
  <Stack gap="md">
    <Group justify="space-between" wrap="wrap">
      <Text size="sm" fw={600}>
        Running now
      </Text>
      <Group gap="xs">
        <Text size="xs" c="dimmed">
          Recording
        </Text>
        <Switch
          size="sm"
          aria-label="Record channel health"
          checked={!!settings?.enabled}
          onChange={(event) =>
            onSettings &&
            onSettings({ ...settings, enabled: event.currentTarget.checked })
          }
        />
      </Group>
    </Group>
    <RunningNow running={running} />

    <Text size="sm" fw={600}>
      Stopped
    </Text>
    <Stopped stopped={stopped} />

    <Text size="sm" fw={600}>
      What happened
    </Text>
    <Events events={events} />

    <Text size="xs" c="dimmed">
      Every running channel is read every few seconds, and when one stops the
      last few minutes of readings are kept for as long as switches are. Speed
      is how well ffmpeg is keeping up with real time: under 1.00× for any
      length of time is a stream falling behind, which is what a stall looks
      like before it becomes one. Turning recording off leaves everything else
      working; only the readings stop.
    </Text>
  </Stack>
);

export default ChannelHealth;
