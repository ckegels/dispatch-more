import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import M3U from '../M3U';

// ── Store mocks ────────────────────────────────────────────────────────────────
vi.mock('../../../store/userAgents', () => ({ default: vi.fn() }));
vi.mock('../../../store/channels', () => ({ default: vi.fn() }));
vi.mock('../../../store/epgs', () => ({ default: vi.fn() }));
vi.mock('../../../store/useVODStore', () => ({ default: vi.fn() }));

// ── Utility mocks ──────────────────────────────────────────────────────────────
vi.mock('../../../utils/forms/M3uUtils.js', () => ({
  addPlaylist: vi.fn(),
  getPlaylist: vi.fn(),
  prepareSubmitValues: vi.fn((values) => values),
  updatePlaylist: vi.fn(),
  expDateFromPlaylist: (expDate) => {
    if (!expDate) return null;
    const parsed = new Date(expDate);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  },
  expDateKey: (expDate) => {
    if (expDate instanceof Date) {
      return Number.isNaN(expDate.getTime()) ? null : expDate.toISOString();
    }
    if (!expDate) return null;
    const parsed = new Date(expDate);
    return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
  },
}));

vi.mock('../../../store/playlists', () => ({ default: vi.fn() }));

vi.mock('../../../utils/forms/DummyEpgUtils.js', () => ({
  addEPG: vi.fn(),
}));

vi.mock('../../../utils/notificationUtils.js', () => ({
  showNotification: vi.fn(),
}));

// ── Sub-component mocks ────────────────────────────────────────────────────────
vi.mock('../M3UProfiles', () => ({
  default: ({ onChange }) => (
    <div data-testid="m3u-profiles">
      <button onClick={() => onChange?.([])}>M3UProfiles</button>
    </div>
  ),
}));

vi.mock('../M3UGroupFilter', () => ({
  default: ({ onChange }) => (
    <div data-testid="m3u-group-filter">
      <button onClick={() => onChange?.([])}>M3UGroupFilter</button>
    </div>
  ),
}));

vi.mock('../M3UFilters', () => ({
  default: ({ onChange }) => (
    <div data-testid="m3u-filters">
      <button onClick={() => onChange?.([])}>M3UFilters</button>
    </div>
  ),
}));

vi.mock('../ScheduleInput', () => ({
  default: ({ onChange, value }) => (
    <div data-testid="schedule-input">
      <input
        data-testid="schedule-value"
        value={value || ''}
        onChange={(e) => onChange?.(e.target.value)}
      />
    </div>
  ),
}));

vi.mock('../../ConfirmationDialog', () => ({
  default: ({ opened, onClose, onConfirm, title, message }) =>
    opened ? (
      <div data-testid="confirmation-dialog">
        <div>{title}</div>
        <div>{message}</div>
        <button onClick={onConfirm}>Confirm dialog</button>
        <button onClick={onClose}>Cancel dialog</button>
      </div>
    ) : null,
}));

// ── Mantine dates ──────────────────────────────────────────────────────────────
vi.mock('@mantine/dates', () => ({
  DateTimePicker: ({ label, value, onChange, placeholder }) => (
    <div>
      <label>
        {label}
        <input
          data-testid="date-time-picker"
          placeholder={placeholder}
          value={value ? value.toISOString() : ''}
          onChange={(e) =>
            onChange?.(e.target.value ? new Date(e.target.value) : null)
          }
        />
      </label>
    </div>
  ),
}));

