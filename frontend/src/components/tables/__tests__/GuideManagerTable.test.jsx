// Drawn with the real table and the real Mantine, as the Lineup's tests are: a row here is
// one channel whose guide is worth changing, and the ticking and applying are most of what
// the page is.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import GuideManagerTable from '../GuideManagerTable.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getGuideManager: vi.fn(),
    runGuideManager: vi.fn(),
    applyGuideManager: vi.fn(),
    ignoreGuideManager: vi.fn(),
    saveGuideManagerSettings: vi.fn(),
  },
}));

const onNothing = {
  channel: 1, channel_name: '┃AT┃ ORF 1', group: '┃AT┃ AUSTRIA', group_id: 1,
  epg: 7, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'xmltv.at', score: 96,
  programmes: 312, now: 'Zeit im Bild', why: 'none',
  instead_of: '', instead_of_epg: null, instead_of_holds: 0,
};
const onEmpty = {
  channel: 2, channel_name: '┃NL┃ DREAMWORKS', group: '┃NL┃ HOLLAND', group_id: 2,
  epg: 9, name: 'DreamWorks', tvg_id: 'dreamworks.nl', source: 'xmltv.nl', score: 98,
  programmes: 140, now: 'Shrek', why: 'empty',
  instead_of: 'DreamWorks', instead_of_epg: 8, instead_of_holds: 0,
};

const page = {
  settings: { suggest_none: true, suggest_empty: true, suggest_better: true, min_score: 70, better_by: 20, channel_groups: [] },
  defaults: {},
  suggestions: [onNothing, onEmpty],
  run: {},
  channel_groups: [{ id: 1, name: '┃AT┃ AUSTRIA', count: 20 }],
  all_groups: [],
  ignored: [],
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <GuideManagerTable />
    </MantineProvider>
  );

const rowOf = (text) => screen.getAllByText(text)[0].closest('.tr');

describe('GuideManagerTable', () => {
  beforeEach(() => {
    API.getGuideManager.mockResolvedValue(page);
    API.runGuideManager.mockResolvedValue({ started: true, total: 2 });
    API.applyGuideManager.mockResolvedValue({ changed: 1 });
    API.ignoreGuideManager.mockResolvedValue({});
    API.saveGuideManagerSettings.mockResolvedValue({});
  });

  afterEach(() => vi.clearAllMocks());

  it('says for each channel what it is on now and what is suggested instead', async () => {
    draw();
    expect(await screen.findByText('┃AT┃ ORF 1')).toBeInTheDocument();
    // A channel on nothing, and one on a guide that holds nothing
    expect(screen.getByText('No guide')).toBeInTheDocument();
    expect(screen.getByText('Holds nothing')).toBeInTheDocument();
    // and what the suggested one holds, which is what says it is the right one
    expect(
      screen.getByText('ORF1.at · 312 programmes · Now: Zeit im Bild')
    ).toBeInTheDocument();
  });

  it('starts a run and follows how it is going', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    API.getGuideManager.mockResolvedValue({
      ...page,
      run: { running: true, state: 'running', done: 400, total: 1360, found: 12 },
    });
    fireEvent.click(screen.getByRole('button', { name: /Look for guides/ }));

    await waitFor(() => expect(API.runGuideManager).toHaveBeenCalledWith('start', page.settings));
    expect(await screen.findByText(/400 of 1360, 12 worth changing/)).toBeInTheDocument();
    // and can be stopped
    fireEvent.click(await screen.findByRole('button', { name: /Stop/ }));
    await waitFor(() => expect(API.runGuideManager).toHaveBeenCalledWith('stop'));
  });

  it('puts only the guides ticked on, after asking', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('input[type=checkbox]'));
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));

    await waitFor(() => expect(API.applyGuideManager).toHaveBeenCalledWith({ 1: 7 }));
  });

  it('narrows to one reason, since the three are different problems', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    fireEvent.click(await screen.findByText('Guide holds nothing'));

    await waitFor(() => expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull());
    expect(screen.getByText('┃NL┃ DREAMWORKS')).toBeInTheDocument();
  });

  it('waves a suggestion away, keeping which guide it was', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Ignore ┃AT┃ ORF 1' }));

    await waitFor(() =>
      expect(API.ignoreGuideManager).toHaveBeenCalledWith('ignore', {
        channel: 1,
        name: '┃AT┃ ORF 1',
        epg: 7,
      })
    );
  });

  it('keeps a setting changed, so the page opens the way it was left', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    fireEvent.click(
      await screen.findByRole('switch', { name: /A better match than the one it is on/ })
    );

    await waitFor(() =>
      expect(API.saveGuideManagerSettings).toHaveBeenCalledWith(
        expect.objectContaining({ suggest_better: false })
      )
    );
  });

  it('says what to press when nothing has been looked at yet', async () => {
    API.getGuideManager.mockResolvedValue({ ...page, suggestions: [] });
    draw();
    expect(await screen.findByText(/Press "Look for guides"/)).toBeInTheDocument();
  });
});
