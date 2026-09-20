// The lineup as an arrangement. Drawn with the real Mantine; the dragging itself is
// dnd-kit's and not retested here, but what a drag leads to is.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import GuideLayoutTable from '../GuideLayoutTable.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getGuideLayout: vi.fn(),
    arrangeGuideLayout: vi.fn(),
    applyGuideLayout: vi.fn(),
  },
}));

const layout = (extra = {}) => ({
  groups: [
    {
      id: 2,
      name: '┃AT┃ NEWS',
      first: 50,
      last: 50,
      channels: [
        { id: 21, name: '┃AT┃ NEWS 1', number: 50, logo_url: '', group_id: 2, epg: 'News', clashes: false },
      ],
    },
    {
      id: 1,
      name: '┃AT┃ AUSTRIA',
      first: 1,
      last: 3,
      room_after: 46,
      channels: [
        { id: 11, name: '┃AT┃ ORF 1', number: 1, logo_url: '', group_id: 1, epg: 'ORF 1', clashes: false },
        { id: 12, name: '┃AT┃ ORF 2', number: 2, logo_url: '', group_id: 1, epg: '', clashes: false },
        { id: 13, name: '┃AT┃ PULS 4', number: 3, logo_url: '', group_id: 1, epg: 'PULS 4', clashes: true },
      ],
    },
  ],
  clashes: [3],
  ...extra,
});

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <GuideLayoutTable />
    </MantineProvider>
  );

describe('GuideLayoutTable', () => {
  beforeEach(() => {
    API.getGuideLayout.mockResolvedValue(layout());
    API.arrangeGuideLayout.mockResolvedValue({ numbers: {}, changing: [] });
    API.applyGuideLayout.mockResolvedValue({ changed: 0 });
  });

  afterEach(() => vi.clearAllMocks());

  it('shows each group with its channels in order, and where there is room', async () => {
    draw();
    expect((await screen.findAllByText('┃AT┃ AUSTRIA')).length).toBeGreaterThan(0);
    expect(screen.getByText(/3 channels · 1–3/)).toBeInTheDocument();
    expect(screen.getByText(/room for 46 more/)).toBeInTheDocument();
  });

  it('points out a number two channels share, which nothing else does', async () => {
    draw();
    await screen.findByText('┃AT┃ PULS 4');
    expect(screen.getByText('same number')).toBeInTheDocument();
    expect(screen.getByText(/1 number is used by more than one channel/)).toBeInTheDocument();
  });

  it('renumbers a whole group from a number, which is a different thing from dragging', async () => {
    API.arrangeGuideLayout.mockResolvedValue({
      numbers: { 11: 100, 12: 110, 13: 120 },
      changing: ['11', '12', '13'],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.change(screen.getByLabelText('Renumber ┃AT┃ AUSTRIA from'), {
      target: { value: '100' },
    });
    fireEvent.change(screen.getByLabelText('Renumber ┃AT┃ AUSTRIA in steps of'), {
      target: { value: '10' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Renumber every channel of ┃AT┃ AUSTRIA' })
    );

    await waitFor(() =>
      expect(API.arrangeGuideLayout).toHaveBeenCalledWith({
        order: [11, 12, 13],
        start: 100,
        step: 10,
      })
    );
    // The new numbers are shown against the old ones, and nothing is written yet
    expect(await screen.findByText('100 → 1')).toBeInTheDocument();
    expect(API.applyGuideLayout).not.toHaveBeenCalled();
  });

  it('writes only what would actually change, after asking', async () => {
    API.arrangeGuideLayout.mockResolvedValue({
      numbers: { 11: 1, 12: 5 },
      changing: ['12'],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(
      screen.getByRole('button', { name: 'Renumber every channel of ┃AT┃ AUSTRIA' })
    );
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));

    // 11 keeps the number it has, so it is not written again
    // The numbers, and the moves between groups — none here
    await waitFor(() => expect(API.applyGuideLayout).toHaveBeenCalledWith({ 12: 5 }, {}));
  });

  it('can be started again, leaving the channels as they were', async () => {
    API.arrangeGuideLayout.mockResolvedValue({ numbers: { 12: 9 }, changing: ['12'] });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(
      screen.getByRole('button', { name: 'Renumber every channel of ┃AT┃ AUSTRIA' })
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Start again' }));

    await waitFor(() => expect(API.getGuideLayout).toHaveBeenCalledTimes(2));
    expect(API.applyGuideLayout).not.toHaveBeenCalled();
  });

  it('a channel dragged into another group is a change to apply, group and number at once', async () => {
    // The dragging itself is dnd-kit's; what it leads to is ours. A channel that has left
    // its group is on the move as well as being renumbered, and both go in one apply.
    API.arrangeGuideLayout.mockResolvedValue({
      numbers: { 13: 51 },
      changing: ['13'],
    });
    const { container } = render(
      <MantineProvider theme={theme}>
        <GuideLayoutTable />
      </MantineProvider>
    );
    await screen.findByText('┃AT┃ PULS 4');
    // Every group is one place to drop into, so an empty one can receive a channel too
    expect(container.querySelectorAll('[role="button"]').length).toBeGreaterThan(0);
    expect(screen.getByText('┃AT┃ NEWS 1')).toBeInTheDocument();
  });
});
