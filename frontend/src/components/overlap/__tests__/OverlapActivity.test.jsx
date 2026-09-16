import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import OverlapActivity from '../OverlapActivity.jsx';

vi.mock('../../../api', () => ({
  default: { getOverlapActivity: vi.fn(), setOverlapRetention: vi.fn() },
}));

// Mantine components are rendered as plain elements, like the other settings tests
vi.mock('@mantine/core', () => {
  const Table = ({ children }) => <table>{children}</table>;
  Table.Thead = ({ children }) => <thead>{children}</thead>;
  Table.Tbody = ({ children }) => <tbody>{children}</tbody>;
  Table.Tr = ({ children }) => <tr>{children}</tr>;
  Table.Th = ({ children }) => <th>{children}</th>;
  Table.Td = ({ children }) => <td>{children}</td>;
  return {
    Alert: ({ children }) => <div role="alert">{children}</div>,
    Badge: ({ children }) => <span>{children}</span>,
    Button: ({ children, onClick }) => (
      <button onClick={onClick}>{children}</button>
    ),
    Group: ({ children }) => <div>{children}</div>,
    Loader: () => <div>loading</div>,
    Modal: ({ opened, title, children }) =>
      opened ? (
        <div role="dialog">
          <span>{title}</span>
          {children}
        </div>
      ) : null,
    Select: ({ label, value, onChange, data, ...rest }) => (
      <select
        aria-label={rest['aria-label'] || label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {data.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    ),
    Stack: ({ children }) => <div>{children}</div>,
    Table,
    Text: ({ children }) => <span>{children}</span>,
  };
});

import API from '../../../api';

const activity = {
  enabled: true,
  keep_seconds: 1800,
  keep_choices: [1800, 7200, 21600, 86400],
  accounts: [
    {
      account: 'TiviBridge A',
      profile: 'Default',
      in_use: 1,
      max_streams: 1,
      held_slots: 1,
      lan_subnets: ['192.168.2.0/24'],
      stop_skipped: true,
      switch_preference: 'order',
    },
  ],
  events: [
    {
      time: 1789500000,
      viewer: '192.168.2.232 · TiviMate',
      from_channel: 'CNN',
      channel: 'Sky Sports',
      account: 'TiviBridge A',
      action: 'overlap slot',
      result: 'confirmed after 1.3s',
    },
  ],
};

describe('OverlapActivity', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    API.getOverlapActivity.mockResolvedValue(activity);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('shows the accounts and the recent switches', async () => {
    render(<OverlapActivity active={true} />);

    expect(await screen.findByText('TiviBridge A')).toBeInTheDocument();
    expect(screen.getByText('1/1 in use')).toBeInTheDocument();
    expect(screen.getByText('1 held')).toBeInTheDocument();
    expect(screen.getByText('192.168.2.232 · TiviMate')).toBeInTheDocument();
    // One cell reads "CNN → Sky Sports"
    expect(
      screen.getByText(
        (_text, element) =>
          element?.tagName === 'TD' &&
          element.textContent === 'CNN → Sky Sports'
      )
    ).toBeInTheDocument();
    expect(screen.getByText('overlap slot')).toBeInTheDocument();
    expect(screen.getByText('confirmed after 1.3s')).toBeInTheDocument();
  });

  it('refreshes by itself and stops when the page is left', async () => {
    const { unmount } = render(<OverlapActivity active={true} />);
    await screen.findByText('TiviBridge A');

    await vi.advanceTimersByTimeAsync(5000);
    expect(API.getOverlapActivity).toHaveBeenCalledTimes(2);

    unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(API.getOverlapActivity).toHaveBeenCalledTimes(2);
  });

  it('says so when the overlap is not enabled anywhere', async () => {
    API.getOverlapActivity.mockResolvedValue({
      enabled: false,
      accounts: [],
      events: [],
      keep_seconds: 1800,
      keep_choices: [1800, 7200, 21600, 86400],
    });

    render(<OverlapActivity active={true} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'not enabled on any M3U account'
    );
  });

  it('shows a message when nothing happened recently', async () => {
    API.getOverlapActivity.mockResolvedValue({ ...activity, events: [] });

    render(<OverlapActivity active={true} />);

    expect(
      await screen.findByText('No channel switches in the last 30 minutes.')
    ).toBeInTheDocument();
  });

  it('changes how long switches are kept', async () => {
    API.setOverlapRetention.mockResolvedValue({
      ...activity,
      keep_seconds: 7200,
    });
    render(<OverlapActivity active={true} />);
    await screen.findByText('TiviBridge A');

    fireEvent.change(screen.getByLabelText('Keep switches for'), {
      target: { value: '7200' },
    });

    await waitFor(() =>
      expect(API.setOverlapRetention).toHaveBeenCalledWith(7200)
    );
    expect(await screen.findByLabelText('Keep switches for')).toHaveValue(
      '7200'
    );
  });

  it('explains the labels in a popup', async () => {
    render(<OverlapActivity active={true} />);
    await screen.findByText('TiviBridge A');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: /what do these mean/i })
    );

    const legend = await screen.findByRole('dialog');
    expect(legend).toHaveTextContent('What this page shows');
    expect(legend).toHaveTextContent('skipped while surfing');
    expect(legend).toHaveTextContent(
      'moved on to another channel during the Surfing Delay'
    );
  });

  it('shows an error when the activity cannot be loaded', async () => {
    API.getOverlapActivity.mockRejectedValue(new Error('nope'));

    render(<OverlapActivity active={true} />);

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Could not load the activity.'
      )
    );
  });
});
