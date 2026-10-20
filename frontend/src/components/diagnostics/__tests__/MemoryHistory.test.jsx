import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import MemoryHistory, { Moment } from '../MemoryHistory.jsx';
import MemoryUse from '../MemoryUse.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getMemoryUse: vi.fn(),
    getMemoryHistory: vi.fn(),
    setMemoryRecorder: vi.fn(),
    getMemoryMoment: vi.fn(),
    downloadMemoryHistory: vi.fn(),
  },
}));

const now = Math.floor(Date.now() / 1000);

const history = (over = {}) => ({
  on: true,
  folder: '/data/memory-history',
  last_sample: now - 20,
  last_sample_age: 20,
  every: 30,
  keep_days: 14,
  days: ['2026-10-20'],
  bytes: 1000,
  samples: 3,
  points: [
    { t: now - 90, used: 2000, container: 2000, limit: 8192, dispatcharr: 900, kinds: {}, largest: ['Web worker (uWSGI)', 300, 100] },
    { t: now - 60, used: 7900, container: 7900, limit: 8192, dispatcharr: 6900, kinds: {}, largest: ['Web worker (uWSGI)', 6000, 100] },
    { t: now - 30, used: 2100, container: 2100, limit: 8192, dispatcharr: 950, kinds: {}, largest: ['Web worker (uWSGI)', 150, 101] },
  ],
  events: [
    { kind: 'stuck', t: now - 60, text: 'Web worker (uWSGI) 100 stuck waiting (state D) at 6000 MB' },
    { kind: 'reboot', t: now - 30, since: now - 600, text: 'The server restarted (no record for 9 min before it)' },
  ],
  ...over,
});

const moment = {
  t: now - 60,
  every: 5,
  sys: { total: 8192, available: 292, swap_total: 1024, swap_free: 0 },
  cg: { current: 7900, max: 8192, anon: 7800, file: 100 },
  redis: { error: 'TimeoutError', ms: 2000 },
  procs: [
    [100, 1, 'Web worker (uWSGI)', 6000, null, 300, 'D', 7, 600.0, now - 3600, 'uwsgi'],
    [101, 1, 'Web worker (uWSGI)', 150, 140, 0, 'S', 7, 1.0, now - 3600, 'uwsgi'],
  ],
  others: [[5, 'kvm', 9000]],
  inflight: {
    100: { at: now - 160, n: 2, req: [['GET', '/api/epg/epgdata/', '192.168.65.7', 95.2], ['GET', '/proxy/ts/stream/x', '192.168.2.110', 600]] },
    101: { at: now - 62, n: 0, req: [] },
  },
};

const renderIn = (ui) => render(<MantineProvider theme={theme}>{ui}</MantineProvider>);

describe('MemoryHistory', () => {
  beforeEach(() => vi.clearAllMocks());

  it('lists what happened, newest first, and opens the moment just before a reboot', async () => {
    API.getMemoryHistory.mockResolvedValue(history());
    API.getMemoryMoment.mockResolvedValue(moment);
    renderIn(<MemoryHistory />);
    await screen.findByText(/stuck waiting \(state D\) at 6000 MB/);
    const rows = screen.getAllByRole('row');
    expect(rows[1].textContent).toMatch(/server restarted/);
    fireEvent.click(screen.getByRole('button', { name: 'Just before' }));
    await waitFor(() => expect(API.getMemoryMoment).toHaveBeenCalledWith(now - 600));
  });

  it('switches the recorder on from the tab', async () => {
    API.getMemoryHistory.mockResolvedValue(history({ on: false, samples: 0, points: [], events: [], days: [], last_sample: null, last_sample_age: null }));
    API.setMemoryRecorder.mockResolvedValue(history());
    renderIn(<MemoryHistory />);
    fireEvent.click(await screen.findByRole('switch'));
    await waitFor(() => expect(API.setMemoryRecorder).toHaveBeenCalledWith(true));
  });

  it('says so when it is on but nothing has been written for minutes', async () => {
    API.getMemoryHistory.mockResolvedValue(history({ last_sample_age: 900 }));
    renderIn(<MemoryHistory />);
    expect(await screen.findByText(/nothing was recorded for 15 min ago/i)).toBeTruthy();
  });

  it('asks for another range', async () => {
    API.getMemoryHistory.mockResolvedValue(history());
    renderIn(<MemoryHistory />);
    await screen.findByText(/3 samples/);
    fireEvent.click(screen.getByText('7 days'));
    await waitFor(() => expect(API.getMemoryHistory).toHaveBeenLastCalledWith('168'));
  });
});

describe('Moment', () => {
  it('shows each process, a stuck one, Redis not answering and what a stuck worker was doing', () => {
    renderIn(<Moment moment={moment} />);
    expect(screen.getByText('stuck (D)')).toBeTruthy();
    expect(screen.getByText(/did not answer \(TimeoutError\)/)).toBeTruthy();
    expect(screen.getByText(/not updated for 100 s \(stuck\?\)/)).toBeTruthy();
    expect(screen.getByText(/95 s GET \/api\/epg\/epgdata\//)).toBeTruthy();
    expect(screen.getByText('nothing')).toBeTruthy();
    expect(screen.getByText(/kvm 5 9000 MB/)).toBeTruthy();
  });
});

describe('MemoryUse with history', () => {
  it('opens on Now and switches to History', async () => {
    vi.clearAllMocks();
    API.getMemoryUse.mockResolvedValue({
      total_mb: 100,
      system: { total_mb: 8000, used_mb: 1000, available_mb: 7000, percent: 12.5 },
      kinds: [],
      processes: [],
    });
    API.getMemoryHistory.mockResolvedValue(history());
    renderIn(<MemoryUse />);
    await screen.findByText(/Dispatcharr holds/);
    expect(API.getMemoryHistory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('History'));
    await waitFor(() => expect(API.getMemoryHistory).toHaveBeenCalledWith('24'));
  });
});
