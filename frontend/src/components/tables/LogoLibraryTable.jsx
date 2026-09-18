import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Group,
  Loader,
  SegmentedControl,
  Stack,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';

// How many rows are drawn at once. Every row can carry several images from other sites,
// and a thousand channels drawn together is a page that takes a while to settle.
const PAGE = 100;

const SOURCE_LABELS = {
  'tv-logos': 'tv-logos',
  'iptv-org': 'iptv-org',
};

const whenBuilt = (seconds) => {
  if (!seconds) return 'never';
  const ago = Date.now() / 1000 - seconds;
  if (ago < 3600) return `${Math.max(1, Math.round(ago / 60))} min ago`;
  if (ago < 86400) return `${Math.round(ago / 3600)} h ago`;
  return `${Math.round(ago / 86400)} days ago`;
};

const Thumb = ({ url, size = 56, onClick, selected }) => (
  <Box
    onClick={onClick}
    style={{
      width: size,
      height: size * 0.62,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      // A logo is often white on nothing, so it sits on a mid tone that suits both
      background: 'rgba(128,128,128,0.18)',
      borderRadius: 4,
      cursor: onClick ? 'pointer' : 'default',
      outline: selected ? '2px solid var(--mantine-color-teal-5)' : 'none',
      flexShrink: 0,
    }}
  >
    {url ? (
      <img
        src={url}
        alt=""
        loading="lazy"
        style={{ maxWidth: '92%', maxHeight: '92%', objectFit: 'contain' }}
      />
    ) : (
      <Text size="xs" c="dimmed">
        none
      </Text>
    )}
  </Box>
);

