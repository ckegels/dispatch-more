// Settings → Service keys: a card per service, saved and tested one at a time.
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../../../mantineTheme';
import ServiceKeysForm from '../ServiceKeysForm.jsx';
import API from '../../../../api';

vi.mock('../../../../api', () => ({
  default: {
    getServiceKeys: vi.fn(),
    saveServiceKeys: vi.fn(),
    testServiceKey: vi.fn(),
  },
}));

const services = [
  {
    id: 'tmdb',
    name: 'TMDB',
    url: 'https://tmdb',
    about: 'Free.',
    fields: [
      { key: 'tmdb_key', label: 'API key or read access token', secret: true },
    ],
  },
  {
    id: 'tvdb',
    name: 'TheTVDB',
    url: 'https://tvdb',
    about: 'Free key.',
    fields: [
      { key: 'tvdb_key', label: 'API key', secret: true },
      {
        key: 'tvdb_pin',
        label: 'Subscriber PIN (only if your key asks for one)',
        secret: true,
      },
    ],
  },
];
const empty = { tmdb_key: '', tvdb_key: '', tvdb_pin: '' };

const draw = () =>
  render(
    <MantineProvider theme={theme}>
      <ServiceKeysForm />
    </MantineProvider>
  );

describe('ServiceKeysForm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    API.getServiceKeys.mockResolvedValue({ services, values: empty });
  });

  it('saves a key, then tests it', async () => {
    API.saveServiceKeys.mockResolvedValue({
      services,
      values: { ...empty, tvdb_key: 'abc' },
    });
    API.testServiceKey.mockResolvedValue({
      ok: true,
      message: 'Works: MasterChef is Food.',
    });
    draw();
    const [key] = await screen.findAllByLabelText('API key');
    const saveButtons = screen.getAllByRole('button', { name: 'Save' });
    expect(saveButtons[1]).toBeDisabled();
    fireEvent.change(key, { target: { value: 'abc' } });
    fireEvent.click(saveButtons[1]);
    await waitFor(() =>
      expect(API.saveServiceKeys).toHaveBeenCalledWith({
        tvdb_key: 'abc',
        tvdb_pin: '',
      })
    );
    const testButton = screen.getAllByRole('button', { name: 'Test' })[1];
    await waitFor(() => expect(testButton).not.toBeDisabled());
    fireEvent.click(testButton);
    expect(
      await screen.findByText('Works: MasterChef is Food.')
    ).toBeInTheDocument();
    expect(API.testServiceKey).toHaveBeenCalledWith('tvdb');
  });

  it('links to where a key is made', async () => {
    draw();
    expect(await screen.findByText('TheTVDB')).toBeInTheDocument();
    expect(
      screen.getAllByRole('link', { name: /Get a key/ })[1]
    ).toHaveAttribute('href', 'https://tvdb');
  });
});
