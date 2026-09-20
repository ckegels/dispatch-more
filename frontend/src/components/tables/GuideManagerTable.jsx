import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CirclePlay, EyeOff, Play, RotateCcw, Square } from 'lucide-react';
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  LoadingOverlay,
  NumberInput,
  Paper,
  Progress,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import API from '../../api';
import useVideoStore from '../../store/useVideoStore';
import useSettingsStore from '../../store/settings';
import { buildLiveStreamUrl } from '../../utils/components/FloatingVideoUtils.js';
import ConfirmationDialog from '../ConfirmationDialog';
import { CustomTable, useTable } from './CustomTable';
import { GuideWindow } from './GuidePicker';

// The Lineup's third of a set: laid out the same way, a panel with a toolbar over a table.
// A row here is one channel whose guide is worth changing, next to what it is on now.

const WHY = {
  none: { label: 'On no guide', color: 'orange' },
  empty: { label: 'Holds nothing', color: 'red' },
  better: { label: 'Better match', color: 'blue' },
};

// Holding nothing means two things and the difference matters: Dispatcharr reads a guide's
// programmes when it goes on a channel and not before, so a guide nobody uses holds nothing
// whatever it is really like. Calling that empty would be telling someone a good guide is
// no good.
const holds = (count, inUse) => {
  if (count) return `${count} programme${count === 1 ? '' : 's'}`;
  return inUse === false ? 'not read yet' : 'holds nothing';
};

// Plays the channel itself in the preview player, so a suggestion can be checked against
// what is actually on the screen: a guide can have the right name and the channel behind it
// be something else entirely.
const WatchChannel = ({ channel }) => {
  const showVideo = useVideoStore((s) => s.showVideo);
  const envMode = useSettingsStore((s) => s.environment?.env_mode);
  if (!channel.uuid) return null;
  return (
    <Tooltip label="Watch this channel">
      <ActionIcon
        size="sm"
        variant="subtle"
        color="blue"
        aria-label={`Watch ${channel.channel_name}`}
        onClick={(event) => {
          event.stopPropagation();
          const path = buildLiveStreamUrl(`/proxy/ts/stream/${channel.uuid}`);
          let url = `${window.location.protocol}//${window.location.host}${path}`;
          if (envMode === 'dev') {
            url = `${window.location.protocol}//${window.location.hostname}:5656${path}`;
          }
          showVideo(url, 'live', { name: channel.channel_name, channelId: channel.channel });
        }}
      >
        <CirclePlay size={16} />
      </ActionIcon>
    </Tooltip>
  );
};

