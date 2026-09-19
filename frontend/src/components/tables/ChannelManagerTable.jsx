import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronsDownUp,
  ChevronsUpDown,
  Play,
  RotateCcw,
  SlidersHorizontal,
} from 'lucide-react';
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  LoadingOverlay,
  NativeSelect,
  Pagination,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Tooltip,
  useMantineTheme,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';
import ChannelManagerLevers from '../forms/ChannelManagerLevers';
import { CustomTable, useTable } from './CustomTable';
import { Logo, Watch } from './StreamParts';

// Laid out like Find Logos and the Logo Manager tabs: the same panel, toolbar, table and
// pagination. Each row is one channel as it would come out, and opens to show every stream
// that goes into it, before and after.

const PAGE_SIZES = ['25', '50', '100', '250'];

const STATUS = {
  new: { label: 'New', color: 'green' },
  merge: { label: 'Merge', color: 'blue' },
  conflict: { label: 'Conflict', color: 'orange' },
  unchanged: { label: 'Unchanged', color: 'gray' },
};

// What is being looked at, which going back to the defaults leaves as it is
const SCOPE_LEVERS = [
  'accounts',
  'stream_groups',
  'channel_groups',
  'target_group',
  'profiles',
];

const QUALITY_COLOR = { '4K': 'grape', FHD: 'teal', HD: 'blue', SD: 'gray' };

const Quality = ({ stream }) =>
  stream.custom ? (
    <Badge size="xs" variant="light" color="yellow">
      fallback
    </Badge>
  ) : (
    <Badge
      size="xs"
      variant="light"
      color={QUALITY_COLOR[stream.quality] || 'dark'}
    >
      {stream.quality || '?'}
      {stream.probed ? ' ✓' : ''}
    </Badge>
  );

// Up and down, for putting a channel's streams in the order they should be tried
const Move = ({ stream, onMove, first, last }) => (
  <Group gap={0} wrap="nowrap" style={{ flexShrink: 0 }}>
    <ActionIcon
      size="xs"
      variant="subtle"
      color="gray"
      aria-label={`Move ${stream.name} up`}
      disabled={first}
      onClick={() => onMove(stream.id, -1)}
    >
      <ArrowUp size={12} />
    </ActionIcon>
    <ActionIcon
      size="xs"
      variant="subtle"
      color="gray"
      aria-label={`Move ${stream.name} down`}
      disabled={last}
      onClick={() => onMove(stream.id, 1)}
    >
      <ArrowDown size={12} />
    </ActionIcon>
  </Group>
);

// One stream, with everything worth knowing about it on one line
// The name first; what it is and where it is from underneath, on as many lines as it takes,
// so a long one grows the row downwards rather than running off to the right
const StreamLine = ({ stream, move }) => (
  <Group
    gap={6}
    wrap="nowrap"
    align="flex-start"
    style={{
      minWidth: 0,
      opacity: stream.in_scope === false && !stream.custom ? 0.6 : 1,
      textDecoration: stream.removed ? 'line-through' : 'none',
    }}
  >
    {move}
    <Watch stream={stream} />
    <Quality stream={stream} />
    <Box style={{ minWidth: 0, flex: 1 }}>
      <Text
        size="xs"
        c={stream.removed ? 'red' : stream.added ? 'green' : undefined}
        style={{ wordBreak: 'break-word' }}
      >
        {stream.added ? '+ ' : stream.removed ? '− ' : ''}
        {stream.name}
      </Text>
      <Group gap={6} wrap="wrap">
        <Badge size="xs" variant="outline" color="gray">
          {stream.account}
        </Badge>
        {stream.group && (
          <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>
            {stream.group}
          </Text>
        )}
        {stream.tvg_id && (
          <Text
            size="xs"
            c="dimmed"
            ff="monospace"
            style={{ wordBreak: 'break-all' }}
          >
            {stream.tvg_id}
          </Text>
        )}
      </Group>
    </Box>
  </Group>
);

// The streams that can be moved: not one taken off, and not the fallback, which stays last
const movable = (stream) => !stream.removed && !stream.custom;

