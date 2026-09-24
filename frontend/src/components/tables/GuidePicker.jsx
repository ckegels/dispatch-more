import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  Badge,
  Box,
  Button,
  Group,
  Loader,
  Modal,
  Stack,
  Text,
  Select,
  TextInput,
} from '@mantine/core';
import API from '../../api';
import useAskAgainWhenNowChanges from '../../hooks/useAskAgainWhenNowChanges';

// Which guide a channel is on, and the choosing of another. Shared by the Lineup, where
// it sits on the row a channel would come out as, and by the Guides tab, where it changes
// what a suggestion suggests -- the same decision in both places, so the same window.

// A menu was tried first and was the wrong shape for the decision. Two entries called
// "ORF 1" from two sources read the same on one line, and there was nowhere to say what
// tells them apart. So each candidate is a card here: where it comes from, its tvg-id,
// how alike the names are, how many programmes it holds, and what is on it at this
// moment. An entry showing Zeit im Bild is the Austrian ORF 1; one holding no programmes
// at all is a name and nothing else, which no list of names can tell you.
// What a guide holds, said without guessing. Holding nothing means two different things:
// Dispatcharr reads a guide's programmes only once something uses it, so an entry no
// channel uses and that holds nothing has almost certainly never been read. Saying "no
// programmes" there would be telling someone a good guide is empty, so it offers to read
// it instead.
const WhatItHolds = ({ guide, onLoad, loading }) => {
  if (guide.programmes) {
    return (
      <>
        <Text size="xs" c="dimmed" style={{ wordBreak: 'break-all' }}>
          {guide.tvg_id || 'no tvg-id'} · {guide.programmes} programme
          {guide.programmes === 1 ? '' : 's'}
        </Text>
        <Text size="xs" c={guide.now ? undefined : 'dimmed'}>
          {guide.now ? `Now: ${guide.now}` : 'Nothing on it now'}
        </Text>
      </>
    );
  }
  // Until the server has said, nothing is claimed either way: the guide a row was
  // matched to is on the list from the start, but the plan's summary does not know what
  // it holds, and saying "not read yet" about it would be a guess
  if (guide.in_use === undefined) {
    return (
      <Text size="xs" c="dimmed" style={{ wordBreak: 'break-all' }}>
        {guide.tvg_id || 'no tvg-id'}
      </Text>
    );
  }
  return (
    <>
      <Text size="xs" c="dimmed" style={{ wordBreak: 'break-all' }}>
        {guide.tvg_id || 'no tvg-id'} ·{' '}
        {/* Somebody has looked: a guide listed in a source's channels with no programme
            of its own in it is common, and "not read yet" about one sends you round the
            same loop for ever */}
        {guide.read
          ? guide.read.why
            ? `could not be read: ${guide.read.why}`
            : 'read, and the guide has none'
          : guide.in_use
            ? 'no programmes'
            : 'programmes not read yet'}
      </Text>
      {guide.in_use ? (
        <Text size="xs" c="dimmed">
          Nothing on it now
        </Text>
      ) : loading ? (
        <Group gap={6}>
          <Loader size={10} />
          <Text size="xs" c="dimmed">
            Reading them, this can take a moment...
          </Text>
        </Group>
      ) : (
        <Button
          size="compact-xs"
          variant="subtle"
          aria-label={`Read the programmes of ${guide.name}`}
          onClick={(event) => {
            event.stopPropagation();
            onLoad(guide);
          }}
        >
          Read them, to see
        </Button>
      )}
    </>
  );
};

