// Drawn with the real table and the real Mantine, as the Lineup's tests are: a row here is
// one channel whose guide is worth changing, and the ticking and applying are most of what
// the page is.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import GuideManagerTable from '../GuideManagerTable.jsx';
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
    getGuideManager: vi.fn(),
    runGuideManager: vi.fn(),
    applyGuideManager: vi.fn(),
    ignoreGuideManager: vi.fn(),
    saveGuideManagerSettings: vi.fn(),
    loadChannelManagerGuide: vi.fn(),
  },
}));

const onNothing = {
  channel: 1, channel_name: '┃AT┃ ORF 1', number: 1, uuid: 'uuid-one',
  group: '┃AT┃ AUSTRIA', group_id: 1,
  epg: 7, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'xmltv.at', score: 96,
  programmes: 312, now: 'Zeit im Bild', in_use: true, why: 'none',
  instead_of: '', instead_of_epg: null, instead_of_holds: 0, instead_of_now: '',
};
const onEmpty = {
  channel: 2, channel_name: '┃NL┃ DREAMWORKS', number: 20, uuid: 'uuid-two',
  group: '┃NL┃ HOLLAND', group_id: 2,
  epg: 9, name: 'DreamWorks', tvg_id: 'dreamworks.nl', source: 'xmltv.nl', score: 98,
  programmes: 140, now: 'Shrek', in_use: true, why: 'empty',
  instead_of: 'DreamWorks', instead_of_epg: 8, instead_of_holds: 0,
  instead_of_now: '', instead_of_source: 'xmltv.uk',
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
    API.loadChannelManagerGuide.mockResolvedValue({ queued: true, reading: 1 });
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
      run: {
        running: true, state: 'running', done: 400, total: 1360, found: 12,
        stage: 'looking at your channels', at: '┃AT┃ ORF 1',
        since: new Date().toISOString(),
      },
    });
    fireEvent.click(screen.getByRole('button', { name: /Look for guides/ }));

    await waitFor(() => expect(API.runGuideManager).toHaveBeenCalledWith('start', page.settings));
    expect(await screen.findByText(/400 of 1360/)).toBeInTheDocument();
    expect(screen.getByText(/12 worth changing/)).toBeInTheDocument();
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

  it('says what is on the guide a channel is on now, not just its name', async () => {
    draw();
    await screen.findByText('┃NL┃ DREAMWORKS');
    // The guide it is on holds nothing and has nothing on: that is the whole complaint
    expect(screen.getByText('xmltv.uk · holds nothing')).toBeInTheDocument();
    expect(screen.getByText('Nothing on it now')).toBeInTheDocument();
  });

  it('watches the channel, since a guide can be right and the channel not', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Watch ┃AT┃ ORF 1' }));

    expect(showVideo).toHaveBeenCalledWith(
      expect.stringContaining('/proxy/ts/stream/uuid-one'),
      'live',
      { name: '┃AT┃ ORF 1', channelId: 1 }
    );
  });

  it('does not call a guide nobody has read empty, and offers to read them', async () => {
    API.getGuideManager.mockResolvedValue({
      ...page,
      suggestions: [{ ...onNothing, programmes: 0, now: '', in_use: false }],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    expect(screen.getByText('ORF1.at · not read yet')).toBeInTheDocument();

    API.getGuideManager.mockResolvedValue(page);
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Read the programmes of every suggested guide not read yet',
      })
    );
    await waitFor(() => expect(API.loadChannelManagerGuide).toHaveBeenCalledWith([7]));
  });

  it('says what it is doing before a single channel has been looked at', async () => {
    // Reading the whole guide catalogue is most of a batch with a lot of EPG, and
    // happens before anything can move: a still bar reads as a page that has stopped
    API.getGuideManager.mockResolvedValue({
      ...page,
      run: {
        running: true, state: 'running', done: 0, total: 1360, found: 0,
        stage: 'reading the guides there are', at: '', since: new Date().toISOString(),
      },
    });
    draw();
    expect(await screen.findByText(/reading the guides there are/)).toBeInTheDocument();
    expect(screen.getByText(/0 of 1360/)).toBeInTheDocument();
  });
});
