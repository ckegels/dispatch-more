import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CalendarClock,
  Eye,
  Pin,
  Plus,
  RefreshCw,
  RotateCcw,
  SlidersHorizontal,
  Trash2,
  Users,
} from 'lucide-react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  TextInput,
  Textarea,
  UnstyledButton,
} from '@mantine/core';
import API from '../../api';
import Section from '../forms/SettingsSection';
import ConfirmationDialog from '../ConfirmationDialog';

// Groups of channels by what is on them right now: a Cooking group holds a copy of every
// channel airing a cooking show and lets it go when the show is over. Laid out as the other
// tabs are, a panel with a toolbar; under it a card per group to switch on or off, and the
// chosen group's channels, what joins next, and what it takes.

const PANEL = {
  backgroundColor: '#27272A',
  border: '1px solid #3f3f46',
  borderRadius: 'var(--mantine-radius-md)',
};

const time = (iso) =>
  iso
    ? new Date(iso).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      })
    : '';

const day = (iso) => {
  if (!iso) return '';
  const when = new Date(iso);
  const today = new Date();
  return when.toDateString() === today.toDateString()
    ? time(iso)
    : `${when.toLocaleDateString([], { weekday: 'short' })} ${time(iso)}`;
};

const slug = (name) =>
  name
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 30) || 'group';

// What a group takes, as the owner can change it; everything else is the page's
const EDITABLE = [
  'name',
  'category_words',
  'title_words',
  'title_exclusions',
  'disqualifiers',
  'use_title_words',
  'use_disqualifiers',
  'always',
  'never',
  'permanent',
  'one_per_airing',
];

const GroupCard = ({ group, chosen, onChoose, onSwitch, live }) => {
  const inNow = group.in_group.length;
  const next = group.coming[0];
  return (
    <Paper
      p="sm"
      style={{
        ...PANEL,
        borderColor: chosen ? 'var(--mantine-color-blue-6)' : '#3f3f46',
        opacity: group.on ? 1 : 0.75,
      }}
    >
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <UnstyledButton
          onClick={onChoose}
          style={{ flex: 1, minWidth: 0 }}
          aria-label={`Show ${group.name}`}
        >
          <Group gap={6} wrap="nowrap">
            <Text fw={600} truncate>
              {group.name}
            </Text>
            {!group.preset && (
              <Badge size="xs" variant="light" color="grape">
                yours
              </Badge>
            )}
          </Group>
        </UnstyledButton>
        <Switch
          size="sm"
          checked={!!group.on}
          onChange={(event) => onSwitch(event.currentTarget.checked)}
          aria-label={`${group.name} on`}
        />
      </Group>
      <UnstyledButton
        onClick={onChoose}
        style={{ width: '100%' }}
        tabIndex={-1}
      >
        <Group gap={6} mt={6}>
          {group.refused ? (
            <Text size="xs" c="red.4" lineClamp={2}>
              {group.refused}
            </Text>
          ) : group.on ? (
            <>
              <Badge
                size="sm"
                variant={inNow ? 'filled' : 'light'}
                color={live ? 'green' : 'gray'}
              >
                {inNow} in now
              </Badge>
              {(group.permanent || []).length > 0 && (
                <Badge
                  size="sm"
                  variant="light"
                  leftSection={<Pin size={10} />}
                >
                  {group.permanent.length} always
                </Badge>
              )}
              <Text
                size="xs"
                c="dimmed"
                truncate
                style={{ minWidth: 0, flex: 1 }}
              >
                {next
                  ? `Next: ${next.title} · ${day(next.start)}`
                  : group.planned
                    ? 'Nothing more coming up'
                    : 'Not worked out yet'}
              </Text>
            </>
          ) : (
            <Text size="xs" c="dimmed">
              Off
            </Text>
          )}
        </Group>
      </UnstyledButton>
    </Paper>
  );
};