const Expanded = ({ row, onMove, groups, chosenGroup, onGroup }) => {
  const moving = row.streams.filter(movable);
  return (
    <Box
      p="sm"
      style={{ background: 'rgba(0,0,0,0.18)', minWidth: 0, maxWidth: '100%' }}
    >
      {row.status === 'new' && (
        <Group gap="xs" mb="sm" align="flex-end" wrap="wrap">
          <Select
            size="xs"
            label="Channel group"
            aria-label={`Channel group for ${row.channel.name}`}
            searchable
            allowDeselect={false}
            data={groups}
            value={String(chosenGroup ?? row.channel.group_id ?? '')}
            onChange={(group) => group && onGroup(row.key, Number(group))}
            style={{ width: 260 }}
          />
          <Text size="xs" c="dimmed" pb={6}>
            {chosenGroup && chosenGroup !== row.channel.group_id
              ? 'Chosen by you; numbered after the last channel of that group when applied.'
              : `Suggested: ${row.channel.group_why || 'its streams’ group'}. Number ${row.channel.number}.`}
          </Text>
        </Group>
      )}
      {row.status === 'conflict' ? (
        <Stack gap={6}>
          <Text size="xs" c="orange">
            These streams could belong to any of these channels, so nothing is
            done with them. Rename one of the channels, or give one a tvg-id or
            an alias, to settle which.
          </Text>
          {(row.candidates || []).map((candidate) => (
            <Group key={candidate.id} gap="xs">
              <Logo url={candidate.logo_url} name={candidate.name} />
              <Text size="sm">{candidate.name}</Text>
              <Text size="xs" c="dimmed">
                {candidate.number} · {candidate.group}
              </Text>
            </Group>
          ))}
          {row.before.streams.map((stream) => (
            <StreamLine key={stream.id} stream={stream} />
          ))}
        </Stack>
      ) : (
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
          <Stack gap={4} style={{ minWidth: 0 }}>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase">
              Before · {row.before.streams.length} stream
              {row.before.streams.length === 1 ? '' : 's'}
            </Text>
            {row.before.streams.length === 0 ? (
              <Text size="xs" c="dimmed">
                {row.status === 'new' ? 'No channel yet' : 'No streams'}
              </Text>
            ) : (
              row.before.streams.map((stream) => (
                <StreamLine key={stream.id} stream={stream} />
              ))
            )}
          </Stack>
          <Stack gap={4} style={{ minWidth: 0 }}>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase">
              After · in the order they are tried
            </Text>
            {row.streams.map((stream) => {
              const at = moving.indexOf(stream);
              return (
                <StreamLine
                  key={`${stream.id}-${stream.removed}`}
                  stream={stream}
                  move={
                    at === -1 ? (
                      // Keeps the names lined up with the ones that can move
                      <Box w={44} style={{ flexShrink: 0 }} />
                    ) : (
                      <Move
                        stream={stream}
                        first={at === 0}
                        last={at === moving.length - 1}
                        onMove={(id, by) => onMove(row.key, id, by)}
                      />
                    )
                  }
                />
              );
            })}
          </Stack>
        </SimpleGrid>
      )}
    </Box>
  );
};

