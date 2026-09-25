// Drawn with the real table and the real Mantine rather than stand-ins: the table's own
// tick box and row expansion are most of what this page is, and a stand-in cannot tell
// whether they are wired up.
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    ignoreChannelManager: vi.fn(),
    saveChannelManagerSettings: vi.fn(),
    getChannelManagerReading: vi.fn(),
    addChannelGroup: vi.fn(),
    getChannelManagerGuides: vi.fn(),
    searchChannelManagerStreams: vi.fn(),
    getGuideMatching: vi.fn(),
    saveGuideMatching: vi.fn(),
    loadChannelManagerGuide: vi.fn(),
  },
}));

const stream = (id, name, extra = {}) => ({
  id, name, hash: `hash-${id}`, account: 'Provider A', group: '┃AT┃ AUSTRIA', quality: 'HD', probed: false,
  tvg_id: '', logo_url: '', added: false, removed: false, custom: false, in_scope: true, ...extra,
});
const fallback = stream(9, 'could not dispatch', { custom: true, account: 'custom' });
const held = {
  id: 5, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'Austria', how: 'kept',
  programmes: 40, now: 'Bundesland heute', in_use: true,
};
const guide = {
  id: 7, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'Austria', how: 'name', score: 96,
  programmes: 312, now: 'Zeit im Bild', in_use: true,
};

