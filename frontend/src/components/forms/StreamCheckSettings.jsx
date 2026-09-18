import React, { useState } from 'react';
import {
  Alert,
  Button,
  Group,
  MultiSelect,
  NumberInput,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';

// How and when Stream Check runs. Saved with a button rather than on every change: a
// change to when it runs is best made once, not keystroke by keystroke.

const Section = ({ title, children }) => (
  <Stack gap={8}>
    <Text size="xs" fw={700} tt="uppercase" c="dimmed">
      {title}
    </Text>
    {children}
  </Stack>
);

const StreamCheckSettings = ({ value, groups, onSave, saving }) => {
  const [draft, setDraft] = useState(value);
  const set = (changes) => setDraft({ ...draft, ...changes });
  const number = (field) => (given) =>
    set({ [field]: given === '' ? '' : Number(given) });

  return (
    <Stack gap="md">
      <SimpleGrid cols={{ base: 1, md: 3 }} spacing="lg">
        <Section title="When">
          <Switch
            size="xs"
            label="Check streams by itself"
            description="Off, it only runs when started here. It never uses a login someone is watching on."
            checked={!!draft.enabled}
            onChange={(e) => set({ enabled: e.currentTarget.checked })}
          />
          <NumberInput
            size="xs"
            label="Every (hours)"
            description="How long before a stream is looked at again."
            min={1}
            value={draft.every_hours}
            onChange={number('every_hours')}
          />
          <Group grow gap="xs">
            <TextInput
              size="xs"
              label="Only from"
              placeholder="23:00"
              description="Server time. Empty is any time."
              value={draft.window_from || ''}
              onChange={(e) =>
                set({ window_from: e.currentTarget.value.trim() })
              }
            />
            <TextInput
              size="xs"
              label="Until"
              placeholder="06:00"
              description="A run carries on in the next window."
              value={draft.window_to || ''}
              onChange={(e) => set({ window_to: e.currentTarget.value.trim() })}
            />
          </Group>
        </Section>

        <Section title="How">
          <NumberInput
            size="xs"
            label="Wait for a picture (seconds)"
            description="How long a stream has to start sending video."
            min={3}
            max={60}
            value={draft.timeout_seconds}
            onChange={number('timeout_seconds')}
          />
          <NumberInput
            size="xs"
            label="Pause between streams (seconds)"
            description="Per provider. Every provider is checked at the same time, one stream each."
            min={0}
            max={60}
            value={draft.gap_seconds}
            onChange={number('gap_seconds')}
          />
          <NumberInput
            size="xs"
            label="Broken after failing (runs in a row)"
            description="Providers hiccup: one bad run is only failing."
            min={1}
            value={draft.broken_after}
            onChange={number('broken_after')}
          />
          <NumberInput
            size="xs"
            label="Provider down after (failures in a row)"
            description="When a provider's first streams in a run all fail, it is the provider that is down: it is left for the run, and those streams are not counted against."
            min={2}
            value={draft.account_failures}
            onChange={number('account_failures')}
          />
        </Section>

        <Section title="What">
          <MultiSelect
            size="xs"
            label="Channel groups"
            description="The channels whose streams are checked. None is every channel."
            data={(groups || []).map((g) => ({
              value: String(g.id),
              label: g.name,
            }))}
            value={(draft.channel_groups || []).map(String)}
            onChange={(picked) => set({ channel_groups: picked.map(Number) })}
            searchable
            clearable
          />
          <Switch
            size="xs"
            label="Put parked streams back when they work again"
            description="Off, a parked stream that works again waits for you to put it back."
            checked={!!draft.restore_recovered}
            onChange={(e) =>
              set({ restore_recovered: e.currentTarget.checked })
            }
          />
        </Section>
      </SimpleGrid>
      <Alert color="gray" p="xs">
        <Text size="xs">
          Before a provider&apos;s streams, its logins are looked at: an expired
          login, or one an Xtream Codes provider refuses, is not used. Before
          every stream, a login nobody is watching on is picked, and a
          connection taken the way a viewer takes one, so a provider is never
          asked for more than it allows. A viewer who comes onto that login has
          the check dropped at once; a provider with every login in use waits
          while the others go on.
        </Text>
      </Alert>
      <Group justify="flex-end">
        <Button size="xs" loading={saving} onClick={() => onSave(draft)}>
          Save settings
        </Button>
      </Group>
    </Stack>
  );
};

export default StreamCheckSettings;
