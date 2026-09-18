import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import LogoSources from '../LogoSources.jsx';

vi.mock('../../../api', () => ({
  default: {
    getLogoSources: vi.fn(),
    checkLogoSource: vi.fn(),
    addLogoSource: vi.fn(),
    setLogoSourceEnabled: vi.fn(),
    removeLogoSource: vi.fn(),
  },
}));

vi.mock('../../ConfirmationDialog', () => ({
  default: ({ opened, title, message, confirmLabel, onConfirm, onClose }) =>
    opened ? (
      <div role="dialog">
        <span>{title}</span>
        <span>{message}</span>
        <button onClick={onConfirm}>{confirmLabel}</button>
        <button onClick={onClose}>Cancel</button>
      </div>
    ) : null,
}));

vi.mock('@mantine/core', () => {
  const Table = ({ children }) => <table>{children}</table>;
  Table.Thead = ({ children }) => <thead>{children}</thead>;
  Table.Tbody = ({ children }) => <tbody>{children}</tbody>;
  Table.Tr = ({ children }) => <tr>{children}</tr>;
  Table.Th = ({ children }) => <th>{children}</th>;
  Table.Td = ({ children }) => <td>{children}</td>;
  Table.ScrollContainer = ({ children }) => <div>{children}</div>;
  const box = ({ children }) => <div>{children}</div>;
  return {
    Alert: ({ children }) => <div role="alert">{children}</div>,
    Badge: ({ children }) => <span>{children}</span>,
    Box: box,
    Button: ({ children, onClick, disabled, ...rest }) => (
      <button aria-label={rest['aria-label']} onClick={onClick} disabled={disabled}>
        {children}
      </button>
    ),
    Group: box,
    Select: ({ value, onChange, data, ...rest }) => (
      <select
        aria-label={rest['aria-label']}
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
    Stack: box,
    Switch: ({ checked, onChange, disabled, ...rest }) => (
      <input
        type="checkbox"
        role="switch"
        aria-label={rest['aria-label']}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
      />
    ),
    Table,
    Text: ({ children }) => <span>{children}</span>,
    TextInput: ({ value, onChange, ...rest }) => (
      <input aria-label={rest['aria-label']} value={value} onChange={onChange} />
    ),
  };
});

import API from '../../../api';

const page = {
  types: ['github', 'm3u', 'xmltv', 'json'],
  sources: [
    {
      id: 'tv-logos',
      name: 'tv-logos',
      type: 'github',
      url: 'https://github.com/tv-logo/tv-logos',
      built_in: true,
      enabled: true,
      count: 10777,
    },
    {
      id: 'iptv-org',
      name: 'iptv-org',
      type: 'json',
      url: 'https://iptv-org.github.io/api/logos.json',
      built_in: true,
      enabled: true,
      count: 48836,
    },
    {
      id: 'ab12',
      name: 'Belgium playlist',
      type: 'm3u',
      url: 'https://example.com/be.m3u',
      enabled: true,
      count: 48,
      error: null,
    },
  ],
};

describe('LogoSources', () => {
  beforeEach(() => {
    API.getLogoSources.mockResolvedValue(page);
  });

  afterEach(() => vi.clearAllMocks());

  it('lists every collection with how many logos it gave', async () => {
    render(<LogoSources />);

    expect(await screen.findByText('Belgium playlist')).toBeInTheDocument();
    expect(screen.getByText('10,777')).toBeInTheDocument();
    expect(screen.getByText('48')).toBeInTheDocument();
    // The built in ones can be switched off but not removed
    expect(screen.queryByLabelText('Remove tv-logos')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Remove Belgium playlist')).toBeInTheDocument();
  });

  it('shows what a collection holds before it is added', async () => {
    API.checkLogoSource.mockResolvedValue({
      count: 1234,
      sample: [{ name: 'TFX', url: 'https://example.com/tfx.png', country: 'fr' }],
    });
    render(<LogoSources />);
    await screen.findByText('Belgium playlist');

    fireEvent.change(screen.getByLabelText('Kind of collection'), {
      target: { value: 'm3u' },
    });
    fireEvent.change(screen.getByLabelText('Collection link'), {
      target: { value: 'https://example.com/fr.m3u' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Check' }));

    expect(await screen.findByText(/1,234 logos found/)).toBeInTheDocument();
    expect(API.checkLogoSource).toHaveBeenCalledWith({
      type: 'm3u',
      url: 'https://example.com/fr.m3u',
      name: '',
    });
    expect(API.addLogoSource).not.toHaveBeenCalled();
  });

  it('adds one, and says the lists need updating', async () => {
    const onChanged = vi.fn();
    API.addLogoSource.mockResolvedValue(page);
    render(<LogoSources onChanged={onChanged} />);
    await screen.findByText('Belgium playlist');

    fireEvent.change(screen.getByLabelText('Collection link'), {
      target: { value: 'someone/logos' },
    });
    fireEvent.change(screen.getByLabelText('Collection name'), {
      target: { value: 'Mine' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() =>
      expect(API.addLogoSource).toHaveBeenCalledWith({
        type: 'github',
        url: 'someone/logos',
        name: 'Mine',
      })
    );
    expect(onChanged).toHaveBeenCalled();
  });

  it('says why a collection could not be added', async () => {
    API.addLogoSource.mockRejectedValue({
      body: { error: 'That was read, but there are no logos in it' },
    });
    render(<LogoSources />);
    await screen.findByText('Belgium playlist');

    fireEvent.change(screen.getByLabelText('Collection link'), {
      target: { value: 'https://example.com/empty.m3u' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('no logos in it');
  });

  it('switches a built in one off', async () => {
    API.setLogoSourceEnabled.mockResolvedValue(page);
    render(<LogoSources />);
    await screen.findByText('Belgium playlist');

    fireEvent.click(screen.getByLabelText('Use iptv-org'));

    await waitFor(() =>
      expect(API.setLogoSourceEnabled).toHaveBeenCalledWith('iptv-org', false)
    );
  });

  it('removes an added one after asking', async () => {
    API.removeLogoSource.mockResolvedValue(page);
    render(<LogoSources />);
    await screen.findByText('Belgium playlist');

    fireEvent.click(screen.getByLabelText('Remove Belgium playlist'));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Remove this collection?');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(API.removeLogoSource).toHaveBeenCalledWith('ab12'));
  });
});
