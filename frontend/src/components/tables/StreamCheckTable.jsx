import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Play, RefreshCw, Settings, Square } from 'lucide-react';
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
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';
import StreamCheckSettings from '../forms/StreamCheckSettings';
import ProviderLimits from '../forms/ProviderLimits';
import { CustomTable, useTable } from './CustomTable';
import { Logo, Watch } from './StreamParts';

// The Channel Manager's second tab, laid out like its first. Each row is a channel with a
// stream that did not play, opening to every stream on it as it was last found; or, under
// Parked, a stream taken off its channels until it works again.

const PAGE_SIZES = ['25', '50', '100', '250'];
// How often the page asks how a run is going, while one is
const POLL_MS = 3000;

const STATE = {
  ok: { label: 'Plays', color: 'green' },
  failing: { label: 'Failing', color: 'yellow' },
  broken: { label: 'Broken', color: 'red' },
  unchecked: { label: 'Not checked', color: 'gray' },
  fallback: { label: 'fallback', color: 'yellow' },
  gone: { label: 'Gone', color: 'gray' },
  // A picture that looked wrong once, looked at again later in the run before it counts
  suspect: { label: 'Checking again', color: 'cyan' },
  ignored: { label: 'Ignored', color: 'gray' },
};

// How each provider is getting on, while a run goes
const ACCOUNT_COLOR = { 'in use': 'yellow.5', unavailable: 'orange.5' };

const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

const StateBadge = ({ state }) => (
  <Badge
    size="xs"
    variant="light"
    color={(STATE[state] || STATE.unchecked).color}
    style={{ flexShrink: 0 }}
  >
    {(STATE[state] || STATE.unchecked).label}
  </Badge>
);

// The last runs, newest first: green played, red did not
const History = ({ history }) =>
  history?.length ? (
    <Tooltip label="The last runs, newest first">
      <Group gap={2} wrap="nowrap" style={{ flexShrink: 0 }}>
        {history.map((ok, i) => (
          <Box
            key={i}
            w={6}
            h={6}
            style={{
              borderRadius: 3,
              background: ok
                ? 'var(--mantine-color-green-6)'
                : 'var(--mantine-color-red-6)',
            }}
          />
        ))}
      </Group>
    </Tooltip>
  ) : null;

// What kind of failure: only one that does not play at all may ever be parked by autopark;
// the others play something, or are the provider's refusal, and wait for a person
const KIND = {
  dead: { label: 'Does not play', color: 'red' },
  refused: { label: 'Refused', color: 'orange', needsYou: true },
  black: { label: 'Black picture', color: 'grape', needsYou: true },
  frozen: { label: 'Frozen picture', color: 'cyan', needsYou: true },
  placeholder: {
    label: '"No stream" picture',
    color: 'violet',
    needsYou: true,
  },
};

const KindBadge = ({ result }) => {
  const kind = result && !result.ok && !result.skipped && KIND[result.kind];
  if (!kind) return null;
  return (
    <Tooltip
      label={
        kind.needsYou
          ? 'Autopark never parks this: it is left for you to decide'
          : 'Autopark can park this, when it is turned on'
      }
    >
      <Badge
        size="xs"
        variant="outline"
        color={kind.color}
        style={{ flexShrink: 0 }}
      >
        {kind.label}
      </Badge>
    </Tooltip>
  );
};

// What was found, in words: why it failed, or the picture it showed
const Finding = ({ result }) => {
  if (!result) return null;
  // Looked at but never really checked: the provider would not give a connection
  const skipped = result.skipped;
  const suspect = result.suspect;
  if (suspect) {
    return (
      <Text size="xs" c="cyan.4" style={{ wordBreak: 'break-word' }}>
        {suspect.reason}: looked at again later in this run to be sure
        {suspect.clean
          ? ` (${suspect.clean} clean look${suspect.clean === 1 ? '' : 's'} so far)`
          : ''}
      </Text>
    );
  }
  const text = skipped
    ? `Not checked: ${result.refused || 'the provider gave no connection'}`
    : result.ok
      ? [result.resolution, result.codec].filter(Boolean).join(' · ') || 'plays'
      : result.reason;
  return (
    <Text
      size="xs"
      c={skipped ? 'yellow.5' : result.ok ? 'dimmed' : 'red.4'}
      style={{ wordBreak: 'break-word' }}
    >
      {text}
      {result.checked_at ? ` · ${when(result.checked_at)}` : ''}
    </Text>
  );
};

