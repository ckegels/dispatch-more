import React, { useState } from 'react';
import {
  Alert,
  Box,
  Group,
  MultiSelect,
  NumberInput,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';

// The levers, in the order a run is thought through: what to look at, how a channel is
// recognised, how its streams are ordered, what may be changed, and where its guide and
// logo come from. Every one says what it does, because most are only touched once.

const Section = ({ title, children }) => (
  <Stack gap={8}>
    <Text size="xs" fw={700} tt="uppercase" c="dimmed">
      {title}
    </Text>
    {children}
  </Stack>
);

const toOptions = (items, withCount) =>
  (items || []).map((item) => ({
    value: String(item.id),
    label:
      withCount && item.count != null
        ? `${item.name} (${item.count})`
        : item.name,
  }));

const ids = (values) => (values || []).map((value) => Number(value));
const asStrings = (values) => (values || []).map((value) => String(value));

// Rules and aliases are edited as text, one per line, and kept as the structures the
// server wants. What cannot be read is reported rather than silently dropped.
const rulesToText = (rules) =>
  (rules || [])
    .map(([find, replace]) => `${find} => ${replace ?? ''}`)
    .join('\n');

const textToRules = (text) =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [find, ...rest] = line.split('=>');
      return [find.trim(), rest.join('=>').trim()];
    });

const aliasesToText = (aliases) =>
  Object.entries(aliases || {})
    .map(([name, others]) => `${name} = ${(others || []).join(', ')}`)
    .join('\n');

const textToAliases = (text) =>
  Object.fromEntries(
    text
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.includes('='))
      .map((line) => {
        const [name, ...rest] = line.split('=');
        return [
          name.trim(),
          rest
            .join('=')
            .split(',')
            .map((alias) => alias.trim())
            .filter(Boolean),
        ];
      })
  );

