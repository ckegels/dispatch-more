import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Code,
  Group,
  Loader,
  MultiSelect,
  SegmentedControl,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import { Download, FileArchive, RefreshCw } from 'lucide-react';
import API from '../../api';

// Diagnostics -> Logs: every log Dispatcharr writes, wherever it writes it -- the systemd
// journal on Linux or an LXC, the log collector's files in Docker -- narrowed to what is being
// looked for, and downloadable whole.

const SINCE = [
  { value: '15m', label: 'Last 15 minutes' },
  { value: '1h', label: 'Last hour' },
  { value: '6h', label: 'Last 6 hours' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: 'all', label: 'Everything' },
];

// What the parts are is the server's to say (core.log_center.TOPICS), so a part added
// there turns up here without being written down twice. This is only what to show before
// it has answered.
const EVERY_PART = { value: '', label: 'Every part' };

const LEVEL_COLOR = {
  CRITICAL: 'red',
  ERROR: 'red',
  WARNING: 'yellow',
  INFO: 'blue',
  DEBUG: 'gray',
};
const FOLLOW_MS = 5000;

// The logger a line came from, as short as it can be said without losing which it is:
// "apps.channels.stream_check" is channels.stream_check, and both of the ways a plugin
// can write are said the one way.
export const shortLogger = (name) =>
  String(name || '')
    .replace(/^_dispatcharr_plugin_/, 'plugins.')
    .replace(/\.plugin$/, '')
    .replace(/^apps\./, '');

const Record = ({ record }) => {
  const [open, setOpen] = useState(false);
  const [first, ...rest] = record.text.split('\n');
  return (
    <Box
      py={2}
      style={{
        borderBottom: '1px solid var(--mantine-color-dark-5)',
        fontFamily: 'monospace',
      }}
    >
      <Group gap={6} wrap="nowrap" align="flex-start">
        {record.level ? (
          <Badge
            size="xs"
            variant="light"
            color={LEVEL_COLOR[record.level] || 'gray'}
            style={{ flexShrink: 0 }}
          >
            {record.level}
          </Badge>
        ) : (
          <Box w={52} style={{ flexShrink: 0 }} />
        )}
        <Text
          size="xs"
          style={{ wordBreak: 'break-word', whiteSpace: 'pre-wrap', flex: 1 }}
        >
          {record.time && (
            <Text span size="xs" c="dimmed">
              {record.time.replace('T', ' ').slice(0, 19)}{' '}
            </Text>
          )}
          {record.service && (
            <Text span size="xs" c="dimmed">
              {record.service}:{' '}
            </Text>
          )}
          {/* What wrote it, which is how a line is found by the part it belongs to */}
          {record.logger && (
            <Text span size="xs" c="blue.3" title={record.logger}>
              {shortLogger(record.logger)}{' '}
            </Text>
          )}
          {first}
          {rest.length > 0 && (
            <Text
              span
              size="xs"
              c="blue.4"
              style={{ cursor: 'pointer' }}
              onClick={() => setOpen(!open)}
            >
              {' '}
              {open ? '[hide]' : `[+${rest.length} lines]`}
            </Text>
          )}
        </Text>
      </Group>
      {open && (
        <Code
          block
          mt={4}
          style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
        >
          {rest.join('\n')}
        </Code>
      )}
    </Box>
  );
};