// What a suggestion is, in a line: which collection, which country, and its size where
// the collection says, which is the quickest way to tell a proper logo from a thumbnail
const About = ({ suggestion }) => (
  <Group gap={4} wrap="nowrap">
    <Badge
      size="xs"
      variant="light"
      color={suggestion.source === 'tv-logos' ? 'teal' : 'blue'}
    >
      {SOURCE_LABELS[suggestion.source] || suggestion.source}
    </Badge>
    {suggestion.country && (
      <Badge size="xs" variant="outline" color="gray">
        {suggestion.country}
      </Badge>
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

const LogoLibraryTable = () => {
  const [data, setData] = useState(null);
  const [show, setShow] = useState('suggested');
  const [search, setSearch] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(0);
  // Which suggestion is chosen for each channel, by index; the first unless picked
  const [picked, setPicked] = useState({});
  // The channels ticked to be applied
  const [ticked, setTicked] = useState({});
  const [confirming, setConfirming] = useState(false);
  const [building, setBuilding] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await API.getLogoLibrary(show));
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Could not load the logo library.');
    }
  }, [show]);

  useEffect(() => {
    setPage(0);
    load();
  }, [load]);

  // Searched here rather than asked for again: the list is already loaded, and asking the
  // server on every keystroke would send a request per letter
  const rows = useMemo(() => {
    const all = data?.channels || [];
    const wanted = search.trim().toLowerCase();
    return wanted ? all.filter((row) => row.name.toLowerCase().includes(wanted)) : all;
  }, [data, search]);
  const shown = rows.slice(page * PAGE, (page + 1) * PAGE);
  const chosenFor = (row) => row.suggestions[picked[row.channel_id] || 0];

  const tickedRows = useMemo(
    () => rows.filter((row) => ticked[row.channel_id] && chosenFor(row)),
    // chosenFor reads picked, which is listed
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, ticked, picked]
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
        const status = await API.getLogoLibraryStatus();
        if (status.built_at && status.built_at > before) {
          await load();
          return;
        }
      }
      setError('The logo lists are taking longer than usual. Try again in a moment.');
    } catch (e) {
      setError(e?.body?.error || 'Could not update the logo lists.');
    } finally {
      setBuilding(false);
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
          return { channel_id: row.channel_id, url: chosen.url, name: chosen.name };
        })
      );
      setTicked({});
      setPicked({});
      await load();
    } catch (e) {
      setError(e?.body?.error || 'Could not apply those logos.');
    } finally {
      setBusy(false);
    }
  };

  const tickAllShown = (on) => {
    const next = { ...ticked };
    rows.forEach((row) => {
      if (row.suggestions.length) next[row.channel_id] = on;
    });
    setTicked(next);
  };

  if (!data) return <Loader size="sm" m="md" />;

  const status = data.status || {};

  return (
    <Stack gap="sm" maw={1200} mx="auto" px="md">
      <Group justify="space-between" wrap="wrap">
        <Text size="sm" c="dimmed">
          {status.built
            ? `${Object.entries(status.counts || {})
                .map(([source, count]) => `${count.toLocaleString()} from ${source}`)
                .join(' · ')} · updated ${whenBuilt(status.built_at)}`
            : 'The logo lists have not been downloaded yet.'}
        </Text>
        <Button size="xs" variant="light" loading={building} onClick={rebuild}>
          {status.built ? 'Update logo lists' : 'Download logo lists'}
        </Button>
      </Group>

      {Object.entries(status.errors || {}).map(([source, message]) => (
        <Alert key={source} color="orange">
          {source} could not be reached, so its logos are missing: {message}
        </Alert>
      ))}
      {error && <Alert color="red">{error}</Alert>}

      <Text size="xs" c="dimmed">
        Each channel is looked up by name in public logo collections. What it has
        now is on the left, what the collections have for it on the right, best
        first: the channel&apos;s own country, then tv-logos (whose links last),
        then iptv-org. Click another to choose it. Nothing changes until you tick
        channels and apply them.
      </Text>

      <Group justify="space-between" wrap="wrap">
        <Group gap="xs">
          <SegmentedControl
            size="xs"
            value={show}
            onChange={setShow}
            data={[
              { value: 'suggested', label: 'With a suggestion' },
              { value: 'missing', label: 'Without a logo' },
              { value: 'all', label: 'All channels' },
            ]}
          />
          <TextInput
            size="xs"
            w={200}
            placeholder="Search channels"
            aria-label="Search channels"
            value={search}
            onChange={(event) => {
              setSearch(event.currentTarget.value);
              setPage(0);
            }}
          />
        </Group>
        <Group gap="xs">
          <Button size="xs" variant="subtle" onClick={() => tickAllShown(true)}>
            Tick all
          </Button>
          <Button size="xs" variant="subtle" onClick={() => setTicked({})}>
            Untick all
          </Button>
          <Button
            size="xs"
            disabled={!tickedRows.length || busy}
            loading={busy}
            onClick={() => setConfirming(true)}
          >
            Apply {tickedRows.length || ''}
          </Button>
        </Group>
      </Group>

      {rows.length === 0 ? (
        <Text size="sm" c="dimmed">
          {status.built
            ? 'Nothing to show.'
            : 'Download the logo lists to see what they have for your channels.'}
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={760} type="native">
          <Table striped withTableBorder verticalSpacing={6} fz="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={36} />
                <Table.Th w="26%">Channel</Table.Th>
                <Table.Th w="22%">Now</Table.Th>
                <Table.Th>Suggested</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {shown.map((row) => {
                const chosen = chosenFor(row);
                return (
                  <Table.Tr key={row.channel_id}>
                    <Table.Td>
                      <Checkbox
                        aria-label={`Use the suggested logo for ${row.name}`}
                        disabled={!chosen}
                        checked={!!ticked[row.channel_id]}
                        onChange={(event) =>
                          setTicked({
                            ...ticked,
                            [row.channel_id]: event.currentTarget.checked,
                          })
                        }
                      />
                    </Table.Td>
                    <Table.Td style={{ wordBreak: 'break-word' }}>
                      <Text size="sm">{row.name}</Text>
                      {row.number != null && (
                        <Text size="xs" c="dimmed">
                          {row.number}
                        </Text>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <Group gap="xs" wrap="nowrap">
                        <Thumb url={row.current?.url} />
                        <Text size="xs" c="dimmed" lineClamp={2}>
                          {row.current?.name || 'no logo'}
                        </Text>
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      {chosen ? (
                        <Group gap="xs" wrap="nowrap" align="center">
                          <Thumb
                            url={chosen.url}
                            size={72}
                            selected={!!ticked[row.channel_id]}
                          />
                          <Stack gap={4}>
                            <About suggestion={chosen} />
                            {/* The others, small, to be chosen instead */}
                            {row.suggestions.length > 1 && (
                              <Group gap={4} wrap="wrap">
                                {row.suggestions.map((suggestion, index) => (
                                  <Thumb
                                    key={suggestion.url}
                                    url={suggestion.url}
                                    size={34}
                                    selected={
                                      index === (picked[row.channel_id] || 0)
                                    }
                                    onClick={() =>
                                      setPicked({
                                        ...picked,
                                        [row.channel_id]: index,
                                      })
                                    }
                                  />
                                ))}
                              </Group>
                            )}
                          </Stack>
                        </Group>
                      ) : (
                        <Text size="xs" c="dimmed">
                          Not in any collection
                        </Text>
                      )}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      {rows.length > PAGE && (
        <Group justify="center" gap="xs">
          <Button
            size="xs"
            variant="subtle"
            disabled={page === 0}
            onClick={() => setPage(page - 1)}
          >
            Previous
          </Button>
          <Text size="xs" c="dimmed">
            {page * PAGE + 1}–{Math.min((page + 1) * PAGE, rows.length)} of{' '}
            {rows.length}
          </Text>
          <Button
            size="xs"
            variant="subtle"
            disabled={(page + 1) * PAGE >= rows.length}
            onClick={() => setPage(page + 1)}
          >
            Next
          </Button>
        </Group>
      )}

      <ConfirmationDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={apply}
        title={`Give ${tickedRows.length} channel${
          tickedRows.length === 1 ? '' : 's'
        } a new logo?`}
        message="Each ticked channel gets the logo shown for it on the right. The logo it had stays in Dispatcharr, so it can be given back from the channel itself."
        confirmLabel="Apply"
      />
    </Stack>
  );
};

export default LogoLibraryTable;
