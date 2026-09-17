import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Group,
  NumberInput,
  Select,
  Stack,
  Switch,
  Text,
} from '@mantine/core';
import API from '../../api';

const EXPLANATION = `Providers close and reopen connections as normal behaviour, every few minutes on some of them. Dispatcharr counts each close as a failure, and after three inside half an hour it gives up on the stream, moves to the next one in the channel, and the channel eventually dies. That is why a stream can work and then stop after a while: the longer someone watches, the likelier it becomes.

With this on, a close is forgiven when the connection had been delivering video for long enough to call it working: the count is cleared instead of increased, so a rotation is a fresh start rather than a step towards giving up.

What it costs: a source that plays for a minute and drops, over and over, is kept instead of being replaced by the next stream in the channel. That is what the limit below is for, and why it can be applied only to the channels a media server is watching, where a long stream is the point and switching stream is most disruptive.

A stream that fails quickly still runs out of retries and still fails over, because a connection that never became stable is never forgiven.`;

const StreamRecovery = () => {
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await API.getStreamRecovery();
      setSettings(data.settings);
    } catch {
      setError('Could not load the stream recovery settings.');
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = async (changes) => {
    setSaving(true);
    setError(null);
    try {
      const data = await API.saveStreamRecovery({ ...settings, ...changes });
      setSettings(data.settings);
    } catch (e) {
      setError(e?.body?.error || 'Could not save that.');
    } finally {
      setSaving(false);
    }
  };

  if (!settings) return null;

  return (
    <Card withBorder padding="sm">
      <Group justify="space-between" wrap="wrap">
        <Group gap="xs">
          <Text fw={600} size="sm">
            Stream Recovery
          </Text>
          <Text size="xs" c="dimmed">
            Keeps a channel alive when the provider closes a connection that was
            working
          </Text>
        </Group>
        <Group gap="xs">
          <Button
            size="compact-xs"
            variant="subtle"
            onClick={() => setOpen(!open)}
          >
            {open ? 'Hide' : 'What does this do?'}
          </Button>
          <Switch
            checked={!!settings.enabled}
            aria-label="Stream Recovery"
            disabled={saving}
            onChange={(event) => save({ enabled: event.currentTarget.checked })}
          />
        </Group>
      </Group>

      {open && (
        <Text size="sm" mt="xs" style={{ whiteSpace: 'pre-line' }}>
          {EXPLANATION}
        </Text>
      )}

      {error && (
        <Alert color="red" mt="xs">
          {error}
        </Alert>
      )}

      {settings.enabled && (
        <Stack gap="xs" mt="sm">
          <Group gap="xs" align="flex-end" wrap="wrap">
            <NumberInput
              size="xs"
              w={190}
              label="Working for at least"
              description="Seconds of video before a close is forgiven"
              min={5}
              max={600}
              value={settings.stable_seconds}
              onChange={(value) => save({ stable_seconds: Number(value) })}
            />
            <NumberInput
              size="xs"
              w={190}
              label="At most per hour"
              description="After this, a channel is failing, not rotating"
              min={1}
              max={120}
              value={settings.max_per_hour}
              onChange={(value) => save({ max_per_hour: Number(value) })}
            />
            <Select
              size="xs"
              w={230}
              label="Applies to"
              description="Which channels this is used for"
              value={settings.scope}
              onChange={(value) => value && save({ scope: value })}
              data={[
                {
                  value: 'media_servers',
                  label: 'Channels a media server is watching',
                },
                { value: 'all', label: 'Every channel' },
              ]}
            />
          </Group>
          <Text size="xs" c="dimmed">
            What it does is shown under Settings → Streaming → Diagnostics →
            Channel health.
          </Text>
        </Stack>
      )}
    </Card>
  );
};

export default StreamRecovery;
