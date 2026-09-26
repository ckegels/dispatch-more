import React, { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Group,
  MultiSelect,
  NumberInput,
  Select,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import Section from './SettingsSection';

// The levers, in the order a run is thought through: what to look at, how a channel is
// recognised, how its streams are ordered, what may be changed, and where its guide and
// logo come from. Every one says what it does, because most are only touched once --
// which is also why they open a section at a time, the way Stream Check's settings do.

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

// resetKey changes when the levers are put back to their defaults: the two text boxes
// are then shown as reset, and the sections stay as open as they were
const ChannelManagerLevers = ({ options, value, onChange, resetKey = 0 }) => {
  const set = (changes) => onChange({ ...value, ...changes });
  // Kept as typed until it is valid, so a half-typed line is not thrown away
  const [rulesText, setRulesText] = useState(rulesToText(value.regex_rules));
  const [aliasText, setAliasText] = useState(aliasesToText(value.aliases));
  const lastReset = useRef(resetKey);
  useEffect(() => {
    if (lastReset.current === resetKey) return;
    lastReset.current = resetKey;
    setRulesText(rulesToText(value.regex_rules));
    setAliasText(aliasesToText(value.aliases));
  }, [resetKey, value.regex_rules, value.aliases]);

  const profileValue = ['all', 'none', 'like_its_group'].includes(
    value.profiles
  )
    ? value.profiles
    : 'some';

  return (
    <Stack gap="xs">
      <Section
        title="What to look at"
        about="which providers, stream groups and channel groups"
        openAtFirst
      >
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
          description={
            "Which of the providers' groups to take streams from. None is every one " +
            'for your channels; new channels then only come from the groups your ' +
            'channels already use (see "From which streams"). A group picked here ' +
            'gives new channels too.'
          }
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
        <MultiSelect
          size="xs"
          label="Channel groups to leave alone"
          description="Left out of all of it: not given streams, not combined, and never a home for a new channel. For the groups something else looks after."
          data={toOptions(options.all_groups)}
          value={asStrings(value.exclude_channel_groups)}
          onChange={(picked) => set({ exclude_channel_groups: ids(picked) })}
          searchable
          clearable
        />
      </Section>

      <Section
        title="Recognising a channel"
        about="how a stream is known to be one of your channels"
      >
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
          label="Combine channels that are the same channel"
          description="Where you have one channel twice — the same channel in two of your groups — the streams of all of them go on the one kept and the rest are deleted. The one kept is the lowest-numbered already in the group suggested for it, and the group can be chosen on the row. Channels of two different countries are never combined, whatever names they share. Off by default: this is the only thing here that deletes a channel, and a deleted channel is gone until a backup is restored."
          checked={!!value.combine_duplicates}
          onChange={(e) => set({ combine_duplicates: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="Trust tvg-id first"
          description="A stream whose name finds none of your channels is the channel with its tvg-id — unless the names say otherwise: another call sign, network, station, town, country, East or West, or names nothing alike. Not an id the provider gives to several of its own channels (every ORF 2 region is orf2.at), nor one that points at several of yours. Off in DispatcharrUtils: providers give one tvg-id to channels that are not the same."
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
        <Switch
          size="xs"
          label="The country however it is written"
          description="┃AT┃, AT|, AT:, [AT] and ┃AUT┃ are one country, and ┃USA┃ is US| — for a provider that writes the country its own way and so matches nothing by name. Only where the letters are a country, so a package such as GO: or VIP| is left alone. Names stay as they are written. Off in DispatcharrUtils, which compares the name as written."
          checked={!!value.country_any_way}
          onChange={(e) => set({ country_any_way: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="American local stations"
          description="When a name matches nothing: by call sign (ABC 10 | ALBANY | WTEN and US| ABC 10 (WTEN) ALBANY), or by network, number and town where one side has no call sign (NBC 46 | SIOUX FALLS IA and US| NBC 46 (KDLT) SIOUX FALLS). Both must say the same network; a subchannel (WLOX-DT2) is a station of its own, and Wichita is not Wichita Falls. Off in DispatcharrUtils, which only compares names."
          checked={!!value.match_call_signs}
          onChange={(e) => set({ match_call_signs: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="A feed that does not say is the East one"
          description="FYI HD is FYI HD [EAST], as American playlists write it: the West feed always says so."
          checked={!!value.east_is_default}
          onChange={(e) => set({ east_is_default: e.currentTarget.checked })}
        />
        <Switch
          size="xs"
          label="Leave out words like TV, Channel and Network"
          description="PARAMOUNT NETWORK is Paramount, LAFF TV is Laff, WDR FERNSEHEN is WDR — only where that still names exactly one of your channels, so a word that tells two apart is never left out."
          checked={!!value.leave_out_filler}
          onChange={(e) => set({ leave_out_filler: e.currentTarget.checked })}
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

      <Section
        title="A channel's streams"
        about="the order they are tried in, and which are left out or taken off"
      >
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
        <Text size="xs" c="dimmed">
          A custom stream on a channel — a fallback such as a &ldquo;could not
          play&rdquo; screen — always stays last, after everything added.
        </Text>
      </Section>

      <Section
        title="New channels"
        about="whether streams no channel has become channels, and where they go"
      >
        <Switch
          size="xs"
          label="Suggest new channels"
          description="For streams no channel has. Only suggested: nothing is made unless its row is ticked and applied."
          checked={!!value.create_new}
          onChange={(e) => set({ create_new: e.currentTarget.checked })}
        />
        {value.create_new && (
          <>
            <Select
              size="xs"
              label="From which streams"
              description={
                value.stream_groups?.length
                  ? 'The stream groups picked above.'
                  : 'With no stream groups picked above. A group no channel uses ' +
                    'yet -- one a provider just added -- gives no new channels ' +
                    'until it is picked, or this says every stream.'
              }
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
            <MultiSelect
              size="xs"
              label="Groups to choose between"
              description="Which groups the picker on a row offers. Every group there is runs to hundreds, nearly all of them a provider's own names that no channel of yours is in, so by default it offers the ones you have channels in and the ones with nothing in them — the ones you made yourself. A group you make here is always offered."
              value={value.group_choices || ['with_channels', 'empty']}
              onChange={(kinds) =>
                set({ group_choices: kinds.length ? kinds : ['with_channels'] })
              }
              data={[
                {
                  value: 'with_channels',
                  label: 'Groups you have channels in',
                },
                { value: 'empty', label: 'Empty groups (ones you made)' },
                {
                  value: 'active_m3u',
                  label: "A provider's groups, from a playlist switched on",
                },
                {
                  value: 'inactive_m3u',
                  label: "A provider's groups, from a playlist switched off",
                },
              ]}
              clearable={false}
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
                {
                  value: 'like_its_group',
                  label: 'The ones its group is in',
                },
                { value: 'all', label: 'Every profile, as Dispatcharr does' },
                { value: 'none', label: 'No profile' },
                { value: 'some', label: 'The ones I pick' },
              ]}
            />
            {/* A new channel goes where the rest of its group is watched, and not into
                  every profile -- which put the sports channels in the kids' profile */}
            {profileValue === 'like_its_group' && (
              <NumberInput
                size="xs"
                label="Where its group has more than"
                description="Channels of the group it is made in, switched on in that profile."
                min={0}
                value={value.profiles_group_more_than ?? 10}
                onChange={(number) =>
                  set({
                    profiles_group_more_than:
                      number === '' ? 10 : Math.max(0, Number(number) || 0),
                  })
                }
              />
            )}
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
          </>
        )}
      </Section>

      <Section
        title="Guide and logo"
        about="where a channel's guide and logo come from"
      >
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
    </Stack>
  );
};

export default ChannelManagerLevers;
