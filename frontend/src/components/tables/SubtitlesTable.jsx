import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  NativeSelect,
  Pagination,
  Paper,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import API from '../../api';
import CaptionsCard from './CaptionsCard';

// Which subtitles every channel's streams carry, read by Stream Check, and the language each
// channel speaks (fork/subtitles.md, step 1). Making captions from the sound and translating
// them come later; this shows what the channels carry, which decides how much of that is
// needed at all.

const PANEL = {
  backgroundColor: '#27272A',
  border: '1px solid #3f3f46',
  borderRadius: 'var(--mantine-radius-md)',
};

const PAGE_SIZES = ['25', '50', '100', '250', '500'];

export const LANGUAGES = {
  deu: 'German',
  dut: 'Dutch',
  eng: 'English',
  fra: 'French',
  ita: 'Italian',
  spa: 'Spanish',
  por: 'Portuguese',
  pol: 'Polish',
  tur: 'Turkish',
  swe: 'Swedish',
  nor: 'Norwegian',
  dan: 'Danish',
  fin: 'Finnish',
};
const languageName = (code) => LANGUAGES[code] || code || '';

const KIND_LABEL = { teletext: 'Teletext', dvb: 'DVB', cc: 'CC', text: 'Text' };
const KIND_COLOR = { teletext: 'blue', dvb: 'grape', cc: 'teal', text: 'cyan' };
const KIND_ABOUT = {
  teletext:
    "Teletext subtitles (page 888, 777, 150…), shown by arrTV from arr.72 (Teletext Subtitles in its player settings). Whether the track holds a subtitle page is not known until its pages are read.",
  dvb: 'DVB subtitles: pictures, shown by arrTV today.',
  cc: 'Closed captions in the picture (CEA-608/708), shown by arrTV today.',
  text: 'Text subtitles in the stream, shown by arrTV today.',
};

const FILTERS = [
  { value: 'all', label: 'Every channel' },
  { value: 'found', label: 'Has subtitles' },
  { value: 'none', label: 'None found' },
  { value: 'unchecked', label: 'Not checked yet' },
  { value: 'teletext', label: 'Teletext' },
  { value: 'dvb', label: 'DVB subtitles' },
  { value: 'cc', label: 'Closed captions' },
];

const SubtitleBadges = ({ subtitles }) => (
  <Group gap={4}>
    {subtitles.map((one) => (
      <Tooltip
        key={`${one.kind}-${one.lang}-${one.hearing_impaired}`}
        label={KIND_ABOUT[one.kind] || one.kind}
        multiline
        maw={300}
      >
        <Badge size="sm" variant="light" color={KIND_COLOR[one.kind] || 'gray'}>
          {KIND_LABEL[one.kind] || one.kind}
          {one.lang ? ` ${one.lang}` : ''}
          {one.hearing_impaired ? ' (HI)' : ''}
        </Badge>
      </Tooltip>
    ))}
  </Group>
);

const State = ({ row }) => {
  if (row.subtitles.length) return <SubtitleBadges subtitles={row.subtitles} />;
  return (
    <Text size="xs" c={row.state === 'none' ? 'dimmed' : 'orange'}>
      {row.state === 'none' ? 'None found' : 'Not checked yet'}
    </Text>
  );
};

