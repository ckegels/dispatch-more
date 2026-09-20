import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  closestCorners,
  DndContext,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  useDroppable,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import { restrictToVerticalAxis } from '@dnd-kit/modifiers';
import {
  arrayMove,
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical, RotateCcw } from 'lucide-react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  LoadingOverlay,
  NumberInput,
  Paper,
  Select,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import API from '../../api';
import ConfirmationDialog from '../ConfirmationDialog';

// Your lineup as an arrangement rather than a list: drag a channel where it belongs and
// the numbers follow. The rule the whole page rests on is that channels keep the numbers
// they have -- a channel dropped between two others takes that place and pushes the rest
// along only as far as it must, so a lineup built over months is not thrown away because
// one channel moved. What the numbers would come to is worked out on the server, so there
// is one set of rules and not one here and another there.

const Channel = ({ channel, was }) => {
  const { transform, transition, setNodeRef, attributes, listeners, isDragging } =
    useSortable({ id: channel.id });
  const moved = was != null && was !== channel.number;
  return (
    <Group
      ref={setNodeRef}
      gap="xs"
      wrap="nowrap"
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.5 : 1,
        padding: '4px 6px',
        borderRadius: 4,
        background: moved ? 'rgba(34,139,230,0.10)' : 'transparent',
      }}
    >
      <Box
        {...attributes}
        {...listeners}
        aria-label={`Move ${channel.name}`}
        style={{ cursor: 'grab', display: 'flex', flexShrink: 0 }}
      >
        <GripVertical size={14} />
      </Box>
      <Text size="sm" w={70} style={{ flexShrink: 0 }} c={moved ? 'blue.4' : undefined}>
        {channel.number ?? '—'}
        {moved && ` → ${was}`}
      </Text>
      {channel.logo_url && (
        <img
          src={channel.logo_url}
          alt=""
          style={{ width: 24, height: 18, objectFit: 'contain', flexShrink: 0 }}
        />
      )}
      <Text size="sm" style={{ minWidth: 0, wordBreak: 'break-word' }}>
        {channel.name}
      </Text>
      {channel.clashes && (
        <Tooltip label="Another channel has this number too. A media server sees one lineup, so it shows only one of them.">
          <Badge size="xs" color="red" variant="light" style={{ flexShrink: 0 }}>
            same number
          </Badge>
        </Tooltip>
      )}
      {!channel.epg && (
        <Badge size="xs" color="orange" variant="light" style={{ flexShrink: 0 }}>
          no guide
        </Badge>
      )}
    </Group>
  );
};

// A group takes a drop as a whole as well as between its channels, so a channel can be
// moved into one that is empty
const GroupBox = ({ id, children }) => {
  const { setNodeRef, isOver } = useDroppable({ id: `group-${id}` });
  return (
    <Paper
      ref={setNodeRef}
      p={4}
      withBorder
      style={isOver ? { outline: '1px solid var(--mantine-color-blue-6)' } : undefined}
    >
      {children}
    </Paper>
  );
};