const StreamLine = ({ stream, channel, onAct }) => (
  <Group gap={6} wrap="nowrap" align="flex-start">
    {stream.custom ? (
      <Box w={22} style={{ flexShrink: 0 }} />
    ) : (
      <Watch stream={stream} />
    )}
    <StateBadge state={stream.state} />
    <Box style={{ minWidth: 0, flex: 1 }}>
      <Group gap={6} wrap="wrap">
        <Text size="xs" style={{ wordBreak: 'break-word' }}>
          {stream.name}
        </Text>
        <Badge size="xs" variant="outline" color="gray">
          {stream.account}
        </Badge>
        <KindBadge result={stream.result} />
        <History history={stream.result?.history} />
      </Group>
      <Finding result={stream.result} />
    </Box>
    {!stream.custom && (
      <Group gap={4} wrap="nowrap" style={{ flexShrink: 0 }}>
        <Button
          size="compact-xs"
          variant="subtle"
          onClick={() => onAct('check', stream)}
        >
          Check again
        </Button>
        <Button
          size="compact-xs"
          variant="light"
          color="yellow"
          onClick={() => onAct('park', stream)}
        >
          Park
        </Button>
        <Button
          size="compact-xs"
          variant="light"
          color="red"
          onClick={() => onAct('remove', stream, channel)}
        >
          Remove
        </Button>
        {['failing', 'broken', 'suspect'].includes(stream.state) && (
          <Tooltip label="Off this list and not checked again. Nothing on the channel changes.">
            <Button
              size="compact-xs"
              variant="subtle"
              color="gray"
              onClick={() => onAct('ignore', stream)}
            >
              Ignore
            </Button>
          </Tooltip>
        )}
      </Group>
    )}
  </Group>
);

const Expanded = ({ row, onAct }) => (
  <Box p="sm" style={{ background: 'rgba(0,0,0,0.18)' }}>
    <Stack gap={8}>
      {row.dead && (
        <Text size="xs" c="red.4">
          Nothing on this channel plays: a viewer gets the fallback, or nothing.
        </Text>
      )}
      {row.streams.map((stream) => (
        <StreamLine
          key={stream.id}
          stream={stream}
          channel={row.channel}
          onAct={onAct}
        />
      ))}
    </Stack>
  </Box>
);

const ACT_TEXT = {
  remove: (stream, channel) => ({
    title: 'Remove this stream?',
    message: `"${stream.name}" comes off ${channel ? `"${channel.name}"` : 'every channel'} for good. The stream itself stays in Dispatcharr, since it is the provider's. Park it instead to have it checked again and put back if it works.`,
    confirmLabel: 'Remove',
  }),
  clear: () => ({
    title: 'Clear the list?',
    message:
      'Every result is forgotten and the list is empty, as if no run had happened. Nothing on your channels changes: no stream is parked, removed or put back. Parked and ignored streams stay as they are.',
    confirmLabel: 'Clear',
  }),
  forget: (stream) => ({
    title: 'Stop keeping this stream?',
    message: `"${stream.name}" stays off the channels it was parked from, and is no longer checked or offered to be put back.`,
    confirmLabel: 'Stop keeping it',
  }),
};

