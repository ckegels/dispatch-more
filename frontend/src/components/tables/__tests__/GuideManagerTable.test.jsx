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
    chooseGuideManager: vi.fn(),
    saveGuideManagerSettings: vi.fn(),
    loadChannelManagerGuide: vi.fn(),
    getChannelManagerGuides: vi.fn(),
    getChannelManagerReading: vi.fn(),
  },
}));

const onNothing = {
  channel: 1, channel_name: '┃AT┃ ORF 1', number: 1, uuid: 'uuid-one',
  group: '┃AT┃ AUSTRIA', group_id: 1,
  epg: 7, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'xmltv.at', score: 96,
  programmes: 312, now: 'Zeit im Bild', in_use: true, why: 'none',
  tier: 'certain', match_why: 'its name exactly',
  instead_of: '', instead_of_epg: null, instead_of_holds: 0, instead_of_now: '',
};
const onEmpty = {
  channel: 2, channel_name: '┃NL┃ DREAMWORKS', number: 20, uuid: 'uuid-two',
  group: '┃NL┃ HOLLAND', group_id: 2,
  epg: 9, name: 'DreamWorks', tvg_id: 'dreamworks.nl', source: 'xmltv.nl', score: 98,
  programmes: 140, now: 'Shrek', in_use: true, why: 'empty',
  tier: 'likely', match_why: 'its name, and the country agrees',
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
    API.chooseGuideManager.mockResolvedValue({});
    API.saveGuideManagerSettings.mockResolvedValue({});
    API.loadChannelManagerGuide.mockResolvedValue({ queued: true, reading: 1 });
    API.getChannelManagerGuides.mockResolvedValue({ guides: [] });
    API.getChannelManagerReading.mockResolvedValue({ reading: {} });
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

  it('changes a suggestion by hand, with the window the Lineup uses', async () => {
    API.getChannelManagerGuides.mockResolvedValue({
      guides: [
        {
          id: 21, name: 'ORF 1 Austria', tvg_id: 'ORF1b.at', source: 'xmltv.at',
          how: 'name', score: 88, programmes: 90, now: 'Wetter', in_use: true,
        },
      ],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' }));

    // The same window: search, cards, what each one holds and what is on it
    expect(await screen.findByLabelText('Search every guide')).toBeInTheDocument();
    fireEvent.click(await screen.findByLabelText('Guide ORF 1 Austria'));

    // The row says it is a choice now, not a suggestion, and is ticked with it
    expect(await screen.findByText('chosen')).toBeInTheDocument();
    expect(screen.getByText('ORF1b.at · 90 programmes · Now: Wetter')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(API.applyGuideManager).toHaveBeenCalledWith({ 1: 21 }));
  });

  it('can choose no guide at all for a channel', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' }));
    fireEvent.click(await screen.findByLabelText('Guide none'));

    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(API.applyGuideManager).toHaveBeenCalledWith({ 1: null }));
  });

  it('says what kind of match it is, not only how alike the names read', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    // A name that happens to read alike is not the same sort of thing as an id that
    // agrees, and one number for both is what made a wrong station read as a certainty
    expect(screen.getByText('Certain · 96%')).toBeInTheDocument();
    expect(screen.getByText('Likely · 98%')).toBeInTheDocument();
  });

  it('loads on its own when the view is changed, without being poked again', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    expect(API.getGuideManager).toHaveBeenCalledWith(false);

    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    fireEvent.click(await screen.findByText('Every channel'));

    // Every channel needs the rows a run never stored, so it asks for them itself
    await waitFor(() => expect(API.getGuideManager).toHaveBeenCalledWith(true));
  });

  it('shows every channel looked at, including the ones nothing fits', async () => {
    const nothingFits = {
      channel: 3, channel_name: '┃USA┃ PBS 12', number: 12, uuid: 'uuid-three',
      group: '┃USA┃ PBS', group_id: 3, why: '',
      epg: null, name: '', tvg_id: '', source: '', programmes: 0, now: '',
      instead_of: '', instead_of_epg: null, instead_of_holds: 0, instead_of_now: '',
    };
    API.getGuideManager.mockResolvedValue({
      ...page,
      suggestions: [onNothing, nothingFits],
    });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    // Not among the suggestions, which is the default view
    await screen.findByText('┃AT┃ ORF 1');
    expect(screen.queryByText('┃USA┃ PBS 12')).toBeNull();

    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    fireEvent.click(await screen.findByText('Every channel'));

    expect(await screen.findByText('┃USA┃ PBS 12')).toBeInTheDocument();
    expect(screen.getByText('Nothing fits it')).toBeInTheDocument();
    // and it can still be settled by hand
    expect(
      screen.getByRole('button', { name: 'Change the guide for ┃USA┃ PBS 12' })
    ).toBeInTheDocument();
  });

  // A channel whose guide has been decided is not asked about again. Not the same as
  // waving a suggestion away, which says that one guide is wrong.
  it('keeps the channels chosen already on a view of their own', async () => {
    const settled = {
      channel: 4, channel_name: '┃AT┃ ORF 2', number: 2, uuid: 'uuid-four',
      group: '┃AT┃ AUSTRIA', group_id: 1, why: '', chosen: true,
      epg: 11, name: 'ORF 2', tvg_id: 'ORF2.at', source: 'xmltv.at', score: 91,
      tier: 'certain', programmes: 40, now: 'Bundesland heute', in_use: true,
      instead_of: 'ORF 2', instead_of_epg: 11, instead_of_holds: 40,
      instead_of_now: 'Bundesland heute', instead_of_source: 'xmltv.at',
    };
    API.getGuideManager.mockResolvedValue({
      ...page,
      suggestions: [onNothing, settled],
      chosen: [{ channel: '4', name: 'ORF 2', epg: 11, at: '2026-09-20T20:00:00' }],
    });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    // Settled, so it is not among what is being put forward
    await screen.findByText('┃AT┃ ORF 1');
    expect(screen.queryByText('┃AT┃ ORF 2')).toBeNull();

    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    fireEvent.click(await screen.findByText('Chosen already'));

    // ...and a settled channel has nothing to suggest, so the rows come from every channel
    await waitFor(() => expect(API.getGuideManager).toHaveBeenCalledWith(true));
    expect(await screen.findByText('┃AT┃ ORF 2')).toBeInTheDocument();
    expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull();
    expect(screen.getByText('Chosen')).toBeInTheDocument();
  });

  it('settles the guide a channel is already on, and unsettles it again', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(
      screen.getByRole('button', { name: 'Keep the guide on ┃NL┃ DREAMWORKS' })
    );
    await waitFor(() =>
      expect(API.chooseGuideManager).toHaveBeenCalledWith('choose', {
        channel: 2, name: 'DreamWorks', epg: 8,
      })
    );
  });

  it('asks again about a channel settled by mistake', async () => {
    const settled = { ...onEmpty, why: '', chosen: true };
    API.getGuideManager.mockResolvedValue({ ...page, suggestions: [settled] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    fireEvent.click(await screen.findByText('Chosen already'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Suggest for ┃NL┃ DREAMWORKS again' })
    );
    await waitFor(() =>
      expect(API.chooseGuideManager).toHaveBeenCalledWith('unchoose', {
        channel: 2, name: 'DreamWorks', epg: 8,
      })
    );
  });
});
