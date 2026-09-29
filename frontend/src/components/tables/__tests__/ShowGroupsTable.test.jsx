// The Show Groups tab, drawn with the real Mantine: a card per group to switch on or off, a
// group of your own added, and a chosen group's channels and the ones always in it.
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import ShowGroupsTable from '../ShowGroupsTable.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getShowGroups: vi.fn(),
    saveShowGroups: vi.fn(),
    runShowGroups: vi.fn(),
    getShowGroupKinds: vi.fn(),
  },
}));

const blank = {
  title_words: '',
  title_exclusions: '',
  disqualifiers: '',
  use_title_words: false,
  use_disqualifiers: false,
  always: '',
  never: '',
  permanent: [],
  one_per_airing: true,
  in_group: [],
  coming: [],
  titles: [],
  title_count: 0,
  planned: false,
  refused: '',
  copies: 0,
};
const cooking = {
  ...blank,
  id: 'cooking',
  name: 'Cooking',
  preset: true,
  on: true,
  category_words: 'cooking',
  planned: true,
  copies: 1,
  permanent: [7],
  in_group: [
    {
      copy: 90,
      source: 5,
      name: 'TLC',
      number: 20001,
      always: false,
      viewers: 2,
      leaves: '2026-09-29T20:15:00Z',
      showing: {
        title: 'Cake Boss',
        start: '2026-09-29T19:00:00Z',
        end: '2026-09-29T19:30:00Z',
        why: 'guide category: Cooking',
      },
    },
  ],
  coming: [
    {
      source: 6,
      channel: 'PBS 13 (#513)',
      joins: '2026-09-29T20:00:00Z',
      leaves: '2026-09-29T21:45:00Z',
      title: 'Milk Street',
      start: '2026-09-29T21:00:00Z',
      end: '2026-09-29T21:30:00Z',
      why: 'guide category: Cooking',
    },
  ],
  titles: [
    {
      title: 'Cake Boss',
      airings: 3,
      taken: 3,
      why: 'guide category: Cooking',
      uncertain: false,
    },
  ],
  title_count: 1,
};
const travel = {
  ...blank,
  id: 'travel',
  name: 'Travel',
  preset: true,
  on: false,
  category_words: 'travel',
};

const page = (changes = {}) => ({
  settings: {
    live: true,
    profile_name: 'Show Groups',
    first_number: 20000,
    viewer_grace: 10,
    announce_changes: true,
    join_ahead: 60,
    leave_after: 60,
    linger: 15,
    min_length: 10,
    source_groups: [],
    plan_hours: 24,
    online_lookups: false,
    wikipedia_languages: 'en',
    tmdb_key: '',
  },
  groups: [cooking, travel],
  plan_made: '2026-09-29T18:00:00Z',
  plan_made_local: 'Tue 20:00',
  plugin: null,
  lookups: null,
  activity: ['2026-09-29 20:00  Cooking: joined  TLC (#20001): Cake Boss'],
  channels: [
    { id: 5, name: 'TLC', number: 40 },
    { id: 7, name: 'Food Network', number: 41 },
  ],
  channel_groups: [{ id: 1, name: 'US', count: 2 }],
  ...changes,
});

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <ShowGroupsTable />
    </MantineProvider>
  );

