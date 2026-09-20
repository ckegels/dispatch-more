import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronsDownUp,
  ChevronsUpDown,
  EyeOff,
  Play,
  RotateCcw,
  SlidersHorizontal,
  Undo2,
  X,
} from 'lucide-react';
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  LoadingOverlay,
  Modal,
  NativeSelect,
  Pagination,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Tooltip,
  useMantineTheme,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';
import ChannelManagerLevers from '../forms/ChannelManagerLevers';
import { CustomTable, useTable } from './CustomTable';
import { Logo, Watch } from './StreamParts';

// Laid out like Find Logos and the Logo Manager tabs: the same panel, toolbar, table and
// pagination. Each row is one channel as it would come out, and opens to show every stream
// that goes into it, before and after.

const PAGE_SIZES = ['25', '50', '100', '250'];

// The last entry of a group picker: not a group, a way to make one
const NEW_GROUP = '__new__';

const STATUS = {
  new: { label: 'New', color: 'green' },
  merge: { label: 'Merge', color: 'blue' },
  combine: { label: 'Combine', color: 'grape' },
  conflict: { label: 'Conflict', color: 'orange' },
  unchanged: { label: 'Unchanged', color: 'gray' },
};

// What is being looked at, which going back to the defaults leaves as it is
const SCOPE_LEVERS = [
  'accounts',
  'stream_groups',
  'channel_groups',
  'target_group',
  'profiles',
];

const QUALITY_COLOR = { '4K': 'grape', FHD: 'teal', HD: 'blue', SD: 'gray' };

const Quality = ({ stream }) =>
  stream.custom ? (
    <Badge size="xs" variant="light" color="yellow">
      fallback
    </Badge>
  ) : (
    <Badge
      size="xs"
      variant="light"
      color={QUALITY_COLOR[stream.quality] || 'dark'}
    >
      {stream.quality || '?'}
      {stream.probed ? ' ✓' : ''}
    </Badge>
  );

// Up and down, for putting a channel's streams in the order they should be tried
const Move = ({ stream, onMove, first, last }) => (
  <Group gap={0} wrap="nowrap" style={{ flexShrink: 0 }}>
    <ActionIcon
      size="xs"
      variant="subtle"
      color="gray"
      aria-label={`Move ${stream.name} up`}
      disabled={first}
      onClick={() => onMove(stream.id, -1)}
    >
      <ArrowUp size={12} />
    </ActionIcon>
    <ActionIcon
      size="xs"
      variant="subtle"
      color="gray"
      aria-label={`Move ${stream.name} down`}
      disabled={last}
      onClick={() => onMove(stream.id, 1)}
    >
      <ArrowDown size={12} />
    </ActionIcon>
  </Group>
);

// One stream, with everything worth knowing about it on one line
// The name first; what it is and where it is from underneath, on as many lines as it takes,
// so a long one grows the row downwards rather than running off to the right
const StreamLine = ({ stream, move, onDrop }) => (
  <Group
    gap={6}
    wrap="nowrap"
    align="flex-start"
    style={{
      minWidth: 0,
      opacity: stream.in_scope === false && !stream.custom ? 0.6 : 1,
      textDecoration:
        stream.removed || stream.dropped ? 'line-through' : 'none',
    }}
  >
    {move}
    <Watch stream={stream} />
    <Quality stream={stream} />
    <Box style={{ minWidth: 0, flex: 1 }}>
      <Text
        size="xs"
        c={stream.removed ? 'red' : stream.added ? 'green' : undefined}
        style={{ wordBreak: 'break-word' }}
      >
        {stream.added ? '+ ' : stream.removed ? '− ' : ''}
        {stream.name}
      </Text>
      <Group gap={6} wrap="wrap">
        <Badge size="xs" variant="outline" color="gray">
          {stream.account}
        </Badge>
        {stream.group && (
          <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>
            {stream.group}
          </Text>
        )}
        {stream.tvg_id && (
          <Text
            size="xs"
            c="dimmed"
            ff="monospace"
            style={{ wordBreak: 'break-all' }}
          >
            {stream.tvg_id}
          </Text>
        )}
      </Group>
    </Box>
    {/* Taken out of this row: not added, or taken off the channel. Never the fallback. */}
    {onDrop && !stream.custom && !stream.removed && (
      <Tooltip
        label={stream.dropped ? 'Keep it after all' : 'Take this stream out'}
      >
        <ActionIcon
          size="xs"
          variant={stream.dropped ? 'filled' : 'subtle'}
          color="red"
          aria-label={`${stream.dropped ? 'Keep' : 'Drop'} ${stream.name}`}
          onClick={() => onDrop(stream.id)}
          style={{ flexShrink: 0 }}
        >
          {stream.dropped ? <Undo2 size={12} /> : <X size={12} />}
        </ActionIcon>
      </Tooltip>
    )}
  </Group>
);

// The streams that can be moved: not one taken off, and not the fallback, which stays last
const movable = (stream) => !stream.removed && !stream.custom;

// Which guide a channel gets, chosen in a window of its own rather than a menu.
//
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
        {guide.in_use ? 'no programmes' : 'programmes not read yet'}
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

