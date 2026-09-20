// The Channel Manager page around its tabs: the warning to make a backup first, since
// both change many channels at once and there is no undo.
import { render, screen } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import theme from '../../mantineTheme';
import ChannelManagerPage from '../ChannelManager.jsx';

vi.mock('../../components/tables/ChannelManagerTable', () => ({ default: () => <div>merge tab</div> }));
vi.mock('../../components/tables/StreamCheckTable', () => ({ default: () => <div>check tab</div> }));
vi.mock('../../components/tables/GuideManagerTable', () => ({ default: () => <div>guides tab</div> }));
vi.mock('../../components/tables/LogoLibraryTable', () => ({ default: () => <div>logos tab</div> }));

describe('ChannelManagerPage', () => {
  it('asks for a backup first, with a link straight to it', () => {
    render(
      <MantineProvider theme={theme}>
        <MemoryRouter>
          <ChannelManagerPage />
        </MemoryRouter>
      </MantineProvider>
    );
    expect(screen.getByText('Make a backup before you apply anything')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Make a backup in Settings/ })).toHaveAttribute(
      'href',
      '/settings#backups'
    );
    expect(screen.getByText('merge tab')).toBeInTheDocument();
  });

  it('keeps every way of changing channels in one place', async () => {
    const { default: userEvent } = await import('@testing-library/user-event');
    render(
      <MantineProvider theme={theme}>
        <MemoryRouter>
          <ChannelManagerPage />
        </MemoryRouter>
      </MantineProvider>
    );
    // Logos came from the Logos page: a change to a channel belongs with the rest
    await userEvent.click(screen.getByRole('tab', { name: 'Logos' }));
    expect(await screen.findByText('logos tab')).toBeInTheDocument();
  });
});
