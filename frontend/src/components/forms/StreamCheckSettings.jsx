import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import {
  Alert,
  Box,
  Button,
  Group,
  MultiSelect,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
  UnstyledButton,
} from '@mantine/core';

// How and when Stream Check runs. Saved with a button rather than on every change: a
// change to when it runs is best made once, not keystroke by keystroke.

// Every setting carries a sentence or two saying what it does, and all of them at once
// is a wall nobody reads. Shut, each section is one line saying what it is for; open, its
// settings get two columns of room instead of four cramped ones.
const Section = ({ title, about, children, openAtFirst = false }) => {
  const [open, setOpen] = useState(openAtFirst);
  return (
    <Box
      style={{
        border: '1px solid #3f3f46',
        borderRadius: 'var(--mantine-radius-sm)',
      }}
    >
      <UnstyledButton
        onClick={() => setOpen(!open)}
        aria-label={`${open ? 'Close' : 'Open'} ${title}`}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          width: '100%',
          padding: '8px 12px',
        }}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <Text size="xs" fw={700} tt="uppercase" c="dimmed">
          {title}
        </Text>
        <Text size="xs" c="dimmed" style={{ minWidth: 0 }}>
          {about}
        </Text>
      </UnstyledButton>
      {open && (
        <Box p="md" pt={0}>
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            {children}
          </SimpleGrid>
        </Box>
      )}
    </Box>
  );
};

