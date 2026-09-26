import React, { useEffect, useState } from 'react';
import { Check, CornerDownRight } from 'lucide-react';
import {
  Badge,
  Box,
  Button,
  Drawer,
  Group,
  Loader,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';

// A suggested new channel that is really one you have -- the matching did not see that
// "AT| ORF EINS" is your "┃AT┃ ORF 1" -- put on that channel instead of made. Found the way
// the stream search finds streams: every word, anywhere, in any order. Nothing is written
// here; the row is applied like any other.

const Candidate = ({ channel, chosen, onChoose }) => (
  <Group gap={6} wrap="nowrap" align="flex-start">
    <Box style={{ minWidth: 0, flex: 1 }}>
      <Text size="xs" style={{ wordBreak: 'break-word' }}>
        {channel.name}
      </Text>
      <Group gap={6} wrap="wrap">
        <Text size="xs" c="dimmed">
          {channel.number != null ? `${channel.number} · ` : ''}
          {channel.group || 'No group'}
        </Text>
        {/* Which providers it has already, so a channel carrying this one is plain */}
        {(channel.providers || []).map((provider) => (
          <Badge key={provider} size="xs" variant="outline" color="gray">
            {provider}
          </Badge>
        ))}
      </Group>
    </Box>
    <Button
      size="compact-xs"
      variant={chosen ? 'filled' : 'light'}
      color={chosen ? 'green' : undefined}
      leftSection={chosen ? <Check size={12} /> : <CornerDownRight size={12} />}
      aria-label={`${chosen ? 'Chosen' : 'Put it on'} ${channel.name}`}
      onClick={() => onChoose(chosen ? null : channel)}
      style={{ flexShrink: 0 }}
    >
      {chosen ? 'Chosen' : 'Put it here'}
    </Button>
  </Group>
);

const FinderBody = ({ row, chosen, onChoose }) => {
  const name = row.channel?.name || '';
  const [search, setSearch] = useState('');
  const [found, setFound] = useState([]);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let current = true;
    const timer = setTimeout(async () => {
      setAsking(true);
      setError(null);
      try {
        // Nothing typed: the channels most like the suggestion, nearly always the one
        const answer = await API.searchChannelManagerChannels({ q: search.trim(), name });
        if (current) setFound(answer?.channels || []);
      } catch (e) {
        if (current) setError(e?.body?.error || 'Could not search your channels.');
      } finally {
        if (current) setAsking(false);
      }
    }, 300);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [search, name]);

  const streams = (row.streams || []).filter((s) => !s.custom);
  return (
    <Stack gap="sm">
      <Text size="xs" c="dimmed">
        {streams.length} stream{streams.length === 1 ? '' : 's'} (
        {streams.map((s) => s.name).join(', ')}) go on the channel chosen, after its own and
        before its fallback, instead of a new channel being made. Applied with the row.
      </Text>
      <TextInput
        size="xs"
        label="Search your channels"
        description="Every word, anywhere in the name, in any order. Empty shows the ones most like this suggestion."
        aria-label="Search your channels"
        value={search}
        onChange={(event) => setSearch(event.currentTarget.value)}
        rightSection={asking ? <Loader size={12} /> : null}
      />
      {error && (
        <Text size="xs" c="red">
          {error}
        </Text>
      )}
      {!asking && found.length === 0 && !error && (
        <Text size="xs" c="dimmed">
          No channel by those words. Fewer words find more.
        </Text>
      )}
      <Stack gap={8}>
        {found.map((channel) => (
          <Candidate
            key={channel.id}
            channel={channel}
            chosen={chosen?.id === channel.id}
            onChoose={onChoose}
          />
        ))}
      </Stack>
    </Stack>
  );
};

const ChannelFinder = ({ row, chosen, onChoose, onClose }) => (
  <Drawer
    opened={!!row}
    onClose={onClose}
    position="right"
    size="lg"
    closeButtonProps={{ 'aria-label': 'Done choosing a channel' }}
    title={row ? `Put ${row.channel?.name || 'this'} on a channel you have` : ''}
  >
    {row && <FinderBody key={row.key} row={row} chosen={chosen} onChoose={onChoose} />}
  </Drawer>
);

export default ChannelFinder;
