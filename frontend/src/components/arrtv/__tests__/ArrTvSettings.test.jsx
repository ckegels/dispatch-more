// With the real Mantine: the switches, their order of dependence, and the reports under them
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import ArrTvSettings from '../ArrTvSettings.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: {
    getArrTvSettings: vi.fn(),
    saveArrTvSettings: vi.fn(),
    getAppReports: vi.fn(),
    getAppReport: vi.fn(),
    deleteAppReport: vi.fn(),
  },
}));

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <ArrTvSettings />
    </MantineProvider>
  );

describe('ArrTvSettings', () => {
  afterEach(() => vi.clearAllMocks());

  it('is all off at first, and the channel change needs the devices', async () => {
    API.getArrTvSettings.mockResolvedValue({ devices: false, switch_hints: false, reports: false });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.saveArrTvSettings.mockResolvedValue({ devices: true, switch_hints: false, reports: false });
    draw();

    // In the order they are shown: devices, the channel change under it, reports
    await screen.findByText('Recognise each arrTV device');
    const [devices, hints] = screen.getAllByRole('switch');
    expect(devices).not.toBeChecked();
    expect(hints).toBeDisabled();
    expect(screen.getByText(/Needs "Recognise each arrTV device"/)).toBeInTheDocument();

    fireEvent.click(devices);
    await waitFor(() =>
      expect(API.saveArrTvSettings).toHaveBeenCalledWith({ devices: true, switch_hints: false, reports: false })
    );
    await waitFor(() => expect(hints).not.toBeDisabled());
  });

  it('saves the home networks when the field is left, and says when one is not a network', async () => {
    API.getArrTvSettings.mockResolvedValue({
      devices: false, switch_hints: false, reports: false, home_networks: '', outside_max_quality: 'HD',
    });
    API.getAppReports.mockResolvedValue({ reports: [] });
    API.saveArrTvSettings.mockRejectedValueOnce({ body: { error: 'nope is not a valid IP address or subnet' } });
    draw();
    const field = await screen.findByPlaceholderText('192.168.2.0/24');
    fireEvent.change(field, { target: { value: 'nope' } });
    expect(API.saveArrTvSettings).not.toHaveBeenCalled();
    fireEvent.blur(field);
    await waitFor(() =>
      expect(API.saveArrTvSettings).toHaveBeenCalledWith(expect.objectContaining({ home_networks: 'nope' }))
    );
    expect(await screen.findByText('nope is not a valid IP address or subnet')).toBeInTheDocument();
  });

  it('lists the reports arrTV sent and opens one whole', async () => {
    API.getArrTvSettings.mockResolvedValue({ devices: true, switch_hints: false, reports: true });
    API.getAppReports.mockResolvedValue({
      reports: [{
        id: 'r1', received_at: 1790000000, user: 'tv', device_name: 'Living room SHIELD',
        what: 'Picture froze', channel: '┃AT┃ ORF 1', error: 'HttpDataSourceException 503',
      }],
    });
    API.getAppReport.mockResolvedValue({
      id: 'r1', what: 'Picture froze', app: { name: 'arrTV' }, player: { state: 'BUFFERING' },
      server: { channel: { name: '┃AT┃ ORF 1', streams: [] }, log: ['a server line'] },
      log: 'an app line',
    });
    draw();

    expect(await screen.findByText('┃AT┃ ORF 1 — Picture froze')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open report r1' }));
    expect(await screen.findByText('a server line')).toBeInTheDocument();
    expect(screen.getByText('an app line')).toBeInTheDocument();
  });

  it('asks before deleting every report, since they are kept until deleted', async () => {
    API.getArrTvSettings.mockResolvedValue({ devices: true, switch_hints: false, reports: true });
    API.getAppReports.mockResolvedValue({
      reports: [
        { id: 'r1', received_at: 1790000000, what: 'Picture froze', channel: 'A', error: '' },
        { id: 'r2', received_at: 1790000100, what: 'No sound', channel: 'B', error: '' },
      ],
    });
    API.deleteAppReport.mockResolvedValue({ deleted: 'all' });
    draw();

    fireEvent.click(await screen.findByRole('button', { name: 'Delete all reports' }));
    expect(API.deleteAppReport).not.toHaveBeenCalled();
    expect(await screen.findByText(/All 2 reports are deleted/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    await waitFor(() => expect(API.deleteAppReport).toHaveBeenCalledWith(null));
  });
});