const ChannelManagerTable = () => {
  const theme = useMantineTheme();

  const [options, setOptions] = useState(null);
  const [levers, setLevers] = useState(null);
  const [plan, setPlan] = useState(null);
  const [previewed, setPreviewed] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showLevers, setShowLevers] = useState(false);
  // Remounts the levers after a reset, so their text boxes show the reset values
  const [leverReset, setLeverReset] = useState(0);
  const [show, setShow] = useState('changes');
  const [search, setSearch] = useState('');
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [ticked, setTicked] = useState(new Set());
  // Streams put in another order by hand, by row: {key: [stream ids]}
  const [orders, setOrders] = useState({});
  // New channels put in another group than suggested, by row: {key: group id}
  const [groupChoice, setGroupChoice] = useState({});
  const [expandAll, setExpandAll] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const tableRef = useRef(null);

  // The plan is worked out on the server and only when asked: it reads every stream, and
  // a lever moved should not set it off on every keystroke
  const preview = useCallback(async (withLevers) => {
    setLoading(true);
    setError(null);
    try {
      const result = await API.previewChannelManager(withLevers);
      setPlan(result);
      setPreviewed(JSON.stringify(withLevers));
      setTicked(new Set());
      setOrders({});
      setGroupChoice({});
      tableRef.current?.setSelectedTableIds?.([]);
      // Kept, so the page opens the way it was left
      API.saveChannelManagerSettings(withLevers).catch(() => {});
    } catch (e) {
      setError(e?.body?.error || 'Could not work out the channels.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const data = await API.getChannelManagerOptions();
        setOptions(data);
        setLevers(data.settings);
        await preview(data.settings);
      } catch (e) {
        setError(e?.body?.error || 'Could not load the Channel Manager.');
      }
    })();
  }, [preview]);

  const leversChanged =
    levers && previewed && JSON.stringify(levers) !== previewed;

  const groupOptions = useMemo(
    () =>
      (options?.all_groups || []).map((g) => ({
        value: String(g.id),
        label: g.name,
      })),
    [options]
  );
  const groupNames = useMemo(
    () =>
      Object.fromEntries(
        (options?.all_groups || []).map((g) => [g.id, g.name])
      ),
    [options]
  );

  const rows = useMemo(() => {
    const all = (plan?.rows || []).map((raw) => {
      const chosen = groupChoice[raw.key];
      const row =
        chosen && chosen !== raw.channel?.group_id
          ? { ...raw, chosenGroup: groupNames[chosen] || String(chosen) }
          : raw;
      const order = orders[row.key];
      if (!order) return { ...row, id: row.key };
      // The streams as they were put by hand; the fallback and anything taken off stay
      // where the plan has them, at the end
      const byId = Object.fromEntries(row.streams.map((s) => [s.id, s]));
      const moved = order.map((id) => byId[id]).filter(Boolean);
      const rest = row.streams.filter((s) => !movable(s));
      return {
        ...row,
        id: row.key,
        streams: [...moved, ...rest],
        reordered: true,
      };
    });
    const byStatus = all.filter((row) =>
      show === 'all'
        ? true
        : show === 'changes'
          ? row.status === 'new' ||
            row.status === 'merge' ||
            row.status === 'conflict' ||
            row.reordered
          : row.status === show
    );
    const wanted = search.trim().toLowerCase();
    if (!wanted) return byStatus;
    return byStatus.filter((row) =>
      [
        row.channel?.name,
        row.before?.channel?.name,
        ...row.before.streams.map((s) => s.name),
      ]
        .filter(Boolean)
        .some((name) => name.toLowerCase().includes(wanted))
    );
  }, [plan, show, search, orders, groupChoice, groupNames]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const paginatedRows = useMemo(
    () => rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    [rows, pageIndex, pageSize]
  );

  // Only what can be applied counts as chosen: a conflict or an unchanged row cannot be
  const tickedKeys = useMemo(
    () =>
      rows
        .filter(
          (row) =>
            ticked.has(row.key) &&
            (row.status === 'new' || row.status === 'merge' || row.reordered)
        )
        .map((row) => row.key),
    [rows, ticked]
  );

  const apply = async () => {
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      const given = Object.fromEntries(
        tickedKeys.filter((key) => orders[key]).map((key) => [key, orders[key]])
      );
      const groups = Object.fromEntries(
        tickedKeys
          .filter((key) => groupChoice[key])
          .map((key) => [key, groupChoice[key]])
      );
      await API.applyChannelManager(levers, tickedKeys, given, groups);
      await preview(levers);
    } catch (e) {
      setError(e?.body?.error || 'Could not apply those channels.');
    } finally {
      setBusy(false);
    }
  };

  // A stream moved one place up or down. The row is ticked with it: an order put by hand
  // is something to apply, and would otherwise be easy to lose.
  const moveStream = useCallback(
    (key, id, by) => {
      const row = (plan?.rows || []).find((r) => r.key === key);
      if (!row) return;
      const current =
        orders[key] || row.streams.filter(movable).map((s) => s.id);
      const from = current.indexOf(id);
      const to = from + by;
      if (from === -1 || to < 0 || to >= current.length) return;
      const next = [...current];
      [next[from], next[to]] = [next[to], next[from]];
      setOrders((all) => ({ ...all, [key]: next }));
      setTicked((all) => {
        const now = new Set(all).add(key);
        tableRef.current?.setSelectedTableIds?.([...now]);
        return now;
      });
    },
    [plan, orders]
  );

  const chooseGroup = useCallback((key, group) => {
    setGroupChoice((all) => ({ ...all, [key]: group }));
    setTicked((all) => {
      const now = new Set(all).add(key);
      tableRef.current?.setSelectedTableIds?.([...now]);
      return now;
    });
  }, []);

  const columns = useMemo(
    () => [
      { id: 'select', size: 50, enableSorting: false },
      { id: 'expand', size: 30, enableSorting: false },
      {
        header: 'Status',
        accessorKey: 'status',
        size: 120,
        cell: ({ row }) => {
          const r = row.original;
          return (
            <Group gap={4} wrap="nowrap">
              <Badge size="xs" variant="light" color={STATUS[r.status].color}>
                {STATUS[r.status].label}
              </Badge>
              {r.reordered && (
                <Badge size="xs" variant="light" color="cyan">
                  Reordered
                </Badge>
              )}
              {r.adds > 0 && (
                <Text size="xs" c="green">
                  +{r.adds}
                </Text>
              )}
              {r.removes > 0 && (
                <Text size="xs" c="red">
                  −{r.removes}
                </Text>
              )}
            </Group>
          );
        },
      },
      {
        header: 'Before',
        accessorKey: 'before',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          const channel = r.before.channel;
          const providers = new Set(r.before.streams.map((s) => s.account))
            .size;
          return (
            <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
              <Logo url={channel?.logo_url} name={channel?.name} />
              <Box style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                  {channel
                    ? channel.name
                    : r.status === 'conflict'
                      ? 'Could be several'
                      : 'No channel yet'}
                </Text>
                <Text size="xs" c="dimmed" lineClamp={1}>
                  {r.before.streams.length} stream
                  {r.before.streams.length === 1 ? '' : 's'}
                  {providers > 0 &&
                    ` · ${providers} provider${providers === 1 ? '' : 's'}`}
                </Text>
              </Box>
            </Group>
          );
        },
      },
      {
        header: 'After',
        accessorKey: 'channel',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const channel = row.original.channel;
          if (!channel) {
            return (
              <Text size="xs" c="orange">
                Left alone
              </Text>
            );
          }
          const epg = channel.epg;
          return (
            <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
              <Logo url={channel.logo_url} name={channel.name} />
              <Box style={{ minWidth: 0 }}>
                <Group gap={6} wrap="wrap">
                  <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                    {channel.name}
                  </Text>
                  <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
                    {row.original.chosenGroup
                      ? `new number · ${row.original.chosenGroup}`
                      : `${channel.number}${channel.group ? ` · ${channel.group}` : ''}`}
                  </Text>
                </Group>
                <Text
                  size="xs"
                  c={epg ? 'dimmed' : 'orange'}
                  style={{ wordBreak: 'break-word' }}
                >
                  {epg
                    ? `Guide: ${epg.name}${epg.how && epg.how !== 'kept' ? ` (by ${epg.how})` : ''}`
                    : 'No guide'}
                </Text>
              </Box>
            </Group>
          );
        },
      },
      {
        header: 'Streams, in order',
        accessorKey: 'streams',
        size: 230,
        enableSorting: false,
        cell: ({ row }) => {
          const streams = row.original.streams.filter((s) => !s.removed);
          return (
            <Group gap={3} wrap="nowrap" style={{ overflow: 'hidden' }}>
              {streams.slice(0, 7).map((stream) => (
                <Tooltip
                  key={stream.id}
                  label={`${stream.name} · ${stream.account}${stream.added ? ' · added' : ''}`}
                >
                  <Badge
                    size="xs"
                    variant={stream.added ? 'filled' : 'light'}
                    color={
                      stream.custom
                        ? 'yellow'
                        : QUALITY_COLOR[stream.quality] || 'dark'
                    }
                  >
                    {stream.custom ? 'fb' : stream.quality || '?'}
                  </Badge>
                </Tooltip>
              ))}
              {streams.length > 7 && (
                <Text size="xs" c="dimmed">
                  +{streams.length - 7}
                </Text>
              )}
            </Group>
          );
        },
      },
    ],
    []
  );

  const renderHeaderCell = (header) => (
    <Text size="sm" name={header.id}>
      {header.column.columnDef.header}
    </Text>
  );

  const table = useTable({
    columns,
    data: paginatedRows,
    allRowIds: paginatedRows.map((row) => row.id),
    enablePagination: false,
    enableRowSelection: true,
    enableRowVirtualization: false,
    renderTopToolbar: false,
    manualSorting: false,
    manualFiltering: false,
    manualPagination: true,
    onRowSelectionChange: (selected) => setTicked(new Set(selected)),
    expandAll,
    expandedRowRenderer: ({ row }) => (
      <Expanded
        row={row.original}
        onMove={moveStream}
        groups={groupOptions}
        chosenGroup={groupChoice[row.original.key]}
        onGroup={chooseGroup}
      />
    ),
    headerCellRenderFns: {
      status: renderHeaderCell,
      before: renderHeaderCell,
      channel: renderHeaderCell,
      streams: renderHeaderCell,
    },
  });

  useEffect(() => {
    tableRef.current = table;
  }, [table]);

  const summary = plan?.summary || {};
  const first = rows.length ? pageIndex * pageSize + 1 : 0;
  const last = Math.min((pageIndex + 1) * pageSize, rows.length);

  return (
    <>
      <Box
        style={{
          display: 'flex',
          justifyContent: 'center',
          padding: '0px',
          minHeight: 'calc(100vh - 200px)',
        }}
      >
        <Stack gap="md" style={{ maxWidth: '1200px', width: '100%' }}>
          <Paper
            style={{
              backgroundColor: '#27272A',
              border: '1px solid #3f3f46',
              borderRadius: 'var(--mantine-radius-md)',
            }}
          >
            {/* Top toolbar */}
            <Box
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                flexWrap: 'wrap',
                gap: 8,
                padding: '16px',
                borderBottom: '1px solid #3f3f46',
              }}
            >
              <Group gap="sm">
                <TextInput
                  placeholder="Filter by name..."
                  aria-label="Search channels"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.currentTarget.value);
                    setPageIndex(0);
                  }}
                  size="xs"
                  style={{ width: 200 }}
                />
                <Select
                  aria-label="Which channels"
                  value={show}
                  onChange={(value) => {
                    if (value) setShow(value);
                    setPageIndex(0);
                  }}
                  allowDeselect={false}
                  data={[
                    { value: 'changes', label: 'What would change' },
                    { value: 'new', label: 'New channels' },
                    { value: 'merge', label: 'Merge' },
                    { value: 'conflict', label: 'Conflicts' },
                    { value: 'unchanged', label: 'Unchanged' },
                    { value: 'all', label: 'Everything' },
                  ]}
                  size="xs"
                  style={{ width: 170 }}
                />
              </Group>

              <Group gap="sm">
                <Button
                  variant="default"
                  size="xs"
                  leftSection={
                    expandAll ? (
                      <ChevronsDownUp size={16} />
                    ) : (
                      <ChevronsUpDown size={16} />
                    )
                  }
                  onClick={() => setExpandAll(!expandAll)}
                >
                  {expandAll ? 'Collapse all' : 'Expand all'}
                </Button>
                <Button
                  leftSection={<SlidersHorizontal size={16} />}
                  variant="default"
                  size="xs"
                  onClick={() => setShowLevers(!showLevers)}
                >
                  {showLevers ? 'Hide Levers' : 'Levers'}
                </Button>
                <Button
                  leftSection={<Play size={16} />}
                  variant={leversChanged ? 'filled' : 'light'}
                  size="xs"
                  loading={loading}
                  disabled={!levers}
                  onClick={() => preview(levers)}
                >
                  Preview
                </Button>
                <Button
                  leftSection={<Check size={18} />}
                  variant="light"
                  size="xs"
                  p={5}
                  disabled={!tickedKeys.length || busy || leversChanged}
                  loading={busy}
                  onClick={() => setConfirming(true)}
                  color={theme.tailwind.green[5]}
                  style={{
                    borderWidth: '1px',
                    borderColor: theme.tailwind.green[5],
                    color: 'white',
                  }}
                >
                  Apply {tickedKeys.length ? `(${tickedKeys.length})` : ''}
                </Button>
              </Group>
            </Box>

            {/* What the plan comes to */}
            <Box
              style={{ padding: '8px 16px', borderBottom: '1px solid #3f3f46' }}
            >
              <Text size="xs" c="dimmed">
                {plan
                  ? `${(summary.streams || 0).toLocaleString()} streams looked at · ${
                      summary.merge || 0
                    } channels gain ${(summary.streams_added || 0).toLocaleString()} streams · ${
                      summary.new || 0
                    } new · ${summary.conflict || 0} conflicts · ${summary.unchanged || 0} unchanged`
                  : 'Working out the channels…'}
                {' — '}
                Open a row to see every stream before and after. Nothing changes
                until channels are ticked and applied.
              </Text>
            </Box>

            {(error || leversChanged) && (
              <Stack
                gap="xs"
                p="md"
                style={{ borderBottom: '1px solid #3f3f46' }}
              >
                {error && <Alert color="red">{error}</Alert>}
                {leversChanged && (
                  <Alert color="blue">
                    The levers have changed. Preview again to see what they come
                    to before applying anything.
                  </Alert>
                )}
              </Stack>
            )}

            {showLevers && options && levers && (
              <Box p="md" style={{ borderBottom: '1px solid #3f3f46' }}>
                <Group justify="flex-end" mb="sm">
                  <Button
                    variant="subtle"
                    size="xs"
                    leftSection={<RotateCcw size={14} />}
                    onClick={() => {
                      // Back to matching as DispatcharrUtils does; what is looked at stays
                      const scope = Object.fromEntries(
                        SCOPE_LEVERS.map((name) => [name, levers[name]])
                      );
                      setLevers({ ...options.defaults, ...scope });
                      setLeverReset((n) => n + 1);
                    }}
                  >
                    Back to the defaults
                  </Button>
                </Group>
                <ChannelManagerLevers
                  key={leverReset}
                  options={options}
                  value={levers}
                  onChange={setLevers}
                />
              </Box>
            )}

            {/* Table container */}
            <Box
              style={{
                position: 'relative',
                borderRadius:
                  '0 0 var(--mantine-radius-md) var(--mantine-radius-md)',
              }}
            >
              <Box style={{ overflow: 'auto', height: 'calc(100vh - 200px)' }}>
                <div style={{ minWidth: 760 }}>
                  <LoadingOverlay visible={loading} />
                  {rows.length === 0 && !loading ? (
                    <Center p="xl">
                      <Text size="sm" c="dimmed">
                        {plan ? 'Nothing to show.' : ''}
                      </Text>
                    </Center>
                  ) : (
                    <CustomTable table={table} />
                  )}
                </div>
              </Box>

              {/* Pagination Controls */}
              <Box
                style={{
                  position: 'sticky',
                  bottom: 0,
                  zIndex: 3,
                  backgroundColor: '#27272A',
                  borderTop: '1px solid #3f3f46',
                }}
              >
                <Group gap={5} justify="center" style={{ padding: 8 }}>
                  <Text size="xs">Page Size</Text>
                  <NativeSelect
                    size="xxs"
                    value={String(pageSize)}
                    data={PAGE_SIZES}
                    onChange={(event) => {
                      setPageSize(parseInt(event.target.value, 10));
                      setPageIndex(0);
                    }}
                    style={{ paddingRight: 20 }}
                  />
                  <Pagination
                    total={pageCount}
                    value={pageIndex + 1}
                    onChange={(page) => setPageIndex(page - 1)}
                    size="xs"
                    withEdges
                    style={{ paddingRight: 20 }}
                  />
                  <Text size="xs">
                    {rows.length
                      ? `${first} to ${last} of ${rows.length}`
                      : '0 channels'}
                  </Text>
                </Group>
              </Box>
            </Box>
          </Paper>
        </Stack>
      </Box>

      <ConfirmationDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={apply}
        title={`Apply ${tickedKeys.length} channel${tickedKeys.length === 1 ? '' : 's'}?`}
        message="Each ticked channel becomes what its row shows: new channels are made in the group shown, numbered after the last channel of that group, and channels you have gain the streams marked +. It is worked out again as it is applied, so what is applied is what is true now. Custom fallback streams stay last."
        confirmLabel="Apply"
      />
    </>
  );
};

export default ChannelManagerTable;
