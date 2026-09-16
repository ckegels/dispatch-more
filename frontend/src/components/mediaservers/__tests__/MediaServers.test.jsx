import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import MediaServers from '../MediaServers.jsx';

vi.mock('../../../api', () => ({
  default: {
    getMediaServers: vi.fn(),
    saveMediaServer: vi.fn(),
    deleteMediaServer: vi.fn(),
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
  return {
    Alert: ({ children }) => <div role="alert">{children}</div>,
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

describe('MediaServers', () => {
  beforeEach(() => {
    API.getMediaServers.mockResolvedValue({ servers });
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

  it('removes a server', async () => {
    API.deleteMediaServer.mockResolvedValue({ servers: [] });

    render(<MediaServers active={true} />);
    await screen.findByText('Home Plex');

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

    await waitFor(() =>
      expect(API.deleteMediaServer).toHaveBeenCalledWith('a1')
    );
  });
});