const GuideCard = ({ guide, picked, onPick, onLoad, loading }) => (
  <Box
    role="button"
    tabIndex={0}
    onClick={() => onPick(guide)}
    onKeyDown={(event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onPick(guide);
      }
    }}
    aria-label={`Guide ${guide ? guide.name : 'none'}`}
    style={{
      cursor: 'pointer',
      width: '100%',
      padding: '8px 10px',
      borderRadius: 6,
      border: `1px solid ${picked ? 'var(--mantine-color-blue-6)' : 'transparent'}`,
      background: picked ? 'rgba(34,139,230,0.12)' : 'rgba(0,0,0,0.18)',
    }}
  >
    {guide ? (
      <Stack gap={2}>
        <Group gap="xs" wrap="nowrap" justify="space-between">
          <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
            {guide.name}
          </Text>
          <Group gap={6} wrap="nowrap" style={{ flexShrink: 0 }}>
            {guide.source && (
              <Badge size="xs" variant="light" color="gray">
                {guide.source}
              </Badge>
            )}
            {guide.tier ? (
              <Badge
                size="xs"
                variant="light"
                color={
                  { certain: 'green', likely: 'blue' }[guide.tier] || 'gray'
                }
              >
                {{ certain: 'Certain', likely: 'Likely' }[guide.tier] ||
                  'A guess'}
                {guide.score != null ? ` · ${guide.score}%` : ''}
              </Badge>
            ) : (
              <Text size="xs" c="dimmed">
                {guide.how === 'tvg-id'
                  ? 'by tvg-id'
                  : guide.score != null
                    ? `${guide.score}%`
                    : ''}
              </Text>
            )}
          </Group>
        </Group>
        <WhatItHolds guide={guide} onLoad={onLoad} loading={loading} />
        {guide.why && (
          <Text size="xs" c="dimmed">
            Matched on {guide.why}
          </Text>
        )}
      </Stack>
    ) : (
      <Text size="sm">No guide</Text>
    )}
  </Box>
);

// As many as the server sends a search at a time (channel_manager.SEARCH_PAGE)
const SEARCH_PAGE = 100;

