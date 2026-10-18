import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Group,
  Image,
  Loader,
  Modal,
  Paper,
  Stack,
  Text,
} from '@mantine/core';
import API from '../../api';

// Do a channel's streams show the same thing? (apps/channels/picture_compare.py) Every
// stream is opened at the same moment as the channel's first and read for a minute and a
// half; what the picture and the sound do is compared, with the delay between providers
// found. It only says so: nothing on the channel changes.

const POLL_MS = 3000;

const VERDICT = {
  same: { color: 'green', label: 'Same' },
  different: { color: 'red', label: 'Different' },
  unsure: { color: 'gray', label: "Can't tell" },
  // Not matched in the ten seconds a check reads: watched for a minute and a half next
  pending: { color: 'cyan', label: 'Watching longer' },
};

export const sessionText = (result) => {
  if (!result) return '';
  if (result.state === 'queued')
    return result.with_check
      ? 'Stream Check is running: it compares this first, as soon as the logins are free.'
      : 'Starting…';
  if (result.state !== 'running') return '';
  const minutes = Math.max(1, Math.ceil(((result.sessions || 1) * 95) / 60));
  return `Watching them side by side: session ${result.session || 1} of ${result.sessions || 1} (about ${minutes} min in all)`;
};

const Thumb = ({ data, label }) =>
  data ? (
    <Stack gap={2} align="center">
      <Image
        src={`data:image/jpeg;base64,${data}`}
        w={192}
        radius="sm"
        alt={label}
      />
      <Text size="xs" c="dimmed">
        {label}
      </Text>
    </Stack>
  ) : null;

const ResultRow = ({ row, reference }) => {
  const verdict = VERDICT[row.verdict] || VERDICT.unsure;
  return (
    <Paper withBorder p="xs">
      <Stack gap={6}>
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
            <Badge color={verdict.color} variant="light">
              {verdict.label}
            </Badge>
            <Text size="sm" fw={600} truncate>
              {row.name}
            </Text>
          </Group>
          <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
            {row.account}
            {row.same_provider ? ' · same provider' : ''}
          </Text>
        </Group>
        <Text size="sm">{row.why}</Text>
        {row.offset != null && (
          <Text size="xs" c="dimmed">
            Best match {Math.round(row.share * 100)}% of {row.moving} changing
            moments, clear by {Math.round((row.clear || 0) * 100)} points
            {row.sound != null
              ? ` · sound alike ${row.sound.toFixed(2)}`
              : ' · no sound to compare'}
          </Text>
        )}
        {(row.thumb_ref || row.thumb) && (
          <Group gap="md" wrap="wrap">
            <Thumb
              data={row.thumb_ref}
              label={reference?.name || 'The first'}
            />
            <Thumb data={row.thumb} label={row.name} />
          </Group>
        )}
      </Stack>
    </Paper>
  );
};

const PictureCompare = ({ channel, onClose }) => {
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!channel?.id) return;
    try {
      const found = await API.getPictureCompare(channel.id);
      setResult(found.result);
    } catch (e) {
      setError(e?.body?.error || 'Could not read the comparison.');
    }
  }, [channel?.id]);

  useEffect(() => {
    setResult(null);
    setError(null);
    if (!channel?.id) return;
    setLoading(true);
    load().finally(() => setLoading(false));
  }, [channel?.id, load]);

  const going = ['queued', 'running'].includes(result?.state);
  useEffect(() => {
    if (!going) return undefined;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [going, load]);

  const start = async () => {
    setError(null);
    try {
      await API.startPictureCompare(channel.id);
      await load();
    } catch (e) {
      setError(e?.body?.error || 'Could not start the comparison.');
    }
  };

  return (
    <Modal
      opened={!!channel}
      onClose={onClose}
      title={`Do the streams of "${channel?.name || ''}" show the same thing?`}
      size="xl"
    >
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          Every stream is opened at the same moment as the channel's first and
          watched for a minute and a half: the picture and the sound are
          compared, and the delay between providers is found. Two logins of one
          provider carry the same streams, so each is compared once. This only
          tells you; nothing on the channel changes.
        </Text>
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
        {loading ? (
          <Loader size="sm" />
        ) : (
          <>
            {going && (
              <Group gap="xs">
                <Loader size="xs" />
                <Text size="sm">{sessionText(result)}</Text>
              </Group>
            )}
            {result?.error && (
              <Alert color="yellow" variant="light">
                {result.error}
              </Alert>
            )}
            {result?.reference && (
              <Text size="sm">
                Compared with: <b>{result.reference.name}</b> (
                {result.reference.account})
                {result.started &&
                  ` · ${new Date(result.started * 1000).toLocaleString()}`}
              </Text>
            )}
            {result?.quick && (
              <Text size="xs" c="dimmed">
                Compared while Stream Check checked them: ten seconds of each,
                read at the same moment. A stream that did not match in those
                ten seconds is watched for a minute and a half next, and only
                that can call it different.
              </Text>
            )}
            {(result?.results || []).map((row) => (
              <ResultRow key={row.id} row={row} reference={result.reference} />
            ))}
            <Group justify="flex-end">
              <Button onClick={start} disabled={going} loading={going}>
                {result ? 'Compare again' : 'Compare now'}
              </Button>
            </Group>
          </>
        )}
      </Stack>
    </Modal>
  );
};

export default PictureCompare;