const mergeRow = {
  key: 'ch:1', status: 'merge', adds: 1, removes: 0, changes: [], country: 'at',
  channel: { id: 1, name: '┃AT┃ ORF 1', number: 1, group: '┃AT┃ AUSTRIA', group_id: 1, logo_url: '', epg: { id: 5, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'Austria', how: 'kept' } },
  before: {
    channel: {
      id: 1, name: '┃AT┃ ORF 1', number: 1, logo_url: '', group: '┃AT┃ AUSTRIA', group_id: 1,
      epg: {
        id: 5, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'Austria', how: 'kept',
        programmes: 40, now: 'Bundesland heute',
      },
    },
    streams: [stream(1, '┃AT┃ ORF 1'), fallback],
  },
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

// The group names appear in the toolbar's filter as well as in a row's own picker, and
// both dropdowns are in the page whether open or not -- so the option is taken from the
// dropdown that belongs to the field being used.
const pickOption = async (fieldName, text) => {
  const field = screen.getByRole('textbox', { name: fieldName });
  fireEvent.click(field);
  const dropdown = document.getElementById(field.getAttribute('aria-controls'));
  fireEvent.click(await within(dropdown).findByText(text));
};

describe('ChannelManagerTable', () => {
  beforeEach(() => {
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts: [], stream_groups: [],
      channel_groups: [], all_groups: [], profiles: [],
    });
    API.previewChannelManager.mockResolvedValue(plan);
    API.saveChannelManagerSettings.mockResolvedValue({});
    API.applyChannelManager.mockResolvedValue({ created: 0, updated: 1, streams_added: 1 });
    API.getChannelManagerGuides.mockResolvedValue({ guides: [] });
    API.getGuideMatching.mockResolvedValue({ matching: null, sources: [] });
    API.saveGuideMatching.mockResolvedValue({});
    API.getChannelManagerReading.mockResolvedValue({ reading: {} });
    API.loadChannelManagerGuide.mockResolvedValue({ queued: true, reading: 1 });
  });

  afterEach(() => vi.clearAllMocks());

  it('shows each channel as it is and as it would be, and what it comes to', async () => {
    draw();
    expect((await screen.findAllByText('┃AT┃ ORF 1')).length).toBeGreaterThan(0);
    // The guide it is on now and the one it would come out with, said the same way on
    // both sides so they can be read against each other
    expect(screen.getAllByText('Guide: ORF 1 · Austria')).toHaveLength(2);
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
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['ch:1'], {}, {}, {}, {}, {}, {}
      )
    );
  });

  it('puts streams in another order by hand, the fallback staying last', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));

    // The fallback cannot be moved, and the first cannot go further up
    expect(screen.queryByLabelText('Move could not dispatch up')).toBeNull();
    expect(await screen.findByLabelText('Move ┃AT┃ ORF 1 up')).toBeDisabled();

    fireEvent.click(screen.getByLabelText('Move ┃AT┃ ORF 1 FHD up'));
    expect(await screen.findByText('Reordered')).toBeInTheDocument();

    // Ticked with the move, and sent with the order given
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' },
        ['ch:1'],
        { 'ch:1': [2, 1] },
        {},
        {},
        {},
        {},
        {}
      )
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

  it('opens every row at once, and closes them again', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    expect(screen.queryByText(/nothing is done with them/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    // The merge and the conflict are both open
    expect(await screen.findByText(/nothing is done with them/)).toBeInTheDocument();
    expect(screen.getAllByText(/After · in the order they are tried/).length).toBe(1);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));
    await waitFor(() => expect(screen.queryByText(/nothing is done with them/)).toBeNull());
  });

  it('suggests a group for a new channel, which can be changed before applying', async () => {
    const newRow = {
      key: 'new:at:puls4', status: 'new', adds: 1, removes: 0, changes: [], country: 'at',
      channel: {
        id: null, name: '┃AT┃ PULS 4', number: 12, group: '┃AT┃ AUSTRIA', group_id: 1,
        group_why: 'where your channels from this stream group are', logo_url: '', epg: null,
      },
      before: { channel: null, streams: [stream(6, '┃AT┃ PULS 4 HD')] },
      streams: [stream(6, '┃AT┃ PULS 4 HD', { added: true }), fallback],
    };
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts: [], stream_groups: [],
      profiles: [],
      channel_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA', count: 20, kind: 'with_channels' },
        { id: 2, name: '┃DE┃ GERMANY', count: 9, kind: 'with_channels' },
      ],
      all_groups: [{ id: 1, name: '┃AT┃ AUSTRIA' }, { id: 2, name: '┃DE┃ GERMANY' }],
    });
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [newRow] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findAllByText('┃AT┃ PULS 4');
    expect(screen.getByText('12 · ┃AT┃ AUSTRIA')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    expect(await screen.findByText(/Suggested: where your channels from this stream group are/)).toBeInTheDocument();

    await pickOption('Channel group for ┃AT┃ PULS 4', '┃DE┃ GERMANY');
    expect(await screen.findByText('new number · ┃DE┃ GERMANY')).toBeInTheDocument();

    // Choosing a group ticks the row, as moving a stream does
    fireEvent.click(screen.getByRole('button', { name: /^Apply/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['new:at:puls4'], {}, { 'new:at:puls4': 2 }, {}, {}, {}, {}
      )
    );
  });

  it('takes a wrong stream out of a row, but never the fallback', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    expect(screen.queryByRole('button', { name: 'Drop could not dispatch' })).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: 'Drop ┃AT┃ ORF 1 FHD' }));
    // Undoable, and the row is ticked
    expect(screen.getByRole('button', { name: 'Keep ┃AT┃ ORF 1 FHD' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^Apply/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['ch:1'], {}, {}, { 'ch:1': [2] }, {}, {}, {}
      )
    );
  });

  it('ignores a suggestion, lists it, and clears the list', async () => {
    API.ignoreChannelManager.mockResolvedValue({});
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Ignore ┃AT┃ ORF 1' }));
    await waitFor(() =>
      expect(API.ignoreChannelManager).toHaveBeenCalledWith('ignore', {
        key: 'ch:1', name: '┃AT┃ ORF 1', kind: 'merge', streams: [2],
      })
    );
    fireEvent.click(screen.getByRole('textbox', { name: 'Which channels' }));
    fireEvent.click(await screen.findByText('Ignored (1)'));
    expect(await screen.findByText('┃AT┃ ORF 1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Clear ignored list' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(API.ignoreChannelManager).toHaveBeenLastCalledWith('clear'));
  });

  it('asks for a fresh preview before applying after the levers move', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('input[type=checkbox]'));
    fireEvent.click(screen.getByRole('button', { name: 'Levers' }));
    fireEvent.click(screen.getByRole('button', { name: 'Open New channels' }));
    fireEvent.click(screen.getByRole('switch', { name: /Suggest new channels/ }));

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
    fireEvent.click(screen.getByRole('button', { name: 'Open Recognising a channel' }));
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

  it('renames a channel on its row, and applies the name with it', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));

    const name = await screen.findByRole('textbox', { name: 'Name for ┃AT┃ ORF 1' });
    fireEvent.change(name, { target: { value: '┃AT┃ ORF Eins' } });
    // The row says what it would come out as, and warns that a channel is renamed
    expect(await screen.findByText(/renamed or re-guided/)).toBeInTheDocument();

    // Typing a name ticks the row, as moving a stream does
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' },
        ['ch:1'],
        {},
        {},
        {},
        { 'ch:1': '┃AT┃ ORF Eins' },
        {},
        {}
      )
    );
  });

  it('asks for the guides a channel could be only when the window is opened', async () => {
    API.getChannelManagerGuides.mockResolvedValue({
      guides: [held, { ...guide, name: 'ORF 1 Austria' }],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));

    // Opening the row asks nothing: with Expand all that would be a question per row
    const change = await screen.findByRole('button', {
      name: 'Change the guide for ┃AT┃ ORF 1',
    });
    expect(API.getChannelManagerGuides).not.toHaveBeenCalled();

    fireEvent.click(change);
    await waitFor(() => expect(API.getChannelManagerGuides).toHaveBeenCalled());
    // What tells two entries of one name apart: where it is from, what it holds,
    // and what is on it now
    expect(await screen.findByText('ORF 1 Austria')).toBeInTheDocument();
    expect(screen.getAllByText('Austria').length).toBeGreaterThan(0);
    expect(screen.getByText('ORF1.at · 312 programmes')).toBeInTheDocument();
    expect(screen.getByText('Now: Zeit im Bild')).toBeInTheDocument();
  });

  it('says when a guide a channel uses really holds nothing', async () => {
    API.getChannelManagerGuides.mockResolvedValue({
      guides: [
        held,
        { ...guide, id: 8, name: 'ORF 1 elsewhere', programmes: 0, now: '', in_use: true },
      ],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );

    expect(await screen.findByText('ORF1.at · no programmes')).toBeInTheDocument();
    expect(screen.getByText('Nothing on it now')).toBeInTheDocument();
    // and the one it has says what it holds, next to it -- in the window, not the row
    const window_ = within(screen.getByRole('dialog'));
    expect(window_.getByText('ORF1.at · 40 programmes')).toBeInTheDocument();
    expect(window_.getByText('Now: Bundesland heute')).toBeInTheDocument();
  });

  it('keeps every candidate on the list, and what the channel has, while searching', async () => {
    API.getChannelManagerGuides.mockResolvedValue({
      guides: [held, guide, { ...guide, id: 8, name: 'ORF Eins', score: 62 }],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );
    expect(await screen.findByLabelText('Guide ORF Eins')).toBeInTheDocument();

    // A search that finds only one still leaves the guide the channel has to go back to
    API.getChannelManagerGuides.mockResolvedValue({
      guides: [held, { ...guide, id: 8, name: 'ORF Eins' }],
    });
    fireEvent.change(screen.getByLabelText('Search every guide'), {
      target: { value: 'Eins' },
    });
    await waitFor(() =>
      expect(API.getChannelManagerGuides).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: 'Eins' })
      )
    );
    expect(await screen.findByLabelText('Guide ORF 1')).toBeInTheDocument();
  });

  it('chooses a guide by hand and sends it with the row', async () => {
    API.getChannelManagerGuides.mockResolvedValue({
      guides: [held, { ...guide, id: 7, name: 'ORF 1 Austria' }],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );
    fireEvent.click(await screen.findByLabelText('Guide ORF 1 Austria'));

    expect(
      await screen.findByText('Guide: ORF 1 Austria · Austria (chosen)')
    ).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['ch:1'], {}, {}, {}, {}, { 'ch:1': 7 }, {}
      )
    );
  });

  it('takes a guide off again by choosing no guide', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );
    fireEvent.click(await screen.findByLabelText('Guide none'));

    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['ch:1'], {}, {}, {}, {}, { 'ch:1': null }, {}
      )
    );
  });

  it('reads a guide\'s programmes on request, rather than calling it empty', async () => {
    const unread = { ...guide, id: 8, name: 'ORF 1 elsewhere', programmes: 0, now: '', in_use: false };
    API.getChannelManagerGuides.mockResolvedValue({ guides: [held, unread] });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );

    // Nothing uses it and it holds nothing, so it has never been read -- not empty
    await screen.findByLabelText('Guide ORF 1 elsewhere');
    expect(screen.getByText('ORF1.at · programmes not read yet')).toBeInTheDocument();

    API.getChannelManagerGuides.mockResolvedValue({
      guides: [held, { ...unread, programmes: 120, now: 'Bundesliga' }],
    });
    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Read the programmes of ORF 1 elsewhere',
      })
    );
    await waitFor(() => expect(API.loadChannelManagerGuide).toHaveBeenCalledWith([8]));

    // They arrive as a task, so the list is asked again until they show up
    expect(
      await screen.findByText('ORF1.at · 120 programmes', undefined, { timeout: 5000 })
    ).toBeInTheDocument();
    expect(screen.getByText('Now: Bundesliga')).toBeInTheDocument();
  });

  it('reads every unread guide at once, which costs one pass of the file', async () => {
    const first = { ...guide, id: 8, name: 'ORF 1 elsewhere', programmes: 0, now: '', in_use: false };
    const second = { ...guide, id: 9, name: 'ORF Eins', programmes: 0, now: '', in_use: false };
    API.getChannelManagerGuides.mockResolvedValue({ guides: [held, first, second] });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );

    // The one the channel has is read already, so it is not among them
    await screen.findByLabelText('Guide ORF Eins');
    const all = screen.getByRole('button', {
      name: 'Read the programmes of every guide shown',
    });
    expect(all).toHaveTextContent('Read all 2');

    API.getChannelManagerGuides.mockResolvedValue({
      guides: [
        held,
        { ...first, programmes: 120, now: 'Bundesliga' },
        { ...second, programmes: 90, now: 'Wetter' },
      ],
    });
    fireEvent.click(all);
    await waitFor(() =>
      expect(API.loadChannelManagerGuide).toHaveBeenCalledWith([8, 9])
    );
    expect(
      await screen.findByText('ORF1.at · 120 programmes', undefined, { timeout: 6000 })
    ).toBeInTheDocument();
    expect(screen.getByText('ORF1.at · 90 programmes')).toBeInTheDocument();
  });

  it('says on the left when a channel is on no guide yet', async () => {
    API.previewChannelManager.mockResolvedValue({
      ...plan,
      rows: [
        {
          ...mergeRow,
          before: { ...mergeRow.before, channel: { ...mergeRow.before.channel, epg: null } },
        },
      ],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    expect(screen.getByText('No guide')).toBeInTheDocument();
    expect(screen.getByText('Guide: ORF 1 · Austria')).toBeInTheDocument();
  });

  it('says what is on the guide a channel is on, not just its name', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    // A name can be right and the guide still be somebody else's, or empty
    expect(screen.getByText('Now: Bundesland heute')).toBeInTheDocument();
  });

  it('warns on a guide that holds no programmes at all', async () => {
    API.previewChannelManager.mockResolvedValue({
      ...plan,
      rows: [
        {
          ...mergeRow,
          before: {
            ...mergeRow.before,
            channel: {
              ...mergeRow.before.channel,
              epg: { ...mergeRow.before.channel.epg, programmes: 0, now: '' },
            },
          },
        },
      ],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    expect(screen.getByText('Holds no programmes')).toBeInTheDocument();
  });

  it('narrows to one group, new channels counting under the group they would go into', async () => {
    const german = {
      ...mergeRow,
      key: 'ch:2',
      channel: { ...mergeRow.channel, id: 2, name: '┃DE┃ ARD', group: '┃DE┃ GERMANY', group_id: 2 },
      before: {
        ...mergeRow.before,
        channel: { ...mergeRow.before.channel, id: 2, name: '┃DE┃ ARD', group_id: 2 },
      },
    };
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [mergeRow, german] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    expect(screen.getAllByText('┃DE┃ ARD').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('textbox', { name: 'Which group' }));
    fireEvent.click(await screen.findByText('┃AT┃ AUSTRIA'));

    await waitFor(() => expect(screen.queryByText('┃DE┃ ARD')).toBeNull());
    expect(screen.getAllByText('┃AT┃ ORF 1').length).toBeGreaterThan(0);
  });

  it('shows which group each channel is in, on both sides', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    // The point of combining is to end with one channel in one group, which you cannot
    // judge without seeing where they are now
    expect(screen.getAllByText('1 · ┃AT┃ AUSTRIA').length).toBeGreaterThan(0);
  });

  it('names the channels a combine would delete, before it is applied', async () => {
    const combineRow = {
      ...mergeRow,
      key: 'combine:at:orf1',
      status: 'combine',
      group_why: 'where most of your at channels are',
      combining: [
        { id: 2, name: '┃AT┃ ORF 1', number: 300, group: '┃AT┃ NEWS', group_id: 3 },
      ],
    };
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [combineRow] });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    expect(screen.getByText('Combine')).toBeInTheDocument();
    expect(screen.getByText('┃AT┃ ORF 1 (300) is deleted')).toBeInTheDocument();
  });

  it('lets the group be chosen on a combine row, which decides what is kept', async () => {
    const combineRow = {
      ...mergeRow,
      key: 'combine:at:orf1',
      status: 'combine',
      group_why: 'where most of your at channels are',
      combining: [{ id: 2, name: '┃AT┃ ORF 1', number: 300, group: '┃AT┃ NEWS', group_id: 3 }],
    };
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts: [], stream_groups: [],
      profiles: [],
      channel_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA', count: 20, kind: 'with_channels' },
        { id: 3, name: '┃AT┃ NEWS', count: 4, kind: 'with_channels' },
      ],
      all_groups: [{ id: 1, name: '┃AT┃ AUSTRIA' }, { id: 3, name: '┃AT┃ NEWS' }],
    });
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [combineRow] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    expect(
      await screen.findByText(/is kept/)
    ).toBeInTheDocument();

    await pickOption('Channel group for ┃AT┃ ORF 1', '┃AT┃ NEWS');
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['combine:at:orf1'], {}, { 'combine:at:orf1': 3 }, {}, {}, {}, {}
      )
    );
  });

  it('lists every group you have channels in, and says why one is empty', async () => {
    // A group the levers are not looking at used to vanish from this filter altogether:
    // the list was made from the plan, and a group that is not in the plan has no rows to
    // make it from. A group of channels that plainly exists could not be found here.
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality', channel_groups: [1] },
      defaults: {}, accounts: [], stream_groups: [], profiles: [],
      channel_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA', count: 20, kind: 'with_channels' },
        { id: 5, name: 'Cooking', count: 6, kind: 'with_channels' },
      ],
      all_groups: [{ id: 1, name: '┃AT┃ AUSTRIA' }, { id: 5, name: 'Cooking' }],
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');

    await pickOption('Which group', 'Cooking');

    expect(
      await screen.findByText(/not one the levers are looking at/)
    ).toBeInTheDocument();
  });

  it('offers only the groups you have channels in, and a way to make one', async () => {
    const newRow = {
      key: 'new:at:puls4', status: 'new', adds: 1, removes: 0, changes: [], country: 'at',
      channel: {
        id: null, name: '┃AT┃ PULS 4', number: 12, group: '┃AT┃ AUSTRIA', group_id: 1,
        group_why: 'where your channels are', logo_url: '', epg: null,
      },
      before: { channel: null, streams: [stream(6, '┃AT┃ PULS 4 HD')] },
      streams: [stream(6, '┃AT┃ PULS 4 HD', { added: true }), fallback],
    };
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts: [], stream_groups: [],
      profiles: [],
      // Every group there is runs to hundreds, most of them a provider's own names
      channel_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA', count: 20, kind: 'with_channels' },
        { id: 99, name: 'A provider group no channel of yours is in', kind: 'active_m3u' },
      ],
      all_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA' },
        { id: 99, name: 'A provider group no channel of yours is in' },
      ],
    });
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [newRow] });
    API.addChannelGroup.mockResolvedValue({ id: 7, name: '┃AT┃ KIDS' });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findAllByText('┃AT┃ PULS 4');
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));

    fireEvent.click(
      await screen.findByRole('textbox', { name: 'Channel group for ┃AT┃ PULS 4' })
    );
    expect((await screen.findAllByText('┃AT┃ AUSTRIA')).length).toBeGreaterThan(0);
    expect(
      screen.queryByText('A provider group no channel of yours is in')
    ).toBeNull();

    // The last entry makes one, rather than leaving the page to go and make it
    fireEvent.click(screen.getByText('+ A new group…'));
    fireEvent.change(await screen.findByLabelText('Name for the new group'), {
      target: { value: '┃AT┃ KIDS' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Make it' }));

    await waitFor(() =>
      expect(API.addChannelGroup).toHaveBeenCalledWith({ name: '┃AT┃ KIDS' })
    );
    expect(await screen.findByText('new number · ┃AT┃ KIDS')).toBeInTheDocument();
  });

  it('chooses a group of that name that is already there, rather than refusing', async () => {
    const newRow = {
      key: 'new:at:puls4', status: 'new', adds: 1, removes: 0, changes: [], country: 'at',
      channel: {
        id: null, name: '┃AT┃ PULS 4', number: 12, group: '┃AT┃ AUSTRIA', group_id: 1,
        group_why: 'where your channels are', logo_url: '', epg: null,
      },
      before: { channel: null, streams: [stream(6, '┃AT┃ PULS 4 HD')] },
      streams: [stream(6, '┃AT┃ PULS 4 HD', { added: true }), fallback],
    };
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts: [], stream_groups: [],
      profiles: [],
      channel_groups: [{ id: 1, name: '┃AT┃ AUSTRIA', count: 20, kind: 'with_channels' }],
      // It exists, it simply has no channels in it yet
      all_groups: [{ id: 1, name: '┃AT┃ AUSTRIA' }, { id: 5, name: '┃AT┃ KIDS' }],
    });
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [newRow] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findAllByText('┃AT┃ PULS 4');
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    fireEvent.click(
      await screen.findByRole('textbox', { name: 'Channel group for ┃AT┃ PULS 4' })
    );
    fireEvent.click(screen.getByText('+ A new group…'));
    fireEvent.change(await screen.findByLabelText('Name for the new group'), {
      target: { value: '┃AT┃ KIDS' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Make it' }));

    // Making one is what was asked for; having that group is what was meant
    expect(await screen.findByText('new number · ┃AT┃ KIDS')).toBeInTheDocument();
    expect(API.addChannelGroup).not.toHaveBeenCalled();
  });

  it('says what the reading of guides is doing, rather than only spinning', async () => {
    const unread = {
      id: 8, name: 'ORF 1', tvg_id: 'ORF1.at', source: 'Austria', how: 'name', score: 96,
      programmes: 0, now: '', in_use: false,
    };
    API.getChannelManagerGuides.mockResolvedValue({ guides: [held, unread] });
    API.getChannelManagerReading.mockResolvedValue({
      reading: {
        reading: true, state: 'reading', stage: 'going through xmltv.at',
        at: 'ORF 1', done: 1, total: 4,
      },
    });
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(
      await screen.findByRole('button', { name: 'Change the guide for ┃AT┃ ORF 1' })
    );
    fireEvent.click(
      await screen.findByRole('button', { name: 'Read the programmes of ORF 1' })
    );

    expect(
      await screen.findByText(/going through xmltv.at/, undefined, { timeout: 6000 })
    ).toBeInTheDocument();
    // The reading loop waits three seconds before it asks the first time, so this test
    // cannot finish inside vitest's five. It waited six and was given five, which passed
    // on a quiet machine and timed out under a full suite -- a flake that was never the
    // page's fault.
  }, 15000);

  it("offers a provider's groups when the levers ask for them", async () => {
    const newRow = {
      key: 'new:at:puls4', status: 'new', adds: 1, removes: 0, changes: [], country: 'at',
      channel: {
        id: null, name: '┃AT┃ PULS 4', number: 12, group: '┃AT┃ AUSTRIA', group_id: 1,
        group_why: 'where your channels are', logo_url: '', epg: null,
      },
      before: { channel: null, streams: [stream(6, '┃AT┃ PULS 4 HD')] },
      streams: [stream(6, '┃AT┃ PULS 4 HD', { added: true }), fallback],
    };
    API.getChannelManagerOptions.mockResolvedValue({
      // Asked for outright, so the hundreds of provider groups are offered too
      settings: { order: 'quality', group_choices: ['with_channels', 'active_m3u'] },
      defaults: {}, accounts: [], stream_groups: [], profiles: [],
      channel_groups: [
        { id: 1, name: '┃AT┃ AUSTRIA', count: 20, kind: 'with_channels' },
        { id: 99, name: 'AT | PROVIDER SPORT', kind: 'active_m3u' },
        { id: 98, name: 'AT | OLD PROVIDER', kind: 'inactive_m3u' },
      ],
      all_groups: [],
    });
    API.previewChannelManager.mockResolvedValue({ ...plan, rows: [newRow] });
    Element.prototype.scrollIntoView = vi.fn();
    draw();
    await screen.findAllByText('┃AT┃ PULS 4');
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    fireEvent.click(
      await screen.findByRole('textbox', { name: 'Channel group for ┃AT┃ PULS 4' })
    );

    expect(await screen.findByText('AT | PROVIDER SPORT')).toBeInTheDocument();
    // ...and the kind that was not asked for is still left out
    expect(screen.queryByText('AT | OLD PROVIDER')).toBeNull();
  });
});

