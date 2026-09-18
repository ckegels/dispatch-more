// The Channel Manager page around its two tabs: the warning to make a backup first, since
// both change many channels at once and there is no undo.
import { render, screen } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import theme from '../../mantineTheme';
import ChannelManagerPage from '../ChannelManager.jsx';

vi.mock('../../components/tables/ChannelManagerTable', () => ({ default: () => <div>merge tab</div> }));
vi.mock('../../components/tables/StreamCheckTable', () => ({ default: () => <div>check tab</div> }));

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
});
