// Drawn with the real table and the real Mantine, as the Merge tab's tests are: opening a
// row and the buttons inside it are most of what this tab is.
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import StreamCheckTable from '../StreamCheckTable.jsx';
import API from '../../../api';

vi.mock('../../../store/useVideoStore', () => ({
  default: (select) => select({ showVideo: vi.fn() }),
}));
vi.mock('../../../store/settings', () => ({
  default: (select) => select({ environment: { env_mode: 'prod' } }),
}));

vi.mock('../../../api', () => ({
  default: {
    getStreamCheck: vi.fn(),
    runStreamCheck: vi.fn(),
    stopStreamCheck: vi.fn(),
    saveStreamCheckSettings: vi.fn(),
    streamCheckAction: vi.fn(),
    clearStreamCheck: vi.fn(),
  },
}));

const result = (ok, extra = {}) => ({
  ok,
  reason: ok ? '' : 'The provider answered HTTP 404',
  resolution: ok ? '1920x1080' : '',
  codec: ok ? 'h264' : '',
  checked_at: '2026-09-18T22:00:00+00:00',
  history: ok ? [1] : [0, 0],
  ...extra,
});

const channelRow = {
  key: 'ch:1',
  kind: 'channel',
  channel: { id: 1, name: '┃AT┃ ORF 1', number: 1, group: '┃AT┃ AUSTRIA', logo_url: '' },
  streams: [
    { id: 11, name: 'ORF 1 A', account: 'Provider A', custom: false, hash: 'h11', state: 'ok', result: result(true) },
    { id: 12, name: 'ORF 1 B', account: 'Provider B', custom: false, hash: 'h12', state: 'broken', result: result(false) },
    { id: 9, name: 'could not dispatch', account: 'custom', custom: true, hash: 'h9', state: 'fallback', result: null },
  ],
  broken: 1,
  failing: 0,
  working: 1,
  dead: false,
};

const parkedRow = {
  id: 13,
  name: 'ORF 1 C',
  account: 'Provider A',
  custom: false,
  hash: 'h13',
  state: 'broken',
  result: result(false),
  parked_at: '2026-09-17T22:00:00+00:00',
  from: [{ id: 1, name: '┃AT┃ ORF 1', order: 2 }],
};

const overview = (extra = {}) => ({
  rows: [channelRow],
  parked: [parkedRow],
  settings: {
    enabled: false, every_hours: 24, window_from: '', window_to: '', timeout_seconds: 12,
    gap_seconds: 1, broken_after: 2, channel_groups: [], restore_recovered: false,
  },
  progress: {},
  running: false,
  last_run: { finished_at: '2026-09-18T22:30:00+00:00', checked: 3, total: 3 },
  ffprobe: true,
  channel_groups: [{ id: 1, name: '┃AT┃ AUSTRIA' }],
  ...extra,
});

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <StreamCheckTable />
    </MantineProvider>
  );

const open = async () => {
  const name = await screen.findByText('┃AT┃ ORF 1');
  fireEvent.click(name.closest('.tr').querySelector('.td:nth-child(1) > div > div'));
  await screen.findByText('ORF 1 B');
};

