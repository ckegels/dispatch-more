import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, Library, RefreshCw, Search } from 'lucide-react';
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Image,
  LoadingOverlay,
  NativeSelect,
  Pagination,
  Paper,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
  useMantineTheme,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';
import { CustomTable, useTable } from './CustomTable';
import LogoPicker from './LogoPicker';
import LogoSources from './LogoSources';
import { sourceColor } from './logoLibraryColors';

// Laid out like the Channel Logos and VOD Logos tabs beside it, down to the sizes: the same
// panel, toolbar, table and pagination, so moving between the three reads as one page.

const PAGE_SIZES = ['25', '50', '100', '250'];

const whenBuilt = (seconds) => {
  if (!seconds) return 'never';
  const ago = Date.now() / 1000 - seconds;
  if (ago < 3600) return `${Math.max(1, Math.round(ago / 60))} min ago`;
  if (ago < 86400) return `${Math.round(ago / 3600)} h ago`;
  return `${Math.round(ago / 86400)} days ago`;
};

// A logo the size the other tabs show one, growing under the pointer the way they do
// The sizes the page is laid out around. A logo you cannot make out is no use for
// choosing between logos, and the rows were sized for text rather than for pictures.
const SHOWN = { width: 56, height: 40 };
const SMALLER = { width: 36, height: 26 };

const Preview = ({
  url,
  alt,
  width = SHOWN.width,
  height = SHOWN.height,
  grow = 1.5,
}) => (
  <Image
    src={url}
    alt={alt || ''}
    width={width}
    height={height}
    w={width}
    h={height}
    fit="contain"
    fallbackSrc="/logo.png"
    style={{ transition: 'transform 0.3s ease', cursor: 'pointer' }}
    onMouseEnter={(e) => {
      e.target.style.transform = `scale(${grow})`;
    }}
    onMouseLeave={(e) => {
      e.target.style.transform = 'scale(1)';
    }}
  />
);

// Where a suggestion came from, and what else is known about it, in one line of badges
const About = ({ suggestion, byHand }) => {
  if (byHand) {
    return (
      <Badge size="xs" variant="light" color="grape">
        {suggestion.source === 'upload'
          ? 'uploaded'
          : suggestion.source === 'link'
            ? 'your link'
            : 'chosen by hand'}
      </Badge>
    );
  }
  return (
    <Group gap={4} wrap="wrap">
      <Badge size="xs" variant="light" color={sourceColor(suggestion.source)}>
        {suggestion.source}
      </Badge>
      {suggestion.country && (
        <Badge size="xs" variant="outline" color="gray">
          {suggestion.country}
        </Badge>
      )}
      {/* Which of your guides, since there can be several with an icon each */}
      {suggestion.guide && (
        <Text size="xs" c="dimmed" lineClamp={1}>
          {suggestion.guide}
        </Text>
      )}
      {suggestion.width && suggestion.height && (
        <Text size="xs" c="dimmed">
          {suggestion.width}×{suggestion.height}
        </Text>
      )}
      {suggestion.closed && (
        <Badge size="xs" variant="light" color="orange">
          closed
        </Badge>
      )}
    </Group>
  );
};

