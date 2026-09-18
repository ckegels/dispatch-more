import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import LogoLibraryTable from '../LogoLibraryTable.jsx';

vi.mock('../../../api', () => ({
  default: {
    getLogoLibrary: vi.fn(),
    getLogoLibraryStatus: vi.fn(),
    refreshLogoLibrary: vi.fn(),
    applyLogoLibrary: vi.fn(),
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
  const box = ({ children, onClick }) => <div onClick={onClick}>{children}</div>;
  return {
    Alert: ({ children }) => <div role="alert">{children}</div>,
    Badge: ({ children }) => <span>{children}</span>,
    Box: box,
    Button: ({ children, onClick, disabled }) => (
      <button onClick={onClick} disabled={disabled}>
        {children}
      </button>
    ),
    Checkbox: ({ checked, onChange, disabled, ...rest }) => (
      <input
        type="checkbox"
        aria-label={rest['aria-label']}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
      />
    ),
    Group: box,
    Loader: () => <div>loading</div>,
    SegmentedControl: ({ value, onChange, data }) => (
      <div>
        {data.map((option) => (
          <button
            key={option.value}
            aria-pressed={option.value === value}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    ),
    Stack: box,
    Table,
    Text: ({ children }) => <span>{children}</span>,
    TextInput: ({ value, onChange, ...rest }) => (
      <input aria-label={rest['aria-label']} value={value} onChange={onChange} />
    ),
  };
});

import API from '../../../api';

const EEN = 'https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/belgium/een-be.png';
const TFX_FR = 'https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/france/tfx-fr.png';
const TFX_BE = 'https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/belgium/tfx-be.png';

const library = {
  status: {
    built: true,
    built_at: Date.now() / 1000 - 7200,
    counts: { 'tv-logos': 10777, 'iptv-org': 48836 },
    errors: {},
  },
  channels: [
    {
      channel_id: 1,
      number: 1,
      name: '┃BE┃ Eén',
      country: 'be',
      current: null,
      suggestions: [
        { url: EEN, name: 'een', source: 'tv-logos', country: 'be', format: 'PNG' },
      ],
    },
    {
      channel_id: 2,
      number: 2,
      name: '┃FR┃ TFX',
      country: 'fr',
      current: { id: 9, name: 'old tfx', url: 'https://old.example/tfx.png' },
      suggestions: [
        { url: TFX_FR, name: 'tfx', source: 'tv-logos', country: 'fr', format: 'PNG' },
        { url: TFX_BE, name: 'tfx', source: 'tv-logos', country: 'be', format: 'PNG' },
      ],
    },
  ],
};

describe('LogoLibraryTable', () => {
  beforeEach(() => {
    API.getLogoLibrary.mockResolvedValue(library);
  });

  afterEach(() => vi.clearAllMocks());

  it('shows what each channel has next to what it could have', async () => {
    render(<LogoLibraryTable />);

    expect(await screen.findByText('┃BE┃ Eén')).toBeInTheDocument();
    expect(screen.getByText('no logo')).toBeInTheDocument();
    expect(screen.getByText('old tfx')).toBeInTheDocument();
    // Where the logos come from, and how many
    expect(screen.getByText(/10,777 from tv-logos/)).toBeInTheDocument();
    expect(screen.getByText(/48,836 from iptv-org/)).toBeInTheDocument();
  });

  it('changes nothing until channels are ticked and applied', async () => {
    render(<LogoLibraryTable />);
    await screen.findByText('┃BE┃ Eén');

    expect(screen.getByRole('button', { name: /^Apply/ })).toBeDisabled();
    expect(API.applyLogoLibrary).not.toHaveBeenCalled();
  });

  it('applies only the ticked channels, after asking', async () => {
    API.applyLogoLibrary.mockResolvedValue({ updated: 1, created_logos: 1 });
    render(<LogoLibraryTable />);
    await screen.findByText('┃BE┃ Eén');

    fireEvent.click(
      screen.getByLabelText('Use the suggested logo for ┃BE┃ Eén')
    );
    fireEvent.click(screen.getByRole('button', { name: 'Apply 1' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Give 1 channel a new logo?');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(API.applyLogoLibrary).toHaveBeenCalledWith([
        { channel_id: 1, url: EEN, name: 'een' },
      ])
    );
  });

  it('applies another suggestion when one is chosen instead', async () => {
    API.applyLogoLibrary.mockResolvedValue({ updated: 1, created_logos: 1 });
    const { container } = render(<LogoLibraryTable />);
    await screen.findByText('┃FR┃ TFX');

    // The second of TFX's two, which is the Belgian one
    const small = [...container.querySelectorAll(`img[src="${TFX_BE}"]`)];
    fireEvent.click(small[small.length - 1].parentElement);
    fireEvent.click(screen.getByLabelText('Use the suggested logo for ┃FR┃ TFX'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply 1' }));
    fireEvent.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Apply',
      })
    );

    await waitFor(() =>
      expect(API.applyLogoLibrary).toHaveBeenCalledWith([
        { channel_id: 2, url: TFX_BE, name: 'tfx' },
      ])
    );
  });

  it('searches the list it already has rather than asking again', async () => {
    render(<LogoLibraryTable />);
    await screen.findByText('┃BE┃ Eén');

    fireEvent.change(screen.getByLabelText('Search channels'), {
      target: { value: 'tfx' },
    });

    expect(screen.queryByText('┃BE┃ Eén')).not.toBeInTheDocument();
    expect(screen.getByText('┃FR┃ TFX')).toBeInTheDocument();
    expect(API.getLogoLibrary).toHaveBeenCalledTimes(1);
  });

  it('asks to download the lists when they have not been', async () => {
    API.getLogoLibrary.mockResolvedValue({
      status: { built: false, counts: {}, errors: {} },
      channels: [],
    });
    render(<LogoLibraryTable />);

    expect(
      await screen.findByRole('button', { name: 'Download logo lists' })
    ).toBeInTheDocument();
  });

  it('says which collection could not be reached', async () => {
    API.getLogoLibrary.mockResolvedValue({
      ...library,
      status: { ...library.status, errors: { 'iptv-org': 'timed out' } },
    });
    render(<LogoLibraryTable />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'iptv-org could not be reached'
    );
  });
});
