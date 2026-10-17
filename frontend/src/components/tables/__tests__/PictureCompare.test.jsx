// Comparing a channel's streams picture against picture: what was found, and starting it.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import PictureCompare, { sessionText } from '../PictureCompare.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getPictureCompare: vi.fn(),
    startPictureCompare: vi.fn(),
  },
}));

const channel = { id: 7, name: '┃FR┃ MTV' };
const found = {
  state: 'done',
  started: 1792260000,
  reference: { id: 1, name: '┃FR┃ MTV HD', account: 'TiviBridge2' },
  results: [
    {
      id: 2,
      name: '┃FR┃ MTV FHD',
      account: 'TiviBridge',
      same_provider: true,
      verdict: 'same',
      why: '120 of 128 changing moments match, 4 s behind',
      offset: 4,
      share: 0.94,
      clear: 0.8,
      sound: 0.97,
      moving: 140,
      thumb_ref: 'AAAA',
      thumb: 'BBBB',
    },
    {
      id: 3,
      name: 'FR| MTV HD',
      account: 'Digitalizard.com',
      same_provider: false,
      verdict: 'different',
      why: 'At best 20 of 70 changing moments match',
      offset: -30,
      share: 0.29,
      clear: 0.03,
      sound: 0.05,
      moving: 140,
    },
  ],
  error: '',
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <PictureCompare channel={channel} onClose={() => {}} />
    </MantineProvider>
  );

describe('PictureCompare', () => {
  afterEach(() => vi.clearAllMocks());

  it('shows what the last comparison found, stream by stream', async () => {
    API.getPictureCompare.mockResolvedValue({ result: found });
    draw();
    // Named over its row and under its picture
    expect(await screen.findAllByText('┃FR┃ MTV FHD')).toHaveLength(2);
    expect(screen.getByText('Same')).toBeInTheDocument();
    expect(screen.getByText('Different')).toBeInTheDocument();
    expect(screen.getByText(/4 s behind/)).toBeInTheDocument();
    expect(screen.getByText(/TiviBridge · same provider/)).toBeInTheDocument();
    expect(
      screen.getByText(/Best match 29% of 140 changing moments/)
    ).toBeInTheDocument();
    // The two pictures side by side, to see for yourself
    expect(screen.getAllByRole('img')).toHaveLength(2);
    expect(
      screen.getByRole('button', { name: 'Compare again' })
    ).toBeInTheDocument();
  });

  it('starts one and says how it is going', async () => {
    API.getPictureCompare
      .mockResolvedValueOnce({ result: null })
      .mockResolvedValue({
        result: { state: 'running', session: 1, sessions: 2, results: [] },
      });
    API.startPictureCompare.mockResolvedValue({ started: true });
    draw();
    fireEvent.click(await screen.findByRole('button', { name: 'Compare now' }));
    await waitFor(() =>
      expect(API.startPictureCompare).toHaveBeenCalledWith(7)
    );
    expect(
      await screen.findByText(/session 1 of 2 \(about 4 min in all\)/)
    ).toBeInTheDocument();
  });

  it('says why it could not start', async () => {
    API.getPictureCompare.mockResolvedValue({ result: null });
    API.startPictureCompare.mockRejectedValue({
      body: { error: 'Another channel is being compared. One at a time.' },
    });
    draw();
    fireEvent.click(await screen.findByRole('button', { name: 'Compare now' }));
    expect(await screen.findByText(/One at a time/)).toBeInTheDocument();
  });

  it('puts the progress into words', () => {
    expect(sessionText({ state: 'queued' })).toBe('Starting…');
    expect(sessionText({ state: 'done' })).toBe('');
  });
});