const GroupDetail = ({ group, channels, onSave, onDelete, onReset, busy }) => {
  const [draft, setDraft] = useState(group);
  useEffect(() => setDraft(group), [group]);
  const dirty = EDITABLE.some(
    (key) => JSON.stringify(draft[key]) !== JSON.stringify(group[key])
  );
  const set = (key) => (value) => setDraft({ ...draft, [key]: value });
  const text = (key) => (event) => set(key)(event.currentTarget.value);
  const channelOptions = useMemo(
    () =>
      channels.map((one) => ({
        value: String(one.id),
        label: one.number != null ? `${one.number} · ${one.name}` : one.name,
      })),
    [channels]
  );

  return (
    <Paper style={PANEL}>
      <Box
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
          padding: '12px 16px',
          borderBottom: '1px solid #3f3f46',
        }}
      >
        <Group gap="sm">
          <TextInput
            size="xs"
            aria-label="Group name"
            value={draft.name}
            onChange={text('name')}
            style={{ width: 200 }}
          />
          <Text size="xs" c="dimmed">
            {group.on
              ? `${group.in_group.length} in the group now · ${group.copies} cop${
                  group.copies === 1 ? 'y' : 'ies'
                } kept`
              : 'Switched off'}
          </Text>
        </Group>
        <Group gap="sm">
          {group.preset ? (
            <Button
              size="xs"
              variant="default"
              leftSection={<RotateCcw size={14} />}
              onClick={onReset}
              disabled={busy}
            >
              Back to how it comes
            </Button>
          ) : (
            <Button
              size="xs"
              color="red"
              variant="light"
              leftSection={<Trash2 size={14} />}
              onClick={onDelete}
              disabled={busy}
            >
              Delete group
            </Button>
          )}
          <Button
            size="xs"
            disabled={!dirty || busy || !draft.name.trim()}
            onClick={() => onSave(draft)}
          >
            Save changes
          </Button>
        </Group>
      </Box>

      <Tabs defaultValue="now" keepMounted={false}>
        <Tabs.List px="sm">
          <Tabs.Tab value="now" leftSection={<Users size={14} />}>
            In the group now
          </Tabs.Tab>
          <Tabs.Tab value="coming" leftSection={<CalendarClock size={14} />}>
            Coming up
          </Tabs.Tab>
          <Tabs.Tab value="titles" leftSection={<Eye size={14} />}>
            Shows it takes
          </Tabs.Tab>
          <Tabs.Tab value="rules" leftSection={<SlidersHorizontal size={14} />}>
            What it takes
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="now" p="md">
          {group.in_group.length === 0 ? (
            <Text size="sm" c="dimmed">
              {group.on
                ? 'Nothing in the group right now.'
                : 'Switch the group on to fill it.'}
            </Text>
          ) : (
            <Table striped highlightOnHover verticalSpacing={4} fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>#</Table.Th>
                  <Table.Th>Channel</Table.Th>
                  <Table.Th>On now</Table.Th>
                  <Table.Th>Leaves</Table.Th>
                  <Table.Th>Watching</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {group.in_group.map((one) => (
                  <Table.Tr key={one.copy}>
                    <Table.Td>{one.number}</Table.Td>
                    <Table.Td>{one.name}</Table.Td>
                    <Table.Td>
                      {one.showing ? (
                        <Text size="sm" title={one.showing.why}>
                          {one.showing.title}{' '}
                          <Text span size="xs" c="dimmed">
                            {time(one.showing.start)}–{time(one.showing.end)}
                          </Text>
                        </Text>
                      ) : one.always ? (
                        <Badge
                          size="xs"
                          variant="light"
                          leftSection={<Pin size={10} />}
                        >
                          always in
                        </Badge>
                      ) : (
                        <Text size="xs" c="dimmed">
                          kept for a viewer
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>{one.always ? '—' : day(one.leaves)}</Table.Td>
                    <Table.Td>{one.viewers || ''}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="coming" p="md">
          {group.coming.length === 0 ? (
            <Text size="sm" c="dimmed">
              {group.planned
                ? 'Nothing joins in the hours the plan looks ahead.'
                : 'Not worked out yet: press “Work it out” above.'}
            </Text>
          ) : (
            <Table striped verticalSpacing={4} fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Joins</Table.Th>
                  <Table.Th>Channel</Table.Th>
                  <Table.Th>Show</Table.Th>
                  <Table.Th>Why</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {group.coming.map((one) => (
                  <Table.Tr key={`${one.source}-${one.joins}`}>
                    <Table.Td>{day(one.joins)}</Table.Td>
                    <Table.Td>{one.channel}</Table.Td>
                    <Table.Td>
                      {one.title}{' '}
                      <Text span size="xs" c="dimmed">
                        {time(one.start)}–{time(one.end)}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="xs" c="dimmed" lineClamp={1}>
                        {one.why}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="titles" p="md">
          {group.titles.length === 0 ? (
            <Text size="sm" c="dimmed">
              {group.planned
                ? 'No show in the guide is taken. Try other words under “What it takes”.'
                : 'Not worked out yet: press “Work it out” above.'}
            </Text>
          ) : (
            <Stack gap={4}>
              <Text size="xs" c="dimmed">
                {group.title_count} show{group.title_count === 1 ? '' : 's'}{' '}
                taken in the hours ahead
                {group.title_count > group.titles.length
                  ? `, the ${group.titles.length} most aired shown`
                  : ''}
                . One that does not belong goes under “Never” in What it takes.
              </Text>
              <Table striped verticalSpacing={2} fz="sm">
                <Table.Tbody>
                  {group.titles.map((one) => (
                    <Table.Tr key={one.title}>
                      <Table.Td>
                        {one.title}
                        {one.uncertain && (
                          <Badge
                            ml={6}
                            size="xs"
                            color="yellow"
                            variant="light"
                          >
                            by its title
                          </Badge>
                        )}
                      </Table.Td>
                      <Table.Td>{one.airings}×</Table.Td>
                      <Table.Td>
                        <Text size="xs" c="dimmed" lineClamp={1}>
                          {one.why}
                        </Text>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Stack>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="rules" p="md">
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            <Stack gap="sm">
              <MultiSelect
                size="xs"
                label="Channels always in this group"
                description="A copy of each stays in the group all the time, whatever is on"
                placeholder="Pick channels"
                data={channelOptions}
                value={(draft.permanent || []).map(String)}
                onChange={(value) => set('permanent')(value.map(Number))}
                searchable
                clearable
                limit={100}
                nothingFoundMessage="No channel by that name"
              />
              <Textarea
                size="xs"
                label="Guide categories"
                description="A programme whose category holds one of these joins. Parts of words count: “reis” finds Reisreportage."
                value={draft.category_words}
                onChange={text('category_words')}
                autosize
                minRows={2}
              />
              <Textarea
                size="xs"
                label="Always these shows"
                description="Whole titles, one per line; win over everything"
                value={draft.always}
                onChange={text('always')}
                autosize
                minRows={2}
              />
              <Textarea
                size="xs"
                label="Never these shows"
                description="Whole titles, one per line"
                value={draft.never}
                onChange={text('never')}
                autosize
                minRows={2}
              />
            </Stack>
            <Stack gap="sm">
              <Switch
                size="xs"
                label="Guess from words in the title"
                description="For shows no guide or online source knows. Marked as a guess."
                checked={!!draft.use_title_words}
                onChange={(event) =>
                  set('use_title_words')(event.currentTarget.checked)
                }
              />
              <Textarea
                size="xs"
                label="Title words"
                value={draft.title_words}
                onChange={text('title_words')}
                autosize
                minRows={2}
                disabled={!draft.use_title_words}
              />
              <Textarea
                size="xs"
                label="…but not titles with"
                value={draft.title_exclusions}
                onChange={text('title_exclusions')}
                autosize
                minRows={1}
                disabled={!draft.use_title_words}
              />
              <Switch
                size="xs"
                label="Refuse programmes also filed under"
                description="For a guide that files a documentary under Film, or news under Travel"
                checked={!!draft.use_disqualifiers}
                onChange={(event) =>
                  set('use_disqualifiers')(event.currentTarget.checked)
                }
              />
              <Textarea
                size="xs"
                aria-label="Refusing categories"
                value={draft.disqualifiers}
                onChange={text('disqualifiers')}
                autosize
                minRows={1}
                disabled={!draft.use_disqualifiers}
              />
              <Switch
                size="xs"
                label="One channel per airing"
                description="The same show on several copies of a channel joins once, on the lowest number"
                checked={draft.one_per_airing !== false}
                onChange={(event) =>
                  set('one_per_airing')(event.currentTarget.checked)
                }
              />
            </Stack>
          </SimpleGrid>
        </Tabs.Panel>
      </Tabs>
    </Paper>
  );
};

const ShowGroupsTable = () => {
  const [page, setPage] = useState(null);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const [chosen, setChosen] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState('');
  const [newWords, setNewWords] = useState('');
  const [confirming, setConfirming] = useState(null);

  const load = useCallback(async () => {
    try {
      setPage(await API.getShowGroups());
    } catch (e) {
      setError(e?.body?.error || 'Show Groups could not be read.');
    }
  }, []);

  useEffect(() => {
    load();
    // What is in a group changes by the minute
    const every = setInterval(load, 30000);
    return () => clearInterval(every);
  }, [load]);

  const act = async (work, done) => {
    setBusy(true);
    setError(null);
    try {
      const answer = await work();
      setPage(answer);
      setMessage(answer.message || done || null);
      return answer;
    } catch (e) {
      setError(e?.body?.error || 'That did not work.');
      return null;
    } finally {
      setBusy(false);
    }
  };

  const groups = useMemo(() => page?.groups || [], [page]);
  const settings = page?.settings;
  const live = !!settings?.live;
  const group = groups.find((one) => one.id === chosen) || null;

  // The groups as the server stores them: what the owner can change, and their ids
  const stored = (list) =>
    list.map((one) => ({
      id: one.id,
      on: one.on,
      ...Object.fromEntries(EDITABLE.map((key) => [key, one[key]])),
    }));

  const saveGroups = (list, done) =>
    act(() => API.saveShowGroups({ groups: stored(list) }), done);
  const saveSettings = (changes) =>
    act(() => API.saveShowGroups({ settings: { ...settings, ...changes } }));

  const switchGroup = (target, on) =>
    saveGroups(
      groups.map((one) => (one.id === target.id ? { ...one, on } : one))
    );

  const addGroup = async () => {
    const taken = new Set(groups.map((one) => one.id));
    let id = `my-${slug(newName)}`;
    for (let n = 2; taken.has(id); n += 1) id = `my-${slug(newName)}-${n}`;
    const answer = await saveGroups(
      [
        ...groups,
        {
          id,
          name: newName.trim(),
          on: true,
          category_words: newWords.trim() || newName.trim().toLowerCase(),
          title_words: '',
          title_exclusions: '',
          disqualifiers: '',
          use_title_words: false,
          use_disqualifiers: false,
          always: '',
          never: '',
          permanent: [],
          one_per_airing: true,
        },
      ],
      `Added ${newName.trim()}.`
    );
    if (answer) {
      setAdding(false);
      setNewName('');
      setNewWords('');
      setChosen(id);
    }
  };

  const counts = useMemo(() => {
    const on = groups.filter((one) => one.on);
    return {
      on: on.length,
      channels: on.reduce((sum, one) => sum + one.in_group.length, 0),
    };
  }, [groups]);

  const status = !page
    ? 'Reading…'
    : !live
      ? counts.on
        ? `Off. ${counts.on} group${counts.on === 1 ? '' : 's'} chosen; switch Show Groups on to fill them.`
        : 'Off. Switch a group on below, then Show Groups on.'
      : `On · ${counts.on} group${counts.on === 1 ? '' : 's'} · ${counts.channels} channel${
          counts.channels === 1 ? '' : 's'
        } in them now`;

  const plugin = page?.plugin;
  const lookups = page?.lookups;

  return (
    <Box
      style={{
        display: 'flex',
        justifyContent: 'center',
        minHeight: 'calc(100vh - 200px)',
      }}
    >
      <Stack gap="md" style={{ maxWidth: '1200px', width: '100%' }}>
        <Paper style={PANEL}>
          <Box
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: 8,
              padding: '16px',
              borderBottom: '1px solid #3f3f46',
            }}
          >
            <Group gap="sm">
              <Switch
                size="md"
                label="Show Groups"
                checked={live}
                disabled={!settings || busy}
                onChange={(event) =>
                  saveSettings({ live: event.currentTarget.checked })
                }
              />
            </Group>
            <Group gap="sm">
              <Button
                size="xs"
                variant="default"
                leftSection={<SlidersHorizontal size={16} />}
                onClick={() => setShowSettings(!showSettings)}
              >
                Settings
              </Button>
              <Button
                size="xs"
                variant="default"
                leftSection={<Eye size={16} />}
                onClick={() => act(() => API.runShowGroups('plan'))}
                disabled={busy || !counts.on}
                title="What the groups that are on would hold, from the guide as it is, without changing a channel"
              >
                Work it out
              </Button>
              <Button
                size="xs"
                variant="default"
                leftSection={<RefreshCw size={16} />}
                onClick={() => act(() => API.runShowGroups('update'))}
                disabled={busy || !live}
              >
                Update now
              </Button>
              <Button
                size="xs"
                variant="light"
                leftSection={<Plus size={16} />}
                onClick={() => setAdding(true)}
                disabled={!page}
              >
                Add a group
              </Button>
              <Button
                size="xs"
                color="red"
                variant="light"
                leftSection={<Trash2 size={16} />}
                onClick={() => setConfirming('remove')}
                disabled={busy || live}
                title={live ? 'Switch Show Groups off first' : undefined}
              >
                Remove everything
              </Button>
            </Group>
          </Box>
          <Box style={{ padding: '8px 16px' }}>
            <Group justify="space-between" gap="xs">
              <Text size="xs" c="dimmed">
                {status}
              </Text>
              <Text size="xs" c="dimmed">
                {page?.plan_made_local
                  ? `Worked out from the guide ${page.plan_made_local}`
                  : 'Not worked out yet'}
                {settings && ` · profile “${settings.profile_name}”`}
              </Text>
            </Group>
          </Box>

          {showSettings && settings && (
            <Box p="md" style={{ borderTop: '1px solid #3f3f46' }}>
              <Stack gap="xs">
                <Section
                  title="Joining and leaving"
                  about="when a channel comes into a group and when it goes"
                  openAtFirst
                >
                  <Group gap="lg" wrap="wrap" align="flex-end">
                    {[
                      ['join_ahead', 'Join before a show starts', 'minutes'],
                      [
                        'leave_after',
                        'Stay if another show starts within',
                        'minutes',
                      ],
                      ['linger', 'Then stay this much longer', 'minutes'],
                      ['min_length', 'Ignore shows shorter than', 'minutes'],
                      [
                        'viewer_grace',
                        'After the last viewer, wait',
                        'minutes',
                      ],
                    ].map(([key, label, unit]) => (
                      <NumberInput
                        key={key}
                        size="xs"
                        label={label}
                        suffix={` ${unit}`}
                        min={0}
                        value={settings[key]}
                        onBlur={(event) => {
                          const value = parseInt(event.currentTarget.value, 10);
                          if (!Number.isNaN(value) && value !== settings[key]) {
                            saveSettings({ [key]: value });
                          }
                        }}
                        style={{ width: 200 }}
                      />
                    ))}
                  </Group>
                </Section>
                <Section
                  title="Which channels"
                  about="the channels a copy may be taken of"
                >
                  <MultiSelect
                    size="xs"
                    label="Only from these channel groups"
                    description="Channels hidden from output never are"
                    placeholder="Every group"
                    data={(page.channel_groups || []).map((one) => ({
                      value: String(one.id),
                      label: `${one.name} (${one.count})`,
                    }))}
                    value={(settings.source_groups || []).map(String)}
                    onChange={(value) =>
                      saveSettings({ source_groups: value.map(Number) })
                    }
                    searchable
                    clearable
                  />
                </Section>
                <Section
                  title="Profile and numbers"
                  about="where the copies show up, and what your app is told"
                >
                  <Group gap="lg" wrap="wrap" align="flex-end">
                    <TextInput
                      size="xs"
                      label="Channel profile"
                      description="Your IPTV app reads /m3u/ and /epg/ with this name"
                      defaultValue={settings.profile_name}
                      onBlur={(event) => {
                        const value = event.currentTarget.value.trim();
                        if (value && value !== settings.profile_name) {
                          saveSettings({ profile_name: value });
                        }
                      }}
                      style={{ width: 220 }}
                    />
                    <NumberInput
                      size="xs"
                      label="Copies numbered from"
                      min={1}
                      value={settings.first_number}
                      onBlur={(event) => {
                        const value = parseInt(event.currentTarget.value, 10);
                        if (
                          !Number.isNaN(value) &&
                          value !== settings.first_number
                        ) {
                          saveSettings({ first_number: value });
                        }
                      }}
                      style={{ width: 160 }}
                    />
                    <NumberInput
                      size="xs"
                      label="Look ahead"
                      suffix=" hours"
                      min={1}
                      max={168}
                      value={settings.plan_hours}
                      onBlur={(event) => {
                        const value = parseInt(event.currentTarget.value, 10);
                        if (
                          !Number.isNaN(value) &&
                          value !== settings.plan_hours
                        ) {
                          saveSettings({ plan_hours: value });
                        }
                      }}
                      style={{ width: 140 }}
                    />
                    <Switch
                      size="xs"
                      label="Tell arrTV at once when channels come and go"
                      checked={!!settings.announce_changes}
                      onChange={(event) =>
                        saveSettings({
                          announce_changes: event.currentTarget.checked,
                        })
                      }
                    />
                  </Group>
                </Section>
                <Section
                  title="Shows no guide knows"
                  about="asking TVmaze, Wikidata, Wikipedia and TMDB what they are"
                >
                  <Stack gap="xs">
                    <Switch
                      size="xs"
                      label="Look them up online"
                      description="In the background, busiest first, each title once"
                      checked={!!settings.online_lookups}
                      onChange={(event) =>
                        saveSettings({
                          online_lookups: event.currentTarget.checked,
                        })
                      }
                    />
                    {lookups && (
                      <Text size="xs" c="dimmed">
                        {lookups.answered} of {lookups.asked} titles known ·{' '}
                        {lookups.waiting} waiting
                      </Text>
                    )}
                  </Stack>
                  <Stack gap="xs">
                    <TextInput
                      size="xs"
                      label="Wikipedia and Wikidata languages"
                      defaultValue={settings.wikipedia_languages}
                      onBlur={(event) => {
                        const value = event.currentTarget.value;
                        if (value !== settings.wikipedia_languages) {
                          saveSettings({ wikipedia_languages: value });
                        }
                      }}
                    />
                    <TextInput
                      size="xs"
                      label="TMDB key"
                      description="Free; its keywords know “cooking competition”"
                      defaultValue={settings.tmdb_key}
                      onBlur={(event) => {
                        const value = event.currentTarget.value.trim();
                        if (value !== settings.tmdb_key)
                          saveSettings({ tmdb_key: value });
                      }}
                    />
                  </Stack>
                </Section>
              </Stack>
            </Box>
          )}
        </Paper>

        {plugin?.enabled && (
          <Alert
            color="blue"
            variant="light"
            title="The Show Groups plugin is still installed"
          >
            <Group justify="space-between" gap="sm">
              <Text size="sm">
                It keeps “{plugin.group}”{plugin.live ? ' live' : ''}. Take its
                group over and it carries on here with the same channels and
                numbers, and the plugin is switched off.
              </Text>
              <Button
                size="xs"
                onClick={() => act(() => API.runShowGroups('take_over'))}
                disabled={busy}
              >
                Take it over
              </Button>
            </Group>
          </Alert>
        )}
        {error && (
          <Alert color="red" withCloseButton onClose={() => setError(null)}>
            {error}
          </Alert>
        )}
        {message && (
          <Alert
            color="green"
            variant="light"
            withCloseButton
            onClose={() => setMessage(null)}
          >
            {message}
          </Alert>
        )}

        <SimpleGrid cols={{ base: 1, sm: 2, md: 3, lg: 4 }} spacing="sm">
          {groups.map((one) => (
            <GroupCard
              key={one.id}
              group={one}
              live={live}
              chosen={one.id === chosen}
              onChoose={() => setChosen(one.id === chosen ? null : one.id)}
              onSwitch={(on) => switchGroup(one, on)}
            />
          ))}
        </SimpleGrid>

        {group ? (
          <GroupDetail
            group={group}
            channels={page?.channels || []}
            busy={busy}
            onSave={(draft) =>
              saveGroups(
                groups.map((one) =>
                  one.id === draft.id ? { ...one, ...draft } : one
                ),
                'Saved.'
              )
            }
            onDelete={() => setConfirming('delete')}
            onReset={() =>
              // Sent as nothing but whether it is on: the server fills in the rest as it comes
              act(
                () =>
                  API.saveShowGroups({
                    groups: stored(groups).map((one) =>
                      one.id === group.id ? { id: one.id, on: one.on } : one
                    ),
                  }),
                `${group.name} is back to how it comes.`
              )
            }
          />
        ) : (
          groups.length > 0 && (
            <Text size="sm" c="dimmed" ta="center">
              Pick a group to see its channels, what joins next, and what it
              takes.
            </Text>
          )
        )}

        {page?.activity?.length > 0 && (
          <Section title="Activity" about="every join and leave, and why">
            <Box style={{ gridColumn: '1 / -1' }}>
              <Text
                component="pre"
                size="xs"
                c="dimmed"
                style={{
                  whiteSpace: 'pre-wrap',
                  margin: 0,
                  maxHeight: 300,
                  overflow: 'auto',
                }}
              >
                {[...page.activity].reverse().join('\n')}
              </Text>
            </Box>
          </Section>
        )}
      </Stack>

      <Modal
        opened={adding}
        onClose={() => setAdding(false)}
        title="Add a group"
      >
        <Stack gap="sm">
          <TextInput
            label="Name"
            placeholder="Formula 1"
            value={newName}
            onChange={(event) => setNewName(event.currentTarget.value)}
            data-autofocus
          />
          <Textarea
            label="Guide categories"
            description="Words to look for in a programme's categories, with commas. Empty: the name."
            placeholder="formula 1, motorsport"
            value={newWords}
            onChange={(event) => setNewWords(event.currentTarget.value)}
            autosize
            minRows={2}
          />
          <Text size="xs" c="dimmed">
            Channels that should always be in it are picked once it is made,
            under “What it takes”.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setAdding(false)}>
              Cancel
            </Button>
            <Button onClick={addGroup} disabled={!newName.trim() || busy}>
              Add
            </Button>
          </Group>
        </Stack>
      </Modal>

      <ConfirmationDialog
        opened={confirming === 'remove'}
        onClose={() => setConfirming(null)}
        onConfirm={() => {
          setConfirming(null);
          act(() => API.runShowGroups('remove'));
        }}
        title="Remove everything"
        message="Every copy Show Groups made is deleted, with its channel groups and the profile. Your own channels are not touched; a copy someone is watching stays until they stop. The groups and their settings are kept."
        confirmLabel="Remove"
        cancelLabel="Cancel"
      />
      <ConfirmationDialog
        opened={confirming === 'delete' && !!group}
        onClose={() => setConfirming(null)}
        onConfirm={() => {
          setConfirming(null);
          setChosen(null);
          saveGroups(
            groups.filter((one) => one.id !== group.id),
            `Deleted ${group.name}.`
          );
        }}
        title={`Delete ${group?.name || 'group'}`}
        message="The group goes, and its copies with it within a minute (not while someone watches one)."
        confirmLabel="Delete"
        cancelLabel="Cancel"
      />
    </Box>
  );
};

export default ShowGroupsTable;
