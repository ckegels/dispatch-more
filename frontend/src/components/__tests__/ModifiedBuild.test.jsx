// Settings → System → Modified build: says what it is, and asks before going back to stock.
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, describe, expect, it, vi } from 'vitest';
import theme from '../../mantineTheme';
import ModifiedBuild from '../ModifiedBuild.jsx';
import API from '../../api';

vi.mock('../../api', () => ({
  default: { getModifiedBuild: vi.fn(), uninstallModifiedBuild: vi.fn() },
}));

const installed = {
  build: 'Dispatch More v99',
  dispatcharr_version: '0.31.0',
  installed: true,
  record: { release: 'v99', for_dispatcharr: '0.31.0', layout: 'systemd', installed_at: '2026-09-19T10:00:00+00:00' },
  uninstall_requested: false,
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <ModifiedBuild />
    </MantineProvider>
  );

describe('ModifiedBuild', () => {
  afterEach(() => vi.clearAllMocks());

  it('says it is not official, and asks before uninstalling', async () => {
    API.getModifiedBuild.mockResolvedValue(installed);
    API.uninstallModifiedBuild.mockResolvedValue({ requested: true, how: 'Stock Dispatcharr is being put back.' });
    draw();
    expect(await screen.findByText('A modified build, not official Dispatcharr')).toBeInTheDocument();
    expect(screen.getByText(/Do not report problems with this build/)).toBeInTheDocument();
    expect(screen.getByText(/Linux \/ LXC/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Uninstall and go back/ }));
    expect(API.uninstallModifiedBuild).not.toHaveBeenCalled();
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Uninstall' }));
    await waitFor(() => expect(API.uninstallModifiedBuild).toHaveBeenCalled());
    expect(await screen.findByText('Stock Dispatcharr is being put back.')).toBeInTheDocument();
  });

  it('cannot uninstall what the installer did not put there', async () => {
    API.getModifiedBuild.mockResolvedValue({ ...installed, installed: false, record: {} });
    draw();
    expect(await screen.findByRole('button', { name: /Uninstall and go back/ })).toBeDisabled();
    expect(screen.getByText(/not put here by the installer/)).toBeInTheDocument();
  });
});
