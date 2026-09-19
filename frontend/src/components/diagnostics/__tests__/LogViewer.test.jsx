// Diagnostics -> Logs: the logs, narrowed to what is looked for, an error kept whole.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import LogViewer from '../LogViewer.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({
  default: { getLogSources: vi.fn(), readLogs: vi.fn(), downloadLogs: vi.fn() },
}));

const sources = {
  sources: [
    { id: 'journal:dispatcharr', label: 'Web server (uWSGI): the proxy, the page, the API', kind: 'journal' },
    { id: 'journal:dispatcharr-celery', label: 'Background tasks (Celery): refreshes, Stream Check', kind: 'journal' },
  ],
  journal_readable: true,
};
const logs = {
  total: 2,
  cut: false,
  records: [
    { time: '2026-09-19T09:21:38+0200', service: 'start-celery.sh', level: 'INFO', text: '2026-09-19 07:21:38,343 INFO apps.channels.stream_check Stream Check: batch ended' },
    {
      time: '2026-09-19T09:22:40+0200', service: 'start-uwsgi.sh', level: 'ERROR',
      text: 'Internal Server Error: /proxy/diagnostics/\nTraceback (most recent call last):\nValueError: badly formed hexadecimal UUID string',
    },
  ],
};

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <LogViewer />
    </MantineProvider>
  );

describe('LogViewer', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    API.getLogSources.mockResolvedValue(sources);
    API.readLogs.mockResolvedValue(logs);
    API.downloadLogs.mockResolvedValue();
  });
  afterEach(() => vi.clearAllMocks());

  it('shows the records, an error with its traceback to open', async () => {
    draw();
    expect(await screen.findByText(/Stream Check: batch ended/)).toBeInTheDocument();
    expect(screen.queryByText(/badly formed hexadecimal/)).toBeNull();
    fireEvent.click(screen.getByText('[+2 lines]'));
    expect(screen.getByText(/badly formed hexadecimal UUID string/)).toBeInTheDocument();
  });

  it('asks for errors only, and for what is searched', async () => {
    draw();
    await screen.findByText(/Stream Check: batch ended/);
    fireEvent.click(screen.getByText('Errors'));
    await waitFor(() =>
      expect(API.readLogs).toHaveBeenLastCalledWith(expect.objectContaining({ level: 'ERROR' }))
    );
    const search = screen.getByLabelText('Search the logs');
    fireEvent.change(search, { target: { value: 'TiviBridge' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    await waitFor(() =>
      expect(API.readLogs).toHaveBeenLastCalledWith(expect.objectContaining({ text: 'TiviBridge', level: 'ERROR' }))
    );
  });

  it('downloads all of it, and the diagnostics bundle', async () => {
    draw();
    await screen.findByText(/Stream Check: batch ended/);
    fireEvent.click(screen.getByRole('button', { name: /Download all of it/ }));
    expect(API.downloadLogs).toHaveBeenCalledWith('download', expect.objectContaining({ since: '1h' }), expect.stringMatching(/\.log$/));
    fireEvent.click(screen.getByRole('button', { name: /Diagnostics bundle/ }));
    expect(API.downloadLogs).toHaveBeenLastCalledWith('bundle', {}, expect.stringMatching(/\.zip$/));
  });

  it('says so when Dispatcharr may not read the journal', async () => {
    API.getLogSources.mockResolvedValue({ ...sources, journal_readable: false });
    draw();
    expect(await screen.findByText(/not allowed to read the system journal/)).toBeInTheDocument();
  });
});
