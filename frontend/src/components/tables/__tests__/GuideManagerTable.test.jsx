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
    getGuideMatching: vi.fn(),
    saveGuideMatching: vi.fn(),
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
    API.getGuideMatching.mockResolvedValue({ matching: null, sources: [] });
    API.saveGuideMatching.mockResolvedValue({});
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
    fireEvent.click(screen.getByRole('button', { name: /^Not .* for ┃AT┃ ORF 1$/ }));

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

  describe('when what is on changes while the page is open', () => {
    beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
    afterEach(() => vi.useRealTimers());

    it('asks again then, and shows what is on now', async () => {
      const ends = new Date(Date.now() + 10 * 60 * 1000).toISOString();
      API.getGuideManager.mockResolvedValue({
        ...page,
        suggestions: [{ ...onNothing, now_changes_at: ends }, onEmpty],
      });
      draw();
      expect(
        await screen.findByText('ORF1.at · 312 programmes · Now: Zeit im Bild')
      ).toBeInTheDocument();
      const asked = API.getGuideManager.mock.calls.length;

      // Nothing is asked before it changes: once per change, not every few seconds
      await vi.advanceTimersByTimeAsync(9 * 60 * 1000);
      expect(API.getGuideManager.mock.calls.length).toBe(asked);

      API.getGuideManager.mockResolvedValue({
        ...page,
        suggestions: [{ ...onNothing, now: 'Wetter' }, onEmpty],
      });
      await vi.advanceTimersByTimeAsync(2 * 60 * 1000);
      expect(
        await screen.findByText('ORF1.at · 312 programmes · Now: Wetter')
      ).toBeInTheDocument();
      // Quietly, and for the same view
      expect(API.getGuideManager).toHaveBeenLastCalledWith(false);
    });

    it('and so does the window, without emptying the list while it asks', async () => {
      const ends = new Date(Date.now() + 60 * 1000).toISOString();
      const card = {
        id: 21, name: 'ORF 1 Austria', tvg_id: 'ORF1b.at', source: 'xmltv.at',
        how: 'name', score: 88, programmes: 90, in_use: true,
      };
      API.getChannelManagerGuides.mockResolvedValue({
        guides: [{ ...card, now: 'Wetter', changes_at: ends }],
      });
      draw();
      await screen.findByText('┃AT┃ ORF 1');
      fireEvent.click(
        screen.getByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
      );
      expect(await screen.findByText('Now: Wetter')).toBeInTheDocument();

      API.getChannelManagerGuides.mockResolvedValue({
        guides: [{ ...card, now: 'Zeit im Bild 2' }],
      });
      await vi.advanceTimersByTimeAsync(2 * 60 * 1000);
      expect(await screen.findByText('Now: Zeit im Bild 2')).toBeInTheDocument();
      expect(screen.getByLabelText('Guide ORF 1 Austria')).toBeInTheDocument();
    });
  });

  it('searches every guide there is, a page at a time, and says how many', async () => {
    const station = (n) => ({
      id: 100 + n, name: `PBS Station ${n}`, tvg_id: `pbs${n}.us`, source: 'PBS',
      how: 'search', programmes: 0, in_use: false,
    });
    const page = (upTo) => ({
      guides: Array.from({ length: upTo }, (_, n) => station(n)),
      total: 140,
    });
    API.getChannelManagerGuides.mockImplementation(async ({ q, limit }) =>
      q ? page(Math.min(limit, 140)) : { guides: [] }
    );
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' }));
    fireEvent.change(await screen.findByLabelText('Search every guide'), {
      target: { value: 'pbs' },
    });

    expect(
      await screen.findByText('140 guides match · the first 100 shown', undefined, {
        timeout: 5000,
      })
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Guide PBS Station 99')).toBeInTheDocument();
    expect(screen.queryByLabelText('Guide PBS Station 100')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show 40 more of 140' }));
    expect(
      await screen.findByLabelText('Guide PBS Station 139', undefined, { timeout: 5000 })
    ).toBeInTheDocument();
    expect(API.getChannelManagerGuides).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: 'pbs', limit: 200 })
    );
    expect(screen.getByText('140 guides match')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /more of 140/ })).not.toBeInTheDocument();
    // A hundred and forty cards are slow to draw with the whole suite running beside it
  }, 15000);

  it('shows only the source it is narrowed to, and a search keeps to it', async () => {
    API.getGuideMatching.mockResolvedValue({
      matching: null,
      sources: [
        { id: 1, name: 'xmltv.at', active: true, holds: 10 },
        { id: 2, name: 'xmltv.de', active: true, holds: 20 },
      ],
    });
    const german = {
      id: 30, name: 'ORF 1 DE', tvg_id: 'orf1.de', source: 'xmltv.de', how: 'search',
      programmes: 5, in_use: true,
    };
    const suggested = {
      id: 7, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'xmltv.at', how: 'kept',
      programmes: 312, in_use: true,
    };
    // As the server answers: the guide the row has comes back only from its own source
    API.getChannelManagerGuides.mockImplementation(async ({ source }) => ({
      guides: source === '2' ? [german] : [suggested, german],
    }));
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' }));
    expect(await screen.findByLabelText('Guide ORF 1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('textbox', { name: 'From one source' }));
    fireEvent.click(await screen.findByText('xmltv.de (20)'));
    await waitFor(() =>
      expect(API.getChannelManagerGuides).toHaveBeenLastCalledWith(
        expect.objectContaining({ source: '2' })
      )
    );
    expect(await screen.findByLabelText('Guide ORF 1 DE')).toBeInTheDocument();
    // The row's own guide is from xmltv.at, and is not put back on a list of xmltv.de
    await waitFor(() =>
      expect(screen.queryByLabelText('Guide ORF 1')).not.toBeInTheDocument()
    );

    // ...and a search keeps to it
    fireEvent.change(screen.getByLabelText('Search every guide'), {
      target: { value: 'orf' },
    });
    await waitFor(() =>
      expect(API.getChannelManagerGuides).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: 'orf', source: '2' })
      )
    );
  });

  describe('a source picked while guides are being read', () => {
    beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
    afterEach(() => vi.useRealTimers());

    it('stays picked: the read does not put every source back', async () => {
      API.getGuideMatching.mockResolvedValue({
        matching: null,
        sources: [
          { id: 1, name: 'xmltv.at', active: true, holds: 10 },
          { id: 2, name: 'xmltv.de', active: true, holds: 20 },
        ],
      });
      const unread = {
        id: 40, name: 'ORF 1 Unread', tvg_id: 'orf1.unread', source: 'xmltv.at',
        how: 'name', programmes: 0, in_use: false,
      };
      const german = {
        id: 30, name: 'ORF 1 DE', tvg_id: 'orf1.de', source: 'xmltv.de', how: 'name',
        programmes: 5, in_use: true,
      };
      API.getChannelManagerGuides.mockImplementation(async ({ source }) => ({
        guides: source === '2' ? [german] : [unread, german],
      }));
      // Still reading, for as long as the test looks
      API.getChannelManagerReading.mockResolvedValue({
        reading: { reading: true, stage: 'going through xmltv.at' },
      });
      draw();
      await screen.findByText('┃AT┃ ORF 1');
      fireEvent.click(
        screen.getByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
      );
      fireEvent.click(
        await screen.findByRole('button', {
          name: 'Read the programmes of every guide shown',
        })
      );
      await waitFor(() => expect(API.loadChannelManagerGuide).toHaveBeenCalled());

      // Picked while the read goes on
      fireEvent.click(screen.getByRole('textbox', { name: 'From one source' }));
      fireEvent.click(await screen.findByText('xmltv.de (20)'));
      await waitFor(() =>
        expect(screen.queryByLabelText('Guide ORF 1 Unread')).not.toBeInTheDocument()
      );

      // The read asks again every three seconds; what it gets back is of xmltv.de
      await vi.advanceTimersByTimeAsync(10000);
      expect(screen.getByLabelText('Guide ORF 1 DE')).toBeInTheDocument();
      expect(screen.queryByLabelText('Guide ORF 1 Unread')).not.toBeInTheDocument();
      expect(API.getChannelManagerGuides).toHaveBeenLastCalledWith(
        expect.objectContaining({ source: '2' })
      );
    });
  });

  it('offers every group with channels, not only the ones the last run left rows for', async () => {
    // Made in the Lineup after the last run: it has channels and no rows yet
    API.getGuideManager.mockResolvedValue({
      ...page,
      channel_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA', count: 20 },
        { id: 2, name: '┃NL┃ HOLLAND', count: 4 },
        { id: 3, name: 'PBS Locals', count: 12 },
      ],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('textbox', { name: 'Which group' }));
    fireEvent.click(await screen.findByText('PBS Locals'));
    // ...and says why there is nothing in it, and where its channels are
    expect(
      await screen.findByText(/Nothing to change in PBS Locals from the last run/)
    ).toBeInTheDocument();
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

  // Laid out the way the Lineup and Stream Check are: a panel with a toolbar, a band
  // under it saying how the run went, and the table below. The band is always there, so
  // the page says where it stands before anything has been run.
  it('says how the last run went even when nothing is running', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    expect(screen.getByText(/Not run yet/)).toBeInTheDocument();
    expect(screen.getByText(/2 on show/)).toBeInTheDocument();

    API.getGuideManager.mockResolvedValue({
      ...page,
      run: { state: 'done', running: false, total: 1360, done: 1360, found: 12 },
    });
    fireEvent.click(screen.getByRole('button', { name: /Look for guides/ }));
    expect(
      await screen.findByText(/Last run: 1360 channels looked at/)
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
    fireEvent.click(await screen.findByText(/^Left alone/));

    // ...and a settled channel has nothing to suggest, so the rows come from every channel
    await waitFor(() => expect(API.getGuideManager).toHaveBeenCalledWith(true));
    expect(await screen.findByText('┃AT┃ ORF 2')).toBeInTheDocument();
    expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull();
    expect(screen.getByText('Left alone')).toBeInTheDocument();
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
    fireEvent.click(await screen.findByText(/^Left alone/));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Suggest for ┃NL┃ DREAMWORKS again' })
    );
    await waitFor(() =>
      expect(API.chooseGuideManager).toHaveBeenCalledWith('unchoose', {
        channel: 2, name: 'DreamWorks', epg: 8,
      })
    );
  });

  // Which guides are matched against at all: the same settings the window on a Lineup row
  // obeys, so one of them cannot offer what the other has been told to leave out
  it('lets a source be clicked out of the matching without switching it off', async () => {
    API.getGuideMatching.mockResolvedValue({
      matching: { sources: [], tvg_id_like: '', country_must_agree: false },
      sources: [
        { id: 7, name: 'epg ripper ALL', active: true, holds: 4000, channels: 12 },
        { id: 8, name: 'open epg', active: true, holds: 900, channels: 3 },
      ],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));

    // Every source is on to begin with; clicking one takes it out, which is the way
    // round somebody wants it -- they know the source they do not trust
    fireEvent.click(await screen.findByRole('button', { name: 'Leave out open epg' }));

    await waitFor(() =>
      expect(API.saveGuideMatching).toHaveBeenCalledWith(
        expect.objectContaining({ sources: [7] })
      )
    );
  });

  it('and a tvg-id to match, and another country refused outright', async () => {
    API.getGuideMatching.mockResolvedValue({
      matching: { sources: [], tvg_id_like: '', country_must_agree: false },
      sources: [],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));

    const box = await screen.findByRole('textbox', { name: /Only tvg-ids like/ });
    fireEvent.change(box, { target: { value: '.uk' } });
    fireEvent.blur(box);
    await waitFor(() =>
      expect(API.saveGuideMatching).toHaveBeenCalledWith(
        expect.objectContaining({ tvg_id_like: '.uk' })
      )
    );

    fireEvent.click(screen.getByLabelText(/Refuse another country/));
    await waitFor(() =>
      expect(API.saveGuideMatching).toHaveBeenCalledWith(
        expect.objectContaining({ country_must_agree: true })
      )
    );
  });

  it('and puts every source back on when none is left out', async () => {
    API.getGuideMatching.mockResolvedValue({
      matching: { sources: [7], tvg_id_like: '', country_must_agree: false },
      sources: [
        { id: 7, name: 'epg ripper ALL', active: true, holds: 4000, channels: 12 },
        { id: 8, name: 'open epg', active: true, holds: 900, channels: 3 },
      ],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));

    // Clicking the last one back on means all of them, which is kept as nothing chosen,
    // so a source added later is matched against too
    fireEvent.click(await screen.findByRole('button', { name: 'Match against open epg' }));

    await waitFor(() =>
      expect(API.saveGuideMatching).toHaveBeenCalledWith(
        expect.objectContaining({ sources: [] })
      )
    );
  });

  // "Not read yet" about a guide that has been read sends you round the same loop for
  // ever: read it, nothing changes, read it again.
  it('says a guide was read and had nothing, rather than not read yet', async () => {
    const empty = {
      ...onNothing,
      channel: 5,
      channel_name: '┃AT┃ ORF 3',
      programmes: 0,
      in_use: false,
      read: { at: '2026-09-21T10:00:00', found: 0, why: '' },
    };
    API.getGuideManager.mockResolvedValue({ ...page, suggestions: [empty] });
    draw();
    await screen.findByText('┃AT┃ ORF 3');

    expect(
      screen.getByText(/read, and the guide has none/)
    ).toBeInTheDocument();
    // ...and it is not offered to be read again
    expect(screen.queryByRole('button', { name: /^Read \d+ guide/ })).toBeNull();
  });

  it('and says what stopped a read when something did', async () => {
    const blocked = {
      ...onNothing,
      channel: 6,
      channel_name: '┃AT┃ ORF 4',
      programmes: 0,
      in_use: false,
      read: { at: '2026-09-21T10:00:00', found: 0, why: 'its source was being refreshed' },
    };
    API.getGuideManager.mockResolvedValue({ ...page, suggestions: [blocked] });
    draw();
    await screen.findByText('┃AT┃ ORF 4');
    expect(
      screen.getByText(/could not be read: its source was being refreshed/)
    ).toBeInTheDocument();
  });

  // Taking a suggestion and settling the channel are what somebody working down this
  // list does every time; doing them apart meant a tick, a trip to the toolbar and a
  // question, for each row
  it('takes a suggestion and settles the channel in one press', async () => {
    draw();
    await screen.findByText('┃AT┃ ORF 1');

    fireEvent.click(
      screen.getByRole('button', { name: 'Take ORF 1 for ┃AT┃ ORF 1' })
    );

    await waitFor(() =>
      expect(API.applyGuideManager).toHaveBeenCalledWith({ 1: 7 })
    );
  });

  // Two grey icons that both made the row go away. They do different things: one is
  // about the channel, the other about the guide being offered for it.
  it('keeps the two ways of saying no apart, and lists both', async () => {
    API.getGuideManager.mockResolvedValue({
      ...page,
      chosen: [{ channel: '4', name: 'ORF 2', epg: 11, at: '2026-09-21T20:00:00' }],
      ignored: [{ channel: '2', name: 'DreamWorks', epg: 9, at: '2026-09-21T20:00:00' }],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');

    // Each has a view of its own, and says how many are in it
    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    expect(await screen.findByText('Left alone (1)')).toBeInTheDocument();
    expect(screen.getByText('Wrong guide, try again (1)')).toBeInTheDocument();
  });

  it('works through the channels on one guide source at a time', async () => {
    // "Everything that is on the source that went stale": the channels are narrowed by
    // what they are on now, which is the other half of narrowing which guides may answer
    Element.prototype.scrollIntoView = vi.fn();
    API.getGuideManager.mockResolvedValue({
      ...page,
      suggestions: [onNothing, onEmpty],
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');

    const field = screen.getByRole('textbox', { name: 'Now on' });
    fireEvent.click(field);
    // What your channels are actually on, with how many are on each
    expect(await screen.findByText('xmltv.uk (1)')).toBeInTheDocument();
    expect(screen.getByText('No guide (1)')).toBeInTheDocument();

    fireEvent.click(screen.getByText('xmltv.uk (1)'));

    expect(await screen.findByText('┃NL┃ DREAMWORKS')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull());
  });

  it('says which channels a run asks about, as well as which guides may answer', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    API.getGuideMatching.mockResolvedValue({
      matching: { sources: [], tvg_id_like: '', country_must_agree: false },
      sources: [
        { id: 3, name: 'xmltv.uk', active: true, holds: 900, channels: 12 },
        { id: 4, name: 'xmltv.nl', active: true, holds: 400, channels: 3 },
      ],
      reference: { names: 0, call_signs: 0 },
    });
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }));

    const field = await screen.findByRole('textbox', { name: /Only channels now on/ });
    fireEvent.click(field);
    fireEvent.click(await screen.findByText('xmltv.uk (12 channels)'));

    await waitFor(() =>
      expect(API.saveGuideManagerSettings).toHaveBeenCalledWith(
        expect.objectContaining({ on_sources: [3] })
      )
    );
  });

  it('and shows the ones waved away when asked', async () => {
    const waved = { ...onEmpty, why: '', waved_away: true, waved_away_guide: 'DreamWorks' };
    API.getGuideManager.mockResolvedValue({ ...page, suggestions: [onNothing, waved] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findByText('┃AT┃ ORF 1');
    expect(screen.queryByText('┃NL┃ DREAMWORKS')).toBeNull();

    fireEvent.click(screen.getByRole('textbox', { name: 'Why' }));
    fireEvent.click(await screen.findByText(/^Wrong guide, try again/));

    expect(await screen.findByText('┃NL┃ DREAMWORKS')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('┃AT┃ ORF 1')).toBeNull());
  });
});
