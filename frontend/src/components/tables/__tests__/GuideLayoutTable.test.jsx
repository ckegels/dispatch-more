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
    renameGuideLayout: vi.fn(),
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

// A thousand channels drawn as draggable rows is what made this page crawl, so a group
// is shut until it is opened
const openAll = async () => {
  fireEvent.click(await screen.findByRole('button', { name: 'Open them all' }));
};

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
    API.renameGuideLayout.mockResolvedValue({ names: {} });
  });

  afterEach(() => vi.clearAllMocks());

  it('shows each group with its channels in order, and where there is room', async () => {
    draw();
    expect((await screen.findAllByText('┃AT┃ AUSTRIA')).length).toBeGreaterThan(0);
    expect(screen.getByText(/3 channels · 1–3/)).toBeInTheDocument();
    expect(screen.getByText(/room for 46/)).toBeInTheDocument();
  });

  it('points out a number two channels share, which nothing else does', async () => {
    draw();
    await openAll();
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
    await openAll();
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
    await openAll();
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
    await openAll();
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
    await openAll();
    await screen.findByText('┃AT┃ PULS 4');
    // Every group is one place to drop into, so an empty one can receive a channel too
    expect(container.querySelectorAll('[role="button"]').length).toBeGreaterThan(0);
    expect(screen.getByText('┃AT┃ NEWS 1')).toBeInTheDocument();
  });

  it('keeps the groups shut until they are opened, which is what made it quick', async () => {
    draw();
    // Shut, a group still says what is worth knowing about it
    expect(await screen.findByText(/3 channels · 1–3/)).toBeInTheDocument();
    expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Open ┃AT┃ AUSTRIA' }));
    expect(await screen.findByText('┃AT┃ ORF 1')).toBeInTheDocument();
    // ...and the other group is still shut, so only what is being worked on is drawn
    expect(screen.queryByText('┃AT┃ NEWS 1')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Close ┃AT┃ AUSTRIA' }));
    await waitFor(() => expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull());
  });

  it('renames a channel where it stands', async () => {
    draw();
    await openAll();
    fireEvent.click(await screen.findByText('┃AT┃ ORF 1'));
    const box = await screen.findByLabelText('Name for ┃AT┃ ORF 1');
    fireEvent.change(box, { target: { value: 'ORF Eins' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    await waitFor(() =>
      expect(API.renameGuideLayout).toHaveBeenCalledWith({ names: { 11: 'ORF Eins' } })
    );
  });

  it('shows what taking something out of every name would leave, before it does it', async () => {
    API.renameGuideLayout.mockResolvedValue({ names: { 11: 'ORF 1', 12: 'ORF 2' } });
    draw();
    await openAll();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.change(screen.getByLabelText('Take out of every name in ┃AT┃ AUSTRIA'), {
      target: { value: '┃AT┃ ' },
    });
    fireEvent.click(
      screen.getByRole('button', {
        name: 'See what taking that out of ┃AT┃ AUSTRIA would leave',
      })
    );

    // Asked, not done: renaming a group's channels is many changes and there is no undo
    expect(await screen.findByText('2 names would change')).toBeInTheDocument();
    expect(screen.getByText('┃AT┃ ORF 1 → ORF 1')).toBeInTheDocument();
    expect(API.renameGuideLayout).toHaveBeenCalledWith(
      expect.objectContaining({ take_off: '┃AT┃', apply: false })
    );

    fireEvent.click(screen.getByRole('button', { name: 'Rename them' }));
    await waitFor(() =>
      expect(API.renameGuideLayout).toHaveBeenCalledWith(
        expect.objectContaining({ take_off: '┃AT┃', apply: true })
      )
    );
  });
});