const StreamCheckSettings = ({ value, groups, onSave, saving, onClear }) => {
  const [draft, setDraft] = useState(value);
  const set = (changes) => setDraft({ ...draft, ...changes });
  const number = (field) => (given) =>
    set({ [field]: given === '' ? '' : Number(given) });

  return (
    <Stack gap="md">
      <Stack gap="xs">
        <Section
          title="When"
          about="how often it runs, and whether it runs while people are watching"
          openAtFirst
        >
          <Switch
            size="xs"
            color="green"
            label="Only while nothing is playing"
            description="Off: checks go on while people watch, but never on a provider anyone is using -- every account on the same server, login or server group counts as one provider. On: nothing is checked while anyone watches anything."
            checked={!!draft.only_when_idle}
            onChange={(e) => set({ only_when_idle: e.currentTarget.checked })}
          />
          <Switch
            size="xs"
            color="green"
            label="Believe the playlist"
            description="On: a stream its provider has stopped listing is taken as gone, without a connection being opened for it -- Dispatcharr already marks those on every refresh, and deletes them by itself after the account's stale days. Off: they are opened and checked like any other."
            checked={!!draft.trust_the_playlist}
            onChange={(e) =>
              set({ trust_the_playlist: e.currentTarget.checked })
            }
          />
          <Switch
            size="xs"
            label="Check streams by itself"
            description="Off, it only runs when started here."
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

        <Section
          title="How"
          about="how long to wait for a stream, and how long to wait between them"
        >
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

        <Section
          title="Failing streams"
          about="when a stream counts as broken, and what is done about it by itself"
        >
          <Switch
            size="xs"
            label="Look for black, frozen and 'no stream' pictures"
            description="Looks a few seconds into every stream. A picture that is black, does not move, or is the provider's own 'no stream' card is a failure of its own. Each check takes that much longer."
            checked={!!draft.picture_check}
            onChange={(e) => set({ picture_check: e.currentTarget.checked })}
          />
          {draft.picture_check && (
            <NumberInput
              size="xs"
              label="Seconds to look"
              min={4}
              max={20}
              value={draft.picture_seconds}
              onChange={number('picture_seconds')}
            />
          )}
          {draft.picture_check && (
            <NumberInput
              size="xs"
              label="Look at a working stream's picture every (days)"
              description="A stream that played last time gets the quick check until then, which is most of what makes a run fast. 0 looks at every picture on every run."
              min={0}
              max={60}
              value={draft.picture_every_days ?? 3}
              onChange={number('picture_every_days')}
            />
          )}
          {draft.picture_check && (
            <Switch
              size="xs"
              label="Look again before calling a picture wrong"
              description="A black, frozen or 'no stream' picture is looked at again later in the same run. Seen again, it counts; three clean looks, and it plays. A news desk or a dark scene can look wrong for a moment."
              checked={draft.relook_pictures !== false}
              onChange={(e) =>
                set({ relook_pictures: e.currentTarget.checked })
              }
            />
          )}
          <Switch
            size="xs"
            label="Check failing streams again"
            description="Without waiting for the next full run."
            checked={!!draft.recheck_failed}
            onChange={(e) => set({ recheck_failed: e.currentTarget.checked })}
          />
          {draft.recheck_failed && (
            <>
              <Select
                size="xs"
                label="When"
                allowDeselect={false}
                value={draft.recheck_mode || 'hours'}
                onChange={(mode) => mode && set({ recheck_mode: mode })}
                data={[
                  { value: 'hours', label: 'Every few hours' },
                  {
                    value: 'refresh',
                    label: "After each refresh of its provider's playlist",
                  },
                ]}
              />
              {draft.recheck_mode !== 'refresh' && (
                <NumberInput
                  size="xs"
                  label="Every (hours)"
                  min={0.5}
                  value={draft.recheck_hours}
                  onChange={number('recheck_hours')}
                />
              )}
            </>
          )}
          <Switch
            size="xs"
            label="Park streams automatically after repeated failures (autopark)"
            description="Off: nothing is ever parked unless you park it. On: a stream that does not play at all, this many checks in a row, is parked by itself -- off its channels, still checked -- and goes back where it was as soon as it plays again. A stream the provider refuses, or with a black, frozen or 'no stream' picture, is never parked by itself: it is left for you."
            checked={!!draft.autopark}
            onChange={(e) => set({ autopark: e.currentTarget.checked })}
          />
          {draft.autopark && (
            <NumberInput
              size="xs"
              label="Failed checks in a row"
              description="After a refresh, three means at least two refreshes went by."
              min={2}
              value={draft.autopark_after}
              onChange={number('autopark_after')}
            />
          )}
          <NumberInput
            size="xs"
            label="Park by itself when this sure (out of 100)"
            description="0 is off. How sure Stream Check is that a stream is broken, built from what it found: failing again on another run, the fault still being there when it was looked at again, the same channel playing from another provider. Every stream says what its number is made of. Parking is the kind one — the stream comes off its channels, is still checked, and goes back by itself if it ever plays again."
            min={0}
            max={100}
            value={draft.park_above ?? 0}
            onChange={number('park_above')}
          />
          <NumberInput
            size="xs"
            label="Remove by itself when this sure (out of 100)"
            description="0 is off, and off is the sensible place to leave it. A removed stream comes off its channels and is not watched for any more, so nothing brings it back if the provider fixes it — park does that, this does not. Nothing short of a stream failing run after run gets near 100."
            min={0}
            max={100}
            value={draft.remove_above ?? 0}
            onChange={number('remove_above')}
          />
          {(draft.remove_above ?? 0) > 0 && (draft.remove_above ?? 0) < 60 && (
            <Alert color="orange" variant="light">
              Below about 60, a single bad run can be enough. A stream removed
              by mistake is not watched for again — park is the one that undoes
              itself.
            </Alert>
          )}
          <Switch
            size="xs"
            label="Hide a channel when all its streams are parked"
            description="A channel left with nothing but its fallback (like Could Not Dispatch) disappears from TVs and media servers -- the playlist, the guide, the tuner -- until a stream of it is put back. A channel you hid yourself is never shown again by this."
            checked={draft.hide_emptied_channels !== false}
            onChange={(e) =>
              set({ hide_emptied_channels: e.currentTarget.checked })
            }
          />
          <Switch
            size="xs"
            label="Also put streams I parked back when they work again"
            description="Off, a parked stream that works again waits for you to put it back."
            checked={!!draft.restore_recovered}
            onChange={(e) =>
              set({ restore_recovered: e.currentTarget.checked })
            }
          />
        </Section>

        <Section title="What" about="which channels are checked at all">
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
        </Section>
      </Stack>
      <Alert color="gray" p="xs">
        <Text size="xs">
          Before a provider&apos;s streams, its logins are looked at: an expired
          login, or one an Xtream Codes provider refuses, is not used. Before
          every stream, it looks whether anyone watches through that provider
          (and asks Xtream Codes providers, which see other apps too); a
          provider in use is left alone while the others are checked. Someone
          who starts watching through the provider being checked has the check
          dropped at once.
        </Text>
      </Alert>
      <Group justify="space-between">
        <Button size="xs" variant="subtle" color="red" onClick={onClear}>
          Forget all results
        </Button>
        <Button size="xs" loading={saving} onClick={() => onSave(draft)}>
          Save settings
        </Button>
      </Group>
    </Stack>
  );
};

export default StreamCheckSettings;
