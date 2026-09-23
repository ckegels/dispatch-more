import React, { useCallback, useEffect, useState } from 'react';
import { Check, FolderSearch, Play, Plus, Square, Trash2 } from 'lucide-react';
import {
  Alert,
  Autocomplete,
  Badge,
  Box,
  Button,
  Group,
  LoadingOverlay,
  NumberInput,
  Paper,
  Progress,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';

// The Channel Manager's sixth tab: iptv-org/epg, which is already installed on the box,
// run from here instead of by hand. Nothing about the grabber changes -- this says where
// it is, what to grab, and when, and takes the file it writes the rest of the way.

const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

// One guide: a run of the grabber and the file it writes
const Guide = ({
  job,
  channelFiles,
  epgSources,
  onChange,
  onRemove,
  onGrab,
  onMakeSource,
  running,
  busy,
}) => {
  const last = job.last || {};
  const set = (changes) => onChange({ ...job, ...changes });
  // What is known about the list that was chosen or typed, where it is one we found
  const known = channelFiles.find((one) => one.path === job.channels);
  return (
    <Paper withBorder p="sm" radius="md">
      <Stack gap="xs">
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Group gap="xs" wrap="wrap">
            <Switch
              size="xs"
              aria-label={`Grab ${job.name} on the timer`}
              checked={!!job.enabled}
              onChange={(event) => set({ enabled: event.currentTarget.checked })}
            />
            <TextInput
              size="xs"
              aria-label="Name for this guide"
              placeholder="What to call it"
              value={job.name}
              onChange={(event) => set({ name: event.currentTarget.value })}
              style={{ width: 240 }}
            />
            {last.at && (
              <Badge size="sm" variant="light" color={last.ok ? 'teal' : 'orange'}>
                {last.ok
                  ? `${(last.programmes || 0).toLocaleString()} programmes for ${(
                      last.channels || 0
                    ).toLocaleString()} channels`
                  : 'last grab did not finish'}
              </Badge>
            )}
          </Group>
          <Group gap="xs">
            <Button
              size="compact-xs"
              variant="light"
              leftSection={<Play size={13} />}
              disabled={running || busy}
              onClick={() => onGrab(job)}
            >
              Grab this one
            </Button>
            <Button
              size="compact-xs"
              variant="subtle"
              color="red"
              aria-label={`Remove ${job.name}`}
              onClick={() => onRemove(job)}
            >
              <Trash2 size={13} />
            </Button>
          </Group>
        </Group>

        {last.at && (
          <Text size="xs" c={last.ok ? 'dimmed' : 'orange'}>
            {when(last.at)}
            {last.seconds ? ` · took ${Math.round(last.seconds / 60)} min` : ''}
            {last.why ? ` · ${last.why}` : ''}
            {last.read_by ? ` · read by ${last.read_by}` : ''}
          </Text>
        )}

        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="sm">
          {/* One of the two, never both: a channel list names exact channels and the
              site each is on; sites grabs whole sites. Typed as well as picked -- a list
              somebody made lives wherever they put it. */}
          <Autocomplete
            size="xs"
            label="Channel list"
            description={
              known
                ? `${known.channels.toLocaleString()} channels in that file`
                : 'One the grabber has, or the path to one you made'
            }
            placeholder="/opt/iptv-org-epg/data/pbs.channels.xml"
            data={channelFiles.map((one) => one.path)}
            value={job.channels}
            onChange={(value) => set({ channels: value || '' })}
            disabled={!!job.sites}
          />
          <TextInput
            size="xs"
            label="Or these sites"
            description="Whole sites of the grabber's own, comma separated"
            placeholder="tvpassport.com"
            value={job.sites}
            onChange={(event) => set({ sites: event.currentTarget.value })}
            disabled={!!job.channels}
          />
          <TextInput
            size="xs"
            label="Write the guide to"
            description="Dispatcharr reads it from here; nothing has to serve it"
            placeholder="/opt/iptv-org-epg/data/pbs.xmltv"
            value={job.output}
            onChange={(event) => set({ output: event.currentTarget.value })}
          />
          <Group gap="xs" align="flex-end" wrap="nowrap">
            <Select
              size="xs"
              label="Read by"
              description="Which EPG source takes it"
              placeholder="Nothing yet"
              data={epgSources.map((one) => ({
                value: String(one.id),
                label: one.name,
              }))}
              value={job.epg_source ? String(job.epg_source) : null}
              onChange={(value) => set({ epg_source: value ? Number(value) : null })}
              searchable
              clearable
              style={{ flex: 1 }}
            />
            <Button
              size="xs"
              variant="default"
              disabled={!job.output || !job.name || busy}
              onClick={() => onMakeSource(job)}
            >
              Make one
            </Button>
          </Group>
        </SimpleGrid>

        {/* The grabber's own options. Left empty is left to the grabber, which is the
            right answer for most of them: a site's own number of days is usually the
            number of days that site has. */}
        <SimpleGrid cols={{ base: 2, md: 6 }} spacing="xs">
          <NumberInput
            size="xs"
            label="Days"
            placeholder="site's own"
            min={0}
            max={31}
            value={job.days || ''}
            onChange={(value) => set({ days: Number(value) || 0 })}
          />
          <TextInput
            size="xs"
            label="Languages"
            placeholder="any"
            value={job.lang}
            onChange={(event) => set({ lang: event.currentTarget.value })}
          />
          <NumberInput
            size="xs"
            label="Timeout (ms)"
            placeholder="30000"
            min={0}
            value={job.timeout_ms || ''}
            onChange={(value) => set({ timeout_ms: Number(value) || 0 })}
          />
          <NumberInput
            size="xs"
            label="Delay (ms)"
            placeholder="0"
            min={0}
            value={job.delay_ms || ''}
            onChange={(value) => set({ delay_ms: Number(value) || 0 })}
          />
          <NumberInput
            size="xs"
            label="At once"
            description="maxConnections"
            placeholder="1"
            min={0}
            max={20}
            value={job.max_connections || ''}
            onChange={(value) => set({ max_connections: Number(value) || 0 })}
          />
          <Switch
            size="xs"
            mt={22}
            label="Also .gz"
            checked={!!job.gzip}
            onChange={(event) => set({ gzip: event.currentTarget.checked })}
          />
        </SimpleGrid>
      </Stack>
    </Paper>
  );
};

// Making a channel list out of a bigger one: the grep that was being done by hand, done
// where the rest of it is. What comes out is a document rather than a heap of lines,
// which is the mistake that answers "Text data outside of root node".
const MakeList = ({ channelFiles, folder, onMade }) => {
  const [from, setFrom] = useState('');
  const [keep, setKeep] = useState('');
  const [leaveOut, setLeaveOut] = useState('');
  const [into, setInto] = useState('');
  const [found, setFound] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const ask = async (apply) => {
    setBusy(true);
    setError(null);
    try {
      const answer = await API.makeEpgGrabberList({
        from,
        keep,
        leave_out: leaveOut,
        into: into || `${folder}/data/${(keep || 'list').split(',')[0].trim().toLowerCase()}.channels.xml`,
        apply,
      });
      setFound(answer);
      if (apply) onMade(answer.into);
    } catch (e) {
      setError(e?.body?.error || 'That list could not be made.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        Make a channel list
      </Text>
      <Text size="xs" c="dimmed">
        Out of one of the grabber&apos;s own: keep the channels that say a word. What
        comes out is a list of its own, which a guide above can then be pointed at.
      </Text>
      <Group gap="xs" align="flex-end" wrap="wrap">
        <Select
          size="xs"
          label="Out of"
          placeholder="One of the grabber's lists"
          data={channelFiles.map((one) => ({
            value: one.path,
            label: `${one.site}/${one.path.split('/').pop()}${
              one.channels ? ` (${one.channels.toLocaleString()})` : ''
            }`,
          }))}
          value={from || null}
          onChange={(value) => {
            setFrom(value || '');
            setFound(null);
          }}
          searchable
          style={{ flex: 1, minWidth: 260 }}
        />
        <TextInput
          size="xs"
          label="Keep the ones saying"
          description="Comma separated; case does not matter"
          placeholder="PBS"
          value={keep}
          onChange={(event) => {
            setKeep(event.currentTarget.value);
            setFound(null);
          }}
          style={{ width: 180 }}
        />
        <TextInput
          size="xs"
          label="...but not"
          description="Left empty keeps them all"
          placeholder="radio"
          value={leaveOut}
          onChange={(event) => {
            setLeaveOut(event.currentTarget.value);
            setFound(null);
          }}
          style={{ width: 150 }}
        />
        <Button
          size="xs"
          variant="default"
          disabled={!from || !keep.trim() || busy}
          onClick={() => ask(false)}
        >
          Show me
        </Button>
      </Group>

      {error && <Alert color="red">{error}</Alert>}

      {found && (
        <Alert color={found.written ? 'teal' : 'blue'} variant="light">
          <Stack gap={4}>
            <Text size="xs">
              {found.kept.toLocaleString()} of {found.of.toLocaleString()} channels
              {found.written ? ` written to ${found.into}` : ''}
            </Text>
            {found.sample.length > 0 && (
              <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>
                {found.sample.join(' · ')}
                {found.kept > found.sample.length ? ' …' : ''}
              </Text>
            )}
            {!found.written && found.kept > 0 && (
              <Group gap="xs" align="flex-end" mt={4}>
                <TextInput
                  size="xs"
                  label="Write it to"
                  placeholder={`${folder}/data/${(keep || 'list').split(',')[0].trim().toLowerCase()}.channels.xml`}
                  value={into}
                  onChange={(event) => setInto(event.currentTarget.value)}
                  style={{ flex: 1, minWidth: 260 }}
                />
                <Button size="xs" loading={busy} onClick={() => ask(true)}>
                  Make it
                </Button>
              </Group>
            )}
          </Stack>
        </Alert>
      )}
    </Stack>
  );
};

const EpgGrabberTable = () => {
  const [page, setPage] = useState(null);
  const [draft, setDraft] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(null);

  const load = useCallback(async (quietly) => {
    if (!quietly) setLoading(true);
    try {
      const data = await API.getEpgGrabber();
      setPage(data);
      // What is being edited is kept while a grab runs underneath, so a poll does not
      // take a half-typed path away
      setDraft((current) => current ?? data.settings);
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Could not read the grabber settings.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // While a grab is going, how far it has got
  const running = page?.running;
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => load(true), 3000);
    return () => clearInterval(timer);
  }, [running, load]);

  const save = async (settings) => {
    setBusy(true);
    setError(null);
    try {
      const answer = await API.saveEpgGrabberSettings(settings);
      setDraft(answer.settings);
      setNotice('Kept.');
      await load(true);
      return answer.settings;
    } catch (e) {
      setError(e?.body?.error || 'Those settings were not kept.');
      return null;
    } finally {
      setBusy(false);
    }
  };

  const grab = async (job) => {
    setError(null);
    setNotice(null);
    // Saved first: what is grabbed is what is on the page, not what was on it last time
    const kept = await save(draft);
    if (!kept) return;
    try {
      await API.runEpgGrabber(job ? job.id : null);
      setNotice(
        job
          ? `Grabbing ${job.name}. It says how far it has got as it goes.`
          : 'Grabbing. It says how far it has got as it goes.'
      );
      await load(true);
    } catch (e) {
      setError(e?.body?.error || 'Could not start it.');
    }
  };

  const stop = async () => {
    await API.stopEpgGrabber();
    await load(true);
  };

  const makeSource = async (job) => {
    setError(null);
    try {
      const made = await API.makeEpgGrabberSource(job.name, job.output);
      const changed = {
        ...draft,
        jobs: draft.jobs.map((one) =>
          one.id === job.id ? { ...one, epg_source: made.id } : one
        ),
      };
      setDraft(changed);
      await save(changed);
      setNotice(
        made.made
          ? `Made the EPG source "${made.name}". It reads the file this writes.`
          : `"${made.name}" now reads the file this writes.`
      );
      await load(true);
    } catch (e) {
      setError(e?.body?.error || 'That EPG source could not be made.');
    }
  };

  if (loading && !page) {
    return <LoadingOverlay visible />;
  }

  const install = page?.install || {};
  const said = page?.progress || {};
  const through = said.total ? Math.round((said.done / said.total) * 100) : 0;
  const setSettings = (changes) => setDraft({ ...draft, ...changes });

  return (
    <>
      <Box style={{ display: 'flex', justifyContent: 'center' }}>
        <Stack gap="md" style={{ maxWidth: 1200, width: '100%' }}>
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
                padding: 16,
                borderBottom: '1px solid #3f3f46',
              }}
            >
              <Group gap="sm">
                <Switch
                  size="xs"
                  label="Grab by itself"
                  checked={!!draft?.enabled}
                  onChange={(event) =>
                    setSettings({ enabled: event.currentTarget.checked })
                  }
                />
                <NumberInput
                  size="xs"
                  aria-label="Every (hours)"
                  label="Every (hours)"
                  min={1}
                  max={168}
                  value={draft?.every_hours ?? 12}
                  onChange={(value) => setSettings({ every_hours: Number(value) || 12 })}
                  style={{ width: 110 }}
                />
                <TextInput
                  size="xs"
                  label="Only from"
                  placeholder="23:00"
                  value={draft?.window_from || ''}
                  onChange={(event) =>
                    setSettings({ window_from: event.currentTarget.value.trim() })
                  }
                  style={{ width: 90 }}
                />
                <TextInput
                  size="xs"
                  label="Until"
                  placeholder="06:00"
                  value={draft?.window_to || ''}
                  onChange={(event) =>
                    setSettings({ window_to: event.currentTarget.value.trim() })
                  }
                  style={{ width: 90 }}
                />
                {/* The guard that does the work: a grabber getting on with it says a line
                    per channel per day, so silence is what means something is wrong */}
                <NumberInput
                  size="xs"
                  label="Stop after silence (min)"
                  min={1}
                  max={240}
                  value={draft?.silent_for_minutes ?? 20}
                  onChange={(value) =>
                    setSettings({ silent_for_minutes: Number(value) || 20 })
                  }
                  style={{ width: 130 }}
                />
                <NumberInput
                  size="xs"
                  label="...or after (min)"
                  description="However well it is going"
                  min={5}
                  max={2880}
                  value={draft?.give_up_after_minutes ?? 720}
                  onChange={(value) =>
                    setSettings({ give_up_after_minutes: Number(value) || 720 })
                  }
                  style={{ width: 140 }}
                />
              </Group>
              <Group gap="sm">
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<Check size={16} />}
                  loading={busy}
                  onClick={() => save(draft)}
                >
                  Save
                </Button>
                {page?.running ? (
                  <Button
                    size="xs"
                    color="red"
                    variant="light"
                    leftSection={<Square size={14} />}
                    onClick={stop}
                  >
                    Stop
                  </Button>
                ) : (
                  <Button
                    size="xs"
                    leftSection={<Play size={16} />}
                    disabled={!install.ok || !(draft?.jobs || []).length}
                    onClick={() => grab(null)}
                  >
                    Grab now
                  </Button>
                )}
              </Group>
            </Box>

            {/* Where the grabber is */}
            <Box style={{ padding: '8px 16px', borderBottom: '1px solid #3f3f46' }}>
              <Group gap="xs" align="flex-end" wrap="wrap">
                <TextInput
                  size="xs"
                  label="Where iptv-org/epg is"
                  description="The folder it was cloned into and npm install was run in"
                  placeholder="/opt/iptv-org-epg"
                  value={draft?.folder || ''}
                  onChange={(event) => setSettings({ folder: event.currentTarget.value })}
                  style={{ flex: 1, minWidth: 260 }}
                />
                <TextInput
                  size="xs"
                  label="Started with"
                  description="Three dashes: npm eats the first pair"
                  value={(draft?.command || []).join(' ')}
                  onChange={(event) =>
                    setSettings({ command: event.currentTarget.value })
                  }
                  style={{ width: 220 }}
                />
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<FolderSearch size={14} />}
                  loading={busy}
                  onClick={() => save(draft)}
                >
                  Check it
                </Button>
              </Group>
              <Text size="xs" c={install.ok ? 'dimmed' : 'orange'} mt={6}>
                {install.ok
                  ? `Found it: ${install.sites} sites, started with ${install.runs}.${
                      install.writable ? '' : ' The folder is not writable by Dispatcharr.'
                    }`
                  : install.why ||
                    'Say where it is, and press Check it.'}
              </Text>
            </Box>

            {/* How a grab is going */}
            <Box style={{ padding: '8px 16px', borderBottom: '1px solid #3f3f46' }}>
              <Text size="xs" c="dimmed">
                {page?.running
                  ? `Grabbing ${said.name || ''}: ${(said.done || 0).toLocaleString()} of ${(
                      said.total || 0
                    ).toLocaleString()} · ${said.now || ''}`
                  : said.finished_at
                    ? `Last grab finished ${when(said.finished_at)}${
                        said.said ? ` · ${said.said}` : ''
                      }`
                    : 'Not grabbed yet.'}
                {' — '}
                A scrape is thousands of requests and takes hours; it runs in the
                background, one at a time, and the guide you have is only replaced once the
                new one has been read back and found to hold something.
              </Text>
              {page?.running && (
                <Progress mt={6} value={through} animated striped size="sm" />
              )}
            </Box>

            {(error || notice) && (
              <Stack gap="xs" p="md" style={{ borderBottom: '1px solid #3f3f46' }}>
                {error && <Alert color="red">{error}</Alert>}
                {notice && (
                  <Alert color="blue" withCloseButton onClose={() => setNotice(null)}>
                    {notice}
                  </Alert>
                )}
              </Stack>
            )}

            {/* The guides */}
            <Box p="md">
              <Stack gap="sm">
                {(draft?.jobs || []).length === 0 && (
                  <Text size="sm" c="dimmed">
                    No guides yet. Add one: choose a channel list the grabber has (or one
                    you made), say where to write the guide, and make an EPG source for it.
                  </Text>
                )}
                {(draft?.jobs || []).map((job) => (
                  <Guide
                    key={job.id || job.name}
                    job={job}
                    channelFiles={page?.channel_files || []}
                    epgSources={page?.epg_sources || []}
                    running={page?.running}
                    busy={busy}
                    onChange={(changed) =>
                      setDraft({
                        ...draft,
                        jobs: draft.jobs.map((one) =>
                          one === job ? changed : one
                        ),
                      })
                    }
                    onRemove={setRemoving}
                    onGrab={grab}
                    onMakeSource={makeSource}
                  />
                ))}
                <Group>
                  <Button
                    size="xs"
                    variant="default"
                    leftSection={<Plus size={14} />}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        jobs: [
                          ...(draft?.jobs || []),
                          {
                            ...(page?.job_defaults || {}),
                            id: '',
                            name: 'Guide',
                            enabled: true,
                            output: `${draft?.folder || '/opt/iptv-org-epg'}/data/guide.xmltv`,
                          },
                        ],
                      })
                    }
                  >
                    Add a guide
                  </Button>
                  <Tooltip label="What the grabber is told, in the words it would have been typed in">
                    <Text size="xs" c="dimmed">
                      {(draft?.command || []).join(' ')} --channels=… --output=…
                    </Text>
                  </Tooltip>
                </Group>
              </Stack>
            </Box>

            {/* The list the guides are pointed at, made here rather than with a grep and
                a shell that has to be reached for */}
            {install.ok && (
              <Box p="md" style={{ borderTop: '1px solid #3f3f46' }}>
                <MakeList
                  channelFiles={page?.channel_files || []}
                  folder={draft?.folder || '/opt/iptv-org-epg'}
                  onMade={(path) => {
                    setNotice(`Made ${path}. Point a guide at it above.`);
                    load(true);
                  }}
                />
              </Box>
            )}
          </Paper>
        </Stack>
      </Box>

      <ConfirmationDialog
        opened={!!removing}
        onClose={() => setRemoving(null)}
        onConfirm={() => {
          const job = removing;
          setRemoving(null);
          save({ ...draft, jobs: draft.jobs.filter((one) => one !== job) });
        }}
        title="Remove this guide?"
        message="It stops being grabbed. The XMLTV file it wrote stays where it is, and so does the EPG source reading it."
        confirmLabel="Remove"
      />
    </>
  );
};

export default EpgGrabberTable;
