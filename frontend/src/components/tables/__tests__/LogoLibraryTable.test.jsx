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
    searchLogoLibrary: vi.fn(),
    uploadLogo: vi.fn(),
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

// The real table is heavy and drawn elsewhere; this one draws the same cells, so what the
// tests look at is what the page shows
vi.mock('../CustomTable', async () => {
  const { useState } = await import('react');
  // The tick box is drawn by the real table itself for a "select" column, and what is
  // ticked comes back through onRowSelectionChange; this does the same
  const CustomTable = ({ table }) => {
    const [ticked, setTicked] = useState(new Set());
    return (
      <div>
        {table.data.map((original) => (
          <div key={original.id}>
            {table.columns.map((column) =>
              column.id === 'select' ? (
                <input
                  key="select"
                  type="checkbox"
                  aria-label={`Use the suggested logo for ${original.name}`}
                  checked={ticked.has(original.id)}
                  onChange={(e) => {
                    const next = new Set(ticked);
                    if (e.target.checked) next.add(original.id);
                    else next.delete(original.id);
                    setTicked(next);
                    table.onRowSelectionChange([...next]);
                  }}
                />
              ) : (
                <div key={column.id || column.accessorKey}>
                  {column.cell({ row: { original } })}
                </div>
              )
            )}
          </div>
        ))}
      </div>
    );
  };
  return {
    useTable: (options) => ({ ...options, setSelectedTableIds: () => {} }),
    CustomTable,
  };
});

