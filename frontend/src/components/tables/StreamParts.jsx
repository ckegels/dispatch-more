import React from 'react';
import { CirclePlay } from 'lucide-react';
import { ActionIcon, Center, Image, Text, Tooltip } from '@mantine/core';
import useVideoStore from '../../store/useVideoStore';
import useSettingsStore from '../../store/settings';
import { buildLiveStreamUrl } from '../../utils/components/FloatingVideoUtils.js';

// Shared by the Channel Manager's tabs: a channel's logo as the Logo Manager shows it, and
// a button to watch a stream.

export const Logo = ({ url, name }) => (
  <Center style={{ width: 40, flexShrink: 0 }}>
    {url ? (
      <Image
        src={url}
        alt={name || ''}
        w={40}
        h={30}
        fit="contain"
        fallbackSrc="/logo.png"
        style={{ transition: 'transform 0.3s ease', cursor: 'pointer' }}
        onMouseEnter={(e) => {
          e.target.style.transform = 'scale(1.5)';
        }}
        onMouseLeave={(e) => {
          e.target.style.transform = 'scale(1)';
        }}
      />
    ) : (
      <Text size="xs" c="dimmed">
        —
      </Text>
    )}
  </Center>
);

export // Plays a stream in the preview player the Streams table uses, so two streams said to be
// the same channel can be looked at rather than taken on trust
const Watch = ({ stream }) => {
  const showVideo = useVideoStore((s) => s.showVideo);
  const envMode = useSettingsStore((s) => s.environment?.env_mode);
  if (!stream.hash) return null;
  return (
    <Tooltip label="Watch this stream">
      <ActionIcon
        size="xs"
        variant="subtle"
        color="blue"
        aria-label={`Watch ${stream.name}`}
        onClick={(event) => {
          event.stopPropagation();
          let url = buildLiveStreamUrl(`/proxy/ts/stream/${stream.hash}`);
          if (envMode === 'dev') {
            url = `${window.location.protocol}//${window.location.hostname}:5656${url}`;
          }
          showVideo(url, 'live', { name: stream.name });
        }}
      >
        <CirclePlay size={14} />
      </ActionIcon>
    </Tooltip>
  );
};