// The whole card chooses the guide, but it carries a button of its own for reading the
// programmes, and a button inside a button is not a thing a browser will render. So the
// card says what it is rather than being a <button>.
// Which guide a channel is on, said the same way before and after so the two lines can
// be read against each other
const GuideLine = ({ guide, how }) => (
  <>
    <Text
      size="xs"
      c={guide ? 'dimmed' : 'orange'}
      style={{ wordBreak: 'break-word' }}
    >
      {guide
        ? `Guide: ${guide.name}${guide.source ? ` · ${guide.source}` : ''}${how || ''}`
        : 'No guide'}
    </Text>
    {/* What is on it now, which is what says whether it is the right guide at all: a
        name can be right and the guide still be somebody else's, or empty */}
    {guide && guide.now && (
      <Text size="xs" c="dimmed" lineClamp={1}>
        Now: {guide.now}
      </Text>
    )}
    {guide && !guide.now && guide.programmes === 0 && (
      <Text size="xs" c="orange">
        Holds no programmes
      </Text>
    )}
  </>
);

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
            <Text size="xs" c="dimmed">
              {guide.how === 'tvg-id'
                ? 'by tvg-id'
                : guide.score != null
                  ? `${guide.score}%`
                  : ''}
            </Text>
          </Group>
        </Group>
        <WhatItHolds guide={guide} onLoad={onLoad} loading={loading} />
      </Stack>
    ) : (
      <Text size="sm">No guide</Text>
    )}
  </Box>
);