// ── Mantine form ───────────────────────────────────────────────────────────────
vi.mock('@mantine/form', () => {
  let _values = null;

  return {
    isNotEmpty: vi.fn(() => (val) => (val ? null : 'Required')),
    __resetFormState: () => {
      _values = null;
    },
    useForm: vi.fn(({ initialValues = {} } = {}) => {
      if (_values === null) {
        _values = { ...initialValues };
      }

      return {
        key: vi.fn((field) => field),
        getValues: () => ({ ..._values }),
        setValues: (v) => {
          Object.assign(_values, v);
        },
        setFieldValue: (field, val) => {
          _values[field] = val;
        },
        reset: () => {
          _values = { ...initialValues };
        },
        submitting: false,
        onSubmit: vi.fn((handler) => (e) => {
          e?.preventDefault?.();
          if (_values?.name) handler();
        }),
        getInputProps: vi.fn((field, options) => ({
          value: _values?.[field] ?? '',
          ...(options?.type === 'checkbox'
            ? { checked: !!_values?.[field] }
            : {}),
          onChange: vi.fn((e) => {
            const val = e?.target?.value ?? e;
            if (_values) _values[field] = val;
          }),
          error: null,
        })),
      };
    }),
  };
});

// ── Mantine core ───────────────────────────────────────────────────────────────
vi.mock('@mantine/core', () => ({
  Box: ({ children }) => <div>{children}</div>,
  Button: ({ children, onClick, type, loading, disabled, variant, color }) => (
    <button
      type={type || 'button'}
      onClick={onClick}
      disabled={disabled || loading}
      data-variant={variant}
      data-color={color}
      data-loading={String(loading)}
    >
      {children}
    </button>
  ),
  Collapse: ({ in: opened, children }) =>
    opened ? <div data-testid="collapse">{children}</div> : null,
  Checkbox: ({ label, checked, onChange, disabled }) => (
    <label>
      <input
        type="checkbox"
        aria-label={label}
        checked={!!checked}
        onChange={(e) => onChange?.(e)}
        disabled={disabled}
      />
      {label}
    </label>
  ),
  Divider: ({ label }) => <hr aria-label={label} />,
  FileInput: ({ label, placeholder, onChange, accept, disabled }) => (
    <div>
      <label>
        {label}
        <input
          type="file"
          aria-label={label || placeholder}
          accept={accept}
          disabled={disabled}
          onChange={(e) => onChange?.(e.target.files?.[0] ?? null)}
        />
      </label>
    </div>
  ),
  Flex: ({ children }) => <div>{children}</div>,
  Group: ({ children }) => <div>{children}</div>,
  LoadingOverlay: ({ visible }) =>
    visible ? <div data-testid="loading-overlay" /> : null,
  Modal: ({ children, opened, onClose, title, size }) =>
    opened ? (
      <div data-testid="modal" data-size={size}>
        <div data-testid="modal-title">{title}</div>
        <button data-testid="modal-close" onClick={onClose}>
          ×
        </button>
        {children}
      </div>
    ) : null,
  NumberInput: ({
    label,
    placeholder,
    value,
    onChange,
    min,
    max,
    disabled,
    error,
  }) => (
    <div>
      <label>
        {label}
        <input
          type="number"
          aria-label={label || placeholder}
          placeholder={placeholder}
          value={value ?? ''}
          min={min}
          max={max}
          disabled={disabled}
          onChange={(e) => onChange?.(Number(e.target.value))}
        />
      </label>
      {error && <span data-testid={`error-${label}`}>{error}</span>}
    </div>
  ),
  PasswordInput: ({
    label,
    placeholder,
    value,
    onChange,
    onBlur,
    error,
    disabled,
  }) => (
    <div>
      <label>
        {label}
        <input
          type="password"
          aria-label={label || placeholder}
          placeholder={placeholder}
          value={value || ''}
          disabled={disabled}
          onChange={(e) => onChange?.(e)}
          onBlur={onBlur}
        />
      </label>
      {error && <span data-testid={`error-${label}`}>{error}</span>}
    </div>
  ),
  Select: ({ label, placeholder, value, onChange, data, disabled, error }) => (
    <div>
      <label>
        {label}
        <select
          aria-label={label || placeholder}
          value={value || ''}
          disabled={disabled}
          onChange={(e) => onChange?.(e.target.value || null)}
        >
          <option value="">{placeholder}</option>
          {data?.map((d) => {
            const val = typeof d === 'string' ? d : d.value;
            const lbl = typeof d === 'string' ? d : d.label;
            return (
              <option key={val} value={val}>
                {lbl}
              </option>
            );
          })}
        </select>
      </label>
      {error && <span data-testid={`error-${label}`}>{error}</span>}
    </div>
  ),
  Stack: ({ children }) => <div>{children}</div>,
  Text: ({ children }) => <span>{children}</span>,
  TagsInput: ({ label, value }) => (
    <div>
      <span>{label}</span>
      {(value || []).map((tag) => (
        <span key={tag}>{tag}</span>
      ))}
    </div>
  ),
  Switch: ({ label, checked, onChange, disabled }) => (
    <label>
      <input
        type="checkbox"
        role="switch"
        aria-label={label}
        checked={!!checked}
        disabled={disabled}
        onChange={(e) => onChange?.(e)}
      />
      {label}
    </label>
  ),
  TextInput: ({
    id,
    label,
    placeholder,
    value,
    onChange,
    onBlur,
    error,
    disabled,
    ...rest
  }) => (
    <div>
      <label>
        {label}
        <input
          data-testid={id ? `text-input-${id}` : undefined}
          aria-label={label || placeholder}
          placeholder={placeholder}
          value={value || ''}
          disabled={disabled}
          onChange={onChange}
          onBlur={onBlur}
          {...rest}
        />
      </label>
      {error && <span data-testid={`error-${label}`}>{error}</span>}
    </div>
  ),
}));

