import React, { useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Code,
  CopyButton,
  Group,
  NativeSelect,
  NumberInput,
  Paper,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core';
import API from '../../api';

// Captions made from the sound (fork/subtitles.md step 3, §5b): the caption worker, what this
// server has, what each model measures here and what fits it. Nothing is installed,
// downloaded or measured until someone asks; making captions while TVs watch comes next.

const PANEL = {
  backgroundColor: '#27272A',
  border: '1px solid #3f3f46',
  borderRadius: 'var(--mantine-radius-md)',
};

const QUALITY = [
  { value: 'balanced', label: 'Balanced: room for one channel more' },
  { value: 'best', label: 'Best text: the largest model that keeps up' },
  { value: 'channels', label: 'Most channels at once' },
];
const TRANSLATORS = [
  {
    value: '',
    label: 'Automatic: DeepL if its key is set, else Ollama, else Opus-MT',
  },
  { value: 'deepl', label: 'DeepL (Service keys)' },
  { value: 'ollama', label: 'A model in Ollama' },
  {
    value: 'opus-mt',
    label: 'Opus-MT in the worker (about 300 MB per language pair)',
  },
  { value: 'off', label: 'Off: the original lines' },
];

const TRANSLATION = {
  ollama: 'a language model in Ollama',
  'opus-mt': 'small translation models (Opus-MT)',
};

const size = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`);

export const machineLine = (machine) => {
  if (!machine) return '';
  const cards = (machine.gpus || []).map(
    (g) => `${g.name} (${size(g.memory_mb)})`
  );
  const cpu = machine.cpu || {};
  const parts = [
    cards.length ? cards.join(', ') : 'No NVIDIA card',
    `${cpu.cores || '?'} processor threads${cpu.avx2 ? ' with AVX2' : ''}${cpu.arch && cpu.arch !== 'x86_64' ? ` (${cpu.arch})` : ''}`,
  ];
  if (machine.memory?.total_mb)
    parts.push(
      `${size(machine.memory.total_mb)} memory, ${size(machine.memory.free_mb)} free`
    );
  if (machine.disk_free_mb)
    parts.push(`${size(machine.disk_free_mb)} disk free`);
  return parts.join(' · ');
};

const busy = (status) =>
  status &&
  (status.install?.requested ||
    ['installing', 'removing'].includes(status.install?.state) ||
    status.models.some((m) => m.downloading || m.benchmarking));

const CaptionsCard = () => {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);
  const [address, setAddress] = useState(null);

  const load = useCallback(async () => {
    try {
      setStatus(await API.getCaptions());
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'The caption settings could not be read.');
    }
  }, []);

  useEffect(() => {
    if (open && !status) load();
  }, [open, status, load]);

  // While something is being installed, downloaded or measured, look again every few seconds
  useEffect(() => {
    if (!open || !busy(status)) return undefined;
    const timer = setTimeout(load, 3000);
    return () => clearTimeout(timer);
  }, [open, status, load]);

  const act = async (action, model) => {
    try {
      setStatus(await API.captionsAction(action, model));
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'That did not work.');
    }
  };

  const save = async (values) => {
    try {
      setStatus(await API.setCaptions(values));
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Those settings were not kept.');
    }
  };

  const worker = status?.worker;
  const install = status?.install || {};
  const proposal = status?.proposal;
  const settings = status?.settings;

  return (
    <Paper style={PANEL}>
      <UnstyledButton
        onClick={() => setOpen(!open)}
        style={{ width: '100%', padding: '16px' }}
        aria-expanded={open}
      >
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            <Text fw={600}>Captions from the sound</Text>
            {status && (
              <Badge
                size="sm"
                variant="light"
                color={worker.running ? 'green' : 'gray'}
              >
                {worker.running
                  ? `Worker running${worker.device === 'cuda' ? ' on the card' : ' on the processor'}`
                  : 'Not installed'}
              </Badge>
            )}
          </Group>
          <Text size="xs" c="dimmed">
            For channels without subtitles: speech to text on this server
          </Text>
        </Group>
      </UnstyledButton>
      {open && (
        <Stack gap="md" p="md" pt={0}>
          {error && (
            <Alert
              color="red"
              variant="light"
              withCloseButton
              onClose={() => setError(null)}
            >
              {error}
            </Alert>
          )}
          {!status ? (
            <Text size="sm" c="dimmed">
              Looking…
            </Text>
          ) : (
            <>
              <Text size="sm" c="dimmed">
                A caption worker turns a channel's sound into text with a speech
                model. It runs apart from Dispatcharr and only when installed;
                what fits depends on this machine, so it is measured here.
                Making captions while a TV watches comes in the next release;
                this sets the worker up and shows what it can do.
              </Text>

              <Box>
                <Group justify="space-between">
                  <Text size="sm" fw={600}>
                    This server
                  </Text>
                  {worker.running && (
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      leftSection={<RefreshCw size={12} />}
                      onClick={() => act('look')}
                    >
                      Look again
                    </Button>
                  )}
                </Group>
                <Text size="sm">{machineLine(status.machine)}</Text>
                {status.machine.seen_from === 'dispatcharr' && (
                  <Text size="xs" c="dimmed">
                    Seen from Dispatcharr; the worker sees for itself once it
                    runs
                    {status.layout === 'docker'
                      ? ' (a card given only to its container shows then)'
                      : ''}
                    .
                  </Text>
                )}
                {worker.cuda_failed && (
                  <Text size="xs" c="orange">
                    The card is there but could not be used, so the processor
                    is: {worker.cuda_failed}
                  </Text>
                )}
              </Box>

              {proposal && (
                <Alert
                  variant="light"
                  color="blue"
                  title={`Fits this server: ${proposal.model}${proposal.measured ? '' : ' (a guess until measured)'}`}
                >
                  <Text size="sm">{proposal.why}</Text>
                  {proposal.translation && (
                    <Text size="sm">
                      Translation: {TRANSLATION[proposal.translation]}
                      {proposal.translation === 'ollama' &&
                        (status.translation.ollama
                          ? ` (Ollama found, ${status.translation.ollama.length} models)`
                          : ' (no Ollama found)')}
                      .
                    </Text>
                  )}
                  {status.translation.deepl && (
                    <Text size="sm">
                      A DeepL key is set and can translate too.
                    </Text>
                  )}
                  {proposal.measure_next && worker.running && (
                    <Text size="sm">
                      The hardware suggests {proposal.measure_next} could do
                      better: download and measure it to know.
                    </Text>
                  )}
                </Alert>
              )}

              <Switch
                checked={settings.live !== false}
                onChange={(e) => save({ live: e.currentTarget.checked })}
                label="Generated captions for TVs"
                description="arrTV offers “Generated captions” in its Subtitles menu; picking it makes captions from the sound of the channel being watched, read alongside the TV (no extra provider connection). Off: nothing is captioned, whatever a TV asks."
              />
              {worker.running && (worker.jobs || []).length > 0 && (
                <Text size="sm">
                  Captioning now:{' '}
                  {worker.jobs
                    .map(
                      (j) =>
                        `${j.key.slice(0, 8)}… (${j.state}${j.language ? `, ${j.language}` : ''}${j.behind ? `, ${j.behind}s behind` : ''})`
                    )
                    .join(', ')}
                </Text>
              )}

              <Group align="flex-end" gap="md">
                <NumberInput
                  label="Channels at once"
                  description="Captioned at the same time"
                  min={1}
                  max={20}
                  w={180}
                  value={settings.channels_at_once}
                  onChange={(v) => v && save({ channels_at_once: v })}
                />
                <NativeSelect
                  label="What matters most"
                  w={320}
                  data={QUALITY}
                  value={settings.quality}
                  onChange={(e) => save({ quality: e.currentTarget.value })}
                />
              </Group>

              <Group align="flex-end" gap="md">
                <NativeSelect
                  label="Translate captions"
                  description="A TV asking for its own language gets the lines translated, once per channel and language for every TV"
                  w={420}
                  data={TRANSLATORS}
                  value={settings.translator || ''}
                  onChange={(e) => save({ translator: e.currentTarget.value })}
                />
                {(settings.translator || '') !== 'off' &&
                  (status.translation.ollama || []).length > 0 && (
                    <NativeSelect
                      label="Ollama model"
                      w={240}
                      data={[
                        { value: '', label: 'The first one' },
                        ...status.translation.ollama.map((m) => ({
                          value: m,
                          label: m,
                        })),
                      ]}
                      value={settings.ollama_model || ''}
                      onChange={(e) =>
                        save({ ollama_model: e.currentTarget.value })
                      }
                    />
                  )}
              </Group>

              <Box>
                <Text size="sm" fw={600}>
                  The worker
                </Text>
                {status.layout === 'docker' ? (
                  <Stack gap={4}>
                    <Text size="sm" c="dimmed">
                      In Docker the worker is a container of its own: add this
                      to the docker-compose.yml that has Dispatcharr, then
                      docker compose up -d. The first start installs
                      faster-whisper (and NVIDIA's libraries for a card), which
                      takes a few minutes.
                    </Text>
                    {['cpu', 'gpu'].map((kind) => (
                      <Box key={kind}>
                        <Group justify="space-between">
                          <Text size="xs" fw={600}>
                            {kind === 'gpu'
                              ? 'With an NVIDIA card (NVIDIA Container Toolkit installed)'
                              : 'Processor only'}
                          </Text>
                          <CopyButton value={status.docker[kind]}>
                            {({ copied, copy }) => (
                              <Button
                                size="compact-xs"
                                variant="subtle"
                                onClick={copy}
                              >
                                {copied ? 'Copied' : 'Copy'}
                              </Button>
                            )}
                          </CopyButton>
                        </Group>
                        <Code block>{status.docker[kind]}</Code>
                      </Box>
                    ))}
                  </Stack>
                ) : install.can_request ? (
                  <Group gap="sm">
                    {!worker.running && install.state !== 'installing' && (
                      <Button
                        size="xs"
                        onClick={() => act('install')}
                        loading={install.requested}
                      >
                        Install the caption worker
                      </Button>
                    )}
                    {(worker.running || install.state === 'failed') && (
                      <Button
                        size="xs"
                        color="red"
                        variant="light"
                        onClick={() => act('remove')}
                        loading={install.requested}
                      >
                        Remove it and its models
                      </Button>
                    )}
                    <Text
                      size="xs"
                      c={install.state === 'failed' ? 'red' : 'dimmed'}
                    >
                      {['installing', 'removing'].includes(install.state) &&
                      install.step
                        ? `${install.step}…`
                        : install.requested
                          ? 'Asked; the installer starts within a few seconds…'
                          : install.step ||
                            `faster-whisper in /opt (about 250 MB${status.machine.gpus?.length ? ', and 1.5 GB of NVIDIA libraries for the card' : ''}), as a service of its own.`}
                    </Text>
                  </Group>
                ) : (
                  <Text size="sm" c="dimmed">
                    This install was not made by Dispatch More's installer, so
                    the page cannot install the worker. Run
                    fork/patcher/captions.sh install by hand, or point to a
                    worker below.
                  </Text>
                )}
                <Group align="flex-end" gap="sm" mt="xs">
                  <TextInput
                    label="Worker address"
                    description="Another machine's worker can be used too (worker.py --host 0.0.0.0 --token …)"
                    w={360}
                    placeholder={status.worker_url}
                    value={address ?? settings.worker_url}
                    onChange={(e) => setAddress(e.currentTarget.value)}
                    onBlur={() => {
                      if (address !== null && address !== settings.worker_url)
                        save({ worker_url: address });
                      setAddress(null);
                    }}
                  />
                  <TextInput
                    label="Token"
                    w={200}
                    defaultValue={settings.token}
                    onBlur={(e) =>
                      e.currentTarget.value !== settings.token &&
                      save({ token: e.currentTarget.value })
                    }
                  />
                </Group>
                {!worker.running && worker.error && (
                  <Text size="xs" c="red">
                    {worker.error}
                  </Text>
                )}
              </Box>

              {worker.running && (
                <Table striped withTableBorder={false} verticalSpacing={4}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>Model</Table.Th>
                      <Table.Th>Download</Table.Th>
                      <Table.Th>Measured here</Table.Th>
                      <Table.Th />
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {status.models.map((m) => (
                      <Table.Tr key={m.name}>
                        <Table.Td>
                          <Group gap={6}>
                            <Text size="sm">{m.label}</Text>
                            {proposal?.model === m.name && (
                              <Badge size="xs" variant="light">
                                fits
                              </Badge>
                            )}
                          </Group>
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" c="dimmed">
                            {m.downloaded ? 'Downloaded' : size(m.download_mb)}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" c={m.error ? 'red' : undefined}>
                            {m.error ||
                              (m.benchmarking
                                ? 'Measuring…'
                                : m.benchmark
                                  ? `${m.benchmark.channels} channel${m.benchmark.channels === 1 ? '' : 's'} at once (${m.benchmark.rtf.toFixed(2)} s per second of sound, ${m.benchmark.device === 'cuda' ? 'card' : 'processor'})`
                                  : '')}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          {m.downloading ? (
                            <Text size="xs" c="dimmed">
                              Downloading…
                            </Text>
                          ) : !m.downloaded ? (
                            <Button
                              size="compact-xs"
                              variant="light"
                              onClick={() => act('download', m.name)}
                            >
                              Download
                            </Button>
                          ) : (
                            <Button
                              size="compact-xs"
                              variant="light"
                              disabled={m.benchmarking}
                              onClick={() => act('benchmark', m.name)}
                            >
                              {m.benchmark ? 'Measure again' : 'Measure'}
                            </Button>
                          )}
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              )}
            </>
          )}
        </Stack>
      )}
    </Paper>
  );
};

export default CaptionsCard;
