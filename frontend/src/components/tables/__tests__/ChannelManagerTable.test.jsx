// Drawn with the real table and the real Mantine rather than stand-ins: the table's own
// tick box and row expansion are most of what this page is, and a stand-in cannot tell
// whether they are wired up.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import ChannelManagerTable from '../ChannelManagerTable.jsx';
import API from '../../../api';

const showVideo = vi.fn();
vi.mock('../../../store/useVideoStore', () => ({
  default: (select) => select({ showVideo }),
}));
vi.mock('../../../store/settings', () => ({
  default: (select) => select({ environment: { env_mode: 'prod' } }),
}));

vi.mock('../../../api', () => ({
  default: {
    getChannelManagerOptions: vi.fn(),
    previewChannelManager: vi.fn(),
    applyChannelManager: vi.fn(),
    saveChannelManagerSettings: vi.fn(),
  },
}));

const stream = (id, name, extra = {}) => ({
  id, name, hash: `hash-${id}`, account: 'Provider A', group: '┃AT┃ AUSTRIA', quality: 'HD', probed: false,
  tvg_id: '', logo_url: '', added: false, removed: false, custom: false, in_scope: true, ...extra,
});
const fallback = stream(9, 'could not dispatch', { custom: true, account: 'custom' });

const mergeRow = {
  key: 'ch:1', status: 'merge', adds: 1, removes: 0, changes: [], country: 'at',
  channel: { id: 1, name: '┃AT┃ ORF 1', number: 1, group: '┃AT┃ AUSTRIA', logo_url: '', epg: { name: 'ORF 1', how: 'kept' } },
  before: { channel: { id: 1, name: '┃AT┃ ORF 1', number: 1, logo_url: '' }, streams: [stream(1, '┃AT┃ ORF 1'), fallback] },
  streams: [
    stream(1, '┃AT┃ ORF 1'),
    stream(2, '┃AT┃ ORF 1 FHD', { added: true, quality: 'FHD', account: 'Provider B' }),
    fallback,
  ],
};
const conflictRow = {
  key: 'conflict:at:puls4', status: 'conflict', adds: 0, removes: 0, changes: [], country: 'at',
  channel: null,
  candidates: [
    { id: 3, name: '┃AT┃ PULS 4', number: 3, group: '┃AT┃ AUSTRIA', logo_url: '' },
    { id: 4, name: '┃AT┃ PULS 4 HD', number: 4, group: '┃AT┃ AUSTRIA', logo_url: '' },
  ],
  before: { channel: null, streams: [stream(5, '┃AT┃ PULS 4 FHD')] },
  streams: [],
};
const plan = {
  summary: { streams: 4, merge: 1, new: 0, conflict: 1, unchanged: 0, streams_added: 1 },
  rows: [mergeRow, conflictRow],
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <ChannelManagerTable />
    </MantineProvider>
  );

const rowOf = (text) => screen.getAllByText(text)[0].closest('.tr');

describe('ChannelManagerTable', () => {
  beforeEach(() => {
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts: [], stream_groups: [],
      channel_groups: [], all_groups: [], profiles: [],
    });
    API.previewChannelManager.mockResolvedValue(plan);
    API.saveChannelManagerSettings.mockResolvedValue({});
    API.applyChannelManager.mockResolvedValue({ created: 0, updated: 1, streams_added: 1 });
  });

  afterEach(() => vi.clearAllMocks());

  it('shows each channel as it is and as it would be, and what it comes to', async () => {
    draw();
    expect((await screen.findAllByText('┃AT┃ ORF 1')).length).toBeGreaterThan(0);
    expect(screen.getByText('Guide: ORF 1')).toBeInTheDocument();
    expect(screen.getByText(/1 channels gain 1 streams/)).toBeInTheDocument();
    expect(screen.getByText(/1 conflicts/)).toBeInTheDocument();
  });

  it('opens a row to every stream before and after, the fallback last', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));

    expect(await screen.findByText('+ ┃AT┃ ORF 1 FHD')).toBeInTheDocument();
    expect(screen.getAllByText('fallback').length).toBeGreaterThan(0);
  });

  it('plays any stream, to see whether it really is the same channel', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));

    fireEvent.click(await screen.findByLabelText('Watch ┃AT┃ ORF 1 FHD'));

    expect(showVideo).toHaveBeenCalledWith(
      expect.stringContaining('/proxy/ts/stream/hash-2'),
      'live',
      { name: '┃AT┃ ORF 1 FHD' }
    );
  });

  it('applies only the channels ticked, after asking', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('input[type=checkbox]'));
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith({ order: 'quality' }, ['ch:1'])
    );
  });

  it('never counts a conflict as something to apply', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('Could be several').querySelector('input[type=checkbox]'));
    expect(screen.getByRole('button', { name: /^Apply/ })).toBeDisabled();
  });

  it('lists the channels a conflict could be, when it is opened', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('Could be several').querySelector('.td:nth-child(2) > div > div'));
    expect(await screen.findByText('┃AT┃ PULS 4 HD')).toBeInTheDocument();
    expect(screen.getByText(/nothing is done with them/)).toBeInTheDocument();
  });

  it('asks for a fresh preview before applying after the levers move', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('input[type=checkbox]'));
    fireEvent.click(screen.getByRole('button', { name: 'Levers' }));
    fireEvent.click(screen.getByRole('switch', { name: /Make new channels/ }));

    expect(await screen.findByText(/The levers have changed/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Apply/ })).toBeDisabled();
  });

  it('goes back to the defaults, keeping what is looked at', async () => {
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality', match_tvg_id: true, channel_groups: [7] },
      defaults: { order: 'provider', match_tvg_id: false, channel_groups: [] },
      accounts: [], stream_groups: [], channel_groups: [], all_groups: [], profiles: [],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Levers' }));
    expect(screen.getByRole('switch', { name: /Trust tvg-id first/ })).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: /Back to the defaults/ }));
    expect(screen.getByRole('switch', { name: /Trust tvg-id first/ })).not.toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: /Preview/ }));
    await waitFor(() =>
      expect(API.previewChannelManager).toHaveBeenLastCalledWith(
        expect.objectContaining({ order: 'provider', match_tvg_id: false, channel_groups: [7] })
      )
    );
  });
});