vi.mock('lucide-react', () => ({
  Check: () => null,
  Library: () => null,
  RefreshCw: () => null,
  Search: () => null,
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
    ActionIcon: ({ children, onClick, ...rest }) => (
      <button aria-label={rest['aria-label']} onClick={onClick}>
        {children}
      </button>
    ),
    Center: box,
    Image: ({ src, alt }) => <img src={src} alt={alt} />,
    LoadingOverlay: () => null,
    NativeSelect: ({ value, onChange, data }) => (
      <select aria-label="Page size" value={value} onChange={onChange}>
        {data.map((option) => (
          <option key={option}>{option}</option>
        ))}
      </select>
    ),
    Pagination: ({ total, value, onChange }) => (
      <div>
        <span>
          page {value} of {total}
        </span>
        <button onClick={() => onChange(value + 1)}>next page</button>
      </div>
    ),
    Paper: box,
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
    Tooltip: ({ children }) => children,
    useMantineTheme: () => ({
      tailwind: { green: { 5: 'green' }, blue: { 6: 'blue' } },
    }),
    Alert: ({ children }) => <div role="alert">{children}</div>,
    Badge: ({ children }) => <span>{children}</span>,
    Box: ({ children, onClick, ...rest }) => (
      <div role={rest.role} aria-label={rest['aria-label']} onClick={onClick}>
        {children}
      </div>
    ),
    Button: ({ children, onClick, disabled, ...rest }) => (
      <button aria-label={rest['aria-label']} onClick={onClick} disabled={disabled}>
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
    FileInput: ({ onChange, ...rest }) => (
      <input
        type="file"
        aria-label={rest['aria-label']}
        onChange={(e) => onChange(e.target.files[0])}
      />
    ),
    Group: box,
    Loader: () => <div>loading</div>,
    Modal: ({ opened, title, children }) =>
      opened ? (
        <div role="dialog" aria-label={title}>
          <span>{title}</span>
          {children}
        </div>
      ) : null,
    SimpleGrid: box,
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
    Tabs: Object.assign(
      ({ children, onChange }) => (
        <div data-tabs onClick={(e) => e.target.dataset.tab && onChange(e.target.dataset.tab)}>
          {children}
        </div>
      ),
      {
        List: ({ children }) => <div>{children}</div>,
        Tab: ({ children, value }) => (
          <button data-tab={value}>{children}</button>
        ),
        Panel: ({ children }) => <div>{children}</div>,
      }
    ),
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
    fireEvent.click(screen.getByRole('button', { name: 'Apply (1)' }));
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
    render(<LogoLibraryTable />);
    await screen.findByText('┃FR┃ TFX');

    // The second of TFX's two, which is the Belgian one
    fireEvent.click(screen.getByLabelText('Choose tv-logos logo 2 for ┃FR┃ TFX'));
    fireEvent.click(screen.getByLabelText('Use the suggested logo for ┃FR┃ TFX'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply (1)' }));
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

  it('finds a logo by hand for a channel nothing was suggested for', async () => {
    API.getLogoLibrary.mockResolvedValue({
      ...library,
      channels: [
        {
          channel_id: 3,
          number: 3,
          name: '┃FR┃ ANGERS TV',
          country: 'fr',
          current: null,
          suggestions: [],
        },
      ],
    });
    API.searchLogoLibrary.mockResolvedValue({
      built: true,
      results: [
        {
          url: 'https://i.imgur.com/angers.png',
          name: 'TV Angers',
          source: 'iptv-org',
          country: 'fr',
        },
      ],
    });
    API.applyLogoLibrary.mockResolvedValue({ updated: 1, created_logos: 1 });
    render(<LogoLibraryTable />);
    await screen.findByText('┃FR┃ ANGERS TV');
    expect(screen.getByText('Not suggested by any collection')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Find a logo for ┃FR┃ ANGERS TV'));

    // It starts from the channel's own name, without the box of country in front
    const box = await screen.findByLabelText('Search logos');
    expect(box.value).toBe('ANGERS TV');
    await waitFor(() =>
      expect(API.searchLogoLibrary).toHaveBeenCalledWith('ANGERS TV', 'fr')
    );
    fireEvent.click(await screen.findByLabelText('Use TV Angers'));

    // Chosen and ticked, since choosing it is the point; applied with the rest
    fireEvent.click(screen.getByRole('button', { name: 'Apply (1)' }));
    fireEvent.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Apply',
      })
    );
    await waitFor(() =>
      expect(API.applyLogoLibrary).toHaveBeenCalledWith([
        { channel_id: 3, url: 'https://i.imgur.com/angers.png', name: 'TV Angers' },
      ])
    );
  });

  it('uses a pasted link', async () => {
    API.applyLogoLibrary.mockResolvedValue({ updated: 1, created_logos: 1 });
    render(<LogoLibraryTable />);
    await screen.findByText('┃BE┃ Eén');

    fireEvent.click(screen.getByLabelText('Find a logo for ┃BE┃ Eén'));
    fireEvent.change(await screen.findByLabelText('Logo link'), {
      target: { value: 'https://example.com/een.png' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Use this link' }));

    expect(screen.getByText('your link')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Apply (1)' }));
    fireEvent.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Apply',
      })
    );
    await waitFor(() =>
      expect(API.applyLogoLibrary).toHaveBeenCalledWith([
        { channel_id: 1, url: 'https://example.com/een.png', name: 'Eén' },
      ])
    );
  });

  it('refuses something that is not a link', async () => {
    render(<LogoLibraryTable />);
    await screen.findByText('┃BE┃ Eén');

    fireEvent.click(screen.getByLabelText('Find a logo for ┃BE┃ Eén'));
    fireEvent.change(await screen.findByLabelText('Logo link'), {
      target: { value: 'een.png' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Use this link' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('http');
  });

  it('gives an uploaded logo by id, since it has no address of its own', async () => {
    API.uploadLogo.mockResolvedValue({
      id: 77,
      name: 'Eén',
      url: '/data/logos/een.png',
      cache_url: '/api/channels/logos/77/cache/',
    });
    API.applyLogoLibrary.mockResolvedValue({ updated: 1, created_logos: 0 });
    render(<LogoLibraryTable />);
    await screen.findByText('┃BE┃ Eén');

    fireEvent.click(screen.getByLabelText('Find a logo for ┃BE┃ Eén'));
    const file = new File(['png'], 'een.png', { type: 'image/png' });
    fireEvent.change(await screen.findByLabelText('Logo file'), {
      target: { files: [file] },
    });
    // What an upload costs on a media server is said before it is done
    expect(screen.getByText(/away from home will not show it/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Upload and use' }));

    await waitFor(() => expect(API.uploadLogo).toHaveBeenCalledWith(file, 'Eén'));
    expect(await screen.findByText('uploaded')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Apply (1)' }));
    fireEvent.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Apply',
      })
    );
    await waitFor(() =>
      expect(API.applyLogoLibrary).toHaveBeenCalledWith([
        { channel_id: 1, logo_id: 77 },
      ])
    );
  });

  it('asks to download the lists when they have not been', async () => {
    API.getLogoLibrary.mockResolvedValue({
      status: { built: false, counts: {}, errors: {} },
      channels: [],
    });
    render(<LogoLibraryTable />);

    expect(
      await screen.findByRole('button', { name: 'Download Lists' })
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
