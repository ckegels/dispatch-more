// The Subtitles tab: what each channel carries, each stream's own result, and its language.
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
import SubtitlesTable from '../SubtitlesTable.jsx';
import API from '../../../api';

// Mantine's dropdown scrolls to its option; jsdom has no scrolling
Element.prototype.scrollIntoView = vi.fn();

vi.mock('../../../api', () => ({
  default: { getSubtitles: vi.fn(), setSpokenLanguage: vi.fn() },
}));

const page = {
  rows: [
    {
      id: 1,
      uuid: 'a',
      name: '┃NL┃ NPO 1',
      number: 1,
      group: 'NL',
      group_id: 5,
      spoken: 'dut',
      spoken_from: 'audio',
      state: 'found',
      subtitles: [{ kind: 'teletext', lang: 'dut', hearing_impaired: false }],
      streams: [
        {
          id: 11,
          name: 'NPO 1 HD',
          account: 'TiviBridge2',
          checked: true,
          checked_at: '',
          subtitles: [
            { kind: 'teletext', lang: 'dut', hearing_impaired: false },
          ],
          audio_languages: ['dut'],
        },
        {
          id: 12,
          name: 'NPO 1 SD',
          account: 'Digitalizard',
          checked: false,
          checked_at: '',
          subtitles: [],
          audio_languages: [],
        },
      ],
    },
    {
      id: 2,
      uuid: 'b',
      name: '┃BE┃ VRT 1',
      number: 2,
      group: 'BE',
      group_id: 6,
      spoken: 'dut',
      spoken_from: 'country',
      state: 'none',
      subtitles: [],
      streams: [],
    },
  ],
  summary: {
    channels: 2,
    found: 1,
    none: 1,
    unchecked: 0,
    teletext: 1,
    dvb: 0,
    cc: 0,
    text: 0,
  },
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <SubtitlesTable />
    </MantineProvider>
  );

describe('SubtitlesTable', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    API.getSubtitles.mockResolvedValue(page);
    API.setSpokenLanguage.mockResolvedValue(page);
  });

  it('shows what each channel carries', async () => {
    draw();
    expect(await screen.findByText('┃NL┃ NPO 1')).toBeInTheDocument();
    expect(screen.getByText('Teletext dut')).toBeInTheDocument();
    expect(
      within(screen.getByRole('table')).getByText('None found')
    ).toBeInTheDocument();
    expect(
      screen.getByText(/1 carry subtitles \(1 teletext/)
    ).toBeInTheDocument();
  });

  it("opens a row to each stream's own result", async () => {
    draw();
    fireEvent.click(await screen.findByLabelText('Streams of ┃NL┃ NPO 1'));
    expect(await screen.findByText('not checked')).toBeInTheDocument();
    expect(screen.getByText(/sound Dutch/)).toBeInTheDocument();
  });

  it('narrows to the channels without subtitles', async () => {
    draw();
    await screen.findByText('┃NL┃ NPO 1');
    const field = screen.getByRole('textbox', { name: 'What it carries' });
    fireEvent.click(field);
    const dropdown = await waitFor(() => {
      const found = document.getElementById(
        field.getAttribute('aria-controls')
      );
      expect(found).not.toBeNull();
      return found;
    });
    fireEvent.click(within(dropdown).getByText('None found'));
    await waitFor(() =>
      expect(screen.queryByText('┃NL┃ NPO 1')).not.toBeInTheDocument()
    );
    expect(screen.getByText('┃BE┃ VRT 1')).toBeInTheDocument();
  });
});