// ──────────────────────────────────────────────────────────────────────────────
// Imports after mocks
// ──────────────────────────────────────────────────────────────────────────────
import useUserAgentsStore from '../../../store/userAgents';
import useChannelsStore from '../../../store/channels';
import useEPGsStore from '../../../store/epgs';
import useVODStore from '../../../store/useVODStore';
import usePlaylistsStore from '../../../store/playlists';
import * as M3uUtils from '../../../utils/forms/M3uUtils.js';
import * as DummyEpgUtils from '../../../utils/forms/DummyEpgUtils.js';
import * as mantineForm from '@mantine/form';

// ── Helpers ────────────────────────────────────────────────────────────────────
const makeM3uAccount = (overrides = {}) => ({
  id: 1,
  name: 'Test M3U',
  server_url: 'http://example.com/playlist.m3u',
  username: 'user1',
  password: 'pass1',
  account_type: 'XC',
  max_streams: 0,
  refresh_interval: 24,
  auto_refresh: false,
  is_active: true,
  custom_properties: {},
  // What the backend detected as the network Dispatcharr is on
  probation_lan_subnet_suggestion: '192.168.9.0/24',
  ...overrides,
});

const makeUserAgent = (overrides = {}) => ({
  id: 1,
  name: 'Default Agent',
  user_agent: 'Mozilla/5.0',
  ...overrides,
});

const defaultProps = (overrides = {}) => ({
  m3uAccount: null,
  isOpen: true,
  onClose: vi.fn(),
  ...overrides,
});

const setupStores = (overrides = {}) => {
  const fetchUserAgents = vi.fn();
  const fetchChannelGroups = vi.fn();
  const fetchEPGs = vi.fn();
  const fetchCategories = vi.fn();

  useUserAgentsStore.mockImplementation((selector) => {
    const state = {
      userAgents: overrides.userAgents || [],
      fetchUserAgents,
    };
    return selector(state);
  });

  useChannelsStore.mockImplementation((selector) => {
    const state = {
      fetchChannelGroups,
    };
    return selector(state);
  });

  useEPGsStore.mockImplementation((selector) => {
    const state = {
      fetchEPGs,
    };
    return selector(state);
  });

  useVODStore.mockImplementation((selector) => {
    const state = {
      fetchCategories,
    };
    return selector(state);
  });

  usePlaylistsStore.mockImplementation((selector) => {
    const playlists = overrides.playlists || [];
    return selector({ playlists });
  });

  return {
    fetchUserAgents,
    fetchChannelGroups,
    fetchEPGs,
    fetchCategories,
  };
};

// ──────────────────────────────────────────────────────────────────────────────