const GuideWindow = ({ channel, chosen, onChoose, onClose }) => {
  // The guide the row would come out with: the one chosen by hand, or the plan's
  const held = chosen === undefined ? channel?.epg : chosen;
  const [guides, setGuides] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  // Guides whose programmes were asked for and have not arrived yet: {id: true}
  const [reading, setReading] = useState({});
  const [readState, setReadState] = useState({});

  // The typed search is the only thing that drives the question. It is deliberately not
  // touched by choosing one: the first try let a choice write itself into the search box,
  // which asked again for that one name and emptied the list of everything else.
  useEffect(() => {
    let dropped = false;
    const wanted = search.trim();
    const timer = setTimeout(
      async () => {
        setLoading(true);
        try {
          const result = await API.getChannelManagerGuides({
            name: channel?.name || '',
            tvg_id: channel?.epg?.tvg_id || '',
            q: wanted,
            current: held?.id ?? '',
          });
          if (!dropped) setGuides(result?.guides || []);
        } catch {
          if (!dropped) setGuides([]);
        } finally {
          if (!dropped) setLoading(false);
        }
      },
      wanted ? 300 : 0
    );
    return () => {
      dropped = true;
      clearTimeout(timer);
    };
  }, [search, channel?.name, channel?.epg?.tvg_id, held?.id]);

  // Reading a guide's programmes is a task on the server, so the answer is not the point
  // it comes back at: the list is asked again until they show up, and given up on after a
  // while rather than left turning for ever. What is still empty then is empty.
  const readThem = useCallback(
    async (wanted) => {
      const ids = (Array.isArray(wanted) ? wanted : [wanted]).map((one) => one.id);
      if (!ids.length) return;
      setReading((all) => ({ ...all, ...Object.fromEntries(ids.map((id) => [id, true])) }));
      try {
        await API.loadChannelManagerGuide(ids);
      } catch {
        setReading((all) => ({ ...all, ...Object.fromEntries(ids.map((id) => [id, false])) }));
        return;
      }
      // Reading is a pass of each source's whole file, which on a big guide is minutes.
      // So it is followed by what the task says it is doing rather than a fixed number
      // of tries, and says so: a spinner that says nothing looks like one that has hung.
      for (let tries = 0; tries < 400; tries += 1) {
        await new Promise((done) => setTimeout(done, 3000));
        let state = {};
        try {
          state = (await API.getChannelManagerReading())?.reading || {};
          setReadState(state);
        } catch {
          // The next try asks again
        }
        let back = null;
        try {
          const result = await API.getChannelManagerGuides({
            name: channel?.name || '',
            tvg_id: channel?.epg?.tvg_id || '',
            q: search.trim(),
            current: held?.id ?? '',
          });
          back = result?.guides || [];
          setGuides(back);
        } catch {
          // The next try asks again
        }
        if (back && ids.every((id) => (back.find((one) => one.id === id) || {}).programmes)) {
          break;
        }
        if (!state.reading && tries > 2) break;
      }
      setReading((all) => ({ ...all, ...Object.fromEntries(ids.map((id) => [id, false])) }));
      setReadState({});
    },
    [channel?.name, channel?.epg?.tvg_id, search, held?.id]
  );



  // The server puts what the channel has at the top of every answer, said the same way
  // as the rest; only a choice it has not been asked about yet is added here
  const shown = useMemo(() => {
    if (!held || guides.some((guide) => guide.id === held.id)) return guides;
    return [held, ...guides];
  }, [guides, held]);

  const pickedId = held?.id ?? null;

  // The ones on the list nobody has read: reading them together costs one pass of the
  // file, the same as reading any one of them on its own
  const unread = useMemo(
    () =>
      shown.filter(
        (guide) => !guide.programmes && guide.in_use === false && !reading[guide.id]
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
            onChange={(event) => setSearch(event.currentTarget.value)}
            rightSection={loading ? <Loader size={12} /> : undefined}
            style={{ flex: 1, minWidth: 200 }}
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
          A guide nobody uses has not been read yet: Dispatcharr reads a guide&apos;s
          programmes when it goes on a channel. Reading one means going through the whole
          guide file, so reading them all together costs no more than reading one.
        </Text>
        {readState.reading && (
          <Text size="xs" c="dimmed">
            Reading guides · {readState.stage || 'asking for them'}
            {readState.at ? ` · ${readState.at}` : ''} ·{' '}
            {readState.done || 0} of {readState.total || 0}
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

const Expanded = ({
  row,
  onMove,
  groups,
  chosenGroup,
  onGroup,
  onMakeGroup,
  onDrop,
  chosenName,
  onName,
  chosenGuide,
  onGuide,
}) => {
  const moving = row.streams.filter(movable);
  // Making a group from the row, rather than leaving the page to go and make one
  const [naming, setNaming] = useState(false);
  const [newGroup, setNewGroup] = useState('');
  const [making, setMaking] = useState(false);
  const [groupError, setGroupError] = useState(null);
  return (
    <Box
      p="sm"
      style={{ background: 'rgba(0,0,0,0.18)', minWidth: 0, maxWidth: '100%' }}
    >
      {(row.status === 'new' || row.status === 'combine') && (
        <Group gap="xs" mb="sm" align="flex-end" wrap="wrap">
          <Select
            size="xs"
            label="Channel group"
            aria-label={`Channel group for ${row.channel.name}`}
            searchable
            allowDeselect={false}
            data={groups}
            value={naming ? NEW_GROUP : String(chosenGroup ?? row.channel.group_id ?? '')}
            onChange={(group) => {
              if (!group) return;
              if (group === NEW_GROUP) return setNaming(true);
              setNaming(false);
              onGroup(row.key, Number(group));
            }}
            style={{ width: 260 }}
          />
          {naming && (
            <Group gap="xs" align="flex-end">
              <TextInput
                size="xs"
                label="Its name"
                aria-label="Name for the new group"
                value={newGroup}
                onChange={(event) => setNewGroup(event.currentTarget.value)}
                error={groupError}
                style={{ width: 220 }}
              />
              <Button
                size="xs"
                variant="default"
                disabled={!newGroup.trim() || making}
                loading={making}
                onClick={async () => {
                  setMaking(true);
                  setGroupError(null);
                  try {
                    const made = await onMakeGroup(newGroup.trim());
                    onGroup(row.key, Number(made.id));
                    setNaming(false);
                    setNewGroup('');
                  } catch (e) {
                    setGroupError(e?.body?.error || 'That group could not be made.');
                  } finally {
                    setMaking(false);
                  }
                }}
              >
                Make it
              </Button>
            </Group>
          )}
          <Text size="xs" c="dimmed" pb={6}>
            {row.status === 'combine'
              ? chosenGroup && chosenGroup !== row.channel.group_id
                ? 'Chosen by you; the lowest-numbered of these channels already in that group is the one kept.'
                : `Suggested: ${row.group_why || 'where most of them are'}. ${row.channel.name} (${row.channel.number}) is kept.`
              : chosenGroup && chosenGroup !== row.channel.group_id
                ? 'Chosen by you; numbered after the last channel of that group when applied.'
                : `Suggested: ${row.channel.group_why || 'its streams’ group'}. Number ${row.channel.number}.`}
          </Text>
        </Group>
      )}
      {row.status === 'conflict' ? (
        <Stack gap={6}>
          <Text size="xs" c="orange">
            These streams could belong to any of these channels, so nothing is
            done with them. Rename one of the channels, or give one a tvg-id or
            an alias, to settle which.
          </Text>
          {(row.candidates || []).map((candidate) => (
            <Group key={candidate.id} gap="xs">
              <Logo url={candidate.logo_url} name={candidate.name} />
              <Text size="sm">{candidate.name}</Text>
              <Text size="xs" c="dimmed">
                {candidate.number} · {candidate.group}
              </Text>
            </Group>
          ))}
          {row.before.streams.map((stream) => (
            <StreamLine key={stream.id} stream={stream} />
          ))}
        </Stack>
      ) : (
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
          <Stack gap={4} style={{ minWidth: 0 }}>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase">
              Before · {row.before.streams.length} stream
              {row.before.streams.length === 1 ? '' : 's'}
            </Text>
            {row.before.channel && (
              <GuideLine guide={row.before.channel.epg} />
            )}
            {row.before.streams.length === 0 ? (
              <Text size="xs" c="dimmed">
                {row.status === 'new' ? 'No channel yet' : 'No streams'}
              </Text>
            ) : (
              row.before.streams.map((stream) => (
                <StreamLine key={stream.id} stream={stream} />
              ))
            )}
          </Stack>
          <Stack gap={4} style={{ minWidth: 0 }}>
            <Text size="xs" fw={700} c="dimmed" tt="uppercase">
              After · in the order they are tried
            </Text>
            <Stack gap={6} mb={4}>
              <TextInput
                size="xs"
                label="Name"
                aria-label={`Name for ${row.channel?.name || 'this channel'}`}
                value={chosenName ?? row.channel?.name ?? ''}
                onChange={(event) => onName(row.key, event.currentTarget.value)}
                style={{ maxWidth: 300 }}
              />
              <GuidePicker
                row={row}
                chosen={chosenGuide}
                onChoose={onGuide}
              />
              {row.status !== 'new' &&
                (chosenName !== undefined || chosenGuide !== undefined) && (
                  <Text size="xs" c="orange">
                    The channel you have is renamed or re-guided when this row is
                    applied.
                  </Text>
                )}
            </Stack>
            {row.streams.map((stream) => {
              const at = moving.indexOf(stream);
              return (
                <StreamLine
                  key={`${stream.id}-${stream.removed}`}
                  onDrop={(id) => onDrop(row.key, id)}
                  stream={stream}
                  move={
                    at === -1 ? (
                      // Keeps the names lined up with the ones that can move
                      <Box w={44} style={{ flexShrink: 0 }} />
                    ) : (
                      <Move
                        stream={stream}
                        first={at === 0}
                        last={at === moving.length - 1}
                        onMove={(id, by) => onMove(row.key, id, by)}
                      />
                    )
                  }
                />
              );
            })}
          </Stack>
        </SimpleGrid>
      )}
    </Box>
  );
};

const ChannelManagerTable = () => {
  const theme = useMantineTheme();

  const [options, setOptions] = useState(null);
  const [levers, setLevers] = useState(null);
  const [plan, setPlan] = useState(null);
  const [previewed, setPreviewed] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showLevers, setShowLevers] = useState(false);
  // Remounts the levers after a reset, so their text boxes show the reset values
  const [leverReset, setLeverReset] = useState(0);
  const [show, setShow] = useState('changes');
  // Which group is being looked at; '' is every one. A new channel counts under the
  // group it is suggested for, or the one chosen for it on the page, so that narrowing
  // to a group shows what would go into it as well as what is in it.
  const [group, setGroup] = useState('');
  const [search, setSearch] = useState('');
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [ticked, setTicked] = useState(new Set());
  // Streams put in another order by hand, by row: {key: [stream ids]}
  const [orders, setOrders] = useState({});
  // New channels put in another group than suggested, by row: {key: group id}
  const [groupChoice, setGroupChoice] = useState({});
  const [expandAll, setExpandAll] = useState(false);
  // Streams taken out of a row on the page: {key: [stream ids]}
  const [drops, setDrops] = useState({});
  // Names typed on a row: {key: name}. A guide chosen on a row: {key: guide id, or null
  // for no guide} -- null is a choice, so what was chosen is "the key is there", not its value
  const [nameChoice, setNameChoice] = useState({});
  const [guideChoice, setGuideChoice] = useState({});
  const [clearing, setClearing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const tableRef = useRef(null);

  // The plan is worked out on the server and only when asked: it reads every stream, and
  // a lever moved should not set it off on every keystroke
  const preview = useCallback(async (withLevers) => {
    setLoading(true);
    setError(null);
    try {
      const result = await API.previewChannelManager(withLevers);
      setPlan(result);
      setPreviewed(JSON.stringify(withLevers));
      setTicked(new Set());
      setOrders({});
      setGroupChoice({});
      setDrops({});
      setNameChoice({});
      setGuideChoice({});
      tableRef.current?.setSelectedTableIds?.([]);
      // Kept, so the page opens the way it was left
      API.saveChannelManagerSettings(withLevers).catch(() => {});
    } catch (e) {
      setError(e?.body?.error || 'Could not work out the channels.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const data = await API.getChannelManagerOptions();
        setOptions(data);
        setLevers(data.settings);
        await preview(data.settings);
      } catch (e) {
        setError(e?.body?.error || 'Could not load the Channel Manager.');
      }
    })();
  }, [preview]);

  const leversChanged =
    levers && previewed && JSON.stringify(levers) !== previewed;

  // Groups made on the page, which are not in what the server sent with it
  const [madeGroups, setMadeGroups] = useState([]);

  // Only the groups you actually have channels in. Every group there is ran to hundreds
  // on this setup, most of them a provider's own names that no channel of yours is in,
  // and finding your own group among them was the hard part. Making a new one is the
  // last entry rather than a button of its own, since that is where you look when none
  // of them is the one you want.
  const groupOptions = useMemo(
    () => [
      ...[...(options?.channel_groups || []), ...madeGroups]
        .map((g) => ({ value: String(g.id), label: g.name }))
        .sort((a, b) => a.label.localeCompare(b.label)),
      { value: NEW_GROUP, label: '+ A new group…' },
    ],
    [options, madeGroups]
  );
  // Only the groups the plan actually has something in, named for the page. A new
  // channel counts under the group it would go into.
  const groupsOnShow = useMemo(() => {
    const names = new Map();
    for (const row of plan?.rows || []) {
      const chosen = groupChoice[row.key];
      const id = chosen ?? row.channel?.group_id ?? row.before?.channel?.group_id;
      if (id == null) continue;
      if (!names.has(String(id))) {
        names.set(
          String(id),
          row.channel?.group || row.before?.channel?.group || String(id)
        );
      }
    }
    return [...names.entries()]
      .map(([value, label]) => ({ value, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [plan, groupChoice]);

  const groupNames = useMemo(
    () =>
      Object.fromEntries(
        [...(options?.all_groups || []), ...madeGroups].map((g) => [g.id, g.name])
      ),
    [options, madeGroups]
  );

  // A group made from a row: kept so every row can choose it, and chosen for that row
  const makeGroup = useCallback(async (name) => {
    const made = await API.addChannelGroup({ name });
    if (!made?.id) throw new Error('No group came back');
    setMadeGroups((all) => [...all, { id: made.id, name: made.name || name }]);
    return made;
  }, []);

  const rows = useMemo(() => {
    const all = (plan?.rows || []).map((given) => {
      const out = drops[given.key];
      const raw = out?.length
        ? {
            ...given,
            dropped: out.length,
            streams: given.streams.map((s) =>
              out.includes(s.id) ? { ...s, dropped: true } : s
            ),
          }
        : given;
      const chosen = groupChoice[raw.key];
      let row =
        chosen && chosen !== raw.channel?.group_id
          ? { ...raw, chosenGroup: groupNames[chosen] || String(chosen) }
          : raw;
      // What was set by hand is what the row says it would come out as
      const name = nameChoice[row.key];
      const guide = guideChoice[row.key];
      if (row.channel && (name !== undefined || guide !== undefined)) {
        row = {
          ...row,
          byHand: true,
          channel: {
            ...row.channel,
            ...(name === undefined ? {} : { name }),
            ...(guide === undefined
              ? {}
              : { epg: guide && { ...guide, how: 'chosen' } }),
          },
        };
      }
      const order = orders[row.key];
      if (!order) return { ...row, id: row.key };
      // The streams as they were put by hand; the fallback and anything taken off stay
      // where the plan has them, at the end
      const byId = Object.fromEntries(row.streams.map((s) => [s.id, s]));
      const moved = order.map((id) => byId[id]).filter(Boolean);
      const rest = row.streams.filter((s) => !movable(s));
      return {
        ...row,
        id: row.key,
        streams: [...moved, ...rest],
        reordered: true,
      };
    });
    const byStatus = all.filter((row) =>
      show === 'all'
        ? true
        : show === 'changes'
          ? row.status === 'new' ||
            row.status === 'merge' ||
            row.status === 'combine' ||
            row.status === 'conflict' ||
            row.reordered ||
            row.byHand
          : row.status === show
    );
    const inGroup = group
      ? byStatus.filter((row) => {
          const chosen = groupChoice[row.key];
          const its = chosen ?? row.channel?.group_id ?? row.before?.channel?.group_id;
          return String(its ?? '') === group;
        })
      : byStatus;
    const wanted = search.trim().toLowerCase();
    if (!wanted) return inGroup;
    return inGroup.filter((row) =>
      [
        row.channel?.name,
        row.before?.channel?.name,
        ...row.before.streams.map((s) => s.name),
      ]
        .filter(Boolean)
        .some((name) => name.toLowerCase().includes(wanted))
    );
  }, [
    plan,
    show,
    search,
    orders,
    groupChoice,
    groupNames,
    drops,
    nameChoice,
    guideChoice,
    group,
  ]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const paginatedRows = useMemo(
    () => rows.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize),
    [rows, pageIndex, pageSize]
  );

  // Only what can be applied counts as chosen: a conflict or an unchanged row cannot be
  const tickedKeys = useMemo(
    () =>
      rows
        .filter(
          (row) =>
            ticked.has(row.key) &&
            (row.status === 'new' ||
              row.status === 'merge' ||
              row.status === 'combine' ||
              row.reordered ||
              row.dropped ||
              row.byHand)
        )
        .map((row) => row.key),
    [rows, ticked]
  );

  const apply = async () => {
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      const given = Object.fromEntries(
        tickedKeys.filter((key) => orders[key]).map((key) => [key, orders[key]])
      );
      const groups = Object.fromEntries(
        tickedKeys
          .filter((key) => groupChoice[key])
          .map((key) => [key, groupChoice[key]])
      );
      const dropped = Object.fromEntries(
        tickedKeys
          .filter((key) => drops[key]?.length)
          .map((key) => [key, drops[key]])
      );
      const named = Object.fromEntries(
        tickedKeys
          .filter((key) => nameChoice[key] !== undefined)
          .map((key) => [key, nameChoice[key]])
      );
      const guided = Object.fromEntries(
        tickedKeys
          .filter((key) => guideChoice[key] !== undefined)
          .map((key) => [key, guideChoice[key]?.id ?? null])
      );
      await API.applyChannelManager(
        levers,
        tickedKeys,
        given,
        groups,
        dropped,
        named,
        guided
      );
      await preview(levers);
    } catch (e) {
      setError(e?.body?.error || 'Could not apply those channels.');
    } finally {
      setBusy(false);
    }
  };

  // A stream moved one place up or down. The row is ticked with it: an order put by hand
  // is something to apply, and would otherwise be easy to lose.
  const moveStream = useCallback(
    (key, id, by) => {
      const row = (plan?.rows || []).find((r) => r.key === key);
      if (!row) return;
      const current =
        orders[key] || row.streams.filter(movable).map((s) => s.id);
      const from = current.indexOf(id);
      const to = from + by;
      if (from === -1 || to < 0 || to >= current.length) return;
      const next = [...current];
      [next[from], next[to]] = [next[to], next[from]];
      setOrders((all) => ({ ...all, [key]: next }));
      setTicked((all) => {
        const now = new Set(all).add(key);
        tableRef.current?.setSelectedTableIds?.([...now]);
        return now;
      });
    },
    [plan, orders]
  );

  const tick = useCallback((key) => {
    setTicked((all) => {
      const now = new Set(all).add(key);
      tableRef.current?.setSelectedTableIds?.([...now]);
      return now;
    });
  }, []);

  // A stream out of a row, or back in; the row is ticked, as a change to apply
  const dropStream = useCallback(
    (key, id) => {
      setDrops((all) => {
        const now = new Set(all[key] || []);
        if (now.has(id)) now.delete(id);
        else now.add(id);
        return { ...all, [key]: [...now] };
      });
      tick(key);
    },
    [tick]
  );

  // A name typed, or a guide chosen, on a row; the row is ticked, as a change to apply.
  // Both are kept even when they are what the plan already said, so that setting a name
  // back to what it was does not silently stop being a choice.
  const chooseName = useCallback(
    (key, name) => {
      setNameChoice((all) => ({ ...all, [key]: name }));
      tick(key);
    },
    [tick]
  );

  const chooseGuide = useCallback(
    (key, guide) => {
      setGuideChoice((all) => ({ ...all, [key]: guide }));
      tick(key);
    },
    [tick]
  );

  // Not suggested again: a new channel or a conflict whole, a channel's +/- streams only
  const ignoreRow = useCallback(async (row) => {
    setError(null);
    try {
      await API.ignoreChannelManager('ignore', {
        key: row.key,
        name:
          row.channel?.name ||
          row.before?.channel?.name ||
          row.before?.streams?.[0]?.name ||
          row.key,
        kind: row.status,
        streams:
          row.status === 'merge'
            ? row.streams.filter((s) => s.added || s.removed).map((s) => s.id)
            : [],
      });
      setPlan((current) => ({
        ...current,
        rows: current.rows.filter((r) => r.key !== row.key),
        ignored: [
          ...(current.ignored || []).filter((i) => i.key !== row.key),
          {
            key: row.key,
            name: row.channel?.name || row.key,
            kind: row.status,
            at: new Date().toISOString(),
          },
        ],
      }));
      setTicked((all) => {
        const now = new Set(all);
        now.delete(row.key);
        tableRef.current?.setSelectedTableIds?.([...now]);
        return now;
      });
    } catch (e) {
      setError(e?.body?.error || 'Could not ignore that.');
    }
  }, []);

  const ignoreRowRef = useRef(ignoreRow);
  ignoreRowRef.current = ignoreRow;

  const chooseGroup = useCallback((key, group) => {
    setGroupChoice((all) => ({ ...all, [key]: group }));
    setTicked((all) => {
      const now = new Set(all).add(key);
      tableRef.current?.setSelectedTableIds?.([...now]);
      return now;
    });
  }, []);

  const columns = useMemo(
    () => [
      { id: 'select', size: 50, enableSorting: false },
      { id: 'expand', size: 30, enableSorting: false },
      {
        header: 'Status',
        accessorKey: 'status',
        size: 150,
        cell: ({ row }) => {
          const r = row.original;
          return (
            <Group gap={4} wrap="nowrap">
              <Badge size="xs" variant="light" color={STATUS[r.status].color}>
                {STATUS[r.status].label}
              </Badge>
              {r.reordered && (
                <Badge size="xs" variant="light" color="cyan">
                  Reordered
                </Badge>
              )}
              {r.adds > 0 && (
                <Text size="xs" c="green">
                  +{r.adds}
                </Text>
              )}
              {r.removes > 0 && (
                <Text size="xs" c="red">
                  −{r.removes}
                </Text>
              )}
              {r.dropped > 0 && (
                <Text size="xs" c="red">
                  ✕{r.dropped}
                </Text>
              )}
              {r.status !== 'unchanged' && (
                <Tooltip label="Don't suggest this again">
                  <ActionIcon
                    size="xs"
                    variant="subtle"
                    color="gray"
                    aria-label={`Ignore ${r.channel?.name || r.key}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      ignoreRowRef.current(r);
                    }}
                  >
                    <EyeOff size={12} />
                  </ActionIcon>
                </Tooltip>
              )}
            </Group>
          );
        },
      },
      {
        header: 'Before',
        accessorKey: 'before',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const r = row.original;
          const channel = r.before.channel;
          const providers = new Set(r.before.streams.map((s) => s.account))
            .size;
          return (
            <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
              <Logo url={channel?.logo_url} name={channel?.name} />
              <Box style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                  {channel
                    ? channel.name
                    : r.status === 'conflict'
                      ? 'Could be several'
                      : 'No channel yet'}
                </Text>
                <Text size="xs" c="dimmed" lineClamp={1}>
                  {channel?.number != null ? `${channel.number} · ` : ''}
                  {channel?.group || (channel ? 'No group' : '')}
                </Text>
                <Text size="xs" c="dimmed" lineClamp={1}>
                  {r.before.streams.length} stream
                  {r.before.streams.length === 1 ? '' : 's'}
                  {providers > 0 &&
                    ` · ${providers} provider${providers === 1 ? '' : 's'}`}
                </Text>
                {channel && <GuideLine guide={channel.epg} />}
              </Box>
            </Group>
          );
        },
      },
      {
        header: 'After',
        accessorKey: 'channel',
        grow: true,
        enableSorting: false,
        cell: ({ row }) => {
          const channel = row.original.channel;
          if (!channel) {
            return (
              <Text size="xs" c="orange">
                Left alone
              </Text>
            );
          }
          const epg = channel.epg;
          return (
            <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
              <Logo url={channel.logo_url} name={channel.name} />
              <Box style={{ minWidth: 0 }}>
                <Group gap={6} wrap="wrap">
                  <Text size="sm" fw={500} style={{ wordBreak: 'break-word' }}>
                    {channel.name}
                  </Text>
                  <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
                    {row.original.chosenGroup
                      ? `new number · ${row.original.chosenGroup}`
                      : `${channel.number}${channel.group ? ` · ${channel.group}` : ''}`}
                  </Text>
                </Group>
                {(row.original.combining || []).length > 0 && (
                  <Text size="xs" c="orange" style={{ wordBreak: 'break-word' }}>
                    {row.original.combining
                      .map((one) => `${one.name} (${one.number})`)
                      .join(', ')}{' '}
                    {row.original.combining.length === 1 ? 'is' : 'are'} deleted
                  </Text>
                )}
                <GuideLine
                  guide={epg}
                  how={
                    epg?.how === 'chosen'
                      ? ' (chosen)'
                      : epg?.how && epg.how !== 'kept'
                        ? ` (by ${epg.how})`
                        : ''
                  }
                />
              </Box>
            </Group>
          );
        },
      },
      {
        header: 'Streams, in order',
        accessorKey: 'streams',
        size: 230,
        enableSorting: false,
        cell: ({ row }) => {
          const streams = row.original.streams.filter((s) => !s.removed);
          return (
            <Group gap={3} wrap="nowrap" style={{ overflow: 'hidden' }}>
              {streams.slice(0, 7).map((stream) => (
                <Tooltip
                  key={stream.id}
                  label={`${stream.name} · ${stream.account}${stream.added ? ' · added' : ''}`}
                >
                  <Badge
                    size="xs"
                    variant={stream.added ? 'filled' : 'light'}
                    color={
                      stream.custom
                        ? 'yellow'
                        : QUALITY_COLOR[stream.quality] || 'dark'
                    }
                  >
                    {stream.custom ? 'fb' : stream.quality || '?'}
                  </Badge>
                </Tooltip>
              ))}
              {streams.length > 7 && (
                <Text size="xs" c="dimmed">
                  +{streams.length - 7}
                </Text>
              )}
            </Group>
          );
        },
      },
    ],
    []
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
    enableRowVirtualization: false,
    renderTopToolbar: false,
    manualSorting: false,
    manualFiltering: false,
    manualPagination: true,
    onRowSelectionChange: (selected) => setTicked(new Set(selected)),
    expandAll,
    expandedRowRenderer: ({ row }) => (
      <Expanded
        row={row.original}
        onMove={moveStream}
        groups={groupOptions}
        chosenGroup={groupChoice[row.original.key]}
        onGroup={chooseGroup}
        onMakeGroup={makeGroup}
        onDrop={dropStream}
        chosenName={nameChoice[row.original.key]}
        onName={chooseName}
        chosenGuide={guideChoice[row.original.key]}
        onGuide={chooseGuide}
      />
    ),
    headerCellRenderFns: {
      status: renderHeaderCell,
      before: renderHeaderCell,
      channel: renderHeaderCell,
      streams: renderHeaderCell,
    },
  });

  useEffect(() => {
    tableRef.current = table;
  }, [table]);

  const summary = plan?.summary || {};
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
                  aria-label="Which channels"
                  value={show}
                  onChange={(value) => {
                    if (value) setShow(value);
                    setPageIndex(0);
                  }}
                  allowDeselect={false}
                  data={[
                    { value: 'changes', label: 'What would change' },
                    { value: 'new', label: 'New channels' },
                    { value: 'merge', label: 'Merge' },
                    { value: 'combine', label: 'Combine duplicates' },
                    { value: 'conflict', label: 'Conflicts' },
                    { value: 'unchanged', label: 'Unchanged' },
                    { value: 'all', label: 'Everything' },
                    {
                      value: 'ignored',
                      label: `Ignored (${plan?.ignored?.length ?? 0})`,
                    },
                  ]}
                  size="xs"
                  style={{ width: 170 }}
                />
                <Select
                  aria-label="Which group"
                  placeholder="Every group"
                  value={group}
                  onChange={(value) => {
                    setGroup(value || '');
                    setPageIndex(0);
                  }}
                  searchable
                  clearable
                  data={groupsOnShow}
                  size="xs"
                  style={{ width: 200 }}
                />
              </Group>

              <Group gap="sm">
                <Button
                  variant="default"
                  size="xs"
                  leftSection={
                    expandAll ? (
                      <ChevronsDownUp size={16} />
                    ) : (
                      <ChevronsUpDown size={16} />
                    )
                  }
                  onClick={() => setExpandAll(!expandAll)}
                >
                  {expandAll ? 'Collapse all' : 'Expand all'}
                </Button>
                <Button
                  leftSection={<SlidersHorizontal size={16} />}
                  variant="default"
                  size="xs"
                  onClick={() => setShowLevers(!showLevers)}
                >
                  {showLevers ? 'Hide Levers' : 'Levers'}
                </Button>
                <Button
                  leftSection={<Play size={16} />}
                  variant={leversChanged ? 'filled' : 'light'}
                  size="xs"
                  loading={loading}
                  disabled={!levers}
                  onClick={() => preview(levers)}
                >
                  Preview
                </Button>
                <Button
                  leftSection={<Check size={18} />}
                  variant="light"
                  size="xs"
                  p={5}
                  disabled={!tickedKeys.length || busy || leversChanged}
                  loading={busy}
                  onClick={() => setConfirming(true)}
                  color={theme.tailwind.green[5]}
                  style={{
                    borderWidth: '1px',
                    borderColor: theme.tailwind.green[5],
                    color: 'white',
                  }}
                >
                  Apply {tickedKeys.length ? `(${tickedKeys.length})` : ''}
                </Button>
              </Group>
            </Box>

            {/* What the plan comes to */}
            <Box
              style={{ padding: '8px 16px', borderBottom: '1px solid #3f3f46' }}
            >
              <Text size="xs" c="dimmed">
                {plan
                  ? `${(summary.streams || 0).toLocaleString()} streams looked at · ${
                      summary.merge || 0
                    } channels gain ${(summary.streams_added || 0).toLocaleString()} streams · ${
                      summary.new || 0
                    } new · ${summary.conflict || 0} conflicts · ${summary.unchanged || 0} unchanged`
                  : 'Working out the channels…'}
                {' — '}
                Open a row to see every stream before and after. Nothing changes
                until channels are ticked and applied.
              </Text>
            </Box>

            {(error || leversChanged) && (
              <Stack
                gap="xs"
                p="md"
                style={{ borderBottom: '1px solid #3f3f46' }}
              >
                {error && <Alert color="red">{error}</Alert>}
                {leversChanged && (
                  <Alert color="blue">
                    The levers have changed. Preview again to see what they come
                    to before applying anything.
                  </Alert>
                )}
              </Stack>
            )}

            {showLevers && options && levers && (
              <Box p="md" style={{ borderBottom: '1px solid #3f3f46' }}>
                <Group justify="flex-end" mb="sm">
                  <Button
                    variant="subtle"
                    size="xs"
                    leftSection={<RotateCcw size={14} />}
                    onClick={() => {
                      // Back to matching as DispatcharrUtils does; what is looked at stays
                      const scope = Object.fromEntries(
                        SCOPE_LEVERS.map((name) => [name, levers[name]])
                      );
                      setLevers({ ...options.defaults, ...scope });
                      setLeverReset((n) => n + 1);
                    }}
                  >
                    Back to the defaults
                  </Button>
                </Group>
                <ChannelManagerLevers
                  key={leverReset}
                  options={options}
                  value={levers}
                  onChange={setLevers}
                />
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
                  {show === 'ignored' ? (
                    <Stack gap={6} p="sm">
                      <Group justify="space-between" wrap="wrap">
                        <Text
                          size="xs"
                          c="dimmed"
                          style={{ flex: 1, minWidth: 200 }}
                        >
                          Suggestions you said not to make again. For a channel
                          you have, only the streams shown then are left out;
                          one the provider adds later is still suggested.
                        </Text>
                        <Button
                          size="xs"
                          variant="subtle"
                          color="red"
                          disabled={!(plan?.ignored || []).length}
                          onClick={() => setClearing(true)}
                        >
                          Clear ignored list
                        </Button>
                      </Group>
                      {(plan?.ignored || []).length === 0 && (
                        <Text size="sm" c="dimmed" ta="center" p="xl">
                          Nothing is ignored.
                        </Text>
                      )}
                      {(plan?.ignored || []).map((item) => (
                        <Group key={item.key} gap={6} wrap="nowrap">
                          <Badge
                            size="xs"
                            variant="light"
                            color={STATUS[item.kind]?.color || 'gray'}
                          >
                            {STATUS[item.kind]?.label || item.kind || '?'}
                          </Badge>
                          <Text
                            size="xs"
                            style={{
                              flex: 1,
                              minWidth: 0,
                              wordBreak: 'break-word',
                            }}
                          >
                            {item.name}
                            {item.streams?.length
                              ? ` · ${item.streams.length} stream${item.streams.length === 1 ? '' : 's'}`
                              : ''}
                          </Text>
                          <Button
                            size="compact-xs"
                            variant="light"
                            onClick={async () => {
                              await API.ignoreChannelManager('unignore', {
                                key: item.key,
                              });
                              await preview(levers);
                            }}
                          >
                            Stop ignoring
                          </Button>
                        </Group>
                      ))}
                    </Stack>
                  ) : rows.length === 0 && !loading ? (
                    <Center p="xl">
                      <Text size="sm" c="dimmed">
                        {plan ? 'Nothing to show.' : ''}
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

      <ConfirmationDialog
        opened={clearing}
        onClose={() => setClearing(false)}
        onConfirm={async () => {
          setClearing(false);
          await API.ignoreChannelManager('clear');
          await preview(levers);
        }}
        title="Clear the ignored list?"
        message="Everything you ignored is suggested again. Nothing on your channels changes."
        confirmLabel="Clear"
      />

      <ConfirmationDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={apply}
        title={`Apply ${tickedKeys.length} channel${tickedKeys.length === 1 ? '' : 's'}?`}
        message="Each ticked channel becomes what its row shows: new channels are made in the group shown, numbered after the last channel of that group, and channels you have gain the streams marked +. Streams you took out are not added, or come off the channel. A Combine row keeps one channel and deletes the others named on it, which cannot be undone except from a backup. It is worked out again as it is applied, so what is applied is what is true now. Custom fallback streams stay last."
        confirmLabel="Apply"
      />
    </>
  );
};

export default ChannelManagerTable;