// Three logins, three providers: a channel is short when one of them has nothing on it
describe('ChannelManagerTable, channels short of a provider', () => {
  const accounts = [
    { id: 1, name: 'Provider A', active: true },
    { id: 2, name: 'Provider B', active: true },
    { id: 3, name: 'Provider C', active: true },
    { id: 4, name: 'Switched off', active: false },
  ];
  const a = (id, name, extra = {}) => stream(id, name, { account_id: 1, ...extra });
  const shortRow = {
    ...mergeRow,
    before: { ...mergeRow.before, streams: [a(1, '┃AT┃ ORF 1'), fallback] },
    streams: [
      a(1, '┃AT┃ ORF 1'),
      a(2, '┃AT┃ ORF 1 FHD', { added: true, quality: 'FHD', account: 'Provider B', account_id: 2 }),
      fallback,
    ],
  };
  const fullRow = {
    key: 'ch:2', status: 'unchanged', adds: 0, removes: 0, changes: [], country: 'at',
    channel: { id: 2, name: '┃AT┃ ORF 2', number: 2, group: '┃AT┃ AUSTRIA', group_id: 1, logo_url: '', epg: null },
    before: {
      channel: { id: 2, name: '┃AT┃ ORF 2', number: 2, group: '┃AT┃ AUSTRIA', group_id: 1, logo_url: '', epg: null },
      streams: [],
    },
    streams: [
      a(11, '┃AT┃ ORF 2'),
      a(12, '┃AT┃ ORF 2 HD', { account: 'Provider B', account_id: 2 }),
      a(13, 'AT: ORF2', { account: 'Provider C', account_id: 3 }),
      fallback,
    ],
  };
  const found = {
    id: 21, name: 'AT: ORF1 HD', hash: 'hash-21', account: 'Provider C', account_id: 3,
    group: 'AT | AUSTRIA', quality: 'HD', probed: false, tvg_id: '', logo_url: '', custom: false,
    in_scope: true, stale: false, channels: [{ id: 9, name: 'ORF 1 Old', number: 90 }],
    check: { state: 'ok', at: '2026-09-25T10:00:00Z' },
  };

  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    API.getChannelManagerOptions.mockResolvedValue({
      settings: { order: 'quality' }, defaults: {}, accounts, stream_groups: [],
      channel_groups: [], all_groups: [], profiles: [],
    });
    API.previewChannelManager.mockResolvedValue({
      summary: { streams: 6, merge: 1, new: 0, conflict: 0, unchanged: 1, streams_added: 1 },
      rows: [shortRow, fullRow],
    });
    API.saveChannelManagerSettings.mockResolvedValue({});
    API.applyChannelManager.mockResolvedValue({ created: 0, updated: 1, streams_added: 1 });
    API.getChannelManagerGuides.mockResolvedValue({ guides: [] });
    API.getGuideMatching.mockResolvedValue({ matching: null, sources: [] });
    API.getChannelManagerReading.mockResolvedValue({ reading: {} });
    API.searchChannelManagerStreams.mockResolvedValue({ streams: [found] });
  });

  afterEach(() => vi.clearAllMocks());

  it('does not count the fallback as a provider', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    // Before: its own stream and the fallback -- two streams, one provider
    expect(screen.getByText('2 streams · 1 provider')).toBeInTheDocument();
    expect(screen.getByText('2 of 3 providers · missing Provider C')).toBeInTheDocument();
  });

  it('shows the channels with fewer providers than asked, from every channel', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    // "What would change" leaves the full, unchanged channel out; the filter looks at all
    fireEvent.change(screen.getByLabelText('Fewer providers than'), { target: { value: '3' } });
    await waitFor(() => expect(screen.queryAllByText('┃AT┃ ORF 2')).toHaveLength(0));
    expect(screen.getAllByText('┃AT┃ ORF 1').length).toBeGreaterThan(0);

    fireEvent.change(screen.getByLabelText('Fewer providers than'), { target: { value: '2' } });
    await waitFor(() => expect(screen.queryAllByText('┃AT┃ ORF 1')).toHaveLength(0));
  });

  it('shows the channels missing one provider in particular', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    await pickOption('Missing provider', 'Missing Provider C');
    await waitFor(() => expect(screen.queryAllByText('┃AT┃ ORF 2')).toHaveLength(0));
    expect(screen.getAllByText('┃AT┃ ORF 1').length).toBeGreaterThan(0);
  });

  it('finds a stream for the missing provider and puts it on, before the fallback', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Put a stream on ┃AT┃ ORF 1' }));

    // The search starts from the channel's name without its country box, and looks only
    // at the provider the channel has nothing from
    expect(await screen.findByDisplayValue('ORF 1')).toBeInTheDocument();
    await waitFor(() =>
      expect(API.searchChannelManagerStreams).toHaveBeenCalledWith({
        q: 'ORF 1', accounts: ['3'], leave_out: ['1', '2', '9'], name: '┃AT┃ ORF 1',
      })
    );
    expect(await screen.findByText('AT: ORF1 HD')).toBeInTheDocument();
    expect(screen.getByText('Already on ORF 1 Old (90)')).toBeInTheDocument();
    expect(screen.getByText('plays')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Put on AT: ORF1 HD' }));
    expect(await screen.findByRole('button', { name: 'Take off AT: ORF1 HD' })).toBeInTheDocument();
    expect(screen.getByText('+1 by hand')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['ch:1'], {}, {}, {}, {}, {}, { 'ch:1': [21] }
      )
    );
  });

  it('keeps a stream put on in the order set by hand', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Put a stream on ┃AT┃ ORF 1' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Put on AT: ORF1 HD' }));
    fireEvent.click(screen.getByRole('button', { name: 'Done putting streams on' }));

    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(await screen.findByLabelText('Move AT: ORF1 HD up'));
    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenLastCalledWith(
        { order: 'quality' }, ['ch:1'], { 'ch:1': [1, 21, 2] }, {}, {}, {}, {}, { 'ch:1': [21] }
      )
    );
  });

  it('takes a stream put on off again with the row\'s own button, and out of its order', async () => {
    draw();
    await screen.findAllByText('┃AT┃ ORF 1');
    fireEvent.click(screen.getByRole('button', { name: 'Put a stream on ┃AT┃ ORF 1' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Put on AT: ORF1 HD' }));
    fireEvent.click(screen.getByRole('button', { name: 'Done putting streams on' }));

    fireEvent.click(rowOf('┃AT┃ ORF 1').querySelector('.td:nth-child(2) > div > div'));
    fireEvent.click(await screen.findByLabelText('Move AT: ORF1 HD up'));
    fireEvent.click(screen.getByLabelText('Drop AT: ORF1 HD'));
    expect(screen.queryByText('+1 by hand')).not.toBeInTheDocument();
    expect(screen.queryByText('+ AT: ORF1 HD')).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: /Apply \(1\)/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }));
    await waitFor(() =>
      expect(API.applyChannelManager).toHaveBeenCalledWith(
        { order: 'quality' }, ['ch:1'], { 'ch:1': [1, 2] }, {}, {}, {}, {}, {}
      )
    );
  });
});
