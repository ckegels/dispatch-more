import { fireEvent, render, screen } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import ChannelManagerLevers from '../ChannelManagerLevers.jsx';

const value = {
  accounts: [], stream_groups: [], channel_groups: [], target_group: null, profiles: 'all',
  match_tvg_id: true, ignore_tags: 'VIP', regex_rules: [['^AT:', '┃AT┃']],
  aliases: { 'National Geographic': ['NGC', 'Nat Geo'] }, same_country: true, order: 'quality',
  skip_stale: true, skip_custom: true, drop_sd_when_hd: false, create_new: false,
  min_streams_new: 1, keep_country_prefix: true, number_start: null, reorder_existing: false,
  replace_streams: false, epg: 'tvg_id_then_name', logo: 'collections',
};
const options = { accounts: [{ id: 1, name: 'A' }], stream_groups: [], channel_groups: [], all_groups: [], profiles: [] };

const draw = (overrides = {}, onChange = vi.fn(), resetKey = 0) => {
  const view = render(
    <MantineProvider theme={theme}>
      <ChannelManagerLevers
        options={options} value={{ ...value, ...overrides }} onChange={onChange} resetKey={resetKey}
      />
    </MantineProvider>
  );
  return Object.assign(onChange, { view });
};

// The sections open one at a time, as Stream Check's do
const open = (...titles) =>
  titles.forEach((title) => fireEvent.click(screen.getByRole('button', { name: `Open ${title}` })));

describe('ChannelManagerLevers', () => {
  it('opens a section at a time, what is looked at first', () => {
    draw();
    expect(screen.getByText(/Where streams are taken from/)).toBeInTheDocument();
    expect(screen.queryByText('Match names')).not.toBeInTheDocument();
    expect(screen.getByText('how a stream is known to be one of your channels')).toBeInTheDocument();
    open('Recognising a channel');
    expect(screen.getByText('Match names')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close Recognising a channel' }));
    expect(screen.queryByText('Match names')).not.toBeInTheDocument();
  });

  it('shows the rules as they were reset, and stays open', () => {
    const onChange = draw();
    open('Recognising a channel');
    onChange.view.rerender(
      <MantineProvider theme={theme}>
        <ChannelManagerLevers
          options={options} value={{ ...value, regex_rules: [] }} onChange={onChange} resetKey={1}
        />
      </MantineProvider>
    );
    expect(screen.getByText('Match names')).toBeInTheDocument();
    expect(screen.queryByDisplayValue(/\^AT: => ┃AT┃/)).not.toBeInTheDocument();
  });

  it('reads the country however it is written, and stations by call sign, when asked', () => {
    const onChange = draw();
    open('Recognising a channel');
    fireEvent.click(screen.getByRole('switch', { name: /The country however it is written/ }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ country_any_way: true }));
    fireEvent.click(screen.getByRole('switch', { name: /American local stations/ }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ match_call_signs: true }));
    fireEvent.click(screen.getByRole('switch', { name: /does not say is the East one/ }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ east_is_default: true }));
    fireEvent.click(screen.getByRole('switch', { name: /Leave out words like TV/ }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ leave_out_filler: true }));
  });

  it('shows rules and other names as lines of text', () => {
    draw();
    open('Recognising a channel');
    expect(screen.getByDisplayValue(/\^AT: => ┃AT┃/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/National Geographic = NGC, Nat Geo/)).toBeInTheDocument();
  });

  it('turns typed rules back into what the server wants', () => {
    const onChange = draw();
    open('Recognising a channel');
    fireEvent.change(screen.getByDisplayValue(/\^AT: => ┃AT┃/), {
      target: { value: '^BE: => ┃BE┃ \n\\s*\\(Backup\\) => ' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ regex_rules: [['^BE:', '┃BE┃'], ['\\s*\\(Backup\\)', '']] })
    );
  });

  it('turns typed names back into a map', () => {
    const onChange = draw();
    open('Recognising a channel');
    fireEvent.change(screen.getByDisplayValue(/National Geographic/), {
      target: { value: 'Eén = Een, VRT 1' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ aliases: { Eén: ['Een', 'VRT 1'] } })
    );
  });

  it('shows the settings for new channels only when making them', () => {
    draw();
    open('New channels');
    expect(screen.getByRole('switch', { name: /Suggest new channels/ })).toBeInTheDocument();
    expect(screen.queryByText('Into group')).not.toBeInTheDocument();
  });

  it('puts a new channel where its group is, with how many said', () => {
    const onChange = draw({ create_new: true, profiles: 'like_its_group' });
    open('New channels');
    expect(screen.getByDisplayValue('The ones its group is in')).toBeInTheDocument();
    const more = screen.getByLabelText('Where its group has more than');
    expect(more).toHaveValue('10');
    fireEvent.change(more, { target: { value: '4' } });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ profiles_group_more_than: 4 })
    );
  });

  it('and asks for no number for the other choices', () => {
    draw({ create_new: true, profiles: 'all' });
    open('New channels');
    expect(
      screen.queryByLabelText('Where its group has more than')
    ).not.toBeInTheDocument();
  });

  it('says a new provider group gives no new channels until it is picked', () => {
    draw({ create_new: true, stream_groups: [] });
    open('New channels');
    expect(
      screen.getByText(/new channels then only come from the groups your channels already use/)
    ).toBeInTheDocument();
    expect(screen.getByText(/one a provider just added/)).toBeInTheDocument();
  });

  it('warns before streams are removed', () => {
    draw({ replace_streams: true });
    open("A channel's streams");
    expect(screen.getByText(/Removed streams are shown struck through/)).toBeInTheDocument();
  });

  it('says the fallback stays last', () => {
    draw();
    open("A channel's streams");
    expect(screen.getByText(/always stays last/)).toBeInTheDocument();
  });
});
