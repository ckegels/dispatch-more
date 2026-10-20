import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  SegmentedControl,
  Stack,
  Switch,
  Table,
  Text,
} from '@mantine/core';
import { useElementSize } from '@mantine/hooks';
import { Download, RefreshCw } from 'lucide-react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import API from '../../api';

// The memory recorder's history (apps/proxy/live_proxy/memory_recorder.py): a sample every 30 s,
// kept on disk for 14 days and through reboots, so when the server fills up again what led to it
// can be read here afterwards. Click the chart or an event for that moment in full: every
// process, and what each web worker and Celery worker was doing.

export const RANGES = [
  { value: '1', label: '1 h' },
  { value: '6', label: '6 h' },
  { value: '24', label: '24 h' },
  { value: '72', label: '3 days' },
  { value: '168', label: '7 days' },
  { value: '336', label: '14 days' },
];

const EVENT_COLORS = {
  reboot: 'red',
  gap: 'gray',
  start: 'teal',
  worker: 'grape',
  jump: 'orange',
  redis: 'yellow',
  stuck: 'red',
};

export const when = (t, hours) => {
  const d = new Date(t * 1000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return hours > 24
    ? `${d.toLocaleDateString([], { weekday: 'short', day: 'numeric' })} ${time}`
    : time;
};

const ago = (seconds) =>
  seconds < 90
    ? `${seconds} s ago`
    : seconds < 5400
      ? `${Math.round(seconds / 60)} min ago`
      : `${Math.round(seconds / 3600)} h ago`;

const mb = (value) => (value === null || value === undefined ? '-' : `${Math.round(value)} MB`);

export const Moment = ({ moment }) => {
  const sys = moment.sys || {};
  const cg = moment.cg && !moment.cg.error ? moment.cg : null;
  const redis = moment.redis || {};
  const inflight = moment.inflight || {};
  return (
    <Stack gap={6}>
      <Text size="sm" fw={600}>
        {new Date(moment.t * 1000).toLocaleString()}
        {moment.start && (
          <Badge ml={8} size="xs" color="teal" variant="light">
            recorder started
          </Badge>
        )}
        {moment.every === 5 && (
          <Badge ml={8} size="xs" color="orange" variant="light">
            sampling every 5 s
          </Badge>
        )}
      </Text>
      <Text size="xs">
        Machine: {mb(sys.total - sys.available)} of {mb(sys.total)} used, swap{' '}
        {mb(sys.swap_total - sys.swap_free)} of {mb(sys.swap_total)}
        {cg && (
          <>
            {' '}
            · container {mb(cg.current)} of {cg.max ? mb(cg.max) : 'no limit'} (
            {mb(cg.anon)} program memory, {mb(cg.file)} cache)
          </>
        )}
        {' '}· Redis{' '}
        {redis.error ? (
          <Text span c="red" size="xs">
            did not answer ({redis.error})
          </Text>
        ) : (
          `${redis.ms} ms, ${mb(redis.used)}`
        )}
      </Text>
      <Table verticalSpacing={2} fz="xs" striped>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Process</Table.Th>
            <Table.Th>What</Table.Th>
            <Table.Th>Memory</Table.Th>
            <Table.Th>Swap</Table.Th>
            <Table.Th>State</Table.Th>
            <Table.Th>Running since</Table.Th>
            <Table.Th>Doing</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {(moment.procs || []).map((p) => {
            const [pid, , kind, rss, pss, swap, state, , , started] = p;
            const doing = inflight[String(pid)];
            const stale = doing && moment.t - doing.at > 30;
            return (
              <Table.Tr key={pid}>
                <Table.Td>{pid}</Table.Td>
                <Table.Td>{kind}</Table.Td>
                <Table.Td>
                  {mb(pss ?? rss)}
                  {pss === null && (
                    <Text span size="xs" c="dimmed">
                      {' '}
                      (RSS)
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>{swap ? mb(swap) : ''}</Table.Td>
                <Table.Td>
                  {state === 'D' ? (
                    <Badge size="xs" color="red" variant="light" title="Waiting on memory or disk">
                      stuck (D)
                    </Badge>
                  ) : (
                    state
                  )}
                </Table.Td>
                <Table.Td>{started ? new Date(started * 1000).toLocaleString() : ''}</Table.Td>
                <Table.Td>
                  {doing ? (
                    <Stack gap={0}>
                      {stale && (
                        <Text size="xs" c="red">
                          not updated for {moment.t - doing.at} s (stuck?); last known:
                        </Text>
                      )}
                      {doing.n === 0 && !stale && (
                        <Text size="xs" c="dimmed">
                          nothing
                        </Text>
                      )}
                      {(doing.req || []).slice(0, 8).map(([method, path, client, seconds], i) => (
                        <Text key={i} size="xs" title={client}>
                          {Math.round(seconds)} s {method} {path}
                        </Text>
                      ))}
                      {doing.n > 8 && (
                        <Text size="xs" c="dimmed">
                          and {doing.n - 8} more
                        </Text>
                      )}
                    </Stack>
                  ) : (
                    ''
                  )}
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
      {(moment.others || []).length > 0 && (
        <Text size="xs" c="dimmed">
          Largest other processes:{' '}
          {moment.others.map(([pid, name, rss]) => `${name} ${pid} ${mb(rss)}`).join(', ')}
        </Text>
      )}
    </Stack>
  );
};

const MemoryHistory = () => {
  const [hours, setHours] = useState('24');
  const [history, setHistory] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [moment, setMoment] = useState(null);
  const [momentError, setMomentError] = useState(null);
  const { ref, width } = useElementSize();

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setHistory(await API.getMemoryHistory(hours));
      setError(null);
    } catch {
      setError('Could not read the memory history.');
    } finally {
      setBusy(false);
    }
  }, [hours]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  }, [load]);

  const show = async (t) => {
    setMomentError(null);
    try {
      setMoment(await API.getMemoryMoment(t));
    } catch {
      setMoment(null);
      setMomentError('Nothing was recorded around that time.');
    }
  };

  const switchRecorder = async (on) => {
    try {
      setHistory(await API.setMemoryRecorder(on));
    } catch {
      setError('Could not switch the memory recorder.');
    }
  };

  if (error && !history) return <Alert color="red">{error}</Alert>;
  if (!history) return <Loader size="sm" />;

  const h = Number(hours);
  const points = history.points.map((p) => ({
    ...p,
    label: when(p.t, h),
    level: p.container ?? p.used,
    largestMb: p.largest ? p.largest[1] : null,
  }));
  const limit = points.find((p) => p.limit)?.limit;
  const silent = history.on && (history.last_sample_age === null || history.last_sample_age > 180);

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="flex-start">
        <Switch
          checked={history.on}
          onChange={(e) => switchRecorder(e.currentTarget.checked)}
          label="Record memory history"
          description={`A sample every ${history.every} s (every 5 s while memory rises fast, Redis is slow or something waits on memory), kept ${history.keep_days} days in ${history.folder}, also through reboots.`}
        />
        <Group gap="xs">
          <Button
            size="xs"
            variant="default"
            leftSection={<Download size={14} />}
            onClick={() =>
              API.downloadMemoryHistory(h, `dispatch-more-memory-${hours}h.zip`)
            }
            disabled={history.days.length === 0}
          >
            Download
          </Button>
          <Button
            size="xs"
            variant="default"
            leftSection={<RefreshCw size={14} />}
            onClick={load}
            loading={busy}
          >
            Look again
          </Button>
        </Group>
      </Group>
      {silent && (
        <Alert color="orange">
          Switched on, but nothing was recorded
          {history.last_sample_age !== null ? ` for ${ago(history.last_sample_age)}` : ' yet'}. The
          recorder runs as a service of its own: on a Linux install see{' '}
          <code>systemctl status dispatch-more-memory</code>.
        </Alert>
      )}
      {!history.on && history.samples === 0 && (
        <Text size="sm" c="dimmed">
          Nothing recorded yet. Switch it on to keep a record that survives reboots and crashes.
        </Text>
      )}
      <Group justify="space-between">
        <SegmentedControl size="xs" value={hours} onChange={setHours} data={RANGES} />
        <Text size="xs" c="dimmed">
          {history.samples} samples
          {history.last_sample ? `, the last ${ago(history.last_sample_age)}` : ''}
        </Text>
      </Group>
      <div ref={ref}>
        {points.length > 1 && (
          <LineChart
            width={width || 800}
            height={260}
            data={points}
            onClick={(e) => e?.activePayload?.[0] && show(e.activePayload[0].payload.t)}
          >
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="label" fontSize={11} minTickGap={30} />
            <YAxis fontSize={11} unit=" MB" width={80} />
            <Tooltip formatter={(value) => mb(value)} />
            <Legend />
            <Line type="monotone" dataKey="level" name="Container / machine in use" stroke="#4dabf7" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="dispatcharr" name="Dispatcharr's processes" stroke="#69db7c" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="largestMb" name="Largest process" stroke="#ff922b" dot={false} isAnimationActive={false} />
            {limit && <ReferenceLine y={limit} stroke="#fa5252" strokeDasharray="4 4" label="limit" />}
            {history.events.map((event, i) => (
              <ReferenceLine
                key={i}
                x={when(event.t, h)}
                stroke={EVENT_COLORS[event.kind] === 'gray' ? '#868e96' : '#fa5252'}
                strokeDasharray={event.kind === 'gap' ? '2 4' : undefined}
              />
            ))}
          </LineChart>
        )}
      </div>
      {history.events.length > 0 && (
        <Table verticalSpacing={2} fz="xs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>When</Table.Th>
              <Table.Th>What happened</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {[...history.events].reverse().map((event, i) => (
              <Table.Tr key={i}>
                <Table.Td>{new Date(event.t * 1000).toLocaleString()}</Table.Td>
                <Table.Td>
                  <Badge size="xs" color={EVENT_COLORS[event.kind] || 'blue'} variant="light" mr={6}>
                    {event.kind}
                  </Badge>
                  {event.text}
                  {event.count > 1 ? ` (${event.count} times)` : ''}
                </Table.Td>
                <Table.Td>
                  <Button size="compact-xs" variant="subtle" onClick={() => show(event.since || event.t)}>
                    {event.since ? 'Just before' : 'Show'}
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      {momentError && <Alert color="gray">{momentError}</Alert>}
      {moment && <Moment moment={moment} />}
    </Stack>
  );
};

export default MemoryHistory;
