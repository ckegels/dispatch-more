// The health tab: a running channel as a card with what it plays, from where and to whom,
// and the parts in the order they are looked at -- running, what happened, stopped.
import { render, screen } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { describe, expect, it } from 'vitest';
import theme from '../../../mantineTheme';
import ChannelHealth from '../ChannelHealth.jsx';

const running = [
  {
    channel: '┃AT┃ ORF 1',
    now: { state: 'active', uptime: 3720, clients: 1, kbps: 5200, speed: 0, source_kbps: 0 },
    samples: [{ kbps: 5000 }, { kbps: 5200 }],
    details: {
      account: 'Provider A',
      profile: 'Default',
      stream_name: 'ORF 1 FHD',
      stream_type: 'ts',
      stream_profile: 'Proxy',
      resolution: '1920x1080',
      video_codec: 'h264',
      source_fps: '50',
      audio_codec: 'aac',
      stream_switch_reason: 'the stream stopped sending',
      viewers: [{ ip: '192.168.2.30', app: 'TiviMate', watching_for: 125, kbps: 5100, format: 'mpegts' }],
    },
  },
];

const draw = (props = {}) =>
  render(
    <MantineProvider theme={theme}>
      <ChannelHealth
        events={[]}
        running={running}
        stopped={[]}
        settings={{ enabled: true }}
        {...props}
      />
    </MantineProvider>
  );

describe('ChannelHealth', () => {
  it('shows what a running channel plays, from where, and who watches it', () => {
    draw();
    expect(screen.getByText('Provider A')).toBeInTheDocument();
    expect(screen.getByText('ORF 1 FHD · TS')).toBeInTheDocument();
    expect(screen.getByText('1920x1080 · H264 · 50 fps')).toBeInTheDocument();
    expect(screen.getByText('5.2 Mbps')).toBeInTheDocument();
    expect(screen.getByText(/the stream stopped sending/)).toBeInTheDocument();
    expect(screen.getByText('192.168.2.30 · TiviMate · for 2m 5s · 5.1 Mbps')).toBeInTheDocument();
  });

  it('leaves the speed out when no ffmpeg is working on the channel', () => {
    draw();
    expect(screen.queryByText(/×$/)).toBeNull();
  });

  it('puts the stopped channels last', () => {
    draw();
    const headings = screen
      .getAllByText(/^(Running now|What happened|Stopped)$/)
      .map((node) => node.textContent);
    expect(headings).toEqual(['Running now', 'What happened', 'Stopped']);
  });
});