const ChannelManagerLevers = ({ options, value, onChange }) => {
  const set = (changes) => onChange({ ...value, ...changes });
  // Kept as typed until it is valid, so a half-typed line is not thrown away
  const [rulesText, setRulesText] = useState(rulesToText(value.regex_rules));
  const [aliasText, setAliasText] = useState(aliasesToText(value.aliases));

  const profileValue =
    value.profiles === 'all'
      ? 'all'
      : value.profiles === 'none'
        ? 'none'
        : 'some';

  return (
    <SimpleGrid
      cols={{ base: 1, md: 2, lg: 3 }}
      spacing="lg"
      verticalSpacing="lg"
    >
      <Section title="What to look at">
        <MultiSelect
          size="xs"
          label="Providers"
          description="Where streams are taken from. The order you pick them in is the order they are preferred in. None picked is every one."
          data={toOptions(options.accounts)}
          value={asStrings(value.accounts)}
          onChange={(picked) => set({ accounts: ids(picked) })}
          searchable
          clearable
        />
        <MultiSelect
          size="xs"
          label="Stream groups"
          description="Which of the providers' groups to take streams from. None is every one."
          data={toOptions(options.stream_groups, true)}
          value={asStrings(value.stream_groups)}
          onChange={(picked) => set({ stream_groups: ids(picked) })}
          searchable
          clearable
        />
        <MultiSelect
          size="xs"
          label="Channel groups"
          description="Which of your channels may be given streams. None is every channel."
          data={toOptions(options.channel_groups, true)}
          value={asStrings(value.channel_groups)}
          onChange={(picked) => set({ channel_groups: ids(picked) })}
          searchable
          clearable
        />
      </Section>

      <Section title="Recognising a channel">
        <Select
          size="xs"
          label="Match names"
          description="How close a stream's name has to be to a channel's."
          allowDeselect={false}
          value={value.name_matching || 'exact'}
          onChange={(mode) => mode && set({ name_matching: mode })}
          data={[
            {
              value: 'exact',
              label:
                'Exactly, apart from a quality at the end and case (as DispatcharrUtils)',
            },
            {
              value: 'loose',
              label:
                'Loosely: letters and digits only, accents folded, quality anywhere',
            },
          ]}
        />
        <Select
          size="xs"
          label="Several channels of that name"
          description="When more than one of your channels is the one a stream belongs to."
          allowDeselect={false}
          value={value.several_matches || 'all'}
          onChange={(mode) => mode && set({ several_matches: mode })}
          data={[
            {
              value: 'all',
              label: 'Give it to each of them (as DispatcharrUtils)',
            },
            { value: 'conflict', label: 'Show a conflict and leave it alone' },
          ]}
        />
        <Switch
          size="xs"
          label="Trust tvg-id first"
          description="A stream with the same tvg-id as a channel is that channel, whatever it is called. Off in DispatcharrUtils: providers give one tvg-id to channels that are not the same, such as every CBS station, or East and West."
          checked={!!value.match_tvg_id}
          onChange={(e) => set({ match_tvg_id: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="Same country only"
          description="Only put a stream on a channel of the same country. One that does not say is not a different country. Off in DispatcharrUtils, where the country box in the name already decides it."
          checked={!!value.same_country}
          onChange={(e) => set({ same_country: e.currentTarget.checked })}
        />
        <TextInput
          size="xs"
          label="Words to ignore"
          description="About the stream, not the channel. Comma separated, taken off wherever they are. A quality (HD, FHD, 4K, 1080p…) at the end of a name is always ignored."
          value={value.ignore_tags || ''}
          onChange={(e) => set({ ignore_tags: e.currentTarget.value })}
        />
        <Textarea
          size="xs"
          label="Find and replace"
          description="One per line, as  find => replace  (regular expressions, applied first). For a provider that writes names its own way."
          placeholder={'^AT:\\s* => ┃AT┃ \n\\s*\\(Backup\\) => '}
          autosize
          minRows={2}
          value={rulesText}
          onChange={(e) => {
            setRulesText(e.currentTarget.value);
            set({ regex_rules: textToRules(e.currentTarget.value) });
          }}
        />
        <Textarea
          size="xs"
          label="Other names"
          description="One channel per line, as  Name = another, another. For channels known by more than one name."
          placeholder="National Geographic = NGC, Nat Geo"
          autosize
          minRows={2}
          value={aliasText}
          onChange={(e) => {
            setAliasText(e.currentTarget.value);
            set({ aliases: textToAliases(e.currentTarget.value) });
          }}
        />
      </Section>

      <Section title="Ordering streams">
        <Select
          size="xs"
          label="Put first"
          description="What decides the order the streams are tried in."
          allowDeselect={false}
          value={value.order}
          onChange={(order) => order && set({ order })}
          data={[
            {
              value: 'quality',
              label: 'The best picture, then the preferred provider',
            },
            {
              value: 'provider',
              label:
                'The preferred provider, then the best picture (as DispatcharrUtils)',
            },
          ]}
        />
        <Switch
          size="xs"
          label="Reorder what channels already have"
          description="Put their existing streams in that order too. Off, what is added goes after what is there."
          checked={!!value.reorder_existing}
          onChange={(e) => set({ reorder_existing: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="Leave out SD where there is better"
          checked={!!value.drop_sd_when_hd}
          onChange={(e) => set({ drop_sd_when_hd: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="Skip streams the provider dropped"
          checked={!!value.skip_stale}
          onChange={(e) => set({ skip_stale: e.currentTarget.checked })}
        />
        <Text size="xs" c="dimmed">
          A custom stream on a channel — a fallback such as a &ldquo;could not
          play&rdquo; screen — always stays last, after everything added.
        </Text>
      </Section>

      <Section title="What may change">
        <Switch
          size="xs"
          label="Suggest new channels"
          description="For streams no channel has. Only suggested: nothing is made unless its row is ticked and applied."
          checked={!!value.create_new}
          onChange={(e) => set({ create_new: e.currentTarget.checked })}
        />
        {value.create_new && (
          <Box pl="md">
            <Stack gap={8}>
              <Select
                size="xs"
                label="From which streams"
                allowDeselect={false}
                value={value.new_from || 'followed'}
                onChange={(from) => from && set({ new_from: from })}
                data={[
                  {
                    value: 'followed',
                    label: 'The stream groups your channels come from',
                  },
                  {
                    value: 'all',
                    label: 'Every stream (can be tens of thousands)',
                  },
                ]}
              />
              <Select
                size="xs"
                label="Logo"
                allowDeselect={false}
                value={value.new_logo || 'collections'}
                onChange={(logo) => logo && set({ new_logo: logo })}
                data={[
                  {
                    value: 'collections',
                    label: 'From the logo collections, else the stream',
                  },
                  { value: 'stream', label: "The stream's own" },
                  { value: 'none', label: 'None' },
                ]}
              />
              <Switch
                size="xs"
                label="End each in your fallback stream"
                description="The custom stream most of your channels end in, such as Could Not Dispatch."
                checked={value.new_fallback !== false}
                onChange={(e) => set({ new_fallback: e.currentTarget.checked })}
              />
              <Select
                size="xs"
                label="Into group"
                description="Empty suggests one per channel: where your channels from the same stream group are. It can be changed on each row."
                clearable
                searchable
                value={value.target_group ? String(value.target_group) : null}
                onChange={(group) =>
                  set({ target_group: group ? Number(group) : null })
                }
                data={toOptions(options.all_groups)}
              />
              <Group grow gap="xs">
                <NumberInput
                  size="xs"
                  label="Numbers from"
                  description="Empty is after the last channel of its group."
                  min={1}
                  value={value.number_start ?? ''}
                  onChange={(number) =>
                    set({ number_start: number === '' ? null : Number(number) })
                  }
                />
                <NumberInput
                  size="xs"
                  label="At least"
                  description="Streams needed to make one."
                  min={1}
                  value={value.min_streams_new ?? 1}
                  onChange={(number) =>
                    set({ min_streams_new: Number(number) || 1 })
                  }
                />
              </Group>
              <Select
                size="xs"
                label="Channel profiles"
                allowDeselect={false}
                value={profileValue}
                onChange={(choice) =>
                  set({
                    profiles: choice === 'some' ? [] : choice,
                  })
                }
                data={[
                  { value: 'all', label: 'Every profile, as Dispatcharr does' },
                  { value: 'none', label: 'No profile' },
                  { value: 'some', label: 'The ones I pick' },
                ]}
              />
              {profileValue === 'some' && (
                <MultiSelect
                  size="xs"
                  aria-label="Profiles to join"
                  data={toOptions(options.profiles)}
                  value={asStrings(value.profiles)}
                  onChange={(picked) => set({ profiles: ids(picked) })}
                />
              )}
              <Switch
                size="xs"
                label="Keep the country in the name"
                checked={!!value.keep_country_prefix}
                onChange={(e) =>
                  set({ keep_country_prefix: e.currentTarget.checked })
                }
              />
            </Stack>
          </Box>
        )}
        <Switch
          size="xs"
          color="orange"
          label="Remove streams that are not this channel"
          description="From channels you have, streams whose name is another channel. Never empties a channel, and never touches a provider or group not looked at."
          checked={!!value.replace_streams}
          onChange={(e) => set({ replace_streams: e.currentTarget.checked })}
        />
        {value.replace_streams && (
          <Alert color="orange" p="xs">
            <Text size="xs">
              Removed streams are shown struck through on each channel before
              anything is applied.
            </Text>
          </Alert>
        )}
      </Section>

      <Section title="Guide and logo">
        <Select
          size="xs"
          label="Guide"
          description="For new channels, and channels without one. A channel's own guide is kept."
          allowDeselect={false}
          value={value.epg}
          onChange={(epg) => epg && set({ epg })}
          data={[
            { value: 'tvg_id_then_name', label: 'By tvg-id, then by name' },
            { value: 'tvg_id', label: 'By tvg-id only' },
            { value: 'keep', label: 'Leave it' },
          ]}
        />
        <Select
          size="xs"
          label="Logo"
          description="For new channels, and channels without one."
          allowDeselect={false}
          value={value.logo}
          onChange={(logo) => logo && set({ logo })}
          data={[
            {
              value: 'collections',
              label: 'From the logo collections, then the stream',
            },
            { value: 'stream', label: "The stream's own" },
            { value: 'keep', label: 'Leave it' },
          ]}
        />
      </Section>
    </SimpleGrid>
  );
};

export default ChannelManagerLevers;
