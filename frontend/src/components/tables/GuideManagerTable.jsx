import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { EyeOff, Play, RotateCcw, Square } from 'lucide-react';
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
import ConfirmationDialog from '../ConfirmationDialog';
import { CustomTable, useTable } from './CustomTable';

// The Lineup's third of a set: laid out the same way, a panel with a toolbar over a table.
// A row here is one channel whose guide is worth changing, next to what it is on now.

const WHY = {
  none: { label: 'On no guide', color: 'orange' },
  empty: { label: 'Holds nothing', color: 'red' },
  better: { label: 'Better match', color: 'blue' },
};

const holds = (count) =>
  count ? `${count} programme${count === 1 ? '' : 's'}` : 'holds nothing';

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
    const timer = setInterval(() => look(true), 3000);
    return () => clearInterval(timer);
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
    let found = (page?.suggestions || []).map((one) => ({ ...one, id: String(one.channel) }));
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
  }, [page, group, why, search]);

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
      tableRef.current?.setSelectedTableIds?.([]);
      await look(true);
    } catch (e) {
      setError(e?.body?.error || 'Could not put those guides on.');
    } finally {
      setBusy(false);
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

  const columns = useMemo(
    () => [
      // The tick box, which the table draws itself once it is asked for by name
      { id: 'select', size: 50, enableSorting: false },
      {
        header: 'Channel',
        accessorKey: 'channel_name',
        grow: true,
        cell: ({ row }) => (
          <Box style={{ minWidth: 0 }}>
            <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
              {row.original.channel_name}
            </Text>
            <Text size="xs" c="dimmed" lineClamp={1}>
              {row.original.group || 'No group'}
            </Text>
          </Box>
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
                <Text
                  size="xs"
                  c={one.instead_of_holds ? 'dimmed' : 'orange'}
                  lineClamp={1}
                >
                  {holds(one.instead_of_holds)}
                  {one.instead_of_score != null && ` · ${one.instead_of_score}%`}
                </Text>
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
                  {one.score}%
                </Text>
              </Group>
              <Text size="xs" c="dimmed" lineClamp={1}>
                {one.tvg_id || 'no tvg-id'} · {holds(one.programmes)}
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
              <Text size="xs" c="dimmed" mb={4}>
                Looking at every channel: {run.done || 0} of {run.total || 0},{' '}
                {run.found || 0} worth changing so far. It runs a batch at a time so
                playlist and guide refreshes are not held up behind it.
              </Text>
              <Progress value={done} size="sm" />
            </Box>
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