const GuideManagerTable = () => {
  const [page, setPage] = useState(null);
  const [levers, setLevers] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [group, setGroup] = useState('');
  const [why, setWhy] = useState('');
  const [ticked, setTicked] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [readState, setReadState] = useState({});
  // Guides chosen by hand, by channel: {channel id: guide}. The suggestion is a
  // suggestion, and the whole point of seeing what each one holds is to be able to
  // disagree with it.
  const [chosen, setChosen] = useState({});
  // The row whose guide is being chosen, if any
  const [choosing, setChoosing] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  const [confirming, setConfirming] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const tableRef = useRef(null);

  const look = useCallback(async (quietly) => {
    if (!quietly) setLoading(true);
    try {
      const data = await API.getGuideManager();
      setPage(data);
      setLevers((now) => now ?? data.settings);
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Could not load the guides.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    look();
  }, [look]);

  // While a run is going the page follows it, and stops asking once it is over
  const running = page?.run?.running;
  useEffect(() => {
    if (!running) return undefined;
    const asking = setInterval(() => look(true), 3000);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(asking);
      clearInterval(clock);
    };
  }, [running, look]);

  const groups = useMemo(() => {
    const names = new Map();
    for (const one of page?.suggestions || []) {
      if (one.group_id == null) continue;
      if (!names.has(String(one.group_id))) {
        names.set(String(one.group_id), one.group || String(one.group_id));
      }
    }
    return [...names.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [page]);

  const rows = useMemo(() => {
    let found = (page?.suggestions || []).map((one) => {
      // "No guide" is a choice of its own and comes back as null, so what counts is
      // whether a choice was made for this channel, not whether it has a value
      const mine = Object.prototype.hasOwnProperty.call(chosen, one.channel)
        ? chosen[one.channel]
        : undefined;
      return {
        ...one,
        ...(mine === undefined
          ? {}
          : {
              epg: mine ? mine.id : null,
              name: mine ? mine.name : 'No guide',
              tvg_id: mine?.tvg_id || '',
              source: mine?.source || '',
              programmes: mine?.programmes || 0,
              now: mine?.now || '',
              in_use: mine?.in_use,
              by_hand: true,
            }),
        id: String(one.channel),
      };
    });
    if (group) found = found.filter((one) => String(one.group_id ?? '') === group);
    if (why) found = found.filter((one) => one.why === why);
    const wanted = search.trim().toLowerCase();
    if (wanted) {
      found = found.filter((one) =>
        [one.channel_name, one.name, one.tvg_id, one.source]
          .filter(Boolean)
          .some((text) => text.toLowerCase().includes(wanted))
      );
    }
    return found.sort((a, b) => a.channel_name.localeCompare(b.channel_name));
  }, [page, group, why, search, chosen]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const answer = await API.runGuideManager('start', levers);
      if (!answer.started) setError(answer.why || 'Could not start looking.');
      await look(true);
    } catch (e) {
      setError(e?.body?.error || 'Could not start looking.');
    } finally {
      setBusy(false);
    }
  };

  const halt = async () => {
    try {
      await API.runGuideManager('stop');
      await look(true);
    } catch {
      setError('Could not stop the run.');
    }
  };

  const apply = async () => {
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      const choices = Object.fromEntries(
        rows.filter((one) => ticked.has(one.id)).map((one) => [one.channel, one.epg])
      );
      await API.applyGuideManager(choices);
      setTicked(new Set());
      setChosen({});
      tableRef.current?.setSelectedTableIds?.([]);
      await look(true);
    } catch (e) {
      setError(e?.body?.error || 'Could not put those guides on.');
    } finally {
      setBusy(false);
    }
  };

  // The guides suggested that nobody has read. Reading one costs a pass of its source's
  // file either way, so they go together -- the same reader the Lineup's guide window uses.
  const unread = useMemo(
    () => [...new Set(rows.filter((one) => !one.programmes && !one.in_use).map((one) => one.epg))],
    [rows]
  );

  const readThem = async () => {
    if (!unread.length) return;
    setReading(true);
    setError(null);
    try {
      await API.loadChannelManagerGuide(unread);
      // Reading is a pass of each source's whole file, which on a big guide is minutes.
      // So it is followed by what the task says it is doing rather than given a number
      // of tries: as long as it is still reading, the page waits.
      for (let tries = 0; tries < 400; tries += 1) {
        await new Promise((done) => setTimeout(done, 3000));
        let state = {};
        try {
          state = (await API.getChannelManagerReading())?.reading || {};
          setReadState(state);
        } catch {
          // The next try asks again
        }
        const data = await API.getGuideManager();
        setPage(data);
        const still = (data.suggestions || []).filter(
          (one) => unread.includes(one.epg) && !one.programmes && !one.in_use
        );
        if (!still.length) break;
        if (!state.reading && tries > 2) break;
      }
    } catch (e) {
      setError(e?.body?.error || 'Could not read those guides.');
    } finally {
      setReading(false);
      setReadState({});
    }
  };

  const waveAway = useCallback(
    async (row) => {
      try {
        await API.ignoreGuideManager('ignore', {
          channel: row.channel,
          name: row.channel_name,
          epg: row.epg,
        });
        await look(true);
      } catch {
        setError('Could not wave that one away.');
      }
    },
    [look]
  );

  const saveLevers = async (changed) => {
    setLevers(changed);
    try {
      await API.saveGuideManagerSettings(changed);
    } catch (e) {
      setError(e?.body?.error || 'Those settings were not kept.');
    }
  };

  const waveAwayRef = useRef(waveAway);
  waveAwayRef.current = waveAway;
  const chooseRef = useRef(setChoosing);
  chooseRef.current = setChoosing;

  const columns = useMemo(
    () => [
      // The tick box, which the table draws itself once it is asked for by name
      { id: 'select', size: 50, enableSorting: false },
      {
        header: 'Channel',
        accessorKey: 'channel_name',
        grow: true,
        cell: ({ row }) => (
          <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
            <WatchChannel channel={row.original} />
            <Box style={{ minWidth: 0 }}>
              <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                {row.original.channel_name}
              </Text>
              <Text size="xs" c="dimmed" lineClamp={1}>
                {row.original.number ? `${row.original.number} · ` : ''}
                {row.original.group || 'No group'}
              </Text>
            </Box>
          </Group>
        ),
      },
      {
        header: 'On now',
        accessorKey: 'instead_of',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const one = row.original;
          return (
            <Box style={{ minWidth: 0 }}>
              <Text size="sm" c={one.instead_of ? undefined : 'orange'}>
                {one.instead_of || 'No guide'}
              </Text>
              {one.instead_of && (
                <>
                  <Text
                    size="xs"
                    c={one.instead_of_holds ? 'dimmed' : 'orange'}
                    lineClamp={1}
                  >
                    {one.instead_of_source ? `${one.instead_of_source} · ` : ''}
                    {holds(one.instead_of_holds, true)}
                    {one.instead_of_score != null && ` · ${one.instead_of_score}%`}
                  </Text>
                  <Text size="xs" c="dimmed" lineClamp={1}>
                    {one.instead_of_now ? `Now: ${one.instead_of_now}` : 'Nothing on it now'}
                  </Text>
                </>
              )}
            </Box>
          );
        },
      },
      {
        header: 'Suggested',
        accessorKey: 'name',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const one = row.original;
          return (
            <Box style={{ minWidth: 0 }}>
              <Group gap={6} wrap="wrap">
                <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                  {one.name}
                </Text>
                <Badge size="xs" variant="light" color="gray">
                  {one.source || 'no source'}
                </Badge>
                <Text size="xs" c="dimmed">
                  {one.by_hand ? 'chosen' : `${one.score}%`}
                </Text>
                {/* A suggestion is a suggestion: the same window the Lineup uses, so
                    another guide can be searched for and looked at before it is taken */}
                <Button
                  size="compact-xs"
                  variant="subtle"
                  aria-label={`Change the guide for ${one.channel_name}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    chooseRef.current(one);
                  }}
                >
                  Change
                </Button>
              </Group>
              <Text
                size="xs"
                c={!one.programmes && one.in_use ? 'orange' : 'dimmed'}
                lineClamp={1}
              >
                {one.tvg_id || 'no tvg-id'} · {holds(one.programmes, one.in_use)}
                {one.now ? ` · Now: ${one.now}` : ''}
              </Text>
            </Box>
          );
        },
      },
      {
        header: 'Why',
        accessorKey: 'why',
        size: 150,
        cell: ({ row }) => {
          const one = row.original;
          const kind = WHY[one.why] || { label: one.why, color: 'gray' };
          return (
            <Group gap={6} wrap="nowrap">
              <Badge size="sm" variant="light" color={kind.color}>
                {kind.label}
              </Badge>
              <Tooltip label="Don't suggest this again">
                <ActionIcon
                  size="xs"
                  variant="subtle"
                  color="gray"
                  aria-label={`Ignore ${one.channel_name}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    waveAwayRef.current(one);
                  }}
                >
                  <EyeOff size={12} />
                </ActionIcon>
              </Tooltip>
            </Group>
          );
        },
      },
    ],
    []
  );

  const table = useTable({
    columns,
    data: rows,
    allRowIds: rows.map((one) => one.id),
    enablePagination: false,
    enableRowSelection: true,
    enableRowVirtualization: false,
    renderTopToolbar: false,
    manualSorting: false,
    manualFiltering: false,
    manualPagination: true,
    onRowSelectionChange: (selected) => setTicked(new Set(selected)),
  });

  useEffect(() => {
    tableRef.current = table;
  }, [table]);

  const run = page?.run || {};
  const done = run.total ? Math.round((run.done / run.total) * 100) : 0;
  // How long it has been going, so a slow run reads as slow rather than as stuck
  const elapsed = useMemo(() => {
    if (!run.since) return '';
    const seconds = Math.max(0, Math.round((now - new Date(run.since).getTime()) / 1000));
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
  }, [run.since, now]);

  return (
    <>
      <Box style={{ display: 'flex', justifyContent: 'center' }}>
        <Paper
          style={{ width: '100%', maxWidth: 1200 }}
          p="sm"
          mx={{ base: 'xs', md: 0 }}
        >
          <LoadingOverlay visible={loading} />
          {error && (
            <Alert color="red" mb="sm" onClose={() => setError(null)} withCloseButton>
              {error}
            </Alert>
          )}

          <Group justify="space-between" mb="sm" wrap="wrap" gap="sm">
            <Group gap="sm" wrap="wrap">
              <TextInput
                size="xs"
                placeholder="Filter by name..."
                aria-label="Search suggestions"
                value={search}
                onChange={(event) => setSearch(event.currentTarget.value)}
                style={{ width: 190 }}
              />
              <Select
                size="xs"
                aria-label="Which group"
                placeholder="Every group"
                value={group}
                onChange={(value) => setGroup(value || '')}
                data={groups}
                searchable
                clearable
                style={{ width: 190 }}
              />
              <Select
                size="xs"
                aria-label="Why"
                placeholder="Any reason"
                value={why}
                onChange={(value) => setWhy(value || '')}
                data={[
                  { value: 'none', label: 'On no guide' },
                  { value: 'empty', label: 'Guide holds nothing' },
                  { value: 'better', label: 'A better match' },
                ]}
                clearable
                style={{ width: 190 }}
              />
            </Group>
            <Group gap="sm" wrap="wrap">
              <Button
                size="xs"
                variant="default"
                onClick={() => setShowSettings(!showSettings)}
              >
                Settings
              </Button>
              {run.running ? (
                <Button
                  size="xs"
                  color="red"
                  variant="light"
                  leftSection={<Square size={14} />}
                  onClick={halt}
                >
                  Stop
                </Button>
              ) : (
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<Play size={14} />}
                  onClick={start}
                  disabled={busy}
                >
                  Look for guides
                </Button>
              )}
              {reading && (
            <Box mb="sm">
              <Group justify="space-between" gap="xs" mb={4} wrap="wrap">
                <Text size="xs" c="dimmed">
                  Reading guides · {readState.stage || 'asking for them'}
                  {readState.at ? ` · ${readState.at}` : ''}
                </Text>
                <Text size="xs" c="dimmed">
                  {readState.done || 0} of {readState.total || unread.length}
                </Text>
              </Group>
              <Progress
                value={
                  readState.total
                    ? Math.round((readState.done / readState.total) * 100)
                    : 0
                }
                animated
                striped
                size="sm"
              />
            </Box>
          )}
          {unread.length > 0 && !reading && (
                <Button
                  size="xs"
                  variant="default"
                  onClick={readThem}
                  loading={reading}
                  aria-label="Read the programmes of every suggested guide not read yet"
                >
                  Read {unread.length} guide{unread.length === 1 ? '' : 's'}
                </Button>
              )}
              <Button
                size="xs"
                disabled={!ticked.size || busy}
                onClick={() => setConfirming(true)}
              >
                Apply ({ticked.size})
              </Button>
            </Group>
          </Group>

          {run.running && (
            <Box mb="sm">
              <Group justify="space-between" gap="xs" mb={4} wrap="wrap">
                <Text size="xs" c="dimmed">
                  {run.stage || 'looking at your channels'}
                  {run.at ? ` · ${run.at}` : ''}
                </Text>
                <Text size="xs" c="dimmed">
                  {run.done || 0} of {run.total || 0} · {run.found || 0} worth changing
                  {elapsed ? ` · ${elapsed}` : ''}
                </Text>
              </Group>
              {/* Striped while the guides are being read, since nothing can move yet */}
              <Progress
                value={run.stage && run.stage.startsWith('reading') ? 100 : done}
                animated={!!(run.stage && !run.stage.startsWith('looking'))}
                striped={!!(run.stage && !run.stage.startsWith('looking'))}
                size="sm"
              />
              <Text size="xs" c="dimmed" mt={4}>
                Every channel is scored against every guide there is, so this takes a
                while with a lot of EPG. It runs a batch at a time, so playlist and guide
                refreshes are not held up behind it, and it can be stopped.
              </Text>
            </Box>
          )}
          {reading && (
            <Box mb="sm">
              <Group justify="space-between" gap="xs" mb={4} wrap="wrap">
                <Text size="xs" c="dimmed">
                  Reading guides · {readState.stage || 'asking for them'}
                  {readState.at ? ` · ${readState.at}` : ''}
                </Text>
                <Text size="xs" c="dimmed">
                  {readState.done || 0} of {readState.total || unread.length}
                </Text>
              </Group>
              <Progress
                value={
                  readState.total
                    ? Math.round((readState.done / readState.total) * 100)
                    : 0
                }
                animated
                striped
                size="sm"
              />
            </Box>
          )}
          {unread.length > 0 && !reading && (
            <Text size="xs" c="dimmed" mb="sm">
              {unread.length} suggested guide{unread.length === 1 ? '' : 's'} say
              &quot;not read yet&quot;: Dispatcharr reads a guide&apos;s programmes when it
              goes on a channel, so one nothing uses holds nothing whatever it is really
              like. Reading one costs a pass of its whole guide file, so reading them
              together costs no more than reading one.
            </Text>
          )}
          {!run.running && run.state === 'done' && (
            <Text size="xs" c="dimmed" mb="sm">
              Looked at {run.total || 0} channel{run.total === 1 ? '' : 's'}.
            </Text>
          )}

          {showSettings && levers && (
            <Paper p="sm" mb="sm" withBorder>
              <Stack gap="sm">
                <Group gap="lg" wrap="wrap">
                  <Switch
                    size="xs"
                    label="Channels on no guide"
                    checked={!!levers.suggest_none}
                    onChange={(event) =>
                      saveLevers({ ...levers, suggest_none: event.currentTarget.checked })
                    }
                  />
                  <Switch
                    size="xs"
                    label="Guides that hold nothing"
                    checked={!!levers.suggest_empty}
                    onChange={(event) =>
                      saveLevers({ ...levers, suggest_empty: event.currentTarget.checked })
                    }
                  />
                  <Switch
                    size="xs"
                    label="A better match than the one it is on"
                    checked={!!levers.suggest_better}
                    onChange={(event) =>
                      saveLevers({ ...levers, suggest_better: event.currentTarget.checked })
                    }
                  />
                </Group>
                <Group gap="lg" wrap="wrap" align="flex-end">
                  <NumberInput
                    size="xs"
                    label="Good enough to suggest"
                    description="Out of a hundred"
                    min={0}
                    max={100}
                    value={levers.min_score}
                    onChange={(value) => saveLevers({ ...levers, min_score: value })}
                    style={{ width: 170 }}
                  />
                  <NumberInput
                    size="xs"
                    label="Better by at least"
                    description="Before a guide that works is replaced"
                    min={1}
                    max={100}
                    value={levers.better_by}
                    onChange={(value) => saveLevers({ ...levers, better_by: value })}
                    style={{ width: 210 }}
                  />
                  <Select
                    size="xs"
                    label="Only these groups"
                    placeholder="Every channel"
                    data={(page?.channel_groups || []).map((one) => ({
                      value: String(one.id),
                      label: `${one.name} (${one.count})`,
                    }))}
                    value={String(levers.channel_groups?.[0] ?? '')}
                    onChange={(value) =>
                      saveLevers({ ...levers, channel_groups: value ? [Number(value)] : [] })
                    }
                    searchable
                    clearable
                    style={{ width: 240 }}
                  />
                </Group>
                {(page?.ignored || []).length > 0 && (
                  <Group gap="sm">
                    <Text size="xs" c="dimmed">
                      {page.ignored.length} suggestion
                      {page.ignored.length === 1 ? '' : 's'} waved away.
                    </Text>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      leftSection={<RotateCcw size={12} />}
                      onClick={async () => {
                        await API.ignoreGuideManager('clear');
                        look(true);
                      }}
                    >
                      Suggest them again
                    </Button>
                  </Group>
                )}
              </Stack>
            </Paper>
          )}

          {rows.length === 0 && !loading ? (
            <Center p="xl">
              <Text size="sm" c="dimmed" ta="center">
                {page?.suggestions?.length
                  ? 'Nothing matches what is being looked at.'
                  : 'Nothing to change. Press "Look for guides" to go through every channel: the ones on no guide, the ones whose guide holds no programmes, and the ones something matches better.'}
              </Text>
            </Center>
          ) : (
            <CustomTable table={table} />
          )}
        </Paper>
      </Box>

      {choosing && (
        <GuideWindow
          channel={{
            name: choosing.channel_name,
            epg: {
              id: choosing.epg,
              name: choosing.name,
              source: choosing.source,
              tvg_id: choosing.tvg_id,
              programmes: choosing.programmes,
              now: choosing.now,
              in_use: choosing.in_use,
            },
          }}
          onClose={() => setChoosing(null)}
          onChoose={(picked) => {
            setChosen((all) => ({ ...all, [choosing.channel]: picked }));
            // Choosing one is something to apply, and would otherwise be easy to lose
            setTicked((all) => {
              const now = new Set(all).add(String(choosing.channel));
              tableRef.current?.setSelectedTableIds?.([...now]);
              return now;
            });
            setChoosing(null);
          }}
        />
      )}

      <ConfirmationDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={apply}
        title={`Put ${ticked.size} guide${ticked.size === 1 ? '' : 's'} on?`}
        message="Each ticked channel goes on the guide suggested for it, and its programmes are read straight away. Channels not ticked are left alone."
        confirmLabel="Apply"
        actionKey="apply-guides"
      />
    </>
  );
};

export default GuideManagerTable;