const GuideLayoutTable = () => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [group, setGroup] = useState('');
  // The arrangement as it stands on the page: {group id: [channel ids]}
  const [order, setOrder] = useState({});
  // What each channel's number would become, and what it is now
  const [numbers, setNumbers] = useState({});
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [from, setFrom] = useState(1);
  const [step, setStep] = useState(1);
  // Channels dragged into another group: {channel id: group id}
  const [moves, setMoves] = useState({});

  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor),
    useSensor(KeyboardSensor)
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const found = await API.getGuideLayout();
      setData(found);
      setOrder(
        Object.fromEntries(
          (found.groups || []).map((g) => [g.id, g.channels.map((c) => c.id)])
        )
      );
      setNumbers({});
      setMoves({});
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Could not load the lineup.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const groups = useMemo(() => data?.groups || [], [data]);
  const shown = useMemo(
    () => (group ? groups.filter((g) => String(g.id) === group) : groups),
    [groups, group]
  );
  const byId = useMemo(() => {
    const all = {};
    for (const one of groups) for (const channel of one.channels) all[channel.id] = channel;
    return all;
  }, [groups]);

  // What the numbers would be, asked of the server after each drag
  const arrange = useCallback(async (body) => {
    try {
      const answer = await API.arrangeGuideLayout(body);
      setNumbers((all) => ({ ...all, ...answer.numbers }));
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'Could not work out those numbers.');
    }
  }, []);

  // Which group a thing being dragged over belongs to: a channel's group, or the group
  // itself when the drop is on its empty space
  const groupOf = useCallback(
    (id) => {
      const text = String(id);
      if (text.startsWith('group-')) return Number(text.slice(6));
      const found = Object.entries(order).find(([, ids]) => ids.includes(id));
      return found ? Number(found[0]) : null;
    },
    [order]
  );

  // Dragged over another group: it goes there and then, so what is under the cursor is
  // what would happen. Where it lands within the group is settled on the drop.
  const onDragOver = (event) => {
    const { active, over } = event;
    if (!over) return;
    const from = groupOf(active.id);
    const to = groupOf(over.id);
    if (from == null || to == null || from === to) return;
    setOrder((all) => {
      const leaving = (all[from] || []).filter((id) => id !== active.id);
      const joining = [...(all[to] || [])];
      const at = joining.indexOf(over.id);
      joining.splice(at === -1 ? joining.length : at, 0, active.id);
      return { ...all, [from]: leaving, [to]: joining };
    });
  };

  const onDragEnd = (event) => {
    const { active, over } = event;
    if (!over) return;
    const to = groupOf(over.id);
    if (to == null) return;
    const ids = order[to] || [];
    const at = ids.indexOf(active.id);
    const onto = ids.indexOf(over.id);
    const arranged =
      at === -1 || onto === -1 || at === onto ? ids : arrayMove(ids, at, onto);
    setOrder((all) => ({ ...all, [to]: arranged }));
    // A channel that has left its group is on the move as well as being renumbered
    if (byId[active.id] && byId[active.id].group_id !== to) {
      setMoves((all) => ({ ...all, [active.id]: to }));
    }
    arrange({ order: arranged, moved: active.id });
  };

  const renumber = (groupId) => {
    const ids = order[groupId] || [];
    if (ids.length) arrange({ order: ids, start: from, step });
  };

  const changing = useMemo(
    () =>
      Object.entries(numbers).filter(
        ([id, number]) => byId[id] && byId[id].number !== number
      ),
    [numbers, byId]
  );
  // A channel can move group without its number changing, and that is a change too
  const changingCount = useMemo(
    () => new Set([...changing.map(([id]) => id), ...Object.keys(moves)]).size,
    [changing, moves]
  );

  const apply = async () => {
    setConfirming(false);
    setBusy(true);
    try {
      await API.applyGuideLayout(Object.fromEntries(changing), moves);
      await load();
    } catch (e) {
      setError(e?.body?.error || 'Could not apply that.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Box style={{ display: 'flex', justifyContent: 'center' }}>
        <Paper style={{ width: '100%', maxWidth: 1200 }} p="sm" mx={{ base: 'xs', md: 0 }}>
          <LoadingOverlay visible={loading} />
          {error && (
            <Alert color="red" mb="sm" withCloseButton onClose={() => setError(null)}>
              {error}
            </Alert>
          )}

          <Group justify="space-between" mb="sm" wrap="wrap" gap="sm">
            <Group gap="sm" wrap="wrap">
              <Select
                size="xs"
                aria-label="Which group"
                placeholder="Every group"
                value={group}
                onChange={(value) => setGroup(value || '')}
                data={groups.map((g) => ({ value: String(g.id), label: g.name }))}
                searchable
                clearable
                style={{ width: 240 }}
              />
              {(data?.clashes || []).length > 0 && (
                <Text size="xs" c="red.4">
                  {data.clashes.length} number
                  {data.clashes.length === 1 ? ' is' : 's are'} used by more than one
                  channel
                </Text>
              )}
            </Group>
            <Group gap="sm" wrap="wrap">
              {changingCount > 0 && (
                <Button
                  size="xs"
                  variant="subtle"
                  leftSection={<RotateCcw size={14} />}
                  onClick={load}
                >
                  Start again
                </Button>
              )}
              <Button
                size="xs"
                disabled={!changingCount || busy}
                onClick={() => setConfirming(true)}
              >
                Apply ({changingCount})
              </Button>
            </Group>
          </Group>

          <Text size="xs" c="dimmed" mb="sm">
            Drag a channel where it belongs, in its own group or into another one. It
            takes that place and the others move only as far as they must, so the numbers
            you have are kept wherever they still work. Nothing is written until you
            apply.
          </Text>

          {shown.length === 0 && !loading ? (
            <Center p="xl">
              <Text size="sm" c="dimmed">
                No channels to arrange.
              </Text>
            </Center>
          ) : (
            // One context around every group, so a channel can be dragged out of its own
            // into another rather than only up and down within it
            <DndContext
              sensors={sensors}
              collisionDetection={closestCorners}
              modifiers={[restrictToVerticalAxis]}
              onDragOver={onDragOver}
              onDragEnd={onDragEnd}
            >
            <Stack gap="lg">
              {shown.map((one) => (
                <Box key={one.id}>
                  <Group justify="space-between" mb={4} wrap="wrap" gap="xs">
                    <Group gap="xs">
                      <Text size="sm" fw={600}>
                        {one.name}
                      </Text>
                      <Text size="xs" c="dimmed">
                        {one.channels.length} channel
                        {one.channels.length === 1 ? '' : 's'}
                        {one.first != null && ` · ${one.first}–${one.last}`}
                        {one.room_after != null &&
                          ` · room for ${one.room_after} more before the next group`}
                      </Text>
                    </Group>
                    <Group gap="xs" align="flex-end">
                      <NumberInput
                        size="xs"
                        label="From"
                        aria-label={`Renumber ${one.name} from`}
                        value={from}
                        onChange={(value) => setFrom(Number(value) || 1)}
                        style={{ width: 90 }}
                      />
                      <NumberInput
                        size="xs"
                        label="Step"
                        aria-label={`Renumber ${one.name} in steps of`}
                        min={1}
                        value={step}
                        onChange={(value) => setStep(Number(value) || 1)}
                        style={{ width: 80 }}
                      />
                      <Button
                        size="xs"
                        variant="default"
                        aria-label={`Renumber every channel of ${one.name}`}
                        onClick={() => renumber(one.id)}
                      >
                        Renumber them all
                      </Button>
                    </Group>
                  </Group>
                  <GroupBox id={one.id}>
                      <SortableContext
                        items={order[one.id] || []}
                        strategy={verticalListSortingStrategy}
                      >
                        <Stack gap={0}>
                          {(order[one.id] || []).map((id) => (
                            <Channel
                              key={id}
                              channel={{
                                ...byId[id],
                                number: numbers[id] ?? byId[id]?.number,
                              }}
                              was={
                                numbers[id] != null && numbers[id] !== byId[id]?.number
                                  ? byId[id]?.number
                                  : null
                              }
                            />
                          ))}
                        </Stack>
                      </SortableContext>
                  </GroupBox>
                </Box>
              ))}
            </Stack>
            </DndContext>
          )}
        </Paper>
      </Box>

      <ConfirmationDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={apply}
        title={`Renumber ${changingCount} channel${changingCount === 1 ? '' : 's'}?`}
        message="Every channel shown with a new number gets it. Your media servers read the numbers, so their own lineups change with it. Nothing else about the channels is touched."
        confirmLabel="Apply"
        actionKey="apply-guide-layout"
      />
    </>
  );
};

export default GuideLayoutTable;
