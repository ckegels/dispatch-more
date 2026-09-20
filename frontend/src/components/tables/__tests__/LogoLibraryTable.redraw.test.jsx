// Drawn with the real table and the real Mantine, unlike the other file, which stands in
// for both. That standing-in is why this bug lived: the real table only redraws a row when
// the row's own data is a different object, and a page that keeps what was chosen beside
// the rows rather than on them can change nothing at all on the screen.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import LogoLibraryTable from '../LogoLibraryTable.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getLogoLibrary: vi.fn(),
    getLogoLibraryStatus: vi.fn(),
    refreshLogoLibrary: vi.fn(),
    applyLogoLibrary: vi.fn(),
    searchLogoLibrary: vi.fn(),
    uploadLogo: vi.fn(),
  },
}));

const at = (where) => `https://logos.example/${where}.png`;

const library = {
  status: { built: true, built_at: 1, counts: {}, errors: {} },
  channels: [
    {
      channel_id: 2,
      number: 2,
      name: '┃FR┃ TFX',
      country: 'fr',
      current: { id: 9, name: 'old tfx', url: at('old') },
      suggestions: [
        { url: at('one'), name: 'tfx one', source: 'tv-logos', country: 'fr' },
        { url: at('two'), name: 'tfx two', source: 'tv-logos', country: 'be' },
        { url: at('three'), name: 'tfx three', source: 'iptv-org', country: 'fr' },
      ],
    },
  ],
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <LogoLibraryTable />
    </MantineProvider>
  );

describe('LogoLibraryTable, drawn by the real table', () => {
  beforeEach(() => {
    API.getLogoLibrary.mockResolvedValue(library);
  });

  afterEach(() => vi.clearAllMocks());

  it('goes on letting another logo be chosen, and not only the once', async () => {
    draw();
    await screen.findByText('┃FR┃ TFX');
    const main = () => screen.getByAltText('Suggested logo for ┃FR┃ TFX');
    expect(main()).toHaveAttribute('src', at('one'));

    // The first choice used to work, because choosing also ticks the row and being
    // ticked is one of the few things the table watches for
    fireEvent.click(screen.getByLabelText(/logo 2 for ┃FR┃ TFX/));
    await waitFor(() => expect(main()).toHaveAttribute('src', at('two')));

    // ...and every one after it, which is the part that did nothing
    fireEvent.click(screen.getByLabelText(/logo 3 for ┃FR┃ TFX/));
    await waitFor(() => expect(main()).toHaveAttribute('src', at('three')));

    fireEvent.click(screen.getByLabelText(/logo 1 for ┃FR┃ TFX/));
    await waitFor(() => expect(main()).toHaveAttribute('src', at('one')));

    // and it is still the one that would be applied
    expect(screen.getByRole('button', { name: /Apply \(1\)/ })).toBeEnabled();
  });
});