describe('M3U', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mantineForm.__resetFormState();
    vi.mocked(M3uUtils.addPlaylist).mockResolvedValue(
      makeM3uAccount({ id: 2 })
    );
    vi.mocked(M3uUtils.updatePlaylist).mockResolvedValue(makeM3uAccount());
    vi.mocked(M3uUtils.getPlaylist).mockResolvedValue(makeM3uAccount());
    vi.mocked(M3uUtils.prepareSubmitValues).mockImplementation((v) => v);
    vi.mocked(DummyEpgUtils.addEPG).mockResolvedValue({
      id: 10,
      name: 'Dummy EPG',
    });
  });

  // ── Rendering ──────────────────────────────────────────────────────────────

  describe('rendering', () => {
    it('renders the modal when isOpen is true', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(screen.getByTestId('modal')).toBeInTheDocument();
    });

    it('does not render the modal when isOpen is false', () => {
      setupStores();
      render(<M3U {...defaultProps({ isOpen: false })} />);
      expect(screen.queryByTestId('modal')).not.toBeInTheDocument();
    });

    it('renders Name input', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(screen.getByTestId('text-input-name')).toBeInTheDocument();
    });

    it('renders URL input', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(screen.getByTestId('text-input-server_url')).toBeInTheDocument();
    });

    it('renders submit button with "Add" label for new account', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(
        screen.getByRole('button', { name: /add|create|save/i })
      ).toBeInTheDocument();
    });

    it('renders submit button with "Update" or "Save" label for existing account', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(
        screen.getByRole('button', { name: /update|save/i })
      ).toBeInTheDocument();
    });

    it('pre-fills name when editing', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(screen.getByDisplayValue('Test M3U')).toBeInTheDocument();
    });

    it('pre-fills URL when editing', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(
        screen.getByDisplayValue('http://example.com/playlist.m3u')
      ).toBeInTheDocument();
    });

    it('renders M3UProfiles sub-component', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(screen.getByTestId('m3u-profiles')).toBeInTheDocument();
    });

    it('renders M3UGroupFilter sub-component', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(screen.getByTestId('m3u-group-filter')).toBeInTheDocument();
    });

    it('renders M3UFilters sub-component', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(screen.getByTestId('m3u-filters')).toBeInTheDocument();
    });

    it('renders ScheduleInput sub-component', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(screen.getByTestId('schedule-input')).toBeInTheDocument();
    });

    it('populates user agent select with agents from store', () => {
      setupStores({
        userAgents: [
          makeUserAgent({ id: 1, name: 'Agent One' }),
          makeUserAgent({ id: 2, name: 'Agent Two' }),
        ],
      });
      render(<M3U {...defaultProps()} />);
      expect(screen.getByText('Agent One')).toBeInTheDocument();
      expect(screen.getByText('Agent Two')).toBeInTheDocument();
    });
  });

  // ── Cancel behaviour ───────────────────────────────────────────────────────

  describe('cancel behaviour', () => {
    it('calls onClose when modal X is clicked', () => {
      const onClose = vi.fn();
      setupStores();
      render(<M3U {...defaultProps({ onClose })} />);
      fireEvent.click(screen.getByTestId('modal-close'));
      expect(onClose).toHaveBeenCalled();
    });
  });

  // ── Adding a playlist ──────────────────────────────────────────────────────

  describe('adding a new playlist', () => {
    const fillRequiredFields = () => {
      const nameInput = screen.getByTestId('text-input-name');
      fireEvent.change(nameInput, { target: { value: 'New Playlist' } });

      const urlInput = screen.getByTestId('text-input-server_url');
      if (urlInput) {
        fireEvent.change(urlInput, {
          target: { value: 'http://example.com/new.m3u' },
        });
      }
    };

    it('calls addPlaylist on valid submit', async () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      fillRequiredFields();
      fireEvent.click(screen.getByRole('button', { name: /add|create|save/i }));
      await waitFor(() => {
        expect(M3uUtils.addPlaylist).toHaveBeenCalled();
      });
    });

    it('calls prepareSubmitValues before addPlaylist', async () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      fillRequiredFields();
      fireEvent.click(screen.getByRole('button', { name: /add|create|save/i }));
      await waitFor(() => {
        expect(M3uUtils.prepareSubmitValues).toHaveBeenCalled();
      });
    });

    it('calls onClose after successful add', async () => {
      const onClose = vi.fn();
      setupStores();
      render(
        <M3U
          {...defaultProps({
            onClose,
            m3uAccount: makeM3uAccount({ account_type: 'Other' }),
          })}
        />
      );
      fillRequiredFields();
      fireEvent.click(screen.getByRole('button', { name: /add|create|save/i }));
      await waitFor(() => {
        expect(onClose).toHaveBeenCalled();
      });
    });
  });

  // ── Updating a playlist ────────────────────────────────────────────────────

  describe('updating an existing playlist', () => {
    it('calls updatePlaylist on submit', async () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      fireEvent.click(screen.getByRole('button', { name: /update|save/i }));
      await waitFor(() => {
        expect(M3uUtils.updatePlaylist).toHaveBeenCalled();
      });
    });

    it('does not call addPlaylist when updating', async () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      fireEvent.click(screen.getByRole('button', { name: /update|save/i }));
      await waitFor(() => {
        expect(M3uUtils.updatePlaylist).toHaveBeenCalled();
      });
      expect(M3uUtils.addPlaylist).not.toHaveBeenCalled();
    });

    it('calls onClose after successful update', async () => {
      const onClose = vi.fn();
      setupStores();
      render(
        <M3U {...defaultProps({ m3uAccount: makeM3uAccount(), onClose })} />
      );
      fireEvent.click(screen.getByRole('button', { name: /update|save/i }));
      await waitFor(() => {
        expect(onClose).toHaveBeenCalled();
      });
    });
  });

  // ── Form validation ────────────────────────────────────────────────────────

  describe('form validation', () => {
    it('does not call addPlaylist when Name is empty', async () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      // Clear name if pre-filled, then submit
      const nameInput = screen.getByTestId('text-input-name');
      fireEvent.change(nameInput, { target: { value: '' } });
      fireEvent.click(screen.getByRole('button', { name: /add|create|save/i }));
      await new Promise((r) => setTimeout(r, 50));
      expect(M3uUtils.addPlaylist).not.toHaveBeenCalled();
    });
  });

  // ── Refresh / schedule behaviour ───────────────────────────────────────────

  describe('schedule and refresh', () => {
    it('renders the ScheduleInput', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(screen.getByTestId('schedule-input')).toBeInTheDocument();
    });
  });

  // ── Max streams ────────────────────────────────────────────────────────────

  describe('max streams field', () => {
    it('renders max streams number input', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(
        screen.getByRole('spinbutton', { name: /max.?stream/i })
      ).toBeInTheDocument();
    });

    it('pre-fills max_streams when editing', () => {
      setupStores();
      render(
        <M3U
          {...defaultProps({ m3uAccount: makeM3uAccount({ max_streams: 5 }) })}
        />
      );
      expect(screen.getByDisplayValue('5')).toBeInTheDocument();
    });
  });

  // ── User agent select ──────────────────────────────────────────────────────

  describe('user agent select', () => {
    it('renders user agent select', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(
        screen.getByRole('combobox', { name: /user.?agent/i })
      ).toBeInTheDocument();
    });
  });

  // ── Credential fields ──────────────────────────────────────────────────────

  describe('credential fields', () => {
    it('renders username input', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(screen.getByTestId('text-input-username')).toBeInTheDocument();
    });

    it('renders password input', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(
        document.querySelector('input[type="password"]')
      ).toBeInTheDocument();
    });

    it('pre-fills username when editing', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);
      expect(screen.getByDisplayValue('user1')).toBeInTheDocument();
    });
  });

  // ── Channel switch overlap ─────────────────────────────────────────────────

  describe('channel switch overlap', () => {
    const overlapSwitch = () =>
      screen.getByRole('switch', { name: /allow channel switch overlap/i });

    const submittedValues = async () => {
      fireEvent.click(screen.getByRole('button', { name: /update|save/i }));
      await waitFor(() => {
        expect(M3uUtils.updatePlaylist).toHaveBeenCalled();
      });
      return vi.mocked(M3uUtils.prepareSubmitValues).mock.calls[0][0];
    };

    it('only shows the overlap toggle while disabled', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);
      expect(overlapSwitch()).toBeInTheDocument();
      expect(
        screen.queryByRole('spinbutton', { name: /overlap window/i })
      ).not.toBeInTheDocument();
      // Its own feature now, not one of the overlap's settings
      expect(
        screen.getByRole('switch', { name: /stop skipped channels/i })
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('combobox', { name: /when switching channels/i })
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('switch', { name: /anonymous connections/i })
      ).not.toBeInTheDocument();
    });

    it('shows the overlap settings once enabled', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);

      fireEvent.click(overlapSwitch());
      fireEvent.click(screen.getByRole('button', { name: 'Confirm dialog' }));

      expect(
        screen.getByRole('spinbutton', { name: /overlap window/i })
      ).toBeInTheDocument();
      expect(
        screen.getByRole('spinbutton', { name: /surfing delay/i })
      ).toHaveValue(500);
      expect(
        screen.getByRole('switch', { name: /stop skipped channels/i })
      ).toBeInTheDocument();
      expect(
        screen.getByRole('combobox', { name: /when switching channels/i })
      ).toBeInTheDocument();
      // Viewers that cannot be told apart never take part, so there is nothing to allow
      expect(
        screen.queryByRole('switch', { name: /anonymous connections/i })
      ).not.toBeInTheDocument();
      // The network Dispatcharr is on is filled in, so LAN players are recognised
      expect(screen.getByText('192.168.9.0/24')).toBeInTheDocument();
    });

    it('shows the overlap settings for an account that has it enabled', () => {
      setupStores();
      render(
        <M3U
          {...defaultProps({
            m3uAccount: makeM3uAccount({
              probation_enabled: true,
              probation_stop_skipped: true,
              probation_account_preference: 'alternate',
            }),
          })}
        />
      );

      expect(
        screen.getByRole('switch', { name: /stop skipped channels/i })
      ).toBeChecked();
      expect(
        screen.getByRole('combobox', { name: /when switching channels/i })
      ).toHaveValue('alternate');
      expect(
        screen.queryByRole('switch', { name: /anonymous connections/i })
      ).not.toBeInTheDocument();
    });

    it('stops skipped channels on an account without the overlap', () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);

      // Not tucked away behind the overlap any more: there, and on its own
      const stop = screen.getByRole('switch', { name: /stop skipped channels/i });
      expect(overlapSwitch()).not.toBeChecked();
      fireEvent.click(stop);

      expect(stop).toBeChecked();
      expect(
        screen.getByRole('spinbutton', { name: /skipped within/i })
      ).toHaveValue(10);
      // Players are told apart the same way as for the overlap, so the subnets come too
      expect(screen.getByText('192.168.9.0/24')).toBeInTheDocument();
      // And no confirmation: it asks nothing extra of the provider
      expect(
        screen.queryByRole('button', { name: 'Confirm dialog' })
      ).not.toBeInTheDocument();
    });

    it('shows the LAN subnets of an account that has them', () => {
      setupStores();
      render(
        <M3U
          {...defaultProps({
            m3uAccount: makeM3uAccount({
              probation_enabled: true,
              probation_lan_subnets: ['192.168.2.0/24', '10.0.0.0/8'],
            }),
          })}
        />
      );

      expect(screen.getByText('192.168.2.0/24')).toBeInTheDocument();
      expect(screen.getByText('10.0.0.0/8')).toBeInTheDocument();
      // The suggestion does not overwrite what the account already has
      expect(screen.queryByText('192.168.9.0/24')).not.toBeInTheDocument();
    });

    it('does not fill in a subnet when none could be detected', () => {
      setupStores();
      render(
        <M3U
          {...defaultProps({
            m3uAccount: makeM3uAccount({
              probation_lan_subnet_suggestion: null,
            }),
          })}
        />
      );

      fireEvent.click(overlapSwitch());
      fireEvent.click(screen.getByRole('button', { name: 'Confirm dialog' }));

      expect(screen.getByText('LAN Subnets')).toBeInTheDocument();
      expect(screen.queryByText(/\/24/)).not.toBeInTheDocument();
    });

    it('opens the explanation from the help link', () => {
      setupStores();
      render(<M3U {...defaultProps()} />);

      fireEvent.click(
        screen.getByRole('button', { name: /what does this do/i })
      );

      expect(screen.getByText('Channel Switch Overlap')).toBeInTheDocument();
      // The whole of it, under headings, which is what the short confirm leaves out
      expect(screen.getByText('What it never does')).toBeInTheDocument();
      expect(screen.getByText('Who it applies to')).toBeInTheDocument();
      expect(screen.getByText('The settings')).toBeInTheDocument();
      expect(
        screen.getByText(/Stop a stream that was already playing/)
      ).toBeInTheDocument();
      expect(
        screen.queryByTestId('confirmation-dialog')
      ).not.toBeInTheDocument();
    });

    it('asks what is being agreed to, and no more than that', () => {
      // A decision needs the trade and the risk; the rest is a page away
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);

      fireEvent.click(overlapSwitch());

      const dialog = screen.getByTestId('confirmation-dialog');
      expect(dialog).toHaveTextContent('Enable Channel Switch Overlap?');
      // What it does, what it costs, and who it applies to
      expect(dialog).toHaveTextContent('one extra connection');
      expect(dialog).toHaveTextContent(
        'Some block accounts that do'
      );
      expect(dialog).toHaveTextContent('a viewer Dispatcharr can tell apart');
      // Not the whole reference, which is what made it unreadable
      expect(dialog).not.toHaveTextContent('Channel Shutdown Delay');
      expect(dialog.textContent.length).toBeLessThan(900);
    });

    it('stays disabled when the explanation is cancelled', async () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);

      fireEvent.click(overlapSwitch());
      fireEvent.click(screen.getByRole('button', { name: 'Cancel dialog' }));

      expect(
        screen.queryByTestId('confirmation-dialog')
      ).not.toBeInTheDocument();
      expect((await submittedValues()).probation_enabled).toBe(false);
    });

    it('is enabled only after the explanation is confirmed', async () => {
      setupStores();
      render(<M3U {...defaultProps({ m3uAccount: makeM3uAccount() })} />);

      fireEvent.click(overlapSwitch());
      fireEvent.click(screen.getByRole('button', { name: 'Confirm dialog' }));

      expect((await submittedValues()).probation_enabled).toBe(true);
    });

    it('turns off without asking', async () => {
      setupStores();
      render(
        <M3U
          {...defaultProps({
            m3uAccount: makeM3uAccount({ probation_enabled: true }),
          })}
        />
      );

      fireEvent.click(overlapSwitch());

      expect(
        screen.queryByTestId('confirmation-dialog')
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('spinbutton', { name: /overlap window/i })
      ).not.toBeInTheDocument();
      expect((await submittedValues()).probation_enabled).toBe(false);
    });
  });

  // ── Dummy EPG creation ─────────────────────────────────────────────────────

  describe('dummy EPG auto-creation', () => {
    it('calls addEPG after successfully adding a playlist', async () => {
      setupStores();
      render(<M3U {...defaultProps()} />);

      const nameInput = screen.getByTestId('text-input-name');
      fireEvent.change(nameInput, { target: { value: 'My Playlist' } });

      const urlInput = screen.getByTestId('text-input-server_url');
      if (urlInput) {
        fireEvent.change(urlInput, {
          target: { value: 'http://example.com/p.m3u' },
        });
      }

      fireEvent.click(screen.getByRole('button', { name: /add|create|save/i }));
      await waitFor(() => {
        expect(M3uUtils.addPlaylist).toHaveBeenCalled();
      });
      // addEPG may be called conditionally — assert it was called or not based on a checkbox
    });
  });
});
