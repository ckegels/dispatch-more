import { fireEvent, render, screen } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { describe, expect, it, vi } from 'vitest';
import theme from '../../../mantineTheme';
import MemoryUse, { memoryAsText } from '../MemoryUse.jsx';
import API from '../../../api';

vi.mock('../../../api', () => ({ default: { getMemoryUse: vi.fn() } }));

describe('MemoryUse', () => {
  it('shows what each kind of process holds, and where the language model is', async () => {
    const onCopy = vi.fn();
    API.getMemoryUse.mockResolvedValue({
      total_mb: 3900,
      system: {
        total_mb: 8000,
        used_mb: 7900,
        available_mb: 100,
        percent: 98.7,
      },
      kinds: [
        { kind: 'Web worker (uWSGI)', processes: 4, mb: 2100, torch: 2 },
        { kind: 'Redis', processes: 1, mb: 90, torch: 0 },
      ],
      processes: [
        {
          pid: 10,
          kind: 'Web worker (uWSGI)',
          mb: 900,
          rss_mb: 950,
          exact: true,
          torch: true,
          command: 'uwsgi --ini',
        },
        {
          pid: 11,
          kind: 'Redis',
          mb: 90,
          rss_mb: 90,
          exact: false,
          torch: false,
          command: 'redis-server',
        },
      ],
    });
    render(
      <MantineProvider theme={theme}>
        <MemoryUse onCopy={onCopy} />
      </MantineProvider>
    );
    expect(await screen.findByText('3900 MB')).toBeInTheDocument();
    expect(screen.getByText('in 2')).toBeInTheDocument();
    expect(screen.getByText('language model')).toBeInTheDocument();
    expect(screen.getByText('(RSS)')).toBeInTheDocument();
    // Its own copy button copies the breakdown, not the channel lists
    fireEvent.click(screen.getByRole('button', { name: /Copy as text/ }));
    const text = onCopy.mock.calls[0][0];
    expect(text).toContain('Dispatcharr: 3900 MB in 2 processes');
    expect(text).toContain('Web worker (uWSGI)  (language model in 2)');
    expect(memoryAsText).toBeInstanceOf(Function);
  });
});