describe('ShowGroupsTable', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    API.getShowGroups.mockResolvedValue(page());
    API.saveShowGroups.mockImplementation(() => Promise.resolve(page()));
  });

  it('shows a card per group with what is in it now', async () => {
    draw();
    expect(await screen.findByText('1 in now')).toBeInTheDocument();
    expect(screen.getByText('1 always')).toBeInTheDocument();
    expect(
      screen.getByText(/On · 1 group · 1 channel in them now/)
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Travel on')).not.toBeChecked();
  });

  it('switches a group on', async () => {
    draw();
    fireEvent.click(await screen.findByLabelText('Travel on'));
    await waitFor(() => expect(API.saveShowGroups).toHaveBeenCalled());
    const sent = API.saveShowGroups.mock.calls[0][0].groups;
    expect(sent.find((one) => one.id === 'travel').on).toBe(true);
    expect(sent.find((one) => one.id === 'cooking').permanent).toEqual([7]);
  });

  it('adds a group of your own', async () => {
    draw();
    fireEvent.click(await screen.findByRole('button', { name: 'Add a group' }));
    fireEvent.change(await screen.findByLabelText('Name'), {
      target: { value: 'Formula 1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(API.saveShowGroups).toHaveBeenCalled());
    const added = API.saveShowGroups.mock.calls[0][0].groups.at(-1);
    expect(added).toMatchObject({
      id: 'my-formula-1',
      name: 'Formula 1',
      on: true,
      category_words: 'formula 1',
    });
  });

  it('shows a chosen group, and what joins next', async () => {
    draw();
    fireEvent.click(await screen.findByLabelText('Show Cooking'));
    expect(await screen.findByText('Cake Boss')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Coming up' }));
    expect(await screen.findByText('PBS 13 (#513)')).toBeInTheDocument();
  });

  it('keeps channels in a group always, picked by name', async () => {
    draw();
    fireEvent.click(await screen.findByLabelText('Show Cooking'));
    fireEvent.click(screen.getByRole('tab', { name: 'What it takes' }));
    const picker = await screen.findByText('Channels always in this group');
    expect(
      within(picker.closest('.mantine-InputWrapper-root')).getByText(
        '41 · Food Network'
      )
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Guide categories'), {
      target: { value: 'cooking, food' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(API.saveShowGroups).toHaveBeenCalled());
    const sent = API.saveShowGroups.mock.calls[0][0].groups.find(
      (one) => one.id === 'cooking'
    );
    expect(sent).toMatchObject({
      category_words: 'cooking, food',
      permanent: [7],
    });
  });

  it('offers to take the plugin over, and cannot remove while live', async () => {
    API.getShowGroups.mockResolvedValue(
      page({ plugin: { enabled: true, live: true, group: 'Cooking' } })
    );
    API.runShowGroups.mockResolvedValue({ ...page(), message: 'Took it over' });
    draw();
    expect(
      await screen.findByRole('button', { name: 'Remove everything' })
    ).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Take it over' }));
    await waitFor(() =>
      expect(API.runShowGroups).toHaveBeenCalledWith('take_over')
    );
    expect(await screen.findByText('Took it over')).toBeInTheDocument();
  });

  it('offers whole channels of the kind, and keeps the ticked ones in', async () => {
    API.getShowGroupKinds.mockResolvedValue({
      kinds: ['cooking'],
      all_kinds: ['cooking', 'travel'],
      channels: [
        {
          id: 7,
          name: 'Food Network',
          number: 41,
          group: 'US',
          hidden: false,
          kinds: ['cooking'],
          listed_as: 'Food Network (US)',
          always: true,
        },
        {
          id: 8,
          name: '┃BE┃ 24KITCHEN',
          number: 12,
          group: 'BE',
          hidden: false,
          kinds: ['cooking'],
          listed_as: '24Kitchen (US)',
          always: false,
        },
      ],
    });
    draw();
    fireEvent.click(await screen.findByLabelText('Show Cooking'));
    fireEvent.click(screen.getByRole('tab', { name: 'Whole channels' }));
    expect(await screen.findByText('┃BE┃ 24KITCHEN')).toBeInTheDocument();
    expect(API.getShowGroupKinds).toHaveBeenCalledWith('cooking');
    expect(screen.getByLabelText('Keep Food Network')).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Keep ┃BE┃ 24KITCHEN'));
    fireEvent.click(screen.getByRole('button', { name: /Keep 1 always in/ }));
    await waitFor(() => expect(API.saveShowGroups).toHaveBeenCalled());
    const sent = API.saveShowGroups.mock.calls[0][0].groups.find(
      (one) => one.id === 'cooking'
    );
    expect(sent.permanent).toEqual([7, 8]);
  });
});
