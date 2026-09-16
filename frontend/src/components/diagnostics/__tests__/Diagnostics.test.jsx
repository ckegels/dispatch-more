import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import Diagnostics from '../Diagnostics.jsx';

vi.mock('../../../api', () => ({
  default: { getDiagnostics: vi.fn(), setDiagnosticsRetention: vi.fn() },
}));

// Mantine components are rendered as plain elements, like the other settings tests
vi.mock('@mantine/core', () => {
  const Table = ({ children }) => <table>{children}</table>;
  Table.Thead = ({ children }) => <thead>{children}</thead>;
  Table.Tbody = ({ children }) => <tbody>{children}</tbody>;
  Table.Tr = ({ children }) => <tr>{children}</tr>;
  Table.Th = ({ children }) => <th>{children}</th>;
  Table.Td = ({ children }) => <td>{children}</td>;

  // A minimal stateful Tabs: the list are buttons, only the open panel renders
  const TabsContext = React.createContext(null);
  const Tabs = ({ defaultValue, children }) => {
    const [value, setValue] = React.useState(defaultValue);
    return (
      <TabsContext.Provider value={{ value, setValue }}>
        <div>{children}</div>
      </TabsContext.Provider>
    );
  };
  const TabsTab = ({ value, children }) => {
    const ctx = React.useContext(TabsContext);
    return <button onClick={() => ctx.setValue(value)}>{children}</button>;
  };
  const TabsPanel = ({ value, children }) => {
    const ctx = React.useContext(TabsContext);
    return ctx.value === value ? <div>{children}</div> : null;
  };
  Tabs.List = ({ children }) => <div>{children}</div>;
  Tabs.Tab = TabsTab;
  Tabs.Panel = TabsPanel;

  return {
    Alert: ({ children }) => <div role="alert">{children}</div>,
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
    Tabs,
    Text: ({ children }) => <span>{children}</span>,
    Tooltip: ({ label, children }) => <div title={label}>{children}</div>,
  };
});

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
  fireEvent.click(screen.getByRole('button', { name: 'Channel switches' }));

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
      screen.getByTitle('first keyframe: 3.76s (at 4.28s)')
    ).toBeInTheDocument();
    expect(screen.getByTitle('slot: 0.04s (at 0.04s)')).toBeInTheDocument();
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

  it('refreshes by itself and stops when the page is left', async () => {
    const { unmount } = render(<Diagnostics active={true} />);
    await screen.findByText('ZIB');

    await vi.advanceTimersByTimeAsync(5000);
    expect(API.getDiagnostics).toHaveBeenCalledTimes(2);

    unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(API.getDiagnostics).toHaveBeenCalledTimes(2);
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