const GuideWindow = ({ channel, chosen, onChoose, onClose }) => {
  // The guide the row would come out with: the one chosen by hand, or the plan's
  const held = chosen === undefined ? channel?.epg : chosen;
  const [guides, setGuides] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  // A search shows everything that matches, a page at a time: how many are asked for,
  // and how many there are. Thousands of cards at once would lock the browser up.
  const [howMany, setHowMany] = useState(SEARCH_PAGE);
  const [total, setTotal] = useState(0);
  // One source to try on its own, rather than all of them at once. Two sources rarely
  // call a channel the same thing, and the way to find out which one has it is to look at
  // them one at a time -- which is a question about this channel, not a setting, so it is
  // here and not in the levers.
  const [source, setSource] = useState('');
  const [sources, setSources] = useState([]);
  // Guides whose programmes were asked for and have not arrived yet: {id: true}
  const [reading, setReading] = useState({});
  const [readState, setReadState] = useState({});
  // Reading a guide is a loop of up to twenty minutes; closing the window must end it
  const onScreen = useRef(true);
  useEffect(() => {
    onScreen.current = true;
    return () => {
      onScreen.current = false;
    };
  }, []);

  // Bumped when what is on one of the guides changes, to ask again without the spinner:
  // the list is the same list, only what is on it has moved on
  const [nowChanged, setNowChanged] = useState(0);
  const askedFor = useRef({});
  useAskAgainWhenNowChanges(
    guides.map((one) => one.changes_at),
    useCallback(() => setNowChanged((n) => n + 1), [])
  );

  // The typed search is the only thing that drives the question. It is deliberately not
  // touched by choosing one: the first try let a choice write itself into the search box,
  // which asked again for that one name and emptied the list of everything else.
  useEffect(() => {
    let dropped = false;
    const wanted = search.trim();
    // The same question asked again -- more of it, or what is on having moved on -- is
    // asked without the spinner and without emptying the list while it waits
    const same =
      askedFor.current.search === wanted && askedFor.current.source === source;
    const quietly = same && askedFor.current.at !== undefined;
    askedFor.current = { search: wanted, source, at: nowChanged };
    const timer = setTimeout(
      async () => {
        if (!quietly) setLoading(true);
        try {
          const result = await API.getChannelManagerGuides({
            name: channel?.name || '',
            tvg_id: channel?.epg?.tvg_id || '',
            q: wanted,
            current: held?.id ?? '',
            source,
            limit: wanted ? howMany : '',
          });
          if (!dropped) {
            setGuides(result?.guides || []);
            setTotal(result?.total || 0);
          }
        } catch {
          if (!dropped) setGuides([]);
        } finally {
          if (!dropped) setLoading(false);
        }
      },
      wanted && !quietly ? 300 : 0
    );
    return () => {
      dropped = true;
      clearTimeout(timer);
    };
  }, [
    search,
    source,
    channel?.name,
    channel?.epg?.tvg_id,
    held?.id,
    nowChanged,
    howMany,
  ]);

  // What sources there are, so one can be tried on its own; how much each holds is said,
  // because "try this source" is not a question anybody can answer from a name alone
  useEffect(() => {
    let dropped = false;
    API.getGuideMatching()
      .then((answer) => {
        if (!dropped) setSources(answer?.sources || []);
      })
      .catch(() => {
        if (!dropped) setSources([]);
      });
    return () => {
      dropped = true;
    };
  }, []);

  // Reading a guide's programmes is a task on the server, so the answer is not the point
  // it comes back at: the list is asked again until they show up, and given up on after a
  // while rather than left turning for ever. What is still empty then is empty.
  const readThem = useCallback(
    async (wanted) => {
      const ids = (Array.isArray(wanted) ? wanted : [wanted]).map(
        (one) => one.id
      );
      if (!ids.length) return;
      setReading((all) => ({
        ...all,
        ...Object.fromEntries(ids.map((id) => [id, true])),
      }));
      try {
        await API.loadChannelManagerGuide(ids);
      } catch {
        setReading((all) => ({
          ...all,
          ...Object.fromEntries(ids.map((id) => [id, false])),
        }));
        return;
      }
      // Reading is a pass of each source's whole file, which on a big guide is minutes.
      // So it is followed by what the task says it is doing rather than a fixed number
      // of tries, and says so: a spinner that says nothing looks like one that has hung.
      for (let tries = 0; tries < 400; tries += 1) {
        await new Promise((done) => setTimeout(done, 3000));
        if (!onScreen.current) return;
        let state = {};
        try {
          state = (await API.getChannelManagerReading())?.reading || {};
          setReadState(state);
        } catch {
          // The next try asks again
        }
        let back = null;
        try {
          // The same question the list is showing: the same source, and as many as
          // are shown, or the list jumps back to the first page while it reads
          const result = await API.getChannelManagerGuides({
            name: channel?.name || '',
            tvg_id: channel?.epg?.tvg_id || '',
            q: search.trim(),
            current: held?.id ?? '',
            source,
            limit: search.trim() ? howMany : '',
          });
          back = result?.guides || [];
          setGuides(back);
        } catch {
          // The next try asks again
        }
        if (
          back &&
          ids.every(
            (id) => (back.find((one) => one.id === id) || {}).programmes
          )
        ) {
          break;
        }
        if (!state.reading && tries > 2) break;
      }
      setReading((all) => ({
        ...all,
        ...Object.fromEntries(ids.map((id) => [id, false])),
      }));
      setReadState({});
    },
    [channel?.name, channel?.epg?.tvg_id, search, held?.id, source, howMany]
  );

  // The server puts what the channel has at the top of every answer, said the same way
  // as the rest; only a choice it has not been asked about yet is added here
  // -- except while one source is being tried on its own. The server leaves the guide
  // the channel has off the list when it is from another source, and putting it back
  // here put a card from another source at the top of a list that says it is one.
  const shown = useMemo(() => {
    if (!held || source || guides.some((guide) => guide.id === held.id)) {
      return guides;
    }
    return [held, ...guides];
  }, [guides, held, source]);

  const pickedId = held?.id ?? null;
  // How many of the search's matches are on the list: the guide the channel has is put
  // first whatever was searched for, and is not one of them
  const searchShown = guides.filter((guide) => guide.how === 'search').length;

  // The ones on the list nobody has read: reading them together costs one pass of the
  // file, the same as reading any one of them on its own
  const unread = useMemo(
    () =>
      shown.filter(
        (guide) =>
          !guide.programmes &&
          guide.in_use === false &&
          !guide.read &&
          !reading[guide.id]
      ),
    [shown, reading]
  );

  return (
    <Modal
      opened
      onClose={onClose}
      title={`Guide for ${channel?.name || 'this channel'}`}
      size="lg"
    >
      <Stack gap="sm">
        <Group gap="xs" align="flex-end" wrap="wrap">
          <TextInput
            size="xs"
            label="Search every guide"
            placeholder="A name or a tvg-id"
            aria-label="Search every guide"
            value={search}
            onChange={(event) => {
              setSearch(event.currentTarget.value);
              setHowMany(SEARCH_PAGE);
            }}
            rightSection={loading ? <Loader size={12} /> : undefined}
            style={{ flex: 1, minWidth: 200 }}
          />
          <Select
            size="xs"
            label="From one source"
            placeholder="Every source"
            aria-label="From one source"
            value={source}
            onChange={(value) => {
              setSource(value || '');
              setHowMany(SEARCH_PAGE);
            }}
            data={(sources || [])
              .filter((one) => one.active)
              .map((one) => ({
                value: String(one.id),
                label: `${one.name} (${one.holds})`,
              }))}
            searchable
            clearable
            style={{ width: 200 }}
          />
          {unread.length > 0 && (
            <Button
              size="xs"
              variant="default"
              onClick={() => readThem(unread)}
              aria-label="Read the programmes of every guide shown"
            >
              Read all {unread.length}
            </Button>
          )}
        </Group>
        <Text size="xs" c="dimmed">
          A guide nobody uses has not been read yet: Dispatcharr reads a
          guide&apos;s programmes when it goes on a channel. Reading one means
          going through the whole guide file, so reading them all together costs
          no more than reading one.
        </Text>
        {readState.reading && (
          <Text size="xs" c="dimmed">
            Reading guides · {readState.stage || 'asking for them'}
            {readState.at ? ` · ${readState.at}` : ''} · {readState.done || 0}{' '}
            of {readState.total || 0}
          </Text>
        )}
        {search.trim() && total > 0 && (
          <Text size="xs" c="dimmed">
            {total} guide{total === 1 ? '' : 's'} match
            {total === 1 ? 'es' : ''}
            {searchShown < total ? ` · the first ${searchShown} shown` : ''}
          </Text>
        )}
        <Stack gap={6} style={{ maxHeight: '50vh', overflowY: 'auto' }}>
          {shown.length === 0 && !loading && (
            <Text size="xs" c="dimmed">
              {search.trim()
                ? 'No guide of that name.'
                : 'No guide is enough like this channel. Search for one by name.'}
            </Text>
          )}
          {shown.map((guide) => (
            <GuideCard
              key={guide.id}
              guide={guide}
              picked={guide.id === pickedId}
              onPick={onChoose}
              onLoad={readThem}
              loading={!!reading[guide.id]}
            />
          ))}
          {search.trim() && searchShown < total && (
            <Button
              size="xs"
              variant="subtle"
              loading={loading}
              onClick={() => setHowMany((n) => n + SEARCH_PAGE)}
            >
              Show {Math.min(SEARCH_PAGE, total - searchShown)} more of {total}
            </Button>
          )}
          <GuideCard guide={null} picked={pickedId == null} onPick={onChoose} />
        </Stack>
      </Stack>
    </Modal>
  );
};

// The guide as the row would come out, and the way to change it. Nothing is asked of the
// server until the window is opened: with Expand all that would be a question per row.
const GuidePicker = ({ row, chosen, onChoose }) => {
  const channel = row.channel;
  const [open, setOpen] = useState(false);
  const guide = chosen === undefined ? channel?.epg : chosen;

  return (
    <Group gap="xs" wrap="nowrap" align="center">
      <Text size="xs" c={guide ? undefined : 'orange'} style={{ minWidth: 0 }}>
        Guide: {guide ? guide.name : 'none'}
        {guide?.source ? ` · ${guide.source}` : ''}
      </Text>
      <Button
        size="compact-xs"
        variant="default"
        aria-label={`Change the guide for ${channel?.name || 'this channel'}`}
        onClick={() => setOpen(true)}
      >
        Change
      </Button>
      {open && (
        <GuideWindow
          channel={channel}
          chosen={chosen}
          onClose={() => setOpen(false)}
          onChoose={(picked) => {
            onChoose(row.key, picked || null);
            setOpen(false);
          }}
        />
      )}
    </Group>
  );
};

export { GuideWindow, GuidePicker };
export default GuidePicker;