const StreamCheckTable = () => {
  const [data, setData] = useState(null);
  const [show, setShow] = useState('problems');
  const [search, setSearch] = useState('');
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [saving, setSaving] = useState(false);
  const [asking, setAsking] = useState(null);

  const load = useCallback(
    async (quiet = false) => {
      if (!quiet) setLoading(true);
      try {
        // Parked streams come with every answer; the channels shown depend on the filter
        setData(
          await API.getStreamCheck(
            ['parked', 'needs_you', 'ignored'].includes(show)
              ? 'problems'
              : show
          )
        );
        setError(null);
      } catch (e) {
        setError(e?.body?.error || 'Could not load Stream Check.');
      } finally {
        if (!quiet) setLoading(false);
      }
    },
    [show]
  );

  useEffect(() => {
    load();
  }, [load]);

  // While a run is going, what it has found so far comes in as it finds it
  useEffect(() => {
    if (!data?.running) return undefined;
    const timer = setInterval(() => load(true), POLL_MS);
    return () => clearInterval(timer);
  }, [data?.running, load]);

  const act = async (action, stream, channel = null) => {
    setError(null);
    setNotice(null);
    try {
      if (action === 'clear') {
        await API.clearStreamCheck();
        setNotice('The list is clear. Nothing on your channels changed.');
      } else if (action === 'check') {
        await API.runStreamCheck([stream.id]);
        setNotice(
          `Checking "${stream.name}" now, if a login of its provider is free.`
        );
      } else {
        await API.streamCheckAction(action, stream.id, channel?.id ?? null);
        setNotice(
          {
            park: `"${stream.name}" is parked: off its channels, and checked again on every run.`,
            remove: `"${stream.name}" was removed.`,
            restore: `"${stream.name}" is back where it was.`,
            forget: `"${stream.name}" is no longer kept.`,
            ignore: `"${stream.name}" is ignored: off the list and not checked again. Nothing on its channels changed.`,
            unignore: `"${stream.name}" is checked again.`,
          }[action]
        );
      }
      await load(true);
    } catch (e) {
      setError(e?.body?.error || 'That did not work.');
    }
  };

  // Removing and forgetting cannot be undone here, so they are asked about first
  const ask = (action, stream, channel = null) => {
    if (ACT_TEXT[action]) setAsking({ action, stream, channel });
    else act(action, stream, channel);
  };

  const runNow = async () => {
    setError(null);
    try {
      await API.runStreamCheck();
      await load(true);
    } catch (e) {
      setError(e?.body?.error || 'Could not start a check.');
    }
  };

  const stop = async () => {
    await API.stopStreamCheck();
    await load(true);
  };

  const saveSettings = async (settings) => {
    setSaving(true);
    setError(null);
    try {
      await API.saveStreamCheckSettings(settings);
      setShowSettings(false);
      await load(true);
    } catch (e) {
      setError(e?.body?.error || 'Could not save the settings.');
    } finally {
      setSaving(false);
    }
  };

  // The columns are made once; the actions they call are the latest, through here
  const actions = useRef({ act, ask });
  actions.current = { act, ask };

  const rows = useMemo(() => {
    const all =
      show === 'parked'
        ? (data?.parked || []).map((p) => ({
            ...p,
            key: `p:${p.id}`,
            kind: 'parked',
            // The row's id becomes its key below; the stream keeps its own here
            stream: p,
          }))
        : show === 'needs_you'
          ? (data?.rows || []).filter((row) => row.needs_you > 0)
          : data?.rows || [];
    const wanted = search.trim().toLowerCase();
    const found = wanted
      ? all.filter((row) =>
          [
            row.channel?.name,
            row.name,
            ...(row.streams || []).map((s) => s.name),
          ]
            .filter(Boolean)
            .some((name) => name.toLowerCase().includes(wanted))
        )
      : all;
    return found.map((row) => ({ ...row, id: row.key }));
  }, [data, show, search]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const paginatedRows = useMemo(
    () => rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    [rows, pageIndex, pageSize]
  );

  const channelColumns = useMemo(
    () => [
      { id: 'expand', size: 30, enableSorting: false },
      {
        header: 'Status',
        accessorKey: 'status',
        size: 150,
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          return (
            <Group gap={4} wrap="wrap">
              {r.dead && (
                <Badge size="xs" color="red">
                  Nothing plays
                </Badge>
              )}
              {!r.dead && r.broken > 0 && (
                <Badge size="xs" variant="light" color="red">
                  {r.broken} broken
                </Badge>
              )}
              {r.failing > 0 && (
                <Badge size="xs" variant="light" color="yellow">
                  {r.failing} failing
                </Badge>
              )}
              {r.suspects > 0 && (
                <Badge size="xs" variant="outline" color="cyan">
                  {r.suspects} checking again
                </Badge>
              )}
              {r.needs_you > 0 && (
                <Badge size="xs" variant="outline" color="orange">
                  {r.needs_you} need{r.needs_you === 1 ? 's' : ''} you
                </Badge>
              )}
              {!r.broken && !r.failing && !r.suspects && (
                <Badge size="xs" variant="light" color="green">
                  Plays
                </Badge>
              )}
            </Group>
          );
        },
      },
      {
        header: 'Channel',
        accessorKey: 'channel',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const channel = row.original.channel;
          return (
            <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
              <Logo url={channel.logo_url} name={channel.name} />
              <Box style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                  {channel.name}
                </Text>
                <Text size="xs" c="dimmed">
                  {channel.number}
                  {channel.group ? ` · ${channel.group}` : ''} ·{' '}
                  {row.original.working} of{' '}
                  {row.original.streams.filter((s) => !s.custom).length} play
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
        cell: ({ row }) => (
          <Group gap={3} wrap="nowrap" style={{ overflow: 'hidden' }}>
            {row.original.streams.slice(0, 8).map((stream) => (
              <Tooltip
                key={stream.id}
                label={`${stream.name} · ${stream.account}`}
              >
                <Badge
                  size="xs"
                  variant="light"
                  color={(STATE[stream.state] || STATE.unchecked).color}
                >
                  {stream.custom
                    ? 'fb'
                    : (STATE[stream.state] || STATE.unchecked).label}
                </Badge>
              </Tooltip>
            ))}
          </Group>
        ),
      },
    ],
    []
  );

  const parkedColumns = useMemo(
    () => [
      {
        header: 'Stream',
        accessorKey: 'name',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          return (
            <Group
              gap={6}
              wrap="nowrap"
              align="flex-start"
              style={{ minWidth: 0 }}
            >
              {r.gone ? <Box w={22} /> : <Watch stream={r} />}
              <Box style={{ minWidth: 0 }}>
                <Group gap={6} wrap="wrap">
                  <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                    {r.name}
                  </Text>
                  {r.account && (
                    <Badge size="xs" variant="outline" color="gray">
                      {r.account}
                    </Badge>
                  )}
                </Group>
                <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>
                  {r.auto ? 'Parked by autopark ' : 'Parked '}
                  {when(r.parked_at)} from{' '}
                  {r.from.map((c) => c.name).join(', ') || 'no channel'}
                  {r.auto && r.park_reason ? ` · ${r.park_reason}` : ''}
                </Text>
              </Box>
            </Group>
          );
        },
      },
      {
        header: 'Latest check',
        accessorKey: 'state',
        size: 260,
        enableSorting: false,
        cell: ({ row }) => (
          <Stack gap={2}>
            <Group gap={6}>
              <StateBadge state={row.original.state} />
              <History history={row.original.result?.history} />
            </Group>
            {row.original.gone ? (
              <Text size="xs" c="dimmed">
                The provider no longer lists it
              </Text>
            ) : (
              <Finding result={row.original.result} />
            )}
          </Stack>
        ),
      },
      {
        header: 'Actions',
        accessorKey: 'actions',
        size: 260,
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          return (
            <Group gap={4} wrap="nowrap">
              <Button
                size="compact-xs"
                variant="light"
                color="green"
                disabled={r.gone || !r.from.length}
                onClick={() => actions.current.act('restore', r.stream)}
              >
                Put back
              </Button>
              <Button
                size="compact-xs"
                variant="subtle"
                disabled={r.gone}
                onClick={() => actions.current.act('check', r.stream)}
              >
                Check again
              </Button>
              <Button
                size="compact-xs"
                variant="subtle"
                color="red"
                onClick={() => actions.current.ask('forget', r.stream)}
              >
                Stop keeping
              </Button>
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
    columns: show === 'parked' ? parkedColumns : channelColumns,
    data: paginatedRows,
    allRowIds: paginatedRows.map((row) => row.id),
    enablePagination: false,
    enableRowSelection: false,
    enableRowVirtualization: false,
    renderTopToolbar: false,
    manualSorting: false,
    manualFiltering: false,
    manualPagination: true,
    expandedRowRenderer: ({ row }) => (
      <Expanded row={row.original} onAct={ask} />
    ),
    headerCellRenderFns: {
      status: renderHeaderCell,
      channel: renderHeaderCell,
      streams: renderHeaderCell,
      name: renderHeaderCell,
      state: renderHeaderCell,
      actions: renderHeaderCell,
    },
  });

  const progress = data?.progress || {};
  const lastRun = data?.last_run || {};
  const rule = data?.settings?.only_when_idle
    ? 'and only while nothing is playing'
    : 'and never on a provider someone is using';
  // Providers the last finished run left alone: expired, refused the login, or down
  const skipped = !data?.running ? lastRun.unavailable || [] : [];
  const first = rows.length ? pageIndex * pageSize + 1 : 0;
  const last = Math.min((pageIndex + 1) * pageSize, rows.length);
  const asked =
    asking && ACT_TEXT[asking.action](asking.stream, asking.channel);

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
                    { value: 'problems', label: 'Broken or failing' },
                    { value: 'broken', label: 'Broken only' },
                    { value: 'needs_you', label: 'Needs you' },
                    {
                      value: 'parked',
                      label: `Parked (${data?.parked?.length ?? 0})`,
                    },
                    {
                      value: 'ignored',
                      label: `Ignored (${data?.ignored?.length ?? 0})`,
                    },
                    { value: 'all', label: 'Every channel checked' },
                  ]}
                  size="xs"
                  style={{ width: 190 }}
                />
              </Group>

              <Group gap="sm">
                <Tooltip
                  label={
                    data?.running
                      ? 'Stop the check first'
                      : 'Empty the list. Nothing on your channels changes.'
                  }
                >
                  <Button
                    variant="default"
                    size="xs"
                    disabled={!!data?.running}
                    onClick={() => setAsking({ action: 'clear', stream: {} })}
                  >
                    Clear list
                  </Button>
                </Tooltip>
                <Button
                  leftSection={<Settings size={16} />}
                  variant="default"
                  size="xs"
                  onClick={() => setShowSettings(!showSettings)}
                >
                  {showSettings ? 'Hide Settings' : 'Settings'}
                </Button>
                <Tooltip label="Reload">
                  <ActionIcon
                    variant="default"
                    size="md"
                    aria-label="Reload"
                    onClick={() => load()}
                  >
                    <RefreshCw size={14} />
                  </ActionIcon>
                </Tooltip>
                {data?.running ? (
                  <Button
                    leftSection={<Square size={14} />}
                    color="red"
                    variant="light"
                    size="xs"
                    onClick={stop}
                  >
                    Stop
                  </Button>
                ) : (
                  <Button
                    leftSection={<Play size={16} />}
                    size="xs"
                    disabled={!data}
                    onClick={runNow}
                  >
                    Check all now
                  </Button>
                )}
              </Group>
            </Box>

            {/* How a run is going, or how the last one went */}
            <Box
              style={{ padding: '8px 16px', borderBottom: '1px solid #3f3f46' }}
            >
              <Text size="xs" c="dimmed">
                {data?.running
                  ? `Checking: ${progress.done || 0} of ${progress.total || 0} streams${
                      progress.broken
                        ? ` · ${progress.broken} broken so far`
                        : ''
                    }${progress.waiting ? ` · paused: ${progress.message || 'waiting'}` : ''}${
                      progress.next_batch_at && progress.waiting
                        ? ` · trying again at ${new Date(progress.next_batch_at).toLocaleTimeString()}`
                        : ''
                    }`
                  : lastRun.finished_at
                    ? `Last run ${when(lastRun.finished_at)}: ${lastRun.checked || 0} of ${
                        lastRun.total || 0
                      } streams looked at.`
                    : 'Not run yet.'}
                {' — '}
                {data?.settings?.enabled
                  ? `Runs by itself every ${data.settings.every_hours} h${
                      data.settings.window_from
                        ? `, between ${data.settings.window_from} and ${data.settings.window_to}`
                        : ''
                    }, ${rule}.`
                  : `Only runs when started here, ${rule}.`}
              </Text>
              {data?.running && (
                <Group gap="md" mt={4}>
                  {Object.values(progress.accounts || {}).map((account) => (
                    <Text
                      key={account.name}
                      size="xs"
                      c={ACCOUNT_COLOR[account.status] || 'dimmed'}
                    >
                      {account.name}: {account.done} checked
                      {account.left > 0 ? `, ${account.left} left` : ''}
                      {account.status && account.status !== 'checking'
                        ? ` · ${account.status}`
                        : ''}
                      {account.reason ? ` (${account.reason})` : ''}
                      {account.until
                        ? ` · until ${new Date(account.until).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                        : ''}
                      {account.status === 'checking' && account.now
                        ? ` · ${account.now}`
                        : ''}
                    </Text>
                  ))}
                </Group>
              )}
            </Box>

            {show === 'parked' && (data?.hidden_channels || []).length > 0 && (
              <Box px="md" pt="sm">
                <Alert color="gray" variant="light" p="xs">
                  <Text size="xs">
                    Hidden from TVs and media servers, every real stream of
                    theirs being parked:{' '}
                    {data.hidden_channels
                      .map((c) => `${c.number ?? ''} ${c.name}`.trim())
                      .join(', ')}
                    . Each is shown again when one of its streams is put back.
                  </Text>
                </Alert>
              </Box>
            )}

            {(error ||
              notice ||
              skipped.length > 0 ||
              (data && !data.ffprobe)) && (
              <Stack
                gap="xs"
                p="md"
                style={{ borderBottom: '1px solid #3f3f46' }}
              >
                {error && <Alert color="red">{error}</Alert>}
                {notice && (
                  <Alert
                    color="blue"
                    withCloseButton
                    onClose={() => setNotice(null)}
                  >
                    {notice}
                  </Alert>
                )}
                {skipped.length > 0 && (
                  <Alert color="orange" title="Not checked in the last run">
                    {skipped.map((account) => (
                      <Text key={account.name} size="xs">
                        {account.name}: {account.reason}. Its streams were left
                        as they were, not counted as failing.
                      </Text>
                    ))}
                  </Alert>
                )}
                {data && !data.ffprobe && (
                  <Alert color="yellow">
                    ffprobe is not installed here, so a stream is judged only by
                    whether MPEG-TS keeps coming. HLS and other formats may be
                    called broken when they are not.
                  </Alert>
                )}
              </Stack>
            )}

            {showSettings && data?.settings && (
              <Box p="md" style={{ borderBottom: '1px solid #3f3f46' }}>
                <StreamCheckSettings
                  value={data.settings}
                  groups={data.channel_groups}
                  onSave={saveSettings}
                  onClear={() => setAsking({ action: 'clear', stream: {} })}
                  saving={saving}
                />
                <Box mt="lg">
                  <ProviderLimits
                    limits={data.limits}
                    onSave={async (row, limit, minutes) => {
                      try {
                        await API.setStreamCheckLimit(
                          row.key,
                          row.name,
                          limit,
                          minutes
                        );
                        await load(true);
                      } catch (e) {
                        setError(
                          e?.body?.error ||
                            'Could not change what that provider allows.'
                        );
                      }
                    }}
                  />
                </Box>
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
                  {show === 'ignored' ? (
                    <Stack gap={6} p="sm">
                      {(data?.ignored || []).length === 0 && (
                        <Text size="sm" c="dimmed" ta="center" p="xl">
                          No stream is ignored.
                        </Text>
                      )}
                      {(data?.ignored || []).map((stream) => (
                        <Group
                          key={stream.id}
                          gap={6}
                          wrap="nowrap"
                          align="flex-start"
                        >
                          <Box style={{ minWidth: 0, flex: 1 }}>
                            <Group gap={6} wrap="wrap">
                              <Text
                                size="xs"
                                style={{ wordBreak: 'break-word' }}
                              >
                                {stream.name}
                              </Text>
                              {stream.account && (
                                <Badge size="xs" variant="outline" color="gray">
                                  {stream.account}
                                </Badge>
                              )}
                            </Group>
                            <Text
                              size="xs"
                              c="dimmed"
                              style={{ wordBreak: 'break-word' }}
                            >
                              Ignored {when(stream.ignored_at)}
                              {stream.ignore_reason
                                ? ` · ${stream.ignore_reason}`
                                : ''}
                            </Text>
                          </Box>
                          <Button
                            size="compact-xs"
                            variant="light"
                            onClick={() => act('unignore', stream)}
                          >
                            Stop ignoring
                          </Button>
                        </Group>
                      ))}
                    </Stack>
                  ) : rows.length === 0 && !loading ? (
                    <Center p="xl">
                      <Text size="sm" c="dimmed">
                        {!data
                          ? ''
                          : show === 'parked'
                            ? 'No streams are parked.'
                            : lastRun.finished_at || data.running
                              ? 'Every stream checked plays.'
                              : 'Nothing checked yet: start a check, or turn it on in Settings.'}
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
                      : show === 'parked'
                        ? '0 streams'
                        : '0 channels'}
                  </Text>
                </Group>
              </Box>
            </Box>
          </Paper>
        </Stack>
      </Box>

      <ConfirmationDialog
        opened={!!asking}
        onClose={() => setAsking(null)}
        onConfirm={() => {
          const { action, stream, channel } = asking;
          setAsking(null);
          act(action, stream, channel);
        }}
        title={asked?.title}
        message={asked?.message}
        confirmLabel={asked?.confirmLabel}
      />
    </>
  );
};

export default StreamCheckTable;