describe('StreamCheckTable', () => {
  beforeEach(() => {
    // Mantine's Select scrolls to the option picked, which jsdom cannot do
    Element.prototype.scrollIntoView = vi.fn();
    API.getStreamCheck.mockResolvedValue(overview());
    API.runStreamCheck.mockResolvedValue({ started: true });
    API.stopStreamCheck.mockResolvedValue({ stopping: true });
    API.saveStreamCheckSettings.mockResolvedValue({});
    API.streamCheckAction.mockResolvedValue({ done: 'park', channels: 1 });
  });

  afterEach(() => vi.clearAllMocks());

  it('shows each channel with a stream that does not play, and why', async () => {
    draw();
    expect(await screen.findByText('1 broken')).toBeInTheDocument();
    await open();
    expect(screen.getByText(/The provider answered HTTP 404/)).toBeInTheDocument();
    expect(screen.getByText(/1920x1080 · h264/)).toBeInTheDocument();
  });

  it('parks a stream without asking, since it can be put back', async () => {
    draw();
    await open();
    fireEvent.click(screen.getAllByRole('button', { name: 'Park' })[1]);
    await waitFor(() => expect(API.streamCheckAction).toHaveBeenCalledWith('park', 12, null));
  });

  it('asks before removing, and removes from that channel only', async () => {
    draw();
    await open();
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[1]);
    expect(API.streamCheckAction).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/comes off "┃AT┃ ORF 1" for good/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(API.streamCheckAction).toHaveBeenCalledWith('remove', 12, 1));
  });

  it('never offers to park or remove the fallback', async () => {
    draw();
    await open();
    // Two real streams, two of each button: none for the fallback
    expect(screen.getAllByRole('button', { name: 'Park' })).toHaveLength(2);
  });

  it('lists parked streams with where they came from, and puts one back', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('textbox', { name: 'Which channels' }));
    fireEvent.click(await screen.findByText('Parked (1)'));

    expect(await screen.findByText('ORF 1 C')).toBeInTheDocument();
    expect(screen.getByText(/from ┃AT┃ ORF 1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Put back' }));
    await waitFor(() => expect(API.streamCheckAction).toHaveBeenCalledWith('restore', 13, null));
  });

  it('starts a check, and while one runs offers to stop it', async () => {
    draw();
    fireEvent.click(await screen.findByRole('button', { name: /Check all now/ }));
    await waitFor(() => expect(API.runStreamCheck).toHaveBeenCalledWith());

    API.getStreamCheck.mockResolvedValue(
      overview({
        running: true,
        progress: { done: 1, total: 3, waiting: true, message: 'Waiting for viewers to finish', accounts: {} },
      })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    expect(await screen.findByText(/Checking: 1 of 3 streams · paused: Waiting for viewers to finish/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Stop/ }));
    await waitFor(() => expect(API.stopStreamCheck).toHaveBeenCalled());
  });

  it('saves the settings', async () => {
    draw();
    fireEvent.click(await screen.findByRole('button', { name: 'Settings' }));
    fireEvent.click(screen.getByRole('switch', { name: /Check streams by itself/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
    await waitFor(() =>
      expect(API.saveStreamCheckSettings).toHaveBeenCalledWith(
        expect.objectContaining({ enabled: true, every_hours: 24 })
      )
    );
  });

  it('shows how each provider is getting on, and which one is in use', async () => {
    API.getStreamCheck.mockResolvedValue(
      overview({
        running: true,
        progress: {
          done: 4, total: 9, accounts: {
            1: { name: 'Provider A', done: 4, left: 2, status: 'checking', now: 'ORF 2', reason: '' },
            2: { name: 'Provider B', done: 0, left: 3, status: 'in use', reason: 'a viewer is on every login of it' },
          },
        },
      })
    );
    draw();
    expect(await screen.findByText(/Provider A: 4 checked, 2 left · ORF 2/)).toBeInTheDocument();
    expect(screen.getByText(/Provider B: 0 checked, 3 left · in use \(a viewer is on every login of it\)/)).toBeInTheDocument();
  });

  it('says which providers the last run left alone, and that their streams were not counted', async () => {
    API.getStreamCheck.mockResolvedValue(
      overview({
        last_run: {
          finished_at: '2026-09-18T22:30:00+00:00', checked: 3, total: 5,
          unavailable: [{ name: 'Provider C', reason: 'the provider refused the login' }],
        },
      })
    );
    draw();
    expect(await screen.findByText(/Provider C: the provider refused the login/)).toBeInTheDocument();
    expect(screen.getByText(/not counted as failing/)).toBeInTheDocument();
  });

  it('by default checks beside viewers but not their providers, and can forget wrong results', async () => {
    API.clearStreamCheck.mockResolvedValue({ cleared: true });
    draw();
    expect(await screen.findByText(/never on a provider someone is using/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    expect(screen.getByRole('switch', { name: /Only while nothing is playing/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Forget all results' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Forget them' }));
    await waitFor(() => expect(API.clearStreamCheck).toHaveBeenCalled());
  });

  it('says a stream the provider refused was not checked, rather than that it plays', async () => {
    const refused = {
      ...channelRow,
      streams: [
        {
          ...channelRow.streams[0],
          state: 'unchecked',
          result: {
            ok: true, skipped: true, refused: 'The provider answered HTTP 407: Proxy Authentication Required',
            checked_at: '2026-09-19T01:00:00+00:00', history: [],
          },
        },
      ],
    };
    API.getStreamCheck.mockResolvedValue(overview({ rows: [refused] }));
    draw();
    const name = await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(name.closest('.tr').querySelector('.td:nth-child(1) > div > div'));
    expect(await screen.findByText(/Not checked: The provider answered HTTP 407/)).toBeInTheDocument();
    expect(screen.queryByText(/^plays/)).toBeNull();
  });

  it('says so when ffprobe is missing', async () => {
    API.getStreamCheck.mockResolvedValue(overview({ ffprobe: false }));
    draw();
    expect(await screen.findByText(/ffprobe is not installed/)).toBeInTheDocument();
  });
});