const LogViewer = () => {
  const [meta, setMeta] = useState(null);
  const [chosen, setChosen] = useState([]);
  const [since, setSince] = useState('1h');
  const [level, setLevel] = useState('ALL');
  const [topic, setTopic] = useState('');
  // One plugin's lines. Every plugin used to log under the loader's name, so a line said
  // a plugin had written it and never which one.
  const [plugin, setPlugin] = useState('');
  const [text, setText] = useState('');
  const [searched, setSearched] = useState('');
  const [follow, setFollow] = useState(false);
  const [found, setFound] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const bottom = useRef(null);

  const query = useMemo(
    () => ({ sources: chosen.join(','), since, level, topic, text: searched, plugin }),
    [chosen, since, level, topic, searched, plugin]
  );

  const load = useCallback(
    async (quiet = false) => {
      if (!quiet) setBusy(true);
      try {
        setFound(await API.readLogs(query));
        setError(null);
      } catch (e) {
        setError(e?.body?.detail || 'Could not read the logs.');
      } finally {
        if (!quiet) setBusy(false);
      }
    },
    [query]
  );

  useEffect(() => {
    API.getLogSources()
      .then(setMeta)
      .catch(() => setError('Could not find the logs.'));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!follow) return undefined;
    const timer = setInterval(() => load(true), FOLLOW_MS);
    return () => clearInterval(timer);
  }, [follow, load]);

  // Following, the newest is kept in view
  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView?.({ block: 'end' });
  }, [found, follow]);

  const stamp = () =>
    new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '');
  const sources = meta?.sources || [];
  const parts = [EVERY_PART, ...(meta?.topics || [])];
  const installed = meta?.plugins || [];

  return (
    <Stack gap="sm">
      {meta && sources.length === 0 && (
        <Alert color="yellow">
          No logs found here: no dispatcharr services in systemd, and no files
          from the log collector.
        </Alert>
      )}
      {meta &&
        !meta.journal_readable &&
        sources.some((s) => s.kind === 'journal') && (
          <Alert color="yellow">
            Dispatcharr is not allowed to read the system journal, so the logs
            below may be empty. On the server:{' '}
            <Code>
              sudo usermod -aG systemd-journal &lt;the user Dispatcharr runs
              as&gt;
            </Code>{' '}
            and restart Dispatcharr. The Dispatch More installer does this
            itself.
          </Alert>
        )}

      <Group gap="xs" wrap="wrap" align="flex-end">
        <MultiSelect
          size="xs"
          label="Logs"
          placeholder={sources.length ? 'All of them' : ''}
          data={sources.map((s) => ({ value: s.id, label: s.label }))}
          value={chosen}
          onChange={setChosen}
          clearable
          style={{ minWidth: 220, flex: '1 1 260px' }}
        />
        <Select
          size="xs"
          label="When"
          data={SINCE}
          value={since}
          onChange={(value) => value && setSince(value)}
          allowDeselect={false}
          w={150}
        />
        <Select
          size="xs"
          label="Part of Dispatcharr"
          data={parts}
          value={topic}
          onChange={(value) => setTopic(value ?? '')}
          allowDeselect={false}
          searchable
          w={260}
        />
        {/* One plugin's own lines. Only offered where there are plugins to offer. */}
        {installed.length > 0 && (
          <Select
            size="xs"
            label="Plugin"
            placeholder="Any plugin"
            data={installed.map((one) => ({ value: one.key, label: one.name }))}
            value={plugin}
            onChange={(value) => setPlugin(value ?? '')}
            searchable
            clearable
            w={200}
          />
        )}
      </Group>

      <Group gap="xs" wrap="wrap" align="flex-end">
        <SegmentedControl
          size="xs"
          value={level}
          onChange={setLevel}
          aria-label="Level"
          data={[
            { value: 'ALL', label: 'Everything' },
            { value: 'INFO', label: 'Info and up' },
            { value: 'WARNING', label: 'Warnings and up' },
            { value: 'ERROR', label: 'Errors' },
          ]}
        />
        <TextInput
          size="xs"
          placeholder="Search for…"
          aria-label="Search the logs"
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          onKeyDown={(e) => e.key === 'Enter' && setSearched(text)}
          onBlur={() => setSearched(text)}
          style={{ flex: '1 1 180px' }}
        />
        <Switch
          size="xs"
          label="Follow"
          checked={follow}
          onChange={(e) => setFollow(e.currentTarget.checked)}
        />
        <Button
          size="xs"
          variant="default"
          leftSection={<RefreshCw size={14} />}
          onClick={() => load()}
        >
          Refresh
        </Button>
        <Button
          size="xs"
          variant="light"
          leftSection={<Download size={14} />}
          onClick={() =>
            API.downloadLogs(
              'download',
              query,
              `dispatcharr-logs-${stamp()}.log`
            )
          }
        >
          Download all of it
        </Button>
        <Button
          size="xs"
          variant="light"
          color="grape"
          leftSection={<FileArchive size={14} />}
          onClick={() =>
            API.downloadLogs(
              'bundle',
              {},
              `dispatcharr-diagnostics-${stamp()}.zip`
            )
          }
        >
          Diagnostics bundle
        </Button>
      </Group>

      {error && <Alert color="red">{error}</Alert>}
      <Text size="xs" c="dimmed">
        {found
          ? `${found.total.toLocaleString()} record${found.total === 1 ? '' : 's'}${
              found.cut
                ? ` — the newest ${found.records.length.toLocaleString()} shown; Download has all of them`
                : ''
            }. An error's traceback is kept with it: open it with [+ lines].`
          : ''}
      </Text>

      <Box
        style={{
          maxHeight: '65vh',
          overflow: 'auto',
          border: '1px solid var(--mantine-color-dark-4)',
          borderRadius: 'var(--mantine-radius-sm)',
          padding: '4px 8px',
        }}
      >
        {busy && !found ? (
          <Loader size="sm" />
        ) : (found?.records || []).length === 0 ? (
          <Text size="xs" c="dimmed" p="sm">
            Nothing matches.
          </Text>
        ) : (
          found.records.map((record, index) => (
            <Record key={index} record={record} />
          ))
        )}
        <div ref={bottom} />
      </Box>
    </Stack>
  );
};

export default LogViewer;