const SubtitlesTable = () => {
  const [page, setPage] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [group, setGroup] = useState('');
  const [only, setOnly] = useState('all');
  const [language, setLanguage] = useState('');
  const [open, setOpen] = useState(new Set());
  const [pageSize, setPageSize] = useState('50');
  const [pageIndex, setPageIndex] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPage(await API.getSubtitles());
      setError(null);
    } catch (e) {
      setError(e?.body?.error || 'The subtitles could not be read.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const setSpoken = async (channel, spoken) => {
    try {
      setPage(await API.setSpokenLanguage(channel, spoken || ''));
    } catch (e) {
      setError(e?.body?.error || 'That language was not kept.');
    }
  };

  const rows = useMemo(() => page?.rows || [], [page]);
  const groups = useMemo(
    () =>
      [
        ...new Map(
          rows
            .filter((r) => r.group_id)
            .map((r) => [String(r.group_id), r.group])
        ).entries(),
      ]
        .map(([value, label]) => ({ value, label }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [rows]
  );
  const shown = useMemo(() => {
    const words = search.toLowerCase().split(/\s+/).filter(Boolean);
    return rows.filter((row) => {
      if (
        words.length &&
        !words.every((w) => row.name.toLowerCase().includes(w))
      )
        return false;
      if (group && String(row.group_id) !== group) return false;
      if (language && row.spoken !== language) return false;
      if (only === 'found' || only === 'none' || only === 'unchecked')
        return row.state === only;
      if (only !== 'all') return row.subtitles.some((s) => s.kind === only);
      return true;
    });
  }, [rows, search, group, language, only]);

  useEffect(() => setPageIndex(1), [search, group, language, only, pageSize]);
  const size = Number(pageSize);
  const pages = Math.max(1, Math.ceil(shown.length / size));
  const visible = shown.slice((pageIndex - 1) * size, pageIndex * size);
  const summary = page?.summary;

  const toggle = (id) => {
    const next = new Set(open);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  };

  return (
    <Box
      style={{
        display: 'flex',
        justifyContent: 'center',
        minHeight: 'calc(100vh - 200px)',
      }}
    >
      <Stack gap="md" style={{ maxWidth: '1200px', width: '100%' }}>
        <CaptionsCard />
        <Paper style={PANEL}>
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
                size="xs"
                placeholder="Filter by name..."
                aria-label="Search channels"
                value={search}
                onChange={(event) => setSearch(event.currentTarget.value)}
                style={{ width: 200 }}
              />
              <Select
                size="xs"
                aria-label="Which group"
                placeholder="Every group"
                value={group || null}
                onChange={(value) => setGroup(value || '')}
                data={groups}
                searchable
                clearable
                style={{ width: 200 }}
              />
              <Select
                size="xs"
                aria-label="What it carries"
                value={only}
                onChange={(value) => setOnly(value || 'all')}
                data={FILTERS}
                allowDeselect={false}
                style={{ width: 170 }}
              />
              <Select
                size="xs"
                aria-label="Spoken language"
                placeholder="Any language"
                value={language || null}
                onChange={(value) => setLanguage(value || '')}
                data={Object.entries(LANGUAGES).map(([value, label]) => ({
                  value,
                  label,
                }))}
                clearable
                style={{ width: 150 }}
              />
            </Group>
            <Button
              size="xs"
              variant="default"
              leftSection={<RefreshCw size={14} />}
              onClick={load}
              loading={loading}
            >
              Look again
            </Button>
          </Box>
          <Box
            style={{ padding: '8px 16px', borderBottom: '1px solid #3f3f46' }}
          >
            <Text size="xs" c="dimmed">
              {summary
                ? `${summary.channels} channels · ${summary.found} carry subtitles (${summary.teletext} teletext, ${summary.dvb} DVB, ${summary.cc} closed captions) · ${summary.none} none found · ${summary.unchecked} not checked yet`
                : 'Reading…'}
              {' — '}
              Stream Check fills this in as it checks each stream. Making
              captions from the sound and translating them come later.
            </Text>
          </Box>
          {error && (
            <Box p="md">
              <Alert color="red">{error}</Alert>
            </Box>
          )}
          <Table striped verticalSpacing={4} fz="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={30} />
                <Table.Th>Channel</Table.Th>
                <Table.Th>Spoken</Table.Th>
                <Table.Th>Broadcast subtitles</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {visible.map((row) => (
                <React.Fragment key={row.id}>
                  <Table.Tr>
                    <Table.Td>
                      <UnstyledButton
                        aria-label={`Streams of ${row.name}`}
                        onClick={() => toggle(row.id)}
                      >
                        {open.has(row.id) ? (
                          <ChevronDown size={14} />
                        ) : (
                          <ChevronRight size={14} />
                        )}
                      </UnstyledButton>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{row.name}</Text>
                      <Text size="xs" c="dimmed">
                        {row.number != null ? `${row.number} · ` : ''}
                        {row.group}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Select
                        size="xs"
                        aria-label={`Language of ${row.name}`}
                        placeholder="Not known"
                        value={row.spoken || null}
                        onChange={(value) => setSpoken(row.id, value)}
                        data={[
                          ...Object.entries(LANGUAGES).map(
                            ([value, label]) => ({ value, label })
                          ),
                          ...(row.spoken && !LANGUAGES[row.spoken]
                            ? [{ value: row.spoken, label: row.spoken }]
                            : []),
                        ]}
                        clearable={row.spoken_from === 'set'}
                        style={{ width: 130 }}
                      />
                      <Text size="xs" c="dimmed">
                        {{
                          set: 'set here',
                          audio: 'from the audio track',
                          country: 'guessed from the country',
                        }[row.spoken_from] || ''}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <State row={row} />
                    </Table.Td>
                  </Table.Tr>
                  {open.has(row.id) && (
                    <Table.Tr>
                      <Table.Td />
                      <Table.Td colSpan={3}>
                        <Stack gap={4}>
                          {row.streams.length === 0 && (
                            <Text size="xs" c="dimmed">
                              No streams from a provider.
                            </Text>
                          )}
                          {row.streams.map((stream) => (
                            <Group key={stream.id} gap="sm" wrap="nowrap">
                              <Text
                                size="xs"
                                style={{ minWidth: 0 }}
                                lineClamp={1}
                              >
                                {stream.name}{' '}
                                <Text span size="xs" c="dimmed">
                                  · {stream.account}
                                  {stream.audio_languages.length
                                    ? ` · sound ${stream.audio_languages.map(languageName).join(', ')}`
                                    : ''}
                                </Text>
                              </Text>
                              {stream.checked ? (
                                stream.subtitles.length ? (
                                  <SubtitleBadges
                                    subtitles={stream.subtitles}
                                  />
                                ) : (
                                  <Text size="xs" c="dimmed">
                                    none
                                  </Text>
                                )
                              ) : (
                                <Text size="xs" c="orange">
                                  not checked
                                </Text>
                              )}
                            </Group>
                          ))}
                        </Stack>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </React.Fragment>
              ))}
            </Table.Tbody>
          </Table>
          <Group
            justify="space-between"
            p="sm"
            style={{ borderTop: '1px solid #3f3f46' }}
          >
            <NativeSelect
              size="xs"
              aria-label="Page size"
              value={pageSize}
              onChange={(event) => setPageSize(event.currentTarget.value)}
              data={PAGE_SIZES}
            />
            <Pagination
              size="sm"
              total={pages}
              value={pageIndex}
              onChange={setPageIndex}
            />
            <Text size="xs" c="dimmed">
              {shown.length
                ? `${(pageIndex - 1) * size + 1} to ${Math.min(pageIndex * size, shown.length)} of ${shown.length}`
                : 'No channels'}
            </Text>
          </Group>
        </Paper>
      </Stack>
    </Box>
  );
};

export default SubtitlesTable;
