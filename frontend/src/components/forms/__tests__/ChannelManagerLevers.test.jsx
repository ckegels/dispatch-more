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

const draw = (overrides = {}, onChange = vi.fn()) => {
  render(
    <MantineProvider theme={theme}>
      <ChannelManagerLevers options={options} value={{ ...value, ...overrides }} onChange={onChange} />
    </MantineProvider>
  );
  return onChange;
};

describe('ChannelManagerLevers', () => {
  it('shows rules and other names as lines of text', () => {
    draw();
    expect(screen.getByDisplayValue(/\^AT: => ┃AT┃/)).toBeInTheDocument();
    expect(screen.getByDisplayValue(/National Geographic = NGC, Nat Geo/)).toBeInTheDocument();
  });

  it('turns typed rules back into what the server wants', () => {
    const onChange = draw();
    fireEvent.change(screen.getByDisplayValue(/\^AT: => ┃AT┃/), {
      target: { value: '^BE: => ┃BE┃ \n\\s*\\(Backup\\) => ' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ regex_rules: [['^BE:', '┃BE┃'], ['\\s*\\(Backup\\)', '']] })
    );
  });

  it('turns typed names back into a map', () => {
    const onChange = draw();
    fireEvent.change(screen.getByDisplayValue(/National Geographic/), {
      target: { value: 'Eén = Een, VRT 1' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ aliases: { Eén: ['Een', 'VRT 1'] } })
    );
  });

  it('shows the settings for new channels only when making them', () => {
    draw();
    expect(screen.queryByText('Into group')).not.toBeInTheDocument();
  });

  it('warns before streams are removed', () => {
    draw({ replace_streams: true });
    expect(screen.getByText(/Removed streams are shown struck through/)).toBeInTheDocument();
  });

  it('says the fallback stays last', () => {
    draw();
    expect(screen.getByText(/always stays last/)).toBeInTheDocument();
  });
});
