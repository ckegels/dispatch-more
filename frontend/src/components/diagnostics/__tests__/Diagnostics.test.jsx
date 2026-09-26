import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import Diagnostics from '../Diagnostics.jsx';

vi.mock('../../../api', () => ({
  default: {
    getDiagnostics: vi.fn(),
    setDiagnosticsRetention: vi.fn(),
    setAppIntegration: vi.fn(),
  },
}));

// Mantine components are rendered as plain elements, like the other settings tests
vi.mock('@mantine/core', () => {
  const Table = ({ children }) => <table>{children}</table>;
  Table.Thead = ({ children }) => <thead>{children}</thead>;
  Table.Tbody = ({ children }) => <tbody>{children}</tbody>;
  Table.Tr = ({ children }) => <tr>{children}</tr>;
  Table.Th = ({ children }) => <th>{children}</th>;
  Table.Td = ({ children }) => <td>{children}</td>;
  Table.ScrollContainer = ({ children }) => <div>{children}</div>;

  return {
    ActionIcon: ({ children, onClick, ...rest }) => (
      <button aria-label={rest['aria-label']} onClick={onClick}>
        {children}
      </button>
    ),
    Alert: ({ children }) => <div role="alert">{children}</div>,
    Switch: ({ label, checked, onChange }) => (
      <label>
        <input type="checkbox" role="switch" checked={checked} onChange={onChange} />
        {label}
      </label>
    ),
    Badge: ({ children }) => <span>{children}</span>,
    Box: (props) => <div {...props} />,
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
    SegmentedControl: ({ value, onChange, data, ...rest }) => (
      <div role="group" aria-label={rest['aria-label']}>
        {data.map((option) => (
          <button
            key={option.value}
            aria-pressed={value === option.value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    ),
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
    Tooltip: ({ label, children }) => <div title={label}>{children}</div>,
  };
});

vi.mock('lucide-react', () => ({ Copy: () => <span>copy</span> }));

// The Logs tab has its own tests and its own half of Mantine; here it only has to be
// something that renders, so that switching to it can be seen
vi.mock('../LogViewer.jsx', () => ({ default: () => <div>the logs</div> }));

import API from '../../../api';

const activity = {
  enabled: true,
  keep_seconds: 1800,
  keep_choices: [1800, 7200, 21600, 86400],
  starts: [
    {
      time: 1789500000,
      channel: 'ZIB',
      client: 'Plex',
      total: 4.31,
      slowest: 'first keyframe',
      server_buffering: 19.3,
      server_decision: 'transcode (video + audio)',
      server_speed: '0.9',
      server_gave_up: false,
      server_playing_is_certain: false,
      server_phases: [
        { label: 'session opened', at: 2.1, took: 2.1 },
        { label: 'transcode started', at: 3.4, took: 1.3 },
        { label: 'first video ready', at: 8.0, took: 4.6 },
        { label: 'playing', at: 19.3, took: 11.3 },
      ],
      phases: [
        { label: 'slot', at: 0.04, took: 0.04 },
        { label: 'provider connected', at: 0.31, took: 0.27 },
        { label: 'first byte', at: 0.52, took: 0.21 },
        { label: 'first keyframe', at: 4.28, took: 3.76 },
        { label: 'first byte to player', at: 4.31, took: 0.03 },
      ],
    },
  ],
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

const openSwitches = () =>
  fireEvent.click(screen.getByRole('button', { name: /Channel switches/ }));

describe('Diagnostics', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    API.getDiagnostics.mockResolvedValue(activity);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('shows channel starts with where the time went', async () => {
    render(<Diagnostics active={true} />);

    expect(await screen.findByText('ZIB')).toBeInTheDocument();
    expect(screen.getByText('4.3s')).toBeInTheDocument();
    // The slowest step is named, and every phase has its own bar
    expect(screen.getAllByText('first keyframe').length).toBeGreaterThan(0);
    expect(
      screen.getByTitle('first keyframe: 3.8s (at 4.3s)')
    ).toBeInTheDocument();
    expect(screen.getByTitle('slot: 0.0s (at 0.0s)')).toBeInTheDocument();
  });

  it('shows what the media server did after the handover', async () => {
    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    // Live TV has no playback position, so the server's word is marked as a "+" number
    expect(screen.getByText('19.3s+')).toBeInTheDocument();
    expect(screen.getByText('transcode 0.9×')).toBeInTheDocument();
    // Every stage on the server side, with when it happened
    expect(
      screen.getByText(
        'session opened 2.1s · transcode started 3.4s · first video ready 8.0s · playing 19.3s'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByTitle('first video ready: 4.6s (at 8.0s)')
    ).toBeInTheDocument();
  });

  it('says when the media server never played anything', async () => {
    API.getDiagnostics.mockResolvedValue({
      ...activity,
      starts: [
        {
          ...activity.starts[0],
          server_gave_up: true,
          server_buffering: 0,
          server_phases: [{ label: 'session opened', at: 2.1, took: 2.1 }],
        },
      ],
    });

    render(<Diagnostics active={true} />);

    expect(await screen.findByText('never played')).toBeInTheDocument();
  });

  it('copies one start, and all of them, as text', async () => {
    const writeText = vi.fn().mockResolvedValue();
    Object.assign(navigator, { clipboard: { writeText } });

    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    fireEvent.click(screen.getByLabelText('Copy the start of ZIB'));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    const line = writeText.mock.calls[0][0];
    expect(line).toContain('ZIB');
    expect(line).toContain('took 4.3s');
    expect(line).toContain('first keyframe 4.3s');
    expect(line).toContain('media server: session opened 2.1s');
    expect(await screen.findByText('Copied')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /copy all as text/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
  });

  it('copies over plain http, where the clipboard API does not exist', async () => {
    // Dispatcharr is usually opened over http, where navigator.clipboard is missing
    Object.assign(navigator, { clipboard: undefined });
    document.execCommand = vi.fn().mockReturnValue(true);

    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    fireEvent.click(screen.getByLabelText('Copy the start of ZIB'));

    await waitFor(() =>
      expect(document.execCommand).toHaveBeenCalledWith('copy')
    );
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });

  it('says when copying did not work', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('no')) },
    });
    document.execCommand = vi.fn().mockReturnValue(false);

    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    fireEvent.click(screen.getByLabelText('Copy the start of ZIB'));

    expect(await screen.findByText(/Could not copy/)).toBeInTheDocument();
  });

  it('says so when no channel has started recently', async () => {
    API.getDiagnostics.mockResolvedValue({ ...activity, starts: [] });

    render(<Diagnostics active={true} />);

    expect(
      await screen.findByText('No channels have started recently.')
    ).toBeInTheDocument();
  });

  it('shows the overlap accounts and switches on the other tab', async () => {
    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    openSwitches();

    expect(screen.getByText('TiviBridge A')).toBeInTheDocument();
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
    expect(screen.getByText('confirmed after 1.3s')).toBeInTheDocument();
  });

  it('switches on what apps may say about themselves, both off at first', async () => {
    API.getDiagnostics.mockResolvedValue({
      ...activity,
      app_integration: { devices: false, switch_hints: false },
    });
    API.setAppIntegration.mockResolvedValue({
      ...activity,
      app_integration: { devices: true, switch_hints: false },
    });
    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');
    openSwitches();

    const devices = screen.getByRole('switch', { name: /Believe the device an app says it is/ });
    const hints = screen.getByRole('switch', { name: /Close the channel an app says it is leaving/ });
    expect(devices).not.toBeChecked();
    expect(hints).not.toBeChecked();

    fireEvent.click(devices);
    await waitFor(() =>
      expect(API.setAppIntegration).toHaveBeenCalledWith({ devices: true, switch_hints: false })
    );
  });

  it('refreshes by itself and stops when the page is left', async () => {
    const { unmount } = render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    await vi.advanceTimersByTimeAsync(5000);
    expect(API.getDiagnostics).toHaveBeenCalledTimes(2);

    unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(API.getDiagnostics).toHaveBeenCalledTimes(2);
  });

  it('stops asking for all of it while the logs are being read', async () => {
    // The Logs tab shows nothing from here, and every ask is every channel start, every
    // running channel and every account, five seconds apart, for a page not showing any
    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');
    expect(API.getDiagnostics).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: /Logs/ }));
    await vi.advanceTimersByTimeAsync(20000);
    expect(API.getDiagnostics).toHaveBeenCalledTimes(1);

    // ...and it picks up again on a tab that shows it
    openSwitches();
    await vi.advanceTimersByTimeAsync(5000);
    expect(API.getDiagnostics.mock.calls.length).toBeGreaterThan(1);
  });

  it('says so when the overlap is not enabled anywhere', async () => {
    API.getDiagnostics.mockResolvedValue({
      ...activity,
      enabled: false,
      accounts: [],
      events: [],
    });

    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    openSwitches();

    expect(screen.getByRole('alert')).toHaveTextContent(
      'not enabled on any M3U account'
    );
  });

  it('changes how long activity is kept', async () => {
    API.setDiagnosticsRetention.mockResolvedValue({
      ...activity,
      keep_seconds: 7200,
    });
    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    fireEvent.change(screen.getByLabelText('Keep for'), {
      target: { value: '7200' },
    });

    await waitFor(() =>
      expect(API.setDiagnosticsRetention).toHaveBeenCalledWith(7200)
    );
    expect(await screen.findByLabelText('Keep for')).toHaveValue('7200');
  });

  it('explains both tables in a popup', async () => {
    render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: /what do these mean/i })
    );

    const legend = await screen.findByRole('dialog');
    expect(legend).toHaveTextContent('Channel starts');
    expect(legend).toHaveTextContent('the middle of a group of pictures');
    expect(legend).toHaveTextContent('skipped while surfing');
  });

  it('shows an error when the diagnostics cannot be loaded', async () => {
    API.getDiagnostics.mockRejectedValue(new Error('nope'));

    render(<Diagnostics active={true} />);

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Could not load the diagnostics.'
      )
    );
  });
});
