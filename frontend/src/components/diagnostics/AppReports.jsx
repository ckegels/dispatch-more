import React, { useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, Copy, Trash2 } from 'lucide-react';
import {
  ActionIcon,
  Alert,
  Box,
  Button,
  Group,
  Loader,
  Stack,
  Text,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import API from '../../api';

// Error reports sent from a player app (arrTV): what the app saw, and what the server knew
// about the channel at that moment (see app_reports.py). Read here, copied whole to pass on.

const when = (seconds) =>
  seconds ? new Date(seconds * 1000).toLocaleString() : '';

// A report as text, to paste into an issue or a message: everything, the long parts last
const reportAsText = (report) =>
  [
    `Report ${report.id} · ${when(report.received_at)}`,
    `From: ${report.user || 'nobody'} · ${report.device_name || report.device_id || report.address}`,
    `What happened: ${report.what || '(nothing written)'}`,
    `Happened at: ${report.happened_at || '?'}`,
    '',
    'App:',
    JSON.stringify(report.app || {}, null, 2),
    '',
    'Player:',
    JSON.stringify(report.player || {}, null, 2),
    '',
    'Network:',
    JSON.stringify(report.network || {}, null, 2),
    '',
    'Server:',
    JSON.stringify(report.server || {}, null, 2),
    '',
    "App's log:",
    report.log || '(none)',
  ].join('\n');

const Section = ({ title, value }) => (
  <Box>
    <Text size="xs" fw={700} tt="uppercase" c="dimmed">
      {title}
    </Text>
    <Box
      component="pre"
      style={{
        fontSize: 11,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        margin: 0,
        maxHeight: 320,
        overflow: 'auto',
      }}
    >
      {typeof value === 'string' ? value : JSON.stringify(value, null, 2)}
    </Box>
  </Box>
);

const Report = ({ summary, onCopy, onDelete }) => {
  const [open, setOpen] = useState(false);
  const [whole, setWhole] = useState(null);

  useEffect(() => {
    if (!open || whole) return;
    API.getAppReport(summary.id).then(setWhole).catch(() => setWhole({ error: true }));
  }, [open, whole, summary.id]);

  return (
    <Box style={{ border: '1px solid #3f3f46', borderRadius: 'var(--mantine-radius-sm)' }}>
      <Group gap="xs" wrap="nowrap" p="xs">
        <UnstyledButton
          onClick={() => setOpen(!open)}
          aria-label={`${open ? 'Close' : 'Open'} report ${summary.id}`}
          style={{ display: 'flex', gap: 8, flex: 1, minWidth: 0, alignItems: 'flex-start' }}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <Box style={{ minWidth: 0 }}>
            <Text size="sm" style={{ wordBreak: 'break-word' }}>
              {summary.channel || 'No channel'} — {summary.what || '(nothing written)'}
            </Text>
            <Text size="xs" c="dimmed">
              {when(summary.received_at)} · {summary.user || 'nobody'}
              {summary.device_name ? ` · ${summary.device_name}` : ''}
              {summary.error ? ` · ${summary.error}` : ''}
            </Text>
          </Box>
        </UnstyledButton>
        <Tooltip label="Copy the whole report">
          <ActionIcon
            size="sm"
            variant="subtle"
            aria-label={`Copy report ${summary.id}`}
            onClick={async () => {
              const report = whole && !whole.error ? whole : await API.getAppReport(summary.id);
              onCopy(reportAsText(report));
            }}
          >
            <Copy size={14} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Delete this report">
          <ActionIcon
            size="sm"
            variant="subtle"
            color="red"
            aria-label={`Delete report ${summary.id}`}
            onClick={() => onDelete(summary.id)}
          >
            <Trash2 size={14} />
          </ActionIcon>
        </Tooltip>
      </Group>
      {open && (
        <Stack gap="sm" p="sm" pt={0}>
          {!whole && <Loader size="xs" />}
          {whole?.error && <Text size="xs" c="red">Could not read this report.</Text>}
          {whole && !whole.error && (
            <>
              <Section title="The app" value={whole.app} />
              <Section title="The player" value={whole.player} />
              {whole.network && Object.keys(whole.network).length > 0 && (
                <Section title="The network" value={whole.network} />
              )}
              <Section title="The channel, as the server has it" value={whole.server?.channel || {}} />
              <Section
                title="What the server saw of it"
                value={{
                  running: whole.server?.running,
                  events: whole.server?.events,
                  stopped: whole.server?.stopped,
                  starts: whole.server?.starts,
                  switches: whole.server?.switches,
                }}
              />
              <Section title="The server's log about it" value={(whole.server?.log || []).join('\n') || '(nothing)'} />
              <Section title="The app's log" value={whole.log || '(none)'} />
            </>
          )}
        </Stack>
      )}
    </Box>
  );
};

const AppReports = ({ enabled, onCopy }) => {
  const [reports, setReports] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      setReports((await API.getAppReports()).reports || []);
      setError(null);
    } catch {
      setError('Could not read the reports.');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const remove = async (id) => {
    await API.deleteAppReport(id);
    load();
  };

  if (error) return <Alert color="red">{error}</Alert>;
  if (!reports) return <Loader size="sm" />;
  return (
    <Stack gap="xs">
      {!enabled && (
        <Alert color="gray" p="xs">
          <Text size="xs">
            Apps cannot send reports while &quot;Take error reports from apps&quot; is off
            (Channel switches tab).
          </Text>
        </Alert>
      )}
      {reports.length === 0 ? (
        <Text size="sm" c="dimmed">
          No reports. In arrTV, someone who has a problem with a channel sends one from the
          player&apos;s settings; it arrives here with what the server knew at that moment.
        </Text>
      ) : (
        <>
          {reports.map((summary) => (
            <Report key={summary.id} summary={summary} onCopy={onCopy} onDelete={remove} />
          ))}
          <Group>
            <Button size="xs" variant="subtle" color="red" onClick={() => remove(null)}>
              Delete all reports
            </Button>
          </Group>
        </>
      )}
    </Stack>
  );
};

export default AppReports;
