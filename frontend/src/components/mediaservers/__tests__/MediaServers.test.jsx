import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import MediaServers from '../MediaServers.jsx';

vi.mock('../../../api', () => ({
  default: {
    getMediaServers: vi.fn(),
    saveMediaServer: vi.fn(),
    deleteMediaServer: vi.fn(),
    setMediaServerEnabled: vi.fn(),
    getMediaServerTuners: vi.fn(),
    addMediaServerTuner: vi.fn(),
    syncMediaServerTuner: vi.fn(),
    deleteMediaServerTuner: vi.fn(),
  },
}));

vi.mock('@mantine/core', () => {
  const Table = ({ children }) => <table>{children}</table>;
  Table.Thead = ({ children }) => <thead>{children}</thead>;
  Table.Tbody = ({ children }) => <tbody>{children}</tbody>;
  Table.Tr = ({ children }) => <tr>{children}</tr>;
  Table.Th = ({ children }) => <th>{children}</th>;
  Table.Td = ({ children }) => <td>{children}</td>;
  const input = ({ label, value, onChange, description }) => (
    <label>
      {label}
      <span>{description}</span>
      <input value={value} onChange={onChange} />
    </label>
  );
  Table.ScrollContainer = ({ children }) => <div>{children}</div>;
  const select = ({ label, value, onChange, data, ...rest }) => (
    <label>
      {label}
      <select
        aria-label={rest['aria-label'] || label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="" />
        {(data || []).map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
  return {
    Alert: ({ children }) => <div role="alert">{children}</div>,
    // A multi select takes and gives a list, so the test types the ids as "5,7"
    NumberInput: ({ label, description, value, onChange, ...rest }) => (
      <label>
        {label}
        <span>{description}</span>
        <input
          aria-label={rest['aria-label'] || label}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      </label>
    ),
    MultiSelect: ({ label, value, onChange, ...rest }) => (
      <label>
        {label}
        <input
          aria-label={rest['aria-label'] || label}
          value={(value || []).join(',')}
          onChange={(e) => onChange(e.target.value.split(',').filter(Boolean))}
        />
      </label>
    ),
    Select: select,
    Switch: ({ checked, onChange, ...rest }) => (
      <input
        type="checkbox"
        role="switch"
        aria-label={rest['aria-label']}
        checked={checked}
        onChange={onChange}
      />
    ),
    Badge: ({ children }) => <span>{children}</span>,
    Button: ({ children, onClick }) => (
      <button onClick={onClick}>{children}</button>
    ),
    Card: ({ children }) => <div>{children}</div>,
    Group: ({ children }) => <div>{children}</div>,
    Loader: () => <div>loading</div>,
    PasswordInput: input,
    Stack: ({ children }) => <div>{children}</div>,
    Table,
    Text: ({ children }) => <span>{children}</span>,
    TextInput: input,
  };
});

import API from '../../../api';

const servers = [
  {
    id: 'a1',
    name: 'Home Plex',
    url: 'http://192.168.2.141:32400',
    has_token: true,
    online: true,
    enabled: true,
    version: '1.41.0',
    error: '',
    sessions: [
      {
        title: 'ZIB',
        user: 'Ckegels',
        player: 'Chrome',
        state: 'playing',
        decision: 'transcode (video + audio)',
        speed: 0.9,
      },
    ],
  },
];

const tuners = {
  tuners: [
    {
      id: '22',
      title: 'Austria',
      uri: 'http://192.168.2.142:9191/hdhr/austria',
      state: 'alive',
      tuners: 2,
      dvr_id: '32',
      ours: true,
    },
    {
      id: '1',
      title: 'A1 TV',
      uri: 'http://192.168.2.141:34400',
      state: 'dead',
      tuners: 2,
      dvr_id: '',
      ours: false,
    },
  ],
  base_url: 'http://192.168.2.142:9191',
  max_tuners: 64,
  calculated_tuners: 1362,
  channel_profiles: [{ id: 1, name: 'austria' }],
  channel_groups: [{ id: 5, name: 'Austria', channels: 25 }],
  output_profiles: [{ id: 3, name: 'Remux' }],
  profile_prefix: 'plexmedia',
};

describe('MediaServers', () => {
  beforeEach(() => {
    API.getMediaServers.mockResolvedValue({ servers });
    API.getMediaServerTuners.mockResolvedValue(tuners);
  });

  afterEach(() => vi.clearAllMocks());

  it('shows a server with what it is playing', async () => {
    render(<MediaServers active={true} />);

    expect(await screen.findByText('Home Plex')).toBeInTheDocument();
    expect(screen.getByText('http://192.168.2.141:32400')).toBeInTheDocument();
    expect(screen.getByText('connected · 1.41.0')).toBeInTheDocument();
    expect(screen.getByText('ZIB')).toBeInTheDocument();
    expect(screen.getByText('Ckegels')).toBeInTheDocument();
    expect(
      screen.getByText('transcode (video + audio) 0.9×')
    ).toBeInTheDocument();
  });

  it('shows why a server cannot be used', async () => {
    API.getMediaServers.mockResolvedValue({
      servers: [
        {
          ...servers[0],
          online: false,
          error: 'The server did not accept this token',
          sessions: [],
        },
      ],
    });

    render(<MediaServers active={true} />);

    expect(
      await screen.findByText('The server did not accept this token')
    ).toBeInTheDocument();
  });

  it('adds a server and clears the form', async () => {
    API.saveMediaServer.mockResolvedValue({ servers });
    API.getMediaServers.mockResolvedValue({ servers: [] });

    render(<MediaServers active={true} />);
    await screen.findByText('Add a media server');

    fireEvent.change(screen.getByLabelText(/Address/), {
      target: { value: 'http://192.168.2.141:32400' },
    });
    fireEvent.change(screen.getByLabelText(/Token/), {
      target: { value: 'secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(API.saveMediaServer).toHaveBeenCalledWith(
        expect.objectContaining({
          url: 'http://192.168.2.141:32400',
          token: 'secret',
        })
      )
    );
    expect(await screen.findByText('Home Plex')).toBeInTheDocument();
  });

  it('says what went wrong when the server cannot be saved', async () => {
    API.getMediaServers.mockResolvedValue({ servers: [] });
    API.saveMediaServer.mockRejectedValue({
      body: { error: 'The server did not accept this token' },
    });

    render(<MediaServers active={true} />);
    await screen.findByText('Add a media server');

    fireEvent.change(screen.getByLabelText(/Address/), {
      target: { value: 'http://x:32400' },
    });
    fireEvent.change(screen.getByLabelText(/Token/), {
      target: { value: 'wrong' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The server did not accept this token'
    );
  });

  it('lists the tuners and flags a leftover', async () => {
    render(<MediaServers active={true} />);

    expect(await screen.findByText('Austria')).toBeInTheDocument();
    expect(screen.getByText('Dispatcharr')).toBeInTheDocument();
    expect(screen.getByText('dead')).toBeInTheDocument();
    // A tuner in no DVR does nothing, which is worth saying
    expect(screen.getByText('not in a DVR')).toBeInTheDocument();
  });

  it('syncs and removes a tuner', async () => {
    API.syncMediaServerTuner.mockResolvedValue(tuners);
    API.deleteMediaServerTuner.mockResolvedValue(tuners);

    render(<MediaServers active={true} />);
    await screen.findByText('Austria');

    fireEvent.click(screen.getAllByRole('button', { name: 'Sync' })[0]);
    await waitFor(() =>
      expect(API.syncMediaServerTuner).toHaveBeenCalledWith('a1', '22', '32')
    );

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[1]);
    await waitFor(() =>
      expect(API.deleteMediaServerTuner).toHaveBeenCalledWith('a1', '22')
    );
  });

  it('adds a tuner built from channel groups', async () => {
    API.addMediaServerTuner.mockResolvedValue(tuners);

    render(<MediaServers active={true} />);
    await screen.findByText('Austria');

    // The guessed address can be corrected before it is used
    expect(screen.getByLabelText(/Dispatcharr address/)).toHaveValue(
      'http://192.168.2.142:9191'
    );
    fireEvent.change(screen.getByLabelText(/New profile name/), {
      target: { value: 'austria' },
    });
    fireEvent.change(screen.getByLabelText('Channel groups'), {
      target: { value: '5' },
    });
    fireEvent.change(screen.getByLabelText('Output profile'), {
      target: { value: '3' },
    });
    // What Dispatcharr would say is shown, so a sane number can be chosen instead
    expect(screen.getByText('Dispatcharr says 1362')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Tuners'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add to server' }));

    await waitFor(() =>
      expect(API.addMediaServerTuner).toHaveBeenCalledWith({
        server: 'a1',
        base_url: 'http://192.168.2.142:9191',
        channel_profile: '',
        new_profile_name: 'austria',
        group_ids: [5],
        output_profile_id: '3',
        tuner_count: '2',
      })
    );
  });

  it('switches a server off', async () => {
    API.setMediaServerEnabled.mockResolvedValue({
      servers: [{ ...servers[0], enabled: false, online: false }],
    });

    render(<MediaServers active={true} />);
    await screen.findByText('Home Plex');

    fireEvent.click(screen.getByRole('switch', { name: 'Use Home Plex' }));

    await waitFor(() =>
      expect(API.setMediaServerEnabled).toHaveBeenCalledWith('a1', false)
    );
    expect(await screen.findByText('switched off')).toBeInTheDocument();
  });

  it('removes a server', async () => {
    API.deleteMediaServer.mockResolvedValue({ servers: [] });

    render(<MediaServers active={true} />);
    await screen.findByText('Home Plex');

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]);

    await waitFor(() =>
      expect(API.deleteMediaServer).toHaveBeenCalledWith('a1')
    );
  });
});