// How much room something takes, in the words somebody would use for it
const roomTaken = (bytes) => {
  if (!bytes) return '0 MB';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

const LogoLibraryTable = () => {
  const theme = useMantineTheme();

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [show, setShow] = useState('suggested');
  const [search, setSearch] = useState('');
  // One group at a time, which is how anybody works through logos: a country's channels
  // share a look, and judging them together is the only way to see that one is wrong
  const [group, setGroup] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  // Which suggestion is chosen for each channel, by index; the first unless picked
  const [picked, setPicked] = useState({});
  // A logo chosen by hand for a channel -- searched for, linked or uploaded -- which wins
  // over anything suggested for it
  const [custom, setCustom] = useState({});
  // The channels ticked to be applied
  const [ticked, setTicked] = useState(new Set());
  const [confirming, setConfirming] = useState(false);
  const [building, setBuilding] = useState(false);
  const [forgetting, setForgetting] = useState(false);
  // The channel a logo is being chosen for by hand, if any
  const [pickerRow, setPickerRow] = useState(null);
  // The collections panel, folded away until wanted
  const [showSources, setShowSources] = useState(false);
  // A collection was added, switched or removed since the lists were last updated
  const [sourcesChanged, setSourcesChanged] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await API.getLogoLibrary(show));
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Could not load the logo library.');
    } finally {
      setLoading(false);
    }
  }, [show]);

  useEffect(() => {
    setPageIndex(0);
    load();
  }, [load]);

  // Searched here rather than asked for again: the list is already loaded, and asking the
  // server on every keystroke would send a request per letter
  const rows = useMemo(() => {
    const all = (data?.channels || []).map((row) => ({
      ...row,
      id: row.channel_id,
      // What has been chosen for this channel is carried on the row itself, and not only
      // read out of state inside the cell. The table redraws a row when its data is a
      // different object, and not otherwise -- so choosing a second logo for a channel
      // changed nothing on the screen. The first choice worked only because it also
      // ticked the row, and being ticked is one of the few things the table does watch.
      picked: picked[row.channel_id] || 0,
      by_hand: custom[row.channel_id] || null,
    }));
    const wanted = search.trim().toLowerCase();
    const found = wanted
      ? all.filter((row) => row.name.toLowerCase().includes(wanted))
      : all;
    return group
      ? found.filter((row) => String(row.group_id ?? '') === group)
      : found;
  }, [data, search, group, picked, custom]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const paginatedRows = useMemo(
    () => rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    [rows, pageIndex, pageSize]
  );

  const chosenFor = useCallback(
    (row) =>
      row.by_hand ||
      custom[row.channel_id] ||
      row.suggestions[row.picked ?? picked[row.channel_id] ?? 0],
    [custom, picked]
  );

  const tickedRows = useMemo(
    () => rows.filter((row) => ticked.has(row.channel_id) && chosenFor(row)),
    [rows, ticked, chosenFor]
  );

  // The table keeps what is ticked, the way the other tabs' tables do; it is told here
  // when a tick is made from somewhere else, such as choosing a logo by hand
  const tableRef = React.useRef(null);
  // Downloading the collections is minutes of asking; leaving the page must end it
  const onScreen = React.useRef(true);
  useEffect(() => {
    onScreen.current = true;
    return () => {
      onScreen.current = false;
    };
  }, []);
  const setTickedEverywhere = useCallback((next) => {
    setTicked(next);
    tableRef.current?.setSelectedTableIds?.([...next]);
  }, []);

  const tick = useCallback(
    (channelId, on) => {
      const next = new Set(ticked);
      if (on) next.add(channelId);
      else next.delete(channelId);
      setTickedEverywhere(next);
    },
    [ticked, setTickedEverywhere]
  );

  // Downloading the collections runs in the background, so the page asks until the
  // index it has is a newer one than before, and gives up after two minutes
  const rebuild = async () => {
    setBuilding(true);
    setError(null);
    const before = data?.status?.built_at || 0;
    try {
      await API.refreshLogoLibrary();
      for (let tries = 0; tries < 40; tries += 1) {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        if (!onScreen.current) return;
        const status = await API.getLogoLibraryStatus();
        if (status.built_at && status.built_at > before) {
          setSourcesChanged(false);
          await load();
          return;
        }
      }
      setError(
        'The logo lists are taking longer than usual. Try again in a moment.'
      );
    } catch (e) {
      setError(e?.body?.error || 'Could not update the logo lists.');
    } finally {
      setBuilding(false);
    }
  };

  const forget = async () => {
    setForgetting(true);
    setError(null);
    try {
      await API.forgetLogoLibrary();
      await load();
    } catch (e) {
      setError(e?.body?.error || 'Could not forget the downloaded lists.');
    } finally {
      setForgetting(false);
    }
  };

  const apply = async () => {
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      await API.applyLogoLibrary(
        tickedRows.map((row) => {
          const chosen = chosenFor(row);
          // An uploaded logo is on disk, not at an address, so it goes by id
          return chosen.logo_id
            ? { channel_id: row.channel_id, logo_id: chosen.logo_id }
            : {
                channel_id: row.channel_id,
                url: chosen.url,
                name: chosen.name,
              };
        })
      );
      setTickedEverywhere(new Set());
      setPicked({});
      setCustom({});
      await load();
    } catch (e) {
      setError(e?.body?.error || 'Could not apply those logos.');
    } finally {
      setBusy(false);
    }
  };

  const columns = useMemo(
    () => [
      {
        // Drawn by the table itself, the same small box with shift-click ranges that the
        // other tabs have; what is ticked comes back through onRowSelectionChange
        id: 'select',
        size: 50,
        enableSorting: false,
      },
      {
        header: 'Channel',
        accessorKey: 'name',
        size: 260,
        cell: ({ row }) => (
          <Box style={{ minWidth: 0 }}>
            <Text fw={500} size="sm" lineClamp={1}>
              {row.original.name}
            </Text>
            {row.original.number != null && (
              <Text size="xs" c="dimmed">
                {row.original.number}
              </Text>
            )}
          </Box>
        ),
      },
      {
        header: 'Now',
        accessorKey: 'current',
        size: 200,
        enableSorting: false,
        cell: ({ row }) => (
          <Group
            gap="sm"
            wrap="nowrap"
            style={{ minWidth: 0, paddingTop: 6, paddingBottom: 6 }}
          >
            <Center style={{ width: SHOWN.width, flexShrink: 0 }}>
              {row.original.current ? (
                <Preview
                  url={row.original.current.url}
                  alt={row.original.current.name}
                />
              ) : (
                <Text size="xs" c="dimmed">
                  —
                </Text>
              )}
            </Center>
            <Text size="xs" c="dimmed" lineClamp={1}>
              {row.original.current?.name || 'no logo'}
            </Text>
          </Group>
        ),
      },
      {
        header: 'Suggested',
        accessorKey: 'suggestions',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const original = row.original;
          const chosen = chosenFor(original);
          if (!chosen) {
            return (
              <Text size="xs" c="dimmed">
                Not suggested by any collection
              </Text>
            );
          }
          const current = original.picked ?? picked[original.channel_id] ?? 0;
          return (
            <Group
              gap="sm"
              wrap="nowrap"
              align="flex-start"
              style={{
                minWidth: 0,
                width: '100%',
                paddingTop: 6,
                paddingBottom: 6,
              }}
            >
              <Center style={{ width: SHOWN.width, flexShrink: 0 }}>
                {/* Named for what it is, not for the logo it happens to be: it is the one
                    that would be applied, and the smaller ones beside it are not */}
                <Preview
                  url={chosen.url}
                  alt={`Suggested logo for ${original.name}`}
                />
              </Center>
              <Box style={{ flexShrink: 0, maxWidth: '40%' }}>
                <About
                  suggestion={chosen}
                  byHand={!!(original.by_hand || custom[original.channel_id])}
                />
              </Box>
              {/* The others, small, to be chosen instead. They take what room is left and
                  go on to the next line, rather than running under the search button */}
              {!original.by_hand &&
                !custom[original.channel_id] &&
                original.suggestions.length > 1 && (
                  <Group
                    gap={6}
                    wrap="wrap"
                    // A row of the table has padding to its sides and none above or below,
                    // so once these wrap onto a second line they sit against the lines of
                    // the rows either side, squeezed with nothing to separate them
                    style={{
                      flex: 1,
                      minWidth: 0,
                      rowGap: 8,
                      paddingTop: 6,
                      paddingBottom: 6,
                    }}
                  >
                    {original.suggestions.map((suggestion, index) => (
                      <Box
                        key={suggestion.url}
                        role="button"
                        aria-label={`Choose ${suggestion.source} logo ${index + 1} for ${original.name}`}
                        onClick={(event) => {
                          // Choosing one of these is choosing it: it becomes the suggested
                          // logo and the row is ticked with it, the same as choosing one by
                          // hand from the search. Picking without ticking meant the logo you
                          // wanted was shown and then not applied.
                          event.stopPropagation();
                          setPicked({
                            ...picked,
                            [original.channel_id]: index,
                          });
                          tick(original.channel_id, true);
                        }}
                        style={{
                          borderRadius: 3,
                          padding: 1,
                          outline:
                            index === current
                              ? `1px solid ${theme.tailwind.green[5]}`
                              : '1px solid transparent',
                          display: 'flex',
                        }}
                      >
                        <Preview
                          url={suggestion.url}
                          alt={suggestion.name}
                          width={SMALLER.width}
                          height={SMALLER.height}
                          grow={2}
                        />
                      </Box>
                    ))}
                  </Group>
                )}
            </Group>
          );
        },
      },
      {
        id: 'actions',
        header: 'Actions',
        size: 80,
        enableSorting: false,
        cell: ({ row }) => (
          <Tooltip label="Search every logo, or use a link or a file">
            <ActionIcon
              variant="transparent"
              size="sm"
              color={theme.tailwind.blue[6]}
              aria-label={`Find a logo for ${row.original.name}`}
              onClick={(event) => {
                event.stopPropagation();
                setPickerRow(row.original);
              }}
            >
              <Search size={18} />
            </ActionIcon>
          </Tooltip>
        ),
      },
    ],
    [picked, custom, chosenFor, theme, tick]
  );

  const renderHeaderCell = (header) => (
    <Text size="sm" name={header.id}>
      {header.column.columnDef.header}
    </Text>
  );

  const table = useTable({
    columns,
    data: paginatedRows,
    allRowIds: paginatedRows.map((row) => row.id),
    enablePagination: false,
    enableRowSelection: true,
    onRowSelectionChange: (ids) => setTicked(new Set(ids)),
    enableRowVirtualization: false,
    renderTopToolbar: false,
    manualSorting: false,
    manualFiltering: false,
    manualPagination: true,
    headerCellRenderFns: {
      name: renderHeaderCell,
      current: renderHeaderCell,
      suggestions: renderHeaderCell,
      actions: renderHeaderCell,
    },
  });

  useEffect(() => {
    tableRef.current = table;
  }, [table]);

  const status = data?.status || {};
  const first = rows.length ? pageIndex * pageSize + 1 : 0;
  const last = Math.min((pageIndex + 1) * pageSize, rows.length);

  return (
    <>
      <Box
        style={{
          display: 'flex',
          justifyContent: 'center',
          padding: '0px',
          minHeight: 'calc(100vh - 200px)',
        }}
      >
        <Stack gap="md" style={{ maxWidth: '1200px', width: '100%' }}>
          <Paper
            style={{
              backgroundColor: '#27272A',
              border: '1px solid #3f3f46',
              borderRadius: 'var(--mantine-radius-md)',
            }}
          >
            {/* Top toolbar */}
            <Box
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                flexWrap: 'wrap',
                gap: 8,
                padding: '16px',
                borderBottom: '1px solid #3f3f46',
              }}
            >
              <Group gap="sm">
                <TextInput
                  placeholder="Filter by name..."
                  aria-label="Search channels"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.currentTarget.value);
                    setPageIndex(0);
                  }}
                  size="xs"
                  style={{ width: 200 }}
                />
                <Select
                  aria-label="Which group"
                  placeholder="Every group"
                  value={group}
                  onChange={(value) => {
                    setGroup(value || '');
                    setPageIndex(0);
                  }}
                  data={(data?.channel_groups || []).map((one) => ({
                    value: String(one.id),
                    label: one.name,
                  }))}
                  searchable
                  clearable
                  size="xs"
                  style={{ width: 200 }}
                />
                <Select
                  aria-label="Which channels"
                  value={show}
                  onChange={(value) => value && setShow(value)}
                  allowDeselect={false}
                  data={[
                    { value: 'suggested', label: 'With a suggestion' },
                    { value: 'missing', label: 'Without a logo' },
                    { value: 'all', label: 'All channels' },
                  ]}
                  size="xs"
                  style={{ width: 170 }}
                />
              </Group>

              <Group gap="sm">
                <Button
                  leftSection={<Library size={16} />}
                  variant="default"
                  size="xs"
                  onClick={() => setShowSources(!showSources)}
                >
                  {showSources ? 'Hide Collections' : 'Collections'}
                </Button>

                <Button
                  leftSection={<RefreshCw size={16} />}
                  variant={
                    sourcesChanged || (status.out_of_date || []).length > 0
                      ? 'filled'
                      : 'light'
                  }
                  size="xs"
                  loading={building}
                  onClick={rebuild}
                >
                  {status.built ? 'Update Lists' : 'Download Lists'}
                </Button>

                {/* The lists are a copy of something public. Keeping them costs room and
                    forgetting them costs the next download, which is seconds. */}
                {status.built && (
                  <Tooltip label="Throw the downloaded lists away. The collections are kept; only the copy is forgotten, and it comes back on the next update.">
                    <Button
                      variant="subtle"
                      color="gray"
                      size="xs"
                      loading={forgetting}
                      onClick={forget}
                    >
                      Free {roomTaken(status.bytes)}
                    </Button>
                  </Tooltip>
                )}

                <Button
                  leftSection={<Check size={18} />}
                  variant="light"
                  size="xs"
                  p={5}
                  disabled={!tickedRows.length || busy}
                  loading={busy}
                  onClick={() => setConfirming(true)}
                  color={theme.tailwind.green[5]}
                  style={{
                    borderWidth: '1px',
                    borderColor: theme.tailwind.green[5],
                    color: 'white',
                  }}
                >
                  Apply {tickedRows.length ? `(${tickedRows.length})` : ''}
                </Button>
              </Group>
            </Box>

            {/* What the lists hold, and anything that went wrong with them */}
            <Box
              style={{
                padding: '8px 16px',
                borderBottom: '1px solid #3f3f46',
              }}
            >
              <Text size="xs" c="dimmed">
                {status.built
                  ? `${Object.entries(status.counts || {})
                      .map(
                        ([source, count]) =>
                          `${count.toLocaleString()} from ${source}`
                      )
                      .join(' · ')} · updated ${whenBuilt(status.built_at)}`
                  : 'The logo lists have not been downloaded yet. Your own streams and guides are shown until they are.'}
                {' — '}
                Collections first, then your streams&apos; logos, then your
                guides. Nothing changes until channels are ticked and applied.
              </Text>
            </Box>

            {(error ||
              sourcesChanged ||
              (status.out_of_date || []).length > 0 ||
              Object.keys(status.errors || {}).length > 0) && (
              <Stack
                gap="xs"
                p="md"
                style={{ borderBottom: '1px solid #3f3f46' }}
              >
                {error && <Alert color="red">{error}</Alert>}
                {Object.entries(status.errors || {}).map(
                  ([source, message]) => (
                    <Alert key={source} color="orange">
                      {source} could not be reached, so its logos are missing:{' '}
                      {message}
                    </Alert>
                  )
                )}
                {(sourcesChanged || (status.out_of_date || []).length > 0) && (
                  <Alert color="blue">
                    {(status.out_of_date || []).length > 0
                      ? `${status.out_of_date.join(', ')} ${
                          status.out_of_date.length === 1 ? 'is' : 'are'
                        } switched on but was not in the last download, so none of its logos are being suggested yet. Update the lists to use it.`
                      : 'The collections have changed. Update the lists to use them.'}
                  </Alert>
                )}
              </Stack>
            )}

            {showSources && (
              <Box p="md" style={{ borderBottom: '1px solid #3f3f46' }}>
                <LogoSources onChanged={() => setSourcesChanged(true)} />
              </Box>
            )}

            {/* Table container */}
            <Box
              style={{
                position: 'relative',
                borderRadius:
                  '0 0 var(--mantine-radius-md) var(--mantine-radius-md)',
              }}
            >
              <Box style={{ overflow: 'auto', height: 'calc(100vh - 200px)' }}>
                <div style={{ minWidth: 760 }}>
                  <LoadingOverlay visible={loading} />
                  {rows.length === 0 && !loading ? (
                    <Center p="xl">
                      <Text size="sm" c="dimmed">
                        {search
                          ? 'No channel by that name.'
                          : 'Nothing to show.'}
                      </Text>
                    </Center>
                  ) : (
                    <CustomTable table={table} />
                  )}
                </div>
              </Box>

              {/* Pagination Controls */}
              <Box
                style={{
                  position: 'sticky',
                  bottom: 0,
                  zIndex: 3,
                  backgroundColor: '#27272A',
                  borderTop: '1px solid #3f3f46',
                }}
              >
                <Group gap={5} justify="center" style={{ padding: 8 }}>
                  <Text size="xs">Page Size</Text>
                  <NativeSelect
                    size="xxs"
                    value={String(pageSize)}
                    data={PAGE_SIZES}
                    onChange={(event) => {
                      setPageSize(parseInt(event.target.value, 10));
                      setPageIndex(0);
                    }}
                    style={{ paddingRight: 20 }}
                  />
                  <Pagination
                    total={pageCount}
                    value={pageIndex + 1}
                    onChange={(page) => setPageIndex(page - 1)}
                    size="xs"
                    withEdges
                    style={{ paddingRight: 20 }}
                  />
                  <Text size="xs">
                    {rows.length
                      ? `${first} to ${last} of ${rows.length}`
                      : '0 channels'}
                  </Text>
                </Group>
              </Box>
            </Box>
          </Paper>
        </Stack>
      </Box>

      <LogoPicker
        row={pickerRow}
        opened={!!pickerRow}
        onClose={() => setPickerRow(null)}
        onChoose={(choice) => {
          // Chosen for this channel and ticked, since choosing it is the point
          setCustom({ ...custom, [pickerRow.channel_id]: choice });
          tick(pickerRow.channel_id, true);
          setPickerRow(null);
        }}
      />

      <ConfirmationDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={apply}
        title={`Give ${tickedRows.length} channel${tickedRows.length === 1 ? '' : 's'} a new logo?`}
        message="Each ticked channel gets the logo shown for it. The logo it had stays in Dispatcharr, so it can be given back from the channel itself."
        confirmLabel="Apply"
      />
    </>
  );
};

export default LogoLibraryTable;
