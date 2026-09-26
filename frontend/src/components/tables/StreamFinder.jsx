import React, { useEffect, useState } from 'react';
import { Check, Plus } from 'lucide-react';
import {
  Badge,
  Box,
  Button,
  Drawer,
  Group,
  Loader,
  Stack,
  Switch,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import { Watch } from './StreamParts';

// Finding the stream the matching could not, and putting it on a channel by hand: a third
// login that writes the name its own way ("AT: ORF1 HD" where the others say "┃AT┃ ORF 1"),
// or a channel the name tells nothing about. Nothing is written here; a stream put on
// joins the row like any other, and is applied with it.

// What a provider writes around a channel's name rather than the name itself: the country
// box, a country prefix, the quality. Left in, "┃AT┃" would have to be in every stream's
// name word for word, and a provider that writes "AT:" would find nothing.
const searchWords = (name) =>
  (name || '')
    .replace(/┃[^┃]*┃/g, ' ')
    .replace(/^\s*[A-Za-z]{2,3}\s*[:|]\s*/, ' ')
    .replace(
      /\b(4K|UHD|FHD|HD|SD|HEVC|H\.?26[45]|1080[pi]?|720p|50fps|60fps)\b/gi,
      ' '
    )
    .replace(/\s+/g, ' ')
    .trim();

const CHECK = {
  ok: { label: 'plays', color: 'green' },
  failing: { label: 'failing', color: 'yellow' },
  suspect: { label: 'picture in doubt', color: 'yellow' },
  broken: { label: 'broken', color: 'red' },
  unchecked: { label: 'not checked', color: 'gray' },
};

const QUALITY_COLOR = { '4K': 'grape', FHD: 'teal', HD: 'blue', SD: 'gray' };

const Found = ({ stream, added, onToggle }) => {
  const check = stream.check && CHECK[stream.check.state];
  return (
    <Group gap={6} wrap="nowrap" align="flex-start">
      <Watch stream={stream} />
      <Badge
        size="xs"
        variant="light"
        color={QUALITY_COLOR[stream.quality] || 'dark'}
        style={{ flexShrink: 0 }}
      >
        {stream.quality || '?'}
      </Badge>
      <Box style={{ minWidth: 0, flex: 1 }}>
        <Text size="xs" style={{ wordBreak: 'break-word' }}>
          {stream.name}
        </Text>
        <Group gap={6} wrap="wrap">
          <Badge size="xs" variant="outline" color="gray">
            {stream.account}
          </Badge>
          {stream.group && (
            <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>
              {stream.group}
            </Text>
          )}
          {check && (
            <Badge size="xs" variant="light" color={check.color}>
              {check.label}
            </Badge>
          )}
          {stream.stale && (
            <Badge size="xs" variant="light" color="orange">
              dropped by the provider
            </Badge>
          )}
        </Group>
        {/* Where it is already: the same stream on another channel is most often the
            sign that it is a different channel of a similar name */}
        {stream.channels?.length > 0 && (
          <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>
            Already on{' '}
            {stream.channels
              .slice(0, 3)
              .map(
                (c) => `${c.name}${c.number != null ? ` (${c.number})` : ''}`
              )
              .join(', ')}
            {stream.channels.length > 3
              ? ` and ${stream.channels.length - 3} more`
              : ''}
          </Text>
        )}
      </Box>
      <Button
        size="compact-xs"
        variant={added ? 'filled' : 'light'}
        color={added ? 'green' : undefined}
        leftSection={added ? <Check size={12} /> : <Plus size={12} />}
        aria-label={`${added ? 'Take off' : 'Put on'} ${stream.name}`}
        onClick={() => onToggle(stream)}
        style={{ flexShrink: 0 }}
      >
        {added ? 'On' : 'Put on'}
      </Button>
    </Group>
  );
};

// Mounted afresh for each row opened, so the search starts from that channel's name
const FinderBody = ({ row, providers, onAdd, onRemove }) => {
  const channel = row.channel;
  const have = new Set(
    (row?.streams || [])
      .filter((s) => !s.custom && !s.removed && !s.dropped)
      .map((s) => s.account_id ?? s.account)
  );
  const missing = providers.filter((p) => !have.has(p.id));
  const [search, setSearch] = useState(searchWords(channel.name));
  const [onlyMissing, setOnlyMissing] = useState(missing.length > 0);
  // Only what a provider carries that no channel has yet; then nothing needs typing
  const [onlyUnassigned, setOnlyUnassigned] = useState(false);
  const [found, setFound] = useState([]);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState(null);

  const onRow = new Set((row?.streams || []).map((s) => s.id));
  const byHand = new Set(
    (row?.streams || []).filter((s) => s.byHand).map((s) => s.id)
  );
  // What the row had before anything was put on it by hand, so taking one off again
  // brings it back to the list
  const leaveOut = (row?.streams || [])
    .filter((s) => !s.byHand)
    .map((s) => s.id);
  const missingIds = missing.map((p) => p.id).join(',');
  const leaveOutIds = leaveOut.join(',');

  const channelName = channel.name;
  useEffect(() => {
    const words = search.trim();
    if (!words && !onlyUnassigned) {
      setFound([]);
      return undefined;
    }
    let current = true;
    // Asked for once typing stops, not on every key
    const timer = setTimeout(async () => {
      setAsking(true);
      setError(null);
      try {
        const answer = await API.searchChannelManagerStreams({
          q: words,
          accounts: onlyMissing && missingIds ? missingIds.split(',') : [],
          leave_out: leaveOutIds ? leaveOutIds.split(',') : [],
          name: channelName,
          unassigned: onlyUnassigned,
        });
        if (current) setFound(answer?.streams || []);
      } catch (e) {
        if (current)
          setError(e?.body?.error || 'Could not search the streams.');
      } finally {
        if (current) setAsking(false);
      }
    }, 300);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [channelName, search, onlyMissing, missingIds, leaveOutIds, onlyUnassigned]);

  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        {providers.length - missing.length} of {providers.length} provider
        {providers.length === 1 ? '' : 's'}
        {missing.length > 0
          ? ` · missing ${missing.map((p) => p.name).join(', ')}`
          : ''}
        . What is put on goes before the fallback, and is applied with the row.
      </Text>
      <TextInput
        size="xs"
        label="Search"
        description="Every word, anywhere in the name, in any order."
        aria-label="Search streams"
        value={search}
        onChange={(event) => setSearch(event.currentTarget.value)}
        rightSection={asking ? <Loader size={12} /> : null}
      />
      {missing.length > 0 && (
        <Switch
          size="xs"
          label={`Only the missing provider${missing.length === 1 ? '' : 's'}`}
          checked={onlyMissing}
          onChange={(event) => setOnlyMissing(event.currentTarget.checked)}
        />
      )}
      <Switch
        size="xs"
        label="Only streams on no channel"
        description="What a provider carries that is nowhere in your lineup yet. Nothing needs typing: empty the search to see them all."
        checked={onlyUnassigned}
        onChange={(event) => setOnlyUnassigned(event.currentTarget.checked)}
      />
      {error && (
        <Text size="xs" c="red">
          {error}
        </Text>
      )}
      {!asking && (search.trim() || onlyUnassigned) && found.length === 0 && !error && (
        <Text size="xs" c="dimmed">
          Nothing by those words
          {onlyMissing && missing.length ? ' from the missing providers' : ''}.
          Fewer words find more.
        </Text>
      )}
      <Stack gap={8}>
        {found
          .filter((s) => !onRow.has(s.id) || byHand.has(s.id))
          .map((stream) => (
            <Found
              key={stream.id}
              stream={stream}
              added={byHand.has(stream.id)}
              onToggle={(s) => (byHand.has(s.id) ? onRemove(s.id) : onAdd(s))}
            />
          ))}
      </Stack>
    </Stack>
  );
};

const StreamFinder = ({ row, providers, onAdd, onRemove, onClose }) => (
  <Drawer
    opened={!!row?.channel}
    onClose={onClose}
    position="right"
    size="lg"
    closeButtonProps={{ 'aria-label': 'Done putting streams on' }}
    title={row?.channel ? `Put a stream on ${row.channel.name}` : ''}
  >
    {row?.channel && (
      <FinderBody
        key={row.key}
        row={row}
        providers={providers}
        onAdd={onAdd}
        onRemove={onRemove}
      />
    )}
  </Drawer>
);

export default StreamFinder;
